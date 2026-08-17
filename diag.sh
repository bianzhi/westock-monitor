#!/usr/bin/env bash
# ============================================================
# Westock Monitor 服务器诊断脚本
# 用法: ./diag.sh [--json]
# 覆盖: 进程 / systemd / API / 数据库 / 采集线程 / CLI / 前端
# ============================================================
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
VENV_PY="$PROJECT_DIR/.venv/bin/python3"
DB="$PROJECT_DIR/data/westock.db"
LOG="$PROJECT_DIR/logs/backend.log"
BASE_URL="${BASE_URL:-http://localhost:8200}"
JSON_MODE=false
[ "${1:-}" = "--json" ] && JSON_MODE=true

PASS=0; FAIL=0; WARN=0
declare -a PROBLEMS=()

_ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
_fail() { FAIL=$((FAIL+1)); PROBLEMS+=("$1 — $2"); echo "  ❌ $1 — $2"; }
_warn() { WARN=$((WARN+1)); PROBLEMS+=("⚠️ $1 — $2"); echo "  ⚠️ $1 — $2"; }
_info() { echo "  ℹ️  $1"; }

echo "========================================"
echo " Westock Monitor 诊断 $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# ----------------------------------------------------------
# 1. 进程 / systemd
# ----------------------------------------------------------
echo ""
echo "🟢 进程 / systemd"

# systemd 服务
if command -v systemctl &>/dev/null && systemctl is-active --quiet westock-monitor 2>/dev/null; then
    _ok "systemd 服务 westock-monitor 运行中"
    _info "  状态: $(systemctl is-active westock-monitor) / 开机自启: $(systemctl is-enabled westock-monitor 2>/dev/null || echo no)"
elif command -v systemctl &>/dev/null && systemctl is-active --quiet westock-monitor 2>/dev/null; then
    _ok "systemd 服务存在"
else
    # 无 systemd 服务 → 检查 nohup 进程
    if pgrep -f "uvicorn app:app" >/dev/null 2>&1; then
        PID=$(pgrep -f "uvicorn app:app" | head -1)
        START=$(ps -o lstart= -p "$PID" 2>/dev/null | tr -s ' ')
        _warn "非 systemd 模式（nohup 进程 PID=$PID，启动于 $START）" "重启服务器后不会自动拉起，建议 ./dev.sh --prod"
    else
        _fail "后端进程" "systemd 服务未运行且无 uvicorn 进程"
    fi
fi

# 端口监听
if curl -sf "$BASE_URL/api/health" >/dev/null 2>&1; then
    _ok "端口 8200 API 响应正常"
else
    _fail "端口 8200" "health 接口无响应"
fi

# ----------------------------------------------------------
# 2. API 健康
# ----------------------------------------------------------
echo ""
echo "📡 API 健康"

HEALTH=$(curl -sf "$BASE_URL/api/health" 2>/dev/null)

if [ -z "$HEALTH" ]; then
    _fail "health 接口" "无响应"
else
    CR=$(echo "$HEALTH" | $VENV_PY -c "import sys,json;d=json.load(sys.stdin);print(d.get('cache_ready'))" 2>/dev/null)
    if [ "$CR" = "True" ]; then
        _ok "cache_ready=True（数据缓存就绪）"
    else
        _fail "cache_ready=$CR" "缓存未就绪 → 前端会预热重试，查 init_cache 日志"
    fi
    # 数据源活性
    echo "$HEALTH" | $VENV_PY -c "
import sys,json
d=json.load(sys.stdin)
for k,v in (d.get('data_sources') or {}).items():
    status=v.get('status','?')
    mark='OK' if status=='ok' else ('WARN' if status in ('degraded','skip') else 'FAIL')
    print(f'  [{mark}] {k}: {status}' + (f' v={v.get(\"version\",\"\")}' if v.get('version') else '') + (f' — {v.get(\"note\",\"\")}' if v.get('note') else ''))
" 2>/dev/null
fi

# ----------------------------------------------------------
# 3. 采集线程
# ----------------------------------------------------------
echo ""
echo "🔧 采集线程"

if [ -f "$LOG" ]; then
    if grep -q "collector loop started" "$LOG"; then
        _ok "collector loop 已启动"
    else
        _fail "collector loop" "日志中未找到启动记录"
    fi
    if grep -q "concept flow cache loop started" "$LOG"; then
        _ok "concept flow 后台缓存线程已启动"
    else
        _fail "concept flow 线程" "未启动（概念板块宽表将退化为请求时同步拉取）"
    fi
    # 最近业务错误：排除 uvicorn/h11 的"非法请求行"（公网扫描器探测端口，
    # 非业务 bug，天天有）；也排除进程关闭时的 ThreadPoolExecutor 竞态
    # （"cannot schedule new futures after interpreter shutdown"，进程重启瞬间
    #  采集线程还在提交任务的噪音，不影响正常运行期数据更新）
    RECENT_ERR=$(grep -E "ERROR|Traceback|NameError" "$LOG" \
        | grep -vE "h11|uvicorn.error|Invalid HTTP request|Traceback \(most recent call last\)|interpreter shutdown|cannot schedule new futures" | tail -3)
    if [ -n "$RECENT_ERR" ]; then
        _warn "最近业务日志有异常" "$(echo "$RECENT_ERR" | head -1)"
    else
        _ok "最近业务日志无 ERROR/Traceback（仅外部扫描噪音不计）"
    fi
    # init_cache 状态
    if grep -q "preloaded, .* codes ready" "$LOG"; then
        _ok "init_cache 预加载完成"
    elif grep -q "preload FAILED\|all .* attempts FAILED" "$LOG"; then
        _fail "init_cache" "预加载失败，查 westock CLI"
    fi
else
    _fail "日志文件" "$LOG 不存在"
fi

# ----------------------------------------------------------
# 4. 数据库
# ----------------------------------------------------------
echo ""
echo "🗄️  数据库"

if [ -f "$DB" ]; then
    for t in sector_meta minute_snapshot concept_daily alert_log; do
        if sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='$t'" 2>/dev/null | grep -q .; then
            _ok "表 $t 存在"
        else
            _fail "表 $t" "不存在"
        fi
    done
    _info "sector_meta: $(sqlite3 "$DB" 'SELECT COUNT(*) FROM sector_meta' 2>/dev/null) 行"
    _info "minute_snapshot: $(sqlite3 "$DB" 'SELECT COUNT(*) FROM minute_snapshot' 2>/dev/null) 行 (今天 $(sqlite3 "$DB" "SELECT COUNT(*) FROM minute_snapshot WHERE trade_date='$(date +%Y%m%d)'" 2>/dev/null))"
    _info "concept_daily: $(sqlite3 "$DB" 'SELECT COUNT(*) FROM concept_daily' 2>/dev/null) 行"
    # 采集活性：交易时段内最新分钟数据距今 >5 分钟 = 数据停采（收盘后停采正常）
    _GAP_MIN=$($VENV_PY -c "
import sqlite3, datetime
c = sqlite3.connect('$DB')
r = c.execute('SELECT MAX(timestamp) FROM minute_snapshot WHERE trade_date=?', (datetime.date.today().strftime('%Y%m%d'),)).fetchone()[0]
c.close()
print(int((datetime.datetime.now() - datetime.datetime.fromisoformat(r)).total_seconds()/60) if r else 99999)
" 2>/dev/null)
    _HM=$(( $(date +%H) * 60 + $(date +%M) ))
    if [ -n "$_GAP_MIN" ] && [ "$_GAP_MIN" != "99999" ]; then
        if { [ "$_HM" -ge 570 ] && [ "$_HM" -le 690 ]; } || { [ "$_HM" -ge 780 ] && [ "$_HM" -le 900 ]; }; then
            if [ "$_GAP_MIN" -gt 5 ]; then
                _fail "采集活性" "交易时段内数据停采：最新数据距今 ${_GAP_MIN} 分钟"
            else
                _ok "采集活性正常（距今 ${_GAP_MIN} 分钟）"
            fi
        else
            _info "非交易时段，采集停采正常（最新距今 ${_GAP_MIN} 分钟）"
        fi
    fi
else
    _fail "数据库文件" "$DB 不存在"
fi

# ----------------------------------------------------------
# 5. westock CLI
# ----------------------------------------------------------
echo ""
echo "⚙️  westock CLI"

# 全局安装检测：多路径探测（nohup/systemd 环境 PATH 可能不含 npm 全局 bin，
# 用 command -v 会误判；bin 名为 westock-data-skillhub，与 dev.sh 探测一致）
_NPM_BIN=""
_WBIN_NAME="westock-data-skillhub"
_NPM_BIN_DIR=$(npm bin -g 2>/dev/null || true)
if [ -n "$_NPM_BIN_DIR" ] && [ -x "$_NPM_BIN_DIR/$_WBIN_NAME" ]; then
    _NPM_BIN="$_NPM_BIN_DIR"
fi
if [ -z "$_NPM_BIN" ]; then
    for _cand in "$(npm prefix -g 2>/dev/null)/bin" /usr/local/bin /usr/bin; do
        [ -x "$_cand/$_WBIN_NAME" ] && _NPM_BIN="$_cand" && break
    done
fi
if [ -n "$_NPM_BIN" ]; then
    _ok "westock-data-skillhub 已全局安装（$_NPM_BIN，跳过 npx 开销）"
else
    _warn "westock-data-skillhub 未全局安装" "走 npx 每次解析包，慢且易超时；./dev.sh 会自动安装，或手动 npm install -g westock-data-skillhub@1.0.5"
fi

# CLI 探活：复用 westock.py 的 _ping_cli（与 health 的 westock_cli 检测完全一致），
# 避免 diag.sh 自己用全局 bin --version 与 westock.py 实际调用方式（全局 bin 或 npx）
# 不一致导致的误报。
_CLI_STATUS=$($VENV_PY -c "from westock import _ping_cli; ok, ver = _ping_cli(); print('ok' if ok else 'fail')" 2>/dev/null)
if [ "$_CLI_STATUS" = "ok" ]; then
    _ok "westock CLI 可执行（_ping_cli 与 health 一致）"
else
    _fail "westock CLI" "_ping_cli 探测失败，fund_flow 将不可用"
fi

# ----------------------------------------------------------
# 6. 前端
# ----------------------------------------------------------
echo ""
echo "🎨 前端"

if [ -d "$PROJECT_DIR/frontend/dist" ]; then
    _ok "前端 dist 已构建"
    if curl -sf "$BASE_URL/" >/dev/null 2>&1; then
        _ok "前端页面可访问"
    else
        _fail "前端页面" "根路径无响应"
    fi
else
    _fail "前端 dist" "未构建，执行 cd frontend && npm run build"
fi

# ----------------------------------------------------------
# 总结
# ----------------------------------------------------------
echo ""
echo "========================================"
echo " 结果: $PASS 通过 / $WARN 警告 / $FAIL 失败"
if [ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]; then
    echo " ✅ 全部正常"
elif [ "$FAIL" -eq 0 ]; then
    echo " ⚠️ 有 $WARN 项警告（可运行，但建议处理）"
else
    echo " ❌ 有 $FAIL 项失败，需处理"
    printf '  - %s\n' "${PROBLEMS[@]}"
fi
echo "========================================"
exit $FAIL
