#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据缓存模块：启动时预加载最近 N 个交易日全板块数据，API 直接从内存取。

解决的问题：
  - /api/sectors 每次请求实时调 westock CLI → uvicorn 单线程阻塞 → 前端超时
  - 启动预热 + 内存缓存后，API 响应从 2s+ 降至 <5ms

缓存结构（内存 dict，全局唯一）：
  {
    "updated": "2026-07-29T16:35:00",
    "max_n": 10,                    # 缓存窗口（交易日数）
    "codes": [...],                 # 板块代码列表
    "sectors": [...],               # 板块元数据
    "daily": {code: [records]},     # 日记录（按时间倒序），近 max_n 日
    "circ_mv": {code: circ_mv_yi},  # 流通市值
  }
"""

import logging
import threading
import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from config import STRENGTH_WINDOW_N, DISPLAY_DAYS, SUMMARY_3D, SUMMARY_5D, get_scale
from sectors import DEFAULT_SECTORS, get_default_codes, get_default_sector_map

logger = logging.getLogger(__name__)

# ============================================================
# 全局缓存状态
# ============================================================
_lock = threading.RLock()
_cache: Dict[str, Any] = {
    "updated": None,
    "max_n": 0,
    "codes": [],
    "sectors": [],
    "daily": {},
    "circ_mv": {},
}
_ready = False  # 缓存就绪标记
_preload_n = 10  # 默认预加载 10 个交易日
_refresh_in_progress = False  # 后台刷新去重锁标记
_refresh_last_error: Optional[str] = None  # 最近一次刷新错误（None 表示成功或未跑过）

# 告警检测状态：记录上一次各板块的强度档位，刷新后对比变化
_prev_strength: Dict[str, Dict[str, Any]] = {}  # {code: {"level": str, "value": float}}


def is_ready() -> bool:
    """缓存是否就绪（启动预加载完成）。"""
    return _ready


def get_updated_time() -> Optional[str]:
    """缓存最后更新时间。"""
    return _cache.get("updated")


def get_max_n() -> int:
    """缓存窗口大小。"""
    return _cache.get("max_n", 0)


def get_codes() -> List[str]:
    """获取缓存的板块代码列表。"""
    with _lock:
        return list(_cache.get("codes", []))


def get_sectors() -> List[Dict]:
    """获取缓存的板块元数据列表。"""
    with _lock:
        return list(_cache.get("sectors", []))


def get_daily(code: str) -> List[Dict]:
    """获取单板块缓存的日记录（近 max_n 日）。"""
    with _lock:
        return list(_cache.get("daily", {}).get(code, []))


def get_daily_map(codes: Optional[List[str]] = None) -> Dict[str, List[Dict]]:
    """获取多板块日记录（默认全部）。"""
    with _lock:
        daily = _cache.get("daily", {})
        if codes is None:
            return {k: list(v) for k, v in daily.items()}
        return {c: list(daily.get(c, [])) for c in codes}


def get_circ_mv(code: str) -> Optional[float]:
    """获取缓存的流通市值（亿元）。"""
    with _lock:
        return _cache.get("circ_mv", {}).get(code)


def get_circ_mv_map() -> Dict[str, float]:
    """获取全部流通市值缓存。"""
    with _lock:
        return dict(_cache.get("circ_mv", {}))


def _load_all_data(n: int = 10) -> Dict:
    """调用 collector 加载全部数据（阻塞，耗时约 2-5s）。

    Args:
        n: 预加载交易日数

    Returns:
        {"daily": {...}, "circ_mv": {...}, "sectors": [...], "codes": [...]}
    """
    from collector import collect_all_sectors_daily, load_sectors, get_sector_codes
    from storage import get_storage

    t0 = time.time()

    # 1. 板块元数据
    codes = get_sector_codes()
    sectors_list = load_sectors()

    # 2. 全板块日级数据（含今日 + 历史估算）
    daily_map, circ_mv_raw = collect_all_sectors_daily(n=n)
    logger.info("data_cache: collect_all_sectors_daily(n=%d) took %.1fs, %d codes, %d valid",
                n, time.time() - t0, len(codes), len(daily_map))

    # 3. 流通市值补充：优先 sector_circ_mv 缓存，其次实时
    storage = get_storage()
    circ_mv_cache = storage.get_latest_sector_circ_mv()
    circ_mv_map: Dict[str, float] = {}
    for code in codes:
        cached = circ_mv_cache.get(code, {})
        if cached and cached.get("circ_mv_yi"):
            circ_mv_map[code] = cached["circ_mv_yi"]
        elif code in circ_mv_raw:
            v = circ_mv_raw[code]
            if v and v > 0:
                circ_mv_map[code] = round(v / 1e8, 2)

    return {
        "daily": daily_map,
        "circ_mv": circ_mv_map,
        "sectors": sectors_list,
        "codes": codes,
    }


def init_cache(n: int = _preload_n, blocking: bool = True) -> bool:
    """初始化/刷新缓存（阻塞调用，耗时 2-5s）。

    Args:
        n: 预加载交易日数
        blocking: True 时同步加载完成再返回

    Returns:
        True 表示加载成功
    """
    global _cache, _ready
    logger.info("data_cache: init_cache(n=%d) starting...", n)

    try:
        data = _load_all_data(n)
    except Exception as e:
        logger.error("data_cache: init_cache failed: %s", e, exc_info=True)
        return False

    with _lock:
        _cache["updated"] = datetime.now().isoformat()
        _cache["max_n"] = n
        _cache["codes"] = data["codes"]
        _cache["sectors"] = data["sectors"]
        _cache["daily"] = data["daily"]
        _cache["circ_mv"] = data["circ_mv"]
        _ready = True

    elapsed = time.time() - _load_all_data._last_t if hasattr(_load_all_data, "_last_t") else 0
    _load_all_data._last_t = time.time()
    logger.info("data_cache: init_cache done, %d codes cached, max_n=%d", len(data["codes"]), n)
    return True


def refresh_cache(n: Optional[int] = None) -> bool:
    """手动刷新缓存（重用已缓存的板块列表，只拉新数据）。

    Args:
        n: None 时用已有 max_n

    Returns:
        True 表示成功
    """
    if n is None:
        n = get_max_n() or _preload_n
    return init_cache(n=n)


def is_refresh_in_progress() -> bool:
    """后台刷新是否正在进行（用于 API 去重轮询）。"""
    with _lock:
        return _refresh_in_progress


def get_refresh_last_error() -> Optional[str]:
    """最近一次后台刷新的错误描述（None 表示成功或未跑过）。"""
    with _lock:
        return _refresh_last_error


def trigger_background_refresh(n: Optional[int] = None) -> bool:
    """触发后台异步刷新（非阻塞，立即返回）。

    去重：若已有刷新在进行中，直接返回 False（不重复拉取）。
    API 调用方可通过 is_refresh_in_progress() 轮询进度，
    通过 get_refresh_last_error() 获取上次错误。

    Args:
        n: None 时用已有 max_n

    Returns:
        True 表示已触发新刷新；False 表示已有刷新在进行中（跳过）
    """
    global _refresh_in_progress, _refresh_last_error
    with _lock:
        if _refresh_in_progress:
            logger.info("data_cache: background refresh already in progress, skip")
            return False
        _refresh_in_progress = True
        _refresh_last_error = None

    target_n = n if n is not None else (get_max_n() or _preload_n)

    def _bg():
        global _refresh_in_progress, _refresh_last_error
        try:
            ok = init_cache(n=target_n)
            if ok:
                # 缓存刷新成功后检测强度档位变化
                try:
                    n_alerts = check_strength_alerts()
                    if n_alerts > 0:
                        logger.info("data_cache: %d strength alerts written", n_alerts)
                except Exception as e:
                    logger.warning("data_cache: strength alert check failed: %s", e)
            else:
                with _lock:
                    _refresh_last_error = "init_cache returned False"
        except Exception as e:
            with _lock:
                _refresh_last_error = str(e)
            logger.error("data_cache: background refresh error: %s", e, exc_info=True)
        finally:
            with _lock:
                _refresh_in_progress = False

    t = threading.Thread(target=_bg, daemon=True, name="data_cache_bg_refresh")
    t.start()
    logger.info("data_cache: background refresh triggered (n=%d)", target_n)
    return True


# ============================================================
# 强度档位变化告警检测
# ============================================================
def check_strength_alerts() -> int:
    """检测所有板块强度档位变化，写入 alert_log。

    从当前缓存读取所有板块的 daily + circ_mv，
    调用 calc_sector_strength 计算最新强度，与上次快照对比。
    仅在有档位变化时写入告警（如 普通→强）。

    Returns:
        写入告警条数
    """
    global _prev_strength
    from strength import calc_sector_strength

    daily_map = get_daily_map()
    circ_mv_map = get_circ_mv_map()
    sectors = get_sectors()
    if not sectors or not daily_map:
        return 0

    from storage import get_storage
    from datetime import datetime as dt

    storage = get_storage()
    now = dt.now()
    trade_date = now.strftime("%Y%m%d")
    alerts_written = 0

    for sec in sectors:
        code = sec["code"]
        records = daily_map.get(code, [])
        if not records:
            continue
        circ_mv_yi = circ_mv_map.get(code)
        if circ_mv_yi is None or circ_mv_yi <= 0:
            continue

        strength = calc_sector_strength(records, circ_mv_yi, STRENGTH_WINDOW_N)
        new_level = strength["level"]
        new_value = strength["value"]

        prev = _prev_strength.get(code)
        if prev and prev.get("level") == new_level:
            continue  # 档位未变

        # 首次运行：只记录状态，不写告警
        if prev is None:
            _prev_strength[code] = {"level": new_level, "value": new_value}
            continue

        # 档位变化：写入告警
        alert = {
            "code": code,
            "name": sec.get("name", ""),
            "trade_date": trade_date,
            "timestamp": now.isoformat(),
            "old_level": prev["level"],
            "new_level": new_level,
            "old_value": prev["value"],
            "new_value": new_value,
            "net_rate_n": strength.get("net_rate_n"),
            "net_flow_n_yi": round(strength.get("net_flow_n", 0) / 1e8, 2) if strength.get("net_flow_n") else None,
            "scale": strength["scale"],
        }
        storage.insert_alert(alert)
        _prev_strength[code] = {"level": new_level, "value": new_value}
        alerts_written += 1
        logger.info(
            "data_cache: alert %s(%s) %s → %s (value %.3f → %.3f)",
            sec.get("name", code), code,
            prev["level"], new_level, prev["value"], new_value,
        )

    return alerts_written


def _background_refresh(interval_sec: int = 60) -> None:
    """后台定时刷新线程（交易时段每 60s 刷新一次）。

    连续失败熔断：失败次数累计，每次失败后睡眠间隔翻倍（上限 16x），
    成功一次则立即恢复到基础间隔。
    """
    import time as _time
    from collector import is_trading_time

    logger.info("data_cache: background refresh started (interval=%ds)", interval_sec)
    consecutive_failures = 0

    while True:
        # 计算熔断后的实际睡眠间隔（指数退避，上限 16 倍）
        if consecutive_failures > 0:
            factor = min(2 ** consecutive_failures, 16)
            sleep_sec = interval_sec * factor
        else:
            sleep_sec = interval_sec

        _time.sleep(sleep_sec)
        now = datetime.now()
        trading = is_trading_time(now)
        if trading:
            try:
                refresh_cache()
                # 刷新成功后检测强度档位变化
                try:
                    check_strength_alerts()
                except Exception:
                    pass  # alert 检测失败不影响主流程
                if consecutive_failures > 0:
                    logger.info(
                        "data_cache: background refresh recovered after %d failures",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                logger.debug("data_cache: background refresh done at %s", now.isoformat())
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    "data_cache: background refresh error (fail #%d, next sleep %ds): %s",
                    consecutive_failures, interval_sec * min(2 ** consecutive_failures, 16), e,
                )


def start_background_refresh(interval_sec: int = 60) -> threading.Thread:
    """启动后台定时刷新线程。

    Args:
        interval_sec: 刷新间隔（秒）

    Returns:
        线程对象
    """
    t = threading.Thread(
        target=_background_refresh,
        args=(interval_sec,),
        daemon=True,
        name="data_cache_refresh",
    )
    t.start()
    return t


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = init_cache(n=5)
    print(f"init_cache: {ok}")
    print(f"ready: {is_ready()}")
    print(f"codes: {len(get_codes())}")
    print(f"updated: {get_updated_time()}")
    d = get_daily("pt01801081")
    if d:
        print(f"pt01801081 records: {len(d)}, first date={d[0].get('date')}")
