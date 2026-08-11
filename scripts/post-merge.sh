#!/usr/bin/env bash
# Git post-merge hook: pull 后自动重启服务
# 安装: cp scripts/post-merge.sh .git/hooks/post-merge

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

# 仅 main 分支触发
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$BRANCH" != "main" ]; then
    exit 0
fi

echo "🔄 git pull 检测到新代码，重启服务..."

if command -v systemctl &> /dev/null && systemctl is-enabled --quiet westock-monitor 2>/dev/null; then
    systemctl restart westock-monitor
    echo "✅ systemd 服务已重启"
else
    pids=$(pgrep -f "uvicorn app:app" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        sleep 1
    fi
    source .venv/bin/activate 2>/dev/null || true
    nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8200 > logs/backend.log 2>&1 &
    echo "✅ nohup 进程已重启 (PID $!)"
fi
