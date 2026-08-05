#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 后端：板块列表/单板块/分钟/强度排行/刷新板块。

启动:
  uvicorn app:app --host 0.0.0.0 --port 8200 --reload
  或
  python app.py

接口列表:
  GET  /api/health                          健康检查
  GET  /api/config                          当前配置
  GET  /api/sectors                         板块列表 + 当前强度（宽表主数据）
  GET  /api/sectors/{code}                  单板块详情 + 近n日数据
  GET  /api/sectors/{code}/minute           单板块当日分钟级数据
  GET  /api/strength/ranking                强度排行 Top N
  GET  /api/minute/realtime                 全板块当日分钟级实时数据
  POST /api/refresh-sectors                 手动刷新板块列表
  POST /api/collect/minute                  手动触发一次分钟采集
"""
import logging
import os
import sys
import threading
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

# 确保能 import 本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import (
    API_HOST, API_PORT, CORS_ORIGINS,
    STRENGTH_WINDOW_N, DISPLAY_DAYS, SUMMARY_3D, SUMMARY_5D,
    SCALE_THRESHOLDS, get_scale, SCALE_TURNOVER_RATE, LOG_DIR, BASE_DIR,
)
from sectors import DEFAULT_SECTORS, get_default_sector_map
from storage import get_storage
from collector import (
    load_sectors, get_sector_codes, refresh_sectors,
    collect_minute_snapshot, collect_all_sectors_daily,
    collect_daily_records, is_trading_time,
)
from circ_mv_collector import collect_all_sectors_circ_mv
from strength import (
    calc_strength, calc_aggregate_net_rate, calc_aggregate_net_flow,
    calc_sector_strength, level_to_color,
)
from westock_fund_metrics import calc_sector_metrics_batch, calc_turnover
from data_cache import (
    init_cache, is_ready, refresh_cache, get_max_n,
    get_codes as cache_get_codes, get_sectors as cache_get_sectors,
    get_daily_map, get_circ_mv_map, get_circ_mv, start_background_refresh,
    trigger_background_refresh, is_refresh_in_progress, get_refresh_last_error,
    get_updated_time,
)

# ============================================================
# 请求缓存：避免同秒内重复调 CLI 雪崩
# ============================================================
_sectors_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SEC = 15  # 15s 内重复请求直接返回缓存

# ============================================================
# 日志配置
# ============================================================
from logging.handlers import RotatingFileHandler

LOG_FILE = LOG_DIR / "app.log"
# 单文件最大 10MB，保留 3 个备份卷（共 ~40MB）
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger("app")

# ============================================================
# FastAPI 应用
# ============================================================
app = FastAPI(
    title="Westock Monitor",
    description="申万二级板块资金流向实时监控",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,  # 仅放行 config.CORS_ORIGINS（生产用环境变量配置）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 响应模型
# ============================================================
class HealthResponse(BaseModel):
    status: str
    timestamp: str
    trading: bool
    storage: Dict[str, Any]
    cache_ready: bool = False
    cache_refreshing: bool = False
    cache_last_error: Optional[str] = None
    cache_updated: Optional[str] = None


class SectorRow(BaseModel):
    code: str
    name: str
    l1: Optional[str] = None
    circ_mv_yi: Optional[float] = None       # 流通市值(亿)
    scale: Optional[str] = None              # 大盘/中盘/小盘
    today_net_flow_yi: Optional[float] = None  # 今日净流入(亿)
    today_turnover_yi: Optional[float] = None  # 今日成交额(亿)
    today_net_rate: Optional[float] = None     # 今日净额率(%)
    history: List[Dict[str, Any]] = []          # 近n日明细
    summary_3d: Optional[Dict[str, Any]] = None  # 近3日汇总
    summary_5d: Optional[Dict[str, Any]] = None  # 近5日汇总
    strength_value: float = 0.0                  # 连续强度值 -2~+2
    strength_level: str = "普通"                 # 5档判定词


class SectorListResponse(BaseModel):
    date: str
    last_update: str
    n_window: int
    sectors: List[SectorRow]
    total: int


class MinuteDataResponse(BaseModel):
    code: str
    name: Optional[str] = None
    trade_date: str
    points: List[Dict[str, Any]]
    count: int


class StrengthRankingResponse(BaseModel):
    date: str
    n_window: int
    top_strong: List[Dict[str, Any]]        # 强/偏强 Top
    top_weak: List[Dict[str, Any]]          # 偏弱/弱 Top
    level_distribution: Dict[str, int]      # 各档数量


class L1SummaryRow(BaseModel):
    l1_name: str
    sector_count: int
    total_circ_mv_yi: Optional[float] = None   # 总流通市值(亿)
    total_net_flow_yi: Optional[float] = None  # 总今日净流入(亿)
    total_turnover_yi: Optional[float] = None  # 总今日成交额(亿)
    net_rate: Optional[float] = None           # 聚合净额率(%)
    avg_strength_value: float = 0.0            # 平均强度值
    strength_distribution: Dict[str, int] = {} # 各档数量
    strong_count: int = 0
    weak_count: int = 0
    top_sectors: List[Dict[str, Any]] = []     # 强度最高 3 个二级板块


class L1SummaryResponse(BaseModel):
    date: str
    last_update: str
    n_window: int
    l1_summaries: List[L1SummaryRow]
    total_l1: int


# ============================================================
# 辅助函数
# ============================================================
def _to_yi(v: Optional[float]) -> Optional[float]:
    """元 → 亿元"""
    if v is None:
        return None
    return round(v / 1e8, 4)


def _net_rate(net_flow: Optional[float], turnover: Optional[float]) -> Optional[float]:
    """计算净额率(%)"""
    if net_flow is None or turnover is None or turnover == 0:
        return None
    return round(net_flow / turnover * 100, 4)


def _ensure_meta() -> int:
    """确保 sector_meta 表已填充基础数据。"""
    storage = get_storage()
    existing = storage.get_all_sector_meta()
    if existing:
        return len(existing)

    # 首次：写入默认板块元数据
    meta_list = []
    for s in DEFAULT_SECTORS:
        meta_list.append({
            "code": s["code"],
            "name": s["name"],
            "l1": s["l1"],
            "circ_mv_yi": None,     # 后续从接口补
            "turnover_yi": None,
        })
    return storage.upsert_sector_meta(meta_list)


def _update_meta_with_realtime(daily_map: Dict, circ_mv_map: Dict) -> None:
    """用实时采集的数据更新 sector_meta 的 circ_mv / turnover。

    流通市值优先级：
        1. sector_circ_mv 缓存表（成分股反推累加，最准）
        2. 今日 fund flow 实时 circ_mv（实测板块级全为 0，兜底）
        3. sector_meta 已有 circ_mv_yi（兜底）
    """
    if not daily_map:
        return
    storage = get_storage()
    # 优先读 sector_circ_mv 缓存（今日 → 历史最新）
    today_str = date.today().strftime("%Y%m%d")
    circ_mv_cache_today = storage.get_all_sector_circ_mv(today_str)
    circ_mv_cache_latest = storage.get_latest_sector_circ_mv()

    meta_list = []
    for code, records in daily_map.items():
        if not records:
            continue
        today = records[0]
        turnover = today.get("turnover")  # 元
        turnover_yi = _to_yi(turnover) if turnover else None

        # 流通市值优先级
        circ_mv_yi = None
        cached = circ_mv_cache_today.get(code) or circ_mv_cache_latest.get(code)
        if cached and cached.get("circ_mv_yi"):
            circ_mv_yi = cached["circ_mv_yi"]
        elif today.get("circ_mv"):
            circ_mv_yi = _to_yi(today.get("circ_mv"))

        meta_list.append({
            "code": code,
            "circ_mv_yi": circ_mv_yi,
            "turnover_yi": turnover_yi,
        })
    if meta_list:
        storage.upsert_sector_meta(meta_list)


# ============================================================
# 接口实现
# ============================================================
@app.get("/api/health", response_model=HealthResponse)
async def health():
    """健康检查 + 存储状态 + 缓存状态"""
    storage = get_storage()
    stats = storage.get_stats()
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "trading": is_trading_time(),
        "storage": stats,
        "cache_ready": is_ready(),
        "cache_refreshing": is_refresh_in_progress(),
        "cache_last_error": get_refresh_last_error(),
        "cache_updated": get_updated_time(),
    }


@app.get("/api/config")
async def get_config():
    """返回当前配置"""
    return {
        "STRENGTH_WINDOW_N": STRENGTH_WINDOW_N,
        "DISPLAY_DAYS": DISPLAY_DAYS,
        "SUMMARY_3D": SUMMARY_3D,
        "SUMMARY_5D": SUMMARY_5D,
        "SCALE_THRESHOLDS": SCALE_THRESHOLDS,
        "trading_now": is_trading_time(),
    }


@app.get("/api/sectors", response_model=SectorListResponse)
async def get_sectors(
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
    force_refresh: bool = Query(False, description="是否强制刷新缓存"),
):
    """板块列表 + 当前强度（宽表主数据）。

    数据来源：启动时预加载的 data_cache（10 交易日），<5ms 响应。
    force_refresh=True 时触发后台异步刷新（非阻塞），立即返回当前缓存数据。
    调用方可通过 /api/health 的 cache_refreshing 字段轮询进度。

    Args:
        n: 强度判定窗口天数（须 ≤ 缓存窗口，否则用缓存窗口）
        force_refresh: True 时触发后台刷新（不阻塞本次响应）
    """
    # 强制刷新：后台异步触发，立即返回当前缓存
    if force_refresh:
        triggered = trigger_background_refresh()
        logger.info("get_sectors: force_refresh triggered=%s", triggered)

    # 未就绪：不阻塞等待，直接返回 503（后台预热会自行完成）
    if not is_ready():
        raise HTTPException(
            status_code=503,
            detail="data cache is still warming up, please retry in a few seconds",
        )

    _ensure_meta()
    storage = get_storage()

    # 从缓存读取
    sector_list = cache_get_sectors()
    codes = cache_get_codes()
    daily_map = get_daily_map()
    circ_mv_map = get_circ_mv_map()
    meta_map = {m["code"]: m for m in storage.get_all_sector_meta()}

    if not codes or not sector_list:
        raise HTTPException(status_code=500, detail="no cached sector data")

    # n 不能超过缓存窗口
    max_n = get_max_n() or n
    actual_n = min(n, max_n)

    # 组装宽表行
    rows: List[SectorRow] = []
    today_str = date.today().isoformat()

    for sec in sector_list:
        code = sec["code"]
        records = daily_map.get(code, [])
        meta = meta_map.get(code, {})

        # 今日数据
        today_rec = records[0] if records else {}
        today_net = today_rec.get("net_flow")
        today_turnover = today_rec.get("turnover")

        # 流通市值：优先缓存，其次 meta
        circ_mv_yi = circ_mv_map.get(code) or meta.get("circ_mv_yi")
        scale = get_scale(circ_mv_yi) if circ_mv_yi else (meta.get("scale") or "小盘")

        # 历史明细
        history = _build_history(records, None, actual_n)

        # 近3日/近5日汇总
        summary_3d = _build_summary(records, SUMMARY_3D, circ_mv_yi)
        summary_5d = _build_summary(records, SUMMARY_5D, circ_mv_yi)

        # 强度判定
        strength = _calc_strength_from_records(records, circ_mv_yi, actual_n)

        rows.append(SectorRow(
            code=code,
            name=sec.get("name") or meta.get("name", ""),
            l1=sec.get("l1") or meta.get("l1"),
            circ_mv_yi=circ_mv_yi,
            scale=scale,
            today_net_flow_yi=_to_yi(today_net),
            today_turnover_yi=_to_yi(today_turnover),
            today_net_rate=_net_rate(today_net, today_turnover),
            history=history,
            summary_3d=summary_3d,
            summary_5d=summary_5d,
            strength_value=strength["value"],
            strength_level=strength["level"],
        ))

    rows.sort(key=lambda r: r.strength_value, reverse=True)

    return SectorListResponse(
        date=today_str,
        last_update=datetime.now().isoformat(),
        n_window=actual_n,
        sectors=rows,
        total=len(rows),
    )


@app.get("/api/sectors/l1-summary", response_model=L1SummaryResponse)
async def get_l1_summary(
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
):
    """一级行业聚合视图：31 个一级行业 × 汇总资金流 + 强度分布。

    对 /api/sectors 的 134 个二级板块按 l1 字段分组，
    汇总总净流入/总成交额/聚合净额率/各档分布/最强 3 个二级板块。
    """
    sectors_resp = await get_sectors(n=n)
    rows = sectors_resp.sectors

    # 按 l1 分组
    from collections import OrderedDict
    groups: Dict[str, List[SectorRow]] = OrderedDict()
    for r in rows:
        l1 = r.l1 or "其他"
        groups.setdefault(l1, []).append(r)

    summaries = []
    for l1_name, group_rows in groups.items():
        count = len(group_rows)

        # 汇总金额（排除 None）
        valid_turnover = [r for r in group_rows if r.today_turnover_yi is not None]
        total_turnover = sum(r.today_turnover_yi for r in valid_turnover) if valid_turnover else None

        valid_net = [r for r in group_rows if r.today_net_flow_yi is not None]
        total_net = sum(r.today_net_flow_yi for r in valid_net) if valid_net else None

        valid_mv = [r for r in group_rows if r.circ_mv_yi is not None]
        total_mv = sum(r.circ_mv_yi for r in valid_mv) if valid_mv else None

        # 聚合净额率
        net_rate = None
        if total_net is not None and total_turnover is not None and total_turnover > 0:
            net_rate = round(total_net / total_turnover * 100, 4)

        # 平均强度值
        avg_strength = sum(r.strength_value for r in group_rows) / count if count > 0 else 0

        # 强度分布
        dist = {"强": 0, "偏强": 0, "普通": 0, "偏弱": 0, "弱": 0}
        for r in group_rows:
            lv = r.strength_level
            if lv in dist:
                dist[lv] += 1

        # 最强 3 个二级板块
        sorted_group = sorted(group_rows, key=lambda x: x.strength_value, reverse=True)
        top3 = [
            {
                "code": s.code,
                "name": s.name,
                "net_rate": s.today_net_rate,
                "strength_level": s.strength_level,
                "strength_value": s.strength_value,
            }
            for s in sorted_group[:3]
        ]

        summaries.append(L1SummaryRow(
            l1_name=l1_name,
            sector_count=count,
            total_circ_mv_yi=total_mv,
            total_net_flow_yi=total_net,
            total_turnover_yi=total_turnover,
            net_rate=net_rate,
            avg_strength_value=round(avg_strength, 3),
            strength_distribution=dist,
            strong_count=dist.get("强", 0),
            weak_count=dist.get("弱", 0),
            top_sectors=top3,
        ))

    # 按平均强度降序
    summaries.sort(key=lambda s: s.avg_strength_value, reverse=True)

    return L1SummaryResponse(
        date=sectors_resp.date,
        last_update=sectors_resp.last_update,
        n_window=n,
        l1_summaries=summaries,
        total_l1=len(summaries),
    )


@app.get("/api/sectors/{code}")
async def get_sector_detail(
    code: str,
    n: int = Query(STRENGTH_WINDOW_N, description="历史天数"),
):
    """单板块详情：近n日数据 + 强度判定"""
    storage = get_storage()
    meta = storage.get_sector_meta(code)
    if not meta:
        raise HTTPException(status_code=404, detail=f"sector {code} not found")

    records = collect_daily_records(code, n=n)
    # 流通市值优先级：sector_circ_mv 缓存 → sector_meta
    circ_mv_yi = meta.get("circ_mv_yi")
    cached_mv = storage.get_sector_circ_mv(code)
    if cached_mv and cached_mv.get("circ_mv_yi"):
        circ_mv_yi = cached_mv["circ_mv_yi"]

    strength = _calc_strength_from_records(records, circ_mv_yi, n)
    history = _build_history(records, None, n)
    summary_3d = _build_summary(records, SUMMARY_3D, circ_mv_yi)
    summary_5d = _build_summary(records, SUMMARY_5D, circ_mv_yi)

    return {
        "code": code,
        "name": meta.get("name"),
        "l1": meta.get("l1"),
        "circ_mv_yi": circ_mv_yi,
        "scale": meta.get("scale"),
        "n_window": n,
        "records": history,
        "summary_3d": summary_3d,
        "summary_5d": summary_5d,
        "strength": strength,
    }


@app.get("/api/sectors/{code}/minute", response_model=MinuteDataResponse)
async def get_sector_minute(
    code: str,
    trade_date: Optional[str] = Query(None, description="YYYYMMDD，默认今日"),
):
    """单板块当日分钟级数据（差分后的本分钟净流入）"""
    storage = get_storage()
    if trade_date is None:
        trade_date = date.today().strftime("%Y%m%d")

    deltas = storage.get_minute_deltas(code, trade_date)
    meta = storage.get_sector_meta(code)

    points = []
    for d in deltas:
        ts = d.get("timestamp")
        # 提取 HH:MM
        hhmm = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                hhmm = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                hhmm = ts[11:16] if len(ts) >= 16 else ""

        points.append({
            "time": hhmm,
            "timestamp": ts,
            "main_net_flow": d.get("main_net_flow"),       # 当日累计(元)
            "minute_delta": d.get("minute_delta"),          # 本分钟净流入增量(元)
            "turnover": d.get("turnover"),
            "turnover_delta": d.get("turnover_delta"),      # 本分钟成交额增量(元)
            "is_open_anchor": d.get("is_open_anchor", 0),   # 0/1 开盘第一条
            "circ_mv": d.get("circ_mv"),
            "main_inflow": d.get("main_inflow"),
            "main_outflow": d.get("main_outflow"),
        })

    return MinuteDataResponse(
        code=code,
        name=meta.get("name") if meta else None,
        trade_date=trade_date,
        points=points,
        count=len(points),
    )


@app.get("/api/minute/realtime")
async def get_realtime_minute(
    trade_date: Optional[str] = Query(None),
):
    """全板块当日分钟级实时数据（批量，供前端图表轮询）"""
    storage = get_storage()
    if trade_date is None:
        trade_date = date.today().strftime("%Y%m%d")

    codes = get_sector_codes()
    deltas_map = storage.get_minute_deltas_batch(codes, trade_date)

    return {
        "trade_date": trade_date,
        "timestamp": datetime.now().isoformat(),
        "sectors": {
            code: [
                {
                    "time": (lambda ts: (
                        datetime.fromisoformat(ts).strftime("%H:%M")
                        if ts else ""
                    ))(d.get("timestamp")),
                    "main_net_flow": d.get("main_net_flow"),
                    "minute_delta": d.get("minute_delta"),
                    "turnover": d.get("turnover"),
                }
                for d in deltas
            ]
            for code, deltas in deltas_map.items()
        },
    }


@app.get("/api/strength/ranking", response_model=StrengthRankingResponse)
async def get_strength_ranking(
    n: int = Query(STRENGTH_WINDOW_N),
    top: int = Query(10, description="每档返回数量"),
):
    """强度排行：强/偏强 Top + 偏弱/弱 Top + 各档分布"""
    # 复用 get_sectors 的逻辑
    sectors_resp = await get_sectors(n=n)
    rows = sectors_resp.sectors

    # 按档位分组
    level_groups: Dict[str, List[Dict[str, Any]]] = {
        "强": [], "偏强": [], "普通": [], "偏弱": [], "弱": [],
    }
    for r in rows:
        item = {
            "code": r.code,
            "name": r.name,
            "l1": r.l1,
            "scale": r.scale,
            "circ_mv_yi": r.circ_mv_yi,
            "today_net_rate": r.today_net_rate,
            "strength_value": r.strength_value,
            "strength_level": r.strength_level,
            "summary_5d": r.summary_5d,
        }
        level_groups[r.strength_level].append(item)

    # 强/偏强 Top
    top_strong = (level_groups["强"] + level_groups["偏强"])[:top]
    # 偏弱/弱 Top（强度值升序）
    top_weak = sorted(
        level_groups["偏弱"] + level_groups["弱"],
        key=lambda x: x["strength_value"],
    )[:top]

    level_distribution = {k: len(v) for k, v in level_groups.items()}

    return StrengthRankingResponse(
        date=sectors_resp.date,
        n_window=n,
        top_strong=top_strong,
        top_weak=top_weak,
        level_distribution=level_distribution,
    )


# ============================================================
# 历史回看
# ============================================================
@app.get("/api/sectors/history", response_model=SectorListResponse)
async def get_sectors_history(
    date: str = Query(..., description="YYYY-MM-DD，查询日期"),
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
):
    """历史回看：查询指定日期的板块强度宽表。

    调用 westock fund flow --date 拉取指定日期的资金流数据，
    用当日累计 + 5D/10D/20D 分段差分重建近 n 日历史，计算强度。
    注意：历史数据量较大时耗时 2-5s（实时 CLI 调用），建议前端加 loading 状态。
    """
    try:
        daily_map, circ_mv_map = collect_all_sectors_daily(n=n, asof_date=date)
    except Exception as e:
        logger.error("history collect failed for %s: %s", date, e, exc_info=True)
        raise HTTPException(status_code=503, detail=f"data fetch failed: {e}")

    storage = get_storage()
    _ensure_meta()
    sector_list = load_sectors()
    meta_map = {m["code"]: m for m in storage.get_all_sector_meta()}

    if not sector_list:
        raise HTTPException(status_code=500, detail="no sector data")

    rows: List[SectorRow] = []

    for sec in sector_list:
        code = sec["code"]
        records = daily_map.get(code, [])
        meta = meta_map.get(code, {})

        today_rec = records[0] if records else {}
        today_net = today_rec.get("net_flow")
        today_turnover = today_rec.get("turnover")

        circ_mv_yi = circ_mv_map.get(code) or meta.get("circ_mv_yi")
        scale = get_scale(circ_mv_yi) if circ_mv_yi else (meta.get("scale") or "小盘")

        history = _build_history(records, None, n)
        summary_3d = _build_summary(records, SUMMARY_3D, circ_mv_yi)
        summary_5d = _build_summary(records, SUMMARY_5D, circ_mv_yi)
        strength = _calc_strength_from_records(records, circ_mv_yi, n)

        rows.append(SectorRow(
            code=code,
            name=sec.get("name") or meta.get("name", ""),
            l1=sec.get("l1") or meta.get("l1"),
            circ_mv_yi=circ_mv_yi,
            scale=scale,
            today_net_flow_yi=_to_yi(today_net),
            today_turnover_yi=_to_yi(today_turnover),
            today_net_rate=_net_rate(today_net, today_turnover),
            history=history,
            summary_3d=summary_3d,
            summary_5d=summary_5d,
            strength_value=strength["value"],
            strength_level=strength["level"],
        ))

    rows.sort(key=lambda r: r.strength_value, reverse=True)

    return SectorListResponse(
        date=date,
        last_update=datetime.now().isoformat(),
        n_window=n,
        sectors=rows,
        total=len(rows),
    )


# ============================================================
# CSV 导出
# ============================================================
import csv
import io

from fastapi.responses import StreamingResponse


@app.get("/api/sectors/export")
async def export_sectors_csv(
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
):
    """导出板块强度宽表为 CSV。

    Returns:
        CSV 文件流，Content-Disposition: attachment
    """
    sectors_resp = await get_sectors(n=n)
    rows = sectors_resp.sectors

    output = io.StringIO()
    writer = csv.writer(output)

    # 表头
    writer.writerow([
        "板块名称", "代码", "一级行业", "流通市值(亿)", "规模",
        "今日净流入(亿)", "今日净额率(%)",
        "近3日净流入(亿)", "近3日净额率(%)",
        "近5日净流入(亿)", "近5日净额率(%)",
        "强度判定", "强度值",
    ])

    for r in rows:
        writer.writerow([
            r.name, r.code, r.l1 or "", r.circ_mv_yi, r.scale or "",
            r.today_net_flow_yi, r.today_net_rate,
            r.summary_3d.net_flow_yi if r.summary_3d else "", r.summary_3d.net_rate if r.summary_3d else "",
            r.summary_5d.net_flow_yi if r.summary_5d else "", r.summary_5d.net_rate if r.summary_5d else "",
            r.strength_level, r.strength_value,
        ])

    output.seek(0)
    filename = f"westock_sectors_{datetime.now().strftime('%Y%m%d')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ============================================================
# 历史回看
# ============================================================
@app.get("/api/alerts")
async def get_alerts(
    date: Optional[str] = Query(None, description="YYYYMMDD，默认全部"),
    limit: int = Query(50, description="最多返回条数"),
):
    """获取强度档位变化告警日志。

    告警在后台缓存刷新时自动检测，档位变化（如 普通→强）时写入。
    """
    try:
        alerts = get_storage().get_alerts(trade_date=date, limit=limit)
        return {
            "total": len(alerts),
            "alerts": alerts,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("get_alerts failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 管理接口
# ============================================================
@app.post("/api/refresh-sectors")
async def api_refresh_sectors():
    """手动刷新板块列表"""
    try:
        n = refresh_sectors()
        return {"status": "ok", "sectors_count": n, "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error("refresh_sectors failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/collect/minute")
async def api_collect_minute():
    """手动触发一次分钟采集"""
    try:
        result = collect_minute_snapshot()
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error("manual minute collect failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/collect/circ-mv")
async def api_collect_circ_mv():
    """手动触发一次流通市值采集（成分股反推累加）。

    采集全部 134 板块的成分股 → 个股 fund flow 反推 → 累加写入 sector_circ_mv 缓存。
    耗时较长（约 30-60s），建议盘前/盘后各执行一次。
    """
    try:
        from circ_mv_collector import collect_all_sectors_circ_mv
        from sectors import get_default_codes
        storage = get_storage()

        result_map = collect_all_sectors_circ_mv()

        # 写入 sector_circ_mv 缓存表
        records = []
        for code, info in result_map.items():
            info["code"] = code
            records.append(info)
        n_written = storage.upsert_sector_circ_mv(records)

        valid = sum(1 for v in result_map.values() if v.get("circ_mv") is not None)
        return {
            "status": "ok",
            "total": len(result_map),
            "valid": valid,
            "written": n_written,
        }
    except Exception as e:
        logger.error("manual circ_mv collect failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 内部辅助
# ============================================================
def _build_history(
    records: List[Dict],
    fallback_circ_mv: Optional[float],
    n: int,
) -> List[Dict[str, Any]]:
    """从日记录构建近n日历史明细（每条含 净流入/成交额/净额率）"""
    history = []
    for r in records[:n]:
        net = r.get("net_flow")
        turnover = r.get("turnover")
        circ_mv = r.get("circ_mv") or fallback_circ_mv
        history.append({
            "date": r.get("date"),
            "trade_date": r.get("trade_date"),
            "net_flow_yi": _to_yi(net),
            "turnover_yi": _to_yi(turnover),
            "net_rate": _net_rate(net, turnover),
            "circ_mv_yi": _to_yi(circ_mv) if circ_mv else None,
            "estimated": r.get("estimated", False),
        })
    return history


def _build_summary(
    records: List[Dict],
    days: int,
    circ_mv_yi: Optional[float],
) -> Optional[Dict[str, Any]]:
    """构建近N日汇总：净流入之和 + 聚合净额率

    坑：westock 不给单日历史成交额，历史记录 turnover=None。
    早期实现只累加"净流入 AND 成交额都有效"的记录，导致历史估算值被丢，
    近3日/近5日都只算到今日1条 → 三窗数据完全相同。

    成交额近似优先级（v2 修订）：
      该日自身成交额（交易时段对今日有效）
      → 流通市值 × 规模分档日均换手率（兜底，历史日和非交易时段必走此项）
    历史日不再用"今日成交额"做近似——开盘不久时今日成交额远小于日均，
    会把所有历史日的成交额也压小，导致历史净额率被系统性低估。
    """
    if not records or days <= 0:
        return None
    subset = records[:days]
    # 流通市值 × 规模分档换手率（circ_mv_yi 单位亿 → 元）
    scale = get_scale(circ_mv_yi) if circ_mv_yi else "小盘"
    rate = SCALE_TURNOVER_RATE.get(scale, 0.02)
    circ_mv_yuan = circ_mv_yi * 1e8 if circ_mv_yi else None
    fallback_turnover = circ_mv_yuan * rate if (circ_mv_yuan and circ_mv_yuan > 0) else None
    total_net = 0.0
    total_turnover = 0.0
    valid = 0
    for idx, r in enumerate(subset):
        net = r.get("net_flow")
        if net is None:
            continue
        # 成交额近似优先级：
        #   今日：自身 turnover → 流通市值×换手率 → 跳过
        #   历史日：自身 turnover → 流通市值×换手率 → 今日成交额（无 circ_mv 时最后的兜底）
        if idx == 0:
            turnover = r.get("turnover")
            if turnover is None or turnover <= 0:
                turnover = fallback_turnover
        else:
            turnover = r.get("turnover")
            if turnover is None or turnover <= 0:
                turnover = fallback_turnover
            # 流通市值未知（如未采集）：回退到今日成交额，避免历史记录全被丢弃
            if (turnover is None or turnover <= 0) and subset[0].get("turnover"):
                turnover = subset[0]["turnover"]
        if turnover is None or turnover <= 0:
            continue
        total_net += net
        total_turnover += turnover
        valid += 1
    if valid == 0 or total_turnover <= 0:
        return {
            "days": days,
            "net_flow_yi": None,
            "net_rate": None,
            "valid_days": 0,
        }
    return {
        "days": days,
        "net_flow_yi": _to_yi(total_net),
        "net_rate": round(total_net / total_turnover * 100, 4),
        "valid_days": valid,
    }


def _calc_strength_from_records(
    records: List[Dict],
    circ_mv_yi: Optional[float],
    n: int,
) -> Dict[str, Any]:
    """从日记录计算强度判定"""
    if not records or circ_mv_yi is None or circ_mv_yi <= 0:
        return {"value": 0.0, "level": "普通", "scale": "小盘"}

    strength = calc_sector_strength(records, circ_mv_yi, n)
    return {
        "value": strength["value"],
        "level": strength["level"],
        "scale": strength["scale"],
    }


# ============================================================
# 应用生命周期（FastAPI ≥0.93 推荐 lifespan，低版本回退 on_event）
# ============================================================
try:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """应用启动/关闭时执行。"""
        # ---- 启动 ----
        logger.info("=== app startup ===")
        _ensure_meta()
        storage = get_storage()
        stats = storage.get_stats()
        logger.info("storage stats: %s", stats)

        # 后台线程：预热 + 缓存加载（不阻塞启动）
        def _bg_init():
            """后台初始化：预热 CLI → 加载缓存 → 启动定时刷新"""
            import time as _time
            try:
                from westock import fund_flow
                fund_flow(["pt01801081"], raw=True)
                logger.info("westock CLI warmup done (background)")
            except Exception as e:
                logger.warning("westock CLI warmup failed: %s", e)

            try:
                ok = init_cache(n=10)
                if ok:
                    logger.info("data_cache: preloaded, %d codes ready", len(cache_get_codes()))
                    start_background_refresh(interval_sec=60)
                else:
                    logger.error("data_cache: preload FAILED")
            except Exception as e:
                logger.error("data_cache: preload error: %s", e, exc_info=True)

        threading.Thread(target=_bg_init, daemon=True, name="startup_init").start()
        logger.info("app startup complete (cache loading in background)")

        yield  # 应用运行中...

        # ---- 关闭 ----
        logger.info("=== app shutdown ===")


    # 将 lifespan 注册到 FastAPI 路由（定义在 app 之后，不能放在构造函数里）
    app.router.lifespan_context = lifespan

except ImportError:
    # Python <3.7 无 asynccontextmanager，或 FastAPI <0.93 不支持 lifespan
    logger.warning("lifespan not available, falling back to on_event")

    @app.on_event("startup")
    async def _startup_event():
        logger.info("=== app startup (on_event) ===")
        _ensure_meta()
        storage = get_storage()
        stats = storage.get_stats()
        logger.info("storage stats: %s", stats)

        def _bg_init():
            import time as _time
            try:
                from westock import fund_flow
                fund_flow(["pt01801081"], raw=True)
                logger.info("westock CLI warmup done (background)")
            except Exception as e:
                logger.warning("westock CLI warmup failed: %s", e)

            try:
                ok = init_cache(n=10)
                if ok:
                    logger.info("data_cache: preloaded, %d codes ready", len(cache_get_codes()))
                    start_background_refresh(interval_sec=60)
                else:
                    logger.error("data_cache: preload FAILED")
            except Exception as e:
                logger.error("data_cache: preload error: %s", e, exc_info=True)

        threading.Thread(target=_bg_init, daemon=True, name="startup_init").start()
        logger.info("app startup complete (cache loading in background)")


# ============================================================
# 前端静态文件（SPA 回退到 index.html）
# ============================================================
_FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")
    # SPA 回退：API 路由以外的路径 → index.html
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index_path = _FRONTEND_DIST / "index.html"
        if index_path.is_file():
            return FileResponse(str(index_path))
        raise HTTPException(status_code=404, detail="frontend not built, run: cd frontend && npm run build")
    # mount 必须在路由之后，这里用 catch-all 替代传统 mount
    logger.info("frontend static files mounted from %s", _FRONTEND_DIST)


def main():
    """直接运行入口"""
    import uvicorn
    uvicorn.run(
        "app:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
