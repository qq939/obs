#!/bin/bash
# restart_obs.sh — 重启 8082 上的 OBS Web App（改代码后使用）
# 1) 结束现有 src/obs/server.py 进程  2) 调用 user_start.sh 重新拉起
set -u
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

for pid in $(pgrep -f "python3 src/obs/server.py"); do
    echo "[restart] kill PID=$pid"
    kill "$pid" 2>/dev/null || true
done
sleep 2
exec ./user_start.sh
