# -*- coding: utf-8 -*-
"""缠论核心算法（移植自 yifangmoyan crates/czsc，除线段外完整版）。

实现链路：去包含 → 分型 → 笔 → 中枢 → 背驰(趋势/盘整) → 走势递归 → 三类买卖点 → 区间套。

数据格式：
    klines = [{dt, open, high, low, close, vol}, ...]（按时间升序）
所有 index 均为原始 K 线索引（与 MACD、前端 xAxis 对齐）。
"""


# ============================================================
# 1. 去除包含关系
# ============================================================
def remove_include(klines):
    """去除 K 线包含关系（向上取高高、向下取低低）。"""
    if not klines:
        return []
    if len(klines) == 1:
        k = klines[0]
        return [dict(k, id=0, elements=[0])]

    bars = [dict(klines[0], id=0, elements=[0]), dict(klines[1], id=1, elements=[1])]

    for i in range(2, len(klines)):
        k3 = klines[i]
        k1 = bars[-2]
        k2 = bars[-1]
        has_include = (k2["high"] <= k3["high"] and k2["low"] >= k3["low"]) or \
                      (k2["high"] >= k3["high"] and k2["low"] <= k3["low"])
        if not has_include:
            bars.append(dict(k3, id=i, elements=[i]))
            continue

        if k1["high"] < k2["high"]:
            high, low = max(k2["high"], k3["high"]), max(k2["low"], k3["low"])
            dt = k2["dt"] if k2["high"] > k3["high"] else k3["dt"]
        elif k1["high"] > k2["high"]:
            high, low = min(k2["high"], k3["high"]), min(k2["low"], k3["low"])
            dt = k2["dt"] if k2["low"] < k3["low"] else k3["dt"]
        else:
            bars.append(dict(k3, id=i, elements=[i]))
            continue

        open_ = high if k3["open"] > k3["close"] else low
        close = low if k3["open"] > k3["close"] else high
        vol = k2.get("vol", 0) + k3.get("vol", 0)
        elements = (k2["elements"] + [i])[-100:]
        bars[-1] = dict(id=k2["id"], dt=dt, open=open_, close=close,
                        high=high, low=low, vol=vol, elements=elements)
    return bars


# ============================================================
# 2. 分型识别
# ============================================================
def check_fxs(bars):
    """识别顶/底分型（顶底交替）。index 为 bars 索引，kline_index 为原始 K 线索引。"""
    if len(bars) < 3:
        return []
    raw = []
    for i in range(1, len(bars) - 1):
        k1, k2, k3 = bars[i - 1], bars[i], bars[i + 1]
        if k1["high"] < k2["high"] > k3["high"] and k1["low"] < k2["low"] > k3["low"]:
            raw.append({"type": "top", "index": i, "kline_index": k2["id"],
                        "dt": k2["dt"], "price": k2["high"]})
        elif k1["low"] > k2["low"] < k3["low"] and k1["high"] > k2["high"] < k3["high"]:
            raw.append({"type": "bottom", "index": i, "kline_index": k2["id"],
                        "dt": k2["dt"], "price": k2["low"]})

    result = [raw[0]] if raw else []
    for fx in raw[1:]:
        last = result[-1]
        if fx["type"] == last["type"]:
            if (fx["type"] == "top" and fx["price"] > last["price"]) or \
               (fx["type"] == "bottom" and fx["price"] < last["price"]):
                result[-1] = fx
        else:
            result.append(fx)
    return result


# ============================================================
# 3. 笔
# ============================================================
def build_bi(bars, fxs, min_bi_len=4):
    """由顶底交替的分型构建笔。si/ei 为原始 K 线索引。"""
    bis = []
    for i in range(len(fxs) - 1):
        a, b = fxs[i], fxs[i + 1]
        if a["type"] == b["type"]:
            continue
        if b["index"] - a["index"] < min_bi_len:
            continue
        direction = "up" if a["type"] == "bottom" else "down"
        start, end = a["price"], b["price"]
        bis.append({
            "dir": direction,
            "si": a["kline_index"], "ei": b["kline_index"],
            "start": start, "end": end,
            "high": max(start, end), "low": min(start, end),
            "start_dt": a["dt"], "end_dt": b["dt"],
        })
    return bis


# ============================================================
# 4. 中枢
# ============================================================
def build_zs(bis):
    """由连续三笔的重叠区间构建笔中枢。"""
    zss = []
    for i in range(len(bis) - 2):
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        zg = min(b1["high"], b2["high"], b3["high"])
        zd = max(b1["low"], b2["low"], b3["low"])
        if zg > zd:
            zss.append({"zg": zg, "zd": zd,
                        "gg": max(b1["high"], b2["high"], b3["high"]),
                        "dd": min(b1["low"], b2["low"], b3["low"]),
                        "si": b1["si"], "ei": b3["ei"],
                        "start_dt": b1["start_dt"], "end_dt": b3["end_dt"],
                        "type": "bi_zs"})
    return zss


# ============================================================
# 5. MACD 与背驰
# ============================================================
def calc_macd(klines, fast=12, slow=26, signal=9):
    """计算 MACD（EMA），返回 {dif, dea, macd} 三序列。"""
    closes = [k["close"] for k in klines]

    def _ema(vals, n):
        if not vals:
            return []
        k = 2.0 / (n + 1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    macd = [(d - e) * 2 for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "macd": macd}


def _section_power(sec, macd):
    """计算段力度：MACD 柱面积、峰值、DIF 极值（对齐 calculate_section_power）。"""
    hist = macd["macd"]
    dif = macd["dif"]
    if not hist:
        return 0.0, 0.0, 0.0
    start = min(sec["si"], len(hist) - 1)
    end = min(sec["ei"], len(hist) - 1)
    if start > end:
        return 0.0, 0.0, 0.0
    h = hist[start:end + 1]
    if sec["dir"] == "up":
        area = sum(v for v in h if v > 0)
        peak = max(h, default=0.0)
        dif_peak = max(dif[start:end + 1]) if dif else 0.0
    else:
        area = sum(-v for v in h if v < 0)
        peak = max((abs(v) for v in h), default=0.0)
        dif_peak = min(dif[start:end + 1]) if dif else 0.0
    return area, peak, dif_peak


def _group_zs_by_trend(zss):
    """按中枢递进方向分组：同方向无重叠递进归一组，方向变/重叠则开新组。"""
    if not zss:
        return []
    groups = [[zss[0]]]
    for i in range(1, len(zss)):
        prev, curr = zss[i - 1], zss[i]
        no_overlap_up = curr["zd"] > prev["zg"]
        no_overlap_down = curr["zg"] < prev["zd"]
        if (no_overlap_up or no_overlap_down) and groups[-1]:
            # 与组内方向一致则并入
            first, last = groups[-1][0], groups[-1][-1]
            dir_up = last["zd"] > first["zg"]
            if (dir_up and no_overlap_up) or ((not dir_up) and no_overlap_down):
                groups[-1].append(curr)
            else:
                groups.append([curr])
        else:
            groups.append([curr])
    return groups


def _classify_trend_direction(group):
    """趋势方向：后中枢高于前中枢 = 上涨，否则下跌。"""
    if len(group) < 2:
        return "up" if group[0]["zg"] > group[0]["zd"] else "down"
    return "up" if group[-1]["zg"] > group[0]["zg"] else "down"


def _check_trend_backdivergence(group, bis, macd):
    """趋势背驰：≥2中枢递进，b/c 段力度比较（面积缩小 OR DIF 背离）。"""
    if len(group) < 2:
        return None
    trend_dir = _classify_trend_direction(group)
    # 确认同方向无重叠递进
    for i in range(len(group) - 1):
        if trend_dir == "up" and group[i + 1]["zd"] <= group[i]["zg"]:
            return None
        if trend_dir == "down" and group[i + 1]["zg"] >= group[i]["zd"]:
            return None

    last_zs = group[-1]
    second_last = group[-2]

    # 找 b 段（倒数第二中枢 → 最后中枢）
    b_sec = None
    for sec in bis:
        if sec["dir"] == trend_dir and second_last["si"] <= sec["si"] < last_zs["si"] and sec["ei"] >= last_zs["si"]:
            b_sec = sec
            break
    if b_sec is None:
        for sec in bis:
            if sec["dir"] == trend_dir and second_last["si"] <= sec["si"] < last_zs["si"]:
                b_sec = sec
                break
    if b_sec is None:
        return None

    # 找 c 段（最后中枢离开段，价格突破中枢 + 创新高/低）
    zs_len = last_zs["ei"] - last_zs["si"]
    c_max_start = last_zs["ei"] + zs_len
    c_sec = None
    for sec in bis:
        if last_zs["si"] <= sec["si"] <= c_max_start and sec["dir"] == trend_dir:
            if trend_dir == "down" and sec["low"] < last_zs["zd"]:
                c_sec = sec
            elif trend_dir == "up" and sec["high"] > last_zs["zg"]:
                c_sec = sec
    if c_sec is None:
        return None

    # c 段必须创新高/新低
    if trend_dir == "up" and c_sec["high"] <= b_sec["high"]:
        return None
    if trend_dir == "down" and c_sec["low"] >= b_sec["low"]:
        return None

    b_area, b_peak, b_dif = _section_power(b_sec, macd)
    c_area, c_peak, c_dif = _section_power(c_sec, macd)
    cond_area = c_area < b_area
    cond_dif = (trend_dir == "up" and c_dif < b_dif) or (trend_dir == "down" and c_dif > b_dif)
    if not cond_area and not cond_dif:
        return None

    direction_label = "顶背驰" if trend_dir == "up" else "底背驰"
    return {
        "index": c_sec["ei"], "dt": c_sec["end_dt"], "price": c_sec["end"],
        "direction": trend_dir, "bc_sub_type": "trend",
        "reason": f"趋势{direction_label}(面积{'缩' if cond_area else '不缩'}+DIF{'背离' if cond_dif else '无'})",
    }


def _check_panzheng_backdivergence(zs, bis, macd):
    """盘整背驰：单中枢 a+A+b，b 段力度 < a 段力度且同方向。"""
    enter = [b for b in bis if b["ei"] <= zs["si"]]
    leave = [b for b in bis if b["si"] >= zs["ei"]]
    if not enter or not leave:
        return None
    a_sec = enter[-1]
    b_sec = leave[0]
    if a_sec["dir"] != b_sec["dir"]:
        return None
    a_area, _, _ = _section_power(a_sec, macd)
    b_area, _, _ = _section_power(b_sec, macd)
    if b_area >= a_area:
        return None
    direction = b_sec["dir"]
    direction_label = "顶背驰" if direction == "up" else "底背驰"
    return {
        "index": b_sec["ei"], "dt": b_sec["end_dt"], "price": b_sec["end"],
        "direction": direction, "bc_sub_type": "panzheng",
        "reason": f"盘整{direction_label}(b面积{b_area:.2f}<a面积{a_area:.2f})",
    }


def detect_beichi(bis, zss, macd):
    """背驰检测：趋势背驰 + 盘整背驰。"""
    results = []
    if len(bis) < 4 or not zss:
        return results
    for group in _group_zs_by_trend(zss):
        if len(group) >= 2:
            bd = _check_trend_backdivergence(group, bis, macd)
            if bd:
                results.append(bd)
            else:
                for zs in group:
                    pd_ = _check_panzheng_backdivergence(zs, bis, macd)
                    if pd_:
                        results.append(pd_)
        else:
            pd_ = _check_panzheng_backdivergence(group[0], bis, macd)
            if pd_:
                results.append(pd_)
    return results


# ============================================================
# 6. 走势递归
# ============================================================
def build_zoushi(zss):
    """走势递归：由中枢递进方向分组，标记上涨趋势/下跌趋势/盘整。"""
    zoushi = []
    for group in _group_zs_by_trend(zss):
        if len(group) >= 2:
            direction = _classify_trend_direction(group)
            zoushi.append({
                "type": "trend", "direction": direction,
                "zs_count": len(group),
                "si": group[0]["si"], "ei": group[-1]["ei"],
                "start_dt": group[0]["start_dt"], "end_dt": group[-1]["end_dt"],
            })
        else:
            zoushi.append({
                "type": "panzheng", "direction": "up" if group[0]["zg"] > group[0]["zd"] else "down",
                "zs_count": 1,
                "si": group[0]["si"], "ei": group[0]["ei"],
                "start_dt": group[0]["start_dt"], "end_dt": group[0]["end_dt"],
            })
    return zoushi


# ============================================================
# 7. 完整三类买卖点
# ============================================================
def _find_buy2_after_buy1(bis, buy1):
    """二买：一买后向上离开段 + 向下回抽段（不破一买=标准，破位=break）。"""
    after = [b for b in bis if b["si"] >= buy1["index"]]
    if len(after) < 2:
        return None
    found_up = False
    for seg in after:
        if not found_up and seg["dir"] == "up":
            found_up = True
            continue
        if found_up and seg["dir"] == "down":
            pullback_low = seg["end"]
            bs_type = "2buy" if pullback_low >= buy1["price"] else "2buy_break"
            return {"type": bs_type, "index": seg["ei"], "dt": seg["end_dt"],
                    "price": pullback_low, "reason": "一买后回抽"}
    return None


def _find_sell2_after_sell1(bis, sell1):
    """二卖：一卖后向下离开段 + 向上回抽段。"""
    after = [b for b in bis if b["si"] >= sell1["index"]]
    if len(after) < 2:
        return None
    found_down = False
    for seg in after:
        if not found_down and seg["dir"] == "down":
            found_down = True
            continue
        if found_down and seg["dir"] == "up":
            pullback_high = seg["end"]
            bs_type = "2sell" if pullback_high <= sell1["price"] else "2sell_break"
            return {"type": bs_type, "index": seg["ei"], "dt": seg["end_dt"],
                    "price": pullback_high, "reason": "一卖后回抽"}
    return None


def detect_buy_sell(bis, zss, beichi):
    """完整三类买卖点（对齐 buy_sell.rs，除线段外）。

    一买/一卖=趋势背驰；二买/二卖=一买/一卖后回抽；三买/三卖=中枢离开+回抽不回中枢。
    """
    points = []
    if len(bis) < 3 or not zss:
        return points

    # 一买/一卖：趋势背驰
    for bd in beichi:
        if bd["bc_sub_type"] != "trend":
            continue
        related = [zs for zs in zss if zs["si"] <= bd["index"]]
        if len(related) < 2:
            continue
        for group in _group_zs_by_trend(related):
            if len(group) < 2:
                continue
            trend_dir = _classify_trend_direction(group)
            if trend_dir == "down" and bd["direction"] == "down":
                points.append({"type": "1buy", "index": bd["index"], "dt": bd["dt"],
                               "price": bd["price"], "reason": "下跌趋势底背驰→一买"})
            elif trend_dir == "up" and bd["direction"] == "up":
                points.append({"type": "1sell", "index": bd["index"], "dt": bd["dt"],
                               "price": bd["price"], "reason": "上涨趋势顶背驰→一卖"})

    # 二买/二卖
    for p in list(points):
        if p["type"] == "1buy":
            bp = _find_buy2_after_buy1(bis, p)
            if bp:
                points.append(bp)
        elif p["type"] == "1sell":
            sp = _find_sell2_after_sell1(bis, p)
            if sp:
                points.append(sp)

    # 三买/三卖：中枢离开 + 回抽不回中枢
    for zs in zss:
        after = [b for b in bis if b["si"] >= zs["ei"]]
        if len(after) < 2:
            continue
        for i in range(len(after) - 1):
            leave, back = after[i], after[i + 1]
            has_newer = any(o["si"] > zs["ei"] and o["si"] <= leave["si"] for o in zss if o is not zs)
            if has_newer:
                continue
            if leave["dir"] == "up" and back["dir"] == "down":
                leave_high = max(leave["start"], leave["end"])
                if leave_high > zs["zg"]:
                    back_low = min(back["start"], back["end"])
                    if back_low >= zs["zg"]:
                        points.append({"type": "3buy", "index": back["ei"], "dt": back["end_dt"],
                                       "price": back_low, "reason": "中枢突破回踩不回中枢→三买"})
            if leave["dir"] == "down" and back["dir"] == "up":
                leave_low = min(leave["start"], leave["end"])
                if leave_low < zs["zd"]:
                    back_high = max(back["start"], back["end"])
                    if back_high <= zs["zd"]:
                        points.append({"type": "3sell", "index": back["ei"], "dt": back["end_dt"],
                                       "price": back_high, "reason": "中枢跌破反抽不回中枢→三卖"})
    return points


# ============================================================
# 8. 区间套
# ============================================================
def _is_buy_type(t):
    return t in ("1buy", "2buy", "2buy_break", "3buy")


def _is_sell_type(t):
    return t in ("1sell", "2sell", "2sell_break", "3sell")


def _qjt_strength(high_t, low_t):
    if high_t == "1buy" and low_t == "1buy":
        return "strong"
    if high_t == "1sell" and low_t == "1sell":
        return "strong"
    two_three_buy = ("2buy", "2buy_break", "3buy")
    two_three_sell = ("2sell", "2sell_break", "3sell")
    if (high_t == "1buy" and low_t in two_three_buy) or (high_t in two_three_buy and low_t == "1buy"):
        return "medium"
    if (high_t == "1sell" and low_t in two_three_sell) or (high_t in two_three_sell and low_t == "1sell"):
        return "medium"
    return "weak"


def detect_qujian_tao(high_bs, low_bs, high_name="日线", low_name="30F"):
    """区间套：大级别买卖点 + 小级别同向买卖点共振（时间窗口 ±30 根）。"""
    signals = []
    for h in high_bs:
        for l in low_bs:
            if not ((_is_buy_type(h["type"]) and _is_buy_type(l["type"])) or
                    (_is_sell_type(h["type"]) and _is_sell_type(l["type"]))):
                continue
            if abs(h["index"] - l["index"]) > 30:
                continue
            strength = _qjt_strength(h["type"], l["type"])
            signals.append({
                "signal_type": h["type"], "high_level": high_name, "low_level": low_name,
                "index": h["index"], "dt": h["dt"], "price": h["price"], "strength": strength,
            })
    # 去重：同 index + 同 type 保留最强
    seen = {}
    for s in signals:
        key = (s["index"], s["signal_type"])
        order = {"strong": 3, "medium": 2, "weak": 1}
        if key not in seen or order[s["strength"]] > order[seen[key]["strength"]]:
            seen[key] = s
    return list(seen.values())


# ============================================================
# 完整分析入口
# ============================================================
def analyze(klines):
    """完整缠论分析（除线段外）。

    Returns:
        {fxs, bis, zss, beichi, zoushi, buy_sell_points, latest_zs}
    """
    bars = remove_include(klines)
    fxs = check_fxs(bars)
    bis = build_bi(bars, fxs)
    zss = build_zs(bis)
    macd = calc_macd(klines)
    beichi = detect_beichi(bis, zss, macd)
    zoushi = build_zoushi(zss)
    bsp = detect_buy_sell(bis, zss, beichi)
    return {
        "fxs": fxs,
        "bis": bis,
        "zss": zss,
        "beichi": beichi,
        "zoushi": zoushi,
        "buy_sell_points": bsp,
        "latest_zs": zss[-1] if zss else None,
    }


if __name__ == "__main__":
    import math
    # 自测：构造「上涨→震荡中枢→突破」的走势
    kl = []
    for i in range(60):
        if i < 15:
            price = 100 + i * 0.5
        elif i < 35:
            price = 107.5 + 1.5 * math.sin((i - 15) * 0.7)
        else:
            price = 107.5 + 0.6 * (i - 35)
        kl.append({"dt": f"d{i}", "open": price - 0.15, "close": price + 0.15,
                   "high": price + 0.45, "low": price - 0.45, "vol": 1000.0})
    r = analyze(kl)
    print(f"分型 {len(r['fxs'])} 笔 {len(r['bis'])} 中枢 {len(r['zss'])} "
          f"背驰 {len(r['beichi'])} 走势 {len(r['zoushi'])} 买卖点 {len(r['buy_sell_points'])}")
    if r["beichi"]:
        print("背驰样例:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r["beichi"][0].items()})
