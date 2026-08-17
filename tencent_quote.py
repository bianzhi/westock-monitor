#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯股票实时行情接口封装（qt.gtimg.cn）。

背景：
    westock-data CLI 的 fund flow 接口只返回资金流字段，不返回板块流通市值、
    涨跌幅、换手率。腾讯自选股网页/App 显示的这些字段来自腾讯行情接口
    `qt.gtimg.cn/q=sh600519`，免费、无需 token、支持逗号批量查询。

字段映射（返回文本以 ~ 分割，0-based 下标）：
    1   名称
    3   最新价
    4   昨收
    32  涨跌幅(%)
    38  换手率(%)
    44  流通市值(亿元)   ← 腾讯自选股显示的"流通市值"
    45  总市值(亿元)
    46  市净率

实测（2026-08-17）：
    sh600519 贵州茅台：44=16100.18 亿 ≈ 1.61 万亿 ✅
    sz000858 五粮液：  44=2805.56 亿 ✅

用途：
    1. 板块流通市值 = 成分股流通市值累加（腾讯自己也是这么实时聚合的）
    2. 板块涨跌幅 / 换手率 = 成分股按流通市值加权
    3. 替代依赖 Tushare token 的方案 A，更可靠且免费
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 腾讯行情接口
TENCENT_QUOTE_URL = "http://qt.gtimg.cn/q="
TENCENT_BATCH_SIZE = 50          # 单次请求代码数量上限（实测逗号分隔 50 个稳定）
TENCENT_WORKERS = 4              # 并发线程数
TENCENT_TIMEOUT = 10             # 单次请求超时(秒)

# 字段下标（0-based，按 ~ 分割）
IDX_NAME = 1
IDX_PRICE = 3
IDX_PREV_CLOSE = 4
IDX_CHANGE_PCT = 32
IDX_TURNOVER_RATE = 38
IDX_CIRC_MV_YI = 44
IDX_TOTAL_MV_YI = 45

YI_TO_YUAN = 1e8  # 亿元 → 元


def _parse_quote_line(text: str) -> Optional[Dict]:
    """解析单条腾讯行情返回行 `v_sh600519="...~...";`。

    Args:
        text: 单条返回文本

    Returns:
        {code, name, price, prev_close, change_pct, turnover_rate,
         circ_mv_yi, total_mv_yi, circ_mv, total_mv}，解析失败返回 None
    """
    if not text or "=" not in text:
        return None
    # 提取 v_sh600519="..." 中的 code 和内容
    try:
        var_part, payload = text.split("=", 1)
        code = var_part.strip().replace("v_", "").strip()
        # 去掉引号和结尾分号
        payload = payload.strip().strip('"').strip(';')
        if not payload:
            return None
        parts = payload.split("~")
        if len(parts) < 46:
            # 字段不足，仍尝试拿关键字段（部分股票可能缺尾部字段）
            pass

        def _f(idx: int) -> Optional[float]:
            if idx >= len(parts):
                return None
            v = parts[idx].strip()
            if v == "" or v == "-":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        name = parts[IDX_NAME] if len(parts) > IDX_NAME else code
        circ_mv_yi = _f(IDX_CIRC_MV_YI)
        total_mv_yi = _f(IDX_TOTAL_MV_YI)
        return {
            "code": code,
            "name": name,
            "price": _f(IDX_PRICE),
            "prev_close": _f(IDX_PREV_CLOSE),
            "change_pct": _f(IDX_CHANGE_PCT),       # %
            "turnover_rate": _f(IDX_TURNOVER_RATE),  # %
            "circ_mv_yi": circ_mv_yi,                # 亿元
            "total_mv_yi": total_mv_yi,              # 亿元
            "circ_mv": circ_mv_yi * YI_TO_YUAN if circ_mv_yi is not None else None,   # 元
            "total_mv": total_mv_yi * YI_TO_YUAN if total_mv_yi is not None else None,
        }
    except Exception as e:
        logger.debug("parse_quote_line error: %s | text[:80]=%s", e, text[:80])
        return None


def _fetch_batch(codes: List[str]) -> Dict[str, Dict]:
    """拉取一批股票行情（单次 HTTP 请求）。"""
    import urllib.request

    url = TENCENT_QUOTE_URL + ",".join(codes)
    result: Dict[str, Dict] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=TENCENT_TIMEOUT) as resp:
            raw = resp.read()
        # 腾讯接口返回 GBK 编码
        try:
            text = raw.decode("gbk")
        except (UnicodeDecodeError, LookupError):
            text = raw.decode("utf-8", errors="replace")
        # 按行分割，每行一个 v_sh600519="..."
        for line in text.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            parsed = _parse_quote_line(line)
            if parsed:
                result[parsed["code"]] = parsed
    except Exception as e:
        logger.warning("tencent quote batch failed (%d codes): %s", len(codes), e)
    return result


def fetch_stock_quotes(codes: List[str]) -> Dict[str, Dict]:
    """批量查个股实时行情（腾讯 qt.gtimg.cn）。

    Args:
        codes: 股票代码列表（westock 格式，如 sh600519 / sz000858 / bj830799）

    Returns:
        {code: {name, price, prev_close, change_pct, turnover_rate,
                circ_mv_yi, total_mv_yi, circ_mv, total_mv}}
        查询失败/无数据的 code 不出现在结果中
    """
    if isinstance(codes, str):
        codes = [codes]
    if not codes:
        return {}

    # 去重保持顺序
    codes = list(dict.fromkeys(codes))
    logger.info("fetch_stock_quotes: %d stocks", len(codes))

    batches = [codes[i:i + TENCENT_BATCH_SIZE] for i in range(0, len(codes), TENCENT_BATCH_SIZE)]
    merged: Dict[str, Dict] = {}

    with ThreadPoolExecutor(max_workers=TENCENT_WORKERS) as pool:
        futures = [pool.submit(_fetch_batch, b) for b in batches]
        for f in as_completed(futures):
            try:
                merged.update(f.result())
            except Exception as e:
                logger.warning("fetch_stock_quotes batch error: %s", e)

    logger.info("fetch_stock_quotes: got %d/%d records", len(merged), len(codes))
    return merged


def aggregate_sector_metrics(quotes: Dict[str, Dict], stock_codes: List[str]) -> Dict:
    """按板块成分股聚合流通市值 / 涨跌幅 / 换手率。

    Args:
        quotes: fetch_stock_quotes 返回的个股行情
        stock_codes: 该板块的成分股代码列表

    Returns:
        {
            "circ_mv": 流通市值(元，成分股累加),
            "circ_mv_yi": 流通市值(亿元),
            "change_pct": 板块涨跌幅(%，按流通市值加权),
            "turnover_rate": 板块换手率(%，按流通市值加权),
            "stock_count": 成分股数,
            "valid_count": 有流通市值的成分股数,
            "fail_rate": 缺失率,
        }
    """
    total_mv = 0.0
    valid_count = 0
    # 加权涨跌幅/换手率累加器（按流通市值加权）
    weighted_change = 0.0
    weighted_turnover = 0.0

    for sc in stock_codes:
        q = quotes.get(sc)
        if not q or q.get("circ_mv") is None:
            continue
        mv = q["circ_mv"]
        total_mv += mv
        valid_count += 1
        if q.get("change_pct") is not None:
            weighted_change += q["change_pct"] * mv
        if q.get("turnover_rate") is not None:
            weighted_turnover += q["turnover_rate"] * mv

    if valid_count == 0:
        return {
            "circ_mv": None, "circ_mv_yi": None,
            "change_pct": None, "turnover_rate": None,
            "stock_count": len(stock_codes), "valid_count": 0,
            "fail_rate": 1.0,
        }

    circ_mv_yi = round(total_mv / YI_TO_YUAN, 2)
    return {
        "circ_mv": total_mv,
        "circ_mv_yi": circ_mv_yi,
        "change_pct": round(weighted_change / total_mv, 2) if total_mv else None,
        "turnover_rate": round(weighted_turnover / total_mv, 2) if total_mv else None,
        "stock_count": len(stock_codes),
        "valid_count": valid_count,
        "fail_rate": round(1 - valid_count / len(stock_codes), 4) if stock_codes else 1.0,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 自测：查两只个股，验证字段单位
    quotes = fetch_stock_quotes(["sh600519", "sz000858"])
    for code, q in quotes.items():
        print(f"{code} {q['name']}: 流通市值={q['circ_mv_yi']}亿, "
              f"总市值={q['total_mv_yi']}亿, 换手率={q['turnover_rate']}%, "
              f"涨跌幅={q['change_pct']}%")
