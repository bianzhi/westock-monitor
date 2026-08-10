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

from config import get_scale, SCALE_THRESHOLDS, TURNOVER_METHOD

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
    method: Optional[str] = None,
) -> Optional[float]:
    """计算板块成交额(元)。

    基金流字段解释：
      MainInFlow  = 主力主动买入金额（主力作为买方）
      MainOutFlow = 主力主动卖出金额（主力作为卖方）
      RetailInFlow  = 散户主动买入金额
      RetailOutFlow = 散户主动卖出金额
    每笔交易有一个买方和一个卖方，因此：
      成交额 = MainInFlow + RetailInFlow  (= MainOutFlow + RetailOutFlow)

    Args:
        record: fund flow 单条记录
        method: "main" 主力买入口径 / "all" 全市场口径 / "auto" 优先all。
                None 时使用 config.TURNOVER_METHOD（默认 "all"）

    Returns:
        成交额(元)，失败返回 None
    """
    if method is None:
        method = TURNOVER_METHOD

    inf = extract_main_inflow(record)
    outf = extract_main_outflow(record)
    ret_inf = extract_retail_inflow(record)
    ret_out = extract_retail_outflow(record)

    if method == "main":
        # 主力作为买方的交易额
        if inf is not None:
            return inf
        return None
    elif method == "all":
        # 全市场成交额 = 买方总和（主力买 + 散户买）
        if inf is not None and ret_inf is not None:
            return inf + ret_inf
        # 买方数据不全时尝试卖方
        if outf is not None and ret_out is not None:
            return outf + ret_out
        return None
    elif method == "auto":
        # 优先买方总和，缺失则卖方
        if inf is not None and ret_inf is not None:
            return inf + ret_inf
        if outf is not None and ret_out is not None:
            return outf + ret_out
        return None
    else:
        logger.warning("calc_turnover: unknown method %s", method)
        return None


def calc_net_rate(
    record: Dict,
    method: Optional[str] = None,
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
    turnover_method: Optional[str] = None,
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
    turnover_method: Optional[str] = None,
) -> List[Dict]:
    """批量计算板块指标。"""
    return [
        calc_sector_metrics(r, turnover_method)
        for r in records
        if isinstance(r, dict)
    ]


# ============================================================
# 累计字段口径验证（5D/10D/20D）
# ============================================================
# 实测结论（2026-07-31，westock-data-skillhub@1.0.5，板块 pt01801081 半导体）：
#   日期          today        5D          10D          20D
#   2026-07-31   +109.81亿   -526.86亿   -863.32亿   -2300.54亿
#   2026-07-17   -241.51亿   -1347.16亿  -1437.21亿  -2409.56亿
# 推理：
#   7-31 的 20D − 7-31 的 10D = -1437.22亿 ≈ 7-17 的 10D（-1437.21亿） ✓
#   说明 20D = 含今日的最近 20 个交易日累计；10D = 含今日的最近 10 个交易日累计
#   同理 5D = 含今日的最近 5 个交易日累计
# 因此 collect_daily_records 的分段差分分母 4/5/10 是正确的：
#   seg_1_4   = (5D  - today) / 4      ← 前 4 个交易日累计
#   seg_5_9   = (10D - 5D)    / 5      ← 前 5~9 个交易日累计
#   seg_10_19 = (20D - 10D)   / 10     ← 前 10~19 个交易日累计
# ============================================================
def verify_net_flow_window_consistency(record: Dict, tolerance: float = 0.02) -> Dict[str, Any]:
    """验证单条 fund flow 记录中 5D/10D/20D 累计字段的口径一致性。

    口径假设（实测成立）：
        5D  = 含今日的最近 5 个交易日累计
        10D = 含今日的最近 10 个交易日累计 → 必须有 10D - 5D ≈ (5D - today) 的同量级
        20D = 含今日的最近 20 个交易日累计 → 必须有 20D - 10D ≈ 10D - 5D 的同量级
    本函数只检查"嵌套包含关系"：今日值应被 5D 包含，5D 应被 10D 包含，10D 应被 20D 包含。
    若嵌套关系不成立，说明字段口径变化（westock 升级等），collect_daily_records 的分段差分会出错。

    Args:
        record: fund flow 单条记录，需含 MainNetFlow / MainNetFlow5D / 10D / 20D
        tolerance: 允许的相对误差（默认 2%，因累计值含浮点抖动）

    Returns:
        {
          "ok": bool,                   # 口径是否一致
          "today": float, "f5d": float, "f10d": float, "f20d": float,
          "checks": [
            {"name": "today_in_5d",  "ok": bool, "delta": float},
            {"name": "5d_in_10d",    "ok": bool, "delta": float},
            {"name": "10d_in_20d",   "ok": bool, "delta": float},
          ],
        }
    """
    today = _to_float(record.get("MainNetFlow"))
    f5d = _to_float(record.get("MainNetFlow5D"))
    f10d = _to_float(record.get("MainNetFlow10D"))
    f20d = _to_float(record.get("MainNetFlow20D"))

    def _nested_ok(bigger: Optional[float], smaller: Optional[float]) -> tuple:
        """bigger 应包含 smaller（同方向累计），且差值非负（同号时）。"""
        if bigger is None or smaller is None:
            return True, 0.0  # 缺字段不视为失败
        delta = bigger - smaller
        # 同号情况下 bigger 的绝对值应 >= smaller 的绝对值（容忍浮点抖动）
        ok = abs(bigger) + tolerance * max(abs(bigger), abs(smaller), 1.0) >= abs(smaller)
        return ok, delta

    checks = [
        {"name": "today_in_5d", "ok": _nested_ok(f5d, today)[0], "delta": _nested_ok(f5d, today)[1]},
        {"name": "5d_in_10d",   "ok": _nested_ok(f10d, f5d)[0],  "delta": _nested_ok(f10d, f5d)[1]},
        {"name": "10d_in_20d",  "ok": _nested_ok(f20d, f10d)[0], "delta": _nested_ok(f20d, f10d)[1]},
    ]
    return {
        "ok": all(c["ok"] for c in checks),
        "today": today, "f5d": f5d, "f10d": f10d, "f20d": f20d,
        "checks": checks,
    }


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

    # 口径自检：5D/10D/20D 嵌套包含关系
    print("\n=== 累计字段口径验证 ===")
    for r in r:
        v = verify_net_flow_window_consistency(r)
        print(f"{r.get('name')}: ok={v['ok']} today={v['today']} 5D={v['f5d']} 10D={v['f10d']} 20D={v['f20d']}")
        for c in v["checks"]:
            print(f"  {c['name']}: ok={c['ok']} delta={c['delta']}")
