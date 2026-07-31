#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股交易日历模块。

功能：
  - 判断某日是否为交易日
  - 获取最近 N 个交易日（含当日）
  - 自动缓存，首选 Tushare 接口，兜底 weekday 启发式

数据源优先级：
  1. Tushare trade_cal 接口（需 token，最准）
  2. 本地缓存文件 (data/trading_days.json)
  3. weekday 启发式（周一至周五，非交易日按周末算——精度差但有兜底）
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from config import DATA_DIR

logger = logging.getLogger(__name__)

TRADING_DAYS_CACHE = DATA_DIR / "trading_days.json"


def _try_tushare_cal() -> Optional[Set[str]]:
    """尝试从 Tushare 拉交易日历。失败返回 None。"""
    try:
        import tushare as ts
        token = ts.get_token()  # 需要用户已配置 ~/.tushare/token
        if not token:
            return None
        pro = ts.pro_api(token)
        # 拉取近 2 年的交易日
        today = date.today()
        start = (today - timedelta(days=400)).strftime("%Y%m%d")
        end = (today + timedelta(days=30)).strftime("%Y%m%d")
        df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        days: Set[str] = set()
        for _, row in df.iterrows():
            if row["is_open"] == 1:
                days.add(row["cal_date"])
        logger.info("tushare trade_cal: loaded %d trading days (%s~%s)", len(days), start, end)
        return days
    except Exception as e:
        logger.debug("tushare trade_cal failed: %s", e)
        return None


def _load_cache() -> Dict:
    """加载本地缓存。"""
    if TRADING_DAYS_CACHE.exists():
        try:
            with open(TRADING_DAYS_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("trading_days cache corrupted: %s", e)
    return {"days": [], "updated": None}


def _save_cache(days: Set[str]) -> None:
    """保存交易日集合到本地缓存。"""
    cache = {
        "days": sorted(days),
        "updated": datetime.now().isoformat(),
        "count": len(days),
    }
    try:
        with open(TRADING_DAYS_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("failed to save trading_days cache: %s", e)


def _weekday_heuristic(d: date) -> bool:
    """weekday 启发式：周一至周五算交易日（精度差，仅兜底）。"""
    return d.weekday() < 5


# ============================================================
# 全局状态（懒加载）
# ============================================================
_trading_days: Optional[Set[str]] = None
_initialized: bool = False


def _ensure_initialized() -> None:
    """初始化交易日集合。"""
    global _trading_days, _initialized
    if _initialized:
        return
    _initialized = True

    # 1. 尝试 Tushare
    days = _try_tushare_cal()
    if days:
        _trading_days = days
        _save_cache(days)
        return

    # 2. 用本地缓存
    cache = _load_cache()
    cached_days = cache.get("days", [])
    if cached_days:
        _trading_days = set(cached_days)
        logger.info("trading_calendar: using cached %d trading days", len(_trading_days))
        return

    # 3. 无数据源，使用 weekday 启发式 + 预填近 2 年
    logger.warning("trading_calendar: no data source, using weekday heuristic (inaccurate!)")
    _trading_days = set()
    today = date.today()
    for i in range(-365, 31):  # 过去 1 年 + 未来 1 个月
        d = today + timedelta(days=i)
        if _weekday_heuristic(d):
            _trading_days.add(d.strftime("%Y%m%d"))


# ============================================================
# 公开 API
# ============================================================
def is_trading_day(d: date) -> bool:
    """判断某日是否为 A 股交易日。

    Args:
        d: 日期对象

    Returns:
        True 表示交易日
    """
    _ensure_initialized()
    return d.strftime("%Y%m%d") in (_trading_days or set())


def get_last_n_trading_days(n: int, from_date: Optional[date] = None) -> List[date]:
    """获取最近 N 个交易日（含 from_date）。

    Args:
        n: 需要的交易日数量
        from_date: 起始日期（含），默认今天

    Returns:
        日期列表，按时间倒序（最近的在前面），长度 ≤ n
    """
    _ensure_initialized()
    if from_date is None:
        from_date = date.today()

    result: List[date] = []
    cursor = from_date
    lookback = 0
    max_lookback = n * 3 + 10  # 防止死循环（如春节长假）

    while len(result) < n and lookback < max_lookback:
        if is_trading_day(cursor):
            result.append(cursor)
        cursor -= timedelta(days=1)
        lookback += 1

    return result


def get_previous_trading_day(from_date: Optional[date] = None) -> Optional[date]:
    """获取上一个交易日。

    Args:
        from_date: 基准日期，默认今天

    Returns:
        上一个交易日，找不到返回 None
    """
    days = get_last_n_trading_days(2, from_date)
    return days[1] if len(days) >= 2 else None


def get_trading_day_offset(from_date: date, offset: int) -> Optional[date]:
    """获取相对某日的第 offset 个交易日。

    Args:
        from_date: 基准日期
        offset: 正数=未来，负数=过去。offset=-1 表示上一个交易日

    Returns:
        对应交易日，找不到返回 None
    """
    _ensure_initialized()
    if offset == 0:
        return from_date if is_trading_day(from_date) else None

    step = 1 if offset > 0 else -1
    target_count = abs(offset)
    found = 0
    cursor = from_date + timedelta(days=step)
    max_steps = target_count * 3 + 10

    for _ in range(max_steps):
        if is_trading_day(cursor):
            found += 1
            if found >= target_count:
                return cursor
        cursor += timedelta(days=step)

    return None


def count_trading_days_between(start: date, end: date) -> int:
    """计算两个日期之间（含两端）的交易日数量。

    Args:
        start: 起始日期
        end: 结束日期（>= start）

    Returns:
        交易天数
    """
    _ensure_initialized()
    count = 0
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def refresh() -> int:
    """强制刷新交易日历（从 Tushare 重新拉取）。

    Returns:
        交易天数，失败返回 0
    """
    global _trading_days, _initialized
    days = _try_tushare_cal()
    if days:
        _trading_days = days
        _initialized = True
        _save_cache(days)
        return len(days)
    return 0


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    today = date.today()
    print(f"今日: {today.isoformat()} (交易日: {is_trading_day(today)})")
    print(f"上一个交易日: {get_previous_trading_day(today)}")
    print(f"最近 5 个交易日:")
    for i, d in enumerate(get_last_n_trading_days(5)):
        print(f"  T-{i}: {d.isoformat()} ({d.strftime('%A')})")
    print(f"上一个交易日 (T-1): {get_trading_day_offset(today, -1)}")
