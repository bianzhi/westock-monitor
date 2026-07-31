#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局配置：强度判定窗口、规模分档阈值、采集间隔等可配项。

读取优先级：环境变量 > 本文件默认值
"""
import os
from pathlib import Path

# ============================================================
# 路径配置
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "westock.db"
SECTORS_CACHE = DATA_DIR / "sectors.json"

for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ============================================================
# westock-data CLI 配置
# ============================================================
WESTOCK_PKG = os.getenv("WESTOCK_PKG", "westock-data-skillhub@1.0.5")
WESTOCK_CMD = ["npx", "-y", WESTOCK_PKG]
WESTOCK_TIMEOUT = int(os.getenv("WESTOCK_TIMEOUT", "30"))   # 单次调用超时(秒)
WESTOCK_BATCH_SIZE = int(os.getenv("WESTOCK_BATCH_SIZE", "20"))  # 批量分批每批数量
WESTOCK_WORKERS = int(os.getenv("WESTOCK_WORKERS", "8"))    # 并发线程数

# ============================================================
# 交易时段配置 (A股)
# ============================================================
TRADING_MORNING = (9, 30, 11, 30)   # 9:30 - 11:30
TRADING_AFTERNOON = (13, 0, 15, 0)  # 13:00 - 15:00

# ============================================================
# 强度判定配置
# ============================================================
# n日窗口（强度判定基于近n日聚合净额率）
STRENGTH_WINDOW_N = int(os.getenv("STRENGTH_WINDOW_N", "5"))

# 表头展示窗口（今日 + 前4日 = 5列）
DISPLAY_DAYS = int(os.getenv("DISPLAY_DAYS", "5"))

# 跨日汇总窗口
SUMMARY_3D = int(os.getenv("SUMMARY_3D", "3"))
SUMMARY_5D = int(os.getenv("SUMMARY_5D", "5"))

# 规模分档阈值（沿用原脚本 calc_strength）
# 净额率百分比阈值：[hi(很强), mid(偏强), lo(偏弱), vlo(很弱)]
SCALE_THRESHOLDS = {
    "大盘": {"min_mv": 40000, "hi": 5.0,  "mid": 2.0,  "lo": -1.0,  "vlo": -1.5},
    "中盘": {"min_mv": 10000, "hi": 7.0,  "mid": 3.0,  "lo": -1.5,  "vlo": -2.0},
    "小盘": {"min_mv": 0,     "hi": 10.0, "mid": 4.0,  "lo": -2.0,  "vlo": -3.0},
}

# 强度档位词（5档）
STRENGTH_LEVELS = ["强", "偏强", "普通", "偏弱", "弱"]

# 按规模分档的估算日均换手率（历史成交额缺失时的兜底）
# 单位：比率（0.01 = 1%）
SCALE_TURNOVER_RATE = {
    "大盘": 0.01,   # 大盘板块日均换手约 1%
    "中盘": 0.02,   # 中盘板块日均换手约 2%
    "小盘": 0.03,   # 小盘板块日均换手约 3%
}

# ============================================================
# 成交额口径配置
# ============================================================
# "main" = 主力口径 (MainInFlow + MainOutFlow)，净额率系统性偏高
# "all"  = 全口径 (主力+散户)，更接近真实成交额（推荐）
# "auto" = 优先 main，缺失则 fallback 到 all
TURNOVER_METHOD = os.getenv("TURNOVER_METHOD", "all")

# ============================================================
# 采集间隔配置
# ============================================================
MINUTE_INTERVAL = int(os.getenv("MINUTE_INTERVAL", "60"))   # 分钟级采集间隔(秒)
IDLE_SLEEP = int(os.getenv("IDLE_SLEEP", "30"))             # 非交易时段轮询间隔(秒)

# 流通市值日级采集时点（24h 制，HH:MM，逗号分隔）
# 实测成分股反推累加耗时约 30-60s，盘前+盘后各一次足够
CIRC_MV_COLLECT_TIMES = os.getenv(
    "CIRC_MV_COLLECT_TIMES", "09:15,15:05"
)
# 流通市值日级采集兜底间隔（若时点配置未命中，每 N 秒检查一次时点）
CIRC_MV_CHECK_INTERVAL = int(os.getenv("CIRC_MV_CHECK_INTERVAL", "300"))

# ============================================================
# 数据保留策略
# ============================================================
# 分钟级数据缓存天数
MINUTE_CACHE_DAYS = int(os.getenv("MINUTE_CACHE_DAYS", "5"))

# ============================================================
# 服务配置
# ============================================================
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8200"))
CORS_ORIGINS = ["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:5173"]

# ============================================================
# 辅助函数
# ============================================================
def get_scale(circ_mv_yi: float) -> str:
    """根据流通市值(亿元)返回规模分档名称。

    Args:
        circ_mv_yi: 流通市值，单位亿元

    Returns:
        "大盘" / "中盘" / "小盘"
    """
    if circ_mv_yi >= SCALE_THRESHOLDS["大盘"]["min_mv"]:
        return "大盘"
    elif circ_mv_yi >= SCALE_THRESHOLDS["中盘"]["min_mv"]:
        return "中盘"
    else:
        return "小盘"


def get_thresholds(scale: str) -> dict:
    """根据规模分档返回阈值字典。"""
    return SCALE_THRESHOLDS[scale]
