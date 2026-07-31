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


def _background_refresh(interval_sec: int = 60) -> None:
    """后台定时刷新线程（交易时段每 60s 刷新一次）。"""
    import time as _time
    from collector import is_trading_time

    logger.info("data_cache: background refresh started (interval=%ds)", interval_sec)
    while True:
        _time.sleep(interval_sec)
        now = datetime.now()
        trading = is_trading_time(now)
        if trading:
            try:
                refresh_cache()
                logger.debug("data_cache: background refresh done at %s", now.isoformat())
            except Exception as e:
                logger.warning("data_cache: background refresh error: %s", e)


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
