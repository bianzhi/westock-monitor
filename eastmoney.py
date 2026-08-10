#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东方财富板块资金流数据源（Tencent 未覆盖板块的 fallback）。

腾讯 fund_flow 缺失 10 个申万二级板块，东方财富有覆盖：
  林业Ⅱ、农业综合Ⅱ、其他家电Ⅱ、旅游零售Ⅱ、体育Ⅱ、
  本地生活服务Ⅱ、社交Ⅱ、其他银行Ⅱ、油气开采Ⅱ、医疗美容

API:
  - fund_flow(pt_codes) → List[Dict]
      返回格式与 westock.fund_flow 对齐（含 MainNetFlow / name 等字段）

认证：无需 token，东方财富 API 为公开接口。
注意：东方财富 API 可能仅限大陆 IP 访问。
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ============================================================
# 申万代码 → 东方财富 BK 代码映射
# ============================================================
SW_TO_BK: Dict[str, str] = {
    "801011": "BK1255",  # 林业Ⅱ
    "801019": "BK1257",  # 农业综合Ⅱ
    "801117": "BK1243",  # 其他家电Ⅱ
    "801207": "BK1269",  # 旅游零售Ⅱ
    "801216": "BK1273",  # 体育Ⅱ
    # 801217 本地生活服务Ⅱ — 自动发现
    # 801768 社交Ⅱ — 自动发现
    # 801786 其他银行Ⅱ — 自动发现
    "801961": "BK1276",  # 油气开采Ⅱ
    "801983": "BK1253",  # 医疗美容
}

# pt 代码 → 申万代码的反向映射（从 sw2021_sectors 复用逻辑）
_PT_TO_SW: Dict[str, str] = {}


def _init_pt_sw_map():
    """初始化 pt → sw 反向映射（从 sw2021_sectors 读取，避免硬编码）。"""
    global _PT_TO_SW
    if _PT_TO_SW:
        return
    try:
        from sw2021_sectors import SW_L2, SW_L2_PT_MAP
        for l2 in SW_L2:
            pt = SW_L2_PT_MAP.get(l2.code, "pt018" + l2.code[1:])
            _PT_TO_SW[pt] = l2.code
    except ImportError:
        logger.warning("eastmoney: cannot import sw2021_sectors, pt→sw map empty")


# ============================================================
# 东方财富 API 调用
# ============================================================
EM_BASE = "https://push2.eastmoney.com/api/qt"

# 通用请求头（东方财富对 UA/Referer 有校验）
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}


# requests session（连接复用 + 统一 UA/Referer）
_session = requests.Session()
_session.headers.update(_HEADERS)
# 禁用代理（走直连，避免代理干扰）
_session.trust_env = False

# 熔断器：连续失败 N 次后暂停调用，避免每次 fund_flow 都拖慢/刷错误日志
_CIRCUIT_FAILURES = 0
_CIRCUIT_THRESHOLD = 5       # 连续失败 5 次触发熔断
_CIRCUIT_COOLDOWN = 300       # 熔断冷却 5 分钟
_CIRCUIT_OPEN_TS: float = 0   # 熔断打开时间戳


def _circuit_ok() -> bool:
    """熔断器是否允许调用。"""
    global _CIRCUIT_FAILURES, _CIRCUIT_OPEN_TS
    if _CIRCUIT_FAILURES < _CIRCUIT_THRESHOLD:
        return True
    # 超过阈值：检查冷却时间
    if time.time() - _CIRCUIT_OPEN_TS > _CIRCUIT_COOLDOWN:
        logger.info("eastmoney: circuit breaker cooling down, retrying...")
        _CIRCUIT_FAILURES = 0
        _CIRCUIT_OPEN_TS = 0
        return True
    return False


def _circuit_record(success: bool):
    """记录一次调用结果，更新熔断状态。"""
    global _CIRCUIT_FAILURES, _CIRCUIT_OPEN_TS
    if success:
        _CIRCUIT_FAILURES = 0
        _CIRCUIT_OPEN_TS = 0
    else:
        _CIRCUIT_FAILURES += 1
        if _CIRCUIT_FAILURES >= _CIRCUIT_THRESHOLD:
            _CIRCUIT_OPEN_TS = time.time()
            logger.warning("eastmoney: circuit breaker OPEN after %d failures", _CIRCUIT_FAILURES)


def _em_get(path: str, params: Dict[str, str], timeout: int = 10) -> Optional[dict]:
    """调用东方财富 JSONP API，返回解析后的 dict。

    熔断期间直接返回 None，不发起 HTTP 请求。
    """
    if not _circuit_ok():
        logger.debug("eastmoney: circuit breaker open, skipping %s", path)
        return None

    url = f"{EM_BASE}/{path}"
    logger.debug("eastmoney GET %s params=%s", url, params)
    try:
        resp = _session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        _circuit_record(True)
        return resp.json()
    except Exception as e:
        logger.warning("eastmoney API error: %s (%s)", e, url[:120])
        _circuit_record(False)
        return None


def _get_sector_quote(bk_code: str, timeout: int = 10) -> Optional[Dict]:
    """获取单个东方财富板块的实时行情（含主力净流入）。"""
    data = _em_get("stock/get", {
        "secid": f"90.{bk_code}",
        "fields": "f43,f57,f58,f62,f66,f69,f78,f184",
    }, timeout=timeout)
    if not data or "data" not in data:
        return None
    return data["data"]


# ============================================================
# BK 代码查找（仅静态映射）
# ============================================================
# 注意：clist/get 行业列表接口在服务器上被东方财富拒绝（rc=102 / 连接重置），
# 因此无法通过名称自动发现 BK 代码。未在 SW_TO_BK 中的板块不做东方财富查询。
# 
# 3 个申万板块在东方财富也不存在独立行业分类：
#   801217 本地生活服务Ⅱ  — 东方财富无此行业
#   801768 社交Ⅱ          — 东方财富无此行业  
#   801786 其他银行Ⅱ      — 东方财富仅「银行Ⅱ」BK0475


def _find_bk_code(sw_code: str) -> Optional[str]:
    """查找申万代码对应的东方财富 BK 代码。"""
    return SW_TO_BK.get(sw_code)


# ============================================================
# fund_flow（对齐 westock.fund_flow 返回格式）
# ============================================================
def fund_flow(pt_codes: List[str], raw: bool = True,
              names: Optional[Dict[str, str]] = None) -> List[Dict]:
    """批量获取板块资金流（东方财富 fallback）。

    只查询腾讯未覆盖的板块（通过 SW_TO_BK 或自动发现识别）。
    返回格式与 westock.fund_flow 一致：
      [{code: "pt01801011", name: "林业Ⅱ", MainNetFlow: 1.23e8, ...}, ...]

    Args:
        pt_codes: westock pt 代码列表（如 pt01801011）
        raw: 固定 True（兼容 westock 签名）
        names: pt_code → sector_name 映射（用于自动发现 BK 代码）

    Returns:
        资金流记录列表（仅成功获取的板块）
    """
    _init_pt_sw_map()
    results: List[Dict] = []

    for pt in pt_codes:
        sw = _PT_TO_SW.get(pt)
        if not sw:
            continue

        # 确定板块名称
        sector_name = names.get(pt, "") if names else ""

        # 查 BK 代码
        bk = _find_bk_code(sw)
        if not bk:
            logger.debug("eastmoney: no BK code for %s (%s)", pt, sector_name)
            continue

        # 调东方财富 API
        quote = _get_sector_quote(bk)
        if not quote:
            logger.debug("eastmoney: no data for BK=%s (%s)", bk, sector_name)
            continue

        # 字段映射：东方财富 → westock/Tencent 格式
        # f62: 主力净流入（元）  f66: 超大单净流入  f78: 大单净流入
        # f184: 主力净流入净占比(%)  f43: 最新价
        main_net = quote.get("f62")
        super_large = quote.get("f66")
        large = quote.get("f78")
        close = quote.get("f43")

        # 换算主力净流入：东方财富 f62 单位是"元"，对齐 Tencent MainNetFlow
        main_net_float = None
        if main_net is not None and main_net != "-":
            try:
                main_net_float = float(main_net)
            except (ValueError, TypeError):
                pass

        record = {
            "code": pt,
            "SecuCode": pt,
            "name": sector_name or quote.get("f58", ""),
            "MainNetFlow": main_net_float,
            "ClosePrice": close if close and close != "-" else None,
            "MainInFlow": None,   # 东方财富不直接分开提供，可由 super_large+large 近似
            "MainOutFlow": None,
            "JumboNetFlow": super_large if super_large and super_large != "-" else None,
            "MidNetFlow": None,
            "SmallNetFlow": None,
            "RetailInFlow": None,
            "RetailOutFlow": None,
            "MainNetFlow5D": None,   # 东方财富板块 API 不提供 5D/10D/20D
            "MainNetFlow10D": None,
            "MainNetFlow20D": None,
            "BlockNetFlow": None,
            "MainInflowCircRate": None,
            "_source": "eastmoney",   # 标记数据来源
        }
        results.append(record)
        logger.debug("eastmoney: %s (%s) MainNetFlow=%s", pt, sector_name, main_net_float)

    return results


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 打印当前 BK 映射
    print("静态 BK 映射:")
    for sw, bk in SW_TO_BK.items():
        print(f"  {sw} → {bk}")

    # 拉取全量行业列表（测试 auto-discover）
    print("\n拉取东方财富行业列表...")
    sectors = _fetch_all_em_sectors()
    print(f"共 {len(sectors)} 个行业板块")

    # 测试查一个已知板块
    print("\n测试查询 BK1253 (医疗美容)...")
    quote = _get_sector_quote("BK1253")
    if quote:
        print(f"  name={quote.get('f58')}, MainNetFlow={quote.get('f62')}")
    else:
        print("  查询失败（可能非大陆 IP）")
