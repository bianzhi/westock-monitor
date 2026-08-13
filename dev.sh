#!/usr/bin/env bash
# ============================================================
# Westock Monitor 一键启动脚本（唯一入口）
# ============================================================
# 用法:
#   ./dev.sh                  # 开发模式：nohup 跑后端，不做 systemd 注册
#   ./dev.sh --prod           # 生产模式：注册 systemd 服务（需 root / sudo）
#   ./dev.sh --build          # 强制重新编译前端
#   ./dev.sh kill             # 停止服务（systemd 与 nohup 都清理）
#   ./dev.sh --help / -h      # 显示本帮助
#
# 默认开发模式与生产模式互斥；--prod 时才允许 cp service 文件到
# /etc/systemd/system/ 并 systemctl enable，避免开发调试意外注册
# 生产服务导致下次开机自启。
# ============================================================
set -e

_show_help() {
    sed -n '3,14p' "$0"
    exit 0
}

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
LOGDIR="$PROJECT_DIR/logs"
SERVICE_FILE="$PROJECT_DIR/westock-monitor.service"
USE_SYSTEMD=false
PROD_MODE=false

KILL_ONLY=false
FORCE_BUILD=false
for arg in "$@"; do
    case "$arg" in
        kill) KILL_ONLY=true ;;
        --build) FORCE_BUILD=true ;;
        --prod) PROD_MODE=true ;;
        --help|-h) _show_help ;;
        *) echo "⚠️  未知参数: $arg（./dev.sh --help 查看用法）" >&2; exit 2 ;;
    esac
done

# 检测 systemd
if command -v systemctl &> /dev/null && [ -d /etc/systemd/system ]; then
    USE_SYSTEMD=true
fi

# ----------------------------------------------------------
# 1. 杀掉旧服务（systemd 或进程）
# ----------------------------------------------------------
echo "🔪 检查并杀掉旧服务..."

_kill_port() {
    local port="$1"
    local pids=""
    # lsof 在某些服务器上会卡住，用 timeout 保护，失败则 fuser 兜底
    pids=$(timeout 3 lsof -ti ":$port" 2>/dev/null || true)
    if [ -z "$pids" ] && command -v fuser &>/dev/null; then
        pids=$(timeout 3 fuser "$port/tcp" 2>/dev/null | tr -d ' ' || true)
    fi
    if [ -z "$pids" ] && command -v ss &>/dev/null; then
        pids=$(timeout 3 ss -tlpn "sport = :$port" 2>/dev/null | grep -oP 'pid=\K\d+' | tr '\n' ' ' || true)
    fi
    [ -z "$pids" ] && return 1
    echo "   端口 $port → PID $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    return 0
}

# 1a. 先停 systemd（否则 Restart=always 会复活进程）
if $USE_SYSTEMD; then
    if systemctl is-active --quiet westock-monitor 2>/dev/null; then
        systemctl stop westock-monitor 2>/dev/null || true
    fi
fi

# 1b. 再杀端口进程（确保彻底清理）
_kill_port 8200 || true
_kill_port 5173 || true
for name in uvicorn vite; do
    pids=$(pgrep -f "$name" 2>/dev/null || true)
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
done

# 1c. 清理 systemd 注册
if $USE_SYSTEMD; then
    if systemctl is-enabled --quiet westock-monitor 2>/dev/null; then
        systemctl disable westock-monitor 2>/dev/null || true
    fi
fi
echo "✅ 旧服务已清理"

if $KILL_ONLY; then
    echo "done."
    exit 0
fi

# ----------------------------------------------------------
# 2. 检查并自动安装依赖
# ----------------------------------------------------------
echo ""
echo "🔍 检查环境..."

# 自动安装函数：依次尝试 yum / dnf / apt / brew
_auto_install() {
    local pkg="$1"
    for mgr in dnf yum apt brew; do
        if command -v "$mgr" &> /dev/null; then
            echo "   正在用 $mgr 安装 $pkg ..."
            $mgr install -y "$pkg" > /dev/null 2>&1 && return 0
        fi
    done
    return 1
}

if ! command -v node &> /dev/null; then
    echo "⚠️  未检测到 Node.js，尝试自动安装..."
    # Node.js 包名在不同发行版可能不同：nodejs / node
    if _auto_install "nodejs"; then
        echo "   ✅ Node.js 安装完成"
    elif _auto_install "node"; then
        echo "   ✅ Node.js 安装完成"
    else
        echo "   ❌ 自动安装失败，请手动安装 Node.js ≥ 18"
        echo "   CentOS/RHEL: curl -fsSL https://rpm.nodesource.com/setup_18.x | bash - && yum install -y nodejs"
        echo "   Ubuntu/Debian: curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && apt install -y nodejs"
        exit 1
    fi
fi

if ! command -v python3 &> /dev/null; then
    echo "⚠️  未检测到 python3，尝试自动安装..."
    if _auto_install "python3"; then
        echo "   ✅ Python3 安装完成"
    else
        echo "   ❌ 自动安装失败，请手动安装 Python ≥ 3.8"
        exit 1
    fi
fi

# 检查 Python 版本：千人千面功能需要 ≥3.8
PYTHON_BIN="python3"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]); then
    echo "⚠️  Python $PY_VER < 3.8，尝试安装 python38 ..."
    if _auto_install "python38"; then
        PYTHON_BIN="python3.8"
        # 同时安装开发包（pip 编译依赖）
        for mgr in dnf yum apt; do
            command -v "$mgr" &> /dev/null && $mgr install -y python38-devel > /dev/null 2>&1 && break
        done 2>/dev/null
        echo "   ✅ python3.8 安装完成（千人千面可用）"
    elif _auto_install "python3.8"; then
        PYTHON_BIN="python3.8"
        echo "   ✅ python3.8 安装完成（千人千面可用）"
    else
        echo "   ⚠️  python38 安装失败，千人千面功能不可用（不影响看板/分时对比）"
    fi
fi

# 虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 创建 Python 虚拟环境..."
    $PYTHON_BIN -m venv .venv
fi
source .venv/bin/activate

if [ ! -f ".venv/.deps_installed" ] || [ "$PROJECT_DIR/requirements.txt" -nt ".venv/.deps_installed" ]; then
    echo "📦 安装/更新 Python 依赖..."
    pip install --upgrade pip -q 2>/dev/null
    pip install -r requirements.txt -q
    # supabase 为千人千面可选依赖（需 Python 3.8+），安装失败不中断
    pip install "supabase>=2.0.0" -q 2>/dev/null || echo "   ⚠️  supabase 安装失败（需 Python 3.8+，千人千面功能不可用）"
    touch .venv/.deps_installed
else
    echo "✅ Python 依赖已是最新"
fi

echo "✅ Python $(python3 --version)"
echo "✅ Node.js $(node -v)"

# 全局安装 westock-data（跳过 npx 启动开销 ~0.5s/次）
if ! command -v westock-data &> /dev/null; then
    echo "📦 全局安装 westock-data..."
    npm install -g westock-data-skillhub@1.0.5 --silent
fi
echo "✅ westock-data $(westock-data --version 2>/dev/null || echo 'ok')"

# ----------------------------------------------------------
# 3. 编译前端
# ----------------------------------------------------------
echo ""
echo "🏗️  编译前端..."
cd frontend

# 清除旧的 node_modules 和构建缓存（避免跨平台/GLIBC 不兼容的原生绑定残留）
if [ ! -d "node_modules" ] || [ ! -d "dist" ]; then
    rm -rf node_modules package-lock.json dist 2>/dev/null
    echo "   安装前端依赖..."
    npm install --silent
fi

npm run build -- --emptyOutDir 2>&1 | tail -2
if [ -d "dist" ]; then
    echo "✅ 前端编译完成 (frontend/dist/)"
else
    echo "❌ 前端编译失败，查看上方错误"
    cd "$PROJECT_DIR"
    exit 1
fi
cd "$PROJECT_DIR"


# ----------------------------------------------------------
# 4. 启动服务
# ----------------------------------------------------------
mkdir -p "$LOGDIR"

if $USE_SYSTEMD && $PROD_MODE; then
    echo ""
    echo "🚀 安装 systemd 服务（生产模式）..."
    cp "$SERVICE_FILE" /etc/systemd/system/westock-monitor.service
    sed -i "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" /etc/systemd/system/westock-monitor.service
    sed -i "s|ExecStart=.*|ExecStart=$VENV_PYTHON -m uvicorn app:app --host 0.0.0.0 --port 8200|" /etc/systemd/system/westock-monitor.service
    sed -i "s|Environment=PATH=.*|Environment=PATH=$PROJECT_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin|" /etc/systemd/system/westock-monitor.service
    # 采集参数写入 systemd（服务器资源有限，降并发防 CLI 超时；加大单次超时）
    # 可通过环境变量覆盖：WESTOCK_WORKERS / WESTOCK_TIMEOUT / MINUTE_INTERVAL
    _env_workers="${WESTOCK_WORKERS:-6}"    # 服务器默认 6 并发（本地 12 会争抢导致超时）
    _env_timeout="${WESTOCK_TIMEOUT:-60}"   # 单次 CLI 超时加大到 60s（服务器网络慢）
    sed -i "/Environment=PATH=.*/a Environment=WESTOCK_WORKERS=$_env_workers\nEnvironment=WESTOCK_TIMEOUT=$_env_timeout" /etc/systemd/system/westock-monitor.service
    sed -i "s|StandardOutput=.*|StandardOutput=append:$LOGDIR/backend.log|" /etc/systemd/system/westock-monitor.service
    sed -i "s|StandardError=.*|StandardError=append:$LOGDIR/backend.log|" /etc/systemd/system/westock-monitor.service
    systemctl daemon-reload
    systemctl enable westock-monitor
    systemctl start westock-monitor
    echo "   systemd 服务已启动（enable 已开，下次开机自启）"
elif $USE_SYSTEMD && ! $PROD_MODE; then
    echo ""
    echo "🚀 启动后端 (nohup, 端口 8200) — 开发模式，跳过 systemd 注册"
    echo "    （如需生产部署加 --prod，才会注册并 enable 开机自启）"
    nohup "$VENV_PYTHON" -m uvicorn app:app --host 0.0.0.0 --port 8200 \
        > "$LOGDIR/backend.log" 2>&1 &
    BACKEND_PID=$!
    echo "   PID: $BACKEND_PID"
else
    echo ""
    echo "🚀 启动后端 (nohup, 端口 8200)..."
    nohup "$VENV_PYTHON" -m uvicorn app:app --host 0.0.0.0 --port 8200 \
        > "$LOGDIR/backend.log" 2>&1 &
    BACKEND_PID=$!
    echo "   PID: $BACKEND_PID"
fi

# 等待启动
echo ""
echo "⏳ 等待服务就绪..."
_start_ok=false
for i in $(seq 1 20); do
    sleep 0.5
    if curl -sf http://localhost:8200/api/health > /dev/null 2>&1; then
        _start_ok=true
        break
    fi
done

# ----------------------------------------------------------
# 5. 验证
# ----------------------------------------------------------
echo ""
echo "📋 验证服务..."
ok=true

if $_start_ok; then
    health_json=$(curl -sf http://localhost:8200/api/health 2>/dev/null || echo '{}')
    cache_ready=$(echo "$health_json" | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('cache_ready',False))" 2>/dev/null || echo "?")
    echo "   ✅ 后端 http://localhost:8200 (cache_ready=$cache_ready)"
else
    echo "   ❌ 后端启动失败！端口 8200 无响应，查看 $LOGDIR/backend.log"
    ok=false
fi

if curl -sf http://localhost:8200/ > /dev/null 2>&1; then
    echo "   ✅ 前端 http://localhost:8200/"
else
    echo "   ⚠️  前端未就绪，请先编译: npm run build (cd frontend)"
    ok=false
fi

sleep 2
if grep -q "collector loop started" "$LOGDIR/backend.log" 2>/dev/null; then
    echo "   ✅ 采集线程已启动"
else
    echo "   ⚠️  采集线程未检测到"
fi

echo ""
echo "============================================"
if $ok; then
    echo "  ✅ 全部就绪"
else
    echo "  ⚠️  部分服务未就绪，请查看 $LOGDIR/"
fi
echo "   API 文档: http://localhost:8200/docs"
if $USE_SYSTEMD; then
    echo "   日志:     journalctl -u westock-monitor -f"
    echo "   状态:     systemctl status westock-monitor"
    echo "   停止:     ./dev.sh kill"
else
    echo "   日志:     $LOGDIR/backend.log"
    echo "   停止:     ./dev.sh kill"
fi
echo "============================================"
