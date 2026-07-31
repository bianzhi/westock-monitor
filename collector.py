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
    get_last_n_trading_days, get_trading_day_offset, is_trading_day,
)

from config import (
    BASE_DIR, DATA_DIR, SECTORS_CACHE, MINUTE_INTERVAL, IDLE_SLEEP,
    TRADING_MORNING, TRADING_AFTERNOON, STRENGTH_WINDOW_N, DISPLAY_DAYS,
    SUMMARY_3D, SUMMARY_5D, MINUTE_CACHE_DAYS, get_scale,
    CIRC_MV_COLLECT_TIMES, CIRC_MV_CHECK_INTERVAL, TURNOVER_METHOD,
)
from sectors import DEFAULT_SECTORS, get_default_codes, get_default_sector_map
from westock import (
    fund_flow, sector_ranking, search_sector,
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
def refresh_sectors() -> int:
    """从接口刷新板块列表，写入 data/sectors.json。

    策略：
      1. 遍历 31 个一级行业名，调 search --type sector
      2. 过滤 分类="申万二级行业清单" 的条目
      3. 合并去重，按代码排序
      4. 失败兜底用 DEFAULT_SECTORS

    Returns:
        刷新后的板块数量
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

    # 兜底：如果接口失败，用默认列表
    if not found:
        logger.warning("refresh_sectors: 接口未返回数据，使用默认列表")
        sectors_data = {"sectors": DEFAULT_SECTORS, "updated_at": datetime.now().isoformat(), "source": "default"}
    else:
        sectors_list = sorted(found.values(), key=lambda x: x["code"])
        sectors_data = {
            "sectors": sectors_list,
            "updated_at": datetime.now().isoformat(),
            "source": "tencent_api",
            "count": len(sectors_list),
        }

    try:
        with open(SECTORS_CACHE, "w", encoding="utf-8") as f:
            json.dump(sectors_data, f, ensure_ascii=False, indent=2)
        logger.info("refresh_sectors: 完成，共 %d 个板块", len(sectors_data.get("sectors", [])))
    except Exception as e:
        logger.error("refresh_sectors: 写入缓存失败: %s", e)
        return 0

    return len(sectors_data.get("sectors", []))


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
def collect_minute_snapshot() -> Dict[str, Any]:
    """采集一次分钟级快照。

    步骤：
      1. 调 westock fund flow 批量拿 MainNetFlow (累计)
      2. 调腾讯 HTTP 拿 turnover (成交额) + circ_mv (流通市值)
      3. 与上一分钟快照差分，得本分钟净流入
      4. 入库 storage.upsert_minute_snapshot

    Returns:
        {"timestamp": "...", "sectors_collected": N, "errors": [...]}
    """
    storage = get_storage()
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
    prev_snapshot = storage.get_last_minute_snapshot()  # {code: {main_net_flow, ...}}

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
        prev = prev_snapshot.get(code)
        if prev and main_net_flow is not None:
            prev_mnf = prev.get("main_net_flow")
            if prev_mnf is not None:
                minute_delta = main_net_flow - prev_mnf

        snapshot_list.append({
            "code": code,
            "timestamp": ts,
            "main_net_flow": main_net_flow,      # 当日累计 (元)
            "turnover": turnover,                # 当日累计成交额 (元，自算)
            "circ_mv": circ_mv,                  # 流通市值(暂缺)
            "minute_delta": minute_delta,        # 本分钟净流入增量 (元)
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

    # 2. 历史 T-1..T-(n-1)：分段差分估算
    #    使用交易日历获取真实历史日期
    if n > 1 and net_5d is not None and today_net is not None:
        # 各段日均净流入（分段阶梯）
        seg_1_4 = (net_5d - today_net) / 4.0 if net_5d is not None and today_net is not None else None  # T-1~T-4
        seg_5_9 = ((net_10d - net_5d) / 5.0) if (net_10d is not None and net_5d is not None) else None    # T-5~T-9
        seg_10_19 = ((net_20d - net_10d) / 10.0) if (net_20d is not None and net_10d is not None) else None  # T-10~T-19

        # 获取最近 n 个交易日（不含今日，含今日则多取 1 个）
        trading_days = get_last_n_trading_days(n + 1, today)
        # 跳过今日 (trading_days[0])
        history_days = trading_days[1:n + 1]  # T-1..T-(n-1)

        for idx, d in enumerate(history_days):
            i = idx + 1  # i=1 表示 T-1, i=2 表示 T-2...

            # 分区选择日均
            if seg_1_4 is not None and i <= 4:
                avg = seg_1_4
            elif seg_5_9 is not None and i <= 9:
                avg = seg_5_9
            elif seg_10_19 is not None and i <= 19:
                avg = seg_10_19
            else:
                # 超出 20D 范围，用最后一段兜底，没有则用 seg_1_4
                avg = seg_10_19 or seg_5_9 or seg_1_4 or 0

            records.append({
                "date": d.isoformat(),
                "trade_date": d.strftime("%Y%m%d"),
                "net_flow": round(avg, 2),
                "turnover": None,
                "circ_mv": today_circ_mv,
                "main_net_flow_5d": net_5d,
                "main_net_flow_10d": net_10d,
                "main_net_flow_20d": net_20d,
                "estimated": True,
            })

    logger.debug("collect_daily_records %s: %d records", code, len(records))
    return records


def collect_all_sectors_daily(n: int = 5) -> Tuple[Dict[str, List[Dict]], Dict[str, float]]:
    """拉取所有板块近n日日级数据 + 流通市值。

    优化：
      - 今日实时：批量 westock + 批量腾讯
      - 历史 T-1..T-(n-1)：从批量结果中用累计差分估算

    Returns:
        (daily_map, circ_mv_map)
        daily_map: {code: [日记录...]}
        circ_mv_map: {code: 流通市值(元)}
    """
    codes = get_sector_codes()
    if not codes:
        return {}, {}

    logger.info("collect_all_sectors_daily: start, codes=%d, n=%d", len(codes), n)

    # 批量 westock fund flow
    flow_records = fund_flow(codes, raw=True)
    flow_map: Dict[str, Dict] = {}
    metrics_map: Dict[str, Dict] = {}
    for r in flow_records:
        code = r.get("code") or r.get("SecuCode")
        if code:
            flow_map[code] = r
            metrics_map[code] = calc_sector_metrics(r, TURNOVER_METHOD)

    today = date.today()
    daily_map: Dict[str, List[Dict]] = {}
    circ_mv_map: Dict[str, float] = {}  # fund flow 不提供流通市值，暂空

    # 预计算交易日列表（所有板块共用）
    trading_days = get_last_n_trading_days(n + 1, today) if n > 1 else []
    history_days_all = trading_days[1:n + 1] if len(trading_days) > 1 else []

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
            "date": today.isoformat(),
            "trade_date": today.strftime("%Y%m%d"),
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
                    "trade_date": d.strftime("%Y%m%d"),
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
# 分钟级采集主循环
# ============================================================
def run_minute_loop(force: bool = False) -> None:
    """分钟级采集主循环。

    Args:
        force: True 时非交易时段也跑（用于测试）
    """
    logger.info("=== collector minute loop started (force=%s) ===", force)

    while True:
        now = datetime.now()
        trading = is_trading_time(now)

        if not trading and not force:
            time.sleep(IDLE_SLEEP)
            continue

        try:
            result = collect_minute_snapshot()
            logger.info("minute snapshot: %s", result)
        except Exception as e:
            logger.error("minute loop error: %s", e, exc_info=True)

        # 等待下一分钟
        time.sleep(MINUTE_INTERVAL)


# ============================================================
# 流通市值日级采集
# ============================================================
def collect_circ_mv_snapshot() -> Dict[str, Any]:
    """采集一次全板块流通市值快照（成分股反推累加），写入 sector_circ_mv 缓存。

    Returns:
        {"timestamp": "...", "total": N, "valid": N, "written": N}
    """
    from circ_mv_collector import collect_all_sectors_circ_mv, _get_tushare_pro
    storage = get_storage()

    ts = datetime.now()
    # 启动日志显示数据源方案（Tushare 直取 / westock 反推兜底）
    has_tushare = _get_tushare_pro() is not None
    logger.info("collect_circ_mv_snapshot: start at %s, source=%s",
                ts.isoformat(), "tushare" if has_tushare else "westock_reverse")

    try:
        result_map = collect_all_sectors_circ_mv()
    except Exception as e:
        logger.error("collect_circ_mv_snapshot: collect failed: %s", e, exc_info=True)
        return {"timestamp": ts.isoformat(), "total": 0, "valid": 0, "written": 0, "error": str(e)}

    # 写入缓存表
    records = []
    for code, info in result_map.items():
        info["code"] = code
        records.append(info)
    n_written = storage.upsert_sector_circ_mv(records)

    valid = sum(1 for v in result_map.values() if v.get("circ_mv") is not None)
    logger.info(
        "collect_circ_mv_snapshot: done, total=%d, valid=%d, written=%d",
        len(result_map), valid, n_written,
    )
    return {
        "timestamp": ts.isoformat(),
        "total": len(result_map),
        "valid": valid,
        "written": n_written,
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
        now_hm = now.strftime("%H:%M")

        # 检查是否命中采集时点且今日未触发
        if now_hm in target_times:
            fired_today = fired.setdefault(today_str, set())
            if now_hm not in fired_today:
                logger.info("circ_mv loop: hit schedule %s, triggering", now_hm)
                try:
                    result = collect_circ_mv_snapshot()
                    logger.info("circ_mv snapshot: %s", result)
                except Exception as e:
                    logger.error("circ_mv loop error: %s", e, exc_info=True)
                finally:
                    fired_today.add(now_hm)

        # 清理过期日期记录（只保留今日）
        expired = [d for d in fired if d != today_str]
        for d in expired:
            fired.pop(d, None)

        time.sleep(CIRC_MV_CHECK_INTERVAL)


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
