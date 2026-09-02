# -*- coding: utf-8 -*-
"""威科夫量价分析核心算法（移植自 yifangmoyan crates/wyckoff）。

实现威科夫三大法则：
  1. 供需法则（量价主导）
  2. 因果法则（交易区间 → 趋势运动）
  3. 努力与结果法则（成交量=努力，价格变动=结果）

数据格式：
    klines = [{dt, open, high, low, close, vol, amount}, ...]（按时间升序）
"""


def _qty(k):
    """量价分析的「量」：优先用成交额 amount（元，口径一致），回退 vol。

    westock 日线 volume 存在单位跳变（历史比今天少 100 倍），故统一改用 amount。
    """
    return k.get("amount") or k.get("vol") or 0


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
    avg_vol = sum(_qty(k) for k in klines) / len(klines)
    results = []
    for i, k in enumerate(klines):
        effort = _qty(k) / avg_vol if avg_vol > 0 else 1.0
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
    first = sum(_qty(k) for k in klines[:half]) / half
    second = sum(_qty(k) for k in klines[half:]) / (len(klines) - half)
    return (second - first) / first if first > 0 else 0.0


def detect_demand_appearing(klines, idx):
    """下跌中的需求出现：放量 + 跌幅极小/收阳。"""
    if idx < 5 or idx >= len(klines):
        return False
    avg_vol = sum(_qty(k) for k in klines[idx - 5:idx + 1]) / 6
    k = klines[idx]
    vol_surge = _qty(k) > avg_vol * 1.5
    small_drop = k["close"] < k["open"] and (k["open"] - k["close"]) / k["open"] < 0.01
    turn_up = k["close"] >= k["open"]
    return vol_surge and (small_drop or turn_up)


def detect_supply_appearing(klines, idx):
    """上涨中的供给出现：放量 + 涨幅极小/收阴。"""
    if idx < 5 or idx >= len(klines):
        return False
    avg_vol = sum(_qty(k) for k in klines[idx - 5:idx + 1]) / 6
    k = klines[idx]
    vol_surge = _qty(k) > avg_vol * 1.5
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
# 3. 形态事件检测
# ============================================================
def _rolling_avg(klines, start, end, fn):
    vals = [fn(k) for k in klines[start:end + 1]]
    return sum(vals) / len(vals) if vals else 0.0


def _add_event(events, event_type, i, k, price, desc):
    events.append({"event_type": event_type, "index": i, "dt": k["dt"],
                   "price": price, "description": desc})


def detect_events(klines):
    """威科夫形态事件检测。

    吸筹：PS(初步支撑)/SC(卖出高潮)/AR(自动反弹)/ST(二次测试)/Spring/SOS/LPS
    派发：PSY(初步供给)/BC(买入高潮)/UTAD/SOW/LPSY/JOC
    """
    if len(klines) < 10:
        return []
    events = []
    VOL_WINDOW = 60
    for i in range(5, len(klines) - 3):
        win_start = max(0, i - VOL_WINDOW)
        avg_vol = _rolling_avg(klines, win_start, i, lambda k: _qty(k))
        avg_spread = _rolling_avg(klines, win_start, i, lambda k: k["high"] - k["low"])
        k = klines[i]
        lookback = max(3, min(10, i))
        trend_down = k["close"] < klines[i - lookback]["close"]
        trend_up = k["close"] > klines[i - lookback]["close"]
        spread = k["high"] - k["low"]
        wide = spread > avg_spread * 2.0
        heavy = _qty(k) > avg_vol * 2.0
        surge = _qty(k) > avg_vol * 1.5

        # PS 初步支撑：下跌中放量但跌幅收窄
        prev_amp = abs(klines[i - 1]["open"] - klines[i - 1]["close"])
        curr_amp = abs(k["open"] - k["close"])
        if trend_down and surge and (curr_amp < prev_amp or k["close"] >= k["open"]):
            _add_event(events, "PS", i, k, k["low"], "初步支撑：下跌中放量但跌幅收窄")

        # SC 卖出高潮：下跌末端 + 宽幅 + 巨量 + 下影线收回
        if trend_down and wide and heavy and k["close"] > k["low"] + spread * 0.2:
            _add_event(events, "SC", i, k, k["low"], "卖出高潮：宽幅巨量下跌末端")

        # BC 买入高潮：上涨末端 + 宽幅 + 巨量
        if trend_up and wide and heavy:
            _add_event(events, "BC", i, k, k["high"], "买入高潮：宽幅巨量上涨末端")

        # PSY 初步供给：上涨中放量滞涨
        if trend_up and surge and k["close"] > k["open"]:
            rise = (k["close"] - k["open"]) / k["open"] if k["open"] else 0
            if rise < 0.01:
                _add_event(events, "PSY", i, k, k["high"], "初步供给：上涨中放量滞涨")

        # SC 后的事件（AR/Spring/ST）
        scs = [e for e in events if e["event_type"] == "SC" and 0 < i - e["index"] <= 10]
        if scs:
            # AR 自动反弹：SC 后放量反弹
            if k["close"] >= k["open"] and surge:
                _add_event(events, "AR", i, k, k["high"], "SC 后放量反弹")
            # Spring：跌破 SC 低点后收回
            for sc in scs:
                if k["low"] < sc["price"] and k["close"] > sc["price"]:
                    _add_event(events, "Spring", i, k, k["low"], "跌破 SC 低点后收回")
                    break
            # ST 二次测试：回测 SC 低点量缩
            if k["low"] <= scs[-1]["price"] * 1.02 and _qty(k) < avg_vol:
                _add_event(events, "ST", i, k, k["low"], "回测 SC 低点量缩")

        # SOS：放量突破 AR 高点
        ars = [e for e in events if e["event_type"] == "AR" and e["index"] < i]
        if ars and k["close"] > ars[-1]["price"] and surge:
            _add_event(events, "SOS", i, k, k["high"], "放量突破 AR 高点")

        # LPS：SOS 后缩量回调
        soss = [e for e in events if e["event_type"] == "SOS" and e["index"] < i]
        if soss and _qty(k) < avg_vol and k["low"] > soss[-1]["price"] * 0.97:
            _add_event(events, "LPS", i, k, k["low"], "SOS 后缩量回调获支撑")

        # JOC：放量突破阻力（突破近期高点）
        if k["close"] > max(klines[j]["high"] for j in range(max(0, i - 10), i)) and heavy:
            _add_event(events, "JOC", i, k, k["high"], "放量突破阻力")

    return events


# ============================================================
# 4. 交易区间识别
# ============================================================
def _find_range_end(klines, events, after_index, upper, lower):
    """交易区间结束：SOS/SOW/UTAD 事件，或价格持续 3 根在区间外。"""
    exits = [e for e in events if e["index"] > after_index and e["event_type"] in ("SOS", "SOW", "UTAD")]
    if exits:
        return exits[0]["index"]
    for i in range(after_index, len(klines)):
        if klines[i]["close"] > upper * 1.02 or klines[i]["close"] < lower * 0.98:
            if i + 3 <= len(klines) and all(
                klines[j]["close"] > upper * 1.02 or klines[j]["close"] < lower * 0.98
                for j in range(i, i + 3)
            ):
                return i
    return len(klines) - 1


def detect_trading_ranges(klines, events):
    """交易区间：SC+AR=吸筹区间，BC+AR=派发区间，否则摆动点分组。"""
    ranges = []
    scs = [e for e in events if e["event_type"] == "SC"]
    ars = [e for e in events if e["event_type"] == "AR"]
    bcs = [e for e in events if e["event_type"] == "BC"]

    for sc in scs:
        ar = next((a for a in ars if 0 < a["index"] - sc["index"] <= 10), None)
        if ar and ar["price"] > sc["price"]:
            upper, lower = ar["price"], sc["price"]
            end = _find_range_end(klines, events, ar["index"], upper, lower)
            ranges.append({"start_index": sc["index"], "end_index": end, "upper": upper,
                           "lower": lower, "ice_line": (upper + lower) / 2, "type": "accumulation"})

    for bc in bcs:
        ar = next((a for a in ars if 0 < a["index"] - bc["index"] <= 10), None)
        if ar and bc["price"] > ar["price"]:
            upper, lower = bc["price"], ar["price"]
            end = _find_range_end(klines, events, ar["index"], upper, lower)
            ranges.append({"start_index": bc["index"], "end_index": end, "upper": upper,
                           "lower": lower, "ice_line": (upper + lower) / 2, "type": "distribution"})

    if not ranges:
        swings = find_swing_points(klines, 5)
        highs = [s for s in swings if s["type"] == "high"]
        lows = [s for s in swings if s["type"] == "low"]
        if len(highs) >= 2 and len(lows) >= 2:
            res_avg = sum(s["price"] for s in highs) / len(highs)
            sup_avg = sum(s["price"] for s in lows) / len(lows)
            if res_avg > sup_avg and (res_avg - sup_avg) / sup_avg < 0.20:
                ranges.append({"start_index": min(s["index"] for s in swings),
                               "end_index": max(s["index"] for s in swings),
                               "upper": res_avg, "lower": sup_avg,
                               "ice_line": (res_avg + sup_avg) / 2, "type": "range"})
    return ranges


# ============================================================
# 5. 供需线
# ============================================================
def draw_supply_demand_lines(klines, ranges):
    """供需线：摆动点趋势线（供给线/需求线）+ 交易区间上沿/下沿/冰线。"""
    lines = []
    swings = find_swing_points(klines, 5)
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    if len(highs) >= 2:
        p1, p2 = highs[-2], highs[-1]
        slope = (p2["price"] - p1["price"]) / (p2["index"] - p1["index"]) if p2["index"] > p1["index"] else 0
        lines.append({"line_type": "supply", "start_index": p1["index"], "end_index": p2["index"],
                      "start_price": p1["price"], "end_price": p2["price"], "slope": slope})
    if len(lows) >= 2:
        p1, p2 = lows[-2], lows[-1]
        slope = (p2["price"] - p1["price"]) / (p2["index"] - p1["index"]) if p2["index"] > p1["index"] else 0
        lines.append({"line_type": "demand", "start_index": p1["index"], "end_index": p2["index"],
                      "start_price": p1["price"], "end_price": p2["price"], "slope": slope})

    for tr in ranges:
        lines.append({"line_type": "supply", "start_index": tr["start_index"], "end_index": tr["end_index"],
                      "start_price": tr["upper"], "end_price": tr["upper"], "slope": 0})
        lines.append({"line_type": "demand", "start_index": tr["start_index"], "end_index": tr["end_index"],
                      "start_price": tr["lower"], "end_price": tr["lower"], "slope": 0})
        lines.append({"line_type": "ice_line", "start_index": tr["start_index"], "end_index": tr["end_index"],
                      "start_price": tr["ice_line"], "end_price": tr["ice_line"], "slope": 0})
    return lines


# ============================================================
# 完整分析入口（标注整合）
# ============================================================
def analyze(klines):
    """完整威科夫量价分析。

    Returns:
        {effort_results, volume_trend, swing_points, phases, latest_phase,
         events, trading_ranges, supply_demand_lines, demand_signals, supply_signals}
    """
    effort = analyze_effort_result(klines)
    swings = find_swing_points(klines)
    phases = identify_phases(klines)
    events = detect_events(klines)
    ranges = detect_trading_ranges(klines, events)
    sdl = draw_supply_demand_lines(klines, ranges)

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
        "events": events,
        "trading_ranges": ranges,
        "supply_demand_lines": sdl,
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
