#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app._build_summary 单元测试。

覆盖：
  - 基本汇总：净流入之和 + 聚合净额率
  - n 窗口截断：仅取前 n 条
  - 成交额缺失兜底：流通市值×换手率
  - 全部记录无效 → valid_days=0 / net_flow_yi=None
  - 空列表 → None
"""
import pytest

from app import _build_summary


def _rec(net_yi, turnover_yi=None):
    """构造日记录，net_yi/turnover_yi 单位亿元。"""
    return {
        "net_flow": net_yi * 1e8,
        "turnover": (turnover_yi * 1e8 if turnover_yi is not None else None),
    }


# ============================================================
# 基本
# ============================================================
def test_basic_summary():
    """3 条有效记录，net_flow 求和、net_rate = Σnet/Σturnover×100（round 4 位）。"""
    records = [
        _rec(1.0, 100.0),   # 净额率 1%
        _rec(2.0, 100.0),   # 2%
        _rec(-1.0, 100.0),  # -1%
    ]
    s = _build_summary(records, days=3, circ_mv_yi=None)
    assert s["valid_days"] == 3
    assert s["net_flow_yi"] == pytest.approx(2.0)
    assert s["net_rate"] == pytest.approx(round(2.0 / 300.0 * 100.0, 4))


def test_window_truncation():
    """days=2 时只取前 2 条。"""
    records = [_rec(1.0, 100.0), _rec(2.0, 100.0), _rec(10.0, 100.0)]
    s = _build_summary(records, days=2, circ_mv_yi=None)
    assert s["valid_days"] == 2
    assert s["net_flow_yi"] == pytest.approx(3.0)  # 1+2，不含第三条 10


# ============================================================
# 成交额兜底
# ============================================================
def test_turnover_fallback_circ_mv():
    """历史日 turnover 缺失时用流通市值×换手率兜底（小盘 3%）。"""
    records = [_rec(1.0, 100.0), _rec(2.0)]  # 第二条无成交额
    # circ_mv_yi=1000 亿 → 元 = 1e11 × 3% = 3e9
    s = _build_summary(records, days=2, circ_mv_yi=1000.0)
    assert s["valid_days"] == 2
    # net_rate = (1+2)亿 / (100亿 + 30亿) × 100（round 4 位）
    assert s["net_rate"] == pytest.approx(round(3.0 / 130.0 * 100.0, 4))


def test_fallback_to_first_day_turnover():
    """无流通市值且历史日 turnover 缺失：回退今日成交额。"""
    records = [_rec(1.0, 100.0), _rec(2.0)]
    s = _build_summary(records, days=2, circ_mv_yi=None)
    assert s["valid_days"] == 2
    # 第二条用第一条的 turnover=100 亿
    assert s["net_rate"] == pytest.approx(3.0 / 200.0 * 100.0)


# ============================================================
# 边界
# ============================================================
def test_empty_records_returns_none():
    assert _build_summary([], days=3, circ_mv_yi=None) is None


def test_days_zero_returns_none():
    records = [_rec(1.0, 100.0)]
    assert _build_summary(records, days=0, circ_mv_yi=None) is None


def test_all_invalid_returns_zero_valid():
    """net_flow 全 None → valid_days=0，净流入/净额率 None。"""
    records = [{"net_flow": None, "turnover": None}]
    s = _build_summary(records, days=3, circ_mv_yi=None)
    assert s["valid_days"] == 0
    assert s["net_flow_yi"] is None
    assert s["net_rate"] is None
