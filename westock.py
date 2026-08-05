#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""westock-data CLI 封装：subprocess 调用 + 批量分批 + 并发。

核心 API:
  - fund_flow(codes, raw=True) -> List[Dict]
      批量查板块资金流，自动按 WESTOCK_BATCH_SIZE 分批，WESTOCK_WORKERS 并发
  - sector_ranking(raw=True) -> Dict
      全市场板块行情榜（一条命令拿全市场）
  - search_sector(keyword, raw=True) -> List[Dict]
      按名称搜板块（用于刷新板块列表）
  - westock(*args, raw=False) -> Any
      通用底层调用，返回解析后的 JSON 或原始文本

实测要点（1.0.5 版本）:
  - asfund 已废弃 → 用 fund flow
  - sector list sw2 不存在 → 用 search 反查
  - minute / quote / board 已废弃
  - --raw 输出 JSON，不加默认 markdown 表格
"""
import json
import subprocess
import logging
from typing import Any, List, Dict, Optional, Union
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    WESTOCK_CMD, WESTOCK_TIMEOUT, WESTOCK_BATCH_SIZE, WESTOCK_WORKERS,
)

logger = logging.getLogger(__name__)


def westock(*args: str, raw: bool = False, timeout: Optional[int] = None) -> Any:
    """通用底层调用 westock-data CLI。

    Args:
        *args: 命令参数，如 "fund", "flow", "pt01801080"
        raw: 是否加 --raw 输出 JSON
        timeout: 超时秒数，默认 WESTOCK_TIMEOUT

    Returns:
        解析后的 JSON 对象，或原始 stdout 文本
    """
    cmd = WESTOCK_CMD + list(args)
    if raw and "--raw" not in cmd:
        cmd.append("--raw")
    t = timeout or WESTOCK_TIMEOUT
    logger.debug("westock cmd: %s (timeout=%ss)", " ".join(cmd), t)
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=t,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            logger.warning("westock failed: %s | stderr=%s", " ".join(args), err[:200])
            return None
        out = result.stdout.strip()
        if not out:
            return None
        # 尝试解析 JSON
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out
    except subprocess.TimeoutExpired:
        logger.warning("westock timeout: %s", " ".join(args))
        return None
    except Exception as e:
        logger.warning("westock error: %s | %s", " ".join(args), e)
        return None


# ============================================================
# 高层 API
# ============================================================
def fund_flow(codes: Union[str, List[str]], raw: bool = True, asof_date: Optional[str] = None) -> List[Dict]:
    """批量查板块资金流（主力净流入 MainNetFlow 等）。

    自动按 WESTOCK_BATCH_SIZE 分批，WESTOCK_WORKERS 并发执行。
    对首轮缺失的板块自动逐批重试，确保全覆盖。

    Args:
        codes: 单个代码字符串，或代码列表
        raw: 是否输出 JSON
        asof_date: YYYY-MM-DD，查询指定交易日数据（None 表示今日）

    Returns:
        合并后的板块资金流列表，每条含 MainNetFlow / MainInFlow / MainOutFlow 等
    """
    if isinstance(codes, str):
        codes = [codes]
    if not codes:
        return []

    all_codes = set(codes)

    def _fetch_batch(batch: List[str]) -> List[Dict]:
        codes_str = ",".join(batch)
        args = ["fund", "flow", codes_str]
        if asof_date:
            args.extend(["--date", asof_date])
        data = westock(*args, raw=raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    def _extract_code(r: Dict) -> Optional[str]:
        return r.get("code") or r.get("SecuCode")

    # ---- 第一轮：并发分批 ----
    batches = [
        codes[i:i + WESTOCK_BATCH_SIZE]
        for i in range(0, len(codes), WESTOCK_BATCH_SIZE)
    ]
    results: List[Dict] = []
    got_codes: Set[str] = set()

    with ThreadPoolExecutor(max_workers=WESTOCK_WORKERS) as pool:
        futures = [pool.submit(_fetch_batch, b) for b in batches]
        for f in as_completed(futures):
            try:
                batch_results = f.result()
                for r in batch_results:
                    c = _extract_code(r)
                    if c:
                        got_codes.add(c)
                results.extend(batch_results)
            except Exception as e:
                logger.warning("fund_flow batch error: %s", e)

    # ---- 第二轮：补齐缺失板块（逐批重试，最多 2 轮） ----
    missing = all_codes - got_codes
    retry_rounds = 0
    max_retry_rounds = 2

    while missing and retry_rounds < max_retry_rounds:
        retry_rounds += 1
        retry_list = sorted(missing)
        logger.info("fund_flow: round %d, %d codes missing, retrying...",
                    retry_rounds, len(retry_list))

        # 用更小的批次重试（每批 5 个，减少 CLI 侧丢数据的可能）
        retry_batches = [
            retry_list[i:i + 5]
            for i in range(0, len(retry_list), 5)
        ]

        with ThreadPoolExecutor(max_workers=WESTOCK_WORKERS) as pool:
            futures = [pool.submit(_fetch_batch, b) for b in retry_batches]
            for f in as_completed(futures):
                try:
                    batch_results = f.result()
                    for r in batch_results:
                        c = _extract_code(r)
                        if c and c in missing:
                            missing.discard(c)
                            got_codes.add(c)
                    results.extend(batch_results)
                except Exception as e:
                    logger.warning("fund_flow retry error: %s", e)

    if missing:
        logger.warning("fund_flow: %d codes still missing after %d retry rounds: %s",
                       len(missing), max_retry_rounds, sorted(missing)[:10])

        # ---- 第三轮：逐个兜底查询 ----
        logger.info("fund_flow: final round, trying %d codes individually", len(missing))
        for code in sorted(missing):
            try:
                r = _fetch_batch([code])
                if r:
                    for item in r:
                        c = _extract_code(item)
                        if c:
                            missing.discard(c)
                            got_codes.add(c)
                    results.extend(r)
            except Exception as e:
                logger.debug("fund_flow individual retry %s: %s", code, e)

    if missing:
        logger.warning("fund_flow: %d codes still missing after all retries: %s",
                       len(missing), sorted(missing)[:10])

    logger.info("fund_flow: requested %d codes, got %d records (%d missing)",
                len(all_codes), len(results), len(missing))
    return results


def sector_ranking(raw: bool = True) -> Dict:
    """全市场板块行情榜。

    返回 {"sections": [[...行业榜...], [...概念榜...], [...资金流入榜...]]}
    """
    data = westock("sector", "ranking", raw=raw)
    if isinstance(data, dict):
        return data
    return {"sections": []}


def search_sector(keyword: str, raw: bool = True) -> List[Dict]:
    """按关键词搜板块（用于刷新板块列表）。

    返回含 code/name/分类 的列表
    """
    data = westock("search", keyword, "--type", "sector", raw=raw)
    if isinstance(data, list):
        return data
    return []


def sector_info(code: str, raw: bool = True) -> Optional[Dict]:
    """查单个板块信息（含流通市值等元数据，实测部分板块返回 null）。"""
    data = westock("sector", "info", code, raw=raw)
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def sector_valuation(code: str, raw: bool = True) -> Optional[Dict]:
    """查板块估值（PE/PB/PCF/PS），不含流通市值。"""
    data = westock("sector", "valuation", code, raw=raw)
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def sector_constituent(codes: Union[str, List[str]], raw: bool = True) -> Dict[str, List[Dict]]:
    """批量查板块成分股。

    Args:
        codes: 单个 pt 代码或代码列表
        raw: 是否输出 JSON

    Returns:
        {pt_code: [{code, name, SectorCode}, ...], ...}
        顺序与请求顺序一致

    实测（重要 bug 规避）：
        - westock 多板块查询时，返回的 sections 顺序与请求顺序**不一致**
          （实测请求 [81,16,12] 返回 sections=[12的数据,16的数据,81的数据]）
        - 但每只成分股的 SectorCode 字段标识了它所属板块
        - 本函数按 SectorCode 反向聚合，确保结果与请求顺序对齐
    """
    if isinstance(codes, str):
        codes = [codes]
    if not codes:
        return {}

    codes_str = ",".join(codes)
    data = westock("sector", "constituent", codes_str, raw=raw)

    # 初始化结果（保持请求顺序）
    result: Dict[str, List[Dict]] = {c: [] for c in codes}

    if isinstance(data, dict) and "sections" in data:
        # 按 SectorCode 字段反向聚合，规避 sections 顺序不一致 bug
        for sec in data["sections"]:
            if not isinstance(sec, list):
                continue
            for item in sec:
                if not isinstance(item, dict):
                    continue
                pt = item.get("SectorCode")
                if pt in result:
                    result[pt].append(item)
    elif isinstance(data, list):
        # 单板块返回 list 时
        if len(codes) == 1:
            result[codes[0]] = data
        else:
            # 多板块但返回 list（异常情况），按 SectorCode 聚合
            for item in data:
                if isinstance(item, dict):
                    pt = item.get("SectorCode")
                    if pt in result:
                        result[pt].append(item)

    return result


def extract_main_inflow_circ_rate(record: Dict) -> Optional[float]:
    """从 fund flow 单条记录提取 MainInflowCircRate（主力净流入占流通市值比，%）。

    实测：
        - 个股层面非零可用，可反推个股流通市值
        - 板块层面全为 0，不可用
    """
    v = record.get("MainInflowCircRate")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================
# 便捷函数：从 fund_flow 结果提取关键字段
# ============================================================
def extract_main_net_flow(record: Dict) -> Optional[float]:
    """从 fund flow 单条记录提取 MainNetFlow，转 float。"""
    v = record.get("MainNetFlow")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_main_inflow(record: Dict) -> Optional[float]:
    """主力流入额。"""
    v = record.get("MainInFlow")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_main_outflow(record: Dict) -> Optional[float]:
    """主力流出额。"""
    v = record.get("MainOutFlow")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    # 自测：单板块 fund flow
    logging.basicConfig(level=logging.INFO)
    r = fund_flow(["pt01801080", "pt01801081"])
    print(json.dumps(r, ensure_ascii=False, indent=2))
