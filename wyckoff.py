# -*- coding: utf-8 -*-
"""威科夫量价分析核心算法（移植自 yifangmoyan crates/wyckoff）。

实现威科夫三大法则：
  1. 供需法则（量价主导）
  2. 因果法则（交易区间 → 趋势运动）
  3. 努力与结果法则（成交量=努力，价格变动=结果）

数据格式：
    klines = [{dt, open, high, low, close, vol}, ...]（按时间升序）
"""


# ============================================================
# 1. 努力与结果法则
# ============================================================
def _classify_effort_result(effort, result, is_up):
    """判断量价协调/背离（对齐 effort.rs）。"""
    high_effort = effort > 1.3   # 成交量超均值 1.3 倍
    high_result = result > 0.015  # 振幅超 1.5%

    if high_effort and high_result:
        return ("harmonious", "demand_dominant" if is_up else "supply_dominant")
    if high_effort and not high_result:
        # 放量涨不动→供给出现；放量跌不动→需求出现
        return ("divergent", "supply_appearing" if is_up else "demand_appearing")
    if not high_effort and high_result:
        return ("divergent", "demand_dominant" if is_up else "supply_dominant")
    return ("neutral", "neutral")


def analyze_effort_result(klines):
    """对每根 K 线计算努力（量比）与结果（振幅），判断量价关系。

    Returns:
        [{index, dt, effort, result, harmony, interpretation}]
        harmony: harmonious(协调)/divergent(背离)/neutral
        interpretation: demand_dominant/supply_dominant/supply_appearing/demand_appearing/neutral
    """
    if len(klines) < 5:
        return []
    avg_vol = sum(k["vol"] for k in klines) / len(klines)
    results = []
    for i, k in enumerate(klines):
        effort = k["vol"] / avg_vol if avg_vol > 0 else 1.0
        result = abs(k["close"] - k["open"]) / k["open"] if k["open"] > 0 else 0.0
        is_up = k["close"] >= k["open"]
        harmony, interp = _classify_effort_result(effort, result, is_up)
        results.append({
            "index": i, "dt": k["dt"], "effort": round(effort, 2),
            "result": round(result, 4), "harmony": harmony, "interpretation": interp,
        })
    return results


def volume_trend(klines):
    """成交量趋势：后半段均值相对前半段的涨跌幅（>0 放量，<0 缩量）。"""
    if len(klines) < 6:
        return 0.0
    half = len(klines) // 2
    first = sum(k["vol"] for k in klines[:half]) / half
    second = sum(k["vol"] for k in klines[half:]) / (len(klines) - half)
    return (second - first) / first if first > 0 else 0.0


def detect_demand_appearing(klines, idx):
    """下跌中的需求出现：放量 + 跌幅极小/收阳。"""
    if idx < 5 or idx >= len(klines):
        return False
    avg_vol = sum(k["vol"] for k in klines[idx - 5:idx + 1]) / 6
    k = klines[idx]
    vol_surge = k["vol"] > avg_vol * 1.5
    small_drop = k["close"] < k["open"] and (k["open"] - k["close"]) / k["open"] < 0.01
    turn_up = k["close"] >= k["open"]
    return vol_surge and (small_drop or turn_up)


def detect_supply_appearing(klines, idx):
    """上涨中的供给出现：放量 + 涨幅极小/收阴。"""
    if idx < 5 or idx >= len(klines):
        return False
    avg_vol = sum(k["vol"] for k in klines[idx - 5:idx + 1]) / 6
    k = klines[idx]
    vol_surge = k["vol"] > avg_vol * 1.5
    small_rise = k["close"] > k["open"] and (k["close"] - k["open"]) / k["open"] < 0.01
    turn_down = k["close"] <= k["open"]
    return vol_surge and (small_rise or turn_down)


# ============================================================
# 2. 摆动点 + 阶段识别
# ============================================================
def find_swing_points(klines, n=3):
    """摆动点：n 根 K 线范围内的局部高低点。"""
    swings = []
    if len(klines) < 2 * n + 1:
        return swings
    for i in range(n, len(klines) - n):
        hi = [klines[j]["high"] for j in range(i - n, i + n + 1)]
        lo = [klines[j]["low"] for j in range(i - n, i + n + 1)]
        if klines[i]["high"] == max(hi):
            swings.append({"type": "high", "index": i, "dt": klines[i]["dt"], "price": klines[i]["high"]})
        elif klines[i]["low"] == min(lo):
            swings.append({"type": "low", "index": i, "dt": klines[i]["dt"], "price": klines[i]["low"]})
    return swings


def identify_phases(klines):
    """阶段识别（简化版）：上涨 / 下跌 / 吸筹 / 派发。

    基于近期价格变化率（10 根 K 线）判断趋势；横盘时按相对位置判吸筹/派发。
    """
    if len(klines) < 10:
        return []
    phases = []
    rolling_high = klines[0]["high"]
    for i, k in enumerate(klines):
        rolling_high = max(rolling_high, k["high"])
        if i < 10:
            phases.append({"index": i, "dt": k["dt"], "phase": "unknown"})
            continue
        lookback = 10
        start = klines[i - lookback]["close"]
        chg = (k["close"] - start) / start if start else 0.0
        if chg > 0.03:
            phase = "上涨"
        elif chg < -0.03:
            phase = "下跌"
        else:
            # 横盘：相对滚动高点的位置判断吸筹（低位）/派发（高位）
            phase = "吸筹" if k["close"] < rolling_high * 0.95 else "派发"
        phases.append({"index": i, "dt": k["dt"], "phase": phase})
    return phases


# ============================================================
# 完整分析入口
# ============================================================
def analyze(klines):
    """完整威科夫量价分析。

    Returns:
        {effort_results, volume_trend, swing_points, phases, latest_phase,
         demand_signals, supply_signals}
    """
    effort = analyze_effort_result(klines)
    swings = find_swing_points(klines)
    phases = identify_phases(klines)

    # 需求/供给出现信号（放量滞涨/滞跌）
    demand_signals = []
    supply_signals = []
    for i in range(len(klines)):
        if detect_demand_appearing(klines, i):
            demand_signals.append({"index": i, "dt": klines[i]["dt"], "price": klines[i]["close"]})
        if detect_supply_appearing(klines, i):
            supply_signals.append({"index": i, "dt": klines[i]["dt"], "price": klines[i]["close"]})

    return {
        "effort_results": effort,
        "volume_trend": round(volume_trend(klines), 4),
        "swing_points": swings,
        "phases": phases,
        "latest_phase": phases[-1]["phase"] if phases else None,
        "demand_signals": demand_signals,
        "supply_signals": supply_signals,
    }


if __name__ == "__main__":
    import math
    # 自测：构造「上涨→派发→下跌」走势
    kl = []
    for i in range(60):
        if i < 20:
            price = 100 + i * 0.8
        elif i < 40:
            price = 116 + 1.0 * math.sin((i - 20) * 0.5)
        else:
            price = 116 - 0.8 * (i - 40)
        vol = 1000 + (500 if 20 <= i < 40 else 0)  # 派发期放量
        kl.append({"dt": f"d{i}", "open": price - 0.2, "close": price + 0.2,
                   "high": price + 0.5, "low": price - 0.5, "vol": vol})
    r = analyze(kl)
    print(f"量价结果 {len(r['effort_results'])} 根, 摆动点 {len(r['swing_points'])} 个, "
          f"需求信号 {len(r['demand_signals'])} 个, 供给信号 {len(r['supply_signals'])} 个")
    print("最新阶段:", r["latest_phase"], "| 量能趋势:", r["volume_trend"])
