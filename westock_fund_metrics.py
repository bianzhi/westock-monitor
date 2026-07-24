#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""westock fund flow 指标计算模块。

替代失效的 tencent_api.py。腾讯原 HTTP 接口已死
（`Can't load controller:FundTrendController`），
本模块基于 westock-data `fund flow` 的返回字段自算成交额 + 净额率。

fund flow 返回字段（实测）：
  MainNetFlow      主力净流入(元) = MainInFlow - MainOutFlow
  MainInFlow       主力流入额(元)
  MainOutFlow      主力流出额(元)
  RetailInFlow     散户流入额(元)
  RetailOutFlow    散户流出额(元)
  JumboNetFlow     超大单净额(元)
  BlockNetFlow     大单净额(元)
  MidNetFlow       中单净额(元)
  SmallNetFlow     小单净额(元)
  MainNetFlow5D    近5日累计主力净流入(元)
  MainNetFlow10D   近10日累计主力净流入(元)
  MainNetFlow20D   近20日累计主力净流入(元)
  MainInflowCircRate 主力净流入占流通市值比(%) —— 实测全为 0，不可用
  MainInflowRank        资金流入全市场排名
  MainInflowIndustryRank 资金流入行业内排名
  ClosePrice       收盘价
  EndDate          数据日期

成交额口径选择（通过 TURNOVER_METHOD 配置）：
  main    = MainInFlow + MainOutFlow              主力口径
  all     = MainInFlow + MainOutFlow + RetailInFlow + RetailOutFlow  全口径
  auto    = 优先 main，缺失则 all

净额率口径：
  净额率(%) = MainNetFlow / turnover × 100

流通市值：
  fund flow 不直接给流通市值。
  本模块提供 estimate_circ_mv_from_price()，用 ClosePrice × 估算股本 回推，
  但精度差。建议从 sector valuation 的 PE/PB 反推，或外部维护一份板块流通市值表。
"""
import logging
from typing import Any, Dict, List, Optional, Union

from config import get_scale, SCALE_THRESHOLDS

logger = logging.getLogger(__name__)


# ============================================================
# 字段提取
# ============================================================
def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def extract_main_net_flow(record: Dict) -> Optional[float]:
    """主力净流入(元) = MainInFlow - MainOutFlow。

    优先用 MainNetFlow 字段；缺失则用 MainInFlow - MainOutFlow 自算。
    """
    direct = _to_float(record.get("MainNetFlow"))
    if direct is not None:
        return direct
    inf = _to_float(record.get("MainInFlow"))
    outf = _to_float(record.get("MainOutFlow"))
    if inf is not None and outf is not None:
        return inf - outf
    return None


def extract_main_inflow(record: Dict) -> Optional[float]:
    return _to_float(record.get("MainInFlow"))


def extract_main_outflow(record: Dict) -> Optional[float]:
    return _to_float(record.get("MainOutFlow"))


def extract_retail_inflow(record: Dict) -> Optional[float]:
    return _to_float(record.get("RetailInFlow"))


def extract_retail_outflow(record: Dict) -> Optional[float]:
    return _to_float(record.get("RetailOutFlow"))


# ============================================================
# 成交额计算
# ============================================================
def calc_turnover(
    record: Dict,
    method: str = "main",
) -> Optional[float]:
    """计算板块成交额(元)。

    Args:
        record: fund flow 单条记录
        method: "main" 主力口径 / "all" 全口径 / "auto" 优先main

    Returns:
        成交额(元)，失败返回 None
    """
    inf = extract_main_inflow(record)
    outf = extract_main_outflow(record)
    ret_inf = extract_retail_inflow(record)
    ret_out = extract_retail_outflow(record)

    if method == "main":
        if inf is not None and outf is not None:
            return inf + outf
        return None
    elif method == "all":
        parts = [inf, outf, ret_inf, ret_out]
        if all(p is not None for p in parts):
            return inf + outf + ret_inf + ret_out
        return None
    elif method == "auto":
        # 优先 main，缺失则 all
        if inf is not None and outf is not None:
            return inf + outf
        parts = [inf, outf, ret_inf, ret_out]
        if all(p is not None for p in parts):
            return inf + outf + ret_inf + ret_out
        return None
    else:
        logger.warning("calc_turnover: unknown method %s", method)
        return None


def calc_net_rate(
    record: Dict,
    method: str = "main",
) -> Optional[float]:
    """计算净额率(%) = MainNetFlow / turnover × 100。

    Args:
        record: fund flow 单条记录
        method: 成交额口径

    Returns:
        净额率百分比，失败返回 None
    """
    net = extract_main_net_flow(record)
    turnover = calc_turnover(record, method)
    if net is None or turnover is None or turnover == 0:
        return None
    return round(net / turnover * 100, 4)


# ============================================================
# 多板块批量计算
# ============================================================
def calc_sector_metrics(
    record: Dict,
    turnover_method: str = "main",
) -> Dict[str, Any]:
    """从单条 fund flow 记录算出完整指标集。

    Returns:
        {
          "code": str,
          "name": str,
          "main_net_flow": float,        # 主力净流入(元)
          "main_inflow": float,           # 主力流入(元)
          "main_outflow": float,          # 主力流出(元)
          "turnover": float,              # 成交额(元)
          "net_rate": float,              # 净额率(%)
          "main_net_flow_5d": float,      # 近5日累计(元)
          "main_net_flow_10d": float,
          "main_net_flow_20d": float,
          "close_price": float,
          "end_date": str,
        }
    """
    return {
        "code": record.get("code") or record.get("SecuCode"),
        "name": record.get("name"),
        "main_net_flow": extract_main_net_flow(record),
        "main_inflow": extract_main_inflow(record),
        "main_outflow": extract_main_outflow(record),
        "retail_inflow": extract_retail_inflow(record),
        "retail_outflow": extract_retail_outflow(record),
        "turnover": calc_turnover(record, turnover_method),
        "net_rate": calc_net_rate(record, turnover_method),
        "main_net_flow_5d": _to_float(record.get("MainNetFlow5D")),
        "main_net_flow_10d": _to_float(record.get("MainNetFlow10D")),
        "main_net_flow_20d": _to_float(record.get("MainNetFlow20D")),
        "close_price": _to_float(record.get("ClosePrice")),
        "end_date": record.get("EndDate"),
    }


def calc_sector_metrics_batch(
    records: List[Dict],
    turnover_method: str = "main",
) -> List[Dict]:
    """批量计算板块指标。"""
    return [
        calc_sector_metrics(r, turnover_method)
        for r in records
        if isinstance(r, dict)
    ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 自测：用 fund flow 拿真实数据算净额率
    from westock import fund_flow
    r = fund_flow(["pt01801081", "pt01801743", "pt01801055"], raw=True)
    if r:
        print(f"\n=== {len(r)} 个板块指标 ===")
        metrics = calc_sector_metrics_batch(r, turnover_method="main")
        for m in metrics:
            print(f"\n{m['name']}({m['code']})")
            print(f"  主力净流入: {m['main_net_flow']/1e8:.2f}亿")
            print(f"  成交额(主力口径): {m['turnover']/1e8:.2f}亿")
            print(f"  净额率: {m['net_rate']}%")
            print(f"  近5日累计: {m['main_net_flow_5d']/1e8:.2f}亿" if m['main_net_flow_5d'] else "  近5日累计: None")
