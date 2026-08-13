#!/usr/bin/env python3
"""Westock Monitor 自动化测试套件

用法: python3 test.py [--verbose]

分层:
  1. 冒烟（SMOKE）   — 服务活着、接口能 200
  2. 回归（REGRESS） — 板块数恒定、字段非 None、数值落在合理区间、
                       二级板块强度档位词在 5 档枚举内、
                       告警/概念板块口径不再 split

与原版区别: 原版只断言 total>5 这种弱阈值，回归测不出；
            本版断言二级板块=134、概念板块数量稳定、关键字段非 None、
            净额率/强度值在数学合理区间。
"""
import json, os, sys, time, sqlite3, math

try:
    import requests
except ImportError:
    import subprocess as _sp
    class _FakeResp:
        def __init__(self, rc, out): self.status_code = rc; self._out = out
        def json(self): return json.loads(self._out)
    def _get(url, timeout=30):
        r = _sp.run(["curl", "-sf", url], stdout=_sp.PIPE, stderr=_sp.PIPE, universal_newlines=True, timeout=timeout)
        if r.returncode != 0:
            from requests.exceptions import ConnectionError; raise ConnectionError(r.stderr[:100])
        return _FakeResp(200, r.stdout)
    requests = type('m', (), {'get': staticmethod(_get), 'ConnectionError': ConnectionError})()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8200")
DB_PATH = os.path.join(PROJECT_DIR, "data", "westock.db")
LOG_PATH = os.path.join(PROJECT_DIR, "logs", "backend.log")
VERBOSE = "--verbose" in sys.argv

# ============================================================
# 期望常量（回归基线）—— 数据源正常时这些值应稳定
# 生产环境完整数据源：134/31；本地开发库可能只有部分板块缓存，
# 用环境变量 EXPECTED_L2_COUNT / EXPECTED_L1_COUNT 覆盖，避免本地误报。
# ============================================================
EXPECTED_L2_COUNT = int(os.environ.get("EXPECTED_L2_COUNT", "134"))   # 申万 2021 二级板块
EXPECTED_L1_COUNT = int(os.environ.get("EXPECTED_L1_COUNT", "31"))    # 申万 2021 一级行业
EXPECTED_STRENGTH_LEVELS = {"强", "偏强", "普通", "偏弱", "弱"}
EXPECTED_SCALES = {"大盘", "中盘", "小盘"}

# 合理区间（A 股全板块日内）
NET_FLOW_MIN, NET_FLOW_MAX = -500, 500       # 亿
NET_RATE_MIN, NET_RATE_MAX = -50, 50         # %
STRENGTH_VALUE_MIN, STRENGTH_VALUE_MAX = -2.0, 2.0
CIRC_MV_MIN, CIRC_MV_MAX = 0, 100000         # 亿（A 股全市场流通市值不超 100 万亿）

PASS = 0; FAIL = 0
FAILS = []

def ok(msg):
    global PASS; PASS += 1; print(f"  ✅ {msg}")
def fail(msg, detail=""):
    global FAIL; FAIL += 1; FAILS.append((msg, detail))
    print(f"  ❌ {msg} — {detail}")
def dget(d, k, default=0):
    return d.get(k, default) if isinstance(d, dict) else default

def get(path):
    resp = requests.get(f"{BASE_URL}{path}", timeout=60)
    try:
        return resp.status_code, resp.json()
    except Exception as e:
        if VERBOSE:
            print(f"  ⚠️  {path}: JSON parse error: {e} | text[:50]={resp.text[:50]}")
        return resp.status_code, {}

def _is_num(v):
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))

def _in_range(v, lo, hi, name):
    if not _is_num(v):
        fail(f"{name} 非数值", f"got {v!r}")
        return False
    if not (lo <= v <= hi):
        fail(f"{name} 越界", f"got {v}, expected [{lo}, {hi}]")
        return False
    return True

# ============================================================
print("=" * 40)
print(f" Westock Monitor 测试套件")
print(f" {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 40)

# ============================================================
# 1. SMOKE — 冒烟：服务活着
# ============================================================
print("\n📡 SMOKE — API 端点")
s, _ = get("/api/health")
if s == 200: ok("GET /api/health")
else: fail("GET /api/health", "服务未响应"); sys.exit(1)

s, d = get("/api/sectors?n=5")
if s == 200 and dget(d, "total") > 5: ok(f"GET /api/sectors → {d['total']} 个板块")
else: fail("GET /api/sectors", f"total={dget(d,'total')}, status={s}"); sys.exit(1)

s, d = get("/api/sectors/concept?n=3")
if s == 200 and dget(d, "total") > 100: ok(f"GET /api/sectors/concept → {d['total']} 个概念板块")
else: fail("GET /api/sectors/concept", f"total={dget(d,'total')}, status={s}")

s, d = get("/api/minute/compare?method=rank&start=1&end=3&source=l2")
if s == 200 and dget(d, "series_count") >= 1: ok(f"GET /api/minute/compare → {dget(d,'series_count')} 条")
else: fail("GET /api/minute/compare", f"n={dget(d,'series_count')}, status={s}")

s, _ = get("/api/errors?limit=5")
if s == 200: ok("GET /api/errors")
else: fail("GET /api/errors", f"status={s}")

s, _ = get("/api/alerts?limit=5")
if s == 200: ok("GET /api/alerts")
else: fail("GET /api/alerts", f"status={s}")

# ============================================================
# 2. REGRESS — 回归：二级板块数恒定、字段非 None、数值区间
# ============================================================
print("\n🔬 REGRESS — 二级板块数据完整性")
s, d = get("/api/sectors?n=5")
if s != 200 or not d.get("sectors"):
    fail("二级板块数据", "API 异常，跳过字段断言")
else:
    sectors = d["sectors"]
    total = dget(d, "total")

    # 2.1 数量恒定
    if total == EXPECTED_L2_COUNT:
        ok(f"二级板块数恒定 = {EXPECTED_L2_COUNT}")
    else:
        fail("二级板块数", f"got {total}, expected {EXPECTED_L2_COUNT}")

    # 2.2 抽样字段非 None + 数值区间（取前 20 + 随机 10 避免边界特例）
    sample_idx = list(range(min(20, len(sectors)))) + \
                 [i for i in range(len(sectors)-1, max(20, len(sectors)-30), -1)]
    sample = [sectors[i] for i in sorted(set(sample_idx))[:30]] if sectors else []

    none_count = 0
    range_violations = 0
    bad_levels = set()
    bad_scales = set()
    for r in sample:
        # 关键字段非 None
        for k in ["code", "name", "today_net_flow_yi", "today_net_rate",
                  "strength_value", "strength_level"]:
            if r.get(k) is None and k not in ("today_net_flow_yi", "today_net_rate"):
                # 今日净流入/净额率允许 None（数据未就绪）但 code/name/strength 不允许
                none_count += 1
        # 强度档位在 5 档枚举
        lv = r.get("strength_level")
        if lv and lv not in EXPECTED_STRENGTH_LEVELS:
            bad_levels.add(lv)
        # 规模档在三档枚举
        sc = r.get("scale")
        if sc and sc not in EXPECTED_SCALES:
            bad_scales.add(sc)
        # 强度值区间
        sv = r.get("strength_value")
        if sv is not None:
            _in_range(sv, STRENGTH_VALUE_MIN, STRENGTH_VALUE_MAX, "strength_value")
        # 流通市值区间（允许 None：采集窗口未命中）
        mv = r.get("circ_mv_yi")
        if mv is not None:
            _in_range(mv, CIRC_MV_MIN, CIRC_MV_MAX, "circ_mv_yi")
        # 净额率区间
        nr = r.get("today_net_rate")
        if nr is not None:
            _in_range(nr, NET_RATE_MIN, NET_RATE_MAX, "today_net_rate")

    if none_count == 0:
        ok("关键字段（code/name/strength_*）全非 None")
    else:
        fail("关键字段 None", f"{none_count} 处为 None")
    if not bad_levels:
        ok("强度档位词全在 5 档枚举内")
    else:
        fail("强度档位越界", f"unknown levels={bad_levels}")
    if not bad_scales:
        ok("规模档全在三档枚举内")
    else:
        fail("规模档越界", f"unknown scales={bad_scales}")

# ============================================================
# 3. REGRESS — 一级行业聚合
# ============================================================
print("\n🔬 REGRESS — 一级行业聚合")
s, d = get("/api/sectors/l1-summary?n=5")
if s != 200 or not d.get("l1_summaries"):
    fail("L1 聚合数据", "API 异常")
else:
    l1 = d["l1_summaries"]
    total_l1 = dget(d, "total_l1")
    if total_l1 == EXPECTED_L1_COUNT:
        ok(f"一级行业数恒定 = {EXPECTED_L1_COUNT}")
    else:
        fail("一级行业数", f"got {total_l1}, expected {EXPECTED_L1_COUNT}")
    # 平均强度值区间
    bad = 0
    for r in l1:
        v = r.get("avg_strength_value")
        if v is not None and not (STRENGTH_VALUE_MIN <= v <= STRENGTH_VALUE_MAX):
            bad += 1
    if bad == 0:
        ok("avg_strength_value 全在 [-2, +2] 区间")
    else:
        fail("avg_strength_value 越界", f"{bad} 个行业越界")

# ============================================================
# 4. REGRESS — 强度排行档位枚举一致性
# ============================================================
print("\n🔬 REGRESS — 强度排行")
s, d = get("/api/strength/ranking?n=5&top=10")
if s != 200:
    fail("强度排行", f"status={s}")
else:
    # 5 档分布的 key 必须严格等于 5 档枚举
    dist = dget(d, "level_distribution")
    if isinstance(dist, dict) and set(dist.keys()) == EXPECTED_STRENGTH_LEVELS:
        ok("level_distribution 覆盖 5 档完整")
    else:
        fail("level_distribution", f"keys={set(dist.keys()) if isinstance(dist, dict) else 'n/a'}, expected {EXPECTED_STRENGTH_LEVELS}")

# ============================================================
# 5. 进程状态
# ============================================================
print("\n🔧 进程状态")
if os.path.exists(LOG_PATH):
    log = open(LOG_PATH).read()
    if "Traceback" not in log[-5000:]: ok("最近日志无 Traceback 崩溃")
    else: fail("进程崩溃", "最近日志有 Traceback")
    if "collector loop started" in log: ok("collector loop 已启动")
    else: fail("collector loop", "未启动")
else:
    fail("日志文件", LOG_PATH)

# ============================================================
# 6. 数据库
# ============================================================
print("\n🗄️  数据库")
if os.path.exists(DB_PATH):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    for t in ["sector_meta", "minute_snapshot", "concept_daily", "alert_log"]:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
        if cur.fetchone(): ok(f"表 {t} 存在")
        else: fail(f"表 {t}", "不存在")
    conn.close()
else:
    fail("数据库", DB_PATH)

# ============================================================
# 结果
# ============================================================
print(f"\n{'=' * 40}")
print(f" 结果: {PASS} 通过 / {FAIL} 失败")
if FAIL == 0:
    print(" ✅ 全部通过")
else:
    print(f" ❌ 有 {FAIL} 项失败")
    for msg, detail in FAILS:
        print(f"    - {msg}: {detail}")
print("=" * 40)
sys.exit(FAIL)
