#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯自选股概念板块清单模块。

concept sector codes are exclusively `pt02xxxxxx` (non-GN prefix).
pt02GN codes are NOT supported by fund_flow — only pt0200 / pt021 / pt022 are.
本模块维护一份已验证可用的概念板块代码清单，支持动态补充和缓存。
"""

import json
import logging
from typing import Dict, List
from config import DATA_DIR

logger = logging.getLogger(__name__)

CONCEPT_CACHE = DATA_DIR / "concept_sectors.json"

# 已验证可用的概念板块（fund_flow 正常返回数据）
VERIFIED_CONCEPTS: Dict[str, str] = {
    "pt02003800": "人工智能",
    "pt02003891": "芯片概念",
    "pt02003511": "ST概念",
    "pt02003505": "中字头",
    "pt02003578": "稀土永磁",
    "pt02003685": "举牌概念",
    "pt02003514": "参股券商",
    "pt02001231": "腾讯云",
    "pt02011304": "人造肉",
    "pt02020003": "黄酒概念",
    "pt02021044": "腾讯概念",
    "pt02021419": "预制菜",
    "pt02031390": "国家大基金持股",
    "pt02041354": "数据中心",
    "pt02050005": "镍概念",
    "pt02050006": "铜概念",
    "pt02050007": "白银概念",
    "pt02050008": "黄金概念",
    "pt02050009": "钒概念",
    "pt02050010": "钛白粉概念",
    "pt02050011": "钴概念",
    "pt02050012": "钨概念",
    "pt02081407": "固态电池",
    "pt02101316": "光刻机",
    "pt02131031": "维生素",
    "pt02131362": "创新药",
    "pt02250003": "生物医药",
    "pt02251323": "医疗美容概念",
    "pt02251415": "辅助生殖",
}


def get_concept_sectors() -> Dict[str, str]:
    """获取概念板块代码→名称映射，优先从缓存读取。"""
    if CONCEPT_CACHE.exists():
        try:
            with open(CONCEPT_CACHE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if isinstance(cached, dict) and cached:
                    return cached
        except (json.JSONDecodeError, OSError):
            pass
    return dict(VERIFIED_CONCEPTS)


def save_concept_sectors(sectors: Dict[str, str]) -> None:
    """保存概念板块清单到缓存文件。"""
    try:
        with open(CONCEPT_CACHE, "w", encoding="utf-8") as f:
            json.dump(sectors, f, ensure_ascii=False, indent=2)
        logger.info("concept_sectors: saved %d sectors to cache", len(sectors))
    except OSError as e:
        logger.warning("concept_sectors: save failed: %s", e)


def add_concept(code: str, name: str) -> bool:
    """手动添加一个概念板块到缓存。

    Args:
        code: 板块代码（pt02xxxxxx）
        name: 板块名称

    Returns:
        True 表示新增，False 表示已存在
    """
    sectors = get_concept_sectors()
    if code in sectors:
        return False
    sectors[code] = name
    save_concept_sectors(sectors)
    return True


def get_default_codes() -> List[str]:
    """获取概念板块代码列表。"""
    return list(get_concept_sectors().keys())


def get_default_name(code: str) -> str:
    """根据代码获取概念板块名称。"""
    return get_concept_sectors().get(code, code)
