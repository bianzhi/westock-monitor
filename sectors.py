#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""申万二级板块代码列表（134 个，对齐申万 2021 官方标准）。

数据来源：sw2021_sectors.py（基于 Tushare 官方申万 2021 行业分类）
申万 2021 标准：31 个一级行业 / 134 个二级行业 / 346 个三级行业

代码格式：
  - westock pt 代码：pt + 018 + 申万二级代码[1:]，与 `fund flow` 入参一致
  - 申万二级代码：801 开头 6 位（沿用 2014 版沿革重新分配，非连续）

更新方式：
  1. 启动时用本文件 DEFAULT_SECTORS 初始化（134 个，权威标准清单）
  2. 调用 /api/refresh-sectors 可重新 search 覆盖
  3. 本文件作为兜底，避免接口异常时无法启动

历史 bug 修正（v2）：
  原 124 个清单有 8 个 pt 代码错位，已按申万官方代码全部修正：
    - 农化制品/非金属材料Ⅱ 代码互换
    - 食品饮料 6 个板块循环错位（食品加工/白酒Ⅱ/非白酒/饮料乳品/休闲食品/调味发酵品Ⅱ）
"""
from typing import Dict, List

from sw2021_sectors import (
    SW_L1, SW_L2, SW_L2_PT_MAP,
    sw_code_to_pt, pt_to_sw_code,
    get_l1_name, get_l1_code,
)


# ============================================================
# DEFAULT_SECTORS：从申万 2021 标准清单派生
# 格式：{"code": westock_pt, "name": 二级名称, "l1": 一级名称,
#        "sw_code": 申万二级6位代码, "sw_l1_code": 申万一级6位代码}
# ============================================================
DEFAULT_SECTORS: List[Dict[str, str]] = []
for _l2 in SW_L2:
    _sw_code = _l2.code
    _name = _l2.name
    _l1_code = _l2.l1_code
    _pt = SW_L2_PT_MAP[_sw_code]
    _l1_name = get_l1_name(_l1_code)
    DEFAULT_SECTORS.append({
        "code": _pt,
        "name": _name,
        "l1": _l1_name,
        "sw_code": _sw_code,
        "sw_l1_code": _l1_code,
    })


# ============================================================
# 向后兼容的查询接口
# ============================================================
def get_default_codes() -> List[str]:
    """返回默认板块 westock pt 代码列表（134 个）。"""
    return [s["code"] for s in DEFAULT_SECTORS]


def get_default_sector_map() -> Dict[str, Dict[str, str]]:
    """返回 pt 代码 -> sector_info 的映射。"""
    return {s["code"]: s for s in DEFAULT_SECTORS}


def count_sectors() -> int:
    """返回默认板块数量（134）。"""
    return len(DEFAULT_SECTORS)


def get_l1_names() -> List[str]:
    """返回所有一级名称列表（去重、排序）。"""
    return sorted({s["l1"] for s in DEFAULT_SECTORS})


# ============================================================
# 申万标准代码查询接口（新增）
# ============================================================
def get_sector_by_sw_code(sw_code: str) -> Dict[str, str]:
    """通过申万二级 6 位代码查板块信息。"""
    pt = sw_code_to_pt(sw_code)
    for s in DEFAULT_SECTORS:
        if s["code"] == pt:
            return s
    return {}


def get_sector_by_pt(pt_code: str) -> Dict[str, str]:
    """通过 westock pt 代码查板块信息。"""
    m = get_default_sector_map()
    return m.get(pt_code, {})


def get_sw_code_by_pt(pt_code: str) -> str:
    """westock pt 代码 → 申万二级 6 位代码。"""
    return pt_to_sw_code(pt_code)


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print("sectors.py 自检")
    print("=" * 70)
    print(f"申万二级板块总数: {count_sectors()}")
    print(f"一级名称数: {len(get_l1_names())}")
    print(f"westock pt 代码唯一: {len(set(get_default_codes())) == count_sectors()}")
    print(f"申万二级代码唯一: {len({s['sw_code'] for s in DEFAULT_SECTORS}) == count_sectors()}")
    print()
    from collections import Counter
    c = Counter(s["l1"] for s in DEFAULT_SECTORS)
    print("按一级分组:")
    for l1, cnt in sorted(c.items(), key=lambda x: -x[1]):
        print(f"  {l1}: {cnt}")
