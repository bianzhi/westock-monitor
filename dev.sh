#!/usr/bin/env bash
# ============================================================
# Westock Monitor 一键编译 + 启动前后端（先杀旧服务）
# 用法:
#   ./dev.sh          # 编译前端 + 启动后端(8200) + 前端(5173)
#   ./dev.sh kill     # 仅杀掉旧服务
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

KILL_ONLY=false
if [ "${1:-}" = "kill" ]; then
    KILL_ONLY=true
fi

# ----------------------------------------------------------
# 1. 杀掉旧服务
# ----------------------------------------------------------
echo "🔪 检查并杀掉旧服务..."
killed=0
for port in 8200 5173; do
    pids=$(lsof -ti ":$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "   端口 $port → PID $pids"
        kill $pids 2>/dev/null || true
        killed=$((killed + 1))
    fi
done
# 等一秒确保端口释放
if [ $killed -gt 0 ]; then
    sleep 1
    echo "✅ 已杀掉 $killed 个旧服务"
else
    echo "   没有运行中的旧服务"
fi

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
echo ""
echo "🚀 启动服务..."

LOGDIR="$PROJECT_DIR/logs"
mkdir -p "$LOGDIR"

# 后端 (FastAPI, port 8200) —— 同时 serve API + 前端静态文件
echo "   启动后端 (端口 8200，含前端)..."
nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8200 \
    > "$LOGDIR/backend.log" 2>&1 &
BACKEND_PID=$!

# 等待启动
sleep 2

# ----------------------------------------------------------
# 5. 验证
# ----------------------------------------------------------
echo ""
echo "📋 验证服务..."
ok=true

# 后端（含前端静态文件）
if curl -sf http://localhost:8200/api/health > /dev/null 2>&1; then
    echo "   ✅ 后端 http://localhost:8200 (PID $BACKEND_PID)"
else
    echo "   ⚠️  后端未就绪，查看 logs/backend.log"
    ok=false
fi

# 前端（走 8200 同一端口）
if curl -sf http://localhost:8200/ > /dev/null 2>&1; then
    echo "   ✅ 前端 http://localhost:8200/"
else
    echo "   ⚠️  前端未就绪，确认 frontend/dist/ 已编译"
    ok=false
fi

echo ""
echo "============================================"
if $ok; then
    echo "  ✅ 全部就绪"
else
    echo "  ⚠️  部分服务未就绪，请查看 logs/"
fi
echo "   API 文档: http://localhost:8200/docs"
echo "   日志:     $LOGDIR/"
echo "   停止:     ./dev.sh kill"
echo "============================================"
