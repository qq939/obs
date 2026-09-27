#!/bin/bash
# =============================================================================
# install_ffmpeg.sh — 为 OBS Web App 安装静态 ffmpeg / ffprobe 到 project/bin/
# -----------------------------------------------------------------------------
# 背景：容器内 apt 无 ffmpeg 包，static-ffmpeg / ffmpeg-static 需要访问 github.com
#       （被网络策略拦截）。这里改用 npm registry 上自带二进制的包（registry 可达）：
#         @ffmpeg-installer/linux-x64   -> ffmpeg
#         @ffprobe-installer/linux-x64  -> ffprobe
# 产物：bin/ffmpeg、bin/ffprobe（已被 .gitignore 忽略，不随仓库提交）
# 用法：bash scripts/install_ffmpeg.sh
# =============================================================================
set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$PROJECT_DIR/bin"
TMP_DIR="$(mktemp -d)"
mkdir -p "$BIN_DIR"

FFMPEG_URL="https://registry.npmjs.org/@ffmpeg-installer/linux-x64/-/linux-x64-4.1.0.tgz"
FFPROBE_URL="https://registry.npmjs.org/@ffprobe-installer/linux-x64/-/linux-x64-5.2.0.tgz"

download_until_valid() {  # $1=url  $2=outfile  $3=member(subpath in tar)
    url="$1"; out="$2"; member="$3"
    for i in 1 2 3 4 5 6 7 8; do
        curl -sSL -C - -o "$out" "$url" >/dev/null 2>&1 || true
        if tar tzf "$out" >/dev/null 2>&1; then
            return 0
        fi
        echo "[install_ffmpeg] 重试 $i（当前 $(stat -c %s "$out" 2>/dev/null || echo 0) 字节）"
    done
    return 1
}

extract_bin() {  # $1=tgz $2=member $3=destname
    tar xzf "$1" -C "$TMP_DIR"
    cp "$TMP_DIR/package/$2" "$BIN_DIR/$3"
    chmod +x "$BIN_DIR/$3"
}

echo "[install_ffmpeg] 下载 ffmpeg ..."
download_until_valid "$FFMPEG_URL" "$TMP_DIR/ffmpeg.tgz" "ffmpeg" || { echo "ffmpeg 下载失败"; exit 1; }
extract_bin "$TMP_DIR/ffmpeg.tgz" "ffmpeg" "ffmpeg"

echo "[install_ffmpeg] 下载 ffprobe ..."
download_until_valid "$FFPROBE_URL" "$TMP_DIR/ffprobe.tgz" "ffprobe" || { echo "ffprobe 下载失败"; exit 1; }
extract_bin "$TMP_DIR/ffprobe.tgz" "ffprobe" "ffprobe"

rm -rf "$TMP_DIR"
echo "[install_ffmpeg] 完成："
"$BIN_DIR/ffmpeg" -version 2>&1 | head -1
"$BIN_DIR/ffprobe" -version 2>&1 | head -1
