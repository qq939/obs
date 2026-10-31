# 服务对外访问地址前缀（站点域名，用于服务端生成的静态链接与示例文字）
#   - 站点域名：https://ocs.dimond.top（反代终止 TLS；http 会 301 跳转到 https）
#     = 宿主机 http://localhost:19082 = 容器内 http://localhost:8082
#   - 网页端的「上传/下载」动作不走这里，而是由前端 JS 先试直连 http://localhost:19082、失败再回退本域名
# 使用位置：
#   - 首页「文件托管」curl 上传示例、文件列表下载链接前缀、上传成功响应文本、启动日志示例
url_head = "https://ocs.dimond.top"

import os
import time
import shutil
import json
import uuid
import zipfile
import asyncio
import threading
import subprocess
import re
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, suppress
from typing import List, Optional, Dict
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import uvicorn
import aiofiles
from contextlib import asynccontextmanager
import hashlib
import base64

# 视频相关配置
HLS_DIR = os.environ.get("HLS_DIR", "obs_shards")
video_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video_worker")

# 加载环境变量
load_dotenv()
load_dotenv("env")
load_dotenv("asset/.env")

# 服务器配置
# PORT 默认 8082：容器内 Web App 统一端口（可用环境变量覆盖）
PORT = int(os.environ.get("PORT", 8082))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "obs")
# 日志目录（全局参数；docker-compose 挂载到主机 logs/，容器内为 /app/logs）
# 使用位置：log_line() 拼接 {LOG_DIR}/server.log；lifespan 启动日志打印 logs dir
LOG_DIR = os.environ.get("LOG_DIR", "logs")


def log_line(message: str) -> None:
    """把带时间戳的一行追加写入 {LOG_DIR}/server.log，同时打到标准输出（便于 docker logs 查看）。
    写文件失败不影响业务。
    使用位置：lifespan 启动；upload_init / upload_chunk / upload_complete / upload_file_put / upload_file_form 的成功与失败分支"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "server.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# 全局性能参数（使用位置见行内注释）
# MAX_UPLOAD_SIZE: 上传大小限制（None 表示无限制）
# 使用位置：upload_file_form 写入循环累计判断；upload_file_put 流式写入累计判断
MAX_UPLOAD_SIZE = None
# UPLOAD_CHUNK_SIZE: 表单上传读取分片大小（10MB）
# 使用位置：upload_file_form 读取循环
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024
# ---------------------------------------------------------------------------
# 文件夹上传（暂存 → 打包 zip → 清理源目录）
# ---------------------------------------------------------------------------
# FOLDER_STAGING_ROOT: 暂存根目录（位于 UPLOAD_DIR 下；点开头，首页列表不展示）
#   使用位置：_staging_root / _staging_dir / _enforce_staging_quota
FOLDER_STAGING_ROOT = ".folder-staging"
# FOLDER_STAGING_LIMIT: 暂存区总容量上限，默认 200GB（可用环境变量覆盖）
#   使用位置：_enforce_staging_quota
FOLDER_STAGING_LIMIT = int(os.environ.get("FOLDER_STAGING_LIMIT", 200 * 1024 ** 3))
# FOLDER_ACTIVE_GRACE_SECONDS: 最近 N 秒内仍有更新的会话视为「活跃」，配额清理时绝不删除
#   使用位置：_cleanup_candidates
FOLDER_ACTIVE_GRACE_SECONDS = int(os.environ.get("FOLDER_ACTIVE_GRACE_SECONDS", 600))
# 打包任务状态表 folder_id -> {status, zip_name, ...}
#   使用位置：/api/folder/{id}/zip、/api/folder/{id}/zip/status、_run_pack_job
_folder_jobs: Dict[str, dict] = {}
_folder_jobs_lock = threading.Lock()
# RANGE_DOWNLOAD_CHUNK_SIZE: Range 分片下载时的单次读取分片大小（10MB）
# 使用位置：download_file -> iterfile(chunk_size=...)
RANGE_DOWNLOAD_CHUNK_SIZE = 10 * 1024 * 1024
# STREAM_DOWNLOAD_CHUNK_SIZE: 完整流式下载分片大小（40MB）
# 使用位置：download_file 无 Range 分支的 StreamingResponse 生成器
STREAM_DOWNLOAD_CHUNK_SIZE = 40 * 1024 * 1024
# UVICORN 运行参数
# 使用位置：__main__ 中的 uvicorn.run(...)
UVICORN_CONFIG = {
    "limit_concurrency": 1000,
    "limit_max_requests": 10000,
    "timeout_keep_alive": 300,
    "backlog": 2048,
}
def get_upload_dir() -> str:
    return os.environ.get("UPLOAD_DIR", UPLOAD_DIR)

def get_chunk_dir() -> str:
    return os.path.join(get_upload_dir(), ".chunks")

def get_hls_dir() -> str:
    return os.environ.get("HLS_DIR", HLS_DIR)

# ============================================================================
# 视频相关辅助函数
# ============================================================================

VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mkv"}

# ---------------------------------------------------------------------------
# HLS 生成配置（全局参数，使用位置见行内注释）
# ---------------------------------------------------------------------------
# HLS_GEN_VERSION: 产物版本号，写入 hls/<name>/meta.json；hls_exists 会用它与源文件 size 校验，
#   版本号递增（或源文件被替换）会让旧产物自动失效并重新生成。
#   使用位置：hls_exists 校验、write_hls_meta 写入
HLS_GEN_VERSION = 4
# HLS_SEGMENT_BYTES: 单个 TS 分片目标大小 4MiB（GOP 对齐，实际略有浮动）
#   使用位置：build_hls_args 的 -hls_segment_size
HLS_SEGMENT_BYTES = 4 * 1024 * 1024
# HLS_TIMEOUT_SECONDS: 单个视频生成 HLS 的超时（大文件 remux 较慢）
#   使用位置：generate_hls 的 asyncio.wait_for
HLS_TIMEOUT_SECONDS = 600
# 每日定时任务时段（Asia/Shanghai，无夏令时）；可用环境变量覆盖
#   使用位置：hls_cron_key / hls_cron_tick / /hls/cron
# 默认关闭：只有用户在页面里打开「每日 HLS 分片任务」才会跑（状态存 logs/hls_cron.json）
HLS_CRON_ENABLED = os.environ.get("HLS_CRON_ENABLED", "0").lower() not in ("0", "false", "no")
# 运行时开关状态文件（相对启动目录，user_start.sh 会 cd 到项目根）
HLS_CRON_STATE_PATH = os.environ.get("HLS_CRON_STATE", os.path.join("logs", "hls_cron.json"))
HLS_CRON_HOUR = int(os.environ.get("HLS_CRON_HOUR", 4))
HLS_CRON_MINUTE = int(os.environ.get("HLS_CRON_MINUTE", 0))
HLS_CRON_TZ = os.environ.get("HLS_CRON_TZ", "Asia/Shanghai")
# HLS_CRON_TICK_SECONDS: 轮询粒度；30s 足以在 04:00 那一分钟内命中
#   使用位置：_hls_cron_loop 的 asyncio.sleep
HLS_CRON_TICK_SECONDS = 30

# 已触发日期键（'YYYY-MM-DD'，本地时区），保证每天只跑一次
#   使用位置：hls_cron_tick
_hls_cron_last_key: Optional[str] = None
# per-name 生成锁：同一视频并发请求只跑一次 ffmpeg
#   使用位置：_get_hls_lock / generate_hls
HLS_LOCKS: Dict[str, asyncio.Lock] = {}
_HLS_LOCKS_GUARD = threading.Lock()

def hls_exists(filename: str) -> bool:
    """检查 HLS 是否为「有效且与当前源文件匹配」的产物。
    需要 index.m3u8 + meta.json，且 meta.version == HLS_GEN_VERSION、meta.size == 源文件大小；
    源文件不存在（孤儿产物）或版本过期都视为未生成，交由定时任务重新生成。"""
    d = os.path.join(get_hls_dir(), filename)
    m3u8 = os.path.join(d, "index.m3u8")
    meta_path = os.path.join(d, "meta.json")
    if not (os.path.isdir(d) and os.path.isfile(m3u8) and os.path.isfile(meta_path)):
        return False
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("version") != HLS_GEN_VERSION:
            return False
        src = os.path.join(get_upload_dir(), filename)
        if not os.path.isfile(src):
            return False
        return meta.get("size") == os.path.getsize(src)
    except Exception:
        return False

def hls_duration_sync(name: str) -> float:
    """从 HLS index.m3u8 求和 EXTINF 获取时长"""
    try:
        m3u8_path = os.path.join(get_hls_dir(), name, "index.m3u8")
        if os.path.exists(m3u8_path):
            with open(m3u8_path, "r") as f:
                content = f.read()
            matches = re.findall(r"#EXTINF:([0-9.]+)", content)
            total = sum(float(m) for m in matches if float(m) > 0)
            if total > 0:
                return total
    except Exception:
        pass
    return 0

# ---------------------------------------------------------------------------
# HLS 生成（ffmpeg）
# ---------------------------------------------------------------------------

def detect_codecs(file_path: str) -> Dict[str, Optional[str]]:
    """用 ffprobe 探测首个视频/音频编码（限制探测窗口，慢速存储上也很快）"""
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-analyzeduration", "1000000", "-probesize", "1000000",
         "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", file_path],
        capture_output=True, text=True, timeout=30
    )
    data = json.loads(result.stdout or "{}")
    video = audio = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not video:
            video = s.get("codec_name")
        if s.get("codec_type") == "audio" and not audio:
            audio = s.get("codec_name")
    return {"video": video, "audio": audio}

def detect_rotation(file_path: str) -> int:
    """探测视频旋转角度（0/90/180/270）"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream_tags=rotate:stream_side_data=rotation",
         "-of", "json", file_path],
        capture_output=True, text=True, timeout=30
    )
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    if not streams:
        return 0
    s = streams[0]
    tag = (s.get("tags") or {}).get("rotate")
    if tag:
        try:
            return int(tag)
        except (TypeError, ValueError):
            return 0
    for sd in (s.get("side_data_list") or []):
        if "rotation" in sd:
            try:
                return int(sd["rotation"])
            except (TypeError, ValueError):
                return 0
    return 0

def can_remux(src_path: str, codecs: Dict[str, Optional[str]], rotation: int) -> bool:
    """H.264(+AAC/MP3) 且未旋转的 mp4/m4v 可直接 -c copy remux，无需重编码"""
    if rotation != 0:
        return False
    if codecs.get("video") != "h264":
        return False
    audio = codecs.get("audio")
    if audio and audio not in ("aac", "mp3"):
        return False
    return os.path.splitext(src_path)[1].lower() in (".mp4", ".m4v")

def build_hls_args(src_path: str, codecs: Dict[str, Optional[str]], rotation: int) -> List[str]:
    """构造 ffmpeg 参数。以 cwd=输出目录运行，使 playlist 里引用相对文件名 seg-%05d.ts，
    从而能通过 /hls/<name>/ 正常访问。
    旋转过的源必须重编码：ffmpeg 的自动旋转会烘焙进像素，播放器才能看到正立画面。"""
    args = ["-y", "-i", src_path, "-map", "0:v:0"]
    if codecs.get("audio"):
        args += ["-map", "0:a:0"]
    common = [
        "-f", "hls",
        "-hls_segment_size", str(HLS_SEGMENT_BYTES),
        "-hls_list_size", "0",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", "seg-%05d.ts",
        "index.m3u8",
    ]
    if can_remux(src_path, codecs, rotation):
        return args + ["-c", "copy"] + common
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-pix_fmt", "yuv420p", "-vf", "scale='min(1920,iw)':-2"]
    args += ["-c:a", "aac", "-b:a", "128k"] if codecs.get("audio") else ["-an"]
    return args + common

def _get_hls_lock(name: str) -> asyncio.Lock:
    """取（或创建）某个视频专属的生成锁"""
    with _HLS_LOCKS_GUARD:
        lock = HLS_LOCKS.get(name)
        if lock is None:
            lock = asyncio.Lock()
            HLS_LOCKS[name] = lock
        return lock

async def generate_hls(name: str) -> bool:
    """为单个视频生成 HLS（幂等 + 并发去重）。已在生成中则等待同一把锁，不重复跑 ffmpeg。"""
    async with _get_hls_lock(name):
        if hls_exists(name):
            return False
        await _do_generate_hls(name)
        return True

async def _do_generate_hls(name: str) -> None:
    """实际执行 ffmpeg 生成：先写临时目录，成功后再原子替换到 hls/<name>/，避免半成品被读到"""
    # 用绝对路径：ffmpeg 以 cwd=tmp_dir 运行，UPLOAD_DIR 为相对路径（如 "obs"）时相对路径会解析失败
    src_path = os.path.abspath(os.path.join(get_upload_dir(), name))
    if not os.path.isfile(src_path):
        raise RuntimeError("源视频不存在")
    hls_root = get_hls_dir()
    os.makedirs(hls_root, exist_ok=True)
    tmp_dir = os.path.join(hls_root, f".tmp-{int(time.time() * 1000)}-{os.urandom(4).hex()}")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        codecs, rotation = await asyncio.gather(
            asyncio.to_thread(detect_codecs, src_path),
            asyncio.to_thread(detect_rotation, src_path),
        )
        args = build_hls_args(src_path, codecs, rotation)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", *args, cwd=tmp_dir,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=HLS_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise RuntimeError(f"HLS 生成超时（>{HLS_TIMEOUT_SECONDS}s）")
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
            raise RuntimeError(f"ffmpeg 退出码 {proc.returncode}: {' '.join(tail)}")
        if not os.path.isfile(src_path):
            raise RuntimeError("生成期间源视频被删除")
        if not os.path.isfile(os.path.join(tmp_dir, "index.m3u8")):
            raise RuntimeError("ffmpeg 未产出 index.m3u8")

        final_dir = os.path.join(hls_root, name)
        if os.path.isdir(final_dir):
            shutil.rmtree(final_dir, ignore_errors=True)
        os.replace(tmp_dir, final_dir)

        stat = os.stat(src_path)
        duration = await asyncio.to_thread(probe_duration_sync, src_path, name)
        segs = len([f for f in os.listdir(final_dir) if re.match(r"^seg-\d+\.ts$", f)])
        with open(os.path.join(final_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "version": HLS_GEN_VERSION,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "rotation": rotation,
                "duration": duration,
            }, f)
        log_line(f"HLS 生成完成 file={name} segments={segs} rotation={rotation} duration={duration:.2f}s")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

def _list_missing_hls() -> List[str]:
    """列出 obs/ 下所有「尚无有效 HLS」的视频名（阻塞 IO，调用方负责 offload）"""
    upload_dir = get_upload_dir()
    pending: List[str] = []
    try:
        for name in sorted(os.listdir(upload_dir)):
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            if not os.path.isfile(os.path.join(upload_dir, name)):
                continue
            if not hls_exists(name):
                pending.append(name)
    except OSError:
        pass
    return pending

def _count_videos() -> int:
    """统计 obs/ 下视频文件总数（阻塞 IO，调用方负责 offload）"""
    upload_dir = get_upload_dir()
    try:
        return sum(1 for n in os.listdir(upload_dir)
                   if os.path.splitext(n)[1].lower() in VIDEO_EXTS
                   and os.path.isfile(os.path.join(upload_dir, n)))
    except OSError:
        return 0

async def sweep_missing_hls(trigger: str = "manual") -> Dict[str, object]:
    """扫描并为缺 HLS 的视频串行生成（一次一个，避免多路 ffmpeg 抢占 CPU）。"""
    pending = await asyncio.to_thread(_list_missing_hls)
    total = len(pending)
    if total == 0:
        log_line(f"HLS 扫描[{trigger}]：无待处理视频")
        return {"total": 0, "pending": 0, "queue": [], "done": [], "failed": []}
    log_line(f"HLS 扫描[{trigger}]：{total} 个视频待生成（串行）")
    done: List[str] = []
    failed: List[str] = []
    for i, name in enumerate(pending, 1):
        try:
            await generate_hls(name)
            done.append(name)
            log_line(f"HLS 扫描[{trigger}] [{i}/{total}] {name}: 完成")
        except Exception as e:
            failed.append(name)
            log_line(f"HLS 扫描[{trigger}] [{i}/{total}] {name}: 失败 {e}")
    log_line(f"HLS 扫描[{trigger}] 结束：成功 {len(done)}，失败 {len(failed)}")
    return {"total": total, "pending": total, "queue": pending, "done": done, "failed": failed}

# ---------------------------------------------------------------------------
# 每日定时任务：每天 04:00（HLS_CRON_TZ）检查并生成缺 HLS 的视频
# ---------------------------------------------------------------------------

def _cron_tzinfo(tz_name: str):
    """取时区对象；系统无 tzdata 时回退到固定 UTC+8（Asia/Shanghai 无夏令时）"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=8))

def hls_cron_enabled_runtime() -> bool:
    """运行时开关：优先读状态文件；文件不存在时用环境变量（默认关闭）。"""
    try:
        with open(HLS_CRON_STATE_PATH, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("enabled"))
    except Exception:
        return HLS_CRON_ENABLED


def set_hls_cron_enabled(enabled: bool) -> bool:
    """写入开关状态（持久化到 logs/hls_cron.json，重启后仍生效）。"""
    d = os.path.dirname(HLS_CRON_STATE_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(HLS_CRON_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"enabled": bool(enabled),
                   "updated_at": datetime.now(timezone.utc).isoformat()}, f)
    return bool(enabled)


def hls_cron_key(now: datetime, tz_name: str, hour: int, minute: int) -> Optional[str]:
    """若 now（带时区）落在目标时区的 hour:minute，返回该天的日期键 'YYYY-MM-DD'，否则 None。
    日期键用于「每天只触发一次」的去重。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        local = now.astimezone(_cron_tzinfo(tz_name))
    except Exception:
        local = now.astimezone(timezone(timedelta(hours=8)))
    if local.hour != hour or local.minute != minute:
        return None
    return local.strftime("%Y-%m-%d")

async def hls_cron_tick() -> None:
    """定时轮询：命中 04:00 且当天未跑过，就执行一次扫描生成"""
    global _hls_cron_last_key
    if not hls_cron_enabled_runtime():
        return
    key = hls_cron_key(datetime.now(timezone.utc), HLS_CRON_TZ, HLS_CRON_HOUR, HLS_CRON_MINUTE)
    if key is None or key == _hls_cron_last_key:
        return
    _hls_cron_last_key = key
    log_line(f"每日定时任务触发：{HLS_CRON_TZ} {HLS_CRON_HOUR:02d}:{HLS_CRON_MINUTE:02d}（{key}）开始检查缺 HLS 的视频")
    try:
        await sweep_missing_hls(trigger="cron")
    except Exception as e:
        log_line(f"每日定时任务异常：{e}")

async def _hls_cron_loop() -> None:
    """后台常驻协程：按 HLS_CRON_TICK_SECONDS 轮询，命中目标时刻则触发一次"""
    log_line(f"HLS 定时任务已启动：{HLS_CRON_TZ} 每天 {HLS_CRON_HOUR:02d}:{HLS_CRON_MINUTE:02d}（{HLS_CRON_TICK_SECONDS}s 轮询）")
    while True:
        try:
            await hls_cron_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_line(f"HLS 定时任务轮询异常：{e}")
        await asyncio.sleep(HLS_CRON_TICK_SECONDS)

# 时长探测并发度（用于 /videos 首次加载时并发跑 ffprobe，避免 28 个视频串行等待）
# 使用位置：list_video_files 中的 duration_executor.map
DURATION_PROBE_WORKERS = 4
# 时长缓存：key = (绝对路径, size, mtime_ns, HLS 签名) -> duration 秒。
# 使用位置：probe_duration_sync 读/写；避免每次 /videos 重复启动 ffprobe 子进程。
_duration_cache: Dict[tuple, float] = {}
_DURATION_CACHE_LOCK = threading.Lock()
duration_executor = ThreadPoolExecutor(max_workers=DURATION_PROBE_WORKERS, thread_name_prefix="ffprobe")

def _hls_signature(name: str) -> float:
    """HLS m3u8 的 mtime（无 HLS 返回 0），用于让缓存能感知 HLS 生成/更新"""
    try:
        m3u8 = os.path.join(get_hls_dir(), name, "index.m3u8")
        return os.path.getmtime(m3u8) if os.path.exists(m3u8) else 0.0
    except OSError:
        return 0.0

def probe_duration_sync(file_path: str, name: str = "") -> float:
    """获取视频时长（秒）。结果按 (路径, size, mtime, HLS 签名) 缓存，避免重复 ffprobe。"""
    # 缓存 key：文件被替换/修改（size/mtime 变化）或 HLS 生成后自动失效
    try:
        st = os.stat(file_path)
        key = (os.path.abspath(file_path), st.st_size, st.st_mtime_ns, _hls_signature(name))
    except OSError:
        key = None
    if key is not None:
        with _DURATION_CACHE_LOCK:
            cached = _duration_cache.get(key)
        if cached is not None:
            return cached

    duration = 0.0
    # 1) 从 HLS 求和 EXTINF
    if name:
        d = hls_duration_sync(name)
        if d > 0:
            duration = d
    # 2) ffprobe 兜底
    if duration <= 0:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", file_path],
                capture_output=True, text=True, timeout=4
            )
            data = json.loads(result.stdout)
            d = float(data.get("format", {}).get("duration", 0))
            duration = d if d > 0 else 0.0
        except Exception:
            duration = 0.0

    if key is not None:
        with _DURATION_CACHE_LOCK:
            _duration_cache[key] = duration
    return duration

def list_video_files() -> List[dict]:
    """列出所有视频文件 - 与 obs-video-app 格式一致。
    时长探测走缓存；未命中缓存的项用 duration_executor 并发探测（不阻塞调用线程的事件循环）。"""
    upload_dir = get_upload_dir()
    videos = []
    try:
        for name in os.listdir(upload_dir):
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTS:
                p = os.path.join(upload_dir, name)
                if os.path.isfile(p):
                    stat = os.stat(p)
                    videos.append({
                        "name": name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "duration": 0.0,
                        "url": f"/obs/{quote(name)}",
                        "hls": f"/hls/{quote(name)}/index.m3u8",
                        "hlsReady": hls_exists(name)
                    })
    except Exception:
        pass

    # 并发补齐时长：已缓存的会立即返回（缓存命中不启动 ffprobe）
    if videos:
        def _fill(item):
            p = os.path.join(upload_dir, item["name"])
            item["duration"] = probe_duration_sync(p, item["name"])
            return item
        try:
            list(duration_executor.map(_fill, videos))
        except Exception:
            pass
    return videos

def get_video_static_dir() -> str:
    """获取 video_static 目录的绝对路径"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_static")

# 内存存储 Notice 内容
NOTICE_CONTENT = ""
NOTICE_LOCK = asyncio.Lock()

# 上传会话管理器（存储分片哈希和总体哈希）（使用位置：upload_init, upload_chunk, upload_complete, upload_status）
# 结构：{
#   "chunk_hashes": {0: "hash0", 1: "hash1", ...},
#   "overall_hash": "hash",
#   "overall_hash_computed": False,
#   "file_hash_obj": hashlib sha256 object,
#   "total_chunks": int,
#   "uploaded_chunks": set
# }
upload_sessions: Dict[str, dict] = {}
SESSION_LOCK = threading.Lock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: capture env-config to app.state (isolation per server instance)
    app.state.upload_dir = os.environ.get("UPLOAD_DIR", UPLOAD_DIR)
    app.state.chunk_dir = os.path.join(app.state.upload_dir, ".chunks")
    os.makedirs(app.state.upload_dir, exist_ok=True)
    os.makedirs(app.state.chunk_dir, exist_ok=True)
    os.makedirs(get_hls_dir(), exist_ok=True)
    # 启动日志落盘（供主机 logs/server.log 查看）
    log_line(f"OBS web app running on port {PORT} (obs dir: {app.state.upload_dir}, logs dir: {LOG_DIR})")
    # 启动每日 04:00 的 HLS 检查任务（每天检查缺 HLS 的视频并生成分片）
    cron_task = asyncio.create_task(_hls_cron_loop()) if HLS_CRON_ENABLED else None
    yield
    # Shutdown：取消定时任务
    if cron_task is not None:
        cron_task.cancel()
        with suppress(asyncio.CancelledError):
            await cron_task
    log_line("OBS web app shutting down")

app = FastAPI(lifespan=lifespan)

# 跨域：页面可能从 ocs.dimond.top 加载，却把上传/接口请求发往 http://localhost:19082（或反之）。
# 不放开 CORS，分片上传的预检（PUT + X-Chunk-SHA256）会被浏览器拦截。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# WebSocket 连接管理器
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"WebSocket Client connected: {websocket.client}", flush=True)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"WebSocket Client disconnected: {websocket.client}", flush=True)

    async def broadcast(self, message: str, exclude: Optional[WebSocket] = None):
        print(f"Broadcasting update to {len(self.active_connections)} clients", flush=True)
        send_tasks = []
        for connection in self.active_connections:
            if connection != exclude:
                send_tasks.append(connection.send_text(message))
        
        if send_tasks:
            results = await asyncio.gather(*send_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    print(f"Failed to send update to client: {result}", flush=True)

manager = ConnectionManager()

# --- Helper Functions ---

async def get_notice():
    global NOTICE_CONTENT
    return NOTICE_CONTENT

async def update_notice(content: str):
    global NOTICE_CONTENT
    async with NOTICE_LOCK:
        NOTICE_CONTENT = content
    return True

# --- Routes ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # 发送初始化内容
        current_content = await get_notice()
        await websocket.send_text(json.dumps({"type": "init", "content": current_content}))
        
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                msg_type = message.get("type")
                
                if msg_type == "update":
                    new_content = message.get("content", "")
                    await update_notice(new_content)
                    print(f"Notice updated by {websocket.client}. Length: {len(new_content)}", flush=True)
                    # 广播给其他客户端
                    broadcast_msg = json.dumps({"type": "update", "content": new_content})
                    await manager.broadcast(broadcast_msg, exclude=websocket)
                    
                elif msg_type == "reset":
                    default_text = ""
                    await update_notice(default_text)
                    print(f"Notice reset by {websocket.client}", flush=True)
                    # 广播给所有客户端
                    broadcast_msg = json.dumps({"type": "update", "content": default_text})
                    await manager.broadcast(broadcast_msg)
                    
            except json.JSONDecodeError:
                print(f"Invalid JSON received from {websocket.client}", flush=True)
            except Exception as e:
                print(f"Error processing message from {websocket.client}: {e}", flush=True)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket Handler Error: {e}", flush=True)
        manager.disconnect(websocket)

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    # HEAD 也要支持：面板/其它服务用 HEAD 探活，只注册 GET 会返回 405
    # 轻量健康探针：不触碰上传目录/视频解码等重资源，供 docker-compose healthcheck 探测
    return {"status": "ok"}

# ============================================================================
# 平台接口：/ask/claude —— 向 Claude 提问
# 规范（systemreadme.md 核心一/二）：所有 Claude 调用统一走 run_claude.js，
# 这样问答才会记录到 logs/agent_tui.log。禁止直接调用 claude CLI。
# 编码识别：q 含空格或长度 < 50 -> URL 解码；否则 -> base64 解码。
# 成功 200 纯文本；失败 500 + 错误信息。
# ============================================================================
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/home/agent/.claude/workspace/project")
ASK_TIMEOUT_SECONDS = int(os.environ.get("ASK_TIMEOUT_SECONDS", 3600))

def _decode_question(q: str) -> str:
    """按平台规范识别 q 的编码方式并解码。"""
    if " " in q or len(q) < 50:
        return unquote(q)
    try:
        return base64.b64decode(q).decode("utf-8")
    except Exception:
        return unquote(q)

async def _run_claude(question: str):
    """spawn node run_claude.js，返回 (status_code, text)。"""
    msg_b64 = base64.b64encode(question.encode("utf-8")).decode("ascii")
    run_claude = os.path.join(WORKSPACE_DIR, "run_claude.js")
    if not os.path.exists(run_claude):
        return 500, f"run_claude.js not found at {run_claude}"
    env = {
        **os.environ,
        "ANTHROPIC_DISABLE_PREFLIGHT": "1",
        "CLAUDE_CAPTURE_STDIO": "1",
        "CLAUDE_MSG": msg_b64,
        "CLAUDE_PERMISSION_MODE": os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", run_claude,
            cwd=WORKSPACE_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except Exception as e:
        return 500, f"spawn error: {e}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=ASK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        with suppress(Exception):
            proc.kill()
        return 504, "Request timeout"
    if proc.returncode == 0:
        return 200, out.decode("utf-8", "replace").strip()
    return 500, (err.decode("utf-8", "replace").strip() or f"Exit code: {proc.returncode}")

@app.api_route("/ask/claude", methods=["GET", "POST"])
async def ask_claude(request: Request):
    q: Optional[str] = request.query_params.get("q")
    if request.method == "POST":
        raw = await request.body()
        if raw:
            body_text = raw.decode("utf-8", "replace")
            try:
                data = json.loads(body_text)
                if isinstance(data, dict):
                    q = data.get("q") or data.get("message") or q
            except Exception:
                q = body_text or q
    if not q:
        return Response(content="Missing q parameter", status_code=400, media_type="text/plain; charset=utf-8")
    question = _decode_question(q)
    if not question.strip():
        return Response(content="Empty question", status_code=400, media_type="text/plain; charset=utf-8")
    status, text = await _run_claude(question)
    return Response(content=text, status_code=status, media_type="text/plain; charset=utf-8")

@app.get("/notice")
async def get_notice_http():
    content = await get_notice()
    return {"content": content}

@app.post("/notice")
async def update_notice_http(request: Request):
    try:
        data = await request.json()
        if 'content' in data:
            await update_notice(data['content'])
            return {"status": "ok"}
        else:
            raise HTTPException(status_code=400, detail="Missing content")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save_notice")
async def save_notice_file(request: Request):
    content = await get_notice()
    if not content:
        raise HTTPException(status_code=400, detail="Notice is empty")
    
    # Generate filename: YYYYMMDDHHMMSS公告板.txt
    filename = datetime.now().strftime("%Y%m%d%H%M%S") + "公告板.txt"
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    save_path = os.path.join(upload_dir, filename)
    
    try:
        os.makedirs(upload_dir, exist_ok=True)
        async with aiofiles.open(save_path, 'w', encoding='utf-8') as f:
            await f.write(content)
        return {"status": "ok", "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save notice: {str(e)}")

@app.get("/")
async def homepage(request: Request, sort: str = Query("time")):
    # 获取文件列表
    files_list = []
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    if os.path.exists(upload_dir):
        try:
            raw_files = [f for f in os.listdir(upload_dir) if not f.startswith('.')]
            
            if sort == 'ext':
                # 按扩展名排序 (A-Z)
                raw_files.sort(key=lambda x: (os.path.splitext(x)[1].lower(), x))
            else:
                raw_files.sort(key=lambda x: os.path.getmtime(os.path.join(upload_dir, x)), reverse=True)
                
            files_list = raw_files
        except Exception:
            files_list = []

    # 构建HTML
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>文件托管服务</title>
        <style>
            body { font-family: sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }
            h1 { color: #333; }
            /* 首页标题：点击跳转到 /video 视频页（使用位置：<body> 内的 .site-title） */
            .site-title { margin: 0 0 16px 0; font-size: 1.6em; }
            .site-title a { color: #333; text-decoration: none; }
            .site-title a:hover { text-decoration: underline; }
            ul { list-style: none; padding: 0; }
            li { padding: 10px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; }
            a { text-decoration: none; color: #007bff; }
            a:hover { text-decoration: underline; }
            .empty { color: #999; font-style: italic; }
            .actions { display: flex; gap: 10px; }
            .btn-delete { cursor: pointer; background: none; border: none; font-size: 1.2em; }
            .btn-delete:hover { opacity: 0.7; }
            .sort-controls { margin-bottom: 20px; }
            .sort-controls a { margin-right: 15px; font-weight: bold; }
            .sort-controls a.active { color: #333; cursor: default; text-decoration: none; }
            
            /* 公告板样式 */
            .notice-board {
                margin: 20px 0; 
                padding: 10px; 
                border: 1px solid #eee; 
                background: #f9f9f9; 
                position: relative;
            }
            .notice-board textarea {
                width: 100%;
                height: 150px;
                border: 1px solid #ccc;
                border-bottom: none;
                resize: vertical;
                font-family: monospace;
                box-sizing: border-box; /* ensure padding doesn't overflow */
            }
            .notice-copy-btn {
                /* 高度 ×3（原来约 24px） */
                display: flex;
                align-items: center;
                justify-content: center;
                height: 72px;
                box-sizing: border-box;
                margin-top: 0;
                padding: 0 12px;
                border: 1px solid #ccc;
                border-top: none;
                background: #81D8D0;
                color: #fff;
                cursor: pointer;
                flex: 20;
            }
            .notice-copy-btn:hover {
                background: #73cbc3;
            }
            .notice-save-btn {
                /* 高度 ×3（原来约 24px） */
                display: flex;
                align-items: center;
                justify-content: center;
                height: 72px;
                box-sizing: border-box;
                padding: 0 12px;
                border: 1px solid #ccc;
                border-top: none;
                background: #fff;
                color: #333;
                cursor: pointer;
                flex: 1;
            }
            .notice-save-btn:hover {
                background: #f2f2f2;
            }
            .notice-copy-bar {
                position: static;
                padding: 0;
                display: flex;
                gap: 6px;
            }
            .notice-board {
                padding-bottom: 0;
            }
            .btn-close-notice {
                position: absolute;
                top: 5px;
                right: 5px;
                border: none;
                background: transparent;
                cursor: pointer;
                font-size: 16px;
                color: #999;
            }
            .btn-close-notice:hover { color: #333; }
            #ws-status-indicator {
                position: absolute;
                top: 5px;
                left: 5px;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                background-color: red; /* Default to disconnected */
                border: 1px solid #ccc;
            }
            .notice-tools {
                position: absolute;
                bottom: 8px;
                right: 8px;
                z-index: 2;
            }
            .notice-tools button {
                cursor: pointer;
                background: none;
                border: none;
                font-size: 16px;
                color: #999;
            }
            .notice-tools button:hover { color: #333; }
            .notice-copy-bar {
                position: static;
                padding: 0;
                display: flex;
                gap: 6px;
            }
            .notice-board {
                padding-bottom: 0;
            }

            /* 粘贴上传确认对话框（使用位置：showPasteConfirm / cancelPasteUpload） */
            .paste-modal-mask {
                position: fixed;
                top: 0; right: 0; bottom: 0; left: 0;
                background: rgba(0, 0, 0, 0.45);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 9999;
            }
            .paste-modal {
                background: #fff;
                border-radius: 8px;
                padding: 20px 24px;
                min-width: 320px;
                max-width: 90vw;
                box-shadow: 0 8px 30px rgba(0, 0, 0, 0.2);
            }
            .paste-modal h3 { margin: 0 0 8px 0; font-size: 1.05em; color: #333; }
            .paste-modal p { margin: 0 0 6px 0; font-size: 0.9em; color: #666; }
            .paste-file-list {
                margin: 10px 0;
                padding-left: 20px;
                list-style: disc;
                max-height: 30vh;
                overflow: auto;
            }
            .paste-file-list li {
                display: list-item;
                justify-content: flex-start;
                padding: 4px 0;
                border-bottom: 1px dashed #eee;
                font-size: 0.9em;
                color: #333;
            }
            .paste-modal-actions {
                display: flex;
                gap: 10px;
                justify-content: flex-end;
                margin-top: 14px;
            }
            .paste-modal-actions button {
                padding: 6px 18px;
                border-radius: 4px;
                cursor: pointer;
                border: 1px solid #ccc;
            }
            .paste-btn-cancel { background: #fff; color: #333; }
            .paste-btn-cancel:hover { background: #f2f2f2; }
            .paste-btn-ok { background: #4A90D9; border-color: #4A90D9; color: #fff; }
            .paste-btn-ok:hover { background: #3a7ec0; }
            /* 文件夹多选：已选列表 + 醒目的「继续添加」 */
            .folder-pick-hint {
                font-size: 0.82em;
                color: #888;
                margin: 0 0 8px 0;
                line-height: 1.5;
            }
            .folder-group-list {
                margin: 8px 0;
                padding: 0;
                list-style: none;
                max-height: 26vh;
                overflow: auto;
            }
            .folder-group-list li {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 5px 0;
                border-bottom: 1px dashed #eee;
                font-size: 0.9em;
                color: #333;
            }
            .folder-group-list .fg-name {
                flex: 1;
                min-width: 0;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .folder-group-list .fg-meta { color: #999; font-size: 0.82em; white-space: nowrap; }
            .folder-group-list .fg-del {
                border: none;
                background: transparent;
                color: #c00;
                cursor: pointer;
                font-size: 1.1em;
                line-height: 1;
                padding: 0 4px;
            }
            .folder-group-list .fg-del:hover { color: #900; }
            .folder-add-more {
                background: #eaf3fc !important;
                border-color: #4A90D9 !important;
                color: #2a6fb0 !important;
                font-weight: 600;
            }
            .folder-add-more:hover { background: #d9eafb !important; }
            /* 子文件夹勾选（一次选父目录、勾选多个子文件夹分别打包） */
            .folder-sub-list {
                margin: 6px 0 0;
                padding: 0;
                list-style: none;
                max-height: 26vh;
                overflow: auto;
                border: 1px solid #e6e6e6;
                border-radius: 4px;
            }
            .folder-sub-list li { border-bottom: 1px dashed #eee; }
            .folder-sub-list li:last-child { border-bottom: none; }
            .folder-sub-label {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 5px 8px;
                font-size: 0.9em;
                color: #333;
                cursor: pointer;
            }
            .folder-sub-label:hover { background: #f7fbff; }
            .folder-sub-label input[type="checkbox"] { margin: 0; flex: none; }
        </style>
        <script>
            const CHUNK_SIZE_BROWSER = 10 * 1024 * 1024; // 浏览器分片上传大小 10MB
            const UPLOAD_CONCURRENCY = 3;                // 分片并发上传路数
            // 文件夹内相对路径编码：逐段 encodeURIComponent，保留 / 作为目录分隔符
            // 使用位置：uploadOneFileDirect（PUT /api/folder/{id}/file/{relpath}）
            function encodeRelPath(p) {
                return String(p || "").split("/").map(encodeURIComponent).join("/");
            }
            // 单文件在界面上的显示名/上传路径：文件夹上传优先用遍历得到的相对路径；
            // 按子文件夹拆分上传时用 WeakMap 临时覆盖（Root/sub/x → sub/x），不改动原始分组。
            const FILE_REL_OVERRIDES = new WeakMap();
            function fileRelPath(file) {
                if (!file) return "";
                const override = FILE_REL_OVERRIDES.get(file);
                return override || file._relPath || file.webkitRelativePath || file.name || "";
            }
            // crypto.subtle 只在安全上下文（https / localhost）可用；
            // 通过 http://<局域网IP> 等非安全上下文访问时它是 undefined，
            // 若直接调用则大文件（>10MB 走分片）全部上传失败，故这里先探测再决定实现。
            const HAS_SUBTLE = !!(globalThis.crypto && globalThis.crypto.subtle && globalThis.crypto.subtle.digest);
            // 纯 JS SHA-256 兜底实现（非安全上下文用）。输出与 crypto.subtle.digest("SHA-256") 完全一致。
            // 使用位置：sha256Hex() 在 HAS_SUBTLE 为 false 时调用
            function sha256HexJS(bytes) {
                const K = [
                    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
                    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
                    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
                    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
                    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
                    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
                    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
                    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
                ];
                const rotr = (x, n) => ((x >>> n) | (x << (32 - n))) >>> 0;
                let h0=0x6a09e667,h1=0xbb67ae85,h2=0x3c6ef372,h3=0xa54ff53a,
                    h4=0x510e527f,h5=0x9b05688c,h6=0x1f83d9ab,h7=0x5be0cd19;
                const len = bytes.length;
                const totalLen = Math.ceil((len + 9) / 64) * 64; // 补 0x80 + 8 字节长度，再补齐到 64 的倍数
                const m = new Uint8Array(totalLen);
                m.set(bytes);
                m[len] = 0x80;
                const bitsHi = Math.floor(len / 536870912); // len*8 的高 32 位（len/2^29）
                const bitsLo = (len << 3) >>> 0;
                m[totalLen - 8] = (bitsHi >>> 24) & 0xff;
                m[totalLen - 7] = (bitsHi >>> 16) & 0xff;
                m[totalLen - 6] = (bitsHi >>> 8) & 0xff;
                m[totalLen - 5] = bitsHi & 0xff;
                m[totalLen - 4] = (bitsLo >>> 24) & 0xff;
                m[totalLen - 3] = (bitsLo >>> 16) & 0xff;
                m[totalLen - 2] = (bitsLo >>> 8) & 0xff;
                m[totalLen - 1] = bitsLo & 0xff;
                const w = new Uint32Array(64);
                for (let off = 0; off < totalLen; off += 64) {
                    for (let t = 0; t < 16; t++) {
                        const j = off + t * 4;
                        w[t] = ((m[j] << 24) | (m[j + 1] << 16) | (m[j + 2] << 8) | m[j + 3]) >>> 0;
                    }
                    for (let t = 16; t < 64; t++) {
                        const x = w[t - 15], y = w[t - 2];
                        const s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
                        const s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
                        w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
                    }
                    let a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
                    for (let t = 0; t < 64; t++) {
                        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
                        const ch = (e & f) ^ (~e & g);
                        const t1 = (h + S1 + ch + K[t] + w[t]) >>> 0;
                        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
                        const maj = (a & b) ^ (a & c) ^ (b & c);
                        const t2 = (S0 + maj) >>> 0;
                        h=g; g=f; f=e; e=(d + t1) >>> 0; d=c; c=b; b=a; a=(t1 + t2) >>> 0;
                    }
                    h0=(h0+a)>>>0; h1=(h1+b)>>>0; h2=(h2+c)>>>0; h3=(h3+d)>>>0;
                    h4=(h4+e)>>>0; h5=(h5+f)>>>0; h6=(h6+g)>>>0; h7=(h7+h)>>>0;
                }
                return [h0,h1,h2,h3,h4,h5,h6,h7].map(x => x.toString(16).padStart(8, "0")).join("");
            }
            // data 可以是 Blob / ArrayBuffer / TypedArray；只对传入的这一块数据算哈希，不读整个文件
            async function sha256Hex(data) {
                const buf = (data instanceof Blob) ? await data.arrayBuffer() : data;
                const bytes = (buf instanceof Uint8Array) ? buf : new Uint8Array(buf);
                if (HAS_SUBTLE) {
                    const digest = await crypto.subtle.digest("SHA-256", bytes);
                    return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, "0")).join("");
                }
                // http（非安全上下文）下 crypto.subtle 不存在，走纯 JS 兜底，保证手机也能上传
                return sha256HexJS(bytes);
            }
            // 各分片 sha256（hex）拼接后再取一次 sha256，作为整文件指纹。
            // 与「整文件 sha256」等价强度地绑定分片集合与顺序，但只需顺序读一遍文件，内存 O(分片)
            async function fileFingerprint(chunkHashes) {
                return await sha256Hex(new TextEncoder().encode(chunkHashes.join("")));
            }
            // 顺序读取文件、逐片算 sha256：内存只占一个分片（不会把整个文件读进内存，手机上传大文件不会 OOM）
            async function computeChunkHashes(file, chunkSize, totalChunks, onProgress) {
                const hashes = new Array(totalChunks);
                for (let i = 0; i < totalChunks; i++) {
                    const blob = file.slice(i * chunkSize, Math.min((i + 1) * chunkSize, file.size));
                    hashes[i] = await sha256Hex(blob);
                    if (onProgress) onProgress(Math.round((i + 1) / totalChunks * 100));
                }
                return hashes;
            }
            async function deleteFile(filename) {
                if (!confirm(`确定要删除 ${filename} 吗？`)) return;
                try {
                    const response = await obsFetch(`/${filename}`, { method: 'DELETE' });
                    if (response.ok) {
                        window.location.reload();
                    } else {
                        alert('删除失败');
                    }
                } catch (e) {
                    alert('删除出错: ' + e);
                }
            }

            // 单文件直传（≤10MB）：整段上传 + 整段校验（X-File-SHA256，服务端校验后才落账）
            // 且先试 http://localhost:19082，不可达再回退 https://ocs.dimond.top
            // folderId 非空时走文件夹暂存接口，URL 使用相对路径（保留目录层级）
            async function uploadOneFileDirect(file, onProgress, folderId) {
                const base = await obsBase();
                const rel = fileRelPath(file);
                return new Promise((resolve, reject) => {
                    const xhr = new XMLHttpRequest();
                    xhr.upload.onprogress = (e) => {
                        if (e.lengthComputable && onProgress) {
                            onProgress(Math.round(e.loaded / e.total * 100), e.loaded);
                        }
                    };
                    xhr.onload = () => {
                        if (xhr.status === 201) resolve(xhr.responseText);
                        else reject(new Error(xhr.responseText || `HTTP ${xhr.status}`));
                    };
                    xhr.onerror = () => reject(new Error('网络错误'));
                    // 整段校验：直传前算一次整文件 SHA-256，服务端比对通过才保留文件
                    const start = async () => {
                        let digest = '';
                        try {
                            digest = await sha256Hex(file);
                        } catch (e) {
                            digest = '';
                        }
                        const url = folderId
                            ? `${base}/api/folder/${folderId}/file/${encodeRelPath(rel)}`
                            : `${base}/${encodeURIComponent(file.name)}`;
                        xhr.open('PUT', url);
                        if (digest) xhr.setRequestHeader('X-File-SHA256', digest);
                        xhr.send(file);
                    };
                    start();
                });
            }

            // 大文件分片上传（>10MB）：/upload/init + /upload/chunk + /upload/complete，支持秒传/断点续传
            //  - 分片哈希只算一次并缓存复用（重试/重传不再重算、不再重读文件）
            //  - 分片 UPLOAD_CONCURRENCY 路并发上传
            async function uploadOneFileResumable(file, onProgress, folderId) {
                const chunkSize = CHUNK_SIZE_BROWSER;
                const size = file.size;
                const totalChunks = Math.ceil(size / chunkSize) || 1;
                const hashAlgo = "sha256";
                const rel = fileRelPath(file);

                // 1) 单遍顺序读取：拿到每片哈希（内存 O(分片)），并合并成整文件指纹
                const chunkHashes = await computeChunkHashes(file, chunkSize, totalChunks,
                    (pct) => { if (onProgress) onProgress(pct, 0, '校验中'); });
                const hash = await fileFingerprint(chunkHashes);

                // 2) 初始化会话（含秒传判定与已上传分片枚举）
                const initResp = await obsFetch('/upload/init', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: file.name, size, hash_algo: hashAlgo, hash, chunk_size: chunkSize, total_chunks: totalChunks,
                                           folder_id: folderId || null, relpath: folderId ? rel : null })
                });
                if (!initResp.ok) throw new Error('初始化失败: ' + await initResp.text());
                const info = await initResp.json();
                if (info.skip) {                                   // 秒传
                    if (onProgress) onProgress(100, size, '上传中');
                    return info.url;
                }
                const uploadId = info.uploadId;
                const uploaded = new Set(info.uploaded || []);

                // 3) 并发上传缺失分片（哈希复用第 1 步结果，重试也不重算）
                let doneBytes = 0;
                for (const i of uploaded) doneBytes += Math.min(chunkSize, size - i * chunkSize);
                let nextIndex = 0;
                const worker = async () => {
                    while (true) {
                        const i = nextIndex++;
                        if (i >= totalChunks) return;
                        if (uploaded.has(i)) continue;
                        const blob = file.slice(i * chunkSize, Math.min((i + 1) * chunkSize, size));
                        let ok = false;
                        for (let attempt = 0; attempt < 3 && !ok; attempt++) {
                            try {
                                const r = await obsFetch(`/upload/chunk/${encodeURIComponent(uploadId)}/${i}`, {
                                    method: 'PUT',
                                    // 逐片校验用哈希；重试直接复用，避免重复计算
                                    headers: { 'X-Chunk-SHA256': chunkHashes[i] },
                                    body: blob,
                                });
                                ok = r.status === 201;
                            } catch (e) {
                                // 网络/跨域等异常同样进入下一次重试：避免单个分片抖动导致整文件上传失败
                            }
                        }
                        if (!ok) throw new Error('分片上传失败: ' + i);
                        doneBytes += blob.size;
                        if (onProgress) onProgress(Math.round(Math.min(size, doneBytes) / size * 100),
                                                   Math.min(size, doneBytes), '上传中');
                    }
                };
                const workers = [];
                for (let w = 0; w < Math.min(UPLOAD_CONCURRENCY, totalChunks); w++) workers.push(worker());
                await Promise.all(workers);

                // 4) 合并（服务端按分片指纹再校验一次）
                const c = await obsFetch(`/upload/complete/${encodeURIComponent(uploadId)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: file.name, size, total_chunks: totalChunks, chunk_size: chunkSize, hash_algo: hashAlgo, hash,
                                           folder_id: folderId || null, relpath: folderId ? rel : null })
                });
                if (!c.ok) throw new Error('合并失败: ' + await c.text());
                return await c.text();
            }

            // 拖拽/选择文件的统一入口（小文件直传，大文件走分片+秒传）
            // opts.folderId：非空时每个文件落到该文件夹暂存区，路径用 file._relPath
            // opts.noReload：文件夹流程要等打包完成再刷新，这里不自动 reload
            async function uploadFiles(files, opts) {
                if (!files || files.length === 0) return { success: 0, fail: 0, failedNames: [], totalFiles: 0 };
                opts = opts || {};
                const folderId = opts.folderId || null;
                const statusEl = document.getElementById('formUploadStatus');
                const totalFiles = files.length;
                let success = 0, fail = 0;
                const failedNames = [];
                const lastSpeedByFile = {};   // 每个文件最近一次测得的速度（用于阶段切换时沿用）
                let overallBytes = 0, totalBytes = 0;
                for (let i = 0; i < totalFiles; i++) totalBytes += files[i].size;
                for (let i = 0; i < totalFiles; i++) {
                    const file = files[i];
                    const displayName = fileRelPath(file);   // 文件夹上传时显示相对路径，便于看清层级
                    statusEl.textContent = `上传中... (${i + 1}/${totalFiles}) ${displayName} 0%`;
                    // 每个文件一个速度计：滑窗 3s 内算实时速度，全程算平均速度
                    const meter = createSpeedMeter(3000);
                    const report = (pct, loaded, stage) => {
                        const bytes = loaded || 0;
                        const sp = meter(bytes);
                        // 传入字节没变化（如进入「校验中」阶段）时沿用上一次测得的速度，避免闪成 0
                        const shownSpeed = sp.speed > 0 ? sp.speed : (lastSpeedByFile[i] || 0);
                        if (sp.speed > 0) lastSpeedByFile[i] = sp.speed;
                        const overallPct = totalBytes > 0 ? Math.round((overallBytes + bytes) / totalBytes * 100) : 0;
                        statusEl.textContent = reportUploadStatus({
                            stage: stage, index: i + 1, totalFiles: totalFiles, filename: displayName,
                            pct: pct, overallPct: overallPct,
                            speedText: fmtSpeed(shownSpeed)
                        });
                    };
                    try {
                        // ≤10MB：整段校验 + 整段上传；>10MB：分段校验 + 分段上传（断点续传）
                        if (file.size <= CHUNK_SIZE_BROWSER) {
                            await uploadOneFileDirect(file, report, folderId);
                        } else {
                            await uploadOneFileResumable(file, report, folderId);
                        }
                        overallBytes += file.size;
                        success++;
                    } catch (err) {
                        fail++;
                        failedNames.push(displayName + ' (' + err.message + ')');
                    }
                }
                if (fail === 0) {
                    statusEl.textContent = `全部上传成功！(${success}个文件)`;
                    if (!opts.noReload) window.location.reload();
                } else {
                    statusEl.textContent = `上传完成：成功${success}个，失败${fail}个 → ${failedNames.join('; ')}`;
                    if (success > 0 && !opts.noReload) setTimeout(() => window.location.reload(), 1500);
                }
                return { success, fail, failedNames, totalFiles };
            }

            function handleDragUpload(fileList) {
                if (!fileList || fileList.length === 0) return;
                uploadFiles(fileList);
            }

            // 上传速度计：滑动窗口内算「实时速度」，全程算「平均速度」
            // sample(bytes, nowMs) → { speed, avg }（字节/秒）；nowMs 可注入便于测试
            function createSpeedMeter(windowMs) {
                const window = windowMs > 0 ? windowMs : 3000;
                let samples = [];
                let firstT = 0, firstBytes = 0, prevBytes = null;
                return function sample(bytes, nowMs) {
                    const t = (nowMs === undefined) ? performance.now() : nowMs;
                    if (samples.length === 0) { firstT = t; firstBytes = bytes; }
                    samples.push({ t: t, bytes: bytes });
                    // 丢弃滑出窗口的旧样本（至少保留 2 个，避免除零）
                    while (samples.length > 2 && t - samples[0].t > window) samples.shift();
                    const oldest = samples[0];
                    const dt = (t - oldest.t) / 1000;
                    let inst = dt > 0 ? (bytes - oldest.bytes) / dt : 0;
                    // 本次采样字节没有增长（上传停滞 / 进入校验阶段）→ 实时速度归零，避免显示假速度
                    const stalled = (prevBytes !== null && bytes <= prevBytes);
                    prevBytes = bytes;
                    if (stalled) inst = 0;
                    const totalDt = (t - firstT) / 1000;
                    const avg = totalDt > 0 ? (bytes - firstBytes) / totalDt : 0;
                    return { speed: inst > 0 ? inst : 0, avg: avg > 0 ? avg : 0 };
                };
            }

            // 人类可读的传输速度（使用位置：reportUploadStatus）
            function fmtSpeed(bytesPerSec) {
                if (!bytesPerSec || bytesPerSec <= 0 || !isFinite(bytesPerSec)) return '';
                const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
                let n = Number(bytesPerSec), i = 0;
                while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
                return (i === 0 ? String(Math.round(n)) : n.toFixed(1)) + units[i];
            }

            // 统一进度文案（使用位置：uploadFiles 的 report 回调）
            function reportUploadStatus(o) {
                const head = (o.stage || '上传中') + '... (' + o.index + '/' + o.totalFiles + ') ' + o.filename;
                const speed = o.speedText ? ' [' + o.speedText + ']' : '';
                return head + ' ' + o.pct + '%' + speed + ' [总进度 ' + o.overallPct + '%]';
            }

            // 从剪贴板事件中提取文件；无文件（纯文本粘贴）返回空数组
            // 说明：右键菜单「粘贴」与 Ctrl/Cmd+V 都会触发 paste 事件
            function collectPasteFiles(clipboardData) {
                if (!clipboardData) return [];
                const out = [];
                const direct = clipboardData.files;
                if (direct && direct.length) {
                    for (let i = 0; i < direct.length; i++) out.push(direct[i]);
                }
                if (out.length === 0 && clipboardData.items) {
                    for (let i = 0; i < clipboardData.items.length; i++) {
                        const it = clipboardData.items[i];
                        if (it && it.kind === 'file' && typeof it.getAsFile === 'function') {
                            const f = it.getAsFile();
                            if (f) out.push(f);
                        }
                    }
                }
                return out;
            }

            // 剪贴板里的截图/图片常常没有文件名，按 MIME 兜底生成一个
            function guessPasteName(mime, index) {
                const map = {
                    'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
                    'image/webp': 'webp', 'image/bmp': 'bmp', 'image/svg+xml': 'svg',
                    'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/webm': 'webm',
                    'application/pdf': 'pdf', 'text/plain': 'txt', 'application/zip': 'zip'
                };
                let ext = map[mime];
                if (!ext && mime && mime.indexOf('/') > -1) ext = mime.split('/')[1].split('+')[0];
                if (!ext) ext = 'bin';
                const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
                return '粘贴文件-' + ts + '-' + (index + 1) + '.' + ext;
            }

            // 无 name 的剪贴板文件补一个名字，保证后续 uploadFiles 逻辑可用
            function withPasteName(file, index) {
                if (file && file.name) return file;
                try {
                    return new File([file], guessPasteName(file && file.type, index),
                                    { type: (file && file.type) || '' });
                } catch (e) {
                    return file;
                }
            }

            // 人类可读的文件大小（使用位置：showPasteConfirm 列表）
            function fmtSize(bytes) {
                if (bytes === null || bytes === undefined || isNaN(bytes)) return '';
                const units = ['B', 'KB', 'MB', 'GB', 'TB'];
                let n = Number(bytes), i = 0;
                while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
                return (i === 0 ? String(n) : n.toFixed(1)) + units[i];
            }

            // 待确认的剪贴板文件（确认后交给 uploadFiles）
            let pastePendingFiles = null;

            function showPasteConfirm(files) {
                pastePendingFiles = files;
                const listEl = document.getElementById('pasteFileList');
                const countEl = document.getElementById('pasteConfirmCount');
                const modalEl = document.getElementById('pasteConfirmModal');
                if (!listEl || !countEl || !modalEl) return;
                listEl.innerHTML = '';
                for (let i = 0; i < files.length; i++) {
                    const li = document.createElement('li');
                    const nm = files[i].name || guessPasteName(files[i].type, i);
                    li.textContent = nm + ' (' + fmtSize(files[i].size) + ')';
                    listEl.appendChild(li);
                }
                countEl.textContent = files.length;
                modalEl.style.display = 'flex';
            }

            function cancelPasteUpload() {
                pastePendingFiles = null;
                const modalEl = document.getElementById('pasteConfirmModal');
                if (modalEl) modalEl.style.display = 'none';
            }

            function confirmPasteUpload() {
                const files = pastePendingFiles;
                cancelPasteUpload();
                if (files && files.length) uploadFiles(files);
            }

            // ==================================================================
            // 文件夹上传：目录遍历（拖拽/粘贴/选择框）→ 确认弹窗 → 逐个上传 → 打包 zip
            // ==================================================================
            function entryFile(entry) {
                return new Promise((resolve) => {
                    try { entry.file(resolve, () => resolve(null)); }
                    catch (e) { resolve(null); }
                });
            }
            // readEntries 每次只返回一批，必须循环读到空数组为止
            function readAllEntries(reader) {
                return new Promise((resolve, reject) => {
                    const out = [];
                    const step = () => {
                        try {
                            reader.readEntries((batch) => {
                                if (!batch || batch.length === 0) { resolve(out); return; }
                                for (let i = 0; i < batch.length; i++) out.push(batch[i]);
                                step();
                            }, reject);
                        } catch (e) { reject(e); }
                    };
                    step();
                });
            }
            async function walkEntry(entry, prefix, files, emptyDirs) {
                if (!entry) return;
                if (entry.isFile) {
                    const f = await entryFile(entry);
                    if (f) {
                        try { f._relPath = (prefix ? prefix + '/' : '') + entry.name; } catch (e) {}
                        files.push(f);
                    }
                    return;
                }
                if (entry.isDirectory) {
                    const dirPath = (prefix ? prefix + '/' : '') + entry.name;
                    let children = [];
                    try { children = await readAllEntries(entry.createReader()); } catch (e) { children = []; }
                    if (children.length === 0) { emptyDirs.push(dirPath); return; }
                    for (let i = 0; i < children.length; i++) await walkEntry(children[i], dirPath, files, emptyDirs);
                }
            }
            // 把若干顶层 Entry 递归收集为「按顶层文件夹分组」的结果：
            // groups = [{folderName, files, emptyDirs}]，顶层散文件放 looseFiles
            async function collectEntriesFromRoots(roots) {
                const groups = [], looseFiles = [];
                let hasDirs = false;
                for (let i = 0; i < roots.length; i++) {
                    const root = roots[i];
                    if (root && root.isDirectory) {
                        hasDirs = true;
                        const group = { folderName: root.name, files: [], emptyDirs: [] };
                        let children = [];
                        try { children = await readAllEntries(root.createReader()); } catch (e) { children = []; }
                        if (children.length === 0) group.emptyDirs.push(root.name);
                        for (let j = 0; j < children.length; j++) {
                            await walkEntry(children[j], root.name, group.files, group.emptyDirs);
                        }
                        groups.push(group);
                    } else if (root && root.isFile) {
                        const f = await entryFile(root);
                        if (f) {
                            try { f._relPath = f.name; } catch (e) {}
                            looseFiles.push(f);
                        }
                    }
                }
                return { groups: groups, looseFiles: looseFiles, hasDirs: hasDirs };
            }
            async function entriesFromDataTransfer(dt) {
                if (!dt || !dt.items || !dt.items.length) return null;
                const roots = [];
                for (let i = 0; i < dt.items.length; i++) {
                    const it = dt.items[i];
                    if (!it || it.kind !== 'file' || typeof it.webkitGetAsEntry !== 'function') continue;
                    const ent = it.webkitGetAsEntry();
                    if (ent) roots.push(ent);
                }
                if (!roots.length) return null;
                return await collectEntriesFromRoots(roots);
            }
            async function entriesFromClipboard(cb) {
                if (!cb || !cb.items || !cb.items.length) return null;
                const roots = [];
                for (let i = 0; i < cb.items.length; i++) {
                    const it = cb.items[i];
                    if (!it || it.kind !== 'file' || typeof it.webkitGetAsEntry !== 'function') continue;
                    const ent = it.webkitGetAsEntry();
                    if (ent) roots.push(ent);
                }
                if (!roots.length) return null;
                return await collectEntriesFromRoots(roots);
            }

            // File System Access API（showDirectoryPicker，需 https/localhost）。
            // 注意：标准没有目录多选选项，multiple:true 会被浏览器忽略，一次只返回一个目录。
            async function walkDirectoryHandle(dirHandle, prefix, files, emptyDirs) {
                let hasChild = false;
                let iter;
                try {
                    iter = (typeof dirHandle.values === 'function') ? dirHandle.values() : dirHandle.entries();
                } catch (e) { emptyDirs.push(prefix); return; }
                for await (const item of iter) {
                    hasChild = true;
                    let handle = item, name = item && item.name;
                    if (Array.isArray(item)) { name = item[0]; handle = item[1]; }
                    if (!handle) continue;
                    const rel = prefix ? (prefix + '/' + name) : name;
                    if (handle.kind === 'file') {
                        try {
                            const f = await handle.getFile();
                            try { f._relPath = rel; } catch (e) {}
                            files.push(f);
                        } catch (e) {}
                    } else {
                        await walkDirectoryHandle(handle, rel, files, emptyDirs);
                    }
                }
                if (!hasChild) emptyDirs.push(prefix);
            }
            async function collectGroupsFromDirectoryHandles(handles) {
                const groups = [];
                for (let i = 0; i < handles.length; i++) {
                    const h = handles[i];
                    if (!h || h.kind !== 'directory') continue;
                    const group = { folderName: h.name, files: [], emptyDirs: [] };
                    await walkDirectoryHandle(h, h.name, group.files, group.emptyDirs);
                    groups.push(group);
                }
                return groups;
            }
            async function pickDirectoriesNative() {
                if (typeof window.showDirectoryPicker !== 'function') return null;
                try {
                    // 标准不支持目录多选（没有 multiple 选项），一次返回一个目录句柄
                    const picked = await window.showDirectoryPicker({ mode: 'read' });
                    if (!picked) return [];
                    return [picked];
                } catch (e) {
                    if (e && e.name === 'AbortError') return [];  // 用户取消
                    return null;  // 不支持/权限被拒 → 回退 webkitdirectory
                }
            }

            // 文件夹确认弹窗（复用粘贴弹窗样式）；支持一次累积多个文件夹
            let folderPending = null;
            let folderPickAppend = false;

            function groupKey(g) {
                let bytes = 0;
                for (let i = 0; i < g.files.length; i++) bytes += g.files[i].size || 0;
                const first = g.files.length ? fileRelPath(g.files[0]) : '';
                return g.folderName + '|' + g.files.length + '|' + bytes + '|' + first;
            }
            // 把单个根文件夹按「直接子文件夹」拆成多个候选分组。
            // 用途：浏览器目录选择器一次只能选一个文件夹，选父目录后勾选多个子文件夹即可分别打包。
            function subfolderGroups(group) {
                const map = new Map();
                const rootPrefix = group && group.folderName ? (group.folderName + '/') : '';
                const strip = (p) => {
                    let r = String(p || '');
                    if (rootPrefix && r.indexOf(rootPrefix) === 0) r = r.slice(rootPrefix.length);
                    return r;
                };
                const ensure = (name) => {
                    if (!map.has(name)) map.set(name, { folderName: name, files: [], emptyDirs: [] });
                    return map.get(name);
                };
                const groupFiles = (group && group.files) || [];
                for (let i = 0; i < groupFiles.length; i++) {
                    const rel = strip(fileRelPath(groupFiles[i]));
                    const slash = rel.indexOf('/');
                    if (slash <= 0) continue;   // 根目录下的散文件不参与拆分
                    ensure(rel.slice(0, slash)).files.push(groupFiles[i]);
                }
                const emptyDirs = (group && group.emptyDirs) || [];
                for (let i = 0; i < emptyDirs.length; i++) {
                    const rel = strip(emptyDirs[i]);
                    const slash = rel.indexOf('/');
                    if (slash <= 0) continue;
                    ensure(rel.slice(0, slash)).emptyDirs.push(rel);
                }
                return Array.from(map.values());
            }
            // 只有「当前恰好一个根文件夹」时才允许按子文件夹拆分；缓存拆分结果，避免每次渲染都重算
            function ensureSplitGroups(pending) {
                if (!pending) return [];
                const groups = pending.groups || [];
                if (groups.length !== 1) {
                    pending.splitGroups = [];
                    pending._splitFor = null;
                    pending.splitSelected = {};
                    return [];
                }
                if (pending._splitFor !== groups[0]) {
                    pending.splitGroups = subfolderGroups(groups[0]);
                    pending._splitFor = groups[0];
                    const valid = {};
                    for (let i = 0; i < pending.splitGroups.length; i++) valid[pending.splitGroups[i].folderName] = true;
                    const sel = pending.splitSelected || {};
                    for (const name in sel) { if (!valid[name]) delete sel[name]; }
                    pending.splitSelected = sel;
                }
                return pending.splitGroups;
            }
            function toggleSplitFolder(name, checked) {
                if (!folderPending) return;
                if (!folderPending.splitSelected) folderPending.splitSelected = {};
                if (checked) folderPending.splitSelected[name] = true;
                else delete folderPending.splitSelected[name];
                renderFolderConfirm();
            }
            // 确认上传前把勾选的子文件夹转成独立上传组；文件相对路径去掉父目录前缀（Root/sub/x → sub/x）
            function applyFolderSplit(pending) {
                if (!pending) return;
                const splitGroups = pending.splitGroups || [];
                const selected = [];
                for (let i = 0; i < splitGroups.length; i++) {
                    const g = splitGroups[i];
                    if (pending.splitSelected && pending.splitSelected[g.folderName]) selected.push(g);
                }
                if (!selected.length) return;   // 未勾选 → 保持整个文件夹
                const rootGroup = (pending.groups || [])[0];
                const rootPrefix = rootGroup && rootGroup.folderName ? (rootGroup.folderName + '/') : '';
                const strip = (p) => {
                    let r = String(p || '');
                    if (rootPrefix && r.indexOf(rootPrefix) === 0) r = r.slice(rootPrefix.length);
                    return r;
                };
                const loose = (pending.looseFiles || []).slice();
                if (rootGroup) {
                    const rootFiles = rootGroup.files || [];
                    for (let i = 0; i < rootFiles.length; i++) {
                        if (strip(fileRelPath(rootFiles[i])).indexOf('/') < 0) loose.push(rootFiles[i]);
                    }
                }
                const finalGroups = [];
                for (let i = 0; i < selected.length; i++) {
                    const g = selected[i];
                    const files = [];
                    for (let j = 0; j < g.files.length; j++) {
                        const f = g.files[j];
                        FILE_REL_OVERRIDES.set(f, strip(fileRelPath(f)));
                        files.push(f);
                    }
                    finalGroups.push({ folderName: g.folderName, files: files, emptyDirs: (g.emptyDirs || []).slice() });
                }
                pending.groups = finalGroups;
                pending.looseFiles = loose;
                pending.splitSelected = {};
            }
            function renderFolderConfirm() {
                const modalEl = document.getElementById('folderConfirmModal');
                if (!modalEl || !folderPending) return;
                const groups = folderPending.groups;
                const splitGroups = ensureSplitGroups(folderPending);
                const selectedNames = [];
                for (let i = 0; i < splitGroups.length; i++) {
                    const name = splitGroups[i].folderName;
                    if (folderPending.splitSelected && folderPending.splitSelected[name]) selectedNames.push(name);
                }
                const splitMode = selectedNames.length > 0;
                // 拆分模式下只统计勾选的子文件夹；否则统计整个（或累积的多个）文件夹
                const displayGroups = splitMode
                    ? splitGroups.filter((g) => folderPending.splitSelected[g.folderName])
                    : groups;
                const files = [];
                let emptyDirs = 0, total = 0;
                for (let i = 0; i < displayGroups.length; i++) {
                    emptyDirs += (displayGroups[i].emptyDirs || []).length;
                    for (let j = 0; j < displayGroups[i].files.length; j++) {
                        files.push(displayGroups[i].files[j]);
                        total += displayGroups[i].files[j].size || 0;
                    }
                }
                for (let i = 0; i < folderPending.looseFiles.length; i++) {
                    files.push(folderPending.looseFiles[i]);
                    total += folderPending.looseFiles[i].size || 0;
                }
                const names = displayGroups.map((g) => g.folderName).join('、');
                document.getElementById('folderConfirmName').textContent = splitMode
                    ? ('已勾选 ' + selectedNames.length + ' 个子文件夹：' + names)
                    : (groups.length > 1 ? ('已选 ' + groups.length + ' 个：' + names) : (names || '文件夹'));
                document.getElementById('folderConfirmCount').textContent = files.length;
                document.getElementById('folderConfirmSize').textContent = fmtSize(total);
                let extra = '';
                if (emptyDirs) extra += '，含 ' + emptyDirs + ' 个空文件夹';
                if (folderPending.looseFiles.length) extra += '，另有 ' + folderPending.looseFiles.length + ' 个散文件直接上传';
                document.getElementById('folderConfirmEmpty').textContent = extra;
                document.getElementById('folderConfirmWarn').style.display =
                    folderPending.structureLost ? 'block' : 'none';
                // 说明多选方式：目录选择器（showDirectoryPicker）按标准一次只返回一个目录，
                // 没有 multiple 选项；因此提供三种多选方式：勾选子文件夹 / 逐个追加 / 一次拖入多个。
                const secureCtx = !!window.isSecureContext;
                const canSplit = groups.length === 1 && splitGroups.length > 0;
                const hintEl = document.getElementById('folderPickHint');
                if (hintEl) {
                    if (!secureCtx) {
                        hintEl.textContent = '当前页面不是安全上下文（需 https 或 localhost），无法直接选择文件夹。'
                            + '请改用 https://ocs.dimond.top 访问，或把多个文件夹一次拖到页面。';
                    } else {
                        let hintText = '浏览器目录选择器一次只能选一个文件夹（Web 标准限制），可用以下方式多选：';
                        if (canSplit) hintText += '在下方勾选该文件夹内的多个子文件夹分别打包；';
                        hintText += '点「＋ 继续添加文件夹」从其他位置继续添加；或直接把多个文件夹一次拖到页面。'
                            + '每个文件夹会分别打包为一个 zip。';
                        hintEl.textContent = hintText;
                    }
                }
                // 已选文件夹列表（每个可单独移除；按子文件夹拆分时显示勾选项，不显示移除）
                const groupListEl = document.getElementById('folderGroupList');
                if (groupListEl) {
                    groupListEl.innerHTML = '';
                    for (let i = 0; i < displayGroups.length; i++) {
                        const g = displayGroups[i];
                        let gSize = 0;
                        for (let j = 0; j < g.files.length; j++) gSize += g.files[j].size || 0;
                        const li = document.createElement('li');
                        const nameEl = document.createElement('span');
                        nameEl.className = 'fg-name';
                        nameEl.textContent = g.folderName;
                        nameEl.title = g.folderName;
                        const metaEl = document.createElement('span');
                        metaEl.className = 'fg-meta';
                        metaEl.textContent = g.files.length + ' 个文件 · ' + fmtSize(gSize)
                            + ((g.emptyDirs || []).length ? ' · ' + g.emptyDirs.length + ' 空目录' : '');
                        li.appendChild(nameEl);
                        li.appendChild(metaEl);
                        if (!splitMode) {
                            const delEl = document.createElement('button');
                            delEl.type = 'button';
                            delEl.className = 'fg-del';
                            delEl.title = '移除该文件夹';
                            delEl.textContent = '×';
                            delEl.onclick = (function (idx) {
                                return function () { removeFolderGroup(idx); };
                            })(i);
                            li.appendChild(delEl);
                        }
                        groupListEl.appendChild(li);
                    }
                }
                // 子文件夹勾选：一次选择父目录后，可勾选多个子文件夹分别打包
                const splitBox = document.getElementById('folderSplitBox');
                const splitList = document.getElementById('folderSubList');
                const splitNote = document.getElementById('folderSplitNote');
                if (splitBox && splitList) {
                    if (canSplit) {
                        splitBox.style.display = 'block';
                        if (splitNote) {
                            let rootFiles = 0;
                            const rootPrefix = groups[0].folderName ? (groups[0].folderName + '/') : '';
                            const rootGroupFiles = groups[0].files || [];
                            for (let i = 0; i < rootGroupFiles.length; i++) {
                                let rel = String(fileRelPath(rootGroupFiles[i]) || '');
                                if (rootPrefix && rel.indexOf(rootPrefix) === 0) rel = rel.slice(rootPrefix.length);
                                if (rel.indexOf('/') < 0) rootFiles++;
                            }
                            splitNote.textContent = '该文件夹下有 ' + splitGroups.length + ' 个子文件夹：勾选的会分别打包为独立 zip'
                                + '（未勾选的不会上传）；不勾选任何项则把整个文件夹打包成一个 zip。'
                                + (rootFiles ? ' 根目录下 ' + rootFiles + ' 个文件在拆分时会直接上传。' : '');
                        }
                        splitList.innerHTML = '';
                        for (let i = 0; i < splitGroups.length; i++) {
                            const sg = splitGroups[i];
                            let sgSize = 0;
                            for (let j = 0; j < sg.files.length; j++) sgSize += sg.files[j].size || 0;
                            const li = document.createElement('li');
                            const label = document.createElement('label');
                            label.className = 'folder-sub-label';
                            const cb = document.createElement('input');
                            cb.type = 'checkbox';
                            cb.checked = !!(folderPending.splitSelected && folderPending.splitSelected[sg.folderName]);
                            cb.onchange = (function (name) {
                                return function () { toggleSplitFolder(name, this.checked); };
                            })(sg.folderName);
                            const nameEl = document.createElement('span');
                            nameEl.className = 'fg-name';
                            nameEl.textContent = sg.folderName;
                            nameEl.title = sg.folderName;
                            const metaEl = document.createElement('span');
                            metaEl.className = 'fg-meta';
                            metaEl.textContent = sg.files.length + ' 个文件 · ' + fmtSize(sgSize)
                                + ((sg.emptyDirs || []).length ? ' · ' + sg.emptyDirs.length + ' 空目录' : '');
                            label.appendChild(cb);
                            label.appendChild(nameEl);
                            label.appendChild(metaEl);
                            li.appendChild(label);
                            splitList.appendChild(li);
                        }
                    } else {
                        splitBox.style.display = 'none';
                        splitList.innerHTML = '';
                    }
                }
                const listEl = document.getElementById('folderFileList');
                listEl.innerHTML = '';
                const limit = Math.min(files.length, 50);
                for (let i = 0; i < limit; i++) {
                    const li = document.createElement('li');
                    li.textContent = fileRelPath(files[i]) + ' (' + fmtSize(files[i].size) + ')';
                    listEl.appendChild(li);
                }
                if (files.length > limit) {
                    const li = document.createElement('li');
                    li.textContent = '… 等共 ' + files.length + ' 个文件';
                    listEl.appendChild(li);
                }
                const addBtn = document.getElementById('folderAddMoreBtn');
                if (addBtn) {
                    addBtn.style.display = (displayGroups.length || folderPending.looseFiles.length) ? 'inline-block' : 'none';
                    addBtn.textContent = '＋ 继续添加文件夹（已选 ' + displayGroups.length + ' 个）';
                }
                const okBtn = document.getElementById('folderConfirmBtn');
                if (okBtn) {
                    if (splitMode) {
                        okBtn.textContent = '上传并打包（' + selectedNames.length + ' 个 zip）';
                    } else if (groups.length > 1) {
                        okBtn.textContent = '上传并打包（' + groups.length + ' 个文件夹）';
                    } else {
                        okBtn.textContent = '上传并打包';
                    }
                }
                modalEl.style.display = 'flex';
            }
            function removeFolderGroup(index) {
                if (!folderPending || !folderPending.groups[index]) return;
                folderPending.groups.splice(index, 1);
                if (!folderPending.groups.length && !folderPending.looseFiles.length) {
                    cancelFolderUpload();
                    return;
                }
                renderFolderConfirm();
            }
            function addGroupsToPending(groups, looseFiles, structureLost) {
                if (!folderPending) folderPending = { groups: [], looseFiles: [], structureLost: false };
                const seen = {};
                for (let i = 0; i < folderPending.groups.length; i++) {
                    seen[groupKey(folderPending.groups[i])] = true;
                }
                for (let i = 0; i < groups.length; i++) {
                    const g = groups[i];
                    if (!g || (!g.files.length && !(g.emptyDirs || []).length)) continue;
                    const k = groupKey(g);
                    if (seen[k]) continue;  // 重复选择同一文件夹时去重
                    seen[k] = true;
                    folderPending.groups.push(g);
                }
                if (looseFiles && looseFiles.length) {
                    folderPending.looseFiles = folderPending.looseFiles.concat(looseFiles);
                }
                folderPending.structureLost = folderPending.structureLost || !!structureLost;
                renderFolderConfirm();
            }
            // append=true 时把新选文件夹追加到当前待上传列表

            // ------------------------------------------------------------------
            // 上传文件夹的两种方式（使用位置：chooseFolderUploadMode / showFolderConfirm）
            //   flat：打散上传——遍历出每个文件，逐个直接进文件列表（不打包）
            //   zip ：压缩上传——整个文件夹打包成 .tar.gz，作为一个文件上传（保留目录结构）
            // 注意：拖拽/粘贴文件夹仍走原来的「暂存 → 服务端打包 zip」流程，不受这里影响。
            // ------------------------------------------------------------------
            function chooseFolderUploadMode() {
                const m = document.getElementById('folderModeModal');
                if (m) m.style.display = 'block';
            }
            function closeFolderMode() {
                const m = document.getElementById('folderModeModal');
                if (m) m.style.display = 'none';
            }
            function pickFolderMode(mode) {
                closeFolderMode();
                // 上传方式跟着选择器一起传下去（不用全局变量，避免用户取消后污染下一次上传）
                openFolderPicker(false, mode === 'zip' ? 'zip' : 'flat');
            }

            // 纯 JS 生成 tar（ustar），再交给 CompressionStream('gzip') 压缩
            // 注意：不能写 NUL 的反斜杠转义字面量——这段 JS 在 Python 字符串里，会被 Python 先解释成真正的
            // NUL 字节，HTML 解析器再把 NUL 换成 U+FFFD，tar 头就坏了（报「不是 tar 包」）。
            const TAR_NUL = String.fromCharCode(0);
            function tarOctal(value, len) {
                const s = value.toString(8);
                return s.padStart(len - 1, '0') + TAR_NUL;
            }
            function tarHeader(name, size, mtime) {
                const enc = new TextEncoder();
                const buf = new Uint8Array(512);
                let nm = name, prefix = '';
                if (enc.encode(nm).length > 100) {
                    const cut = nm.lastIndexOf('/', 155);
                    if (cut > 0) { prefix = nm.slice(0, cut); nm = nm.slice(cut + 1); }
                }
                const put = (str, off, len) => {
                    const b = enc.encode(str);
                    buf.set(b.subarray(0, len), off);
                };
                put(nm, 0, 100);
                put(tarOctal(0o644, 8), 100, 8);
                put(tarOctal(0, 8), 108, 8);
                put(tarOctal(0, 8), 116, 8);
                put(tarOctal(size, 12), 124, 12);
                put(tarOctal(mtime, 12), 136, 12);
                for (let i = 148; i < 156; i++) buf[i] = 0x20;   // 校验和先填空格
                buf[156] = 0x30;                                  // typeflag '0' = 普通文件
                put('ustar' + TAR_NUL + '00', 257, 8);
                put('root', 265, 32);
                put('root', 297, 32);
                put(prefix, 345, 155);
                let sum = 0;
                for (let i = 0; i < 512; i++) sum += buf[i];
                put(sum.toString(8).padStart(6, '0') + TAR_NUL + ' ', 148, 8);
                return buf;
            }

            // groups: [{folderName, files:[File]}]  looseFiles: [File]
            async function buildTarGz(groups, looseFiles) {
                const parts = [];
                const entries = [];
                for (const g of (groups || [])) {
                    for (const f of (g.files || [])) {
                        let rel = f._relPath || f.webkitRelativePath || f.name;
                        if (g.folderName && !rel.startsWith(g.folderName + '/')) {
                            rel = g.folderName + '/' + rel.replace(/^\/+/, '');
                        }
                        entries.push([rel, f]);
                    }
                }
                for (const f of (looseFiles || [])) entries.push([f.name, f]);

                for (const [rel, f] of entries) {
                    const mtime = Math.floor((f.lastModified || Date.now()) / 1000);
                    parts.push(tarHeader(rel, f.size, mtime));
                    parts.push(f);
                    const pad = (512 - (f.size % 512)) % 512;
                    if (pad) parts.push(new Uint8Array(pad));
                }
                parts.push(new Uint8Array(1024));   // 两个结束块

                const tarBlob = new Blob(parts, { type: 'application/x-tar' });
                const base = (groups && groups[0] && groups[0].folderName) ? groups[0].folderName : 'archive';
                if (typeof CompressionStream === 'function') {
                    const gz = await new Response(tarBlob.stream().pipeThrough(new CompressionStream('gzip'))).blob();
                    return { blob: gz, name: base + '.tar.gz' };
                }
                return { blob: tarBlob, name: base + '.tar' };
            }

            async function zipAndUpload(groups, looseFiles) {
                const statusEl = document.getElementById('formUploadStatus');
                if (statusEl) statusEl.textContent = '正在打包并压缩（' + ((groups || []).length) + ' 个文件夹）…';
                const { blob, name } = await buildTarGz(groups, looseFiles);
                if (statusEl) statusEl.textContent = '压缩完成：' + name + '（' + (blob.size / 1048576).toFixed(2) + ' MB），开始上传…';
                const file = new File([blob], name, { type: 'application/gzip', lastModified: Date.now() });
                return await uploadFiles([file], {});
            }

            // 打散上传：把选中的文件逐个直接放进文件列表（不打包、不压缩）
            // 同名文件自动用「目录__文件名」区分，避免互相覆盖；File 不能改名，所以包一层新 File
            async function flatUpload(groups, looseFiles) {
                const statusEl = document.getElementById('formUploadStatus');
                const originals = [];
                for (const g of (groups || [])) {
                    for (const f of (g.files || [])) originals.push(f);
                }
                for (const f of (looseFiles || [])) originals.push(f);
                if (!originals.length) return;
                const used = new Set();
                const files = [];
                for (const f of originals) {
                    const rel = (fileRelPath(f) || f.name || '').replace(/^\/+/, '');
                    const base = rel.split('/').pop() || f.name;
                    let name = base;
                    if (used.has(name)) name = rel.replace(/\//g, '__');
                    let n = 2;
                    while (used.has(name)) name = rel.replace(/\//g, '__') + '(' + (n++) + ')';
                    used.add(name);
                    let nf = f;
                    if (name !== f.name) {
                        try { nf = new File([f], name, { type: f.type, lastModified: f.lastModified || Date.now() }); }
                        catch (e) { nf = f; }
                    }
                    try { nf._relPath = name; } catch (e) {}
                    files.push(nf);
                }
                if (statusEl) statusEl.textContent = '逐个上传 ' + files.length + ' 个文件…';
                return await uploadFiles(files, {});
            }

            async function showFolderConfirm(groups, looseFiles, structureLost, append, uploadMode) {
                // 「上传文件夹」选定方式后不再逐文件确认：压缩 → 打成一个 tar.gz；打散 → 逐个直接上传
                if (uploadMode === 'zip') {
                    try { return await zipAndUpload(groups, looseFiles); }
                    catch (e) { alert('压缩上传失败：' + e.message); return; }
                }
                if (uploadMode === 'flat') {
                    try { return await flatUpload(groups, looseFiles); }
                    catch (e) { alert('打散上传失败：' + e.message); return; }
                }
                if (!append) folderPending = null;
                addGroupsToPending(groups || [], looseFiles || [], structureLost);
            }
            function cancelFolderUpload() {
                folderPending = null;
                folderPickAppend = false;
                const el = document.getElementById('folderConfirmModal');
                if (el) el.style.display = 'none';
            }
            function confirmFolderUpload() {
                const pending = folderPending;
                if (!pending) { cancelFolderUpload(); return; }
                applyFolderSplit(pending);   // 勾选了子文件夹则转成多个独立上传组
                cancelFolderUpload();
                uploadFolder(pending);
            }
            // 选择文件夹：优先用 File System Access API（https/localhost 可用，但标准规定一次只返回一个目录）；
            // 否则回退 webkitdirectory。两者都靠弹窗「＋ 继续添加文件夹」累积多个，或直接拖拽多个文件夹。
            // 触发 webkitdirectory 原生选择器；上传方式挂在 input 的 dataset 上，由 onchange 带回
            function pickFolderViaInput(addMore, uploadMode) {
                folderPickAppend = !!addMore;
                const inp = document.getElementById('folderInput');
                if (!inp) return;
                try { inp.value = ''; } catch (e) {}
                try { inp.dataset.uploadMode = (uploadMode === 'zip') ? 'zip' : 'flat'; } catch (e) {}
                inp.click();
            }
            async function openFolderPicker(addMore, uploadMode) {
                if (!addMore) { folderPending = null; folderPickAppend = false; }
                // 非安全上下文或浏览器不支持该 API 时，直接同步触发 input，避免丢失用户手势
                if (!window.isSecureContext || typeof window.showDirectoryPicker !== 'function') {
                    pickFolderViaInput(addMore, uploadMode);
                    return;
                }
                const nativeHandles = await pickDirectoriesNative();
                if (nativeHandles === null) {
                    pickFolderViaInput(addMore, uploadMode);
                    return;
                }
                if (!nativeHandles.length) return;  // 用户取消
                let groups = [];
                try { groups = await collectGroupsFromDirectoryHandles(nativeHandles); } catch (e) { groups = []; }
                if (groups.length) showFolderConfirm(groups, [], false, !!addMore, uploadMode);
            }
            // webkitdirectory 回退：webkitRelativePath 已含顶层文件夹名，按首段分组
            function groupsFromFileList(fileList) {
                const map = new Map();
                for (let i = 0; i < fileList.length; i++) {
                    const f = fileList[i];
                    const rel = f.webkitRelativePath || f.name;
                    try { f._relPath = rel; } catch (e) {}
                    const slash = rel.indexOf('/');
                    const rootName = slash >= 0 ? rel.slice(0, slash) : '';
                    if (!map.has(rootName)) {
                        map.set(rootName, { folderName: rootName || '文件夹', files: [], emptyDirs: [] });
                    }
                    map.get(rootName).files.push(f);
                }
                return Array.from(map.values());
            }
            function uploadFolderFromInput(fileList, uploadMode) {
                if (!fileList || !fileList.length) return;
                const groups = groupsFromFileList(fileList);
                showFolderConfirm(groups, [], false, folderPickAppend, uploadMode);
                folderPickAppend = false;
                const inp = document.getElementById('folderInput');
                if (inp) { try { inp.value = ''; delete inp.dataset.uploadMode; } catch (e) {} }
            }

            // 同一文件夹（名称/文件数/总大小/首个文件路径一致）的会话 key：刷新后可续传
            function folderResumeKey(folderName, files, totalBytes) {
                const first = files.length ? fileRelPath(files[0]) : '';
                return 'ocs-folder:' + folderName + ':' + files.length + ':' + totalBytes + ':' + first;
            }
            // 多文件夹上传主流程：逐个 init → 上传 → /zip → 轮询，全部完成后统一刷新
            async function uploadFolder(pending) {
                const statusEl = document.getElementById('formUploadStatus');
                const groups = (pending.groups || []).slice();
                const looseFiles = pending.looseFiles || [];
                const failed = [];
                for (let i = 0; i < groups.length; i++) {
                    const ok = await uploadFolderGroup(groups[i], i + 1, groups.length, statusEl);
                    if (!ok) failed.push(groups[i].folderName || '文件夹');
                }
                if (looseFiles.length) {
                    statusEl.textContent = (groups.length ? '文件夹处理完成，' : '') + '正在上传散文件...';
                    const res = await uploadFiles(looseFiles, { noReload: true });
                    if (res.fail > 0) failed.push(res.failedNames.join('; '));
                }
                if (failed.length) {
                    statusEl.textContent = `上传未完成：${failed.length} 项失败 → ${failed.join('; ')}（暂存已保留，重新选择可续传）`;
                    return;
                }
                if (groups.length || looseFiles.length) {
                    statusEl.textContent = (groups.length > 1
                        ? `全部 ${groups.length} 个文件夹已打包完成`
                        : '上传完成') + '，正在刷新...';
                    setTimeout(() => window.location.reload(), 800);
                }
            }
            async function uploadFolderGroup(group, index, totalGroups, statusEl) {
                const files = group.files || [];
                const folderName = group.folderName || '文件夹';
                const emptyDirs = group.emptyDirs || [];
                if (!files.length && !emptyDirs.length) return true;
                let totalBytes = 0;
                for (let i = 0; i < files.length; i++) totalBytes += files[i].size || 0;
                const prefix = totalGroups > 1 ? `[${index}/${totalGroups}] ${folderName}：` : '';
                const resumeKey = folderResumeKey(folderName, files, totalBytes);
                let resumeId = null;
                try { resumeId = localStorage.getItem(resumeKey); } catch (e) { resumeId = null; }
                statusEl.textContent = prefix + '创建文件夹上传会话...';
                let info;
                try {
                    const initResp = await obsFetch('/api/folder/init', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            folder_name: folderName,
                            total_files: files.length,
                            total_bytes: totalBytes,
                            empty_dirs: emptyDirs,
                            resume_folder_id: resumeId || null
                        })
                    });
                    if (!initResp.ok) { statusEl.textContent = prefix + '创建会话失败: ' + await initResp.text(); return false; }
                    info = await initResp.json();
                } catch (e) {
                    statusEl.textContent = prefix + '创建会话失败: ' + e;
                    return false;
                }
                const folderId = info.folder_id;
                try { localStorage.setItem(resumeKey, folderId); } catch (e) {}
                if (files.length) {
                    const res = await uploadFiles(files, { folderId: folderId, noReload: true });
                    if (res.fail > 0) {
                        statusEl.textContent = `${prefix}上传未完成：成功${res.success}个，失败${res.fail}个 → ${res.failedNames.join('; ')}（暂存已保留，重新选择可续传）`;
                        return false;
                    }
                }
                statusEl.textContent = prefix + '文件上传完成，正在打包 zip...';
                try {
                    const zipResp = await obsFetch('/api/folder/' + folderId + '/zip', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ zip_name: folderName + '.zip' })
                    });
                    if (!zipResp.ok) { statusEl.textContent = prefix + '打包失败: ' + await zipResp.text(); return false; }
                } catch (e) {
                    statusEl.textContent = prefix + '打包失败: ' + e;
                    return false;
                }
                return await pollFolderZip(folderId, resumeKey, prefix);
            }
            async function pollFolderZip(folderId, resumeKey, prefix) {
                const statusEl = document.getElementById('formUploadStatus');
                for (let i = 0; i < 600; i++) {
                    await new Promise((resolve) => setTimeout(resolve, 1000));
                    try {
                        const r = await obsFetch('/api/folder/' + folderId + '/zip/status', { cache: 'no-store' });
                        if (!r.ok) { statusEl.textContent = (prefix || '') + '查询打包状态失败: ' + await r.text(); return false; }
                        const st = await r.json();
                        if (st.status === 'done') {
                            try { localStorage.removeItem(resumeKey); } catch (e) {}
                            statusEl.textContent = `${prefix || ''}打包完成：${st.zip_name} (${fmtSize(st.zip_size || 0)})`;
                            return true;
                        }
                        if (st.status === 'failed') {
                            statusEl.textContent = (prefix || '') + '打包失败: ' + (st.error || '未知错误') + '（暂存已保留，可重试）';
                            return false;
                        }
                        statusEl.textContent = (prefix || '') + '正在打包 zip...';
                    } catch (e) {
                        // 网络抖动：继续轮询
                    }
                }
                statusEl.textContent = (prefix || '') + '打包超时，请稍后刷新页面查看结果';
                return false;
            }

            // 页面任意位置粘贴：优先用 Entry API 还原文件夹结构；纯文本粘贴保持默认行为
            document.addEventListener('paste', async (e) => {
                const cb = (e && e.clipboardData) || window.clipboardData;
                if (!cb) return;
                let entRes = null;
                try { entRes = await entriesFromClipboard(cb); } catch (err) { entRes = null; }
                if (entRes && (entRes.groups.length > 0 || entRes.looseFiles.length > 0)) {
                    e.preventDefault();
                    if (entRes.hasDirs && entRes.groups.length > 0) {
                        showFolderConfirm(entRes.groups, entRes.looseFiles, false, false);
                    } else {
                        showPasteConfirm(entRes.looseFiles.map((f, i) => withPasteName(f, i)));
                    }
                    return;
                }
                const raw = collectPasteFiles(cb);
                if (raw.length === 0) return;
                e.preventDefault();
                const named = raw.map((f, i) => withPasteName(f, i));
                // 单个文件保持原有粘贴弹窗；多个文件但拿不到目录结构时如实提示
                if (named.length === 1) {
                    showPasteConfirm(named);
                } else {
                    showFolderConfirm([{ folderName: '粘贴文件', files: named, emptyDirs: [] }], [], true, false);
                }
            });

            document.addEventListener('DOMContentLoaded', () => {
                const zone = document.getElementById('uploadZone');
                if (!zone) return;
                ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(evt => {
                    zone.addEventListener(evt, (e) => { e.preventDefault(); e.stopPropagation(); });
                });
                ['dragenter', 'dragover'].forEach(evt => {
                    zone.addEventListener(evt, () => { zone.style.borderColor = '#4A90D9'; zone.style.background = '#eef6ff'; });
                });
                ['dragleave', 'drop'].forEach(evt => {
                    zone.addEventListener(evt, () => { zone.style.borderColor = '#ccc'; zone.style.background = '#f9f9f9'; });
                });
                zone.addEventListener('drop', async (e) => {
                    e.preventDefault(); e.stopPropagation();
                    let entRes = null;
                    try { entRes = await entriesFromDataTransfer(e.dataTransfer); } catch (err) { entRes = null; }
                    if (entRes && (entRes.groups.length > 0 || entRes.looseFiles.length > 0)) {
                        if (entRes.groups.length > 0) {
                            showFolderConfirm(entRes.groups, entRes.looseFiles, false, false);
                        } else {
                            handleDragUpload(entRes.looseFiles);
                        }
                        return;
                    }
                    const files = e.dataTransfer.files;
                    if (files.length > 0) handleDragUpload(files);
                });
            });

            async function saveNotice() {
                try {
                    const response = await fetch('/save_notice', { method: 'POST' });
                    if (response.ok) {
                        const data = await response.json();
                        alert(`公告已保存为: ${data.filename}`);
                        window.location.reload();
                    } else {
                        const err = await response.json();
                        alert('保存失败: ' + (err.detail || '未知错误'));
                    }
                } catch (e) {
                    alert('保存出错: ' + e);
                }
            }

            function copyNoticeToClipboard() {
                try {
                    const contentEl = document.getElementById('notice-content');
                    const content = contentEl.value || '';

                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(content)
                            .then(() => {})
                            .catch(() => legacyCopy(contentEl, content));
                    } else {
                        legacyCopy(contentEl, content);
                    }
                } catch (e) {
                    alert('复制出错: ' + e);
                }
            }

            function legacyCopy(el, text) {
                try {
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.style.position = 'fixed';
                    ta.style.top = '-1000px';
                    ta.style.left = '-1000px';
                    document.body.appendChild(ta);
                    ta.focus();
                    ta.select();
                    const ok = document.execCommand('copy');
                    document.body.removeChild(ta);
                    if (!ok) {
                        alert('复制失败，请手动选择文本后复制');
                    }
                } catch (err) {
                    try {
                        el.focus();
                        el.select();
                        const ok2 = document.execCommand('copy');
                        if (!ok2) {
                            alert('复制失败，请手动选择文本后复制');
                        }
                    } catch (err2) {
                        alert('复制失败，请手动选择文本后复制');
                    }
                }
            }

            // Notice Board Logic
            document.addEventListener('DOMContentLoaded', () => {
                const noticeArea = document.getElementById('notice-content');
                const statusIndicator = document.getElementById('ws-status-indicator');
                
                // WebSocket connection
                const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                // 使用当前 host 和 protocol 连接 WebSocket，路径为 /ws
                const wsUrl = `${wsProtocol}//${window.location.host}/ws`;
                
                let ws;
                let isConnected = false;

                function connect() {
                    statusIndicator.style.backgroundColor = 'yellow'; // Connecting
                    statusIndicator.title = `Connecting to ${wsUrl}...`;
                    console.log('Connecting to WebSocket:', wsUrl);
                    ws = new WebSocket(wsUrl);

                    ws.onopen = () => {
                        console.log('WebSocket connected');
                        isConnected = true;
                        statusIndicator.style.backgroundColor = 'green'; // Connected
                        statusIndicator.title = 'Connected';
                    };

                    ws.onmessage = (event) => {
                        console.log('WebSocket message received:', event.data);
                        try {
                            const data = JSON.parse(event.data);
                            if (data.type === 'init' || data.type === 'update') {
                                if (noticeArea.value !== data.content) {
                                    const start = noticeArea.selectionStart;
                                    const end = noticeArea.selectionEnd;
                                    
                                    noticeArea.value = data.content;
                                    
                                    if (document.activeElement === noticeArea) {
                                        noticeArea.setSelectionRange(start, end);
                                    }
                                }
                            }
                        } catch (e) {
                            console.error('Error parsing WebSocket message:', e);
                        }
                    };

                    ws.onclose = () => {
                        console.log('WebSocket disconnected, reconnecting...');
                        isConnected = false;
                        statusIndicator.style.backgroundColor = 'red'; // Disconnected
                        statusIndicator.title = 'Disconnected (Reconnecting...)';
                        setTimeout(connect, 3000);
                    };

                    ws.onerror = (err) => {
                        console.error('WebSocket error:', err);
                        ws.close();
                    };
                }

                connect();

                // 监听输入事件，发送更新
                noticeArea.addEventListener('input', () => {
                    if (ws && isConnected) {
                        ws.send(JSON.stringify({
                            type: 'update',
                            content: noticeArea.value
                        }));
                    }
                });

                // 暴露重置函数给全局作用域
                window.resetNotice = function() {
                    if (ws && isConnected) {
                        ws.send(JSON.stringify({
                            type: 'reset'
                        }));
                    } else {
                        alert('未连接到服务器，无法重置');
                    }
                };
            });
        </script>
        <script>
            // 下载/打开：先试宿主机本地地址 http://localhost:19082，不可用再回退域名 https://ocs.dimond.top
            // 页面文字只展示域名 https://ocs.dimond.top；本地地址仅用于加速探测（二者指向同一服务）
            // 回退必须用 https：页面本身是 https，向 http 域名发 XHR/fetch 会被浏览器按混合内容拦截。
            var DOWNLOAD_HOST_PRIMARY = "__OCS_LOCAL_BASE__";
            var DOWNLOAD_HOST_FALLBACK = "https://ocs.dimond.top";

            // 上传/接口地址：先试 http://localhost:19082，不可达再回退域名 https://ocs.dimond.top
            var OBS_HOSTS = [DOWNLOAD_HOST_PRIMARY, DOWNLOAD_HOST_FALLBACK];
            var _obsBasePromise = null;
            function obsBase() {
                if (!_obsBasePromise) {
                    _obsBasePromise = (async function () {
                        for (var i = 0; i < OBS_HOSTS.length; i++) {
                            var h = OBS_HOSTS[i];
                            try {
                                var ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
                                var t = setTimeout(function () { if (ctl) { try { ctl.abort(); } catch (e) {} } }, 1200);
                                await fetch(h + "/health", { method: "HEAD", mode: "no-cors", cache: "no-store",
                                                             signal: ctl ? ctl.signal : undefined });
                                clearTimeout(t);
                                return h;
                            } catch (e) { /* 换下一个 host */ }
                        }
                        return "";   // 都不可达：回退为相对当前页 origin
                    })();
                }
                return _obsBasePromise;
            }
            async function obsFetch(url, opts) {
                var base = await obsBase();
                return fetch(base + url, opts);
            }
            obsBase();   // 页面加载即预热探测，首次上传无需等待

            function _fileUrl(host, name) {
                return host + "/" + String(name).split("/").map(encodeURIComponent).join("/");
            }

            // 下载/查看地址：页面加载后统一改写为「先 http://localhost:19082，不可达再 https://ocs.dimond.top」。
            // 关键：不能在点击回调里先 preventDefault 再异步探测、之后用 a.click() 触发下载——
            // 程序化点击发生在用户手势之外，会被浏览器当作弹窗拦截（点击「没反应」）；
            // 而复制链接粘贴到地址栏属于直接导航，不受拦截，所以才「粘贴能下、点击不能」。
            // 正确做法：在页面加载时把 href 写好，点击走浏览器原生跳转；服务端响应带
            // Content-Disposition: attachment，原生跳转即直接下载。
            function applyDownloadHosts(base) {
                var links = document.querySelectorAll("a[data-name]");
                for (var i = 0; i < links.length; i++) {
                    var name = links[i].getAttribute("data-name") || "";
                    links[i].href = _fileUrl(base || "", name);   // base 为空则用相对地址（当前 origin）
                }
            }
            function initDownloadHosts() {
                obsBase().then(applyDownloadHosts);
            }
            if (document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", initDownloadHosts);
            } else {
                initDownloadHosts();
            }
        </script>
    </head>
    <body>

        <!-- 设置入口：右上角⚙，开关「每日 HLS 分片任务」（使用位置：本文件的 #settings-fab 样式与脚本） -->
        <style>
            #settings-fab{position:fixed;top:14px;right:16px;z-index:9999;width:38px;height:38px;border-radius:50%;
                border:1px solid #ddd;background:#fff;box-shadow:0 2px 8px rgba(0,0,0,.12);cursor:pointer;
                font-size:19px;line-height:36px;text-align:center;user-select:none}
            #settings-fab:hover{background:#f5f5f5}
            #settings-panel{position:fixed;top:60px;right:16px;z-index:9999;width:290px;background:#fff;
                border:1px solid #e2e2e2;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.16);padding:14px 16px;
                font-size:14px;color:#333}
            #settings-panel[hidden]{display:none}
            #settings-panel .sp-title{font-weight:600;margin-bottom:10px}
            #settings-panel .sp-row{display:flex;align-items:flex-start;gap:10px;cursor:pointer}
            #settings-panel .sp-row span{flex:1;line-height:1.35}
            #settings-panel .sp-row small{color:#999;font-size:12px}
            #settings-panel input[type=checkbox]{width:18px;height:18px;margin-top:2px;cursor:pointer}
            #settings-panel .sp-status{margin-top:10px;font-size:12px;color:#888}
        </style>
        <div id="settings-fab" title="设置" role="button" aria-label="设置">⚙</div>
        <div id="settings-panel" hidden>
            <div class="sp-title">设置</div>
            <label class="sp-row">
                <span>每日 HLS 分片任务<br><small>每天 04:00 检查并为缺失的视频生成 HLS 分片</small></span>
                <input type="checkbox" id="hlsCronToggle">
            </label>
            <div class="sp-status" id="hlsCronStatus">读取中…</div>
        </div>
        <script>
        (function () {
            const fab = document.getElementById('settings-fab');
            const panel = document.getElementById('settings-panel');
            const toggle = document.getElementById('hlsCronToggle');
            const status = document.getElementById('hlsCronStatus');
            if (!fab || !panel || !toggle) return;

            fab.addEventListener('click', (e) => {
                e.stopPropagation();
                panel.hidden = !panel.hidden;
            });
            document.addEventListener('click', (e) => {
                if (!panel.hidden && !panel.contains(e.target) && e.target !== fab) panel.hidden = true;
            });
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape') panel.hidden = true; });

            function render(d) {
                toggle.checked = !!d.enabled;
                const when = ('0' + d.hour).slice(-2) + ':' + ('0' + d.minute).slice(-2);
                status.textContent = d.enabled
                    ? ('已开启 · 每天 ' + when + '（' + d.tz + '）')
                    : '已关闭（默认）';
            }

            async function load() {
                try {
                    const r = await fetch('/api/hls-cron', { cache: 'no-store' });
                    render(await r.json());
                } catch (e) { status.textContent = '读取失败：' + e.message; }
            }

            toggle.addEventListener('change', async () => {
                status.textContent = '保存中…';
                try {
                    const r = await fetch('/api/hls-cron', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ enabled: toggle.checked })
                    });
                    const d = await r.json();
                    render(d);
                } catch (e) { status.textContent = '保存失败：' + e.message; }
            });

            load();
        })();
        </script>
        <!-- 页面标题：点击跳转到 /video 视频页（使用位置：.site-title 样式） -->
        <h1 class="site-title"><a href="/video">文件托管服务</a></h1>


        <!-- 上传文件夹方式选择：打散上传 / 压缩上传（使用位置：chooseFolderUploadMode） -->
        <div id="folderModeModal" style="display:none; position:fixed; inset:0; z-index:10000; background:rgba(0,0,0,.45);">
            <div style="position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); width:min(440px,92vw);
                        background:#fff; border-radius:12px; box-shadow:0 12px 40px rgba(0,0,0,.28); padding:20px 22px;">
                <div style="font-size:16px; font-weight:600; margin-bottom:6px;">上传文件夹</div>
                <div style="font-size:13px; color:#888; line-height:1.5; margin-bottom:16px;">
                    请选择处理方式：打散上传把每个文件单独放进文件列表；压缩上传把整个文件夹打成一个包。
                </div>
                <button type="button" onclick="pickFolderMode('files')" style="display:block; width:100%; text-align:left;
                        padding:12px 14px; margin-bottom:10px; border:1px solid #dcdcdc; border-radius:8px;
                        background:#fff; cursor:pointer; font-size:14px;">
                    <b>打散上传</b><br><span style="font-size:12px; color:#888;">遍历出每个文件逐个上传，文件列表里看到的是一个个文件（不打包；同名自动加目录前缀）</span>
                </button>
                <button type="button" onclick="pickFolderMode('zip')" style="display:block; width:100%; text-align:left;
                        padding:12px 14px; margin-bottom:14px; border:1px solid #dcdcdc; border-radius:8px;
                        background:#fff; cursor:pointer; font-size:14px;">
                    <b>压缩上传</b><br><span style="font-size:12px; color:#888;">整个文件夹打成 <code>.tar.gz</code>，作为一个文件上传（保留目录结构）</span>
                </button>
                <div style="text-align:right;">
                    <button type="button" onclick="closeFolderMode()" style="padding:6px 16px; border:1px solid #ccc;
                            background:#fff; border-radius:6px; cursor:pointer;">取消</button>
                </div>
            </div>
        </div>
        <!-- 公告板模块 -->
        <div class="notice-board">
            <div id="ws-status-indicator" title="Connecting..."></div>
            <button class="btn-close-notice" onclick="resetNotice()" title="重置公告">x</button>
            <textarea id="notice-content" placeholder="公告板..."></textarea>
            <div class="notice-copy-bar">
                <button class="notice-copy-btn" onclick="copyNoticeToClipboard()">复制公告到剪贴板</button>
                <button class="notice-save-btn" onclick="saveNotice()" title="保存公告">保存</button>
            </div>
        </div>

        <div id="uploadZone" style="margin: 20px 0; padding: 30px; border: 2px dashed #ccc; background: #f9f9f9; text-align: center; border-radius: 8px; transition: border-color 0.3s, background 0.3s;">
            <p style="font-size: 0.8em; margin: 0 0 10px 0; color: #999;">拖拽文件/文件夹到此处上传（文件夹可一次拖入多个），或在页面按 Ctrl+V / Cmd+V 粘贴上传</p>
            <p style="font-size: 0.8em; margin: 0 0 10px 0; color: #999;">文件托管上传： <code>curl --upload-file file.txt {url_head}/file.txt</code></p>
            <input type="file" id="formFile" onchange="uploadFiles(this.files)" style="display:none;" multiple>
            <!-- 文件夹上传：https/localhost 优先 showDirectoryPicker（一次一个目录）；webkitdirectory 为回退；弹窗内「继续添加文件夹」累积 -->
            <input type="file" id="folderInput" webkitdirectory directory multiple onchange="uploadFolderFromInput(this.files, this.dataset.uploadMode)" style="display:none;">
            <button type="button" onclick="document.getElementById('formFile').click()" style="cursor:pointer; padding:6px 18px; border:1px solid #ccc; background:#fff; border-radius:4px;">上传文件</button>
            <button type="button" onclick="chooseFolderUploadMode()" title="先选上传方式：逐文件上传，或整个文件夹打包成 tar.gz" style="cursor:pointer; padding:6px 18px; border:1px solid #ccc; background:#fff; border-radius:4px; margin-left:8px;">上传文件夹</button>
            <span id="formUploadStatus" style="display:block; margin-top:8px; font-size:0.85em; color:#999;"></span>
        </div>

        <!-- 粘贴上传确认对话框（默认隐藏；使用位置：showPasteConfirm 显示 / cancelPasteUpload 隐藏 / confirmPasteUpload 确认后走 uploadFiles） -->
        <div id="pasteConfirmModal" class="paste-modal-mask" style="display:none;">
            <div class="paste-modal">
                <h3>检测到剪贴板中的文件</h3>
                <p>共 <b id="pasteConfirmCount">0</b> 个文件，是否上传？</p>
                <ul id="pasteFileList" class="paste-file-list"></ul>
                <div class="paste-modal-actions">
                    <button type="button" class="paste-btn-cancel" onclick="cancelPasteUpload()">取消</button>
                    <button type="button" class="paste-btn-ok" onclick="confirmPasteUpload()">确认上传</button>
                </div>
            </div>
        </div>

        <!-- 文件夹上传确认对话框（默认隐藏；使用位置：showFolderConfirm / cancelFolderUpload / confirmFolderUpload） -->
        <div id="folderConfirmModal" class="paste-modal-mask" style="display:none;">
            <div class="paste-modal">
                <h3>已选择的文件夹（可多选）</h3>
                <p id="folderPickHint" class="folder-pick-hint"></p>
                <p>文件夹：<b id="folderConfirmName"></b></p>
                <ul id="folderGroupList" class="folder-group-list"></ul>
                <div id="folderSplitBox" style="display:none; margin:8px 0;">
                    <p id="folderSplitNote" class="folder-pick-hint"></p>
                    <ul id="folderSubList" class="folder-sub-list"></ul>
                </div>
                <p>共 <b id="folderConfirmCount">0</b> 个文件，合计 <b id="folderConfirmSize">0 B</b><span id="folderConfirmEmpty"></span></p>
                <p id="folderConfirmWarn" style="display:none; color:#c00;">粘贴只拿到文件列表、目录结构已丢失，是否继续扁平上传？</p>
                <ul id="folderFileList" class="paste-file-list"></ul>
                <div class="paste-modal-actions">
                    <button type="button" class="paste-btn-cancel" onclick="cancelFolderUpload()">取消</button>
                    <button type="button" id="folderAddMoreBtn" class="paste-btn-cancel folder-add-more" onclick="openFolderPicker(true)" style="display:none;">＋ 继续添加文件夹</button>
                    <button type="button" id="folderConfirmBtn" class="paste-btn-ok" onclick="confirmFolderUpload()">上传并打包</button>
                </div>
            </div>
        </div>

        <div class="sort-controls">
            排序方式: 
            <a href="?sort=time" class="{time_active}">按时间 (最新)</a>
            <a href="?sort=ext" class="{ext_active}">按扩展名 (A-Z)</a>
        </div>

        <ul>
    """
    
    # 动态设置 active 类
    time_active = "active" if sort != 'ext' else ""
    ext_active = "active" if sort == 'ext' else ""
    html = html.replace("{time_active}", time_active).replace("{ext_active}", ext_active).replace("{url_head}", url_head)
    
    if not files_list:
        html += '<li class="empty">暂无文件</li>'
    else:
        for f in files_list:
            file_url = f"{url_head}/{f}"
            safe_f = html_escape(f)
            # 可预览的扩展名：点击文件名先进预览页（右上角有明显下载按钮）
            if is_previewable(f):
                name_link = (f'<a href="/preview/{quote(f, safe="")}" data-preview="{safe_f}" '
                             f'title="点击预览">{safe_f}</a>')
            else:
                name_link = f'<a href="{file_url}" target="_blank" data-name="{safe_f}">{safe_f}</a>'
            html += f'''
            <li>
                {name_link}
                <span class="actions">
                    <a href="{file_url}" download data-name="{safe_f}">下载</a>
                    <button class="btn-delete" onclick="deleteFile('{safe_f}')" title="删除">🗑️</button>
                </span>
            </li>
            '''
    
    html += """
        </ul>
    </body>
    </html>
    """
    # 宿主机本地直连地址：默认 19082（ocs 卡片），其它卡片用自己的端口，避免把上传发到别的卡片
    html = html.replace("__OCS_LOCAL_BASE__", os.environ.get("OCS_LOCAL_BASE", "http://localhost:19082"))
    return HTMLResponse(content=html)

def make_upload_id(filename: str, size: int, hash_algo: str, file_hash: str) -> str:
    safe_name = filename.replace("/", "_")
    return f"{hash_algo}:{file_hash}:{size}:{safe_name}"

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def chunk_fingerprint(path: str, chunk_size: int) -> str:
    """按 chunk_size 顺序切分文件，对各分片 sha256（hex）拼接后再取一次 sha256，作为整文件指纹。
    流式读取，内存占用 O(chunk_size)，不会把整个文件读进内存。
    与前端 computeChunkHashes + fileFingerprint 的实现一一对应。
    （使用位置：upload_init 秒传判定、upload_complete 整文件校验）"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            part = f.read(chunk_size)
            if not part:
                break
            h.update(hashlib.sha256(part).hexdigest().encode())
    return h.hexdigest()

def _scan_uploaded_parts(up_dir: str):
    """枚举已上传分片（由 upload_init 放进线程池执行，避免阻塞事件循环）"""
    uploaded = []
    chunk_hashes = {}
    try:
        for name in os.listdir(up_dir):
            if name.endswith(".part"):
                try:
                    idx = int(name[:-5])
                    uploaded.append(idx)
                    chunk_hashes[idx] = file_sha256(os.path.join(up_dir, name))
                except Exception:
                    pass
    except Exception:
        uploaded = []
    return uploaded, chunk_hashes

# ============================================================================
# 文件夹上传：暂存区 / manifest / 配额 / 打包
# 流程：/api/folder/init 建会话 → 逐文件上传到 obs/.folder-staging/<id>/data/
#       → /api/folder/<id>/zip 后台打包 → 成功后删暂存，首页只留一个 zip
# ============================================================================

_FOLDER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_LOCK = threading.Lock()
# 文件夹最近活动时间（folder_id -> epoch 秒）：配额清理时绝不删除活跃会话
#   使用位置：_touch_folder / _cleanup_candidates
_folder_activity: Dict[str, float] = {}
_folder_activity_lock = threading.Lock()


def _touch_folder(folder_id: str) -> None:
    with _folder_activity_lock:
        _folder_activity[folder_id] = time.time()


def _folder_last_active(folder_id: str) -> float:
    with _folder_activity_lock:
        return _folder_activity.get(folder_id, 0.0)


def _staging_root() -> str:
    return os.path.join(get_upload_dir(), FOLDER_STAGING_ROOT)


def _check_folder_id(folder_id: str) -> str:
    """校验 folder_id 形态（uuid4().hex），避免借 folder_id 做目录穿越。"""
    if not folder_id or not _FOLDER_ID_RE.match(folder_id):
        raise HTTPException(status_code=400, detail="folder_id 非法")
    return folder_id


def _staging_dir(folder_id: str) -> str:
    return os.path.join(_staging_root(), _check_folder_id(folder_id))


def _staging_data_dir(folder_id: str) -> str:
    return os.path.join(_staging_dir(folder_id), "data")


def _manifest_path(folder_id: str) -> str:
    return os.path.join(_staging_dir(folder_id), "manifest.json")


def _safe_relpath(relpath: str) -> str:
    """把浏览器给的相对路径规范化，拒绝绝对路径 / 目录穿越 / NUL 字节。
    返回 posix 风格（/ 分隔）的相对路径。"""
    if relpath is None:
        raise HTTPException(status_code=400, detail="缺少 relpath")
    if "\x00" in str(relpath):
        raise HTTPException(status_code=400, detail="relpath 含非法字符")
    p = str(relpath).replace("\\", "/").strip()
    if not p:
        raise HTTPException(status_code=400, detail="relpath 不能为空")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise HTTPException(status_code=400, detail="relpath 不能是绝对路径")
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise HTTPException(status_code=400, detail="relpath 不能包含 ..")
        parts.append(seg)
    if not parts:
        raise HTTPException(status_code=400, detail="relpath 不能为空")
    return "/".join(parts)


def _resolve_staging_target(folder_id: str, relpath: str) -> str:
    """规范化 relpath 并解析为暂存 data 目录下的绝对路径，确保不越界。"""
    safe = _safe_relpath(relpath)
    base = os.path.realpath(_staging_data_dir(folder_id))
    target = os.path.realpath(os.path.join(base, *safe.split("/")))
    if target == base or not target.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="relpath 越界")
    return target


def _read_manifest(folder_id: str) -> Optional[dict]:
    try:
        with open(_manifest_path(folder_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_manifest(folder_id: str, manifest: dict) -> None:
    """原子写 manifest（先写临时文件再 replace），避免读到半截 JSON。"""
    path = _manifest_path(folder_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    os.replace(tmp, path)


def _manifest_update(folder_id: str, mutate) -> Optional[dict]:
    """在全局锁内读-改-写 manifest；mutate(manifest) 原地修改。"""
    with _MANIFEST_LOCK:
        manifest = _read_manifest(folder_id)
        if manifest is None:
            return None
        mutate(manifest)
        manifest["updated_at"] = time.time()
        _write_manifest(folder_id, manifest)
        return manifest


def _manifest_mark_file(folder_id: str, relpath: str, size: int, sha256: str,
                        status: str = "done") -> None:
    def _mut(m):
        files = m.setdefault("files", {})
        files[relpath] = {
            "relpath": relpath,
            "size": int(size or 0),
            "sha256": sha256 or "",
            "status": status,
            "updated_at": time.time(),
        }
    _manifest_update(folder_id, _mut)


def _manifest_stats(manifest: dict) -> dict:
    files = manifest.get("files") or {}
    done = sum(1 for v in files.values() if v.get("status") == "done")
    partial = sum(1 for v in files.values() if v.get("status") != "done")
    return {
        "total_files": int(manifest.get("total_files") or 0),
        "done_files": done,
        "partial_files": partial,
        "total_bytes": int(manifest.get("total_bytes") or 0),
        "uploaded_bytes": sum(int(v.get("size") or 0) for v in files.values()
                              if v.get("status") == "done"),
    }


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _staging_entry_size(folder_id: str, path: str) -> int:
    """单个暂存会话的占用：优先用 manifest 声明的大小（避免每次 init 遍历数 GB 目录），
    manifest 不可用时才回退实际遍历；打包中的临时 zip 额外计入。"""
    total = 0
    manifest = _read_manifest(folder_id)
    if manifest is not None:
        try:
            total = max(0, int(manifest.get("total_bytes") or 0))
        except (TypeError, ValueError):
            total = 0
    if total <= 0:
        total = _dir_size(path)
    try:
        total += os.path.getsize(os.path.join(path, ".packing.zip.tmp"))
    except OSError:
        pass
    return total


def _staging_total_bytes() -> int:
    root = _staging_root()
    if not os.path.isdir(root):
        return 0
    total = 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if os.path.isdir(d) and _FOLDER_ID_RE.match(name):
            total += _staging_entry_size(name, d)
    return total


def _cleanup_candidates(protect: Optional[str]) -> List[tuple]:
    """列出可清理的暂存会话 [(last_active, folder_id, size)]，按「最久未活动」升序。
    保护当前会话（protect）、最近仍在活跃的会话（FOLDER_ACTIVE_GRACE_SECONDS 内）以及正在打包的会话。"""
    root = _staging_root()
    if not os.path.isdir(root):
        return []
    now = time.time()
    out = []
    for name in os.listdir(root):
        if name == protect:
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d) or not _FOLDER_ID_RE.match(name):
            continue
        try:
            mtime = os.path.getmtime(os.path.join(d, "manifest.json"))
        except OSError:
            try:
                mtime = os.path.getmtime(d)
            except OSError:
                continue
        last_active = max(mtime, _folder_last_active(name))
        if now - last_active < FOLDER_ACTIVE_GRACE_SECONDS:
            continue   # 最近仍在活动：不删
        with _folder_jobs_lock:
            job = _folder_jobs.get(name) or {}
        if job.get("status") in ("queued", "running"):
            continue   # 正在打包：不删
        out.append((last_active, name, _staging_entry_size(name, d)))
    out.sort(key=lambda x: x[0])
    return out


def _enforce_staging_quota(extra_bytes: int, protect: Optional[str] = None) -> None:
    """确保新增 extra_bytes 后不超过 FOLDER_STAGING_LIMIT。
    空间不足时按「最久未活动优先」清理旧会话；清理后仍不足则拒绝新会话（活跃会话绝不删除）。"""
    if FOLDER_STAGING_LIMIT <= 0:
        return
    extra = max(0, int(extra_bytes or 0))
    total = _staging_total_bytes()
    if total + extra <= FOLDER_STAGING_LIMIT:
        return
    for _last_active, folder_id, size in _cleanup_candidates(protect):
        if total + extra <= FOLDER_STAGING_LIMIT:
            break
        shutil.rmtree(_staging_dir(folder_id), ignore_errors=True)
        total -= size
        with _folder_jobs_lock:
            _folder_jobs.pop(folder_id, None)
        with _folder_activity_lock:
            _folder_activity.pop(folder_id, None)
        log_line(f"文件夹暂存配额清理：删除会话 {folder_id}（{size} 字节，最久未活动）")
    if total + extra > FOLDER_STAGING_LIMIT:
        raise HTTPException(
            status_code=507,
            detail="暂存区空间不足（上限 %d 字节），请等待当前上传/打包完成或稍后重试" % FOLDER_STAGING_LIMIT,
        )


def _sanitize_zip_name(zip_name: Optional[str], folder_name: str) -> str:
    """生成安全的 zip 文件名：去掉路径分隔符与控制字符，补齐 .zip 后缀。"""
    raw = (zip_name or "").strip() or (folder_name or "").strip() or "folder"
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip().strip(".")
    if not raw:
        raw = "folder"
    if not raw.lower().endswith(".zip"):
        raw += ".zip"
    return raw


def _unique_zip_path(upload_dir: str, zip_name: str) -> str:
    """同名 zip 已存在时追加时间戳后缀，避免静默覆盖已有文件。"""
    candidate = os.path.join(upload_dir, zip_name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(zip_name)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = os.path.join(upload_dir, f"{stem}-{stamp}{ext}")
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(upload_dir, f"{stem}-{stamp}-{n}{ext}")
        n += 1
    return candidate


def _free_disk_bytes() -> int:
    try:
        return shutil.disk_usage(get_upload_dir()).free
    except OSError:
        return 0


def _pack_folder_sync(folder_id: str, zip_name: str) -> dict:
    """把暂存 data 目录打包成 zip（阻塞，调用方 offload 到线程）。
    校验：zip 可打开、文件条目数与 manifest 完成数一致、大小 > 0；成功后删除暂存目录。"""
    manifest = _read_manifest(folder_id)
    if manifest is None:
        raise RuntimeError("manifest 不存在")
    data_dir = _staging_data_dir(folder_id)
    if not os.path.isdir(data_dir):
        raise RuntimeError("暂存数据目录不存在")
    expected = int(manifest.get("total_files") or 0)
    done = sum(1 for v in (manifest.get("files") or {}).values() if v.get("status") == "done")
    if expected and done < expected:
        raise RuntimeError(f"仍有文件未上传完成（{done}/{expected}），暂不打包")
    empty_dirs = [d for d in (manifest.get("empty_dirs") or []) if isinstance(d, str)]

    upload_dir = get_upload_dir()
    os.makedirs(upload_dir, exist_ok=True)
    final_path = _unique_zip_path(upload_dir, zip_name)
    tmp_path = os.path.join(_staging_dir(folder_id), ".packing.zip.tmp")

    file_count = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            for root, _dirs, files in os.walk(data_dir):
                for name in files:
                    src = os.path.join(root, name)
                    arc = os.path.relpath(src, data_dir).replace(os.sep, "/")
                    zf.write(src, arcname=arc)
                    file_count += 1
            # 空目录写成显式目录条目，解压后目录结构才完整
            for d in empty_dirs:
                safe = _safe_relpath(d)
                zf.writestr(safe.rstrip("/") + "/", b"")
        size = os.path.getsize(tmp_path)
        if size <= 0:
            raise RuntimeError("打包产物为空")
        # 校验：可打开、无损坏条目、文件条目数与 manifest 完成数一致
        with zipfile.ZipFile(tmp_path, "r") as zf:
            names = zf.namelist()
            actual_files = sum(1 for n in names if not n.endswith("/"))
            if zf.testzip() is not None:
                raise RuntimeError("zip 校验失败（存在损坏条目）")
        if expected and actual_files != expected:
            raise RuntimeError(f"zip 文件数 {actual_files} 与 manifest 完成数 {expected} 不一致")
        os.replace(tmp_path, final_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    # 打包成功：清理暂存（源文件夹不再保留）
    shutil.rmtree(_staging_dir(folder_id), ignore_errors=True)
    with _folder_activity_lock:
        _folder_activity.pop(folder_id, None)
    log_line(f"文件夹打包完成 folder={manifest.get('folder_name')} zip={os.path.basename(final_path)} "
             f"files={file_count} size={size}")
    return {"zip_name": os.path.basename(final_path), "zip_path": final_path, "zip_size": size,
            "file_count": file_count, "folder_name": manifest.get("folder_name")}


async def _run_pack_job(folder_id: str, zip_name: str) -> None:
    with _folder_jobs_lock:
        _folder_jobs[folder_id] = {"status": "running", "zip_name": zip_name,
                                   "started_at": time.time()}
    try:
        result = await asyncio.to_thread(_pack_folder_sync, folder_id, zip_name)
        with _folder_jobs_lock:
            _folder_jobs[folder_id] = {"status": "done", "zip_name": result["zip_name"],
                                       "zip_size": result["zip_size"],
                                       "file_count": result["file_count"],
                                       "url": "/" + quote(result["zip_name"]),
                                       "finished_at": time.time()}
        log_line(f"文件夹打包任务成功 folder_id={folder_id} zip={result['zip_name']}")
    except Exception as e:
        with _folder_jobs_lock:
            _folder_jobs[folder_id] = {"status": "failed", "zip_name": zip_name,
                                       "error": str(e), "finished_at": time.time()}
        log_line(f"文件夹打包任务失败 folder_id={folder_id} error={e}")

@app.post("/upload/init")
async def upload_init(request: Request):
    data = await request.json()
    filename = data.get("filename")
    size = int(data.get("size", 0))
    hash_algo = data.get("hash_algo", "sha256")
    file_hash = data.get("hash")
    total_chunks = int(data.get("total_chunks", 0))
    chunk_size = int(data.get("chunk_size", 0))
    if not filename or not size or not file_hash or chunk_size <= 0 or total_chunks <= 0:
        raise HTTPException(status_code=400, detail="缺少必要参数（filename/size/hash/chunk_size/total_chunks）")
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    chunk_dir = getattr(request.app.state, "chunk_dir", get_chunk_dir())
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(chunk_dir, exist_ok=True)
    # 文件夹上传：目标路径改为暂存区 data/<relpath>（否则仍走首页根目录）
    folder_id = (data.get("folder_id") or "").strip() or None
    relpath = data.get("relpath") or filename
    if folder_id:
        _check_folder_id(folder_id)
        if _read_manifest(folder_id) is None:
            raise HTTPException(status_code=404, detail="文件夹会话不存在")
        relpath = _safe_relpath(relpath)
        _touch_folder(folder_id)
        final_path = _resolve_staging_target(folder_id, relpath)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        target_name = f"{folder_id}/{relpath}"
    else:
        final_path = os.path.join(upload_dir, filename)
        target_name = filename
    if os.path.exists(final_path) and os.path.getsize(final_path) == size:
        # 进行秒传校验（按同一分片大小重算指纹，流式读取不占内存）
        if hash_algo == "sha256":
            existing_hash = await asyncio.to_thread(chunk_fingerprint, final_path, chunk_size)
            if existing_hash == file_hash:
                if folder_id:
                    _manifest_mark_file(folder_id, relpath, size, file_hash, status="done")
                    url = f"/api/folder/{folder_id}/file/{quote(relpath, safe='/')}"
                    log_line(f"文件夹秒传命中 folder={folder_id} file={relpath} size={size} chunks={total_chunks}")
                else:
                    url = f"{url_head}/{filename}"
                    log_line(f"秒传命中 file={filename} size={size} chunks={total_chunks}")
                return JSONResponse({"skip": True, "url": url,
                                     "folder_id": folder_id, "relpath": relpath if folder_id else None})
    upload_id = make_upload_id(target_name, size, hash_algo, file_hash)
    up_dir = os.path.join(chunk_dir, upload_id)
    os.makedirs(up_dir, exist_ok=True)

    # 枚举已上传分片（放线程池，避免阻塞事件循环）
    uploaded, chunk_hashes = await asyncio.to_thread(_scan_uploaded_parts, up_dir)

    # 初始化上传会话（存储分片哈希和总体哈希）
    with SESSION_LOCK:
        upload_sessions[upload_id] = {
            "chunk_hashes": chunk_hashes,
            "overall_hash": None,
            "overall_hash_computed": False,
            "file_hash_obj": hashlib.sha256() if hash_algo == "sha256" else None,
            "total_chunks": total_chunks,
            "uploaded_chunks": set(uploaded),
            "folder_id": folder_id,
            "relpath": relpath if folder_id else None,
        }

    if folder_id:
        _manifest_mark_file(folder_id, relpath, size, file_hash or "", status="partial")

    return JSONResponse({
        "uploadId": upload_id,
        "uploaded": sorted(uploaded),
        "totalChunks": total_chunks,
        "chunkSize": chunk_size,
        "folder_id": folder_id,
        "relpath": relpath if folder_id else None,
    })

@app.put("/upload/chunk/{upload_id}/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request):
    """上传分片：落盘后立即用客户端声明的分片哈希（X-Chunk-SHA256）校验，不一致则丢弃分片并拒绝"""
    if index < 0:
        raise HTTPException(status_code=400, detail="分片序号非法")
    chunk_dir = getattr(request.app.state, "chunk_dir", get_chunk_dir())
    up_dir = os.path.join(chunk_dir, upload_id)
    os.makedirs(up_dir, exist_ok=True)
    part_path = os.path.join(up_dir, f"{index}.part")
    expected_hash = (request.headers.get("x-chunk-sha256") or "").strip().lower()

    with SESSION_LOCK:
        session = upload_sessions.get(upload_id)
    if session and session["total_chunks"] and index >= session["total_chunks"]:
        raise HTTPException(status_code=400, detail=f"分片序号 {index} 超出总数 {session['total_chunks']}")
    if session and session.get("folder_id"):
        _touch_folder(session["folder_id"])

    try:
        # 写入文件
        async with aiofiles.open(part_path, "wb") as f:
            async for chunk in request.stream():
                await f.write(chunk)

        # 逐片校验：必须与客户端声明一致（缺失声明视为不合格，不允许跳过校验）
        actual_hash = await asyncio.to_thread(file_sha256, part_path)
        if not expected_hash or actual_hash != expected_hash:
            try:
                os.remove(part_path)
            except OSError:
                pass
            with SESSION_LOCK:
                if upload_id in upload_sessions:
                    upload_sessions[upload_id]["chunk_hashes"].pop(index, None)
                    upload_sessions[upload_id]["uploaded_chunks"].discard(index)
            detail = "缺少分片哈希声明 X-Chunk-SHA256" if not expected_hash else \
                f"分片 {index} 哈希校验失败"
            raise HTTPException(status_code=422, detail=detail)

        # 校验通过才落账（哈希已经是可信值，直接记录，无需再算一遍）
        with SESSION_LOCK:
            if upload_id in upload_sessions:
                upload_sessions[upload_id]["chunk_hashes"][index] = actual_hash
                upload_sessions[upload_id]["uploaded_chunks"].add(index)

        return Response(content="OK", status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分片写入失败: {str(e)}")

@app.post("/upload/status/{upload_id}")
async def upload_status(upload_id: str):
    """查询上传状态，包括分片哈希和总体哈希"""
    with SESSION_LOCK:
        if upload_id not in upload_sessions:
            raise HTTPException(status_code=404, detail="上传会话不存在")
        
        session = upload_sessions[upload_id]
        
        # 获取分片哈希列表
        chunk_hashes = []
        for i in range(session["total_chunks"]):
            if i in session["chunk_hashes"]:
                chunk_hashes.append(session["chunk_hashes"][i])
            else:
                chunk_hashes.append(None)
        
        return JSONResponse({
            "uploadId": upload_id,
            "totalChunks": session["total_chunks"],
            "uploaded": list(session["uploaded_chunks"]),
            "chunkHashes": chunk_hashes,
            "overallHash": session["overall_hash"],
            "overallHashComputed": session["overall_hash_computed"]
        })

@app.post("/upload/complete/{upload_id}")
async def upload_complete(upload_id: str, request: Request):
    data = await request.json()
    filename = data.get("filename")
    size = int(data.get("size", 0))
    total_chunks = int(data.get("total_chunks", 0))
    chunk_size = int(data.get("chunk_size", 0))
    hash_algo = data.get("hash_algo", "sha256")
    file_hash = data.get("hash")
    if not filename or not size or total_chunks <= 0 or chunk_size <= 0:
        raise HTTPException(status_code=400, detail="缺少必要参数（filename/size/total_chunks/chunk_size）")
    chunk_dir = getattr(request.app.state, "chunk_dir", get_chunk_dir())
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    up_dir = os.path.join(chunk_dir, upload_id)
    if not os.path.exists(up_dir):
        raise HTTPException(status_code=404, detail="上传会话不存在")
    # 会话匹配校验：会话存在时必须与本次声明的分片总数一致（进程重启后会话丢失则跳过，交由下方整文件哈希兜底）
    with SESSION_LOCK:
        session = upload_sessions.get(upload_id)
    if session and session["total_chunks"] and session["total_chunks"] != total_chunks:
        raise HTTPException(
            status_code=409,
            detail=f"上传会话不匹配：会话分片数 {session['total_chunks']}，本次声明 {total_chunks}"
        )
    # 文件夹上传：最终落点改为暂存区 data/<relpath>（会话里记录的优先，防止客户端漏传）
    folder_id = (data.get("folder_id") or "").strip() or None
    relpath = data.get("relpath") or filename
    if session:
        if not folder_id and session.get("folder_id"):
            folder_id = session["folder_id"]
        if session.get("relpath"):
            relpath = session["relpath"]
    if folder_id:
        _check_folder_id(folder_id)
        if _read_manifest(folder_id) is None:
            raise HTTPException(status_code=404, detail="文件夹会话不存在")
        relpath = _safe_relpath(relpath)
        _touch_folder(folder_id)
    # 校验分片完整
    for i in range(total_chunks):
        if not os.path.exists(os.path.join(up_dir, f"{i}.part")):
            raise HTTPException(status_code=409, detail=f"缺少分片 {i}")
    
    # 合并
    tmp_path = os.path.join(up_dir, "__merge.tmp")
    try:
        async with aiofiles.open(tmp_path, "wb") as out:
            for i in range(total_chunks):
                p = os.path.join(up_dir, f"{i}.part")
                async with aiofiles.open(p, "rb") as inp:
                    while True:
                        chunk = await inp.read(1024 * 1024)
                        if not chunk:
                            break
                        await out.write(chunk)
        
        # 校验大小
        real_size = os.path.getsize(tmp_path)
        if real_size != size:
            raise HTTPException(status_code=422, detail="合并后大小不匹配")
        
        # 哈希验证：逐片校验已在 /upload/chunk 完成，这里按同一分片大小重算整文件指纹，
        # 把「服务端拿到的字节」与「客户端声明的指纹」真正绑定（流式读取，内存 O(chunk_size)）
        if hash_algo == "sha256" and file_hash:
            actual_hash = await asyncio.to_thread(chunk_fingerprint, tmp_path, chunk_size)
            if actual_hash != file_hash:
                raise HTTPException(status_code=422, detail="文件指纹校验失败")
        
        # 移动到最终位置（文件夹上传 → 暂存区 data/<relpath>）
        if folder_id:
            final_path = _resolve_staging_target(folder_id, relpath)
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
        else:
            final_path = os.path.join(upload_dir, filename)
        os.replace(tmp_path, final_path)

        # 清理分片和会话
        try:
            for i in range(total_chunks):
                os.remove(os.path.join(up_dir, f"{i}.part"))
            os.remove(os.path.join(up_dir, "__merge.tmp")) if os.path.exists(os.path.join(up_dir, "__merge.tmp")) else None
            os.rmdir(up_dir)
        except Exception:
            pass

        # 清理会话
        with SESSION_LOCK:
            if upload_id in upload_sessions:
                del upload_sessions[upload_id]

        if folder_id:
            _manifest_mark_file(folder_id, relpath, size, file_hash or "", status="done")
            url = f"/api/folder/{folder_id}/file/{quote(relpath, safe='/')}"
            log_line(f"上传完成(文件夹分片) folder={folder_id} file={relpath} size={size} chunks={total_chunks}")
            return JSONResponse({"ok": True, "url": url, "filename": filename,
                                 "folder_id": folder_id, "relpath": relpath})
        url = f"/obs/{quote(filename)}"
        log_line(f"上传完成(分片) file={filename} size={size} chunks={total_chunks} -> {final_path}")
        return JSONResponse({"ok": True, "url": url, "filename": filename})
    except HTTPException as he:
        log_line(f"上传失败(分片合并) file={filename} status={he.status_code} detail={he.detail}")
        raise
    except Exception as e:
        log_line(f"上传失败(分片合并) file={filename} error={e}")
        raise HTTPException(status_code=500, detail=f"合并失败: {str(e)}")

# ============================================================================
# 文件夹上传 API：init / manifest / 文件直传 / 打包 / 打包状态
# 注意：这些路由都带 /api/folder 前缀且为多段路径，不会与 /{filename} 冲突
# ============================================================================

@app.post("/api/folder/init")
async def folder_init(request: Request):
    """创建（或复用）文件夹上传会话，返回 folder_id。
    前端把 folder_id 传入后续的单文件/分片上传接口，文件落到暂存区。"""
    data = await request.json()
    folder_name = (data.get("folder_name") or "").strip() or "folder"
    total_files = int(data.get("total_files", 0))
    total_bytes = int(data.get("total_bytes", 0))
    empty_dirs_raw = data.get("empty_dirs") or []
    if total_files <= 0 and not empty_dirs_raw:
        raise HTTPException(status_code=400, detail="文件夹为空")
    if total_files < 0 or total_bytes < 0:
        raise HTTPException(status_code=400, detail="参数非法")
    empty_dirs = []
    for d in empty_dirs_raw:
        try:
            empty_dirs.append(_safe_relpath(d))
        except HTTPException:
            continue

    # 断点续传：前端把上次的 folder_id 存在 localStorage，重新选同一文件夹时带上
    resume_id = (data.get("resume_folder_id") or "").strip() or None
    if resume_id and _FOLDER_ID_RE.match(resume_id):
        manifest = _read_manifest(resume_id)
        if (manifest is not None
                and int(manifest.get("total_files") or 0) == total_files
                and int(manifest.get("total_bytes") or 0) == total_bytes
                and manifest.get("status") == "uploading"):
            _touch_folder(resume_id)
            await asyncio.to_thread(_enforce_staging_quota, 0, resume_id)
            stats = _manifest_stats(manifest)
            log_line(f"文件夹上传会话复用 folder={folder_name} id={resume_id} "
                     f"done={stats['done_files']}/{stats['total_files']}")
            return JSONResponse({"folder_id": resume_id, "folder_name": manifest.get("folder_name"),
                                 "total_files": total_files, "total_bytes": total_bytes,
                                 "empty_dirs": manifest.get("empty_dirs") or [],
                                 "resume": True, **stats})

    # 配额：按预计总大小检查，必要时按「最久未活动优先」清理旧会话
    await asyncio.to_thread(_enforce_staging_quota, total_bytes, None)

    folder_id = uuid.uuid4().hex
    os.makedirs(_staging_data_dir(folder_id), exist_ok=True)
    manifest = {
        "folder_id": folder_id,
        "folder_name": folder_name,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "empty_dirs": sorted(set(empty_dirs)),
        "files": {},
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "uploading",
    }
    _write_manifest(folder_id, manifest)
    _touch_folder(folder_id)
    log_line(f"文件夹上传会话创建 folder={folder_name} id={folder_id} "
             f"files={total_files} bytes={total_bytes} empty_dirs={len(empty_dirs)}")
    return JSONResponse({"folder_id": folder_id, "folder_name": folder_name,
                         "total_files": total_files, "total_bytes": total_bytes,
                         "empty_dirs": manifest["empty_dirs"], "resume": False,
                         **_manifest_stats(manifest)})


@app.get("/api/folder/{folder_id}/manifest")
async def folder_manifest(folder_id: str):
    """查询文件夹上传进度（已完成/未完成文件、字节数、空目录）。"""
    _check_folder_id(folder_id)
    manifest = _read_manifest(folder_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="文件夹会话不存在")
    stats = _manifest_stats(manifest)
    return JSONResponse({**manifest, **stats})


@app.put("/api/folder/{folder_id}/file/{relpath:path}")
async def folder_file_put(folder_id: str, relpath: str, request: Request):
    """文件夹内单文件直传（≤10MB 走此路径）：写入暂存区，可选 X-File-SHA256 整段校验。"""
    _check_folder_id(folder_id)
    if _read_manifest(folder_id) is None:
        raise HTTPException(status_code=404, detail="文件夹会话不存在")
    safe = _safe_relpath(relpath)
    save_path = _resolve_staging_target(folder_id, safe)
    expected_hash = (request.headers.get("x-file-sha256") or "").strip().lower()
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    try:
        async with aiofiles.open(save_path, "wb") as out_file:
            total_written = 0
            async for chunk in request.stream():
                await out_file.write(chunk)
                total_written += len(chunk)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")
        if expected_hash:
            actual_hash = await asyncio.to_thread(file_sha256, save_path)
            if actual_hash != expected_hash:
                try:
                    os.remove(save_path)
                except OSError:
                    pass
                log_line(f"上传失败(文件夹PUT-校验) folder={folder_id} file={safe} 整段 SHA-256 不一致")
                raise HTTPException(status_code=400, detail="整段 SHA-256 校验失败")
        recorded = expected_hash or await asyncio.to_thread(file_sha256, save_path)
        _manifest_mark_file(folder_id, safe, total_written, recorded, status="done")
        _touch_folder(folder_id)
        log_line(f"上传完成(文件夹PUT) folder={folder_id} file={safe} bytes={total_written}")
        return Response(content=f"/api/folder/{folder_id}/file/{quote(safe, safe='/')}",
                        media_type="text/plain", status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        log_line(f"上传失败(文件夹PUT) folder={folder_id} file={safe} error={e}")
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


@app.post("/api/folder/{folder_id}/zip")
async def folder_zip(folder_id: str, request: Request):
    """后台把暂存文件夹打包成单个 zip；成功后删除暂存目录，首页只留下一份 zip。"""
    _check_folder_id(folder_id)
    manifest = _read_manifest(folder_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="文件夹会话不存在")
    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    zip_name = _sanitize_zip_name(data.get("zip_name"), manifest.get("folder_name") or "folder")
    stats = _manifest_stats(manifest)
    if stats["total_files"] and stats["done_files"] < stats["total_files"]:
        raise HTTPException(
            status_code=409,
            detail=f"仍有文件未上传完成（{stats['done_files']}/{stats['total_files']}），暂不打包",
        )
    # 打包前检查磁盘余量（源 + zip 保守 ×2）；有 manifest 声明大小就不遍历大目录
    try:
        source_size = sum(int(f.get("size") or 0) for f in (manifest.get("files") or {}).values())
    except (TypeError, ValueError):
        source_size = 0
    if source_size <= 0:
        source_size = await asyncio.to_thread(_dir_size, _staging_data_dir(folder_id))
    need = source_size * 2 + 16 * 1024 * 1024
    free = await asyncio.to_thread(_free_disk_bytes)
    if free and free < need:
        raise HTTPException(status_code=507, detail=f"磁盘空间不足（需要约 {need} 字节，剩余 {free} 字节）")
    with _folder_jobs_lock:
        existing = _folder_jobs.get(folder_id)
        if existing and existing.get("status") in ("queued", "running"):
            return JSONResponse({"status": existing["status"], "folder_id": folder_id,
                                 "zip_name": existing.get("zip_name")})
        _folder_jobs[folder_id] = {"status": "queued", "zip_name": zip_name,
                                   "queued_at": time.time()}
    _manifest_update(folder_id, lambda m: m.__setitem__("status", "packing"))
    _touch_folder(folder_id)
    asyncio.create_task(_run_pack_job(folder_id, zip_name))
    log_line(f"文件夹打包任务已排队 folder={manifest.get('folder_name')} id={folder_id} zip={zip_name}")
    return JSONResponse({"status": "started", "folder_id": folder_id, "zip_name": zip_name})


@app.get("/api/folder/{folder_id}/zip/status")
async def folder_zip_status(folder_id: str):
    """查询打包进度；完成后返回 zip 名称/大小/下载地址。"""
    _check_folder_id(folder_id)
    with _folder_jobs_lock:
        job = dict(_folder_jobs.get(folder_id) or {})
    if not job:
        manifest = _read_manifest(folder_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="文件夹会话不存在")
        stats = _manifest_stats(manifest)
        return JSONResponse({"status": "idle", "folder_id": folder_id, **stats})
    return JSONResponse({"folder_id": folder_id, **job})

@app.post("/")
async def upload_file_form(request: Request):
    # Flexible file upload handler
    try:
        form = await request.form()
        
        # Find the first UploadFile field
        upload_file: UploadFile = None
        for key, value in form.items():
            # Duck typing check for UploadFile (has filename and file attribute)
            if hasattr(value, "filename") and hasattr(value, "file"):
                upload_file = value
                break
        
        if not upload_file:
            # Fallback for "file" param if it was somehow passed differently or check body
            raise HTTPException(status_code=422, detail="No file field found in form data")

        filename = upload_file.filename
        if not filename:
            raise HTTPException(status_code=400, detail="Filename is empty")
            
        upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
        save_path = os.path.join(upload_dir, filename)
        
        async with aiofiles.open(save_path, 'wb') as out_file:
            # 使用 10MB 分片读取并写入；若设置了 MAX_UPLOAD_SIZE，则进行累计校验
            total_written = 0
            while content := await upload_file.read(UPLOAD_CHUNK_SIZE):
                os.makedirs(upload_dir, exist_ok=True)
                await out_file.write(content)
                total_written += len(content)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")
        
        log_line(f"上传完成(表单) file={filename} bytes={total_written} -> {save_path}")
        return Response(content=f"文件上传成功: {url_head}/{filename}", media_type="text/plain", status_code=201)
        
    except Exception as e:
        log_line(f"上传失败(表单) error={e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@app.put("/{filename}")
async def upload_file_put(filename: str, request: Request):
    """整段直传（≤10MB 走此路径）。
    若客户端声明了 X-File-SHA256（整段校验），落盘后必须比对通过，否则删除文件并报错；
    未声明则视为旧客户端（如 curl --upload-file），保持兼容不做校验。"""
    filename = unquote(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    save_path = os.path.join(upload_dir, filename)
    expected_hash = (request.headers.get("x-file-sha256") or "").strip().lower()
    try:
        async with aiofiles.open(save_path, 'wb') as out_file:
            # 保持与客户端流大小一致；若设置了 MAX_UPLOAD_SIZE，则进行累计校验
            total_written = 0
            async for chunk in request.stream():
                os.makedirs(upload_dir, exist_ok=True)
                await out_file.write(chunk)
                total_written += len(chunk)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")

        # 整段校验：声明了整文件 SHA-256 就比对，不通过则删除，避免留下损坏文件
        if expected_hash:
            actual_hash = await asyncio.to_thread(file_sha256, save_path)
            if actual_hash != expected_hash:
                try:
                    os.remove(save_path)
                except OSError:
                    pass
                log_line(f"上传失败(PUT-校验) file={filename} 整段 SHA-256 不一致")
                raise HTTPException(status_code=400, detail="整段 SHA-256 校验失败")

        file_url = f"{url_head}/{filename}"
        log_line(f"上传完成(PUT) file={filename} bytes={total_written} -> {save_path}")
        return Response(content=file_url, media_type="text/plain", status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        log_line(f"上传失败(PUT) file={filename} error={e}")
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

# ============================================================================
# 视频页面（使用 obs-video-app master 分支的 UI）
# 注意：/{filename} 路由必须在 /video 等特定路由之后定义，避免被错误匹配
# ============================================================================

@app.get("/video")
async def video_page():
    """视频页面 - 使用静态文件"""
    static_dir = get_video_static_dir()
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Video page not found</h1>", status_code=404)

@app.get("/video/style.css")
async def video_css():
    """视频页面 CSS"""
    static_dir = get_video_static_dir()
    css_path = os.path.join(static_dir, "style.css")
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/css")
    return Response(content="Not found", status_code=404)

@app.get("/video/app.js")
async def video_js():
    """视频页面 JS"""
    static_dir = get_video_static_dir()
    js_path = os.path.join(static_dir, "app.js")
    if os.path.exists(js_path):
        with open(js_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="application/javascript")
    return Response(content="Not found", status_code=404)

@app.get("/video/vendor/hls.min.js")
async def video_hls_js():
    """HLS.js 库"""
    static_dir = get_video_static_dir()
    hls_path = os.path.join(static_dir, "vendor", "hls.min.js")
    if os.path.exists(hls_path):
        with open(hls_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="application/javascript")
    return Response(content="Not found", status_code=404)

# ============================================================================
# 视频 API（兼容 obs-video-app master 分支）
# ============================================================================

@app.get("/obs/{filename:path}")
async def obs_file(filename: str):
    """访问 obs 目录下的文件"""
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, unquote(filename))
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@app.get("/videos")
async def videos_list():
    """获取视频列表。
    走 asyncio.to_thread：list_video_files 内部有 ffprobe 等阻塞调用，
    放在线程池执行，避免阻塞事件循环拖慢正在播放的视频流（多客户端并发时的卡顿根因）。"""
    return JSONResponse({"videos": await asyncio.to_thread(list_video_files)})

@app.post("/hls/generate-all")
async def hls_generate_all(dry_run: bool = Query(False)):
    """触发一次全量扫描：为所有缺（或过期）HLS 的视频生成分片。
    - 默认：后台串行生成，立即返回待处理队列（与旧版 obs-video-app 行为一致）。
    - ?dry_run=true：只返回待处理队列、不启动生成（便于运维先确认影响范围）。
    注意：本路由必须在 /hls/{filename}/{path} 之前声明。"""
    pending = await asyncio.to_thread(_list_missing_hls)
    total = await asyncio.to_thread(_count_videos)
    if not pending:
        log_line(f"HLS 扫描[manual]：无待处理视频（共 {total} 个视频）")
        return JSONResponse({"total": total, "pending": 0, "queue": [],
                             "done": [], "failed": [], "started": False})
    if dry_run:
        log_line(f"HLS 扫描[manual][dry-run]：{len(pending)} 个视频待生成（未启动）")
        return JSONResponse({"total": total, "pending": len(pending),
                             "queue": pending, "started": False})
    asyncio.create_task(sweep_missing_hls(trigger="manual"))
    log_line(f"HLS 扫描[manual]：已排队 {len(pending)} 个视频（串行生成）")
    return JSONResponse({"total": total, "pending": len(pending),
                         "queue": pending, "started": True})

@app.get("/hls/cron")
async def hls_cron_status():
    """每日 HLS 检查任务的调度信息（便于运维确认 04:00 定时任务已生效）"""
    return JSONResponse({
        "enabled": HLS_CRON_ENABLED,
        "hour": HLS_CRON_HOUR,
        "minute": HLS_CRON_MINUTE,
        "timezone": HLS_CRON_TZ,
        "tickSeconds": HLS_CRON_TICK_SECONDS,
        "lastFiredKey": _hls_cron_last_key,
    })

@app.get("/hls/{filename}/{path:path}")
async def hls_playlist(filename: str, path: str):
    """HLS 播放列表和分片"""
    hls_dir = os.path.join(get_hls_dir(), filename)
    file_path = os.path.join(hls_dir, path)

    if not os.path.exists(os.path.join(hls_dir)):
        raise HTTPException(status_code=404, detail="HLS 目录不存在")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    if path.endswith(".m3u8"):
        return FileResponse(file_path, media_type="application/vnd.apple.mpegurl")
    else:
        return FileResponse(file_path, media_type="video/mp2t")

@app.post("/compress/{filename}")
async def compress_video(filename: str):
    """压缩视频"""
    filename = unquote(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="视频不存在")

    # 后台执行压缩
    asyncio.create_task(run_compress_async(filename))
    return JSONResponse({"ok": True, "message": "压缩任务已启动"})

async def run_compress_async(filename: str):
    """后台执行视频压缩"""
    import tempfile
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    tmp_out = os.path.join(upload_dir, f".comp-{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4")

    try:
        args = [
            "ffmpeg", "-y", "-i", file_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p", "-vf", "scale='min(1920,iw)':-2",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            tmp_out
        ]
        await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if os.path.exists(tmp_out) and os.path.getsize(tmp_out) < os.path.getsize(file_path):
            os.replace(tmp_out, file_path)
    except Exception as e:
        print(f"Compression failed: {e}")
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except:
                pass

# ============================================================================
# 文件预览路由（/{filename} 之前声明，避免被后面 catch-all 抢先匹配）
# 需求：点击 csv/xls/xlsx/doc/docx/pdf/html/jpeg/png/mp3 等文件先进入预览页，
#       页面右上角有明显的「下载」按钮（下载走 /{filename}，服务端强制 attachment）。
# ============================================================================

PREVIEW_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "avif", "jfif"}
PREVIEW_AUDIO_EXTS = {"mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus"}
PREVIEW_VIDEO_EXTS = {"mp4", "webm", "mov", "m4v", "ogv", "mkv"}
PREVIEW_PDF_EXTS = {"pdf"}
PREVIEW_HTML_EXTS = {"html", "htm"}
PREVIEW_TEXT_EXTS = {"txt", "csv", "tsv", "json", "md", "log", "xml", "yml", "yaml",
                     "ini", "conf", "cfg", "toml", "env", "py", "js", "mjs", "cjs",
                     "ts", "tsx", "jsx", "css", "sh", "bash", "sql", "c", "h", "cpp",
                     "hpp", "cc", "java", "go", "rs", "rb", "php", "vue", "bat", "ps1",
                     "lua", "kt", "swift", "scala", "pl", "dart", "r"}
# 允许在预览页直接编辑并保存的后缀（纯文本；写入统一按 UTF-8）。
EDITABLE_TEXT_EXTS = {"txt", "md"}
# 在线编辑的体积上限，避免把超大文件整段读进内存。
MAX_EDIT_TEXT_BYTES = 5 * 1024 * 1024
PREVIEW_OFFICE_EXTS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf"}

PREVIEW_EXTS = (PREVIEW_IMAGE_EXTS | PREVIEW_AUDIO_EXTS | PREVIEW_VIDEO_EXTS |
                PREVIEW_PDF_EXTS | PREVIEW_HTML_EXTS | PREVIEW_TEXT_EXTS | PREVIEW_OFFICE_EXTS)


def file_ext(name: str) -> str:
    """取文件名扩展名（小写，不含点）。无扩展名返回空串。"""
    base = os.path.basename(name)
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[1].strip().lower()


def is_previewable(name: str) -> bool:
    return file_ext(name) in PREVIEW_EXTS


def preview_kind(name: str) -> str:
    e = file_ext(name)
    if e in PREVIEW_IMAGE_EXTS:
        return "image"
    if e in PREVIEW_AUDIO_EXTS:
        return "audio"
    if e in PREVIEW_VIDEO_EXTS:
        return "video"
    if e in PREVIEW_PDF_EXTS:
        return "pdf"
    if e in PREVIEW_HTML_EXTS:
        return "html"
    if e in PREVIEW_TEXT_EXTS:
        return "text"
    if e in PREVIEW_OFFICE_EXTS:
        return "office"
    return "other"


def is_editable_text(name: str, size: int = 0) -> bool:
    """txt/md 且不超过编辑体积上限时，允许在预览页直接编辑保存。"""
    return file_ext(name) in EDITABLE_TEXT_EXTS and 0 <= size <= MAX_EDIT_TEXT_BYTES


def safe_upload_path(upload_dir: str, filename: str) -> str:
    """把 filename 解析为 upload_dir 下的绝对路径；越界（../ 等）或非法返回空串。"""
    base = os.path.realpath(upload_dir)
    target = os.path.realpath(os.path.join(base, filename))
    if target == base or target.startswith(base + os.sep):
        return target
    return ""


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ---- Office 文档在线预览 ----
# 不依赖 LibreOffice 等 Office 套件，改用纯 Python 库把文档转成 HTML 片段，
# 再由预览页用 iframe 内嵌显示（思路参考 qq939/interview-jobfinder：服务端转 HTML + iframe 渲染）。
#   docx → mammoth；xlsx/xlsm → openpyxl；xls → xlrd；pptx → python-pptx；
#   其余（doc/ppt/odt/ods/odp/rtf）无纯 Python 解析器，退化为下载占位页。
OFFICE_CONVERTIBLE_EXTS = {"docx", "xlsx", "xlsm", "xls", "pptx"}
OFFICE_MAX_BYTES = 25 * 1024 * 1024      # 超过则不转换，退化为下载占位
OFFICE_MAX_ROWS = 3000                   # 表格最多渲染行数
_OFFICE_HTML_CACHE = {}
_OFFICE_HTML_CACHE_LOCK = threading.Lock()
_OFFICE_HTML_CACHE_MAX = 24


def _cell_text(val) -> str:
    """单元格取值转文本：None→空串；整数值的浮点去掉多余的 .0（xlrd 会把数字读成 float）。"""
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _rows_to_table(rows) -> str:
    """把二维行/列数据渲染成 HTML 表格；首行作表头，最多 OFFICE_MAX_ROWS 行。"""
    out, truncated = [], False
    for i, row in enumerate(rows):
        if i >= OFFICE_MAX_ROWS:
            truncated = True
            break
        cells = []
        for val in row:
            tag = "th" if i == 0 else "td"
            cells.append("<" + tag + ">" + html_escape(_cell_text(val)) + "</" + tag + ">")
        out.append("<tr>" + "".join(cells) + "</tr>")
    table = "<table>" + "".join(out) + "</table>"
    if truncated:
        table += '<p class="muted">（内容过多，仅显示前 %d 行）</p>' % OFFICE_MAX_ROWS
    return table


def _sheet_sections(sections) -> str:
    """渲染多 sheet 工作簿：顶部 sheet 切换条 + 每张表独立锚点区块（逐个都可预览）。
    sections 为 (标题, 行数据) 的可迭代对象。"""
    nav, body, count = [], [], 0
    for title, rows in sections:
        count += 1
        sid = "sheet-%d" % count
        nav.append('<a href="#%s">%s</a>' % (sid, html_escape(title)))
        body.append('<h3 id="%s">%s</h3>' % (sid, html_escape(title)))
        body.append(_rows_to_table(rows))
    if count <= 1:
        # 单 sheet 无需切换条
        return "".join(body)
    return ('<nav class="sheet-nav">' + "".join(nav) + "</nav>" + "".join(body))


def office_to_html(file_path: str) -> str:
    """用纯 Python 库把受支持的 Office 文件转成 HTML 片段；不支持/失败返回空串。"""
    ext = file_ext(file_path)
    try:
        if ext == "docx":
            import mammoth
            with open(file_path, "rb") as f:
                return mammoth.convert_to_html(f).value
        if ext in ("xlsx", "xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
            try:
                # 每张 sheet 都渲染（顶部切换条 + 各自锚点区块）
                return _sheet_sections(
                    (ws.title, ws.iter_rows(values_only=True)) for ws in wb.worksheets)
            finally:
                wb.close()
        if ext == "xls":
            import xlrd
            bk = xlrd.open_workbook(file_path)
            return _sheet_sections(
                (sh.name,
                 ([sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)))
                for sh in bk.sheets())
        if ext == "pptx":
            from pptx import Presentation
            parts = []
            for i, slide in enumerate(Presentation(file_path).slides, 1):
                parts.append("<h3>第 %d 页</h3>" % i)
                buf = []
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        txt = (shape.text_frame.text or "").strip()
                        if txt:
                            buf.append("<p>" + "<br>".join(html_escape(txt).split("\n")) + "</p>")
                parts.append("".join(buf) or '<p class="muted">（本页无文本）</p>')
            return "".join(parts)
    except Exception as e:
        log_line(f"office 预览转换失败: {file_path} {e}")
        return ""
    return ""


def office_html_cached(file_path: str) -> str:
    """按 mtime+size 失效的内存缓存；失败结果（空串）也缓存，避免反复触发转换。"""
    try:
        st = os.stat(file_path)
    except OSError:
        return ""
    key = (file_path, st.st_mtime_ns, st.st_size)
    with _OFFICE_HTML_CACHE_LOCK:
        if key in _OFFICE_HTML_CACHE:
            return _OFFICE_HTML_CACHE[key]
    result = office_to_html(file_path)
    with _OFFICE_HTML_CACHE_LOCK:
        if len(_OFFICE_HTML_CACHE) >= _OFFICE_HTML_CACHE_MAX:
            _OFFICE_HTML_CACHE.clear()
        _OFFICE_HTML_CACHE[key] = result
    return result


_OFFICE_DOC_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
    body { margin: 0; padding: 20px 26px 48px; background: #fff; color: #222; line-height: 1.65;
           font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
           font-size: 15px; word-wrap: break-word; }
    h1, h2, h3 { margin: 1.1em 0 .5em; }
    h3 { font-size: 16px; color: #1f4fa3; border-bottom: 1px solid #e6e6e6; padding-bottom: 4px; }
    p { margin: .6em 0; }
    img { max-width: 100%; height: auto; }
    table { border-collapse: collapse; width: 100%; margin: .6em 0; font-size: 13.5px; }
    th, td { border: 1px solid #d9d9d9; padding: 6px 10px; text-align: left; vertical-align: top; }
    th { background: #f2f5fa; }
    tr:nth-child(even) td { background: #fafbfd; }
    .muted { color: #999; font-size: 13px; }
    .sheet-nav { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; gap: 6px;
                 margin: -4px 0 14px; padding: 8px 0; background: #fff; border-bottom: 1px solid #e6e6e6; }
    .sheet-nav a { padding: 4px 12px; border: 1px solid #cdd8ea; border-radius: 14px;
                   background: #f2f5fa; color: #1f4fa3; text-decoration: none; font-size: 13px; }
    .sheet-nav a:hover { background: #e4ebf7; }
</style>
</head>
<body>
{{BODY}}
</body>
</html>
"""


_PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; height: 100%; }
    body { background: #0f1115; color: #e6e6e6; height: 100vh; display: flex; flex-direction: column;
           font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
    .topbar { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; padding: 10px 16px;
              background: #171a21; border-bottom: 1px solid #262b36; }
    .btn-back { flex: 0 0 auto; color: #9aa2b1; text-decoration: none; font-size: 14px; padding: 6px 10px; border-radius: 6px; }
    .btn-back:hover { background: #222732; color: #e6e6e6; }
    .name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            font-size: 14px; color: #cfd3dc; }
    .name .meta { color: #7d8494; font-size: 12px; margin-left: 10px; }
    .btn-download { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 6px; background: #2f6feb;
                    color: #fff; text-decoration: none; padding: 9px 18px; border-radius: 8px; font-size: 14px;
                    font-weight: 600; box-shadow: 0 2px 10px rgba(47,111,235,.35); }
    .btn-download:hover { background: #3b7cf7; }
    .btn-edit { flex: 0 0 auto; display: none; align-items: center; gap: 6px; background: #262b36; color: #e6e6e6;
                border: 1px solid #39404f; padding: 8px 16px; border-radius: 8px; font-size: 14px; cursor: pointer; }
    .btn-edit:hover { background: #303744; }
    .btn-edit[disabled] { opacity: .55; cursor: default; }
    .save-state { flex: 0 0 auto; color: #7d8494; font-size: 12px; white-space: nowrap; }
    .save-state.ok { color: #4ade80; }
    .save-state.err { color: #ff7b72; }
    .stage { flex: 1 1 auto; min-height: 0; position: relative; overflow: auto; padding: 16px;
             display: flex; align-items: center; justify-content: center; }
    .stage img { max-width: 100%; max-height: 100%; object-fit: contain; background: #000;
                 box-shadow: 0 4px 24px rgba(0,0,0,.45); }
    .stage video { max-width: 100%; max-height: 100%; background: #000; }
    .stage audio { width: min(720px, 100%); }
    iframe.frame { width: 100%; height: 100%; border: 0; background: #fff; border-radius: 8px; }
    .csv-wrap, .text-wrap, .editor-wrap { width: 100%; height: 100%; overflow: auto; border: 1px solid #262b36; border-radius: 10px; background: #12151b; }
    .editor-wrap { display: none; flex-direction: column; }
    .editor-head { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 10px 16px;
                   background: #171a21; border-bottom: 1px solid #262b36; color: #9aa2b1; font-size: 13px; }
    .editor-head input { flex: 1 1 auto; min-width: 0; background: #12151b; color: #e6e6e6; border: 1px solid #39404f;
                         border-radius: 6px; padding: 7px 10px; font-size: 13px; font-family: inherit; }
    .editor-head input:focus { outline: none; border-color: #2f6feb; }
    .editor-head .ext-tip { flex: 0 0 auto; color: #7d8494; font-size: 12px; }
    pre.text { margin: 0; padding: 18px 22px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
               font-size: 13px; line-height: 1.6; white-space: pre; tab-size: 4; }
    textarea.editor { display: block; flex: 1 1 auto; min-height: 0; width: 100%; margin: 0; padding: 18px 22px; border: 0; outline: none;
                      resize: none; overflow: auto; background: #12151b; color: #e6e6e6;
                      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                      font-size: 13px; line-height: 1.6; white-space: pre; tab-size: 4; }
    table.csv { border-collapse: collapse; width: 100%; font-size: 13px; }
    table.csv th, table.csv td { border: 1px solid #2a3040; padding: 6px 10px; text-align: left; white-space: nowrap; }
    table.csv th { background: #1c2230; position: sticky; top: 0; }
    table.csv tr:nth-child(even) td { background: #161a22; }
    .fallback { max-width: 560px; text-align: center; padding: 40px 28px; background: #171a21;
                border: 1px solid #262b36; border-radius: 14px; }
    .fallback .icon { font-size: 56px; line-height: 1; margin-bottom: 14px; }
    .fallback h2 { margin: 0 0 8px; font-size: 18px; word-break: break-all; }
    .fallback p { margin: 0 0 22px; color: #9aa2b1; font-size: 14px; }
    .fallback .btn-download { padding: 12px 26px; font-size: 15px; }
    .err { color: #ff7b72; }
</style>
</head>
<body>
<div class="topbar">
    <a class="btn-back" href="/">← 返回</a>
    <div class="name" title="{{NAME}}"><span id="fileNameText">{{NAME}}</span><span class="meta" id="fileMeta">{{META}}</span></div>
    <span class="save-state" id="saveState"></span>
    <button class="btn-edit" id="editBtn" type="button">✎ 编辑</button>
    <a class="btn-download" id="dlBtn" href="{{DOWNLOAD_URL}}" download>⬇ 下载</a>
</div>
<div class="stage">
{{BODY}}
</div>
<script>
(function () {
    var inlineUrl = {{INLINE_JSON}};
    var kind = {{KIND_JSON}};
    var delim = {{DELIM_JSON}};
    var editable = {{EDITABLE_JSON}};
    var saveUrl = {{SAVE_JSON}};
    var fileExt = {{EXT_JSON}};
    var currentName = {{FILENAME_JSON}};
    var editBtn = document.getElementById("editBtn");
    var saveState = document.getElementById("saveState");
    var textView = document.getElementById("textView");
    var editorWrap = document.getElementById("editorWrap");
    var editor = document.getElementById("editorText");
    var nameInput = document.getElementById("fileNameInput");
    var fileNameText = document.getElementById("fileNameText");
    var dlBtn = document.getElementById("dlBtn");
    var loadedText = null;
    var editing = false;
    var dirty = false;

    function decodeResp(r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); }
    function fmtSize(n) {
        var v = Number(n) || 0;
        if (v < 1024) { return v + " B"; }
        var units = ["KB", "MB", "GB", "TB"], i = -1;
        do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
        return v.toFixed(1) + " " + units[i];
    }
    function setSaveState(msg, cls) {
        if (!saveState) { return; }
        saveState.textContent = msg || "";
        saveState.className = "save-state" + (cls ? " " + cls : "");
    }
    function encPath(name) {
        return String(name).split("/").map(encodeURIComponent).join("/");
    }
    function baseName(name) {
        var s = String(name || "");
        var i = s.lastIndexOf("/");
        return i >= 0 ? s.slice(i + 1) : s;
    }
    function updateNameUi(newName) {
        currentName = newName;
        saveUrl = "/api/text/" + encPath(newName);
        fileExt = (baseName(newName).split(".").pop() || "").toLowerCase();
        if (fileNameText) { fileNameText.textContent = baseName(newName); }
        if (nameInput) { nameInput.value = baseName(newName); }
        if (dlBtn) { dlBtn.href = "/" + encodeURIComponent(newName); }
        document.title = baseName(newName) + " - 预览";
        try { history.replaceState(null, "", "/preview/" + encPath(newName)); } catch (e) {}
    }
    function validateName(name) {
        if (!name) { return "文件名不能为空"; }
        if (name === "." || name === "..") { return "文件名不合法"; }
        if (name.indexOf("/") >= 0 || name.indexOf(String.fromCharCode(92)) >= 0) { return "文件名不能包含路径分隔符"; }
        if (name.length > 200) { return "文件名过长"; }
        if (!/[.](txt|md)$/i.test(name)) { return "仅支持 .txt / .md 后缀"; }
        return "";
    }
    function apiErr(r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
            throw new Error("HTTP " + r.status + (d && d.detail ? "：" + d.detail : ""));
        });
    }
    function enterEdit() {
        if (loadedText === null || !editorWrap || !editor) { return; }
        editor.value = loadedText;
        if (nameInput) { nameInput.value = baseName(currentName); }
        editing = true; dirty = false;
        if (textView) { textView.style.display = "none"; }
        editorWrap.style.display = "flex";
        editBtn.textContent = "💾 保存";
        setSaveState("文件名与正文均可修改 · Ctrl+S 保存", "");
        editor.focus();
    }
    function exitEdit() {
        editing = false; dirty = false;
        if (editorWrap) { editorWrap.style.display = "none"; }
        if (textView) { textView.style.display = "block"; }
        if (editBtn) { editBtn.textContent = "✎ 编辑"; }
        setSaveState("", "");
    }
    function saveEdit() {
        if (!editing || !editor) { return; }
        var value = editor.value;
        var newBase = nameInput ? nameInput.value.trim() : baseName(currentName);
        var nameErr = nameInput ? validateName(newBase) : "";
        if (nameErr) { setSaveState(nameErr, "err"); return; }
        editBtn.disabled = true;
        setSaveState("保存中…", "");
        var renamePromise = Promise.resolve();
        if (newBase !== baseName(currentName)) {
            renamePromise = fetch("/api/text/" + encPath(currentName) + "/rename", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ new_name: newBase })
            }).then(function (r) {
                if (!r.ok) { return apiErr(r); }
                return r.json();
            }).then(function (data) {
                updateNameUi(data.filename);
            });
        }
        renamePromise.then(function () {
            return fetch(saveUrl, {
                method: "PUT",
                headers: { "Content-Type": "text/plain;charset=utf-8" },
                body: value
            });
        }).then(function (r) {
            if (!r.ok) { return apiErr(r); }
            return r.json();
        }).then(function (data) {
            loadedText = value;
            var pre = document.getElementById("previewText");
            if (pre) { pre.textContent = value; }
            var meta = document.getElementById("fileMeta");
            if (meta) { meta.textContent = fmtSize(data.size) + (fileExt ? " · " + fileExt.toUpperCase() : ""); }
            editBtn.disabled = false;
            exitEdit();
            setSaveState("已保存 · " + fmtSize(data.size), "ok");
            setTimeout(function () { if (!dirty) { setSaveState("", ""); } }, 4000);
        }).catch(function (e) {
            editBtn.disabled = false;
            setSaveState("保存失败：" + e.message, "err");
        });
    }
    if (editable && editBtn && editor) {
        editBtn.style.display = "inline-flex";
        editBtn.disabled = true;
        editBtn.addEventListener("click", function () {
            if (editing) { saveEdit(); } else { enterEdit(); }
        });
        editor.addEventListener("input", function () {
            dirty = true;
            setSaveState("未保存", "");
        });
        if (nameInput) {
            nameInput.addEventListener("input", function () {
                dirty = true;
                setSaveState("未保存", "");
            });
            nameInput.addEventListener("keydown", function (e) {
                if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
                    e.preventDefault(); saveEdit();
                }
            });
        }
        editor.addEventListener("keydown", function (e) {
            if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
                e.preventDefault(); saveEdit();
            } else if (e.key === "Escape") {
                if (dirty && !window.confirm("放弃未保存的修改？")) { return; }
                exitEdit();
            }
        });
        window.addEventListener("beforeunload", function (e) {
            if (dirty) { e.preventDefault(); e.returnValue = ""; }
        });
    }
    function escapeHtml(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }
    function parseCsv(text, d) {
        var rows = [], row = [], cur = "", inQ = false, i, code, ch, n = text.length;
        for (i = 0; i < n; i++) {
            ch = text[i];
            code = text.charCodeAt(i);
            if (inQ) {
                if (ch === '"') {
                    if (text[i + 1] === '"') { cur += '"'; i++; continue; }
                    inQ = false; continue;
                }
                cur += ch; continue;
            }
            if (ch === '"') { inQ = true; continue; }
            if (ch === d) { row.push(cur); cur = ""; continue; }
            if (code === 10) { row.push(cur); rows.push(row); row = []; cur = ""; continue; }
            if (code === 13) { continue; }
            cur += ch;
        }
        row.push(cur);
        if (!(row.length === 1 && row[0] === "")) { rows.push(row); }
        return rows;
    }
    function renderCsv(text) {
        var wrap = document.getElementById("csvWrap");
        if (!wrap) { return; }
        var rows = parseCsv(text, delim);
        if (!rows.length) { wrap.innerHTML = '<pre class="text">（空文件）</pre>'; return; }
        var maxCols = 0, r, c, val, tag, html = '<table class="csv">';
        for (r = 0; r < rows.length; r++) { if (rows[r].length > maxCols) { maxCols = rows[r].length; } }
        for (r = 0; r < rows.length; r++) {
            html += '<tr>';
            for (c = 0; c < maxCols; c++) {
                val = rows[r][c] === undefined ? '' : rows[r][c];
                tag = (r === 0) ? 'th' : 'td';
                html += '<' + tag + '>' + escapeHtml(val) + '</' + tag + '>';
            }
            html += '</tr>';
        }
        html += '</table>';
        wrap.innerHTML = html;
    }
    if (kind === "text") {
        fetch(inlineUrl, { cache: "no-store" }).then(decodeResp).then(function (t) {
            loadedText = t;
            var pre = document.getElementById("previewText");
            if (pre) { pre.textContent = t; }
            if (editable && editBtn) { editBtn.disabled = false; }
        }).catch(function (e) {
            var pre = document.getElementById("previewText");
            if (pre) { pre.textContent = "加载失败：" + e.message; pre.className = "text err"; }
            if (editable && editBtn) { editBtn.disabled = true; }
        });
    } else if (kind === "csv") {
        fetch(inlineUrl, { cache: "no-store" }).then(decodeResp).then(renderCsv).catch(function (e) {
            var w = document.getElementById("csvWrap");
            if (w) { w.innerHTML = '<pre class="text err">加载失败：' + escapeHtml(e.message) + '</pre>'; }
        });
    }
})();
</script>
</body>
</html>
"""


def render_preview_page(filename: str, size: int, kind: str, office_ok: bool = False) -> str:
    name = os.path.basename(filename) or filename
    ext = file_ext(filename)
    inline_url = "/obs/" + quote(filename, safe="/")
    download_url = "/" + quote(filename, safe="")   # safe='' → 斜杠编码成 %2F，确保命中 /{filename}
    kind_token = "none"
    delim = ","
    editable = False
    save_url = ""

    if kind == "office" and office_ok:
        content_url = "/preview-content/" + quote(filename, safe="/")
        body = '<iframe class="frame" src="' + content_url + '" title="Office 预览"></iframe>'
    elif kind == "image":
        body = '<img src="' + inline_url + '" alt="' + html_escape(name) + '">'
    elif kind == "audio":
        body = '<audio controls autoplay src="' + inline_url + '"></audio>'
    elif kind == "video":
        body = '<video controls autoplay playsinline src="' + inline_url + '"></video>'
    elif kind == "pdf":
        body = '<iframe class="frame" src="' + inline_url + '" title="PDF 预览"></iframe>'
    elif kind == "html":
        body = '<iframe class="frame" sandbox="allow-scripts allow-popups" src="' + inline_url + '" title="HTML 预览"></iframe>'
    elif kind == "text" and ext in ("csv", "tsv"):
        kind_token = "csv"
        delim = "," if ext == "csv" else "\t"
        body = '<div class="csv-wrap" id="csvWrap"><pre class="text">加载中…</pre></div>'
    elif kind == "text":
        kind_token = "text"
        editable = is_editable_text(filename, size)
        save_url = "/api/text/" + quote(filename, safe="/") if editable else ""
        body = '<div class="text-wrap" id="textView"><pre class="text" id="previewText">加载中…</pre></div>'
        if editable:
            # 编辑态的 textarea 与只读预览并排存在，切换时只改 display
            body += ('<div class="editor-wrap" id="editorWrap">'
                     '<div class="editor-head"><span>文件名</span>'
                     '<input id="fileNameInput" type="text" spellcheck="false" autocomplete="off">'
                     '<span class="ext-tip">.txt / .md</span></div>'
                     '<textarea class="editor" id="editorText" spellcheck="false"></textarea></div>')
    else:
        icon = "📄" if kind == "office" else "📦"
        desc = ("此格式暂不支持在线预览（支持 docx/xlsx/xls/pptx），请点击右上角「下载」按钮下载后查看。"
                if kind == "office" else "该文件类型暂不支持在线预览，请下载后查看。")
        body = ('<div class="fallback"><div class="icon">' + icon + '</div>'
                '<h2>' + html_escape(name) + '</h2>'
                '<p>' + desc + '</p>'
                '<a class="btn-download" href="' + download_url + '" download>⬇ 下载文件</a></div>')

    meta = human_size(size) + (" · " + ext.upper() if ext else "")
    html = _PREVIEW_TEMPLATE
    html = html.replace("{{TITLE}}", html_escape(name) + " - 预览")
    html = html.replace("{{NAME}}", html_escape(name))
    html = html.replace("{{META}}", html_escape(meta))
    html = html.replace("{{DOWNLOAD_URL}}", html_escape(download_url))
    html = html.replace("{{BODY}}", body)
    html = html.replace("{{INLINE_JSON}}", json.dumps(inline_url))
    html = html.replace("{{KIND_JSON}}", json.dumps(kind_token))
    html = html.replace("{{DELIM_JSON}}", json.dumps(delim))
    html = html.replace("{{EDITABLE_JSON}}", json.dumps(bool(editable)))
    html = html.replace("{{SAVE_JSON}}", json.dumps(save_url))
    html = html.replace("{{EXT_JSON}}", json.dumps(ext))
    # currentName 是相对路径，注入到 <script> 前转义 < > &，防止文件名里的 </script> 破坏页面
    html = html.replace("{{FILENAME_JSON}}", json.dumps(filename).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return html


@app.get("/preview/{filename:path}")
async def preview_file(filename: str, request: Request):
    """文件预览页：图片/PDF/HTML/音视频/文本(CSV)/Office 等先在页面内预览，右上角提供下载按钮。"""
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    filename = unquote(filename)
    file_path = safe_upload_path(upload_dir, filename)
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        size = os.path.getsize(file_path)
    except OSError:
        size = 0
    kind = preview_kind(filename)
    office_ok = False
    if (kind == "office" and file_ext(filename) in OFFICE_CONVERTIBLE_EXTS
            and size <= OFFICE_MAX_BYTES):
        # 先转换一次（结果进内存缓存）：成功则内嵌 iframe，失败则退化为下载占位页
        office_ok = bool(await asyncio.to_thread(office_html_cached, file_path))
    return HTMLResponse(content=render_preview_page(filename, size, kind, office_ok))


@app.get("/preview-content/{filename:path}")
async def preview_content(filename: str, request: Request):
    """把 Office 文档转成的 HTML 片段包成独立文档，供预览页 iframe 内嵌（纯 Python 库，无需 Office 套件）。"""
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    filename = unquote(filename)
    file_path = safe_upload_path(upload_dir, filename)
    if (not file_path or not os.path.isfile(file_path)
            or file_ext(filename) not in OFFICE_CONVERTIBLE_EXTS):
        raise HTTPException(status_code=404, detail="该文件不支持内容预览")
    body = await asyncio.to_thread(office_html_cached, file_path)
    if not body:
        raise HTTPException(status_code=415, detail="该文件无法转换预览")
    page = (_OFFICE_DOC_TEMPLATE
            .replace("{{TITLE}}", html_escape(os.path.basename(filename)))
            .replace("{{BODY}}", body))
    return HTMLResponse(content=page)


@app.put("/api/text/{filename:path}")
async def save_text_content(filename: str, request: Request):
    """保存预览页里编辑过的 txt/md 内容。

    仅允许已存在、后缀为 txt/md、且不超过 MAX_EDIT_TEXT_BYTES 的文件；
    请求体按 UTF-8 文本处理，先写同目录临时文件再原子替换，避免半截文件。
    路径经 safe_upload_path() 校验，越界一律 404。
    """
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    filename = unquote(filename)
    file_path = safe_upload_path(upload_dir, filename)
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_ext(filename) not in EDITABLE_TEXT_EXTS:
        raise HTTPException(status_code=415, detail="该文件类型不支持在线编辑")
    try:
        old_size = os.path.getsize(file_path)
    except OSError:
        old_size = 0
    if old_size > MAX_EDIT_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="文件过大，不支持在线编辑")

    body = await request.body()
    if len(body) > MAX_EDIT_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="内容超过在线编辑上限")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="内容必须是 UTF-8 文本")

    tmp_path = file_path + ".edit.tmp"
    try:
        async with aiofiles.open(tmp_path, "wb") as out_file:
            await out_file.write(body)
        await asyncio.to_thread(os.replace, tmp_path, file_path)
    except Exception as e:
        with suppress(OSError):
            os.remove(tmp_path)
        log_line(f"在线编辑保存失败 file={filename} error={e}")
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

    log_line(f"在线编辑保存 file={filename} bytes={len(body)}")
    return JSONResponse({
        "ok": True,
        "filename": filename,
        "size": len(body),
        "url": f"{url_head}/{filename}",
    })


@app.post("/api/text/{filename:path}/rename")
async def rename_text_file(filename: str, request: Request):
    """重命名已存在的 txt/md（仅改文件名，保留所在目录）。

    新文件名只允许同目录下的 basename：不含路径分隔符/NUL，后缀仍须为 txt/md，
    且目标不能已存在（409）。源文件同样必须是已存在的 txt/md。
    """
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    filename = unquote(filename)
    file_path = safe_upload_path(upload_dir, filename)
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_ext(filename) not in EDITABLE_TEXT_EXTS:
        raise HTTPException(status_code=415, detail="该文件类型不支持在线重命名")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    new_name = str(payload.get("new_name") or "").strip()
    if (not new_name or new_name in (".", "..")
            or "/" in new_name or "\\" in new_name or "\x00" in new_name
            or len(new_name.encode("utf-8")) > 255):
        raise HTTPException(status_code=400, detail="文件名不合法")
    if file_ext(new_name) not in EDITABLE_TEXT_EXTS:
        raise HTTPException(status_code=415, detail="文件名后缀必须为 .txt 或 .md")

    parent = os.path.dirname(filename)
    new_rel = f"{parent}/{new_name}" if parent else new_name
    new_path = safe_upload_path(upload_dir, new_rel)
    if not new_path:
        raise HTTPException(status_code=400, detail="文件名不合法")
    if new_path == file_path:
        return JSONResponse({
            "ok": True, "filename": new_rel, "old_filename": filename,
            "size": os.path.getsize(file_path), "url": f"{url_head}/{new_rel}",
        })
    if os.path.exists(new_path):
        raise HTTPException(status_code=409, detail="目标文件名已存在")

    try:
        await asyncio.to_thread(os.rename, file_path, new_path)
    except OSError as e:
        log_line(f"在线重命名失败 file={filename} -> {new_rel} error={e}")
        raise HTTPException(status_code=500, detail=f"重命名失败: {str(e)}")
    log_line(f"在线重命名 file={filename} -> {new_rel}")
    try:
        size = os.path.getsize(new_path)
    except OSError:
        size = 0
    return JSONResponse({
        "ok": True,
        "filename": new_rel,
        "old_filename": filename,
        "size": size,
        "url": f"{url_head}/{new_rel}",
    })


# ============================================================================
# 文件下载和删除路由（/{filename}）
# 注意：这些路由必须在所有特定路由之后定义，避免被错误匹配
# ============================================================================

@app.get("/{filename}")
async def download_file(filename: str, request: Request):
    filename = unquote(filename)
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    
    if os.path.exists(file_path) and os.path.isfile(file_path):
        encoded_filename = quote(filename)
        file_size = os.path.getsize(file_path)
        range_header = request.headers.get("range")
        base_headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        }
        if range_header:
            try:
                unit, rng = range_header.strip().split("=")
                if unit != "bytes":
                    raise ValueError()
                if "," in rng:
                    raise ValueError()
                if rng.startswith("-"):
                    length = int(rng[1:])
                    start = max(file_size - length, 0)
                    end = file_size - 1
                else:
                    parts = rng.split("-")
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if len(parts) > 1 and parts[1] != "" else file_size - 1
                if start > end or start >= file_size:
                    return Response(
                        status_code=416,
                        headers={**base_headers, "Content-Range": f"bytes */{file_size}"}
                    )
                async def iterfile(path, start_pos, end_pos, chunk_size=RANGE_DOWNLOAD_CHUNK_SIZE):
                    async with aiofiles.open(path, "rb") as f:
                        await f.seek(start_pos)
                        remain = end_pos - start_pos + 1
                        while remain > 0:
                            read_size = min(chunk_size, remain)
                            data = await f.read(read_size)
                            if not data:
                                break
                            remain -= len(data)
                            yield data
                headers = {
                    **base_headers,
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(end - start + 1),
                }
                return StreamingResponse(iterfile(file_path, start, end), status_code=206, headers=headers, media_type="application/octet-stream")
            except Exception:
                return Response(
                    status_code=416,
                    headers={**base_headers, "Content-Range": f"bytes */{file_size}"}
                )
        else:
            async def iter_all(path, chunk_size=STREAM_DOWNLOAD_CHUNK_SIZE):
                async with aiofiles.open(path, "rb") as f:
                    while True:
                        data = await f.read(chunk_size)
                        if not data:
                            break
                        yield data
            headers = {
                **base_headers,
                "Content-Length": str(file_size),
            }
            return StreamingResponse(iter_all(file_path), status_code=200, headers=headers, media_type="application/octet-stream")
    else:
        raise HTTPException(status_code=404, detail="File not found")

@app.delete("/{filename}")
async def delete_file(filename: str, request: Request):
    """删除文件：同时清理该文件的 HLS 分片目录，避免留下孤儿产物。
    使用位置：首页文件列表删除按钮、视频页播放器浮层删除按钮、视频页设置页「删除」按钮。"""
    filename = unquote(filename)
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)

    if not (os.path.exists(file_path) and os.path.isfile(file_path)):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        os.remove(file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")

    # 一并删除 HLS 分片目录（不存在则忽略；失败不影响原文件删除结果）
    hls_sub = os.path.join(get_hls_dir(), filename)
    hls_removed = False
    if os.path.isdir(hls_sub):
        try:
            await asyncio.to_thread(shutil.rmtree, hls_sub, True)
            hls_removed = True
        except Exception as e:
            log_line(f"删除 HLS 目录失败 file={filename} error={e}")
    log_line(f"删除文件 file={filename} hls_removed={hls_removed}")
    return Response(content="Deleted", status_code=200)

@app.get("/api/hls-cron")
async def get_hls_cron():
    """查询每日 HLS 分片任务的开关状态（默认关闭）。"""
    return {"enabled": hls_cron_enabled_runtime(), "hour": HLS_CRON_HOUR,
            "minute": HLS_CRON_MINUTE, "tz": HLS_CRON_TZ}

@app.post("/api/hls-cron")
async def post_hls_cron(request: Request):
    """打开/关闭每日 HLS 分片任务（持久化，重启后仍生效）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = bool(body.get("enabled"))
    set_hls_cron_enabled(enabled)
    log_line(f"HLS 定时任务开关 -> {'开启' if enabled else '关闭'}")
    return {"enabled": enabled, "hour": HLS_CRON_HOUR,
            "minute": HLS_CRON_MINUTE, "tz": HLS_CRON_TZ}

# 启动服务器
if __name__ == "__main__":
    print(f"文件托管服务器启动: http://localhost:{PORT}", flush=True)
    print(f"上传命令示例: curl --upload-file your-file.wav {url_head}/your-file.wav", flush=True)
    print(f"文件保存目录: {os.path.abspath(UPLOAD_DIR)}", flush=True)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        limit_concurrency=UVICORN_CONFIG["limit_concurrency"],
        limit_max_requests=UVICORN_CONFIG["limit_max_requests"],
        timeout_keep_alive=UVICORN_CONFIG["timeout_keep_alive"],
        backlog=UVICORN_CONFIG["backlog"],
    )
