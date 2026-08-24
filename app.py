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
    TURNOVER_METHOD,
)
from sectors import DEFAULT_SECTORS, get_default_sector_map
from storage import get_storage
from collector import (
    load_sectors, get_sector_codes, refresh_sectors,
    collect_minute_snapshot, collect_all_sectors_daily,
    is_trading_time,
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
    get_daily, get_daily_map, get_circ_mv_map, get_circ_mv, start_background_refresh,
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

# 概念板块全量 flow 后台缓存（解决单 worker 阻塞：API 秒回，刷新由后台线程做）
_concept_flow_cache: Dict[str, Any] = {}  # {"ts": float, "flow_map": dict, "codes": list}
_concept_flow_lock = threading.Lock()
CONCEPT_FLOW_TTL_SEC = 45  # 后台每 ~45s 刷新一次全量 flow
_concept_flow_refreshing = False  # 刷新去重锁

# 内存错误缓冲（供 /api/errors 实时查询）
from collections import deque
_error_buffer: deque = deque(maxlen=200)  # 最近 200 条错误


def _record_error(level: str, msg: str):
    """记录错误到内存缓冲，供 /api/errors 页面查询。"""
    from datetime import datetime
    _error_buffer.append({
        "time": datetime.now().isoformat(),
        "level": level,
        "msg": msg[:500],  # 截断过长消息
    })

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
    # 数据源活性：每个源最近一次调用结果（ok/fail/skip）+ 最近错误
    data_sources: Dict[str, Any] = {}


class SectorRow(BaseModel):
    code: str
    name: str
    l1: Optional[str] = None
    circ_mv_yi: Optional[float] = None       # 流通市值(亿)
    scale: Optional[str] = None              # 大盘/中盘/小盘
    today_net_flow_yi: Optional[float] = None  # 今日净流入(亿)
    today_turnover_yi: Optional[float] = None  # 今日成交额(亿)
    today_net_rate: Optional[float] = None     # 今日净额率(%)
    change_pct: Optional[float] = None          # 板块涨跌幅(%)，按流通市值加权
    turnover_rate: Optional[float] = None       # 板块换手率(%)，按流通市值加权
    fund_strength: Optional[float] = None       # 资金强度 = 净流入/流通市值(%)
    consecutive_inflow_days: int = 0            # 连续净流入天数
    divergence: bool = False                    # 背离：净流入>0 但涨跌幅<0
    history: List[Dict[str, Any]] = []          # 近n日明细
    summary_3d: Optional[Dict[str, Any]] = None  # 近3日汇总
    summary_5d: Optional[Dict[str, Any]] = None  # 近5日汇总
    strength_value: float = 0.0                  # 连续强度值 -2~+2
    strength_level: str = "普通"                 # 5档判定词
    estimated: bool = False                      # True=缓存空仅今日 fallback，非真多日累加


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
    """健康检查 + 存储状态 + 缓存状态 + 数据源活性"""
    storage = get_storage()
    stats = storage.get_stats()
    init_status = get_init_status()

    # 数据源活性：实测三个外部源是否可用
    # westock CLI（资金流 + 成交额） / Tushare（流通市值） / 交易日历
    # 注意：腾讯原 HTTP 接口已死，成交额改用 westock 自身字段，无独立数据源
    data_sources: Dict[str, Any] = {}
    # 1. westock CLI —— 用 version 调用代替真实 fund flow，避免污染数据
    try:
        from westock import _ping_cli
        cli_ok, cli_ver = _ping_cli()
        data_sources["westock_cli"] = {
            "status": "ok" if cli_ok else "fail",
            "version": cli_ver,
            "note": "资金流 + 成交额（替代已死的腾讯 HTTP）",
        }
    except Exception as e:
        data_sources["westock_cli"] = {"status": "skip", "error": str(e)[:200]}

    # 2. Tushare —— 流通市值数据源（circ_mv_collector 方案 A）
    try:
        from circ_mv_collector import _ping_tushare
        tushare_ok, scope = _ping_tushare()
        data_sources["tushare"] = {
            "status": "ok" if tushare_ok else "degraded",
            "scope": scope,
            "note": "流通市值主方案；degraded 时降级 westock 反推（精度 ±20%）" if not tushare_ok else "",
        }
    except Exception as e:
        data_sources["tushare"] = {"status": "skip", "error": str(e)[:200]}

    # 3. 交易日历 —— Tushare Token 是否就绪（缺则降级 weekday 启发式）
    try:
        from trading_calendar import has_tushare_token
        data_sources["trading_calendar"] = {
            "status": "ok" if has_tushare_token() else "degraded",
            "note": "degraded 时降级 weekday 启发式，节假日会错" if not has_tushare_token() else "",
        }
    except Exception as e:
        data_sources["trading_calendar"] = {"status": "skip", "error": str(e)[:200]}

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
        "data_sources": data_sources,
    }


@app.get("/api/errors")
async def get_errors(limit: int = Query(100, description="返回最近 N 条错误")):
    """错误日志：内存缓冲 + 日志文件最近 ERROR/WARNING 行"""
    import re
    errors = list(_error_buffer)
    # 补充日志文件中的 ERROR/WARNING（去重）
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        seen = {e["msg"] for e in errors}
        for line in reversed(lines):
            if len(errors) >= limit:
                break
            m = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(\w+)\] (\w+): (.+)', line)
            if m:
                time_str, level, _, msg = m.group(1), m.group(2), m.group(3), m.group(4).strip()
                if level in ("ERROR", "WARNING", "CRITICAL") and msg not in seen:
                    errors.append({"time": time_str, "level": level, "msg": msg[:500]})
                    seen.add(msg)
    except Exception:
        pass
    errors.sort(key=lambda e: e["time"], reverse=True)
    return {
        "count": len(errors),
        "errors": errors[:limit],
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
    # 批量读流通市值缓存（含 change_pct/turnover_rate，方案 C 腾讯落库）
    circ_mv_detail = storage.get_latest_sector_circ_mv()

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

        # 涨跌幅/换手率（方案 C 腾讯落库）
        circ_detail = circ_mv_detail.get(code, {})
        change_pct = circ_detail.get("change_pct")
        turnover_rate = circ_detail.get("turnover_rate")
        # 资金强度 = 净流入 / 流通市值 (%)
        fund_strength = None
        if today_net is not None and circ_mv_yi and circ_mv_yi > 0:
            fund_strength = round(today_net / (circ_mv_yi * 1e8) * 100, 4)
        # 连续净流入天数（从最新往前数连续 net_flow > 0）
        consecutive_days = 0
        for _r in records:
            _nf = _r.get("net_flow")
            if _nf is not None and _nf > 0:
                consecutive_days += 1
            else:
                break
        # 背离：净流入 > 0 但涨跌幅 < 0（警惕出货）
        divergence = (today_net is not None and today_net > 0
                      and change_pct is not None and change_pct < 0)

        rows.append(SectorRow(
            code=code,
            name=sec.get("name") or meta.get("name", ""),
            l1=sec.get("l1") or meta.get("l1"),
            circ_mv_yi=circ_mv_yi,
            scale=scale,
            today_net_flow_yi=_to_yi(today_net),
            today_turnover_yi=_to_yi(today_turnover),
            today_net_rate=_net_rate(today_net, today_turnover),
            change_pct=change_pct,
            turnover_rate=turnover_rate,
            fund_strength=fund_strength,
            consecutive_inflow_days=consecutive_days,
            divergence=divergence,
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


# ============================================================
# 概念板块全量 flow 后台缓存（解决单 worker 阻塞）
# ============================================================
def _refresh_concept_flow_cache() -> Dict[str, Any]:
    """后台拉取全量概念板块 fund_flow，存 _concept_flow_cache。

    与 get_concept_sectors 解耦：API 层只读缓存，刷新由后台线程做，
    避免 626 码 fund_flow 的 10-15s 阻塞 uvicorn 单 worker。
    幂等：_concept_flow_refreshing 去重，并发调用只拉一次。
    """
    global _concept_flow_refreshing
    if _concept_flow_refreshing:
        return _concept_flow_cache.get("flow_map", {})
    _concept_flow_refreshing = True
    try:
        from concept_sectors import get_default_codes
        from westock import fund_flow
        codes = get_default_codes()
        if not codes:
            return {}
        t0 = time.time()
        flow_records = fund_flow(codes, raw=True)
        flow_map: Dict[str, Dict] = {}
        for r in flow_records:
            c = r.get("code") or r.get("SecuCode")
            if c:
                flow_map[c] = r
        with _concept_flow_lock:
            _concept_flow_cache["ts"] = time.time()
            _concept_flow_cache["flow_map"] = flow_map
            _concept_flow_cache["codes"] = codes
        logger.info("concept_flow_cache: refreshed %d/%d codes in %.1fs",
                    len(flow_map), len(codes), time.time() - t0)
        return flow_map
    except Exception as e:
        logger.warning("concept_flow_cache refresh failed: %s", e, exc_info=True)
        return _concept_flow_cache.get("flow_map", {})
    finally:
        _concept_flow_refreshing = False


def _concept_flow_loop():
    """后台定期刷新概念板块 flow 缓存（每 CONCEPT_FLOW_TTL_SEC 秒）。"""
    import time as _t
    while True:
        try:
            _refresh_concept_flow_cache()
        except Exception as e:
            logger.warning("concept_flow_loop error: %s", e)
        _t.sleep(CONCEPT_FLOW_TTL_SEC)


def _spawn_concept_flow_refresh() -> None:
    """异步触发一次概念板块 flow 后台刷新（不阻塞请求路径）。

    用于缓存过期/缺失时：本请求先用旧缓存或走兜底，刷新交给后台线程完成。
    `_refresh_concept_flow_cache` 内部有 `_concept_flow_refreshing` 去重锁，
    并发多次触发只会真正拉取一次。
    """
    def _run():
        try:
            _refresh_concept_flow_cache()
        except Exception as e:
            logger.warning("concept flow async refresh failed: %s", e)
    threading.Thread(target=_run, daemon=True, name="concept_flow_async_refresh").start()


def _collector_loop_entry():
    """collector 线程入口（延迟导入，供守护器使用）。"""
    from collector import run_collector_loop
    run_collector_loop()


def _circ_mv_loop_entry():
    """流通市值日级采集线程入口（延迟导入，供守护器使用）。

    方案 C（腾讯）落库 change_pct/turnover_rate 依赖此循环在 09:15/15:05 触发，
    否则涨跌幅/换手率两列无数据。
    """
    from collector import run_circ_mv_loop
    run_circ_mv_loop()


def _supervise_thread(target, name: str, restart_delay: int = 10,
                      restart_on_exit: bool = True) -> None:
    """统一线程守护器：被守护线程异常退出后自动重启。

    原子执行单元 = 线程。任何 daemon 线程若因未捕获异常退出，
    systemd 不会感知（它只管主进程），需在此层兜底。
    用 wrapper 捕获线程内异常（t.join() 不传播线程内部异常，
    无法直接区分正常返回/异常退出）。

    Args:
        target: 线程目标函数
        name: 线程名（日志用）
        restart_delay: 异常退出后重启间隔（秒）
        restart_on_exit: True=常驻线程（任何退出都重启）；
                         False=任务型线程（正常完成不重启，异常才重启）
    """
    import time as _t
    while True:
        state: Dict[str, Any] = {"error": None}

        def _wrap():
            try:
                target()
            except Exception as e:  # noqa: BLE001
                state["error"] = e

        t = threading.Thread(target=_wrap, daemon=True, name=name)
        t.start()
        t.join()  # 阻塞直到线程退出（常驻线程正常永不退出）

        if not restart_on_exit and state["error"] is None:
            # 任务型线程正常完成 → 不重启
            logger.info("%s thread finished normally", name)
            return
        if state["error"] is not None:
            logger.error("%s thread died: %s, restarting in %ds...",
                         name, state["error"], restart_delay)
        else:
            logger.error("%s thread exited, restarting in %ds...", name, restart_delay)
        _t.sleep(restart_delay)


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

    # 非强制刷新：命中响应缓存直接返回（秒回）
    if not force_refresh:
        with _concept_cache_lock:
            entry = _concept_cache.get(cache_key)
            if entry and (now - entry["ts"]) < CONCEPT_CACHE_TTL_SEC:
                logger.debug("get_concept_sectors: cache hit (age=%.1fs)", now - entry["ts"])
                return entry["data"]

    from concept_sectors import get_default_codes, get_default_name
    from westock import extract_main_net_flow

    storage = get_storage()
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

        # 优先读后台 flow 缓存（已 refresh 且未过期 → 秒回，不阻塞单 worker）
        with _concept_flow_lock:
            cached = _concept_flow_cache.get("flow_map", {})
            cached_ts = _concept_flow_cache.get("ts", 0)
        if cached:
            # 有过期但非空缓存：先用旧缓存顶上，异步触发后台刷新，不同步调 CLI
            if (now - cached_ts) >= CONCEPT_FLOW_TTL_SEC:
                _spawn_concept_flow_refresh()
            flow_map = cached
        else:
            # 缓存完全缺失：异步触发后台刷新，本请求走兜底（503/空列表），不同步调 CLI
            _spawn_concept_flow_refresh()
            raise RuntimeError("concept flow cache empty, background refresh triggered")
        if not flow_map:
            raise RuntimeError("concept flow cache empty")

        rows = []
        today_str = date.today().isoformat()
        # 批量读流通市值缓存（含 change_pct/turnover_rate，方案 C 腾讯落库）
        circ_mv_detail = storage.get_latest_sector_circ_mv()
        for code in codes:
            flow = flow_map.get(code, {})
            metrics = calc_sector_metrics(flow, TURNOVER_METHOD) if flow else {}
            today_net = extract_main_net_flow(flow)
            today_turnover = metrics.get("turnover")
            net_5d = _sf(flow.get("MainNetFlow5D"))
            net_10d = _sf(flow.get("MainNetFlow10D"))
            net_20d = _sf(flow.get("MainNetFlow20D"))

            # 近3日 + 近5日：统一走 concept_daily 真缓存累加（缓存空则仅今日）
            # 修正 G4：原近5日用 MainNetFlow5D/4 均摊估算，与近3日口径分裂、
            # 且强度判定输入与展示输入不一致。现统一走 concept_daily 真记录，
            # 缓存空时 fallback 仅今日（valid_days=1），不再混入估算。
            cached_daily = storage.get_concept_daily_batch([code], days=20).get(code, [])
            records_5d = []
            records_3d = []
            if cached_daily:
                # cached_daily 已按 trade_date 倒序，取需要的窗口
                cached_empty_fallback = False
                for d in cached_daily:
                    rec = {
                        "date": d["trade_date"],
                        "net_flow": d.get("net_flow"),
                        "turnover": d.get("turnover"),
                    }
                    records_5d.append(rec)
                    records_3d.append(rec)
                # 近3日仅取前 3 条（含今日）
                records_3d = records_3d[:SUMMARY_3D]
                # 近5日仅取前 5 条
                records_5d = records_5d[:SUMMARY_5D]
            else:
                today_rec = {"date": today_str, "net_flow": today_net, "turnover": today_turnover}
                records_3d = [today_rec]
                records_5d = [today_rec]
                cached_empty_fallback = True

            summary_3d = _build_summary(records_3d, SUMMARY_3D, None)
            summary_5d = _build_summary(records_5d, SUMMARY_5D, None)

            # 概念板块流通市值/涨跌/换手（方案 C 腾讯落库）
            circ_detail = circ_mv_detail.get(code, {})
            c_mv_yi = circ_detail.get("circ_mv_yi")
            c_change_pct = circ_detail.get("change_pct")
            c_turnover = circ_detail.get("turnover_rate")
            c_scale = get_scale(c_mv_yi) if c_mv_yi else "小盘"
            # 资金强度 = 净流入 / 流通市值 (%)
            c_fund_strength = None
            if today_net is not None and c_mv_yi and c_mv_yi > 0:
                c_fund_strength = round(today_net / (c_mv_yi * 1e8) * 100, 4)
            # 连续净流入天数
            c_consecutive = 0
            for _r in (cached_daily or [today_rec]):
                _nf = _r.get("net_flow")
                if _nf is not None and _nf > 0:
                    c_consecutive += 1
                else:
                    break
            # 背离：净流入 > 0 但涨跌幅 < 0
            c_divergence = (today_net is not None and today_net > 0
                            and c_change_pct is not None and c_change_pct < 0)
            # 强度判定输入与展示输入一致：用近 n 日真记录（不足 n 时用已有），
            # 流通市值用真实值（修复硬编码 None 导致全部「普通」的问题）
            strength_records = (cached_daily[:n] if cached_daily else [today_rec])
            strength = _calc_strength_from_records(strength_records, c_mv_yi, n)

            rows.append(SectorRow(
                code=code, name=flow.get("name") or get_default_name(code),
                l1="概念", circ_mv_yi=c_mv_yi, scale=c_scale,
                today_net_flow_yi=_to_yi(today_net),
                today_turnover_yi=_to_yi(today_turnover),
                today_net_rate=_net_rate(today_net, today_turnover),
                change_pct=c_change_pct,
                turnover_rate=c_turnover,
                fund_strength=c_fund_strength,
                consecutive_inflow_days=c_consecutive,
                divergence=c_divergence,
                history=[], summary_3d=summary_3d, summary_5d=summary_5d,
                strength_value=strength["value"], strength_level=strength["level"],
                estimated=cached_empty_fallback,
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


@app.get("/api/sectors/history", response_model=SectorListResponse)
async def get_sectors_history(
    date: str = Query(..., description="YYYY-MM-DD，查询日期"),
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
):
    """历史回看：查询指定日期的板块强度宽表。

    从 sector_daily 落库表读取（采集后台线程收盘后已写入），
    不在请求路径上同步调 CLI。库中无该日期数据时返回空列表。

    注意：本路由必须先于 /api/sectors/{code} 注册，否则 "history" 会被
    当作 code 参数被 {code} 路由拦截。
    """
    storage = get_storage()
    _ensure_meta()
    sector_list = load_sectors()
    meta_map = {m["code"]: m for m in storage.get_all_sector_meta()}

    if not sector_list:
        raise HTTPException(status_code=500, detail="no sector data")

    # date(YYYY-MM-DD) → YYYYMMDD
    try:
        asof_td = datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date format, expect YYYY-MM-DD")

    codes = [sec["code"] for sec in sector_list]
    db_map = storage.get_sector_daily_asof(codes, asof_td, n)

    rows: List[SectorRow] = []

    for sec in sector_list:
        code = sec["code"]
        db_records = db_map.get(code, [])
        # 落库记录缺 date(ISO) 字段，补上；保持 trade_date 倒序（最近在前）
        records = []
        for d in db_records[:n]:
            td = d.get("trade_date")
            records.append({
                "date": f"{td[:4]}-{td[4:6]}-{td[6:8]}" if td and len(td) == 8 else td,
                "trade_date": td,
                "net_flow": d.get("net_flow"),
                "turnover": d.get("turnover"),
                "estimated": False,
            })
        meta = meta_map.get(code, {})

        today_rec = records[0] if records else {}
        today_net = today_rec.get("net_flow")
        today_turnover = today_rec.get("turnover")

        circ_mv_yi = meta.get("circ_mv_yi")
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


@app.get("/api/sectors/{code}")
async def get_sector_detail(
    code: str,
    n: int = Query(STRENGTH_WINDOW_N, description="历史天数"),
):
    """单板块详情：近n日数据 + 强度判定"""
    storage = get_storage()

    # 概念板块：读后台 concept_flow 缓存（今日实时）+ concept_daily 落库历史，不在此同步调 CLI
    if code.startswith("pt02"):
        from concept_sectors import get_default_name
        from westock import extract_main_net_flow as _emnf
        with _concept_flow_lock:
            _cflow = _concept_flow_cache.get("flow_map", {}).get(code, {})
        if not _cflow:
            raise HTTPException(status_code=404, detail=f"concept sector {code} not found")
        flow = _cflow
        today_net = _emnf(flow)
        metrics = calc_sector_metrics(flow, TURNOVER_METHOD) if flow else {}
        today_turnover = metrics.get("turnover")
        today_str = date.today().isoformat()
        today_td = date.today().strftime("%Y%m%d")

        # 近 n 日记录：今日用实时 flow，历史读 concept_daily 落库表（trade_date 倒序）
        records = [{
            "date": today_str, "trade_date": today_td,
            "net_flow": today_net, "turnover": today_turnover,
            "estimated": False,
        }]
        cached_daily = storage.get_concept_daily_batch([code], days=n + 1).get(code, [])
        for d in cached_daily:
            td = d.get("trade_date")
            if td == today_td:
                continue  # 今日已用实时值，跳过落库里的今日重复
            iso = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if td and len(td) == 8 else td
            records.append({
                "date": iso, "trade_date": td,
                "net_flow": d.get("net_flow"), "turnover": d.get("turnover"),
                "estimated": False,
            })
        records = records[:n]

        strength = _calc_strength_from_records(records, None, n)
        history = _build_history(records, None, n)
        summary_3d = _build_summary(records, SUMMARY_3D, None)
        summary_5d = _build_summary(records, SUMMARY_5D, None)
        name = flow.get("name") or get_default_name(code)
        return {
            "code": code,
            "name": name,
            "l1": "概念",
            "circ_mv_yi": None,
            "scale": "小盘",
            "n_window": n,
            "records": history,
            "summary_3d": summary_3d,
            "summary_5d": summary_5d,
            "strength": strength,
        }

    # l2 板块：走缓存（采集后台线程已落库/入缓存），不在此同步调 CLI
    meta = storage.get_sector_meta(code)
    if not meta:
        raise HTTPException(status_code=404, detail=f"sector {code} not found")

    # 缓存未就绪：与宽表一致，返回 503 让前端重试，不在请求路径上同步拉外部接口
    if not is_ready():
        raise HTTPException(
            status_code=503,
            detail="data cache is still warming up, please retry in a few seconds",
        )

    records = get_daily(code)
    actual_n = min(n, get_max_n() or n)

    # 流通市值优先级：sector_circ_mv 缓存 → sector_meta
    circ_mv_yi = meta.get("circ_mv_yi")
    cached_mv = storage.get_sector_circ_mv(code)
    if cached_mv and cached_mv.get("circ_mv_yi"):
        circ_mv_yi = cached_mv["circ_mv_yi"]

    strength = _calc_strength_from_records(records, circ_mv_yi, actual_n)
    history = _build_history(records, None, actual_n)
    summary_3d = _build_summary(records, SUMMARY_3D, circ_mv_yi)
    summary_5d = _build_summary(records, SUMMARY_5D, circ_mv_yi)

    return {
        "code": code,
        "name": meta.get("name"),
        "l1": meta.get("l1"),
        "circ_mv_yi": circ_mv_yi,
        "scale": meta.get("scale"),
        "n_window": actual_n,
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
    # 采集由后台线程负责，API 只读库；数据库无分钟数据则返回空（前端展示暂无数据）
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


@app.get("/api/sector-daily-history")
async def get_sector_daily_history(
    codes: str = Query(..., description="板块代码，逗号分隔（如 pt01801081,pt02003800）"),
    days: int = Query(30, ge=1, le=60, description="近 N 交易日"),
):
    """板块日级净流入折线图数据源（读取 sector_daily 表，采集线程落库）。

    Args:
        codes: 板块代码列表（逗号分隔）
        days: 近 N 交易日（默认 30，≤60）

    Returns:
        {"days": N, "series": [{code, name, points: [{trade_date, net_flow_yi, turnover_yi}, ...]}]}
        points 按 trade_date 升序（折线图从左到右）。
    """
    storage = get_storage()
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"days": days, "series": []}

    # 读取最近 days 个交易日记录（get_sector_daily_batch 按 trade_date 倒序）
    data_map = storage.get_sector_daily_batch(code_list, days=days)

    # 盘中：sector_daily 是收盘后落库，交易时段尚无今日记录；
    # 从 minute_snapshot 取今日最新累计净流入/成交额，作为日线图的「今日」点。
    today_td = date.today().strftime("%Y%m%d")
    today_map = storage.get_latest_minute_snapshot_by_date(code_list, today_td)

    series = []
    for code in code_list:
        recs = data_map.get(code, [])
        # 倒序 → 升序，供折线图从左到右
        recs_sorted = sorted(recs, key=lambda r: r["trade_date"])
        points = [{
            "trade_date": r["trade_date"],
            "net_flow_yi": _to_yi(r.get("net_flow")),
            "turnover_yi": _to_yi(r.get("turnover")),
        } for r in recs_sorted]
        # 今日尚未落库且分钟快照有今日数据时，补上今日点（盘中动态更新）
        has_today = any(r["trade_date"] == today_td for r in recs_sorted)
        if not has_today:
            snap = today_map.get(code)
            if snap and snap.get("main_net_flow") is not None:
                points.append({
                    "trade_date": today_td,
                    "net_flow_yi": _to_yi(snap.get("main_net_flow")),
                    "turnover_yi": _to_yi(snap.get("turnover")),
                })
        name = recs[0].get("name") if recs else code
        series.append({
            "code": code,
            "name": name,
            "points": points,
            "point_count": len(points),
        })

    return {"days": days, "series": series}


@app.get("/api/minute/compare")
async def get_minute_compare(
    method: str = Query("rank", description="排序方式: rank=按净流入 / net_rate=按净额率 / fund_strength=按资金强度 / code=按板块编号 / manual=手动指定"),
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

    # 手动指定模式：支持代码精确匹配 + 名称模糊匹配（跨二级+概念清单，大小写不敏感）
    if method == "manual" and codes:
        tokens = [c.strip() for c in codes.split(",") if c.strip()]
        # 合并二级 + 概念 的 code→name，用于代码精确/名称模糊匹配，也用于后续 name 显示
        from sectors import get_default_sector_map as _l2_map
        from concept_sectors import get_concept_sectors as _concept_map
        merged_name = {}
        for c, info in _l2_map().items():
            merged_name[c] = info.get("name") if isinstance(info, dict) else str(info)
        for c, n in _concept_map().items():
            merged_name.setdefault(c, n)

        selected = []
        for tok in tokens:
            if tok in merged_name:
                # 精确代码
                if tok not in selected:
                    selected.append(tok)
                continue
            # 名称精确匹配（大小写不敏感）优先，避免英文子串误伤（如 CRO 误带 MicroLED）
            tlow = tok.lower()
            exact = [c for c, n in merged_name.items()
                     if n and str(n).lower() == tlow]
            if exact:
                for c in exact:
                    if c not in selected:
                        selected.append(c)
                continue
            # 仅中文 token 才做包含匹配（中文无子串误伤问题，且支持「半导体」匹配到
            # 「半导体产业」等衍生板块）；英文 token 不做包含匹配，只精确匹配
            if any("\u4e00" <= ch <= "\u9fff" for ch in tok):
                for c, name in merged_name.items():
                    if name and tlow in str(name).lower() and c not in selected:
                        selected.append(c)

        # 后续 series 组装用合并映射取 name，保证跨清单（l2/concept）也能正确显示名称
        sector_map = {c: {"name": n} for c, n in merged_name.items()}
    elif method in ("rank", "net_rate", "fund_strength"):
        # 按指标倒序取区间。
        # 今日：读实时缓存；历史日期：读落库表当日数据，保证 top N 板块随日期变动。
        # 指标：rank=净流入(元) / net_rate=净额率(%) = 净流入/成交额 / fund_strength=资金强度(%) = 净流入/流通市值
        today_td = date.today().strftime("%Y%m%d")
        is_today = (trade_date == today_td)

        def _sf(v):
            if v is None or v == "": return None
            try: return float(v)
            except (TypeError, ValueError): return None

        net_map: Dict[str, Optional[float]] = {}
        turnover_map: Dict[str, Optional[float]] = {}
        circ_mv_map: Dict[str, Optional[float]] = {}

        if source == "concept":
            if is_today:
                # 读后台 concept_flow 缓存（45s 刷新），不在此同步调 CLI
                with _concept_flow_lock:
                    flow_map = _concept_flow_cache.get("flow_map", {})
                for c in all_codes:
                    flow = flow_map.get(c, {})
                    net_map[c] = _sf(flow.get("MainNetFlow"))
                    m = calc_sector_metrics(flow, TURNOVER_METHOD) if flow else {}
                    turnover_map[c] = m.get("turnover")
                circ_detail = storage.get_latest_sector_circ_mv()
                for c, d in circ_detail.items():
                    circ_mv_map[c] = d.get("circ_mv_yi")
            else:
                daily_map = storage.get_concept_daily_asof(all_codes, trade_date, 1)
                for c in all_codes:
                    rec = (daily_map.get(c) or [{}])[0]
                    net_map[c] = rec.get("net_flow")
                    turnover_map[c] = rec.get("turnover")
                circ_detail = storage.get_all_sector_circ_mv(trade_date)
                for c, d in circ_detail.items():
                    circ_mv_map[c] = d.get("circ_mv_yi")
        else:
            if is_today:
                # 读 data_cache 日级数据（含 net_flow / turnover）；缓存未就绪时用 None
                daily_map = get_daily_map()
                for c in all_codes:
                    rec = (daily_map.get(c) or [{}])[0]
                    net_map[c] = rec.get("net_flow")
                    turnover_map[c] = rec.get("turnover")
                # circ_mv 统一读落库表（持久化），不依赖 data_cache 内存预热
                _cd = storage.get_latest_sector_circ_mv()
                for _c, _d in _cd.items():
                    circ_mv_map[_c] = _d.get("circ_mv_yi")
            else:
                daily_map = storage.get_sector_daily_asof(all_codes, trade_date, 1)
                for c in all_codes:
                    rec = (daily_map.get(c) or [{}])[0]
                    net_map[c] = rec.get("net_flow")
                    turnover_map[c] = rec.get("turnover")
                circ_detail = storage.get_all_sector_circ_mv(trade_date)
                for c, d in circ_detail.items():
                    circ_mv_map[c] = d.get("circ_mv_yi")

        def _sort_val(c):
            net = net_map.get(c)
            if method == "net_rate":
                tv = turnover_map.get(c)
                if net is None or not tv:
                    return None
                return net / tv * 100
            if method == "fund_strength":
                mv = circ_mv_map.get(c)
                if net is None or not mv:
                    return None
                return net / (mv * 1e8) * 100
            return net  # rank：按净流入(元)

        ranked = [(c, _sort_val(c)) for c in all_codes]
        # 无法计算(None)的排最后，其余按值降序
        ranked.sort(key=lambda x: (x[1] is None, -(x[1] or 0)))
        ordered_codes = [c for c, _ in ranked]
        selected = ordered_codes[start - 1:end]
    else:
        ordered_codes = sorted(all_codes)
        selected = ordered_codes[start - 1:end]

    # 批量拿分钟数据（采集由后台统一线程负责，API 只读不写）
    # 数据库无数据时返回空，前端展示"暂无数据"——不在此调 CLI 兜底
    deltas_map = storage.get_minute_deltas_batch(selected, trade_date)

    # 流通市值（亿元），用于前端展示「资金强度」曲线（净流入/流通市值）
    # 统一读落库表 sector_circ_mv（持久化），不依赖 data_cache 内存预热（重启后为空）
    circ_mv_series: Dict[str, Optional[float]] = {}
    if trade_date == date.today().strftime("%Y%m%d"):
        _cd = storage.get_latest_sector_circ_mv()
        for _c, _d in _cd.items():
            circ_mv_series[_c] = _d.get("circ_mv_yi")
    else:
        _cd = storage.get_all_sector_circ_mv(trade_date)
        for _c, _d in _cd.items():
            circ_mv_series[_c] = _d.get("circ_mv_yi")

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
            "circ_mv_yi": circ_mv_series.get(code),           # 流通市值(亿元)，资金强度展示用
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


@app.get("/api/sectors/concept/history", response_model=SectorListResponse)
async def get_concept_sectors_history(
    date: str = Query(..., description="YYYY-MM-DD，查询日期"),
    n: int = Query(STRENGTH_WINDOW_N, description="强度判定窗口"),
):
    """概念板块历史宽表：读 concept_daily + sector_circ_mv 落库表。

    不在请求路径同步调 CLI；库中无该日期数据时返回空列表。
    """
    storage = get_storage()
    from concept_sectors import get_default_codes, get_default_name

    codes = get_default_codes()
    if not codes:
        return SectorListResponse(
            date=date, last_update="", n_window=n, sectors=[], total=0,
        )

    try:
        asof_td = datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date format, expect YYYY-MM-DD")

    # 读 concept_daily 落库（近 n 日，截至 asof）用于净流入/强度/连续流入
    daily_map = storage.get_concept_daily_asof(codes, asof_td, n + 1)
    # 读 sector_circ_mv 落库（涨跌幅/换手率/流通市值，按 asof 当日）
    circ_mv_detail = storage.get_all_sector_circ_mv(asof_td)

    rows: List[SectorRow] = []
    for code in codes:
        db_records = daily_map.get(code, [])
        records = []
        for d in db_records[:n]:
            td = d.get("trade_date")
            records.append({
                "date": f"{td[:4]}-{td[4:6]}-{td[6:8]}" if td and len(td) == 8 else td,
                "trade_date": td,
                "net_flow": d.get("net_flow"),
                "turnover": d.get("turnover"),
                "estimated": False,
            })

        today_rec = records[0] if records else {}
        today_net = today_rec.get("net_flow")
        today_turnover = today_rec.get("turnover")

        circ_detail = circ_mv_detail.get(code, {})
        c_mv_yi = circ_detail.get("circ_mv_yi")
        c_change_pct = circ_detail.get("change_pct")
        c_turnover = circ_detail.get("turnover_rate")
        c_scale = get_scale(c_mv_yi) if c_mv_yi else "小盘"

        c_fund_strength = None
        if today_net is not None and c_mv_yi and c_mv_yi > 0:
            c_fund_strength = round(today_net / (c_mv_yi * 1e8) * 100, 4)

        c_consecutive = 0
        for _r in records:
            _nf = _r.get("net_flow")
            if _nf is not None and _nf > 0:
                c_consecutive += 1
            else:
                break

        c_divergence = (today_net is not None and today_net > 0
                        and c_change_pct is not None and c_change_pct < 0)

        strength = _calc_strength_from_records(records[:n], c_mv_yi, n)
        summary_3d = _build_summary(records[:SUMMARY_3D], SUMMARY_3D, c_mv_yi)
        summary_5d = _build_summary(records[:SUMMARY_5D], SUMMARY_5D, c_mv_yi)

        rows.append(SectorRow(
            code=code, name=get_default_name(code),
            l1="概念", circ_mv_yi=c_mv_yi, scale=c_scale,
            today_net_flow_yi=_to_yi(today_net),
            today_turnover_yi=_to_yi(today_turnover),
            today_net_rate=_net_rate(today_net, today_turnover),
            change_pct=c_change_pct,
            turnover_rate=c_turnover,
            fund_strength=c_fund_strength,
            consecutive_inflow_days=c_consecutive,
            divergence=c_divergence,
            history=[], summary_3d=summary_3d, summary_5d=summary_5d,
            strength_value=strength["value"], strength_level=strength["level"],
            estimated=not bool(db_records),
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
    user_id: Optional[str] = Depends(get_current_user),
):
    """获取强度档位变化告警日志，可按用户阈值筛选。

    告警在后台缓存刷新时自动检测，档位变化（如 普通→强）时写入。
    若用户已登录且设了阈值（strength_up/strength_down/levels），则
    仅返回符合阈值的告警；未登录或未设阈值则返回全部。
    """
    try:
        alerts = get_storage().get_alerts(trade_date=date, limit=limit)

        # 按用户阈值筛选
        if user_id:
            try:
                from supabase_user import get_user_alerts as _ga
                user_cfg = _ga(user_id) or {}
            except Exception:
                user_cfg = {}
            # levels 白名单：如 ["强","偏强"] 仅留这些档位的告警
            levels_whitelist = user_cfg.get("levels")
            # strength_up：仅留 new_value >= 该阈值的告警（如 1.0 = 偏强以上）
            up_threshold = user_cfg.get("strength_up")
            # strength_down：仅留 new_value <= 该阈值的告警（如 -1.0 = 偏弱以下）
            down_threshold = user_cfg.get("strength_down")
            # codes 白名单：仅留用户关注的板块（可配合 watchlist 用）
            codes_whitelist = user_cfg.get("codes")

            def _match(a):
                new_level = a.get("new_level")
                new_value = a.get("new_value")
                code = a.get("code")
                if levels_whitelist and new_level not in levels_whitelist:
                    return False
                if up_threshold is not None and (new_value is None or new_value < up_threshold):
                    return False
                if down_threshold is not None and (new_value is None or new_value > down_threshold):
                    return False
                if codes_whitelist and code not in codes_whitelist:
                    return False
                return True

            if any(v is not None for v in (levels_whitelist, up_threshold, down_threshold, codes_whitelist)):
                alerts = [a for a in alerts if _match(a)]

        return {
            "total": len(alerts),
            "alerts": alerts,
            "timestamp": datetime.now().isoformat(),
            "user_filter_applied": user_id is not None,
        }
    except Exception as e:
        logger.error("get_alerts failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 管理接口
# ============================================================
@app.post("/api/refresh-sectors")
async def api_refresh_sectors():
    """手动刷新板块列表，返回 diff（新增/剔除）"""
    try:
        result = refresh_sectors()  # 现返回 dict 含 sectors_count/added/removed/source
        return {"status": "ok", **result, "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error("refresh_sectors failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/refresh-concepts")
async def api_refresh_concepts(body: Dict[str, Any] = None):
    """从 westock search 反查关键词，把概念板块补全到白名单。

    body: {"keyword": "军工"}       —— 反查并真实写入白名单
          {"keyword": "军工", "dry_run": true} —— 只返回候选，不写库
          空 body                    —— 按内置高频词批量 dry-run（预览），不写库
    用于"概念板块可发现"：用户搜不到的概念板块，通过此接口反查补全。
    """
    from concept_sectors import search_and_merge_concepts, get_default_codes
    try:
        # body 可能为 None（无 body 调用）
        body = body or {}
        keyword = body.get("keyword", "").strip()
        dry_run = bool(body.get("dry_run", False))

        if keyword:
            result = search_and_merge_concepts(keyword, dry_run=dry_run)
            return {"status": "ok", "keyword": keyword, "dry_run": dry_run, **result,
                    "timestamp": datetime.now().isoformat()}
        # 空 body：批量 dry-run 预览（不写库），避免无确认的持久化副作用
        BATCH_KEYWORDS = ["ChatGPT", "军工", "新能源", "半导体", "AI",
                          "光伏", "储能", "氢能", "芯片", "数字经济"]
        merged_added = []
        before = len(get_default_codes())
        for kw in BATCH_KEYWORDS:
            r = search_and_merge_concepts(kw, dry_run=True)
            merged_added.extend(r["added"])
        return {"status": "ok", "keyword": "(batch-dry-run)", "dry_run": True,
                "added": merged_added,
                "total_before": before, "total_after": before + len(merged_added),
                "note": "dry-run 预览，未写库；要真正补全请传 keyword",
                "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error("refresh_concepts failed: %s", e, exc_info=True)
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

        # 启动后台线程，统一由守护器保护（异常退出自动重启）：
        #  - startup_init: 任务型（预热完成正常返回不重启，异常才重启）
        #  - collector: 常驻采集线程，退出即重启
        #  - concept_flow: 常驻概念 flow 缓存线程，退出即重启
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_startup",
                         args=(_bg_init, "startup_init", 10, False)).start()
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_collector",
                         args=(_collector_loop_entry, "collector", 10, True)).start()
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_concept_flow",
                         args=(_concept_flow_loop, "concept_flow", 10, True)).start()
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_circ_mv",
                         args=(_circ_mv_loop_entry, "circ_mv", 10, True)).start()
        logger.info("thread supervisors started (startup_init/collector/concept_flow/circ_mv)")

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

        # 启动后台线程，统一由守护器保护（异常退出自动重启）：
        #  - startup_init: 任务型（预热完成正常返回不重启，异常才重启）
        #  - collector: 常驻采集线程，退出即重启
        #  - concept_flow: 常驻概念 flow 缓存线程，退出即重启
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_startup",
                         args=(_bg_init, "startup_init", 10, False)).start()
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_collector",
                         args=(_collector_loop_entry, "collector", 10, True)).start()
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_concept_flow",
                         args=(_concept_flow_loop, "concept_flow", 10, True)).start()
        threading.Thread(target=_supervise_thread, daemon=True, name="sup_circ_mv",
                         args=(_circ_mv_loop_entry, "circ_mv", 10, True)).start()
        logger.info("thread supervisors started (startup_init/collector/concept_flow/circ_mv)")


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
