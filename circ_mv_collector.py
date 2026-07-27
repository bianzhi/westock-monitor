#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块流通市值采集模块。

背景：
    westock-data CLI 不直接提供板块流通市值：
      - `sector info` 实测返回 [null]
      - `sector valuation` 只有 PE/PB/PCF/PS
      - `fund flow` 板块级 MainInflowCircRate 全为 0
    Tushare 板块级市值接口（sw_daily / index_dailybasic）对当前 token 无权限。

方案 A（主，精度高）：Tushare 个股直取 + westock 成分股累加
    1. westock `sector constituent` 拿板块真实成分股（实测半导体 182 只）
    2. Tushare `daily_basic(trade_date)` 一次拿全市场个股 circ_mv（5526 只，万元）
    3. 按 westock 成分股代码匹配 Tushare 个股 circ_mv，按板块累加
    4. 单股精度高（茅台实测 16151 亿），累加量级正确

方案 B（兜底，Tushare 不可用时）：westock fund flow 反推
    1. westock `sector constituent` 拿成分股
    2. 个股 `fund flow` 拿 MainNetFlow + MainInflowCircRate
    3. 个股 circ_mv = |MainNetFlow| / MainInflowCircRate × 100
    4. 板块 circ_mv = 成分股累加
    精度限制：rate 最小精度 0.01%，小额 mnf 反推失真，量级可用

代码格式转换：
    westock 成分股代码 sh600519 → Tushare 600519.SH
    sz开头 → .SZ，bj开头 → .BJ

调用频率：
    流通市值日内变化小，建议每日采集 1-2 次（开盘后 + 午盘）。
    Storage 层缓存当日值，API 直接读缓存。
"""
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from concurrent.futures import ThreadPoolExecutor, as_completed

from config import WESTOCK_BATCH_SIZE, WESTOCK_WORKERS
from westock import (
    sector_constituent, fund_flow,
    extract_main_net_flow, extract_main_inflow_circ_rate,
)

logger = logging.getLogger(__name__)

# Tushare 配置
TUSHARE_TOKEN_ENV = "SYSTEM_TUSHARE_TOKEN"
TUSHARE_DAILY_BASIC_FIELDS = "ts_code,trade_date,circ_mv,total_mv"
# Tushare circ_mv 单位：万元；westock/项目内部流通市值单位：元
WAN_TO_YUAN = 1e4
YI_TO_YUAN = 1e8

# westock 反推方案的安全阈值（方案 B 兜底）
MIN_CIRC_RATE = 0.001
MAX_CIRC_RATE = 50.0
MAX_SINGLE_STOCK_MV = 5e12

# Tushare client 单例（延迟初始化，token 从环境变量读）
_tushare_pro = None
_tushare_lock = threading.Lock()


# ============================================================
# Tushare 客户端
# ============================================================
def _get_tushare_pro():
    """延迟初始化 Tushare pro 客户端单例。

    Token 从环境变量 SYSTEM_TUSHARE_TOKEN 读取。
    未配置 token 返回 None，调用方应回退到兜底方案。
    """
    global _tushare_pro
    if _tushare_pro is not None:
        return _tushare_pro
    with _tushare_lock:
        if _tushare_pro is not None:
            return _tushare_pro
        token = os.environ.get(TUSHARE_TOKEN_ENV, "").strip()
        if not token:
            logger.warning("Tushare token 未配置（环境变量 %s），将回退到 westock 反推方案",
                           TUSHARE_TOKEN_ENV)
            return None
        try:
            import tushare as ts
            ts.set_token(token)
            _tushare_pro = ts.pro_api()
            logger.info("Tushare pro 客户端初始化成功")
        except ImportError:
            logger.warning("tushare 包未安装，将回退到 westock 反推方案")
            return None
        except Exception as e:
            logger.warning("Tushare 客户端初始化失败: %s，将回退到 westock 反推方案", e)
            return None
    return _tushare_pro


def _westock_to_tushare(wcode: str) -> str:
    """westock 个股代码 → Tushare 个股代码。

    sh600519 → 600519.SH
    sz000001 → 000001.SZ
    bj830799 → 830799.BJ
    """
    if not wcode:
        return wcode
    if wcode.startswith("sh"):
        return wcode[2:] + ".SH"
    if wcode.startswith("sz"):
        return wcode[2:] + ".SZ"
    if wcode.startswith("bj"):
        return wcode[2:] + ".BJ"
    return wcode


def _fetch_tushare_daily_basic(trade_date: str) -> Dict[str, float]:
    """拿 Tushare 全市场单日个股 circ_mv。

    Args:
        trade_date: YYYYMMDD

    Returns:
        {tushare_ts_code: circ_mv_元}，空 dict 表示失败/无数据
    """
    pro = _get_tushare_pro()
    if pro is None:
        return {}
    try:
        df = pro.daily_basic(trade_date=trade_date,
                             fields=TUSHARE_DAILY_BASIC_FIELDS)
        if df is None or df.empty:
            return {}
        # circ_mv 单位万元 → 元
        return {
            row.ts_code: float(row.circ_mv) * WAN_TO_YUAN
            for row in df.itertuples(index=False)
            if row.circ_mv is not None and not str(row.circ_mv).strip() == ""
        }
    except Exception as e:
        logger.warning("Tushare daily_basic %s 失败: %s", trade_date, e)
        return {}


def _find_valid_trade_date(preferred: str, lookback: int = 7) -> str:
    """找一个有数据的交易日（向后回溯）。

    Tushare daily_basic 在非交易日/当日盘前无数据，回溯最近 lookback 天。
    周末/节假日跳过。

    Args:
        preferred: 首选日期 YYYYMMDD
        lookback: 最多回溯天数

    Returns:
        实际可用的 trade_date YYYYMMDD，失败返回 preferred
    """
    try:
        d = datetime.strptime(preferred, "%Y%m%d").date()
    except Exception:
        return preferred
    for i in range(lookback):
        check = d - timedelta(days=i)
        # 跳过周末（5=周六, 6=周日）
        if check.weekday() >= 5:
            continue
        return check.strftime("%Y%m%d")
    return preferred


# ============================================================
# westock 成分股采集（两方案共用）
# ============================================================
def collect_constituents(codes: List[str]) -> Dict[str, List[Dict]]:
    """批量采集多板块成分股。

    Args:
        codes: pt 代码列表

    Returns:
        {pt_code: [{code, name, SectorCode}, ...], ...}
    """
    if not codes:
        return {}
    logger.info("collect_constituents: %d sectors", len(codes))
    return sector_constituent(codes)


def collect_constituents_all_sectors() -> Dict[str, List[str]]:
    """采集全部 134 板块的成分股，返回 {pt_code: [westock_stock_code, ...]}。

    分批调用 sector_constituent，避免单次请求过多板块。
    """
    from sectors import get_default_codes
    all_codes = get_default_codes()
    logger.info("collect_constituents_all_sectors: %d sectors", len(all_codes))

    batch_size = 10  # 实测 10 个板块一次调用稳定
    result: Dict[str, List[str]] = {}

    for i in range(0, len(all_codes), batch_size):
        batch = all_codes[i:i + batch_size]
        try:
            cons_map = collect_constituents(batch)
            for pt, stocks in cons_map.items():
                result[pt] = [s.get("code") for s in stocks if s.get("code")]
        except Exception as e:
            logger.warning("collect_constituents batch %d-%d failed: %s",
                          i, i + len(batch), e)

    logger.info("collect_constituents_all_sectors: done, %d sectors, %d stocks total",
                len(result), sum(len(v) for v in result.values()))
    return result


# ============================================================
# 方案 A：Tushare 直取 + westock 成分股累加
# ============================================================
def collect_all_sectors_circ_mv(
    stock_map: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Dict]:
    """采集全部 134 板块的流通市值（主方案 A，兜底方案 B）。

    Args:
        stock_map: 可选，预采集的 {pt_code: [westock_stock_code, ...]}。
                   不传则自动采集成分股。

    Returns:
        {pt_code: {
            "circ_mv": float,         # 流通市值（元），失败为 None
            "circ_mv_yi": float,      # 流通市值（亿元）
            "stock_count": int,
            "valid_count": int,
            "skip_count": int,
            "fail_rate": float,
            "is_estimated": bool,     # 方案 B 兜底时为 True
            "source": str,            # "tushare" | "westock_reverse" | "mixed"
            "trade_date": str,        # YYYYMMDD
        }, ...}
    """
    from sectors import get_default_sector_map

    start_ts = time.time()
    today_str = date.today().strftime("%Y%m%d")

    # 1. 采集成分股（如果没预传）
    if stock_map is None:
        stock_map = collect_constituents_all_sectors()

    if not stock_map:
        logger.warning("collect_all_sectors_circ_mv: no stock_map, abort")
        return {}

    # 板块清单：预传 stock_map 时只处理它的板块，否则用默认全 134 板块
    all_pt_codes = list(stock_map.keys())

    # 2. 扁平化所有成分股，去重
    # 坑：collect_constituents 返回 List[Dict]（每只 {code,name,SectorCode}），
    # 但本函数期望 List[str] 股票代码。统一提取为字符串列表。
    all_stocks = set()
    for stocks in stock_map.values():
        for s in stocks:
            wcode = s.get("code") if isinstance(s, dict) else s
            if wcode:
                all_stocks.add(wcode)
    logger.info("collect_all_sectors_circ_mv: %d unique stocks across %d sectors",
                len(all_stocks), len(stock_map))

    # 3. 尝试方案 A：Tushare 直取
    pro = _get_tushare_pro()
    mv_map: Dict[str, float] = {}  # tushare_ts_code → circ_mv_元
    use_tushare = pro is not None

    if use_tushare:
        valid_date = _find_valid_trade_date(today_str)
        mv_map = _fetch_tushare_daily_basic(valid_date)
        logger.info("collect_all_sectors_circ_mv: Tushare daily_basic %s got %d stocks",
                    valid_date, len(mv_map))
        if not mv_map:
            use_tushare = False
            logger.warning("Tushare daily_basic 无数据，回退到 westock 反推方案")

    # 4. 方案 B 兜底：westock fund flow 反推
    flow_index: Dict[str, Dict] = {}
    if not use_tushare:
        logger.info("collect_all_sectors_circ_mv: using westock reverse plan")
        flow_records = fund_flow(sorted(all_stocks), raw=True)
        flow_index = {
            (r.get("code") or r.get("SecuCode")): r
            for r in flow_records if (r.get("code") or r.get("SecuCode"))
        }
        logger.info("collect_all_sectors_circ_mv: westock fund flow got %d records",
                    len(flow_index))

    # 5. 按 pt_code 聚合累加
    result: Dict[str, Dict] = {}
    for pt_code in all_pt_codes:
        stock_codes = stock_map.get(pt_code, [])

        if not stock_codes:
            result[pt_code] = _empty_result(today_str)
            continue

        total_mv = 0.0
        valid_count = 0
        skip_count = 0
        source = "unknown"

        if use_tushare:
            source = "tushare"
            for s in stock_codes:
                wcode = s.get("code") if isinstance(s, dict) else s
                if not wcode:
                    skip_count += 1
                    continue
                tushare_code = _westock_to_tushare(wcode)
                mv = mv_map.get(tushare_code)
                if mv is not None and mv > 0:
                    total_mv += mv
                    valid_count += 1
                else:
                    skip_count += 1
        else:
            source = "westock_reverse"
            for s in stock_codes:
                wcode = s.get("code") if isinstance(s, dict) else s
                if not wcode:
                    skip_count += 1
                    continue
                record = flow_index.get(wcode)
                if record is None:
                    skip_count += 1
                    continue
                mv = _estimate_stock_circ_mv(record)
                if mv is not None and mv > 0:
                    total_mv += mv
                    valid_count += 1
                else:
                    skip_count += 1

        fail_rate = skip_count / len(stock_codes) if stock_codes else 1.0

        if valid_count == 0:
            circ_mv = None
            circ_mv_yi = None
        else:
            circ_mv = total_mv
            circ_mv_yi = round(total_mv / YI_TO_YUAN, 2)

        result[pt_code] = {
            "circ_mv": circ_mv,
            "circ_mv_yi": circ_mv_yi,
            "stock_count": len(stock_codes),
            "valid_count": valid_count,
            "skip_count": skip_count,
            "fail_rate": round(fail_rate, 4),
            "is_estimated": (not use_tushare) or (fail_rate > 0.5),
            "source": source,
            "trade_date": today_str,
        }

    elapsed = time.time() - start_ts
    valid_sectors = sum(1 for v in result.values() if v["circ_mv"] is not None)
    logger.info("collect_all_sectors_circ_mv: done in %.1fs, %d/%d sectors valid, source=%s",
                elapsed, valid_sectors, len(result),
                "tushare" if use_tushare else "westock_reverse")

    return result


def _empty_result(today_str: str) -> Dict:
    """空板块结果（无成分股）。"""
    return {
        "circ_mv": None,
        "circ_mv_yi": None,
        "stock_count": 0,
        "valid_count": 0,
        "skip_count": 0,
        "fail_rate": 1.0,
        "is_estimated": True,
        "source": "unknown",
        "trade_date": today_str,
    }


# ============================================================
# 方案 B 兜底：westock fund flow 反推
# ============================================================
def _estimate_stock_circ_mv(record: Dict) -> Optional[float]:
    """从个股 fund flow 记录反推流通市值（元）。

    实测字段语义：
        MainInflowCircRate = |MainNetFlow| / circ_mv × 100
        即"主力净流入占流通市值比"的**绝对值百分比**（不与 mnf 同号）

    反推公式：
        circ_mv = |MainNetFlow| / MainInflowCircRate × 100

    精度限制：
        - rate 最小精度 0.01，小额 mnf 反推失真
        - 量级正确，可用作大/中/小盘分档

    Args:
        record: 个股 fund flow 单条记录

    Returns:
        流通市值（元），失败返回 None
    """
    mnf = extract_main_net_flow(record)
    rate = extract_main_inflow_circ_rate(record)

    if mnf is None or rate is None:
        return None
    if mnf == 0:
        return None

    # rate 噪声过滤
    if abs(rate) < MIN_CIRC_RATE:
        return None
    if abs(rate) > MAX_CIRC_RATE:
        return None

    # 绝对值反推（rate 是绝对值百分比，不与 mnf 同号）
    abs_mnf = abs(mnf)
    abs_rate = abs(rate)
    if abs_rate == 0:
        return None

    circ_mv = abs_mnf / abs_rate * 100

    # 稳健性过滤
    if circ_mv <= 0:
        return None
    if circ_mv > MAX_SINGLE_STOCK_MV:
        return None

    return circ_mv


def estimate_sector_circ_mv(
    pt_code: str,
    stock_codes: List[str],
) -> Tuple[Optional[float], Dict]:
    """估算单个板块的流通市值（方案 B 单板块版，兜底用）。

    Args:
        pt_code: 板块 pt 代码
        stock_codes: 成分股代码列表

    Returns:
        (流通市值元, 估算元数据)
    """
    if not stock_codes:
        return None, {
            "pt_code": pt_code,
            "stock_count": 0,
            "valid_count": 0,
            "skip_count": 0,
            "fail_rate": 1.0,
            "is_estimated": True,
            "source": "westock_reverse",
        }

    flow_records = fund_flow(stock_codes, raw=True)
    if not flow_records:
        return None, {
            "pt_code": pt_code,
            "stock_count": len(stock_codes),
            "valid_count": 0,
            "skip_count": len(stock_codes),
            "fail_rate": 1.0,
            "is_estimated": True,
            "source": "westock_reverse",
        }

    total_mv = 0.0
    valid_count = 0
    skip_count = 0

    for record in flow_records:
        mv = _estimate_stock_circ_mv(record)
        if mv is not None:
            total_mv += mv
            valid_count += 1
        else:
            skip_count += 1

    fail_rate = skip_count / len(stock_codes) if stock_codes else 1.0

    if valid_count == 0:
        return None, {
            "pt_code": pt_code,
            "stock_count": len(stock_codes),
            "valid_count": 0,
            "skip_count": skip_count,
            "fail_rate": 1.0,
            "is_estimated": True,
            "source": "westock_reverse",
        }

    return total_mv, {
        "pt_code": pt_code,
        "stock_count": len(stock_codes),
        "valid_count": valid_count,
        "skip_count": skip_count,
        "fail_rate": round(fail_rate, 4),
        "is_estimated": fail_rate > 0.5,
        "source": "westock_reverse",
    }


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 测试 1：代码格式转换
    print("=" * 60)
    print("测试 1：westock → Tushare 代码转换")
    print("=" * 60)
    cases = [
        ("sh600519", "600519.SH"),
        ("sz000001", "000001.SZ"),
        ("bj830799", "830799.BJ"),
        ("sh688593", "688593.SH"),
    ]
    for wcode, expected in cases:
        actual = _westock_to_tushare(wcode)
        ok = "✅" if actual == expected else "❌"
        print(f"  {wcode} → {actual} (期望 {expected}) {ok}")

    # 测试 2：Tushare 客户端初始化
    print()
    print("=" * 60)
    print("测试 2：Tushare 客户端")
    print("=" * 60)
    pro = _get_tushare_pro()
    if pro is not None:
        print("  Tushare 客户端初始化 ✅")
        # 测试全市场单日
        from datetime import date, timedelta
        today = date.today()
        # 用最近工作日
        check = today
        while check.weekday() >= 5:
            check = check - timedelta(days=1)
        mv = _fetch_tushare_daily_basic(check.strftime("%Y%m%d"))
        print(f"  daily_basic {check.strftime('%Y%m%d')}: {len(mv)} 只个股 ✅")
    else:
        print("  Tushare token 未配置，方案 B 兜底可用")

    # 测试 3：反推函数边界
    print()
    print("=" * 60)
    print("测试 3：反推函数边界（方案 B）")
    print("=" * 60)
    test_records = [
        ("96948411", "0.01", True, "正常正值"),
        ("-96948411", "0.01", True, "正常负值"),
        ("100", "0", False, "rate=0 跳过"),
        ("100", "0.0005", False, "rate 太小"),
        ("100", "60", False, "rate 异常大"),
        ("0", "0.5", False, "mnf=0"),
    ]
    for mnf, rate, expect_valid, desc in test_records:
        result = _estimate_stock_circ_mv({"MainNetFlow": mnf, "MainInflowCircRate": rate})
        valid = result is not None
        ok = "✅" if valid == expect_valid else "❌"
        print(f"  {desc}: {result} {ok}")

    # 测试 4：单板块估算（方案 B）
    print()
    print("=" * 60)
    print("测试 4：单板块流通市值估算（半导体）")
    print("=" * 60)
    cons = collect_constituents(["pt01801081"])
    stocks = cons.get("pt01801081", [])
    print(f"  半导体成分股: {len(stocks)} 只")
    if stocks:
        sample_codes = [s["code"] for s in stocks[:20]]
        mv, meta = estimate_sector_circ_mv("pt01801081", sample_codes)
        print(f"  估算: {mv} 元 = {mv/1e8:.2f} 亿元" if mv else "  估算失败")
        print(f"  元数据: {meta}")
