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
                    UNIQUE(code, timestamp)
                )
            """)

            # 索引：按代码+时间范围查询
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_minute_code_date
                ON minute_snapshot(code, trade_date, timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_minute_date
                ON minute_snapshot(trade_date)
            """)

            conn.commit()
        logger.info("storage initialized: %s", self.db_path)

    # ============================================================
    # 分钟级快照写入
    # ============================================================
    def upsert_minute_snapshots(self, records: List[Dict]) -> int:
        """批量写入分钟快照，自动计算差分 minute_delta。

        Args:
            records: 快照列表，每条含:
              code, timestamp, main_net_flow, turnover, circ_mv,
              main_inflow, main_outflow, minute_delta(可空)

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
                if minute_delta is None and main_net_flow is not None:
                    prev = prev_snapshot.get(code)
                    if prev and prev.get("main_net_flow") is not None:
                        minute_delta = main_net_flow - prev["main_net_flow"]

                try:
                    cur.execute("""
                        INSERT INTO minute_snapshot
                            (code, trade_date, timestamp, main_net_flow,
                             turnover, circ_mv, main_inflow, main_outflow,
                             minute_delta)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code, timestamp) DO UPDATE SET
                            main_net_flow = excluded.main_net_flow,
                            turnover = excluded.turnover,
                            circ_mv = excluded.circ_mv,
                            main_inflow = excluded.main_inflow,
                            main_outflow = excluded.main_outflow,
                            minute_delta = excluded.minute_delta
                    """, (
                        code, trade_date, ts_str, main_net_flow,
                        turnover, circ_mv, main_inflow, main_outflow,
                        minute_delta,
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
        """获取某板块某日的所有分钟增量（差分后的本分钟净流入）。

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
                       main_inflow, main_outflow, minute_delta, trade_date
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
                       main_inflow, main_outflow, minute_delta, trade_date
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
