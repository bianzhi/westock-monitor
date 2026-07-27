#!/usr/bin/env bash
# ============================================================
# Westock Monitor 一键启动脚本 (macOS)
# 用法:
#   ./start.sh          # 启动后端 + 前端 + 采集循环
#   ./start.sh backend  # 仅启动后端
#   ./start.sh frontend # 仅启动前端
#   ./start.sh collector# 仅启动采集循环
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# 检查 Node.js
if ! command -v node &> /dev/null; then
    echo "❌ 未检测到 Node.js，请先安装: brew install node"
    exit 1
fi
if ! command -v npx &> /dev/null; then
    echo "❌ 未检测到 npx，请升级 Node.js 到 v18+"
    exit 1
fi

NODE_MAJOR=$(node -v | sed 's/v\([0-9]*\)\..*/\1/')
if [ "$NODE_MAJOR" -lt 18 ]; then
    echo "❌ Node.js 版本过低 ($(node -v))，需要 v18+"
    exit 1
fi

echo "✅ Node.js $(node -v)"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未检测到 python3"
    exit 1
fi

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]); then
    echo "❌ Python 版本过低 ($(python3 --version))，需要 3.8+"
    exit 1
fi

echo "✅ Python $(python3 --version)"

# 创建虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# 安装依赖
if [ ! -f ".venv/.deps_installed" ]; then
    echo "📦 安装 Python 依赖..."
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    touch .venv/.deps_installed
fi

echo "✅ Python 依赖已安装"

# 验证 westock-data 可用
echo "🔍 验证 westock-data CLI..."
if ! npx -y westock-data-skillhub@1.0.5 sector --help &> /dev/null; then
    echo "⚠️  westock-data CLI 验证失败，请检查网络"
else
    echo "✅ westock-data CLI 可用"
fi

# ============================================================
# 启动模式
# ============================================================
MODE="${1:-all}"

start_backend() {
    echo "🚀 启动 FastAPI 后端 (端口 8200)..."
    uvicorn app:app --host 0.0.0.0 --port 8200 --reload
}

start_frontend() {
    echo "🚀 启动 React 前端 (端口 5173)..."
    cd frontend
    if [ ! -d "node_modules" ]; then
        echo "📦 安装前端依赖..."
        npm install --silent
    fi
    npm run dev
}

start_collector() {
    echo "🚀 启动采集循环..."
    python collector.py --loop
}

case "$MODE" in
    backend)
        start_backend
        ;;
    frontend)
        start_frontend
        ;;
    collector)
        start_collector
        ;;
    all)
        echo "🚀 启动全部服务..."
        # 后端
        start_backend &
        BACKEND_PID=$!
        # 前端
        start_frontend &
        FRONTEND_PID=$!
        # 采集
        start_collector &
        COLLECTOR_PID=$!

        echo ""
        echo "✅ 全部服务已启动"
        echo "   后端:    http://localhost:8200 (PID $BACKEND_PID)"
        echo "   前端:    http://localhost:5173 (PID $FRONTEND_PID)"
        echo "   采集器:  PID $COLLECTOR_PID"
        echo ""
        echo "按 Ctrl+C 停止所有服务"

        trap "kill $BACKEND_PID $FRONTEND_PID $COLLECTOR_PID 2>/dev/null; exit" SIGINT SIGTERM
        wait
        ;;
    *)
        echo "用法: $0 {all|backend|frontend|collector}"
        exit 1
        ;;
esac
