# -*- coding: utf-8 -*-
"""缠论核心算法（移植自 yifangmoyan crates/czsc）。

实现链路：去包含 → 分型 → 笔 → 中枢 → 三类买卖点。

数据格式：
    klines = [{dt, open, high, low, close, vol}, ...]（按时间升序）
输出字段见各函数 docstring。价格单位为元，成交量单位自定。
"""


# ============================================================
# 1. 去除包含关系
# ============================================================
def remove_include(klines):
    """去除 K 线包含关系。

    向上趋势取「高高」（高点取大、低点取大），向下趋势取「低低」。
    方向由已确认的最后两根无包含 K 线 k1、k2 决定。

    Returns:
        [{id, dt, open, high, low, close, vol, elements}]
    """
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

        # 判断 k2/k3 是否包含
        has_include = (k2["high"] <= k3["high"] and k2["low"] >= k3["low"]) or \
                      (k2["high"] >= k3["high"] and k2["low"] <= k3["low"])

        if not has_include:
            bars.append(dict(k3, id=i, elements=[i]))
            continue

        if k1["high"] < k2["high"]:
            # 向上：取高高
            high = max(k2["high"], k3["high"])
            low = max(k2["low"], k3["low"])
            dt = k2["dt"] if k2["high"] > k3["high"] else k3["dt"]
        elif k1["high"] > k2["high"]:
            # 向下：取低低
            high = min(k2["high"], k3["high"])
            low = min(k2["low"], k3["low"])
            dt = k2["dt"] if k2["low"] < k3["low"] else k3["dt"]
        else:
            # k1/k2 高点相等，无法定方向，k3 作为新 K 线
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
    """识别顶/底分型（顶底交替）。

    顶分型：k2 的高、低点都高于 k1 和 k3。
    底分型：k2 的高、低点都低于 k1 和 k3。
    连续同类型只保留更极端的那个（顶取更高、底取更低）。

    Returns:
        [{type: 'top'|'bottom', index, dt, price}]
    """
    if len(bars) < 3:
        return []

    raw = []
    for i in range(1, len(bars) - 1):
        k1, k2, k3 = bars[i - 1], bars[i], bars[i + 1]
        if k1["high"] < k2["high"] > k3["high"] and k1["low"] < k2["low"] > k3["low"]:
            raw.append({"type": "top", "index": i, "dt": k2["dt"], "price": k2["high"]})
        elif k1["low"] > k2["low"] < k3["low"] and k1["high"] > k2["high"] < k3["high"]:
            raw.append({"type": "bottom", "index": i, "dt": k2["dt"], "price": k2["low"]})

    # 顶底交替：连续同类型保留更极端的
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
    """由顶底交替的分型构建笔。

    相邻顶底分型连成一笔：底→顶 = 向上笔，顶→底 = 向下笔。
    笔至少跨越 min_bi_len 根合并 K 线（索引差）。

    Returns:
        [{dir: 'up'|'down', si, ei, start, end, high, low}]
    """
    bis = []
    for i in range(len(fxs) - 1):
        a, b = fxs[i], fxs[i + 1]
        if a["type"] == b["type"]:
            continue
        if b["index"] - a["index"] < min_bi_len:
            continue
        direction = "up" if a["type"] == "bottom" else "down"
        start = a["price"]
        end = b["price"]
        high = max(start, end)
        low = min(start, end)
        bis.append({
            "dir": direction, "si": a["index"], "ei": b["index"],
            "start": start, "end": end, "high": high, "low": low,
            "start_dt": a["dt"], "end_dt": b["dt"],
        })
    return bis


# ============================================================
# 4. 中枢
# ============================================================
def build_zs(bis):
    """由连续三笔的重叠区间构建笔中枢。

    中枢区间 zg = min(三笔高点)，zd = max(三笔低点)；zg > zd 才构成中枢。

    Returns:
        [{zg, zd, gg, dd, si, ei, type: 'bi_zs'}]
    """
    zss = []
    i = 0
    while i + 2 < len(bis):
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        zg = min(b1["high"], b2["high"], b3["high"])
        zd = max(b1["low"], b2["low"], b3["low"])
        gg = max(b1["high"], b2["high"], b3["high"])
        dd = min(b1["low"], b2["low"], b3["low"])
        if zg > zd:
            zss.append({"zg": zg, "zd": zd, "gg": gg, "dd": dd,
                        "si": b1["si"], "ei": b3["ei"],
                        "start_dt": b1["start_dt"], "end_dt": b3["end_dt"],
                        "type": "bi_zs"})
        i += 1
    return zss


# ============================================================
# 5. 三类买卖点（简化：基于中枢与趋势）
# ============================================================
def detect_buy_sell(bis, zss):
    """检测三类买卖点（简化版）。

    - 一类买/卖点：中枢下方/上方出现反向笔（趋势背驰近似）。
    - 二类买/卖点：一类点后第一次回抽。
    - 三类买/卖点：中枢突破后回踩不回中枢。

    Returns:
        [{type: 'b1'|'b2'|'b3'|'s1'|'s2'|'s3', dt, price}]
    """
    points = []
    if not bis or not zss:
        return points

    # 取最近一个中枢
    zs = zss[-1]
    zg, zd = zs["zg"], zs["zd"]

    # 遍历笔，找离开中枢后的回抽
    for idx, bi in enumerate(bis):
        if bi["ei"] < zs["si"]:
            continue  # 笔在中枢之前

        # 一类/三类买点：向下笔跌破中枢下沿后，出现向上笔（回踩）
        if bi["dir"] == "down" and bi["end"] < zd:
            # 找后续向上笔作为买点
            for j in range(idx + 1, len(bis)):
                nb = bis[j]
                if nb["dir"] == "up":
                    if nb["start"] > zd:
                        points.append({"type": "b3", "dt": nb["start_dt"], "price": nb["start"]})
                    else:
                        points.append({"type": "b1", "dt": nb["start_dt"], "price": nb["start"]})
                    break

        # 一类/三类卖点：向上笔突破中枢上沿后，出现向下笔（回抽）
        if bi["dir"] == "up" and bi["end"] > zg:
            for j in range(idx + 1, len(bis)):
                nb = bis[j]
                if nb["dir"] == "down":
                    if nb["start"] < zg:
                        points.append({"type": "s3", "dt": nb["start_dt"], "price": nb["start"]})
                    else:
                        points.append({"type": "s1", "dt": nb["start_dt"], "price": nb["start"]})
                    break

    return points


# ============================================================
# 完整分析入口
# ============================================================
def analyze(klines):
    """完整缠论分析。

    Returns:
        {fxs, bis, zss, buy_sell_points, latest_zs}
    """
    bars = remove_include(klines)
    fxs = check_fxs(bars)
    bis = build_bi(bars, fxs)
    zss = build_zs(bis)
    bsp = detect_buy_sell(bis, zss)
    return {
        "fxs": fxs,
        "bis": bis,
        "zss": zss,
        "buy_sell_points": bsp,
        "latest_zs": zss[-1] if zss else None,
    }


if __name__ == "__main__":
    import math
    # 自测：构造「上涨→震荡中枢→突破」的走势（正弦波震荡，分型有间隔）
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
    print(f"分型 {len(r['fxs'])} 个, 笔 {len(r['bis'])} 笔, 中枢 {len(r['zss'])} 个, 买卖点 {len(r['buy_sell_points'])} 个")
    if r["latest_zs"]:
        print("最近中枢:", {k: round(v, 2) for k, v in r["latest_zs"].items() if isinstance(v, (int, float))})
