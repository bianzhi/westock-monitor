#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储层：SQLite + 仅缓存最近5日分钟级数据。

表结构：
  sector_meta          板块元数据（代码/名称/一级/流通市值/规模分档）
  minute_snapshot      分钟级快照（差分前的当日累计值）
  minute_delta         分钟级增量（差分后的本分钟净流入）

设计原则：
  1. 日级数据不长期落地，实时从接口获取
  2. 分钟级数据仅缓存最近5日（MINUTE_CACHE_DAYS）
  3. 流通市值等慢变元数据缓存到 sector_meta，定时刷新

核心 API:
  - upsert_minute_snapshots(records)
      批量写入分钟快照，自动差分计算 minute_delta
  - get_last_minute_snapshot() -> {code: {main_net_flow, ...}}
      获取上一分钟的快照（用于差分）
  - get_minute_deltas(code, date) -> List[Dict]
      获取某板块某日的所有分钟增量
  - get_minute_deltas_batch(codes, date) -> Dict
      批量获取分钟增量
  - cleanup_old_minute_data(days)
      清理 days 天前的分钟数据
  - upsert_sector_meta(meta_list)
      批量写入板块元数据
  - get_sector_meta(code) -> Dict
      获取单板块元数据
  - get_all_sector_meta() -> List[Dict]
      获取所有板块元数据
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Union

from config import DB_PATH, DATA_DIR, get_scale

logger = logging.getLogger(__name__)

# 线程锁，确保 SQLite 写入串行
_db_lock = threading.Lock()


class Storage:
    """SQLite 存储层。

    使用单连接 + 线程锁，避免多线程并发写入冲突。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DB_PATH)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ============================================================
    # 连接与建表
    # ============================================================
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=30
            )
            self._conn.row_factory = sqlite3.Row
            # 开启 WAL 模式，提升并发读写性能
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:
                pass
        return self._conn

    def _init_db(self) -> None:
        """初始化数据库表结构。"""
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()

            # 板块元数据表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_meta (
                    code            TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    l1              TEXT,
                    circ_mv_yi      REAL,       -- 流通市值(亿元)
                    scale           TEXT,       -- 大盘/中盘/小盘
                    turnover_yi     REAL,       -- 当日成交额(亿元)，定时刷新
                    updated_at      TEXT,
                    created_at      TEXT
                )
            """)

            # 板块流通市值缓存表（日级，成分股累加）
            # source: tushare (主方案，个股 circ_mv 直取) / westock_reverse (兜底，反推) / mixed / unknown
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_circ_mv (
                    code            TEXT NOT NULL,
                    trade_date      TEXT NOT NULL,      -- YYYYMMDD
                    circ_mv         REAL,               -- 流通市值(元)
                    circ_mv_yi      REAL,               -- 流通市值(亿元)
                    stock_count     INTEGER,            -- 成分股数
                    valid_count     INTEGER,            -- 有效累加数
                    skip_count      INTEGER,            -- 跳过数
                    fail_rate       REAL,               -- 失败率
                    is_estimated    INTEGER,            -- 0/1 是否估算值
                    source          TEXT,               -- 数据来源 tushare/westock_reverse/tencent/mixed/unknown
                    change_pct      REAL,               -- 板块涨跌幅(%)，按流通市值加权
                    turnover_rate   REAL,               -- 板块换手率(%)，按流通市值加权
                    updated_at      TEXT,
                    PRIMARY KEY (code, trade_date)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_circ_mv_date
                ON sector_circ_mv(trade_date)
            """)
            # 旧表无 source/change_pct/turnover_rate 列时补列（兼容升级）
            for _col, _type in (("source", "TEXT"), ("change_pct", "REAL"), ("turnover_rate", "REAL")):
                try:
                    cur.execute(f"ALTER TABLE sector_circ_mv ADD COLUMN {_col} {_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在

            # 分钟快照表（差分前的当日累计值）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS minute_snapshot (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    code            TEXT NOT NULL,
                    trade_date      TEXT NOT NULL,      -- YYYYMMDD
                    timestamp       TEXT NOT NULL,      -- ISO datetime
                    main_net_flow   REAL,               -- 当日累计主力净流入(元)
                    turnover        REAL,               -- 当日累计成交额(元)
                    circ_mv         REAL,               -- 流通市值(元)
                    main_inflow     REAL,
                    main_outflow    REAL,
                    minute_delta    REAL,               -- 本分钟净流入增量(元)
                    turnover_delta  REAL,               -- 本分钟成交额增量(元)
                    is_open_anchor  INTEGER DEFAULT 0,  -- 0/1 开盘第一条快照（无差分基准）
                    UNIQUE(code, timestamp)
                )
            """)
            # 旧表无 turnover_delta / is_open_anchor 列时补列（兼容升级）
            for _col in ("turnover_delta", "is_open_anchor"):
                try:
                    cur.execute(f"ALTER TABLE minute_snapshot ADD COLUMN {_col} REAL")
                except sqlite3.OperationalError:
                    pass  # 列已存在

            # 索引：按代码+时间范围查询
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_minute_code_date
                ON minute_snapshot(code, trade_date, timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_minute_date
                ON minute_snapshot(trade_date)
            """)

            # 强度告警日志表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alert_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    code            TEXT NOT NULL,
                    name            TEXT,
                    trade_date      TEXT NOT NULL,      -- YYYYMMDD
                    timestamp       TEXT NOT NULL,      -- ISO datetime
                    old_level       TEXT,               -- 旧档位
                    new_level       TEXT,               -- 新档位
                    old_value       REAL,               -- 旧强度值
                    new_value       REAL,               -- 新强度值
                    net_rate_n      REAL,               -- 近n日聚合净额率(%)
                    net_flow_n_yi   REAL,               -- 近n日净流入(亿)
                    scale           TEXT                -- 大盘/中盘/小盘
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_date
                ON alert_log(trade_date, timestamp)
            """)

            # 概念板块日记录表（收盘后快照，用于近 N 日净值计算）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS concept_daily (
                    code            TEXT NOT NULL,
                    name            TEXT,
                    trade_date      TEXT NOT NULL,      -- YYYYMMDD
                    net_flow        REAL,               -- 当日主力净流入(元)
                    turnover        REAL,               -- 当日成交额(元)
                    PRIMARY KEY (code, trade_date)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_concept_daily_date
                ON concept_daily(trade_date)
            """)

            # 全板块日级净流入表（pt018 二级 + pt02 概念统一记录，日线图数据源）
            # 保留近 30 个交易日；与 concept_daily 并存（后者是概念板块专用，
            # 由概念快照写入；sector_daily 是通用表，供日线图查询）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_daily (
                    code            TEXT NOT NULL,
                    name            TEXT,
                    trade_date      TEXT NOT NULL,      -- YYYYMMDD
                    net_flow        REAL,               -- 当日主力净流入(元)
                    turnover        REAL,               -- 当日成交额(元)
                    PRIMARY KEY (code, trade_date)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sector_daily_date
                ON sector_daily(trade_date)
            """)

            conn.commit()
        logger.info("storage initialized: %s", self.db_path)

    # ============================================================
    # 分钟级快照写入
    # ============================================================
    def upsert_minute_snapshots(self, records: List[Dict]) -> int:
        """批量写入分钟快照，自动计算差分 minute_delta 和 turnover_delta。

        Args:
            records: 快照列表，每条含:
              code, timestamp, main_net_flow, turnover, circ_mv,
              main_inflow, main_outflow,
              minute_delta(可空), turnover_delta(可空), is_open_anchor(0/1)

        Returns:
            成功写入的行数
        """
        if not records:
            return 0

        # 获取上一分钟的快照用于差分（对未提供 minute_delta 的记录）
        codes = list({r["code"] for r in records})
        prev_snapshot = self.get_last_minute_snapshot(codes)

        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            count = 0
            now_iso = datetime.now().isoformat()

            for r in records:
                code = r["code"]
                ts = r["timestamp"]
                ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
                trade_date = ts_str[:10].replace("-", "")

                main_net_flow = r.get("main_net_flow")
                turnover = r.get("turnover")
                circ_mv = r.get("circ_mv")
                main_inflow = r.get("main_inflow")
                main_outflow = r.get("main_outflow")

                # 差分计算
                minute_delta = r.get("minute_delta")
                turnover_delta = r.get("turnover_delta")
                prev = prev_snapshot.get(code)
                if minute_delta is None and main_net_flow is not None:
                    if prev and prev.get("main_net_flow") is not None:
                        minute_delta = main_net_flow - prev["main_net_flow"]
                if turnover_delta is None and turnover is not None:
                    if prev and prev.get("turnover") is not None:
                        turnover_delta = turnover - prev["turnover"]
                # 开盘锚点：无 prev 快照时标记
                is_open_anchor = r.get("is_open_anchor", 0)
                if prev is None:
                    is_open_anchor = 1

                try:
                    cur.execute("""
                        INSERT INTO minute_snapshot
                            (code, trade_date, timestamp, main_net_flow,
                             turnover, circ_mv, main_inflow, main_outflow,
                             minute_delta, turnover_delta, is_open_anchor)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code, timestamp) DO UPDATE SET
                            main_net_flow = excluded.main_net_flow,
                            turnover = excluded.turnover,
                            circ_mv = excluded.circ_mv,
                            main_inflow = excluded.main_inflow,
                            main_outflow = excluded.main_outflow,
                            minute_delta = excluded.minute_delta,
                            turnover_delta = excluded.turnover_delta,
                            is_open_anchor = excluded.is_open_anchor
                    """, (
                        code, trade_date, ts_str, main_net_flow,
                        turnover, circ_mv, main_inflow, main_outflow,
                        minute_delta, turnover_delta, is_open_anchor,
                    ))
                    count += 1
                except sqlite3.Error as e:
                    logger.warning("upsert_minute_snapshots %s: %s", code, e)

            conn.commit()
        logger.debug("upsert_minute_snapshots: %d/%d", count, len(records))
        return count

    # ============================================================
    # 分钟级快照查询
    # ============================================================
    def get_last_minute_snapshot(
        self, codes: Optional[List[str]] = None
    ) -> Dict[str, Dict]:
        """获取每个板块最新一条分钟快照。

        Args:
            codes: 限定代码列表，None 表示全部

        Returns:
            {code: {main_net_flow, turnover, circ_mv, timestamp, ...}}
        """
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()

            if codes:
                placeholders = ",".join("?" * len(codes))
                cur.execute(
                    f"""
                    SELECT m.* FROM minute_snapshot m
                    INNER JOIN (
                        SELECT code, MAX(timestamp) AS max_ts
                        FROM minute_snapshot
                        WHERE code IN ({placeholders})
                        GROUP BY code
                    ) latest ON m.code = latest.code
                        AND m.timestamp = latest.max_ts
                    """,
                    codes,
                )
            else:
                cur.execute("""
                    SELECT m.* FROM minute_snapshot m
                    INNER JOIN (
                        SELECT code, MAX(timestamp) AS max_ts
                        FROM minute_snapshot
                        GROUP BY code
                    ) latest ON m.code = latest.code
                        AND m.timestamp = latest.max_ts
                """)

            result: Dict[str, Dict] = {}
            for row in cur.fetchall():
                d = dict(row)
                result[d["code"]] = d
            return result

    def get_minute_deltas(
        self,
        code: str,
        trade_date: Optional[str] = None,
    ) -> List[Dict]:
        """获取某板块某日的所有分钟增量（差分后的本分钟净流入 + 成交额增量）。

        Args:
            code: 板块代码
            trade_date: YYYYMMDD，None 表示今日

        Returns:
            分钟增量列表，按时间升序
        """
        if trade_date is None:
            trade_date = date.today().strftime("%Y%m%d")

        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT timestamp, main_net_flow, turnover, circ_mv,
                       main_inflow, main_outflow, minute_delta, trade_date,
                       turnover_delta, is_open_anchor
                FROM minute_snapshot
                WHERE code = ? AND trade_date = ?
                ORDER BY timestamp ASC
                """,
                (code, trade_date),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_minute_deltas_batch(
        self,
        codes: List[str],
        trade_date: Optional[str] = None,
    ) -> Dict[str, List[Dict]]:
        """批量获取分钟增量。

        Args:
            codes: 板块代码列表
            trade_date: YYYYMMDD

        Returns:
            {code: [分钟增量...]}
        """
        if trade_date is None:
            trade_date = date.today().strftime("%Y%m%d")

        if not codes:
            return {}

        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            placeholders = ",".join("?" * len(codes))
            cur.execute(
                f"""
                SELECT code, timestamp, main_net_flow, turnover, circ_mv,
                       main_inflow, main_outflow, minute_delta, trade_date,
                       turnover_delta, is_open_anchor
                FROM minute_snapshot
                WHERE code IN ({placeholders}) AND trade_date = ?
                ORDER BY code, timestamp ASC
                """,
                codes + [trade_date],
            )

            result: Dict[str, List[Dict]] = {c: [] for c in codes}
            for row in cur.fetchall():
                d = dict(row)
                result[d["code"]].append(d)
            return result

    # ============================================================
    # 板块元数据
    # ============================================================
    def upsert_sector_meta(self, meta_list: List[Dict]) -> int:
        """批量写入/更新板块元数据。

        Args:
            meta_list: 元数据列表，每条含 code/name/l1/circ_mv_yi/turnover_yi

        Returns:
            写入行数
        """
        if not meta_list:
            return 0

        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            count = 0
            now_iso = datetime.now().isoformat()

            for m in meta_list:
                code = m.get("code")
                if not code:
                    continue
                name = m.get("name", "")
                l1 = m.get("l1", "")
                circ_mv_yi = m.get("circ_mv_yi")
                turnover_yi = m.get("turnover_yi")

                # 规模分档
                scale = None
                if circ_mv_yi is not None and circ_mv_yi > 0:
                    scale = get_scale(circ_mv_yi)

                try:
                    cur.execute("""
                        INSERT INTO sector_meta
                            (code, name, l1, circ_mv_yi, scale,
                             turnover_yi, updated_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET
                            name = excluded.name,
                            l1 = excluded.l1,
                            circ_mv_yi = COALESCE(excluded.circ_mv_yi, sector_meta.circ_mv_yi),
                            scale = COALESCE(excluded.scale, sector_meta.scale),
                            turnover_yi = COALESCE(excluded.turnover_yi, sector_meta.turnover_yi),
                            updated_at = excluded.updated_at
                    """, (
                        code, name, l1, circ_mv_yi, scale,
                        turnover_yi, now_iso, now_iso,
                    ))
                    count += 1
                except sqlite3.Error as e:
                    logger.warning("upsert_sector_meta %s: %s", code, e)

            conn.commit()
        logger.debug("upsert_sector_meta: %d/%d", count, len(meta_list))
        return count

    def get_sector_meta(self, code: str) -> Optional[Dict]:
        """获取单板块元数据。"""
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM sector_meta WHERE code = ?", (code,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_all_sector_meta(self) -> List[Dict]:
        """获取所有板块元数据。"""
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM sector_meta ORDER BY code")
            return [dict(row) for row in cur.fetchall()]

    def get_circ_mv_map(self) -> Dict[str, float]:
        """获取所有板块流通市值映射 {code: circ_mv_yi}。"""
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT code, circ_mv_yi FROM sector_meta WHERE circ_mv_yi IS NOT NULL"
            )
            return {row["code"]: row["circ_mv_yi"] for row in cur.fetchall()}

    # ============================================================
    # 板块流通市值缓存（日级，成分股反推累加）
    # ============================================================
    def upsert_sector_circ_mv(self, records: List[Dict]) -> int:
        """批量写入/更新板块流通市值缓存。

        Args:
            records: 流通市值记录列表，每条含:
              code, trade_date(YYYYMMDD), circ_mv(元), circ_mv_yi(亿元),
              stock_count, valid_count, skip_count, fail_rate,
              is_estimated(0/1), source(tushare/westock_reverse/mixed/unknown)

        Returns:
            写入行数
        """
        if not records:
            return 0

        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            count = 0
            now_iso = datetime.now().isoformat()

            for r in records:
                code = r.get("code")
                trade_date = r.get("trade_date")
                if not code or not trade_date:
                    continue
                try:
                    cur.execute("""
                        INSERT INTO sector_circ_mv
                            (code, trade_date, circ_mv, circ_mv_yi,
                             stock_count, valid_count, skip_count,
                             fail_rate, is_estimated, source,
                             change_pct, turnover_rate, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code, trade_date) DO UPDATE SET
                            circ_mv = excluded.circ_mv,
                            circ_mv_yi = excluded.circ_mv_yi,
                            stock_count = excluded.stock_count,
                            valid_count = excluded.valid_count,
                            skip_count = excluded.skip_count,
                            fail_rate = excluded.fail_rate,
                            is_estimated = excluded.is_estimated,
                            source = excluded.source,
                            change_pct = excluded.change_pct,
                            turnover_rate = excluded.turnover_rate,
                            updated_at = excluded.updated_at
                    """, (
                        code, trade_date,
                        r.get("circ_mv"), r.get("circ_mv_yi"),
                        r.get("stock_count"), r.get("valid_count"),
                        r.get("skip_count"), r.get("fail_rate"),
                        1 if r.get("is_estimated") else 0,
                        r.get("source") or "unknown",
                        r.get("change_pct"), r.get("turnover_rate"),
                        now_iso,
                    ))
                    count += 1
                except sqlite3.Error as e:
                    logger.warning("upsert_sector_circ_mv %s: %s", code, e)

            conn.commit()
        logger.debug("upsert_sector_circ_mv: %d/%d", count, len(records))
        return count

    def get_sector_circ_mv(
        self, code: str, trade_date: Optional[str] = None
    ) -> Optional[Dict]:
        """获取单板块流通市值缓存。

        Args:
            code: 板块 pt 代码
            trade_date: YYYYMMDD，默认今日

        Returns:
            流通市值记录 dict 或 None
        """
        if trade_date is None:
            trade_date = date.today().strftime("%Y%m%d")
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM sector_circ_mv WHERE code = ? AND trade_date = ?",
                (code, trade_date),
            )
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["is_estimated"] = bool(d.get("is_estimated"))
                return d
            return None

    def get_all_sector_circ_mv(
        self, trade_date: Optional[str] = None
    ) -> Dict[str, Dict]:
        """获取全部板块流通市值缓存。

        Args:
            trade_date: YYYYMMDD，默认今日

        Returns:
            {code: {circ_mv, circ_mv_yi, is_estimated, ...}, ...}
        """
        if trade_date is None:
            trade_date = date.today().strftime("%Y%m%d")
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM sector_circ_mv WHERE trade_date = ?",
                (trade_date,),
            )
            result = {}
            for row in cur.fetchall():
                d = dict(row)
                d["is_estimated"] = bool(d.get("is_estimated"))
                result[d["code"]] = d
            return result

    def get_latest_sector_circ_mv(self) -> Dict[str, Dict]:
        """获取每个板块最新一日的流通市值缓存。

        用于盘前/盘后无当日数据时的兜底。

        Returns:
            {code: {circ_mv, circ_mv_yi, trade_date, is_estimated, ...}, ...}
        """
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT c.* FROM sector_circ_mv c
                INNER JOIN (
                    SELECT code, MAX(trade_date) AS max_date
                    FROM sector_circ_mv
                    GROUP BY code
                ) latest
                ON c.code = latest.code AND c.trade_date = latest.max_date
            """)
            result = {}
            for row in cur.fetchall():
                d = dict(row)
                d["is_estimated"] = bool(d.get("is_estimated"))
                result[d["code"]] = d
            return result

    # ============================================================
    # 数据清理
    # ============================================================
    def cleanup_old_minute_data(self, days: int) -> int:
        """清理 days 天前的分钟数据。

        Args:
            days: 保留最近多少天

        Returns:
            删除行数
        """
        cutoff_date = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM minute_snapshot WHERE trade_date < ?",
                (cutoff_date,),
            )
            deleted = cur.rowcount
            conn.commit()
        if deleted > 0:
            logger.info("cleanup_old_minute_data: deleted %d rows (cutoff=%s)",
                        deleted, cutoff_date)
        return deleted

    # ============================================================
    # 强度告警日志
    # ============================================================
    def insert_alert(self, alert: Dict) -> int:
        """写入一条强度档位变化告警。

        Args:
            alert: 告警记录，含 code, name, trade_date, timestamp,
                   old_level, new_level, old_value, new_value,
                   net_rate_n, net_flow_n_yi, scale

        Returns:
            插入行数（通常 1）
        """
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO alert_log
                    (code, name, trade_date, timestamp,
                     old_level, new_level, old_value, new_value,
                     net_rate_n, net_flow_n_yi, scale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert["code"], alert.get("name"),
                alert.get("trade_date", ""), alert["timestamp"],
                alert.get("old_level"), alert.get("new_level"),
                alert.get("old_value"), alert.get("new_value"),
                alert.get("net_rate_n"), alert.get("net_flow_n_yi"),
                alert.get("scale"),
            ))
            conn.commit()
            return cur.lastrowid

    def get_alerts(
        self,
        trade_date: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """获取告警日志。

        Args:
            trade_date: YYYYMMDD，None 表示全部
            limit: 最多返回条数

        Returns:
            告警列表，按时间倒序
        """
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            if trade_date:
                cur.execute(
                    """SELECT * FROM alert_log
                    WHERE trade_date = ?
                    ORDER BY timestamp DESC LIMIT ?""",
                    (trade_date, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM alert_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cur.fetchall()]

    # ============================================================
    # 概念板块日记录（收盘后快照，近 20 交易日滚动）
    # ============================================================
    def upsert_concept_daily_batch(self, records: List[Dict]) -> int:
        """批量写入概念板块日记录（按 code + trade_date 去重）。

        Args:
            records: [{code, name, trade_date(YYYYMMDD), net_flow(元), turnover(元)}, ...]

        Returns:
            写入行数
        """
        if not records:
            return 0
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            count = 0
            for r in records:
                try:
                    cur.execute(
                        """INSERT OR REPLACE INTO concept_daily
                           (code, name, trade_date, net_flow, turnover)
                           VALUES (?, ?, ?, ?, ?)""",
                        (r["code"], r.get("name"), r["trade_date"],
                         r.get("net_flow"), r.get("turnover")),
                    )
                    count += 1
                except sqlite3.Error as e:
                    logger.warning("concept_daily upsert %s: %s", r.get("code"), e)
            conn.commit()
        logger.info("concept_daily: upserted %d records", count)
        return count

    def get_concept_daily_batch(self, codes: List[str], days: int = 20) -> Dict[str, List[Dict]]:
        """批量获取概念板块近 N 交易日记录。

        Returns:
            {code: [{trade_date, net_flow, turnover}, ...]}
        """
        if not codes:
            return {}
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            placeholders = ",".join("?" * len(codes))
            cur.execute(
                f"""SELECT code, name, trade_date, net_flow, turnover
                    FROM concept_daily
                    WHERE code IN ({placeholders})
                    ORDER BY trade_date DESC
                    LIMIT ?""",
                codes + [days * len(codes)],  # 宽松的上限
            )
            result: Dict[str, List[Dict]] = {c: [] for c in codes}
            for row in cur.fetchall():
                d = dict(row)
                result[d["code"]].append(d)
            return result

    def cleanup_concept_daily(self, keep_days: int = 20) -> int:
        """清理超过 keep_days 天的概念板块日记录。"""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=keep_days)).strftime("%Y%m%d")
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM concept_daily WHERE trade_date < ?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()
        if deleted:
            logger.info("concept_daily: cleaned %d old records", deleted)
        return deleted

    # ============================================================
    # 全板块日级净流入（sector_daily，日线图数据源，保留近 30 交易日）
    # ============================================================
    def upsert_sector_daily_batch(self, records: List[Dict]) -> int:
        """批量写入全板块日净流入（按 code + trade_date 去重）。

        Args:
            records: [{code, name, trade_date(YYYYMMDD), net_flow(元), turnover(元)}, ...]

        Returns:
            写入行数
        """
        if not records:
            return 0
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            count = 0
            for r in records:
                try:
                    cur.execute(
                        """INSERT OR REPLACE INTO sector_daily
                           (code, name, trade_date, net_flow, turnover)
                           VALUES (?, ?, ?, ?, ?)""",
                        (r["code"], r.get("name"), r["trade_date"],
                         r.get("net_flow"), r.get("turnover")),
                    )
                    count += 1
                except sqlite3.Error as e:
                    logger.warning("sector_daily upsert %s: %s", r.get("code"), e)
            conn.commit()
        logger.info("sector_daily: upserted %d records", count)
        return count

    def get_sector_daily_batch(self, codes: List[str], days: int = 30) -> Dict[str, List[Dict]]:
        """批量获取板块近 N 交易日净流入记录（日线图数据源）。

        Returns:
            {code: [{trade_date, net_flow, turnover}, ...]}（trade_date 倒序）
        """
        if not codes:
            return {}
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            placeholders = ",".join("?" * len(codes))
            cur.execute(
                f"""SELECT code, name, trade_date, net_flow, turnover
                    FROM sector_daily
                    WHERE code IN ({placeholders})
                    ORDER BY trade_date DESC
                    LIMIT ?""",
                codes + [days * len(codes)],  # 宽松上限
            )
            result: Dict[str, List[Dict]] = {c: [] for c in codes}
            for row in cur.fetchall():
                d = dict(row)
                result[d["code"]].append(d)
            return result

    def get_sector_daily_asof(self, codes: List[str], asof_date: str, days: int = 30) -> Dict[str, List[Dict]]:
        """获取板块截至指定交易日的近 N 交易日净流入记录（历史回看数据源）。

        与 get_sector_daily_batch 不同：本方法按 asof_date 截止过滤，
        用于「历史回看」从落库表读数据，而非实时调 CLI。

        Args:
            codes: 板块代码列表
            asof_date: 截止交易日 YYYYMMDD（含）
            days: 往回取的天数

        Returns:
            {code: [{trade_date, net_flow, turnover}, ...]}（trade_date 倒序，截至 asof_date）
        """
        if not codes:
            return {}
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            placeholders = ",".join("?" * len(codes))
            cur.execute(
                f"""SELECT code, name, trade_date, net_flow, turnover
                    FROM sector_daily
                    WHERE code IN ({placeholders}) AND trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT ?""",
                codes + [asof_date, days * len(codes)],
            )
            result: Dict[str, List[Dict]] = {c: [] for c in codes}
            for row in cur.fetchall():
                d = dict(row)
                result[d["code"]].append(d)
            return result

    def cleanup_sector_daily(self, keep_days: int = 30) -> int:
        """清理超过 keep_days 天的全板块日净流入记录。"""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=keep_days)).strftime("%Y%m%d")
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM sector_daily WHERE trade_date < ?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()
        if deleted:
            logger.info("sector_daily: cleaned %d old records", deleted)
        return deleted

    # ============================================================
    # 统计信息
    # ============================================================
    def get_stats(self) -> Dict[str, Any]:
        """获取存储统计信息。"""
        with _db_lock:
            conn = self._get_conn()
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) AS cnt FROM sector_meta")
            meta_count = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM minute_snapshot")
            minute_count = cur.fetchone()["cnt"]

            cur.execute(
                "SELECT COUNT(DISTINCT trade_date) AS cnt FROM minute_snapshot"
            )
            minute_dates = cur.fetchone()["cnt"]

            cur.execute(
                "SELECT MIN(trade_date) AS min_d, MAX(trade_date) AS max_d "
                "FROM minute_snapshot"
            )
            row = cur.fetchone()
            date_range = {
                "min": row["min_d"] if row else None,
                "max": row["max_d"] if row else None,
            }

            return {
                "sector_meta_count": meta_count,
                "minute_snapshot_count": minute_count,
                "minute_dates_count": minute_dates,
                "date_range": date_range,
                "db_path": self.db_path,
            }


# ============================================================
# 单例
# ============================================================
_storage_instance: Optional[Storage] = None
_instance_lock = threading.Lock()


def get_storage() -> Storage:
    """获取 Storage 单例。"""
    global _storage_instance
    if _storage_instance is None:
        with _instance_lock:
            if _storage_instance is None:
                _storage_instance = Storage()
    return _storage_instance


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    storage = Storage()

    # 测试元数据写入
    test_meta = [
        {"code": "pt01801080", "name": "电子", "l1": "电子", "circ_mv_yi": 50000.0, "turnover_yi": 100.0},
        {"code": "pt01801081", "name": "半导体", "l1": "电子", "circ_mv_yi": 30000.0, "turnover_yi": 80.0},
    ]
    n = storage.upsert_sector_meta(test_meta)
    print(f"upsert meta: {n}")

    # 测试分钟快照写入
    test_snapshot = [
        {
            "code": "pt01801080",
            "timestamp": datetime(2026, 7, 24, 9, 31),
            "main_net_flow": 1e8,
            "turnover": 5e8,
            "circ_mv": 5e12,
            "main_inflow": 3e8,
            "main_outflow": 2e8,
        },
        {
            "code": "pt01801080",
            "timestamp": datetime(2026, 7, 24, 9, 32),
            "main_net_flow": 1.5e8,
            "turnover": 6e8,
            "circ_mv": 5e12,
            "main_inflow": 3.5e8,
            "main_outflow": 2e8,
        },
    ]
    n = storage.upsert_minute_snapshots(test_snapshot)
    print(f"upsert snapshot: {n}")

    # 查询差分
    deltas = storage.get_minute_deltas("pt01801080", "20260724")
    print(f"deltas: {deltas}")

    # 统计
    stats = storage.get_stats()
    print(f"stats: {json.dumps(stats, ensure_ascii=False, indent=2)}")
