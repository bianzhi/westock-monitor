#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""强度计算模块：5档判定 + 分段线性插值 + n日窗口可配。

核心 API:
  - calc_strength_value(net_rate_pct, scale) -> float
      净额率(%) + 规模分档 → 连续强度值 -2.0 ~ +2.0
  - calc_strength_level(net_rate_pct, scale) -> str
      净额率(%) + 规模分档 → 5档判定词 "强/偏强/普通/偏弱/弱"
  - calc_aggregate_net_rate(daily_records, n) -> float
      从日记录列表算近n日聚合净额率(%)
  - calc_sector_strength(daily_records, circ_mv_yi, n) -> dict
      一站式：输入日记录 + 流通市值 + 窗口 → {value, level, net_rate}

阈值沿用原脚本 realtime_strength.py 的 calc_strength：

| 档位 | 流通市值 | 很强(hi) | 偏强(mid) | 偏弱(lo) | 很弱(vlo) |
|------|---------|---------|----------|---------|----------|
| 大盘 | ≥4万亿  | 5%      | 2%       | -1%     | -1.5%    |
| 中盘 | 1~4万亿 | 7%      | 3%       | -1.5%   | -2%      |
| 小盘 | <1万亿  | 10%     | 4%       | -2%     | -3%      |

映射为连续值（分段线性插值）:
  nr ≥ hi          → +2.0
  [mid, hi]        → [1.0, 2.0]
  [0, mid]         → [0, 1.0]
  [lo, 0]          → [-1.0, 0]
  [vlo, lo]        → [-2.0, -1.0]

5档判定词：
  value ≥ 1.5   → "强"
  1.0 ≤ v < 1.5 → "偏强"
  -1.0 < v < 1.0 → "普通"
  -1.5 < v ≤ -1.0 → "偏弱"
  v ≤ -1.5      → "弱"
"""
import logging
from typing import List, Dict, Optional, Any

from config import (
    SCALE_THRESHOLDS, STRENGTH_LEVELS, get_scale, SCALE_TURNOVER_RATE,
)

logger = logging.getLogger(__name__)


# ============================================================
# 单点强度计算
# ============================================================
def calc_strength_value(net_rate_pct: float, scale: str) -> float:
    """净额率(%) + 规模分档 → 连续强度值 -2.0 ~ +2.0。

    Args:
        net_rate_pct: 净额率百分比，如 5.2 表示 5.2%
        scale: "大盘" / "中盘" / "小盘"

    Returns:
        -2.0 ~ +2.0 的连续强度值
    """
    th = SCALE_THRESHOLDS[scale]
    hi, mid, lo, vlo = th["hi"], th["mid"], th["lo"], th["vlo"]
    nr = net_rate_pct

    if nr >= hi:
        return 2.0
    elif nr >= mid:
        # [mid, hi] -> [1.0, 2.0]
        return 1.0 + (nr - mid) / (hi - mid)
    elif nr >= 0:
        # [0, mid] -> [0, 1.0]
        return (nr / mid) * 1.0 if mid != 0 else 0.0
    elif nr >= lo:
        # [lo, 0] -> [-1.0, 0]
        return (nr / lo) * 1.0 if lo != 0 else 0.0
    else:
        # [vlo, lo] -> [-2.0, -1.0]
        if vlo == lo:
            return -2.0
        return -1.0 + (nr - lo) / (vlo - lo)


def calc_strength_level(net_rate_pct: float, scale: str) -> str:
    """净额率(%) + 规模分档 → 5档判定词。

    Args:
        net_rate_pct: 净额率百分比
        scale: 规模分档

    Returns:
        "强" / "偏强" / "普通" / "偏弱" / "弱"
    """
    th = SCALE_THRESHOLDS[scale]
    hi, mid, lo, vlo = th["hi"], th["mid"], th["lo"], th["vlo"]
    nr = net_rate_pct

    if nr >= hi:
        return "强"
    elif nr >= mid:
        return "偏强"
    elif nr >= lo:
        return "普通"
    elif nr >= vlo:
        return "偏弱"
    else:
        return "弱"


def calc_strength(net_rate_pct: float, circ_mv_yi: float) -> Dict[str, Any]:
    """一站式单点强度计算。

    Args:
        net_rate_pct: 净额率百分比
        circ_mv_yi: 流通市值(亿元)

    Returns:
        {"value": float, "level": str, "scale": str}
    """
    scale = get_scale(circ_mv_yi)
    value = calc_strength_value(net_rate_pct, scale)
    level = calc_strength_level(net_rate_pct, scale)
    return {
        "value": round(value, 3),
        "level": level,
        "scale": scale,
    }


# ============================================================
# 聚合净额率计算（近n日）
# ============================================================
def calc_aggregate_net_rate(daily_records: List[Dict], n: int, circ_mv_yi: Optional[float] = None) -> Optional[float]:
    """从日记录列表算近n日聚合净额率(%)。

    聚合口径：
        近n日净额率 = 近n日主力净流入之和 ÷ 近n日成交额之和 × 100

    成交额近似优先级：
      1. 该日自身成交额（交易时段有效）
      2. 今日成交额（开盘前为0也无效）
      3. 流通市值 × 按规模分档的日均换手率（兜底：大盘1%/中盘2%/小盘3%）
    净流入累加必须包含所有有效历史估算值。

    Args:
        daily_records: 日记录列表，按时间倒序（今日在前），
                       每条含 net_flow(元), turnover(元)
        n: 窗口天数
        circ_mv_yi: 流通市值(亿元)，用于成交额兜底近似

    Returns:
        聚合净额率百分比，无数据返回 None
    """
    if not daily_records or n <= 0:
        return None

    # 取前n条（按时间倒序，今日=0, 昨日=1, ...）
    records = daily_records[:n]
    # 今日成交额：作为历史成交额缺失时的日均近似
    today_turnover = _to_float(records[0].get("turnover")) if records else None
    # 流通市值 × 规模分档换手率（circ_mv_yi 单位亿 → 元）
    scale = get_scale(circ_mv_yi) if circ_mv_yi else "小盘"
    rate = SCALE_TURNOVER_RATE.get(scale, 0.02)
    circ_mv_yuan = circ_mv_yi * 1e8 if circ_mv_yi else None
    fallback_turnover = circ_mv_yuan * rate if (circ_mv_yuan and circ_mv_yuan > 0) else None

    total_net = 0.0
    total_turnover = 0.0
    valid_count = 0

    for r in records:
        net = _to_float(r.get("net_flow") or r.get("main_net_flow"))
        if net is None:
            continue
        # 成交额缺失时用近似补齐：今日成交额 → 流通市值×2% 兜底
        turnover = _to_float(r.get("turnover"))
        if (turnover is None or turnover <= 0) and (today_turnover and today_turnover > 0):
            turnover = today_turnover
        if (turnover is None or turnover <= 0) and fallback_turnover:
            turnover = fallback_turnover
        if turnover is None or turnover <= 0:
            continue
        total_net += net
        total_turnover += turnover
        valid_count += 1

    if valid_count == 0 or total_turnover <= 0:
        return None

    return round(total_net / total_turnover * 100, 4)


def calc_aggregate_net_flow(daily_records: List[Dict], n: int) -> Optional[float]:
    """近n日主力净流入之和（元）。"""
    if not daily_records or n <= 0:
        return None
    records = daily_records[:n]
    total = 0.0
    valid = 0
    for r in records:
        net = _to_float(r.get("net_flow") or r.get("main_net_flow"))
        if net is not None:
            total += net
            valid += 1
    if valid == 0:
        return None
    return round(total, 2)


# ============================================================
# 板块级强度计算（含n日窗口判定）
# ============================================================
def calc_sector_strength(
    daily_records: List[Dict],
    circ_mv_yi: float,
    n: int,
) -> Dict[str, Any]:
    """一站式板块强度计算：近n日聚合 → 规模分档 → 5档判定。

    Args:
        daily_records: 日记录列表，按时间倒序（今日在前）
        circ_mv_yi: 流通市值(亿元)
        n: 强度判定窗口天数

    Returns:
        {
          "value": float,          # 连续强度值 -2~+2
          "level": str,            # 5档判定词
          "scale": str,            # 规模分档
          "net_rate_n": float,     # 近n日聚合净额率(%)
          "net_flow_n": float,     # 近n日净流入之和(元)
          "circ_mv_yi": float,     # 流通市值(亿)
        }
        无数据时 value=0, level="普通"
    """
    scale = get_scale(circ_mv_yi)
    net_rate_n = calc_aggregate_net_rate(daily_records, n, circ_mv_yi)
    net_flow_n = calc_aggregate_net_flow(daily_records, n)

    if net_rate_n is None:
        # 无足够数据，默认普通
        return {
            "value": 0.0,
            "level": "普通",
            "scale": scale,
            "net_rate_n": None,
            "net_flow_n": None,
            "circ_mv_yi": circ_mv_yi,
        }

    value = calc_strength_value(net_rate_n, scale)
    level = calc_strength_level(net_rate_n, scale)

    return {
        "value": round(value, 3),
        "level": level,
        "scale": scale,
        "net_rate_n": net_rate_n,
        "net_flow_n": net_flow_n,
        "circ_mv_yi": circ_mv_yi,
    }


# ============================================================
# 工具
# ============================================================
def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def level_to_color(level: str) -> str:
    """5档判定词 → 前端配色 hex。"""
    return {
        "强": "#e74c3c",       # 红
        "偏强": "#f39c12",     # 橙
        "普通": "#95a5a6",     # 灰
        "偏弱": "#3498db",     # 蓝
        "弱": "#2c3e50",       # 深蓝
    }.get(level, "#95a5a6")


if __name__ == "__main__":
    # 自测
    logging.basicConfig(level=logging.INFO)

    # 测试1：大盘 5% 净额率 → 强
    r = calc_strength(5.2, 50000)
    print(f"大盘 5.2%: value={r['value']}, level={r['level']}, scale={r['scale']}")

    # 测试2：小盘 3% 净额率 → 偏强
    r = calc_strength(3.0, 5000)
    print(f"小盘 3.0%: value={r['value']}, level={r['level']}, scale={r['scale']}")

    # 测试3：中盘 -2% → 偏弱
    r = calc_strength(-2.0, 20000)
    print(f"中盘 -2.0%: value={r['value']}, level={r['level']}, scale={r['scale']}")

    # 测试4：近5日聚合
    daily = [
        {"date": "2026-07-24", "net_flow": 5.24e8, "turnover": 45.2e8},
        {"date": "2026-07-23", "net_flow": -0.55e8, "turnover": 24.1e8},
        {"date": "2026-07-22", "net_flow": 0.71e8, "turnover": 29.1e8},
        {"date": "2026-07-21", "net_flow": -0.82e8, "turnover": 29.2e8},
        {"date": "2026-07-20", "net_flow": -1.33e8, "turnover": 25.6e8},
    ]
    agg_rate = calc_aggregate_net_rate(daily, 5)
    print(f"近5日聚合净额率: {agg_rate}%")
    r = calc_sector_strength(daily, circ_mv_yi=1232.53, n=5)
    print(f"地面兵装Ⅱ 5日判定: {r}")
