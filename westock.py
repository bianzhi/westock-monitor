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
            cmd, capture_output=True, text=True, timeout=t,
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
def fund_flow(codes: Union[str, List[str]], raw: bool = True) -> List[Dict]:
    """批量查板块资金流（主力净流入 MainNetFlow 等）。

    自动按 WESTOCK_BATCH_SIZE 分批，WESTOCK_WORKERS 并发执行。

    Args:
        codes: 单个代码字符串，或代码列表
        raw: 是否输出 JSON

    Returns:
        合并后的板块资金流列表，每条含 MainNetFlow / MainInFlow / MainOutFlow 等
    """
    if isinstance(codes, str):
        codes = [codes]
    if not codes:
        return []

    # 分批
    batches = [
        codes[i:i + WESTOCK_BATCH_SIZE]
        for i in range(0, len(codes), WESTOCK_BATCH_SIZE)
    ]
    results: List[Dict] = []

    def _fetch_batch(batch: List[str]) -> List[Dict]:
        codes_str = ",".join(batch)
        data = westock("fund", "flow", codes_str, raw=raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    with ThreadPoolExecutor(max_workers=WESTOCK_WORKERS) as pool:
        futures = [pool.submit(_fetch_batch, b) for b in batches]
        for f in as_completed(futures):
            try:
                results.extend(f.result())
            except Exception as e:
                logger.warning("fund_flow batch error: %s", e)

    logger.info("fund_flow: requested %d codes, got %d records", len(codes), len(results))
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
