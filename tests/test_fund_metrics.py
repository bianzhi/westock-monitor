#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""westock_fund_metrics.calc_turnover 单元测试。

覆盖：
  - all 口径：成交额 = 买方总和（主力买 + 散户买），不重复计卖方
  - main 口径：只取主力买入
  - auto 口径：买方总和缺失时回退卖方总和
  - 字段缺失/字符串空值 → None
  - 未知 method → None
"""
import pytest

from westock_fund_metrics import calc_turnover


def _rec(inf="100", outf="200", ret_inf="300", ret_out="400"):
    """构造 fund flow 记录，字段为字符串（模拟 CLI 返回）。"""
    return {
        "MainInFlow": inf,
        "MainOutFlow": outf,
        "RetailInFlow": ret_inf,
        "RetailOutFlow": ret_out,
    }


# ============================================================
# all 口径：成交额 = 买方总和，不得重复计卖方
# ============================================================
def test_all_buyer_sum_only():
    """all 口径 = 主力买 + 散户买 = 400，不把卖方重复计入（回归 v5 的双边重复 bug）。"""
    assert calc_turnover(_rec(), method="all") == 100 + 300


def test_all_falls_back_to_seller_side():
    """all 口径买方缺失时用卖方总和。"""
    rec = _rec(inf=None, ret_inf=None)
    assert calc_turnover(rec, method="all") == 200 + 400


def test_all_missing_returns_none():
    """all 口径所有字段缺失 → None。"""
    assert calc_turnover(_rec(inf=None, outf=None, ret_inf=None, ret_out=None), method="all") is None


# ============================================================
# main 口径：只取主力买入
# ============================================================
def test_main_buyer_only():
    assert calc_turnover(_rec(), method="main") == 100


def test_main_missing_returns_none():
    assert calc_turnover(_rec(inf=None), method="main") is None


# ============================================================
# auto 口径：优先买方总和，缺失回退卖方
# ============================================================
def test_auto_prefers_buyer_sum():
    assert calc_turnover(_rec(), method="auto") == 100 + 300


def test_auto_falls_back_to_seller():
    rec = _rec(inf=None, ret_inf=None)
    assert calc_turnover(rec, method="auto") == 200 + 400


# ============================================================
# 边界
# ============================================================
def test_empty_string_treated_as_none():
    """空字符串字段按缺失处理（模拟 CLI 返回 ''）。"""
    assert calc_turnover(_rec(inf="", ret_inf=""), method="all") == 200 + 400


def test_unknown_method_returns_none():
    assert calc_turnover(_rec(), method="bogus") is None


def test_numeric_fields_work():
    """字段为 int/float 时同样可用（非 CLI 场景）。"""
    rec = {"MainInFlow": 100, "MainOutFlow": 200,
           "RetailInFlow": 300, "RetailOutFlow": 400}
    assert calc_turnover(rec, method="all") == 400
