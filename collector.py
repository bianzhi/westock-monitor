#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集层：分钟级差分 + 日级实时拉取 + 板块列表刷新。

数据流：
  ┌─────────────┐   ┌──────────────┐   ┌─────────────┐
  │ westock CLI  │──▶│  collector   │──▶│  storage    │
  │ (MainNetFlow)│   │  (差分/聚合) │   │  (SQLite)   │
  └─────────────┘   └──────────────┘   └─────────────┘
  ┌─────────────┐         ▲
  │ 腾讯HTTP     │─────────┘
  │ (turnover/   │
  │  circ_mv)    │
  └─────────────┘

核心 API:
  - refresh_sectors() -> int
      从接口刷新板块列表，返回数量
  - collect_minute_snapshot() -> Dict
      采集一次分钟级快照（主力净流入 + 成交额 + 流通市值），差分后入库
  - collect_daily_records(code, n) -> List[Dict]
      实时拉取单板块近n日日级记录（今日+前n-1日）
  - collect_all_sectors_daily(n) -> Dict
      拉取所有板块近n日日级数据（用于宽表展示）
  - run_minute_loop(force=False)
      分钟级采集主循环

关键设计：
  1. 日级数据"能从接口获取的统统从接口获取"，本地不长期存
     - 今日净流入/成交额/流通市值：实时调 westock fund flow + 腾讯HTTP
     - 近3日/近5日净流入/净额率：优先调 westock MainNetFlow5D 等累计字段
     - 计算不到的：用本地缓存的分钟数据聚合回填
  2. 分钟级数据本地缓存最近5日，差分得到分钟净流入
  3. 板块列表刷新：search 反查 + 硬编码兜底
"""
import json
import logging
import threading
import time
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from trading_calendar import (
    get_last_n_trading_days, get_previous_trading_day, is_trading_day,
)

from config import (
    BASE_DIR, DATA_DIR, SECTORS_CACHE, MINUTE_INTERVAL, IDLE_SLEEP,
    TRADING_MORNING, TRADING_AFTERNOON, STRENGTH_WINDOW_N, DISPLAY_DAYS,
    SUMMARY_3D, SUMMARY_5D, MINUTE_CACHE_DAYS, get_scale,
    CIRC_MV_COLLECT_TIMES, CIRC_MV_CHECK_INTERVAL, TURNOVER_METHOD,
    FOCUSED_INTERVAL, FOCUSED_BACKOFF_BASE, FOCUSED_BACKOFF_MAX,
)
from sectors import DEFAULT_SECTORS, get_default_codes, get_default_sector_map
from westock import (
    fund_flow, sector_ranking, search_sector, sector_constituent,
    extract_main_net_flow, extract_main_inflow, extract_main_outflow,
)
from westock_fund_metrics import (
    calc_sector_metrics, calc_sector_metrics_batch,
    calc_turnover, calc_net_rate,
    extract_main_net_flow, extract_main_inflow, extract_main_outflow,
)
from strength import (
    calc_strength, calc_aggregate_net_rate, calc_aggregate_net_flow,
    calc_sector_strength,
)

logger = logging.getLogger(__name__)

# 单例 storage，延迟导入避免循环依赖
_storage = None
_storage_lock = threading.Lock()


def get_storage():
    """延迟初始化 storage 单例。"""
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                from storage import Storage
                _storage = Storage()
    return _storage


# ============================================================
# 板块列表刷新
# ============================================================
def refresh_sectors() -> Dict[str, Any]:
    """从接口刷新板块列表，写入 data/sectors.json，并返回 diff（新增/剔除）。

    策略：
      1. 遍历 31 个一级行业名，调 search --type sector
      2. 过滤 分类="申万二级行业清单" 的条目
      3. 合并去重，按代码排序
      4. 失败兜底用 DEFAULT_SECTORS
      5. 与旧缓存对比，算出 added/removed（板块清单变更感知 E1）

    Returns:
      {"sectors_count": N, "added": [...], "removed": [...], "source": str}
      其中 added/removed 是 [{code, name}, ...]，便于前端展示"新增 X 个 / 剔剔除 Y 个"
    """
    logger.info("refresh_sectors: 开始从接口拉取板块列表")

    # 31 个申万一级行业名
    l1_names = sorted({s["l1"] for s in DEFAULT_SECTORS})

    found: Dict[str, Dict] = {}
    for l1 in l1_names:
        try:
            results = search_sector(l1, raw=True)
            if not isinstance(results, list):
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                # 只收申万二级
                category = item.get("分类") or item.get("category") or ""
                if "申万二级" not in category:
                    continue
                code = item.get("code") or item.get("SecuCode")
                name = item.get("name") or item.get("Name")
                if code and name:
                    found[code] = {
                        "code": code, "name": name, "l1": l1,
                        "category": category,
                    }
        except Exception as e:
            logger.warning("refresh_sectors search %s failed: %s", l1, e)

    # 读旧缓存算 diff
    old_codes: set = set()
    old_name_map: Dict[str, str] = {}
    try:
        if SECTORS_CACHE.exists():
            with open(SECTORS_CACHE, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            for s in old_data.get("sectors", []):
                old_codes.add(s.get("code"))
                old_name_map[s.get("code")] = s.get("name", "")
    except Exception as e:
        logger.warning("refresh_sectors: 读旧缓存失败，跳过 diff: %s", e)

    new_codes = set(found.keys())
    added_codes = new_codes - old_codes
    removed_codes = old_codes - new_codes
    added = [{"code": c, "name": found[c]["name"]} for c in sorted(added_codes)]
    removed = [{"code": c, "name": old_name_map.get(c, c)} for c in sorted(removed_codes)]
    if added or removed:
        logger.info("refresh_sectors: diff detected — added=%d, removed=%d",
                    len(added), len(removed))

    # 兜底：如果接口失败，用默认列表
    if not found:
        logger.warning("refresh_sectors: 接口未返回数据，使用默认列表")
        sectors_data = {"sectors": DEFAULT_SECTORS, "updated_at": datetime.now().isoformat(), "source": "default"}
        source = "default"
    else:
        sectors_list = sorted(found.values(), key=lambda x: x["code"])
        sectors_data = {
            "sectors": sectors_list,
            "updated_at": datetime.now().isoformat(),
            "source": "tencent_api",
            "count": len(sectors_list),
        }
        source = "tencent_api"

    try:
        with open(SECTORS_CACHE, "w", encoding="utf-8") as f:
            json.dump(sectors_data, f, ensure_ascii=False, indent=2)
        logger.info("refresh_sectors: 完成，共 %d 个板块", len(sectors_data.get("sectors", [])))
    except Exception as e:
        logger.error("refresh_sectors: 写入缓存失败: %s", e)
        return {"sectors_count": 0, "added": [], "removed": [], "source": source}

    return {
        "sectors_count": len(sectors_data.get("sectors", [])),
        "added": added,
        "removed": removed,
        "source": source,
    }


def load_sectors() -> List[Dict]:
    """加载板块列表，优先缓存，否则用默认。"""
    try:
        if SECTORS_CACHE.exists():
            with open(SECTORS_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("sectors", DEFAULT_SECTORS)
    except Exception as e:
        logger.warning("load_sectors: 读取缓存失败: %s", e)
    return DEFAULT_SECTORS


def get_sector_codes() -> List[str]:
    """获取当前板块代码列表。"""
    return [s["code"] for s in load_sectors()]


# ============================================================
# 交易时段判断
# ============================================================
def is_trading_time(now: Optional[datetime] = None) -> bool:
    """判断当前是否为 A 股交易时段。"""
    now = now or datetime.now()
    h, m = now.hour, now.minute
    minutes = h * 60 + m

    # 周末
    if now.weekday() >= 5:
        return False

    # 上午 9:30-11:30
    start = TRADING_MORNING[0] * 60 + TRADING_MORNING[1]
    end = TRADING_MORNING[2] * 60 + TRADING_MORNING[3]
    if start <= minutes <= end:
        return True

    # 下午 13:00-15:00
    start = TRADING_AFTERNOON[0] * 60 + TRADING_AFTERNOON[1]
    end = TRADING_AFTERNOON[2] * 60 + TRADING_AFTERNOON[3]
    if start <= minutes <= end:
        return True

    return False


# ============================================================
# 分钟级采集
# ============================================================
def collect_minute_snapshot(codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """采集一次分钟级快照。

    步骤：
      1. 调 westock fund flow 批量拿 MainNetFlow (累计)
      2. 成交额自算（fund flow 的 MainInFlow + MainOutFlow）
      3. 与上一分钟快照差分，得本分钟净流入
      4. 入库 storage.upsert_minute_snapshot

    Args:
        codes: None 时用二级板块代码（get_sector_codes）；
               传概念板块代码（pt02）则采集概念板块分钟数据。

    Returns:
        {"timestamp": "...", "sectors_collected": N, "errors": [...]}
    """
    storage = get_storage()
    if codes is None:
        codes = get_sector_codes()
    if not codes:
        logger.warning("collect_minute_snapshot: no sector codes")
        return {"timestamp": datetime.now().isoformat(), "sectors_collected": 0, "errors": ["no_codes"]}

    ts = datetime.now()
    logger.info("collect_minute_snapshot: start at %s, codes=%d", ts.isoformat(), len(codes))

    # 1. westock fund flow 批量
    flow_records = fund_flow(codes, raw=True)
    flow_map: Dict[str, Dict] = {}
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if code:
            flow_map[code] = r

    # 2. 成交额自算（fund flow 的 MainInFlow + MainOutFlow）
    #    腾讯原 HTTP 接口已死，净额率改用 westock 自身字段计算
    metrics_map: Dict[str, Dict] = {}
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if code:
            metrics_map[code] = calc_sector_metrics(r, TURNOVER_METHOD)

    # 3. 组装快照 + 差分
    snapshot_list: List[Dict] = []
    prev_snapshot = storage.get_last_minute_snapshot()  # {code: {main_net_flow, turnover, ...}}

    for code in codes:
        flow = flow_map.get(code, {})
        metrics = metrics_map.get(code, {})

        # 主力净流入累计值 (元)
        main_net_flow = extract_main_net_flow(flow)
        # 成交额 (元) —— 自算
        turnover = metrics.get("turnover")
        # 流通市值 (元) —— fund flow 不提供，留空
        circ_mv = None

        # 差分：本分钟净流入 = 本分钟累计 - 上分钟累计
        minute_delta = None
        turnover_delta = None
        is_open_anchor = 0
        prev = prev_snapshot.get(code)
        if prev and main_net_flow is not None:
            prev_mnf = prev.get("main_net_flow")
            if prev_mnf is not None:
                minute_delta = main_net_flow - prev_mnf
            # 成交额差分（本分钟成交额 = 本分钟累计 - 上分钟累计）
            prev_turnover = prev.get("turnover")
            if turnover is not None and prev_turnover is not None:
                turnover_delta = turnover - prev_turnover
        # 开盘第一分钟：无 prev 快照，标记 is_open_anchor=1
        if prev is None:
            is_open_anchor = 1

        snapshot_list.append({
            "code": code,
            "timestamp": ts,
            "main_net_flow": main_net_flow,      # 当日累计 (元)
            "turnover": turnover,                # 当日累计成交额 (元，自算)
            "circ_mv": circ_mv,                  # 流通市值(暂缺)
            "minute_delta": minute_delta,        # 本分钟净流入增量 (元)
            "turnover_delta": turnover_delta,    # 本分钟成交额增量 (元)
            "is_open_anchor": is_open_anchor,    # 0/1 开盘第一条快照
            "main_inflow": extract_main_inflow(flow),
            "main_outflow": extract_main_outflow(flow),
        })

    # 4. 入库
    storage.upsert_minute_snapshots(snapshot_list)
    # 清理过期分钟数据
    storage.cleanup_old_minute_data(MINUTE_CACHE_DAYS)

    errors = []
    if len(flow_map) < len(codes) * 0.5:
        errors.append("westock_low_coverage")

    logger.info(
        "collect_minute_snapshot: done, flow=%d, errors=%s",
        len(flow_map), errors,
    )

    return {
        "timestamp": ts.isoformat(),
        "sectors_collected": len(snapshot_list),
        "flow_coverage": len(flow_map),
        "errors": errors,
    }


# 概念板块日快照去重标记（每天只存一次）
_concept_daily_snapshot_date: str = ""
_CONCEPT_DAILY_KEEP = 20  # 保留近 20 交易日


def collect_concept_daily_snapshot() -> Dict[str, Any]:
    """收盘后采集全量概念板块当日净流入，写入 concept_daily 表。

    每天只执行一次（按 trade_date 去重），用于近 N 日净值真实累加，
    替代 MainNetFlow5D 估算。
    """
    global _concept_daily_snapshot_date
    today = date.today().strftime("%Y%m%d")
    if _concept_daily_snapshot_date == today:
        return {"skipped": True, "reason": "already snapshotted today"}

    # 交易日守卫：非交易日（周末/节假日）不写入，避免污染近3/5日真实累加
    if not is_trading_day(date.today()):
        logger.info("concept_daily_snapshot: %s not a trading day, skipped", today)
        return {"skipped": True, "reason": "not a trading day"}

    from concept_sectors import get_default_codes, get_default_name
    from westock import fund_flow as _ff, extract_main_net_flow as _emnf
    from westock_fund_metrics import calc_sector_metrics

    codes = get_default_codes()
    if not codes:
        return {"error": "no concept codes"}

    t0 = time.time()
    flow_records = _ff(codes, raw=True)
    elapsed = time.time() - t0
    logger.info("concept_daily_snapshot: fund_flow %d codes in %.1fs, got %d",
                len(codes), elapsed, len(flow_records))

    records = []
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if not code:
            continue
        net = _emnf(r)
        metrics = calc_sector_metrics(r, TURNOVER_METHOD) if r else {}
        records.append({
            "code": code,
            "name": r.get("name") or get_default_name(code),
            "trade_date": today,
            "net_flow": net,
            "turnover": metrics.get("turnover"),
        })

    if records:
        storage = get_storage()
        n = storage.upsert_concept_daily_batch(records)
        storage.cleanup_concept_daily(_CONCEPT_DAILY_KEEP)
        _concept_daily_snapshot_date = today
        logger.info("concept_daily_snapshot: saved %d records for %s", n, today)
        return {"saved": n, "trade_date": today, "elapsed": round(elapsed, 1)}

    return {"saved": 0, "trade_date": today, "elapsed": round(elapsed, 1)}


# 全板块日快照去重标记（每天只存一次，与概念快照独立）
_sector_daily_snapshot_date: str = ""
_SECTOR_DAILY_KEEP = 75  # 保留近 50 交易日（约 69 自然日，留余量）


def collect_sector_daily_snapshot() -> Dict[str, Any]:
    """收盘后采集全板块（pt018 二级 + pt02 概念）当日净流入，写入 sector_daily。

    日线图数据源：每天收盘后把当日主力净流入落库，前端日级折线图读取。
    与 collect_concept_daily_snapshot 并存（后者写 concept_daily 供宽表
    近3/5日用；本函数写 sector_daily 供日线图用，保留 30 交易日）。
    """
    global _sector_daily_snapshot_date
    today = date.today().strftime("%Y%m%d")
    if _sector_daily_snapshot_date == today:
        return {"skipped": True, "reason": "already snapshotted today"}

    # 交易日守卫：非交易日不写入
    if not is_trading_day(date.today()):
        logger.info("sector_daily_snapshot: %s not a trading day, skipped", today)
        return {"skipped": True, "reason": "not a trading day"}

    from westock import fund_flow as _ff, extract_main_net_flow as _emnf
    from westock_fund_metrics import calc_sector_metrics

    # 全板块代码：二级(pt018) + 概念(pt02)，去重合并
    all_codes = []
    seen = set()
    for c in get_sector_codes():
        if c not in seen:
            seen.add(c)
            all_codes.append(c)
    from concept_sectors import get_default_codes as _gdc
    for c in _gdc():
        if c not in seen:
            seen.add(c)
            all_codes.append(c)

    if not all_codes:
        return {"error": "no sector codes"}

    t0 = time.time()
    flow_records = _ff(all_codes, raw=True)
    elapsed = time.time() - t0
    logger.info("sector_daily_snapshot: fund_flow %d codes in %.1fs, got %d",
                len(all_codes), elapsed, len(flow_records))

    from sectors import get_default_sector_map
    l2_map = get_default_sector_map()
    from concept_sectors import get_default_name as _gdn

    records = []
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if not code:
            continue
        net = _emnf(r)
        metrics = calc_sector_metrics(r, TURNOVER_METHOD) if r else {}
        name = (r.get("name")
                or (l2_map.get(code, {}) or {}).get("name")
                or _gdn(code)
                or code)
        records.append({
            "code": code,
            "name": name,
            "trade_date": today,
            "net_flow": net,
            "turnover": metrics.get("turnover"),
            "close_price": metrics.get("close_price"),
        })

    if records:
        storage = get_storage()
        n = storage.upsert_sector_daily_batch(records)
        storage.cleanup_sector_daily(_SECTOR_DAILY_KEEP)
        _sector_daily_snapshot_date = today
        logger.info("sector_daily_snapshot: saved %d records for %s", n, today)
        return {"saved": n, "trade_date": today, "elapsed": round(elapsed, 1)}

    return {"saved": 0, "trade_date": today, "elapsed": round(elapsed, 1)}


def backfill_sector_daily(days: int = 50) -> Dict[str, Any]:
    """回溯填充最近 N 个交易日的日净流入 + 涨跌幅。

    1. 净流入/成交额 → sector_daily（日线图净流入/成交额柱状图数据源）
    2. 涨跌幅 → sector_circ_mv.change_pct（日线图涨跌幅柱状图数据源）

    涨跌幅来源：腾讯板块指数历史行情不可回溯，用 westock fund_flow 的
    ClosePrice 相邻交易日差分得到，与腾讯涨跌幅口径一致（实测 8/28=2.88%、
    8/31=-1.25% 均吻合）。

    用于服务刚启动、历史日未落库时补齐日线图数据。
    用 westock fund_flow --date 查每个历史交易日，落库（upsert 按 code+trade_date 去重，幂等）。

    Args:
        days: 回溯交易日数（默认 50，不含今日；今日由 collect_sector_daily_snapshot 落库）

    Returns:
        {"backfilled_days": N, "total_records": M, "change_pct_records": K, "trade_dates": [...]}
    """
    from westock import fund_flow as _ff, extract_main_net_flow as _emnf
    from westock_fund_metrics import calc_sector_metrics
    from sectors import get_default_sector_map
    from concept_sectors import get_default_name as _gdn

    storage = get_storage()

    # 全板块代码：二级(pt018) + 概念(pt02)，去重合并
    all_codes = []
    seen = set()
    for c in get_sector_codes():
        if c not in seen:
            seen.add(c)
            all_codes.append(c)
    from concept_sectors import get_default_codes as _gdc
    for c in _gdc():
        if c not in seen:
            seen.add(c)
            all_codes.append(c)

    if not all_codes:
        return {"error": "no sector codes"}

    # 最近 N 个交易日（含今日；盘中是实时值，收盘后 collect_sector_daily_snapshot 会覆盖最终值）
    trading_days = get_last_n_trading_days(days, date.today())
    # 升序遍历（最远的在前），便于涨跌幅相邻交易日差分
    history_days = list(reversed(trading_days))

    l2_map = get_default_sector_map()
    backfilled_dates = []
    total_records = 0
    # 每个板块每个交易日的收盘价（用于相邻交易日差分算涨跌幅）
    close_map: Dict[str, Dict[str, float]] = {}

    for td in history_days:
        td_str = td.strftime("%Y%m%d")
        try:
            flow_records = _ff(all_codes, raw=True, asof_date=td.isoformat())
        except Exception as e:
            logger.warning("backfill_sector_daily %s fund_flow failed: %s", td_str, e)
            continue
        if not flow_records:
            logger.info("backfill_sector_daily %s: no data", td_str)
            continue

        records = []
        for r in flow_records:
            code = r.get("code") or r.get("SecuCode")
            if not code:
                continue
            net = _emnf(r)
            metrics = calc_sector_metrics(r, TURNOVER_METHOD) if r else {}
            name = (r.get("name")
                    or (l2_map.get(code, {}) or {}).get("name")
                    or _gdn(code)
                    or code)
            close = metrics.get("close_price")
            records.append({
                "code": code,
                "name": name,
                "trade_date": td_str,
                "net_flow": net,
                "turnover": metrics.get("turnover"),
                "close_price": close,
            })
            if close is not None:
                close_map.setdefault(code, {})[td_str] = close

        if records:
            n = storage.upsert_sector_daily_batch(records)
            total_records += n
            backfilled_dates.append(td_str)
            logger.info("backfill_sector_daily %s: %d records", td_str, n)

    # 涨跌幅：相邻交易日 ClosePrice 差分落库（只更新 change_pct，不覆盖 circ_mv）
    change_records = []
    for code, closes in close_map.items():
        prev_close = None
        for td_str in sorted(closes.keys()):
            close = closes[td_str]
            if prev_close is not None and prev_close > 0 and close is not None:
                pct = round((close - prev_close) / prev_close * 100, 2)
                change_records.append({"code": code, "trade_date": td_str, "change_pct": pct})
            prev_close = close
    n_change = storage.upsert_sector_change_pct(change_records)

    storage.cleanup_sector_daily(_SECTOR_DAILY_KEEP)
    logger.info("backfill_sector_daily: done, %d days, %d records, %d change_pct",
                len(backfilled_dates), total_records, n_change)
    return {
        "backfilled_days": len(backfilled_dates),
        "total_records": total_records,
        "change_pct_records": n_change,
        "trade_dates": backfilled_dates,
    }


def backfill_sparse_sector_daily(days: int = 12) -> Dict[str, Any]:
    """回溯补采日级记录不足的板块（稀疏板块）。

    历史遗留：部分板块（如新发现的概念板块）落库记录远少于其余板块。
    本函数找出记录天数 < days 的板块，用 westock fund_flow --date 回溯
    补采最近 days 个交易日，upsert 按 code+trade_date 去重，幂等。

    Args:
        days: 目标补采到的交易日数（默认 12，对齐其余板块）

    Returns:
        {"sparse_codes": N, "backfilled_days": M, "total_records": K, "trade_dates": [...]}
    """
    from westock import fund_flow as _ff, extract_main_net_flow as _emnf
    from westock_fund_metrics import calc_sector_metrics
    from sectors import get_default_sector_map
    from concept_sectors import get_default_name as _gdn

    storage = get_storage()
    sparse_codes = storage.get_sparse_sector_daily_codes(days)
    if not sparse_codes:
        logger.info("backfill_sparse_sector_daily: no sparse sectors (< %d days)", days)
        return {"sparse_codes": 0, "backfilled_days": 0, "total_records": 0}

    logger.info("backfill_sparse_sector_daily: %d sparse codes", len(sparse_codes))

    l2_map = get_default_sector_map()
    trading_days = get_last_n_trading_days(days, date.today())

    backfilled_dates = []
    total_records = 0
    for td in trading_days:
        td_str = td.strftime("%Y%m%d")
        try:
            flow_records = _ff(sparse_codes, raw=True, asof_date=td.isoformat())
        except Exception as e:
            logger.warning("backfill_sparse_sector_daily %s fund_flow failed: %s", td_str, e)
            continue
        if not flow_records:
            logger.info("backfill_sparse_sector_daily %s: no data", td_str)
            continue

        records = []
        for r in flow_records:
            code = r.get("code") or r.get("SecuCode")
            if not code:
                continue
            net = _emnf(r)
            metrics = calc_sector_metrics(r, TURNOVER_METHOD) if r else {}
            name = (r.get("name")
                    or (l2_map.get(code, {}) or {}).get("name")
                    or _gdn(code)
                    or code)
            records.append({
                "code": code,
                "name": name,
                "trade_date": td_str,
                "net_flow": net,
                "turnover": metrics.get("turnover"),
            })

        if records:
            n = storage.upsert_sector_daily_batch(records)
            total_records += n
            backfilled_dates.append(td_str)
            logger.info("backfill_sparse_sector_daily %s: %d records", td_str, n)

    logger.info("backfill_sparse_sector_daily: done, %d codes, %d days, %d records",
                len(sparse_codes), len(backfilled_dates), total_records)
    return {
        "sparse_codes": len(sparse_codes),
        "backfilled_days": len(backfilled_dates),
        "total_records": total_records,
        "trade_dates": backfilled_dates,
    }


def verify_daily_against_api(threshold: float = 0.2) -> Dict[str, Any]:
    """用落库的日级数据校验 westock 接口的 5D/10D 累计字段。

    读 sector_daily 落库的近 5 日/10 日净流入累加，与接口返回的
    MainNetFlow5D/10D 对比，偏差 > threshold（默认 20%）记为异常。

    用途：发现接口累计字段与每日落库累加不一致的情况（数据质量校验）。
    落库数据来自 collect_sector_daily_snapshot（westock 原始数据每日落库）。

    Args:
        threshold: 偏差阈值（相对偏差，默认 0.2 = 20%）

    Returns:
        {"checked": N, "mismatch_count": M,
         "mismatches": [{code, name, db_5d, api_5d, dev_5d, db_10d, api_10d, dev_10d}]}
    """
    codes = get_sector_codes()
    if not codes:
        return {"checked": 0, "mismatches": [], "mismatch_count": 0}

    storage = get_storage()
    # 读落库数据（近 10 交易日，含当日若已落库）
    daily_map = storage.get_sector_daily_batch(codes, days=10)
    # 调接口拿 5D/10D 累计字段
    flow_records = fund_flow(codes, raw=True)
    flow_map = {r.get("code") or r.get("SecuCode"): r for r in flow_records}

    from sectors import get_default_sector_map
    l2_map = get_default_sector_map()

    mismatches = []
    checked = 0
    for code in codes:
        records = daily_map.get(code, [])
        if len(records) < 5:
            continue
        checked += 1

        # 落库累加近 5 日净流入
        sum_5d_db = sum(r.get("net_flow") or 0 for r in records[:5])
        api_5d = _safe_float(flow_map.get(code, {}).get("MainNetFlow5D"))
        dev_5d = abs(sum_5d_db - api_5d) / abs(api_5d) if api_5d else None

        # 落库累加近 10 日：需 ≥10 条落库数据才准确，否则跳过 10 日校验
        # （避免落库仅 5-9 条时 sum_10d_db 偏小、误报 10 日偏差）
        sum_10d_db = None
        dev_10d = None
        api_10d = _safe_float(flow_map.get(code, {}).get("MainNetFlow10D"))
        if len(records) >= 10:
            sum_10d_db = sum(r.get("net_flow") or 0 for r in records[:10])
            if api_10d:
                dev_10d = abs(sum_10d_db - api_10d) / abs(api_10d)

        if (dev_5d is not None and dev_5d > threshold) or \
           (dev_10d is not None and dev_10d > threshold):
            name = (l2_map.get(code, {}) or {}).get("name") or code
            mismatches.append({
                "code": code,
                "name": name,
                "db_5d": round(sum_5d_db, 2),
                "api_5d": api_5d,
                "dev_5d": round(dev_5d, 4) if dev_5d is not None else None,
                "db_10d": round(sum_10d_db, 2) if sum_10d_db is not None else None,
                "api_10d": api_10d,
                "dev_10d": round(dev_10d, 4) if dev_10d is not None else None,
            })

    logger.info("verify_daily_against_api: checked=%d, mismatch=%d",
                checked, len(mismatches))
    return {"checked": checked, "mismatches": mismatches,
            "mismatch_count": len(mismatches)}


# ============================================================
# 日级数据采集（实时拉取，不长期落地）
# ============================================================
def collect_daily_records(code: str, n: int = 5) -> List[Dict]:
    """实时拉取单板块近n日日级记录。

    数据来源：
      1. 今日数据：调 westock fund flow
      2. 历史 T-1..T-(n-1)：westock 不给单日历史，用累计字段分段差分估算。

    分段差分策略（阶梯估算，避免全平）：
      - T-1~T-4：    (MainNetFlow5D  - 今日) / 4         → "近5日段"
      - T-5~T-9：    (MainNetFlow10D - MainNetFlow5D) / 5  → "近10日段"
      - T-10~T-19：  (MainNetFlow20D - MainNetFlow10D) / 10 → "近20日段"
      - 超出 20D 范围：回退到上一段均值
      日期使用交易日历（跳过周末/节假日），标记 estimated=True。

    实测 westock fund flow 返回字段：
      MainNetFlow (今日累计净流入, 元)
      MainNetFlow5D / 10D / 20D (近N日累计净流入, 元)

    Args:
        code: 板块代码
        n: 天数

    Returns:
        日记录列表，按时间倒序（今日在前），每条含:
          date, trade_date, net_flow(元), turnover(元),
          main_net_flow_5d/10d/20d, estimated
    """
    records: List[Dict] = []
    today = date.today()

    # 1. 今日实时：westock fund flow
    flow_records = fund_flow([code], raw=True)
    flow = flow_records[0] if flow_records else {}
    metrics = calc_sector_metrics(flow, TURNOVER_METHOD) if flow else {}

    today_net = extract_main_net_flow(flow)
    today_turnover = metrics.get("turnover")
    today_circ_mv = None  # fund flow 不提供流通市值
    net_5d = _safe_float(flow.get("MainNetFlow5D"))
    net_10d = _safe_float(flow.get("MainNetFlow10D"))
    net_20d = _safe_float(flow.get("MainNetFlow20D"))

    records.append({
        "date": today.isoformat(),
        "trade_date": today.strftime("%Y%m%d"),
        "net_flow": today_net,
        "turnover": today_turnover,
        "circ_mv": today_circ_mv,
        "main_net_flow_5d": net_5d,
        "main_net_flow_10d": net_10d,
        "main_net_flow_20d": net_20d,
    })

    # 读 sector_daily 落库真实数据（每日收盘后落库），用于替代分段估算
    db_map: Dict[str, Dict] = {}
    if n > 1:
        try:
            db_records = get_storage().get_sector_daily_batch([code], days=n).get(code, [])
            db_map = {d["trade_date"]: d for d in db_records if d.get("net_flow") is not None}
        except Exception as e:
            logger.warning("collect_daily_records %s read sector_daily failed: %s", code, e)

    # 2. 历史 T-1..T-(n-1)：优先读落库真实数据，缺失才分段差分估算
    #    使用交易日历获取真实历史日期。
    #    边界处理：today 非交易日时（周末/节假日盘中查询），
    #    fund flow 的 MainNetFlow/5D/10D/20D 实测仍是"含最近交易日"的累计值
    #    （westock CLI 在非交易日返回的是上一个交易日收盘值），
    #    因此历史估算仍应锚定"上一个交易日为 T-1"，而非把 today 当 T-0。
    #
    #    修复：历史日期框架必须独立于 net_5d 存在——MainNetFlow5D 缺失
    #    （接口超时/字段不全）时也应生成近 n 天的日期记录（数据为 None 占位），
    #    否则前端行展开日柱状图只有当天一天。
    #    但保留 today_net 条件：fund_flow 完全空（连今日数据都没有）时不生成历史日。
    if n > 1 and today_net is not None:
        # 各段日均净流入（分段阶梯；字段缺失时为 None）
        seg_1_4 = (net_5d - today_net) / 4.0 if (net_5d is not None and today_net is not None) else None  # T-1~T-4
        seg_5_9 = ((net_10d - net_5d) / 5.0) if (net_10d is not None and net_5d is not None) else None    # T-5~T-9
        seg_10_19 = ((net_20d - net_10d) / 10.0) if (net_20d is not None and net_10d is not None) else None  # T-10~T-19

        # 锚定"今日对应的最近交易日"为 T-0，往前取 n-1 个历史交易日 T-1..T-(n-1)
        # 避免用 get_last_n_trading_days(n+1, today)+切片 的歧义写法：
        #   today 非交易日时，get_last_n_trading_days 的第 0 个元素是上一个交易日，
        #   [1:n+1] 切片会把"上上一个交易日"当 T-1，丢失了真正的 T-1。
        anchor = today if is_trading_day(today) else get_previous_trading_day(today)
        if anchor is None:
            history_days: List[date] = []
        else:
            # get_last_n_trading_days 返回 [anchor, T-1, T-2, ...] 倒序
            trading_days = get_last_n_trading_days(n, anchor)
            # 跳过 anchor（trading_days[0]），取 T-1..T-(n-1)
            history_days = trading_days[1:n]

        for idx, d in enumerate(history_days):
            i = idx + 1  # i=1 表示 T-1, i=2 表示 T-2...
            td = d.strftime("%Y%m%d")

            # 优先用 sector_daily 落库真实数据（estimated=False）
            db_rec = db_map.get(td)
            if db_rec is not None:
                records.append({
                    "date": d.isoformat(),
                    "trade_date": td,
                    "net_flow": db_rec.get("net_flow"),
                    "turnover": db_rec.get("turnover"),
                    "circ_mv": today_circ_mv,
                    "main_net_flow_5d": net_5d,
                    "main_net_flow_10d": net_10d,
                    "main_net_flow_20d": net_20d,
                    "estimated": False,
                })
                continue

            # 缺失落库数据：分区选择日均（分段差分估算）
            if seg_1_4 is not None and i <= 4:
                avg = seg_1_4
            elif seg_5_9 is not None and i <= 9:
                avg = seg_5_9
            elif seg_10_19 is not None and i <= 19:
                avg = seg_10_19
            else:
                # 超出 20D 范围，用最后一段兜底；seg 全 None（5D/10D/20D 全缺失）时 None 占位
                avg = seg_10_19 or seg_5_9 or seg_1_4

            records.append({
                "date": d.isoformat(),
                "trade_date": td,
                "net_flow": round(avg, 2) if avg is not None else None,
                "turnover": None,
                "circ_mv": today_circ_mv,
                "main_net_flow_5d": net_5d,
                "main_net_flow_10d": net_10d,
                "main_net_flow_20d": net_20d,
                "estimated": True,
            })

    logger.debug("collect_daily_records %s: %d records", code, len(records))
    return records


def collect_all_sectors_daily(n: int = 5, asof_date: Optional[str] = None) -> Tuple[Dict[str, List[Dict]], Dict[str, float]]:
    """拉取所有板块近n日日级数据 + 流通市值。

    优化：
      - 今日实时：批量 westock + 批量腾讯
      - 历史 T-1..T-(n-1)：从批量结果中用累计差分估算

    Args:
        n: 窗口天数
        asof_date: YYYY-MM-DD，查询指定交易日（None 表示今日）

    Returns:
        (daily_map, circ_mv_map)
        daily_map: {code: [日记录...]}
        circ_mv_map: {code: 流通市值(元)}
    """
    codes = get_sector_codes()
    if not codes:
        return {}, {}

    logger.info("collect_all_sectors_daily: start, codes=%d, n=%d, asof=%s", len(codes), n, asof_date)

    # 批量 westock fund flow（支持指定日期）
    flow_records = fund_flow(codes, raw=True, asof_date=asof_date)
    flow_map: Dict[str, Dict] = {}
    metrics_map: Dict[str, Dict] = {}
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if code:
            flow_map[code] = r
            metrics_map[code] = calc_sector_metrics(r, TURNOVER_METHOD)

    anchor_date = date.fromisoformat(asof_date) if asof_date else date.today()
    daily_map: Dict[str, List[Dict]] = {}
    circ_mv_map: Dict[str, float] = {}

    # 预计算交易日列表（所有板块共用）
    # 锚定 anchor 对应的最近交易日为 T-0，往前取 n-1 个历史交易日
    anchor_td = anchor_date if is_trading_day(anchor_date) else get_previous_trading_day(anchor_date)
    if anchor_td is None:
        anchor_td = anchor_date
    trading_days = get_last_n_trading_days(n, anchor_td) if n > 1 else []
    history_days_all = trading_days[1:n] if len(trading_days) > 1 else []

    # 批量读 sector_daily 真实落库数据（每日收盘后 collect_sector_daily_snapshot 写入），
    # 用于替代 5D/10D 分段估算，保证历史日与接口累计字段可交叉校验。
    db_daily_map: Dict[str, Dict[str, Dict]] = {}
    if n > 1:
        try:
            raw_db = get_storage().get_sector_daily_batch(codes, days=n)
            db_daily_map = {
                code: {d["trade_date"]: d for d in recs if d.get("net_flow") is not None}
                for code, recs in raw_db.items()
            }
            if db_daily_map:
                logger.info("collect_all_sectors_daily: loaded %d sectors daily from db",
                            len(db_daily_map))
        except Exception as e:
            logger.warning("collect_all_sectors_daily read sector_daily failed: %s", e)

    for code in codes:
        flow = flow_map.get(code, {})
        metrics = metrics_map.get(code, {})

        today_net = extract_main_net_flow(flow)
        today_turnover = metrics.get("turnover")
        today_circ_mv = None  # fund flow 不提供流通市值
        net_5d = _safe_float(flow.get("MainNetFlow5D"))
        net_10d = _safe_float(flow.get("MainNetFlow10D"))
        net_20d = _safe_float(flow.get("MainNetFlow20D"))

        records: List[Dict] = []
        records.append({
            "date": anchor_date.isoformat(),
            "trade_date": anchor_date.strftime("%Y%m%d"),
            "net_flow": today_net,
            "turnover": today_turnover,
            "circ_mv": today_circ_mv,
            "main_net_flow_5d": net_5d,
            "main_net_flow_10d": net_10d,
            "main_net_flow_20d": net_20d,
        })

        # 历史 T-1..T-(n-1)：分段差分估算（使用交易日历）
        if n > 1 and net_5d is not None and today_net is not None:
            seg_1_4 = (net_5d - today_net) / 4.0 if net_5d is not None and today_net is not None else None
            seg_5_9 = ((net_10d - net_5d) / 5.0) if (net_10d is not None and net_5d is not None) else None
            seg_10_19 = ((net_20d - net_10d) / 10.0) if (net_20d is not None and net_10d is not None) else None

            for idx, d in enumerate(history_days_all):
                i = idx + 1
                td = d.strftime("%Y%m%d")

                # 优先用 sector_daily 落库真实数据（estimated=False）
                db_rec = db_daily_map.get(code, {}).get(td)
                if db_rec is not None:
                    records.append({
                        "date": d.isoformat(),
                        "trade_date": td,
                        "net_flow": db_rec.get("net_flow"),
                        "turnover": db_rec.get("turnover"),
                        "circ_mv": today_circ_mv,
                        "main_net_flow_5d": net_5d,
                        "main_net_flow_10d": net_10d,
                        "main_net_flow_20d": net_20d,
                        "estimated": False,
                    })
                    continue

                # 缺失落库数据：分段差分估算（estimated=True）
                if seg_1_4 is not None and i <= 4:
                    avg = seg_1_4
                elif seg_5_9 is not None and i <= 9:
                    avg = seg_5_9
                elif seg_10_19 is not None and i <= 19:
                    avg = seg_10_19
                else:
                    avg = seg_10_19 or seg_5_9 or seg_1_4 or 0

                records.append({
                    "date": d.isoformat(),
                    "trade_date": td,
                    "net_flow": round(avg, 2),
                    "turnover": None,
                    "circ_mv": today_circ_mv,
                    "main_net_flow_5d": net_5d,
                    "main_net_flow_10d": net_10d,
                    "main_net_flow_20d": net_20d,
                    "estimated": True,
                })

        daily_map[code] = records

    logger.info(
        "collect_all_sectors_daily: done, flow=%d, circ_mv=%d",
        len(flow_map), len(circ_mv_map),
    )
    return daily_map, circ_mv_map


# ============================================================
# 高频聚焦采集（只采选中板块，用于分时图实时对比）
# ============================================================
_focused_codes: List[str] = []
_focused_lock = threading.Lock()


def set_focused_codes(codes: List[str]) -> int:
    """设置当前需要高频采集的板块代码列表。

    Args:
        codes: 板块代码列表（空列表表示停止聚焦采集）

    Returns:
        设置的代码数量
    """
    global _focused_codes
    with _focused_lock:
        _focused_codes = list(codes)
    logger.info("set_focused_codes: %d codes", len(_focused_codes))
    return len(_focused_codes)


def get_focused_codes() -> List[str]:
    """获取当前聚焦采集的板块代码列表。"""
    with _focused_lock:
        return list(_focused_codes)


def _collect_focused_snapshot(codes: List[str]) -> Dict[str, Any]:
    """对指定板块做一次分钟快照采集（轻量版，不扫描全量）。

    Args:
        codes: 需要采集的板块代码

    Returns:
        {"flow_coverage": N, "snapshots_written": N, "errors": [...]}
    """
    storage = get_storage()
    if not codes:
        return {"flow_coverage": 0, "snapshots_written": 0, "errors": []}

    ts = datetime.now()
    # 只拉指定板块的 fund flow（小批量，快速）
    flow_records = fund_flow(codes, raw=True)
    flow_map: Dict[str, Dict] = {}
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if code:
            flow_map[code] = r

    prev_snapshot = storage.get_last_minute_snapshot(codes)
    snapshot_list: List[Dict] = []
    for code in codes:
        flow = flow_map.get(code, {})
        main_net_flow = extract_main_net_flow(flow)
        metrics = calc_sector_metrics(flow, TURNOVER_METHOD) if flow else {}
        turnover = metrics.get("turnover")

        minute_delta = None
        turnover_delta = None
        is_open_anchor = 0
        prev = prev_snapshot.get(code)
        if prev and main_net_flow is not None:
            prev_mnf = prev.get("main_net_flow")
            if prev_mnf is not None:
                minute_delta = main_net_flow - prev_mnf
            prev_turnover = prev.get("turnover")
            if turnover is not None and prev_turnover is not None:
                turnover_delta = turnover - prev_turnover
        if prev is None:
            is_open_anchor = 1

        snapshot_list.append({
            "code": code,
            "timestamp": ts,
            "main_net_flow": main_net_flow,
            "turnover": turnover,
            "circ_mv": None,
            "minute_delta": minute_delta,
            "turnover_delta": turnover_delta,
            "is_open_anchor": is_open_anchor,
            "main_inflow": extract_main_inflow(flow),
            "main_outflow": extract_main_outflow(flow),
        })

    storage.upsert_minute_snapshots(snapshot_list)
    return {
        "flow_coverage": len(flow_map),
        "snapshots_written": len(snapshot_list),
        "errors": ["low_coverage"] if len(flow_map) < len(codes) * 0.5 else [],
    }


def run_collector_loop(force: bool = False) -> None:
    """统一自适应采集循环。

    聚焦模式活跃时：只采选中板块（高频 8s，带限流退避），
    同时每 ~60s 补采一次非聚焦板块的分钟快照。
    无聚焦模式时：全量 134 板块每 60s 一轮。

    Args:
        force: True 时非交易时段也跑（测试用）
    """
    logger.info("=== collector loop started (focused=%ds, full=%ds) ===",
                FOCUSED_INTERVAL, MINUTE_INTERVAL)
    backoff = 0          # 连续限流次数
    cycle = 0            # 周期计数（用于定期补采全量）
    FULL_EVERY_N = 7     # 每 N 个聚焦周期补采一次全量（7×8s≈56s）
    # 概念板块分钟补采独立计数器：不能复用 cycle（无聚焦分支末尾会重置 cycle，
    # 复用会导致 % CONCEPT_MINUTE_EVERY_N 永不成立——历史 bug）
    concept_cycle = 0
    # 概念板块分钟采集频率：每轮全量采集都补采一次（与 l2 同频，≈60s 一帧）。
    # 历史版本设为 5（每 ~5 分钟一次），导致概念板块分时图数据点间隔 6-14 分钟，
    # 折线稀疏失真。改为 1 后概念板块分时图约 60s 一个点。
    CONCEPT_MINUTE_EVERY_N = 1
    # 盘后修正窗口去重：按 date 标记，跨日重置（每日 15:00-15:30 仅补采一次）
    fired_after_close_date: str = ""
    fired_after_close: bool = False
    # 概念板块分钟补采去重：交易时段采集不完整时，非交易时段越早越好补采一次
    concept_minute_backfill_date: str = ""

    while True:
        now = datetime.now()
        today_str = now.strftime("%Y%m%d")
        # 跨日重置盘后修正标记
        if fired_after_close_date != today_str:
            fired_after_close_date = today_str
            fired_after_close = False
        trading = is_trading_time(now)

        if not trading and not force:
            # 盘后修正窗口：15:00-15:30 间继续采一次分钟快照，
            # 捕捉 A 股收盘后主力净流入的小幅修正（实测盘后会有纠正值）。
            # 用 fired_after_close 去重，30 分钟窗口内只补一次。
            hour = now.hour
            minute = now.minute
            if hour == 15 and minute <= 30 and not fired_after_close:
                try:
                    result = collect_minute_snapshot()
                    logger.info("collector: post-close correction snapshot: %s", result)
                    fired_after_close = True
                except Exception as e:
                    logger.warning("collector post-close snapshot error: %s", e)

            # 概念板块分钟补采：交易时段若采集不完整（服务重启/失败导致），
            # 非交易时段越早越好补采一次全量（每天一次，跨日重置）。
            # 判断标准：当日概念板块分钟记录数 < 概念板块数 × 50%。
            if concept_minute_backfill_date != today_str:
                concept_minute_backfill_date = today_str
                try:
                    from concept_sectors import get_default_codes as _gdc
                    from storage import get_storage as _gs
                    concept_codes = _gdc()
                    if concept_codes:
                        _deltas = _gs().get_minute_deltas_batch(concept_codes, today_str)
                        _collected = sum(1 for v in _deltas.values() if v)
                        if _collected < len(concept_codes) * 0.5:
                            logger.info("collector: concept minute incomplete (%d/%d), backfilling...",
                                        _collected, len(concept_codes))
                            c_result = collect_minute_snapshot(concept_codes)
                            logger.info("collector: concept minute backfill done: %s", c_result)
                except Exception as e:
                    logger.warning("collector concept minute backfill error: %s", e, exc_info=True)

            # 收盘后采集概念板块日快照（每天仅一次，幂等）
            # 触发窗口收紧到 15:00-23:59：避免盘前 09:00 前误触；函数内
            # 按 trade_date 去重，多次调用也只写一次。盘后若服务已停，
            # 次日开盘后 09:30 第一帧会补采昨日——但为保近3日真实数据
            # 不中断，建议盘后保留服务运行至少至快照完成（见 dev.sh）。
            if hour >= 15:
                try:
                    result = collect_concept_daily_snapshot()
                    if result and not result.get("skipped"):
                        logger.info("concept_daily_snapshot done: %s", result)
                except Exception as e:
                    logger.warning("concept_daily_snapshot error: %s", e)
                # 全板块日快照（日线图数据源，pt018+pt02，保留 30 交易日）
                try:
                    result = collect_sector_daily_snapshot()
                    if result and not result.get("skipped"):
                        logger.info("sector_daily_snapshot done: %s", result)
                except Exception as e:
                    logger.warning("sector_daily_snapshot error: %s", e)
            time.sleep(IDLE_SLEEP)
            backoff = 0
            cycle = 0
            continue

        with _focused_lock:
            focused = list(_focused_codes)

        cycle += 1

        if focused:
            # ---- 聚焦模式：高频采选中板块 ----
            try:
                result = _collect_focused_snapshot(focused)
                flow_ok = result["flow_coverage"] >= len(focused) * 0.5
                if flow_ok:
                    if backoff > 0:
                        logger.info("collector: recovered after %d backoffs", backoff)
                    backoff = 0
                else:
                    backoff += 1
                    logger.warning("collector: low coverage %d/%d, backoff #%d",
                                   result["flow_coverage"], len(focused), backoff)
            except Exception as e:
                backoff += 1
                logger.warning("collector error (backoff #%d): %s", backoff, e)

            # 定期补采非聚焦板块的全量分钟快照
            if cycle % FULL_EVERY_N == 0:
                all_codes = get_sector_codes()
                remaining = [c for c in all_codes if c not in focused]
                if remaining:
                    try:
                        _collect_focused_snapshot(remaining)
                        logger.debug("collector: full refresh %d remaining sectors", len(remaining))
                    except Exception as e:
                        logger.warning("collector full refresh error: %s", e)

            # 休眠：正常用基础间隔，限流时指数退避
            if backoff > 0:
                delay = min(FOCUSED_BACKOFF_BASE * (2 ** (backoff - 1)), FOCUSED_BACKOFF_MAX)
            else:
                delay = FOCUSED_INTERVAL
        else:
            # ---- 无聚焦模式：全量 60s 采集 ----
            try:
                result = collect_minute_snapshot()
                logger.info("collector full snapshot: %s", result)
            except Exception as e:
                logger.error("collector full error: %s", e, exc_info=True)

            # 概念板块分钟补采：每 CONCEPT_MINUTE_EVERY_N 轮采一次
            # （626 码 fund_flow 约 10-15s，不能与 l2 同频；每 ~5 分钟一次
            #   足够分时图呈现曲线，也避免过度占用 CLI）
            concept_cycle += 1
            if concept_cycle % CONCEPT_MINUTE_EVERY_N == 0:
                try:
                    from concept_sectors import get_default_codes as _gdc
                    concept_codes = _gdc()
                    c_result = collect_minute_snapshot(concept_codes)
                    logger.info("collector concept minute snapshot: %s", c_result)
                except Exception as e:
                    logger.warning("collector concept minute error: %s", e, exc_info=True)

            backoff = 0
            cycle = 0
            delay = MINUTE_INTERVAL

        time.sleep(delay)


# 保留旧函数名作为别名（兼容 app.py 启动代码）


def run_focused_loop(force: bool = False) -> None:
    run_collector_loop(force)


def run_minute_loop(force: bool = False) -> None:
    run_collector_loop(force)


# ============================================================
# 流通市值日级采集
# ============================================================
def collect_circ_mv_snapshot() -> Dict[str, Any]:
    """采集一次全板块流通市值快照，写入 sector_circ_mv 缓存。

    优先方案 C（腾讯 qt.gtimg.cn，免费无需 token），失败 fallback 方案 A/B。

    Returns:
        {"timestamp": "...", "total": N, "valid": N, "written": N, "source": str}
    """
    # 非交易日不采集：腾讯接口在周末返回前一交易日的涨跌幅，
    # 若落库到周末日期会导致 K 线出现周末假蜡烛（涨跌幅数值错误）。
    # 用 weekday 判断（is_trading_day 依赖缓存可能过期，工作日会误判）。
    if datetime.now().weekday() >= 5:
        logger.info("collect_circ_mv_snapshot: weekend, skip")
        return {"timestamp": datetime.now().isoformat(), "total": 0, "valid": 0,
                "written": 0, "skipped": True}

    from circ_mv_collector import (
        collect_all_sectors_circ_mv,
        collect_all_sectors_circ_mv_tencent,
    )
    storage = get_storage()

    ts = datetime.now()
    result_map = None
    source = "unknown"

    # 优先方案 C：腾讯 qt.gtimg.cn（免费无需 token，自选股 App 同源）
    try:
        result_map = collect_all_sectors_circ_mv_tencent()
        # 检查是否有有效流通市值：腾讯接口挂时会返回非空 dict 但每个板块
        # circ_mv=None（空结果），此时不能算成功，应 fallback 到方案 A/B
        if result_map and any(v.get("circ_mv") is not None for v in result_map.values()):
            source = "tencent"
            logger.info("collect_circ_mv_snapshot: start at %s, source=tencent (plan C)",
                        ts.isoformat())
        else:
            result_map = None
            logger.warning("collect_circ_mv_snapshot: tencent plan C got no valid circ_mv, fallback to A/B")
    except Exception as e:
        logger.warning("collect_circ_mv_snapshot: tencent plan C failed: %s, fallback to A/B", e)
        result_map = None

    # fallback 方案 A/B（Tushare 直取 / westock 反推）
    if not result_map:
        try:
            result_map = collect_all_sectors_circ_mv()
            source = "tushare_or_reverse"
        except Exception as e:
            logger.error("collect_circ_mv_snapshot: collect failed: %s", e, exc_info=True)
            return {"timestamp": ts.isoformat(), "total": 0, "valid": 0,
                    "written": 0, "error": str(e)}

    # 写入缓存表
    records = []
    for code, info in result_map.items():
        info["code"] = code
        records.append(info)
    n_written = storage.upsert_sector_circ_mv(records)

    valid = sum(1 for v in result_map.values() if v.get("circ_mv") is not None)
    logger.info(
        "collect_circ_mv_snapshot: done, total=%d, valid=%d, written=%d, source=%s",
        len(result_map), valid, n_written, source,
    )
    return {
        "timestamp": ts.isoformat(),
        "total": len(result_map),
        "valid": valid,
        "written": n_written,
        "source": source,
    }


def _parse_circ_mv_times() -> List[str]:
    """解析 CIRC_MV_COLLECT_TIMES 配置，返回 ["HH:MM", ...]。"""
    times = []
    for part in CIRC_MV_COLLECT_TIMES.split(","):
        part = part.strip()
        if part:
            times.append(part)
    return times


def run_circ_mv_loop(force: bool = False) -> None:
    """流通市值日级采集主循环。

    按 CIRC_MV_COLLECT_TIMES 时点每日触发一次（默认 09:15 + 15:05）。
    每日每个时点最多触发一次，避免重复采集。

    Args:
        force: True 时忽略时点判断立即跑一次（用于测试），之后正常循环
    """
    logger.info("=== collector circ_mv loop started, times=%s ===", CIRC_MV_COLLECT_TIMES)

    # 已触发时点记录（date -> set(HH:MM)），避免同一天同一时点重复触发
    fired: Dict[str, set] = {}
    target_times = _parse_circ_mv_times()

    if force:
        try:
            result = collect_circ_mv_snapshot()
            logger.info("circ_mv force snapshot: %s", result)
        except Exception as e:
            logger.error("circ_mv force snapshot error: %s", e, exc_info=True)

    while True:
        now = datetime.now()
        today_str = now.strftime("%Y%m%d")

        # 检查已过目标时点且当日未触发（而非精确分钟匹配，避免 sleep 周期错过 09:15/15:05）
        fired_today = fired.setdefault(today_str, set())
        for target_hm in target_times:
            try:
                target_dt = datetime.strptime(f"{today_str} {target_hm}", "%Y%m%d %H:%M")
            except ValueError:
                continue
            if now >= target_dt and target_hm not in fired_today:
                logger.info("circ_mv loop: hit schedule %s, triggering", target_hm)
                try:
                    result = collect_circ_mv_snapshot()
                    logger.info("circ_mv snapshot: %s", result)
                except Exception as e:
                    logger.error("circ_mv loop error: %s", e, exc_info=True)
                finally:
                    fired_today.add(target_hm)

        # 清理过期日期记录（只保留今日）
        expired = [d for d in fired if d != today_str]
        for d in expired:
            fired.pop(d, None)

        time.sleep(CIRC_MV_CHECK_INTERVAL)


# ============================================================
# 涨停池 / 板块成分股采集
# ============================================================
# 东方财富涨停/炸板/跌停池接口（zt=涨停封住 / zb=炸板 / dt=跌停）
_TOPIC_POOL_URLS = {
    "zt": "https://push2ex.eastmoney.com/getTopicZTPool",
    "zb": "https://push2ex.eastmoney.com/getTopicZBPool",
    "dt": "https://push2ex.eastmoney.com/getTopicDTPool",
}


def _add_stock_prefix(code: str) -> str:
    """6 位数字股票代码补交易所前缀（东财涨停池只给 6 位数字）。"""
    code = (code or "").strip()
    if len(code) != 6 or not code.isdigit():
        return code
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return code


def _fmt_zt_time(v: Any) -> Optional[str]:
    """92500 -> '092500'（涨停时间 HHMMSS）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s.isdigit():
        return None
    return s.zfill(6)


def _fetch_topic_pool(pool_type: str, trade_date: str) -> List[Dict]:
    """采集东方财富涨停/炸板/跌停池，返回带 type 标记的 rows。

    Args:
        pool_type: zt=涨停(封住) / zb=炸板 / dt=跌停
        trade_date: YYYYMMDD
    """
    import urllib.request

    url = _TOPIC_POOL_URLS[pool_type]
    full_url = (
        f"{url}?ut=7eea3edcaed734bea9cbfc24409ed989"
        f"&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt%3Aasc&date={trade_date}"
    )
    rows: List[Dict] = []
    req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    pool = (payload.get("data") or {}).get("pool") or []
    for item in pool:
        zdp = _safe_float(item.get("zdp"))
        # 涨停池口径对齐主流行情软件：北交所 30% 涨停（涨跌幅 > 21%）不计入
        # 涨停家数（东财 getTopicZTPool 会混入北交所，导致家数多出 3 家）。
        if pool_type == "zt" and zdp is not None and zdp > 21:
            continue
        zttj = item.get("zttj") or {}
        p = item.get("p")
        rows.append({
            "code": _add_stock_prefix(str(item.get("c", ""))),
            "name": item.get("n"),
            "trade_date": trade_date,
            "type": pool_type,
            "price": (_safe_float(p) / 1000.0) if p is not None else None,
            "change_pct": zdp,
            "amount": _safe_float(item.get("amount")),
            "ltsz": _safe_float(item.get("ltsz")),
            "turnover_rate": _safe_float(item.get("hs")),
            "lbc": item.get("lbc"),
            "fbt": _fmt_zt_time(item.get("fbt")),
            "lbt": _fmt_zt_time(item.get("lbt")),
            "fund": _safe_float(item.get("fund")),
            "zbc": item.get("zbc"),
            "hybk": item.get("hybk"),
            "zt_days": zttj.get("days"),
            "zt_ct": zttj.get("ct"),
        })
    return rows


def collect_limit_up_pool(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """采集当日全市场涨停池 + 炸板池 + 跌停池，落库 limit_up_pool。

    数据源优先级说明：涨停/炸板/跌停名单 westock CLI 无法直接获取（changedist
    只有家数无名单、quote 已废弃），故走东方财富 HTTP 接口兜底（符合「westock
    优先、缺失字段走 HTTP 兜底」原则）。

    Args:
        trade_date: 交易日 YYYYMMDD，默认今天

    Returns:
        {trade_date, count, saved, zt, zb, dt, error?}
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")

    rows: List[Dict] = []
    counts: Dict[str, int] = {}
    for pool_type in ("zt", "zb", "dt"):
        try:
            sub = _fetch_topic_pool(pool_type, trade_date)
            counts[pool_type] = len(sub)
            rows.extend(sub)
        except Exception as e:
            logger.warning("collect_limit_up_pool %s error: %s", pool_type, e)
            counts[pool_type] = 0

    if not rows:
        return {"trade_date": trade_date, "count": 0, "saved": 0, **counts}

    # 涨停票批量补主力净流入（westock fund flow 个股，涨停池接口不返回该字段）
    zt_codes = [r["code"] for r in rows if r.get("type") == "zt"]
    if zt_codes:
        from westock import fund_flow as _ff, extract_main_net_flow as _emnf
        inflow_map = {}
        for rec in _ff(zt_codes, raw=True):
            code = rec.get("code") or rec.get("SecuCode")
            if code:
                inflow_map[code] = _emnf(rec)
        for r in rows:
            if r.get("type") == "zt":
                r["main_net_inflow"] = inflow_map.get(r["code"])

    storage = get_storage()
    # 先删后插：涨停池是当日快照，接口口径变化（如北交所剔除）时旧记录
    # 可能不再返回，INSERT OR REPLACE 不会清理残留，故先按日期删除旧数据。
    storage.delete_limit_up_pool(trade_date)
    saved = storage.upsert_limit_up_pool(rows)
    logger.info("collect_limit_up_pool: %s count=%d saved=%d (%s)",
                trade_date, len(rows), saved, counts)
    return {"trade_date": trade_date, "count": len(rows), "saved": saved, **counts}


def collect_all_sector_constituents(codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """采集板块成分股（westock sector constituent），落库 sector_constituent。

    默认采集二级行业(pt018) + 概念板块(pt02)，用于「个股→板块」反查
    （涨停票的所属概念、板块涨停票统计）。

    Args:
        codes: 板块代码列表，默认全量（二级行业 + 概念板块）

    Returns:
        {sectors, stocks, saved}
    """
    if codes is None:
        codes = list(get_sector_codes())
        # 追加概念板块（pt02），供涨停票「所属概念」反查
        from concept_sectors import get_default_codes as _gdc
        seen = set(codes)
        for c in _gdc():
            if c not in seen:
                seen.add(c)
                codes.append(c)
    if not codes:
        return {"sectors": 0, "stocks": 0, "saved": 0}

    storage = get_storage()
    total_stocks = 0
    total_saved = 0
    for i in range(0, len(codes), 20):
        batch = codes[i:i + 20]
        try:
            result = sector_constituent(batch, raw=True)
        except Exception as e:
            logger.warning("sector_constituent batch error: %s", e)
            continue
        for pt_code, items in (result or {}).items():
            stocks = [{"code": it.get("code"), "name": it.get("name")} for it in items]
            total_stocks += len(stocks)
            total_saved += storage.upsert_sector_constituents(pt_code, stocks)
    logger.info("collect_all_sector_constituents: sectors=%d stocks=%d saved=%d",
                len(codes), total_stocks, total_saved)
    return {"sectors": len(codes), "stocks": total_stocks, "saved": total_saved}


def run_limit_up_loop(force: bool = False) -> None:
    """涨停池 + 板块成分股采集循环。

    - 涨停池：交易时段每 ~60s 采一次（东方财富接口，快，独立于 westock CLI）
    - 成分股：每日一次（非交易时段触发，846 板块分批，量大放盘后）

    Args:
        force: True 时非交易时段也持续采集（测试用）
    """
    logger.info("=== limit_up loop started (force=%s) ===", force)
    constituent_date = ""
    close_collected_date = ""  # 收盘后补采标记（涨停池最终数据）
    while True:
        now = datetime.now()
        today_str = now.strftime("%Y%m%d")
        trading = is_trading_time(now)

        # 涨停池采集（交易时段）
        if trading or force:
            try:
                collect_limit_up_pool(today_str)
            except Exception as e:
                logger.warning("limit_up pool collect error: %s", e)

        # 收盘后补采一次最终涨停池（15:00 后当天第一次）：盘中涨停在收盘前
        # 可能炸板，盘中采集家数偏多，收盘后重采落库最终数据，供次日连板
        # 晋级率使用（分母需昨日收盘后的精确涨停家数）。
        if close_collected_date != today_str and now.hour >= 15 and not trading:
            try:
                collect_limit_up_pool(today_str)
                close_collected_date = today_str
            except Exception as e:
                logger.warning("limit_up close collect error: %s", e)

        # 成分股每日一次（非交易时段触发，跨日去重；失败下次重试）
        if constituent_date != today_str and (force or not trading):
            try:
                collect_all_sector_constituents()
                constituent_date = today_str
            except Exception as e:
                logger.warning("constituent collect error: %s", e)

        time.sleep(MINUTE_INTERVAL if trading else IDLE_SLEEP)


# ============================================================
# 工具
# ============================================================
def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    import sys
    if "--refresh-sectors" in sys.argv:
        n = refresh_sectors()
        print(f"refreshed sectors: {n}")
    elif "--test-minute" in sys.argv:
        r = collect_minute_snapshot()
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif "--test-daily" in sys.argv:
        daily_map, circ_mv_map = collect_all_sectors_daily(n=5)
        print(f"sectors: {len(daily_map)}, circ_mv: {len(circ_mv_map)}")
        # 打印第一个
        if daily_map:
            code = next(iter(daily_map))
            print(f"\n{code} daily records:")
            print(json.dumps(daily_map[code], ensure_ascii=False, indent=2, default=str))
    elif "--test-circ-mv" in sys.argv:
        # 一次性采集全板块流通市值，写入 sector_circ_mv 缓存
        r = collect_circ_mv_snapshot()
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    elif "--backfill-sparse" in sys.argv:
        # 回溯补采日级记录不足的板块（稀疏板块）
        r = backfill_sparse_sector_daily()
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    elif "--circ-mv-loop" in sys.argv:
        # 流通市值日级采集主循环
        run_circ_mv_loop(force="--force" in sys.argv)
    else:
        print("Usage:")
        print("  python collector.py --refresh-sectors")
        print("  python collector.py --test-minute")
        print("  python collector.py --test-daily")
        print("  python collector.py --test-circ-mv           # 一次性采集流通市值")
        print("  python collector.py --circ-mv-loop [--force] # 日级采集主循环")
        print()
        print("可用的采集函数:")
        print("  collect_circ_mv_snapshot()   # 一次性流通市值采集")
        print("  run_circ_mv_loop(force=False)  # 日级采集主循环")
        print()
        print("环境变量:")
        print('  CIRC_MV_COLLECT_TIMES=09:15,15:05  # 采集时点')
        print('  CIRC_MV_CHECK_INTERVAL=300          # 时点检查间隔(秒)')
