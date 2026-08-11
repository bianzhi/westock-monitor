#!/usr/bin/env python3
"""Westock Monitor 自动化测试套件

用法: python3 test.py [--verbose]
覆盖: API 端点 / 进程健康 / 数据库 / 数据完整性
"""
import json, os, sys, time, sqlite3

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
PASS = 0; FAIL = 0

def ok(msg):  global PASS; PASS += 1; print(f"  ✅ {msg}")
def fail(msg, detail=""): global FAIL; FAIL += 1; print(f"  ❌ {msg} — {detail}")
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

# ============================================================
print("=" * 40)
print(f" Westock Monitor 测试套件")
print(f" {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 40)

# 1. API
print("\n📡 API 端点")
s, _ = get("/api/health")
if s == 200: ok("GET /api/health")
else: fail("GET /api/health", "服务未响应"); sys.exit(1)

s, d = get("/api/sectors?n=5")
if s == 200 and dget(d, "total") > 5: ok(f"GET /api/sectors → {d['total']} 个板块")
else: fail("GET /api/sectors", f"total={dget(d,'total')}, status={s}")

s, d = get("/api/sectors/concept?n=3")
if s == 200 and dget(d, "total") > 500: ok(f"GET /api/sectors/concept → {d['total']} 个概念板块")
else: fail("GET /api/sectors/concept", f"total={dget(d,'total')}, status={s}")

s, d = get("/api/minute/compare?method=rank&start=1&end=3&source=l2")
if s == 200 and dget(d, "series_count") >= 1: ok(f"GET /api/minute/compare → {dget(d,'series_count')} 条")
else: fail("GET /api/minute/compare", f"n={dget(d,'series_count')}, status={s}")

s, _ = get("/errors?limit=5")
if s == 200: ok("GET /api/errors")
else: fail("GET /api/errors", f"status={s}")

# 2. 进程
print("\n🔧 进程状态")
if os.path.exists(LOG_PATH):
    log = open(LOG_PATH).read()
    if "Traceback" not in log[-5000:]: ok("最近日志无 Traceback 崩溃")
    else: fail("进程崩溃", "最近日志有 Traceback")
    if "collector loop started" in log: ok("collector loop 已启动")
    else: fail("collector loop", "未启动")
else: fail("日志文件", LOG_PATH)

# 3. 数据库
print("\n🗄️  数据库")
if os.path.exists(DB_PATH):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    for t in ["sector_meta","minute_snapshot","concept_daily","alert_log"]:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(t,))
        if cur.fetchone(): ok(f"表 {t} 存在")
        else: fail(f"表 {t}", "不存在")
    conn.close()
else: fail("数据库", DB_PATH)

# 4. 数据
print("\n📊 数据完整性")
for label, path in [("概念板块", "/api/sectors/concept?n=1"), ("二级板块", "/api/sectors?n=1")]:
    s, d = get(path)
    if s == 200 and d.get("sectors"):
        v = d["sectors"][0].get("today_net_flow_yi")
        if v is not None: ok(f"{label} today_net_flow_yi 有值")
        else: fail(f"{label} today_net_flow_yi", "为 None")
    else: fail(f"{label} 数据", "API 异常")

print(f"\n{'=' * 40}")
print(f" 结果: {PASS} 通过 / {FAIL} 失败")
if FAIL == 0: print(" ✅ 全部通过")
else: print(f" ❌ 有 {FAIL} 项失败")
print("=" * 40)
sys.exit(FAIL)
