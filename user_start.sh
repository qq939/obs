#!/bin/bash
# =============================================================================
# user_start.sh — OBS Web App（端口 8082）启动脚本
# -----------------------------------------------------------------------------
# 由 project/start.sh 在容器启动时调用。
# 启动对象：src/obs/server.py（OBS 文件托管 + /video 视频流 二合一，FastAPI）
# 规范参考：systemreadme.md / skill hermit-ports、hermit-logging
#
# 行为：
#   1. 准备 obs/ obs_shards/ logs/ 运行目录
#   2. 若 8082 已有健康实例则跳过，避免重复启动
#   3. 后台启动应用，全部启动日志写入 logs/start.log
#   4. 轮询 /health 直到就绪（最多 30s），输出结果
# =============================================================================
set -u

PROJECT_DIR="/home/agent/.claude/workspace/project"
cd "$PROJECT_DIR" || exit 1

mkdir -p logs obs obs/.chunks obs_shards
LOG_FILE="$PROJECT_DIR/logs/start.log"
PORT=8082

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [user_start] $*" | tee -a "$LOG_FILE"
}

log "===== OBS web app 启动流程开始 (port $PORT) ====="

# 已有健康实例则不重复启动
if curl -sf -m 3 "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
    log "检测到 8082 已有健康实例，跳过启动。"
    log "===== 启动流程结束（已就绪）====="
    exit 0
fi

if [ ! -f src/obs/server.py ]; then
    log "[ERROR] 未找到 src/obs/server.py，终止启动。"
    exit 1
fi

log "启动: python3 src/obs/server.py (PORT=$PORT UPLOAD_DIR=obs HLS_DIR=obs_shards LOG_DIR=logs)"
# 优先使用项目自带静态 ffmpeg/ffprobe（bin/），使 HLS 转码与视频时长探测可用
if [ -x "$PROJECT_DIR/bin/ffmpeg" ]; then
    export PATH="$PROJECT_DIR/bin:$PATH"
    log "已启用内置 ffmpeg: $PROJECT_DIR/bin/ffmpeg"
fi
# 幂等安装依赖（重建容器后不依赖镜像层里的 pip 包）
python3 -m pip install -q --break-system-packages -r requirements.txt >> logs/pip_boot.log 2>&1 || true
PORT="$PORT" UPLOAD_DIR="obs" HLS_DIR="obs_shards" LOG_DIR="logs" \
    nohup python3 src/obs/server.py >> "$LOG_FILE" 2>&1 &
PID=$!
log "obs server 已启动，PID=$PID"

# 等待就绪
READY=0
for i in $(seq 1 30); do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
done

if [ "$READY" = "1" ]; then
    log "健康检查通过: http://127.0.0.1:${PORT}/health (耗时 ${i}s)"
    log "OBS 文件托管首页: http://127.0.0.1:${PORT}/"
    log "视频流页面: http://127.0.0.1:${PORT}/video"
    log "===== 启动流程结束（成功）====="
else
    log "[ERROR] 8082 在 30s 内未通过健康检查，请查看 logs/start.log 末尾输出。"
    log "===== 启动流程结束（失败）====="
    exit 1
fi
