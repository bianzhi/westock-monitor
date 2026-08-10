#!/usr/bin/env bash
# ============================================================
# Westock Monitor 生产级启动脚本
#
# 优先使用 systemd（稳定 + 自动重启），
# 无 systemd 时降级为 nohup 后台进程。
#
# 用法:
#   ./start.sh          # 启动
#   ./start.sh --build  # 重新编译前端后启动
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
LOGDIR="$PROJECT_DIR/logs"
mkdir -p "$LOGDIR"

BUILD_FRONTEND=false
if [ "${1:-}" = "--build" ]; then
    BUILD_FRONTEND=true
fi

# ----------------------------------------------------------
# 1. 编译前端
# ----------------------------------------------------------
if $BUILD_FRONTEND || [ ! -d "frontend/dist" ]; then
    echo "🏗️  编译前端..."
    cd frontend
    if [ ! -d "node_modules" ]; then
        npm install --silent
    fi
    npm run build -- --emptyOutDir 2>&1 | tail -3
    cd "$PROJECT_DIR"
    echo "✅ 前端编译完成"
fi

# ----------------------------------------------------------
# 2. 确保虚拟环境 + 依赖
# ----------------------------------------------------------
if [ ! -f "$VENV_PYTHON" ]; then
    echo "📦 创建 Python 虚拟环境..."
    python3 -m venv .venv
fi

if [ ! -f ".venv/.deps_installed" ] || [ "requirements.txt" -nt ".venv/.deps_installed" ]; then
    echo "📦 安装/更新 Python 依赖..."
    "$VENV_PYTHON" -m pip install --upgrade pip -q 2>/dev/null
    "$VENV_PYTHON" -m pip install -r requirements.txt -q
    touch .venv/.deps_installed
fi

# ----------------------------------------------------------
# 3. 先停旧进程
# ----------------------------------------------------------
if command -v systemctl &> /dev/null && systemctl is-active --quiet westock-monitor 2>/dev/null; then
    echo "🔪 停止旧 systemd 服务..."
    systemctl stop westock-monitor
else
    pids=$(pgrep -f "uvicorn app:app" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "🔪 杀掉旧进程: $pids"
        kill $pids 2>/dev/null || true
        sleep 1
    fi
fi

# ----------------------------------------------------------
# 4. 启动
# ----------------------------------------------------------
SERVICE_FILE="$PROJECT_DIR/westock-monitor.service"

if command -v systemctl &> /dev/null; then
    echo "🚀 安装 systemd 服务..."
    cp "$SERVICE_FILE" /etc/systemd/system/westock-monitor.service
    # 替换 WorkingDirectory 路径为实际路径
    sed -i "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" /etc/systemd/system/westock-monitor.service
    sed -i "s|ExecStart=.*|ExecStart=$VENV_PYTHON -m uvicorn app:app --host 0.0.0.0 --port 8200|" /etc/systemd/system/westock-monitor.service
    sed -i "s|Environment=PATH=.*|Environment=PATH=$PROJECT_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin|" /etc/systemd/system/westock-monitor.service
    sed -i "s|StandardOutput=.*|StandardOutput=append:$LOGDIR/backend.log|" /etc/systemd/system/westock-monitor.service
    sed -i "s|StandardError=.*|StandardError=append:$LOGDIR/backend.log|" /etc/systemd/system/westock-monitor.service

    systemctl daemon-reload
    systemctl enable westock-monitor
    systemctl start westock-monitor

    echo "✅ systemd 服务已启动"
    echo "   状态: systemctl status westock-monitor"
    echo "   日志: journalctl -u westock-monitor -f"
    echo "   停止: systemctl stop westock-monitor"
else
    echo "🚀 无 systemd，降级为 nohup 后台进程..."
    nohup "$VENV_PYTHON" -m uvicorn app:app --host 0.0.0.0 --port 8200 \
        > "$LOGDIR/backend.log" 2>&1 &
    PID=$!
    echo "✅ 后台进程已启动 (PID $PID)"
    echo "   停止: kill $PID"
fi

# ----------------------------------------------------------
# 5. 验证
# ----------------------------------------------------------
echo ""
echo "⏳ 等待服务就绪..."
for i in $(seq 1 20); do
    sleep 0.5
    if curl -sf http://localhost:8200/api/health > /dev/null 2>&1; then
        echo "✅ 服务就绪: http://localhost:8200"
        echo "   API 文档: http://localhost:8200/docs"
        exit 0
    fi
done
echo "⚠️  服务未就绪，查看 $LOGDIR/backend.log"
exit 1
