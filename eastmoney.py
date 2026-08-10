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
import urllib.request
from typing import Any, Dict, List, Optional

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


def _em_get(path: str, params: Dict[str, str], timeout: int = 10) -> Optional[dict]:
    """调用东方财富 JSONP API，返回解析后的 dict。"""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{EM_BASE}/{path}?{qs}"
    logger.debug("eastmoney GET %s", url)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except Exception as e:
        logger.warning("eastmoney API error: %s (%s)", e, url[:120])
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
# 名称 → BK 代码自动发现
# ============================================================
_NAME_TO_BK: Optional[Dict[str, str]] = None
_NAME_FETCH_TS: float = 0
_NAME_CACHE_TTL = 3600  # 1 小时刷新一次行业列表


def _fetch_all_em_sectors() -> Dict[str, str]:
    """从东方财富拉取全量行业板块列表，返回 name → BK code 映射。

    接口回包格式（每行一个板块）：
      {rc:0, data:{total:N, diff:[{f12:"BK1255", f14:"林业Ⅱ"}, ...]}}
    """
    global _NAME_TO_BK, _NAME_FETCH_TS
    now = time.time()
    if _NAME_TO_BK is not None and (now - _NAME_FETCH_TS) < _NAME_CACHE_TTL:
        return _NAME_TO_BK

    data = _em_get("clist/get", {
        "fid": "f62",
        "po": "1",
        "pz": "200",
        "pn": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": "m:90+t2",
        "fields": "f12,f14",
    }, timeout=15)

    result: Dict[str, str] = {}
    if data and data.get("rc") == 0 and "data" in data:
        diff = data["data"].get("diff") or []
        for item in diff:
            code = item.get("f12", "")
            name = item.get("f14", "")
            if code and name:
                result[name] = code
        logger.info("eastmoney: fetched %d industry sectors", len(result))
    else:
        logger.warning("eastmoney: failed to fetch sector list, rc=%s", data.get("rc") if data else "None")

    _NAME_TO_BK = result
    _NAME_FETCH_TS = now
    return result


def _find_bk_code(sw_code: str, sector_name: str) -> Optional[str]:
    """通过板块名称查找东方财富 BK 代码。"""
    # 先查静态映射
    bk = SW_TO_BK.get(sw_code)
    if bk:
        return bk

    # 名称自动发现
    em_sectors = _fetch_all_em_sectors()
    bk = em_sectors.get(sector_name)
    if bk:
        logger.info("eastmoney: auto-discovered BK for %s (%s) → %s", sw_code, sector_name, bk)
        SW_TO_BK[sw_code] = bk  # 缓存到静态映射
    return bk


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
        bk = _find_bk_code(sw, sector_name)
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
