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
# 2. 检查依赖
# ----------------------------------------------------------
echo ""
echo "🔍 检查环境..."

if ! command -v node &> /dev/null; then
    echo "❌ 未检测到 Node.js，请先安装"
    exit 1
fi
if ! command -v python3 &> /dev/null; then
    echo "❌ 未检测到 python3"
    exit 1
fi

# 虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 创建 Python 虚拟环境..."
    python3 -m venv .venv
fi
source .venv/bin/activate

if [ ! -f ".venv/.deps_installed" ]; then
    echo "📦 安装 Python 依赖..."
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    touch .venv/.deps_installed
fi

echo "✅ Python $(python3 --version)"
echo "✅ Node.js $(node -v)"

# ----------------------------------------------------------
# 3. 编译前端
# ----------------------------------------------------------
echo ""
echo "🏗️  编译前端..."
cd frontend
if [ ! -d "node_modules" ]; then
    echo "   安装前端依赖..."
    npm install --silent
fi
npm run build -- --emptyOutDir 2>&1 | tail -2
echo "✅ 前端编译完成 (frontend/dist/)"
cd "$PROJECT_DIR"

# ----------------------------------------------------------
# 4. 启动服务
# ----------------------------------------------------------
echo ""
echo "🚀 启动服务..."

LOGDIR="$PROJECT_DIR/logs"
mkdir -p "$LOGDIR"

# 后端 (FastAPI, port 8200)
echo "   启动后端 (端口 8200)..."
nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8200 \
    > "$LOGDIR/backend.log" 2>&1 &
BACKEND_PID=$!

# 前端 dev server (Vite, port 5173)
echo "   启动前端 (端口 5173)..."
nohup npm run dev -- --host 0.0.0.0 --port 5173 \
    > "$LOGDIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
cd "$PROJECT_DIR"

# 等待启动
sleep 2

# ----------------------------------------------------------
# 5. 验证
# ----------------------------------------------------------
echo ""
echo "📋 验证服务..."
ok=true

# 后端
if curl -sf http://localhost:8200/api/health > /dev/null 2>&1; then
    echo "   ✅ 后端 http://localhost:8200 (PID $BACKEND_PID)"
else
    echo "   ⚠️  后端未就绪，查看 logs/backend.log"
    ok=false
fi

# 前端
if curl -sf http://localhost:5173 > /dev/null 2>&1; then
    echo "   ✅ 前端 http://localhost:5173 (PID $FRONTEND_PID)"
else
    echo "   ⚠️  前端未就绪，查看 logs/frontend.log"
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
