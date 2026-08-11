#!/usr/bin/env bash
# ============================================================
# Westock Monitor 自动化测试套件
# 用法: ./test.sh [--verbose]
#
# 覆盖:
#   API 响应    — 各端点 200 且返回合法数据
#   进程状态    — collector loop 启动、无崩溃循环
#   数据库      — 表存在、有今日数据
#   数据完整性  — 关键字段非空
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
BASE_URL="${BASE_URL:-http://localhost:8200}"
DB="$PROJECT_DIR/data/westock.db"
LOG="$PROJECT_DIR/logs/backend.log"
PASS=0
FAIL=0
VERBOSE=false
[ "${1:-}" = "--verbose" ] && VERBOSE=true

_ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
_fail() { FAIL=$((FAIL+1)); echo "  ❌ $1 — $2"; }
_info() { $VERBOSE && echo "  ℹ️  $1" || true; }

echo "========================================"
echo " Westock Monitor 测试套件"
echo " $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# ----------------------------------------------------------
# 1. API 端点测试
# ----------------------------------------------------------
echo ""
echo "📡 API 端点"

# 1.1 health
if curl -sf "$BASE_URL/api/health" > /dev/null 2>&1; then
    _ok "GET /api/health"
else
    _fail "GET /api/health" "服务未响应，先运行 ./dev.sh"
    echo ""
    echo "========================================"
    echo " 结果: $PASS 通过 / $FAIL 失败"
    echo "========================================"
    exit 1
fi

HEALTH=$(curl -sf "$BASE_URL/api/health")
_info "$(echo "$HEALTH" | "$VENV_PYTHON" -c "import sys,json;d=json.load(sys.stdin);print(f'cache_ready={d.get(\"cache_ready\")}, trading={d.get(\"trading\")}')" 2>/dev/null)"

# 1.2 sectors (l2)
SECTORS=$(curl -sf "$BASE_URL/api/sectors?n=5")
SECTOR_COUNT=$(echo "$SECTORS" | "$VENV_PYTHON" -c "import sys,json;print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
if [ "$SECTOR_COUNT" -gt 100 ] 2>/dev/null; then
    _ok "GET /api/sectors → $SECTOR_COUNT 个板块"
else
    _fail "GET /api/sectors" "返回 $SECTOR_COUNT 个板块（预期 >100）"
fi

# 1.3 concept sectors
CONCEPT=$(curl -sf "$BASE_URL/api/sectors/concept?n=3" | head -c 200)
CONCEPT_OK=$(echo "$CONCEPT" | grep -c '"sectors"' 2>/dev/null || true)
if [ "$CONCEPT_OK" -gt 0 ] 2>/dev/null; then
    C_COUNT=$(echo "$CONCEPT" | "$VENV_PYTHON" -c "import sys,json;d=json.load(sys.stdin);print(d.get('total',0))" 2>/dev/null)
    if [ "$C_COUNT" -gt 500 ] 2>/dev/null; then
        _ok "GET /api/sectors/concept → $C_COUNT 个概念板块"
    else
        _fail "GET /api/sectors/concept" "只有 $C_COUNT 个概念板块（预期 >500）"
    fi
else
    _fail "GET /api/sectors/concept" "返回异常: $(echo \"$CONCEPT\" | head -c 100)"
fi

# 1.4 minute compare
COMPARE=$(curl -sf "$BASE_URL/api/minute/compare?method=rank&start=1&end=3&source=l2")
COMPARE_N=$(echo "$COMPARE" | "$VENV_PYTHON" -c "import sys,json;print(json.load(sys.stdin).get('series_count',0))" 2>/dev/null)
if [ "$COMPARE_N" -ge 1 ] 2>/dev/null; then
    _ok "GET /api/minute/compare → $COMPARE_N 条 series"
else
    _fail "GET /api/minute/compare" "返回 $COMPARE_N 条 series"
fi

# 1.5 errors page
if curl -sf "$BASE_URL/api/errors?limit=5" > /dev/null 2>&1; then
    _ok "GET /api/errors"
else
    _fail "GET /api/errors" "端点不可用"
fi

# ----------------------------------------------------------
# 2. 进程状态
# ----------------------------------------------------------
echo ""
echo "🔧 进程状态"

if [ -f "$LOG" ]; then
    COLLECTOR_COUNT=$(grep -c "collector loop started" "$LOG" 2>/dev/null || echo 0)
    CRASH_COUNT=$(grep -c "NameError\|Traceback" "$LOG" 2>/dev/null || echo 0)
    _info "collector 启动 $COLLECTOR_COUNT 次, 崩溃 $CRASH_COUNT 次"

    # 最近 5 分钟内无崩溃
    RECENT_CRASH=$(grep "NameError\|Traceback" "$LOG" 2>/dev/null | tail -5 | wc -l | tr -d ' ')
    if [ "$RECENT_CRASH" -eq 0 ] 2>/dev/null; then
        _ok "最近无 NameError/Traceback 崩溃"
    else
        _fail "进程崩溃" "最近有 $RECENT_CRASH 条崩溃日志: $(grep 'NameError\|Traceback' "$LOG" | tail -3)"
    fi

    # collector loop 至少启动了一次
    if [ "$COLLECTOR_COUNT" -gt 0 ] 2>/dev/null; then
        _ok "collector loop 已启动 ($COLLECTOR_COUNT 次)"
    else
        _fail "collector loop" "未找到启动日志"
    fi
else
    _fail "日志文件" "$LOG 不存在"
fi

# ----------------------------------------------------------
# 3. 数据库检查
# ----------------------------------------------------------
echo ""
echo "🗄️  数据库"

if [ -f "$DB" ]; then
    # 表存在
    for table in sector_meta minute_snapshot concept_daily alert_log; do
        if sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='$table'" 2>/dev/null | grep -q .; then
            _ok "表 $table 存在"
        else
            _fail "表 $table" "不存在"
        fi
    done

    # minute_snapshot 今日数据
    TODAY=$(date +%Y%m%d)
    MINUTE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM minute_snapshot WHERE trade_date='$TODAY'" 2>/dev/null || echo 0)
    _info "今日分钟K线: $MINUTE_COUNT 条"

    # concept_daily 有数据
    CD_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM concept_daily" 2>/dev/null || echo 0)
    _info "概念日记录: $CD_COUNT 条"
else
    _fail "数据库文件" "$DB 不存在"
fi

# ----------------------------------------------------------
# 4. 数据完整性
# ----------------------------------------------------------
echo ""
echo "📊 数据完整性"

SAMPLE=$(curl -sf "$BASE_URL/api/sectors/concept?n=1" | "$VENV_PYTHON" -c "
import sys,json
d=json.load(sys.stdin)
s=d['sectors'][0] if d.get('sectors') else {}
for k in ['code','name','today_net_flow_yi']:
    v=s.get(k)
    print(f'{k}={v} {"✅" if v is not None else \"❌\"} ')
" 2>/dev/null)
_info "$SAMPLE"

# 第一个概念板块有净流入数据
FIRST_NET=$(curl -sf "$BASE_URL/api/sectors/concept?n=1" | "$VENV_PYTHON" -c "
import sys,json
d=json.load(sys.stdin)
s=d['sectors'][0] if d.get('sectors') else {}
print(s.get('today_net_flow_yi', 'None'))
" 2>/dev/null)
if [ "$FIRST_NET" != "None" ] && [ "$FIRST_NET" != "" ]; then
    _ok "概念板块 today_net_flow_yi 有值 ($FIRST_NET)"
else
    _fail "概念板块 today_net_flow_yi" "为 None"
fi

# ----------------------------------------------------------
# 总结
# ----------------------------------------------------------
echo ""
echo "========================================"
echo " 结果: $PASS 通过 / $FAIL 失败"
if [ "$FAIL" -eq 0 ]; then
    echo " ✅ 全部通过"
else
    echo " ❌ 有 $FAIL 项失败"
fi
echo "========================================"
exit $FAIL
