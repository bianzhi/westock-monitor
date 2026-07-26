#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 后端：板块列表/单板块/分钟/强度排行/刷新板块。

启动:
  uvicorn app:app --host 0.0.0.0 --port 8000 --reload
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
from pydantic import BaseModel

from config import (
    API_HOST, API_PORT, CORS_ORIGINS,
    STRENGTH_WINDOW_N, DISPLAY_DAYS, SUMMARY_3D, SUMMARY_5D,
    SCALE_THRESHOLDS, get_scale,
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

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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
    allow_origins=CORS_ORIGINS + ["*"],  # 开发环境放宽
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
    """健康检查 + 存储状态"""
    storage = get_storage()
    stats = storage.get_stats()
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "trading": is_trading_time(),
        "storage": stats,
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
    use_cache: bool = Query(True, description="是否使用缓存的分钟数据"),
):
    """板块列表 + 当前强度（宽表主数据）。

    数据流：
      1. 实时调 westock fund flow + 腾讯HTTP，拿今日净流入/成交额/流通市值
      2. 实时调 westock MainNetFlow5D/10D/20D，反推近n日历史
      3. 计算 近3日/近5日 净额率
      4. 计算 5档强度判定

    Args:
        n: 强度判定窗口天数
        use_cache: 是否使用 storage 缓存
    """
    _ensure_meta()
    storage = get_storage()
    codes = get_sector_codes()
    if not codes:
        raise HTTPException(status_code=500, detail="no sector codes configured")

    # 1. 实时拉取全板块日级数据
    daily_map, circ_mv_map = collect_all_sectors_daily(n=n)
    _update_meta_with_realtime(daily_map, circ_mv_map)

    # 2. 加载板块元数据
    meta_map = {m["code"]: m for m in storage.get_all_sector_meta()}
    sector_list = load_sectors()

    # 3. 组装宽表行
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
        today_circ_mv = today_rec.get("circ_mv")

        # 流通市值：优先今日实时，其次缓存
        circ_mv_yi = None
        if today_circ_mv:
            circ_mv_yi = _to_yi(today_circ_mv)
        elif meta.get("circ_mv_yi"):
            circ_mv_yi = meta["circ_mv_yi"]

        scale = get_scale(circ_mv_yi) if circ_mv_yi else (meta.get("scale") or "小盘")

        # 历史明细 (近 DISPLAY_DAYS 日)
        history = _build_history(records, today_circ_mv, n)

        # 近3日/近5日汇总
        summary_3d = _build_summary(records, SUMMARY_3D, circ_mv_yi)
        summary_5d = _build_summary(records, SUMMARY_5D, circ_mv_yi)

        # 强度判定：基于近 n 日聚合净额率
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

    # 按强度值降序
    rows.sort(key=lambda r: r.strength_value, reverse=True)

    return SectorListResponse(
        date=today_str,
        last_update=datetime.now().isoformat(),
        n_window=n,
        sectors=rows,
        total=len(rows),
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
            "minute_delta": d.get("minute_delta"),          # 本分钟增量(元)
            "turnover": d.get("turnover"),
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
    """构建近N日汇总：净流入之和 + 聚合净额率"""
    if not records or days <= 0:
        return None
    subset = records[:days]
    total_net = 0.0
    total_turnover = 0.0
    valid = 0
    for r in subset:
        net = r.get("net_flow")
        turnover = r.get("turnover")
        if net is not None and turnover is not None and turnover > 0:
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
# 启动
# ============================================================
@app.on_event("startup")
async def startup_event():
    """应用启动时初始化"""
    logger.info("=== app startup ===")
    _ensure_meta()
    storage = get_storage()
    stats = storage.get_stats()
    logger.info("storage stats: %s", stats)


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
