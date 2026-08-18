#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.collect_daily_records 单元测试。

覆盖：
  - 分段差分：T-1~T-4 / T-5~T-9 / T-10~T-19 的阶梯估算
  - 交易日历跳过非交易日（周末/节假日不进 history_days）
  - 非交易日查询时锚定上一个交易日为 T-0，不切片错位
  - 边界：fund_flow 返回空 → 仅 1 条今日记录
  - 边界：n=1 → 不估算历史，只返回今日
  - 超出 20D 范围（n>20）回退兜底

测试用 monkeypatch 替代 fund_flow，避免依赖网络与 westock CLI。
"""
import pytest
from datetime import date, timedelta

import collector


# ============================================================
# 辅助构造
# ============================================================
def _mock_fund_flow_today_only(today_net_yi: float = 1.0,
                               net_5d_yi: float = 5.0,
                               net_10d_yi: float = 10.0,
                               net_20d_yi: float = 20.0):
    """返回一个只对今日 fund_flow 调用生效的 mock。

    构造一条 fund flow 记录，单位：亿元 → 元。
    """
    def _fake(codes, raw=True):
        return [{
            "code": list(codes)[0] if codes else "pt01801081",
            "name": "测试板块",
            "MainNetFlow": today_net_yi * 1e8,
            "MainInFlow": (today_net_yi + 10) * 1e8,
            "MainOutFlow": 10 * 1e8,
            "RetailInFlow": 5 * 1e8,
            "RetailOutFlow": 5 * 1e8,
            "MainNetFlow5D": net_5d_yi * 1e8,
            "MainNetFlow10D": net_10d_yi * 1e8,
            "MainNetFlow20D": net_20d_yi * 1e8,
            "ClosePrice": "100.0",
            "EndDate": date.today().isoformat(),
        }]
    return _fake


class _EmptyStorage:
    """mock storage：get_sector_daily_batch 返回空，隔离真实落库数据。"""
    def get_sector_daily_batch(self, codes, days=30):
        return {c: [] for c in codes}


# ============================================================
# 分段差分估算
# ============================================================
class TestSegmentedDifferencing:
    @pytest.fixture(autouse=True)
    def _isolate_storage(self, monkeypatch):
        """隔离真实 sector_daily 落库数据，避免历史日读到真实值而非估算值。"""
        monkeypatch.setattr(collector, "get_storage", lambda: _EmptyStorage())

    def test_today_first_no_estimated_flag(self, monkeypatch):
        """今日记录（records[0]）不应有 estimated 标记"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only())
        records = collector.collect_daily_records("pt01801081", n=5)
        assert len(records) == 5
        assert records[0].get("estimated") is None or records[0].get("estimated") is False

    def test_history_marked_estimated(self, monkeypatch):
        """所有历史日都应标记 estimated=True"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only())
        records = collector.collect_daily_records("pt01801081", n=5)
        for r in records[1:]:
            assert r.get("estimated") is True

    def test_seg_1_4_correct(self, monkeypatch):
        """T-1~T-4 应填 seg_1_4 = (5D - today) / 4"""
        # today=1, 5D=5 → seg_1_4 = (5-1)/4 = 1 亿 = 1e8 元
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only(
            today_net_yi=1, net_5d_yi=5, net_10d_yi=10, net_20d_yi=20
        ))
        records = collector.collect_daily_records("pt01801081", n=5)
        # T-1~T-4 全部应填 1e8
        for r in records[1:5]:
            assert r["net_flow"] == pytest.approx(1e8, rel=1e-6)

    def test_seg_5_9_correct(self, monkeypatch):
        """T-5~T-9 应填 seg_5_9 = (10D - 5D) / 5"""
        # 5D=5, 10D=10 → seg_5_9 = (10-5)/5 = 1 亿
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only(
            today_net_yi=1, net_5d_yi=5, net_10d_yi=10, net_20d_yi=20
        ))
        records = collector.collect_daily_records("pt01801081", n=10)
        # records[0]=今日, records[1..4]=T-1~T-4(seg_1_4), records[5..9]=T-5~T-9(seg_5_9)
        for r in records[5:10]:
            assert r["net_flow"] == pytest.approx(1e8, rel=1e-6)

    def test_seg_10_19_correct(self, monkeypatch):
        """T-10~T-19 应填 seg_10_19 = (20D - 10D) / 10"""
        # 10D=10, 20D=20 → seg_10_19 = (20-10)/10 = 1 亿
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only(
            today_net_yi=1, net_5d_yi=5, net_10d_yi=10, net_20d_yi=20
        ))
        records = collector.collect_daily_records("pt01801081", n=20)
        # records[10..19] = T-10~T-19
        for r in records[10:20]:
            assert r["net_flow"] == pytest.approx(1e8, rel=1e-6)

    def test_seg_stepping_diff_values(self, monkeypatch):
        """三段应呈现阶梯差异（避免全平）"""
        # today=0, 5D=4, 10D=14, 20D=34
        # seg_1_4 = (4-0)/4 = 1
        # seg_5_9 = (14-4)/5 = 2
        # seg_10_19 = (34-14)/10 = 2
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only(
            today_net_yi=0, net_5d_yi=4, net_10d_yi=14, net_20d_yi=34
        ))
        records = collector.collect_daily_records("pt01801081", n=20)
        assert records[1]["net_flow"] == pytest.approx(1e8, rel=1e-6)   # seg_1_4
        assert records[5]["net_flow"] == pytest.approx(2e8, rel=1e-6)   # seg_5_9
        assert records[10]["net_flow"] == pytest.approx(2e8, rel=1e-6)  # seg_10_19


# ============================================================
# 交易日历跳过非交易日
# ============================================================
class TestTradingCalendarSkip:
    def test_history_dates_are_real_trading_days(self, monkeypatch):
        """history_days 不应含周末（weekday 5/6）"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only())
        records = collector.collect_daily_records("pt01801081", n=10)
        for r in records[1:]:
            d = date.fromisoformat(r["date"])
            assert d.weekday() < 5, f"{r['date']} 是周末"

    def test_no_duplicate_today_in_history(self, monkeypatch):
        """今日不应重复出现在 history_days（回归 P0-2 切片错位）"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only())
        records = collector.collect_daily_records("pt01801081", n=5)
        today_str = date.today().isoformat()
        # records[0] 是今日，records[1:] 不应再有今日
        assert records[0]["date"] == today_str
        for r in records[1:]:
            assert r["date"] != today_str, "今日重复出现在历史记录中"

    def test_history_count_equals_n_minus_1(self, monkeypatch):
        """n=5 → 今日 + 4 个历史日 = 5 条"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only())
        for n in [3, 5, 10, 20]:
            records = collector.collect_daily_records("pt01801081", n=n)
            assert len(records) == n, f"n={n} 时应返回 {n} 条，实际 {len(records)}"


# ============================================================
# 边界情况
# ============================================================
class TestEdgeCases:
    def test_fund_flow_empty_returns_today_only(self, monkeypatch):
        """fund_flow 返回空 → 仅今日 1 条（net_flow=None）"""
        monkeypatch.setattr(collector, "fund_flow", lambda codes, raw=True: [])
        monkeypatch.setattr(collector, "get_storage", lambda: _EmptyStorage())
        records = collector.collect_daily_records("pt01801081", n=5)
        assert len(records) == 1
        assert records[0]["net_flow"] is None

    def test_n_1_returns_today_only(self, monkeypatch):
        """n=1 → 不估算历史，只返回今日"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only())
        records = collector.collect_daily_records("pt01801081", n=1)
        assert len(records) == 1
        assert records[0]["net_flow"] == pytest.approx(1e8, rel=1e-6)

    def test_n_beyond_20d_uses_fallback(self, monkeypatch):
        """n=25（超 20D 范围）→ T-20~T-24 用 seg_10_19 兜底"""
        monkeypatch.setattr(collector, "fund_flow", _mock_fund_flow_today_only(
            today_net_yi=1, net_5d_yi=5, net_10d_yi=10, net_20d_yi=20
        ))
        records = collector.collect_daily_records("pt01801081", n=25)
        # T-20~T-24 应兜底为 seg_10_19 = (20-10)/10 = 1 亿
        for r in records[20:25]:
            assert r["net_flow"] == pytest.approx(1e8, rel=1e-6)

    def test_missing_5d_no_history(self, monkeypatch):
        """5D 字段缺失 → 历史日 None 占位（不估算假值），今日有值"""
        def _fake(codes, raw=True):
            return [{"code": "x", "MainNetFlow": 1e8, "MainNetFlow5D": None,
                     "MainNetFlow10D": None, "MainNetFlow20D": None,
                     "MainInFlow": 2e8, "MainOutFlow": 1e8,
                     "RetailInFlow": 0, "RetailOutFlow": 0,
                     "ClosePrice": "100", "EndDate": "2026-07-31"}]
        monkeypatch.setattr(collector, "fund_flow", _fake)
        monkeypatch.setattr(collector, "get_storage", lambda: _EmptyStorage())
        records = collector.collect_daily_records("pt01801081", n=5)
        assert len(records) == 5  # 今日 + 4 个历史日（None 占位）
        assert records[0]["net_flow"] == pytest.approx(1e8, rel=1e-6)
        for r in records[1:]:
            assert r["net_flow"] is None  # 历史日 None 占位，不估算假值
