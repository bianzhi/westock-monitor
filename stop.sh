#!/usr/bin/env bash
# Westock Monitor 停止脚本
set -e

if command -v systemctl &> /dev/null && systemctl is-active --quiet westock-monitor 2>/dev/null; then
    echo "🔪 停止 systemd 服务..."
    systemctl stop westock-monitor
    echo "✅ 已停止"
elif pids=$(pgrep -f "uvicorn app:app" 2>/dev/null); then
    echo "🔪 杀掉进程: $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    echo "✅ 已停止"
else
    echo "没有运行中的服务"
fi
