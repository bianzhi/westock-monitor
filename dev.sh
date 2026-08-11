#!/usr/bin/env bash
# ============================================================
# Westock Monitor 一键启动脚本（唯一入口）
# 用法:
#   ./dev.sh          # 编译前端 + 启动后端(8200)
#   ./dev.sh kill     # 停止服务
#   ./dev.sh --build  # 强制重新编译前端
#
# 自动适配 systemd（生产）与 nohup（开发），无需其他脚本。
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
LOGDIR="$PROJECT_DIR/logs"
SERVICE_FILE="$PROJECT_DIR/westock-monitor.service"
USE_SYSTEMD=false

KILL_ONLY=false
FORCE_BUILD=false
for arg in "$@"; do
    case "$arg" in
        kill) KILL_ONLY=true ;;
        --build) FORCE_BUILD=true ;;
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
    local pids=$(lsof -ti ":$port" 2>/dev/null || true)
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

# 1a. 先杀端口进程（不管用没用 systemd，端口必须释放）
_kill_port 8200 || true
_kill_port 5173 || true
# 额外清理 uvicorn/vite 残留
for name in uvicorn vite; do
    pids=$(pgrep -f "$name" 2>/dev/null || true)
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
done

# 1b. 再处理 systemd 服务
if $USE_SYSTEMD; then
    if systemctl is-active --quiet westock-monitor 2>/dev/null; then
        systemctl stop westock-monitor 2>/dev/null || true
    fi
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

# 安装 git post-merge hook（git pull 后自动重启）
HOOK_FILE="$PROJECT_DIR/.git/hooks/post-merge"
if [ -d "$PROJECT_DIR/.git" ] && [ ! -f "$HOOK_FILE" ]; then
    cp "$PROJECT_DIR/scripts/post-merge.sh" "$HOOK_FILE"
    chmod +x "$HOOK_FILE"
    echo "✅ git post-merge hook 已安装"
fi

# ----------------------------------------------------------
# 4. 启动服务
# ----------------------------------------------------------
mkdir -p "$LOGDIR"

if $USE_SYSTEMD; then
    echo ""
    echo "🚀 安装 systemd 服务..."
    cp "$SERVICE_FILE" /etc/systemd/system/westock-monitor.service
    sed -i "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" /etc/systemd/system/westock-monitor.service
    sed -i "s|ExecStart=.*|ExecStart=$VENV_PYTHON -m uvicorn app:app --host 0.0.0.0 --port 8200|" /etc/systemd/system/westock-monitor.service
    sed -i "s|Environment=PATH=.*|Environment=PATH=$PROJECT_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin|" /etc/systemd/system/westock-monitor.service
    sed -i "s|StandardOutput=.*|StandardOutput=append:$LOGDIR/backend.log|" /etc/systemd/system/westock-monitor.service
    sed -i "s|StandardError=.*|StandardError=append:$LOGDIR/backend.log|" /etc/systemd/system/westock-monitor.service
    systemctl daemon-reload
    systemctl enable westock-monitor
    systemctl start westock-monitor
    echo "   systemd 服务已启动"
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
