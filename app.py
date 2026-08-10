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
import time
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

# 确保能 import 本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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
from westock_fund_metrics import calc_sector_metrics_batch, calc_turnover, calc_sector_metrics
from data_cache import (
    init_cache, is_ready, refresh_cache, get_max_n,
    get_codes as cache_get_codes, get_sectors as cache_get_sectors,
    get_daily_map, get_circ_mv_map, get_circ_mv, start_background_refresh,
    trigger_background_refresh, is_refresh_in_progress, get_refresh_last_error,
    get_updated_time, get_init_status,
)

# ============================================================
# 请求缓存：避免同秒内重复调 CLI 雪崩
# ============================================================
_sectors_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SEC = 15  # 15s 内重复请求直接返回缓存

# 概念板块独立缓存（避免与二级板块主缓存冲突）
_concept_cache: Dict[str, Any] = {}
_concept_cache_lock = threading.Lock()
CONCEPT_CACHE_TTL_SEC = 15  # 15s 内重复请求直接返回缓存，避免每次切 tab 都同步阻塞调 CLI
_concept_fail_count: int = 0  # CLI 连续失败次数（成功时重置）

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
# Supabase 用户认证（千人千面依赖）
# ============================================================
security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[str]:
    """从 JWT Bearer token 中提取用户 ID（无 token 返回 None，不强制登录）。"""
    if credentials is None:
        return None
    try:
        from supabase_base import create_anon_client
        client = create_anon_client()
        user = client.auth.get_user(credentials.credentials)
        return user.user.id if user and user.user else None
    except Exception:
        return None


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
    init_status = get_init_status()
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "trading": is_trading_time(),
        "storage": stats,
        "cache_ready": is_ready(),
        "cache_init": init_status,
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
    force_refresh=True 时同步拉取最新数据（阻塞 2-5s），然后返回最新结果。

    Args:
        n: 强度判定窗口天数（须 ≤ 缓存窗口，否则用缓存窗口）
        force_refresh: True 时同步刷新缓存再返回（数据与腾讯自选股实时对齐）
    """
    # 强制刷新：同步等待缓存刷新完成再返回
    if force_refresh:
        logger.info("get_sectors: force_refresh — sync refresh...")
        ok = refresh_cache()
        if not ok:
            raise HTTPException(status_code=503, detail="cache refresh failed")
        logger.info("get_sectors: force_refresh done")

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


@app.get("/api/sectors/concept", response_model=SectorListResponse)
async def get_concept_sectors(
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
    force_refresh: bool = Query(False, description="是否强制刷新"),
):
    """概念板块列表 + 当前强度（实时调 westock CLI，15s 内存缓存）。

    与二级板块不同，概念板块不依赖 data_cache（量小、变动快、与二级板块
    代码空间隔离），每次请求实时拉 fund flow。因此**不检查 is_ready()**——
    即便二级板块缓存还在预热，概念板块只要 CLI 可用就能返回数据。
    为避免切 tab 触发的雪崩式 CLI 调用，加 15s 内存缓存；CLI 异常时有
    缓存则返回旧缓存，连续失败超过阈值时返回空列表（不再 503）。
    """
    global _concept_fail_count
    cache_key = f"concept_{n}"
    now = time.time()

    # 非强制刷新：命中缓存直接返回
    if not force_refresh:
        with _concept_cache_lock:
            entry = _concept_cache.get(cache_key)
            if entry and (now - entry["ts"]) < CONCEPT_CACHE_TTL_SEC:
                logger.debug("get_concept_sectors: cache hit (age=%.1fs)", now - entry["ts"])
                return entry["data"]

    from concept_sectors import get_default_codes, get_default_name
    from westock import fund_flow, extract_main_net_flow

    codes = get_default_codes()
    if not codes:
        empty_resp = SectorListResponse(
            date=date.today().isoformat(), last_update="",
            n_window=n, sectors=[], total=0,
        )
        return empty_resp

    # 实时拉概念板块 fund flow，整体兜底
    try:
        def _sf(v):
            if v is None or v == "": return None
            try: return float(v)
            except (TypeError, ValueError): return None

        flow_records = fund_flow(codes, raw=True)
        flow_map: dict = {}
        for r in flow_records:
            c = r.get("code") or r.get("SecuCode")
            if c:
                flow_map[c] = r

        rows = []
        today_str = date.today().isoformat()
        for code in codes:
            flow = flow_map.get(code, {})
            metrics = calc_sector_metrics(flow, TURNOVER_METHOD) if flow else {}
            today_net = extract_main_net_flow(flow)
            today_turnover = metrics.get("turnover")
            net_5d = _sf(flow.get("MainNetFlow5D"))
            net_10d = _sf(flow.get("MainNetFlow10D"))
            net_20d = _sf(flow.get("MainNetFlow20D"))

            # 构建 minimal daily records 用于强度计算
            records = [{"date": today_str, "net_flow": today_net, "turnover": today_turnover,
                         "main_net_flow_5d": net_5d, "main_net_flow_10d": net_10d,
                         "main_net_flow_20d": net_20d}]
            summary_3d = _build_summary(records, SUMMARY_3D, None)
            summary_5d = _build_summary(records, SUMMARY_5D, None)
            strength = _calc_strength_from_records(records, None, n)

            rows.append(SectorRow(
                code=code, name=flow.get("name") or get_default_name(code),
                l1="概念", circ_mv_yi=None, scale="小盘",
                today_net_flow_yi=_to_yi(today_net),
                today_turnover_yi=_to_yi(today_turnover),
                today_net_rate=_net_rate(today_net, today_turnover),
                history=[], summary_3d=summary_3d, summary_5d=summary_5d,
                strength_value=strength["value"], strength_level=strength["level"],
            ))

        rows.sort(key=lambda r: r.strength_value, reverse=True)
        resp = SectorListResponse(
            date=today_str, last_update=datetime.now().isoformat(),
            n_window=n, sectors=rows, total=len(rows),
        )

        # 写入缓存
        with _concept_cache_lock:
            _concept_cache[cache_key] = {"ts": now, "data": resp}

        _concept_fail_count = 0  # 成功后重置失败计数
        return resp

    except HTTPException:
        raise
    except Exception as e:
        _concept_fail_count += 1
        logger.exception("get_concept_sectors failed (consecutive=%d): %s", _concept_fail_count, e)

        # 有旧缓存则返回旧缓存（过期也先顶上）
        with _concept_cache_lock:
            entry = _concept_cache.get(cache_key)
            if entry:
                logger.info("get_concept_sectors: returning stale cache (age=%.1fs)", now - entry["ts"])
                return entry["data"]

        # 连续失败 ≤3 次：503 让前端重试，CLI 可能只是暂时抖动
        if _concept_fail_count <= 3:
            raise HTTPException(status_code=503, detail=f"concept fund flow failed: {e}")

        # 连续失败 >3 次：CLI 确认不可用，返回空列表不再 503
        logger.warning("get_concept_sectors: %d consecutive failures, returning empty (CLI unavailable)",
                       _concept_fail_count)
        return SectorListResponse(
            date=date.today().isoformat(), last_update="",
            n_window=n, sectors=[], total=0,
        )


@app.get("/api/sectors/l1-summary", response_model=L1SummaryResponse)
async def get_l1_summary(
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
):
    """一级行业聚合视图（直接从缓存读取，不调用 get_sectors）。"""
    if not is_ready():
        raise HTTPException(status_code=503, detail="cache warming up")

    _ensure_meta()
    sector_list = cache_get_sectors()
    daily_map = get_daily_map()
    circ_mv_map = get_circ_mv_map()

    # 按 l1 分组，聚合 today_net / today_turnover / strength
    from collections import OrderedDict
    groups: Dict[str, list] = OrderedDict()
    for sec in sector_list:
        code = sec["code"]
        l1 = sec.get("l1") or "其他"
        records = daily_map.get(code, [])
        today_rec = records[0] if records else {}
        today_net = today_rec.get("net_flow")
        today_turnover = today_rec.get("turnover")
        circ_mv_yi = circ_mv_map.get(code)
        scale = get_scale(circ_mv_yi) if circ_mv_yi else "小盘"
        strength = _calc_strength_from_records(records, circ_mv_yi, n)
        groups.setdefault(l1, []).append({
            "code": code,
            "name": sec.get("name", ""),
            "today_net": today_net,
            "today_turnover": today_turnover,
            "net_rate": _net_rate(today_net, today_turnover),
            "circ_mv_yi": circ_mv_yi,
            "strength_value": strength["value"],
            "strength_level": strength["level"],
        })

    summaries = []
    for l1_name, group_rows in groups.items():
        count = len(group_rows)
        valid_net = [r for r in group_rows if r["today_net"] is not None]
        total_net = sum(r["today_net"] for r in valid_net) if valid_net else None
        valid_to = [r for r in group_rows if r["today_turnover"] is not None]
        total_turnover = sum(r["today_turnover"] for r in valid_to) if valid_to else None
        total_mv = sum(r["circ_mv_yi"] for r in group_rows if r["circ_mv_yi"]) or None
        net_rate = _net_rate(total_net, total_turnover) if total_net and total_turnover else None

        avg_strength = sum(r["strength_value"] for r in group_rows) / count if count else 0
        dist = {"强": 0, "偏强": 0, "普通": 0, "偏弱": 0, "弱": 0}
        for r in group_rows:
            lv = r["strength_level"]
            if lv in dist:
                dist[lv] += 1
        sorted_group = sorted(group_rows, key=lambda x: x["strength_value"], reverse=True)
        top3 = [{"code": s["code"], "name": s["name"], "net_rate": s["net_rate"],
                 "strength_level": s["strength_level"], "strength_value": s["strength_value"]}
                for s in sorted_group[:3]]

        summaries.append(L1SummaryRow(
            l1_name=l1_name, sector_count=count,
            total_circ_mv_yi=total_mv, total_net_flow_yi=_to_yi(total_net),
            total_turnover_yi=_to_yi(total_turnover), net_rate=net_rate,
            avg_strength_value=round(avg_strength, 3), strength_distribution=dist,
            strong_count=dist.get("强", 0), weak_count=dist.get("弱", 0),
            top_sectors=top3,
        ))

    summaries.sort(key=lambda s: s.avg_strength_value, reverse=True)
    return L1SummaryResponse(
        date=date.today().isoformat(), last_update=datetime.now().isoformat(),
        n_window=n, l1_summaries=summaries, total_l1=len(summaries),
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
                hhmm = dt.strftime("%H:%M:%S")
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
                        datetime.fromisoformat(ts).strftime("%H:%M:%S")
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


@app.get("/api/minute/compare")
async def get_minute_compare(
    method: str = Query("rank", description="排序方式: rank=按今日净流入排名 / code=按板块编号 / manual=手动指定"),
    start: int = Query(1, ge=1, description="起始序号（1-based）"),
    end: int = Query(10, ge=1, description="结束序号（含）"),
    source: str = Query("l2", description="板块来源: l2=二级板块 / concept=概念板块"),
    codes: Optional[str] = Query(None, description="手动指定板块代码，逗号分隔（method=manual 时使用）"),
    trade_date: Optional[str] = Query(None, description="YYYYMMDD，默认今日"),
):
    """多板块分时对比图数据。

    Args:
        method: rank(按今日净流入倒序取区间) / code(按板块编号顺序取区间) / manual(手动指定 codes)
        start/end: 区间序号
        source: l2 或 concept
        codes: 手动指定板块代码，逗号分隔
        trade_date: 交易日期
    """
    storage = get_storage()
    if trade_date is None:
        trade_date = date.today().strftime("%Y%m%d")

    # 获取板块代码列表和名称映射
    if source == "concept":
        from concept_sectors import get_default_codes, get_concept_sectors
        all_codes = get_default_codes()
        sector_map = {c: {"name": n} for c, n in get_concept_sectors().items()}
    else:
        from sectors import get_default_sector_map
        sector_map = get_default_sector_map()
        all_codes = get_sector_codes()

    # 手动指定模式
    if method == "manual" and codes:
        selected = [c.strip() for c in codes.split(",") if c.strip()]
    elif method == "rank":
        # 按今日净流入倒序
        from westock import fund_flow
        if source == "concept":
            flow_records = fund_flow(all_codes, raw=True)
            flow_map = {r.get("code") or r.get("SecuCode"): r for r in flow_records if r}
            def _sf(v):
                if v is None or v == "": return None
                try: return float(v)
                except (TypeError, ValueError): return None
            ranked = [(c, _sf(flow_map.get(c, {}).get("MainNetFlow")) or 0) for c in all_codes]
        else:
            daily_map = get_daily_map()
            if daily_map:
                ranked = [(c, (daily_map.get(c, [{}])[0].get("net_flow") or 0)) for c in all_codes]
            else:
                # 缓存未就绪：降级用 fund_flow 实时排名
                logger.info("get_minute_compare: daily_map empty, falling back to fund_flow for ranking")
                from westock import extract_main_net_flow as _emnf
                flow_records = fund_flow(all_codes, raw=True)
                flow_map = {r.get("code") or r.get("SecuCode"): r for r in flow_records if r}
                ranked = [(c, _emnf(flow_map.get(c, {})) or 0) for c in all_codes]
        ranked.sort(key=lambda x: x[1], reverse=True)
        ordered_codes = [c for c, _ in ranked]
        selected = ordered_codes[start - 1:end]
    else:
        ordered_codes = sorted(all_codes)
        selected = ordered_codes[start - 1:end]

    # 批量拿分钟数据（采集由后台统一线程负责，API 只读不写）
    deltas_map = storage.get_minute_deltas_batch(selected, trade_date)

    # 分钟数据全空时兜底：调 fund_flow 拿至少一个实时快照点
    all_empty = all(len(v) == 0 for v in deltas_map.values())
    if all_empty and selected:
        logger.info("get_minute_compare: no minute data in storage, falling back to fund_flow snapshot")
        try:
            from westock import fund_flow as _ff, extract_main_net_flow as _emnf
            flow_records = _ff(selected, raw=True)
            flow_map = {r.get("code") or r.get("SecuCode"): r for r in flow_records}
            now_ts = datetime.now()
            now_iso = now_ts.isoformat()
            for code in selected:
                flow = flow_map.get(code, {})
                mnf = _emnf(flow)
                deltas_map[code] = [{
                    "timestamp": now_iso,
                    "main_net_flow": mnf,
                    "minute_delta": None,
                    "turnover": None,
                    "turnover_delta": None,
                    "is_open_anchor": 1,  # 标记为快照点（非分钟差分），前端可用于特殊显示
                }]
            logger.info("get_minute_compare: fund_flow snapshot ok, %d codes", len(flow_map))
        except Exception as e:
            logger.warning("get_minute_compare: fund_flow fallback failed: %s", e)

    series = []
    for code in selected:
        deltas = deltas_map.get(code, [])
        points = []
        for d in deltas:
            ts = d.get("timestamp")
            hhmm = ""
            if ts:
                try:
                    hhmm = datetime.fromisoformat(ts).strftime("%H:%M:%S")
                except (ValueError, TypeError):
                    hhmm = ts[11:16] if len(ts) >= 16 else ""
            points.append({
                "time": hhmm,
                "timestamp": ts,                               # ISO 时间戳（前端 time 轴用）
                "main_net_flow": d.get("main_net_flow"),      # 当日累计(元)
                "minute_delta": d.get("minute_delta"),         # 本分钟增量(元)
                "turnover": d.get("turnover"),
                "turnover_delta": d.get("turnover_delta"),
                "is_open_anchor": d.get("is_open_anchor", 0),
            })
        sec = sector_map.get(code, {})
        series.append({
            "code": code,
            "name": sec.get("name", code),
            "l1": sec.get("l1"),
            "rank": start - 1 + selected.index(code) + 1,
            "points": points,
            "point_count": len(points),
        })

    return {
        "trade_date": trade_date,
        "method": method,
        "start": start,
        "end": end,
        "series_count": len(series),
        "series": series,
    }


@app.post("/api/minute/focus")
async def set_focus_codes(
    body: Dict[str, Any],
):
    """设置高频聚焦采集的板块代码。

    Request body: {"codes": ["pt01801081", "pt01801055", ...]}
    传空列表即停止聚焦采集。
    """
    codes = body.get("codes", [])
    from collector import set_focused_codes
    n = set_focused_codes(codes)
    return {
        "status": "ok",
        "focused_count": n,
        "focused_codes": codes,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/minute/focus/status")
async def get_focus_status():
    """获取当前聚焦采集的板块列表。"""
    from collector import get_focused_codes
    codes = get_focused_codes()
    return {
        "focused_count": len(codes),
        "focused_codes": codes,
        "timestamp": datetime.now().isoformat(),
    }


# ============================================================
# 用户千人千面 API（自选板块 / 告警 / 偏好）
# ============================================================
@app.get("/api/user/watchlist")
async def get_user_watchlist(user_id: Optional[str] = Depends(get_current_user)):
    """获取当前用户的自选板块列表。"""
    if not user_id:
        return {"watchlist": [], "user_id": None}
    from supabase_user import get_user_watchlist as _get_wl
    codes = _get_wl(user_id)
    return {"watchlist": codes, "user_id": user_id}


@app.post("/api/user/watchlist")
async def add_user_watchlist(
    body: Dict[str, Any],
    user_id: Optional[str] = Depends(get_current_user),
):
    """添加自选板块。body: {"codes": ["pt01801081", ...]}"""
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")
    codes = body.get("codes", [])
    from supabase_user import add_user_watchlist as _add
    n = _add(user_id, codes)
    return {"status": "ok", "added": n}


@app.delete("/api/user/watchlist")
async def remove_user_watchlist(
    body: Dict[str, Any],
    user_id: Optional[str] = Depends(get_current_user),
):
    """删除自选板块。body: {"codes": ["pt01801081", ...]}"""
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")
    codes = body.get("codes", [])
    from supabase_user import remove_user_watchlist as _rm
    n = _rm(user_id, codes)
    return {"status": "ok", "removed": n}


@app.get("/api/user/alerts")
async def get_user_alerts(user_id: Optional[str] = Depends(get_current_user)):
    """获取用户告警阈值。"""
    if not user_id:
        return {"alerts": {}}
    from supabase_user import get_user_alerts as _ga
    return {"alerts": _ga(user_id), "user_id": user_id}


@app.post("/api/user/alerts")
async def save_user_alerts(
    body: Dict[str, Any],
    user_id: Optional[str] = Depends(get_current_user),
):
    """保存用户告警阈值。body: {"strength_up": 2, ...}"""
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")
    from supabase_user import save_user_alerts as _sa
    _sa(user_id, body)
    return {"status": "ok"}


@app.get("/api/user/prefs")
async def get_user_prefs(user_id: Optional[str] = Depends(get_current_user)):
    """获取用户看板偏好（m/n 等）。"""
    if not user_id:
        return {"prefs": {}}
    from supabase_user import get_user_prefs as _gp
    return {"prefs": _gp(user_id), "user_id": user_id}


@app.post("/api/user/prefs")
async def save_user_prefs(
    body: Dict[str, Any],
    user_id: Optional[str] = Depends(get_current_user),
):
    """保存用户看板偏好。body: {"compare_start": 1, "compare_end": 10, ...}"""
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")
    from supabase_user import save_user_prefs as _sp
    _sp(user_id, body)
    return {"status": "ok"}


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

        # 后台线程：预热 + 缓存加载（不阻塞启动，失败自动重试）
        def _bg_init():
            """后台初始化：预热 CLI → 加载缓存 → 启动定时刷新。
            若 init_cache 失败，每 30s 自动重试，最多 10 次（共 5 分钟窗口）。
            """
            import time as _time
            from data_cache import _set_init_error, _inc_init_retry

            try:
                from westock import fund_flow
                fund_flow(["pt01801081"], raw=True)
                logger.info("westock CLI warmup done (background)")
            except Exception as e:
                logger.warning("westock CLI warmup failed: %s", e)

            max_retries = 10
            for attempt in range(1, max_retries + 1):
                try:
                    ok = init_cache(n=10)
                    if ok:
                        logger.info("data_cache: preloaded, %d codes ready (attempt %d)",
                                    len(cache_get_codes()), attempt)
                        start_background_refresh(interval_sec=60)
                        _set_init_error(None)
                        return
                    else:
                        _inc_init_retry()
                        _set_init_error(f"init_cache returned False (attempt {attempt}/{max_retries})")
                        logger.error("data_cache: preload FAILED (attempt %d/%d)", attempt, max_retries)
                except Exception as e:
                    _inc_init_retry()
                    _set_init_error(f"init_cache exception: {e} (attempt {attempt}/{max_retries})")
                    logger.error("data_cache: preload error (attempt %d/%d): %s",
                                 attempt, max_retries, e, exc_info=True)

                if attempt < max_retries:
                    logger.info("data_cache: retrying init_cache in 30s (attempt %d/%d)...",
                                attempt, max_retries)
                    _time.sleep(30)

            logger.error("data_cache: all %d init_cache attempts FAILED, giving up", max_retries)

        threading.Thread(target=_bg_init, daemon=True, name="startup_init").start()
        logger.info("app startup complete (cache loading in background)")

        # 启动统一自适应采集线程（聚焦 8s / 全量 60s，单一线程无竞争）
        from collector import run_collector_loop
        threading.Thread(target=run_collector_loop, daemon=True, name="collector").start()
        logger.info("collector loop started")

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
            from data_cache import _set_init_error, _inc_init_retry
            try:
                from westock import fund_flow
                fund_flow(["pt01801081"], raw=True)
                logger.info("westock CLI warmup done (background)")
            except Exception as e:
                logger.warning("westock CLI warmup failed: %s", e)

            max_retries = 10
            for attempt in range(1, max_retries + 1):
                try:
                    ok = init_cache(n=10)
                    if ok:
                        logger.info("data_cache: preloaded, %d codes ready (attempt %d)",
                                    len(cache_get_codes()), attempt)
                        start_background_refresh(interval_sec=60)
                        _set_init_error(None)
                        return
                    else:
                        _inc_init_retry()
                        _set_init_error(f"init_cache returned False (attempt {attempt}/{max_retries})")
                        logger.error("data_cache: preload FAILED (attempt %d/%d)", attempt, max_retries)
                except Exception as e:
                    _inc_init_retry()
                    _set_init_error(f"init_cache exception: {e} (attempt {attempt}/{max_retries})")
                    logger.error("data_cache: preload error (attempt %d/%d): %s",
                                 attempt, max_retries, e, exc_info=True)

                if attempt < max_retries:
                    logger.info("data_cache: retrying init_cache in 30s (attempt %d/%d)...",
                                attempt, max_retries)
                    _time.sleep(30)

            logger.error("data_cache: all %d init_cache attempts FAILED, giving up", max_retries)

        threading.Thread(target=_bg_init, daemon=True, name="startup_init").start()
        logger.info("app startup complete (cache loading in background)")

        # 启动统一自适应采集线程（聚焦 8s / 全量 60s，单一线程无竞争）
        from collector import run_collector_loop
        threading.Thread(target=run_collector_loop, daemon=True, name="collector").start()
        logger.info("collector loop started")


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
