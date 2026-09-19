# 服务对外访问地址前缀（全局参数，用于渲染替换页面文字和下载前缀）
# 使用位置：
#   - 第 750 行  首页「文件托管」curl 上传示例
#   - 第 771 行  首页 HTML 的 {url_head} 占位符渲染替换
#   - 第 777 行  首页文件列表的下载链接前缀
#   - 第 830 行  /upload/init 秒传命中的返回 url
#   - 第 1089 行 表单上传成功响应文本
#   - 第 1113 行 PUT 上传返回的文件 url
#   - 第 1337 行 启动日志中的上传命令示例
url_head = "http://obs.dimond.top"

import os
import shutil
import json
import asyncio
import threading
import subprocess
import re
from datetime import datetime
from urllib.parse import quote, unquote
from typing import List, Optional, Dict
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import uvicorn
import aiofiles
from contextlib import asynccontextmanager
import hashlib

# 视频相关配置
HLS_DIR = os.environ.get("HLS_DIR", "obs_shards")
video_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video_worker")

# 加载环境变量
load_dotenv()
load_dotenv("env")
load_dotenv("asset/.env")

# 服务器配置
PORT = int(os.environ.get("PORT", 8088))
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

def hls_exists(filename: str) -> bool:
    """检查 HLS 是否已生成"""
    hls_dir = os.path.join(get_hls_dir(), filename)
    return os.path.isdir(hls_dir) and os.path.exists(os.path.join(hls_dir, "index.m3u8"))

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
    # 启动日志落盘（供主机 logs/server.log 查看）
    log_line(f"OBS web app running on port {PORT} (obs dir: {app.state.upload_dir}, logs dir: {LOG_DIR})")
    yield
    # Shutdown
    log_line("OBS web app shutting down")

app = FastAPI(lifespan=lifespan)

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

@app.get("/health")
async def health():
    # 轻量健康探针：不触碰上传目录/视频解码等重资源，供 docker-compose healthcheck 探测
    return {"status": "ok"}

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
async def homepage(request: Request, sort: str = Query("time", enum=["time", "ext"])):
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
                display: block;
                margin-top: 0;
                padding: 4px 12px;
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
                display: block;
                padding: 4px 12px;
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
        </style>
        <script>
            const CHUNK_SIZE_BROWSER = 10 * 1024 * 1024; // 浏览器分片上传大小 10MB
            const UPLOAD_CONCURRENCY = 3;                // 分片并发上传路数
            // crypto.subtle 只在安全上下文（https / localhost）可用；
            // 通过 http://obs.dimond.top 或 http://<局域网IP> 访问时它是 undefined，
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
                    const response = await fetch(`/${filename}`, { method: 'DELETE' });
                    if (response.ok) {
                        window.location.reload();
                    } else {
                        alert('删除失败');
                    }
                } catch (e) {
                    alert('删除出错: ' + e);
                }
            }

            // 单文件直传（≤10MB）：XHR + 上传进度
            function uploadOneFileDirect(file, onProgress) {
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
                    xhr.open('PUT', `/${encodeURIComponent(file.name)}`);
                    xhr.send(file);
                });
            }

            // 大文件分片上传（>10MB）：/upload/init + /upload/chunk + /upload/complete，支持秒传/断点续传
            //  - 分片哈希只算一次并缓存复用（重试/重传不再重算、不再重读文件）
            //  - 分片 UPLOAD_CONCURRENCY 路并发上传
            async function uploadOneFileResumable(file, onProgress) {
                const chunkSize = CHUNK_SIZE_BROWSER;
                const size = file.size;
                const totalChunks = Math.ceil(size / chunkSize) || 1;
                const hashAlgo = "sha256";

                // 1) 单遍顺序读取：拿到每片哈希（内存 O(分片)），并合并成整文件指纹
                const chunkHashes = await computeChunkHashes(file, chunkSize, totalChunks,
                    (pct) => { if (onProgress) onProgress(pct, 0, '校验中'); });
                const hash = await fileFingerprint(chunkHashes);

                // 2) 初始化会话（含秒传判定与已上传分片枚举）
                const initResp = await fetch('/upload/init', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: file.name, size, hash_algo: hashAlgo, hash, chunk_size: chunkSize, total_chunks: totalChunks })
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
                            const r = await fetch(`/upload/chunk/${encodeURIComponent(uploadId)}/${i}`, {
                                method: 'PUT',
                                // 逐片校验用哈希；重试直接复用，避免重复计算
                                headers: { 'X-Chunk-SHA256': chunkHashes[i] },
                                body: blob,
                            });
                            ok = r.status === 201;
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
                const c = await fetch(`/upload/complete/${encodeURIComponent(uploadId)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: file.name, size, total_chunks: totalChunks, chunk_size: chunkSize, hash_algo: hashAlgo, hash })
                });
                if (!c.ok) throw new Error('合并失败: ' + await c.text());
                return await c.text();
            }

            // 拖拽/选择文件的统一入口（小文件直传，大文件走分片+秒传）
            async function uploadFiles(files) {
                if (!files || files.length === 0) return;
                const statusEl = document.getElementById('formUploadStatus');
                const totalFiles = files.length;
                let success = 0, fail = 0;
                const failedNames = [];
                let overallBytes = 0, totalBytes = 0;
                for (let i = 0; i < totalFiles; i++) totalBytes += files[i].size;
                for (let i = 0; i < totalFiles; i++) {
                    const file = files[i];
                    statusEl.textContent = `上传中... (${i + 1}/${totalFiles}) ${file.name} 0%`;
                    const report = (pct, loaded, stage) => {
                        const overallPct = totalBytes > 0 ? Math.round((overallBytes + (loaded || 0)) / totalBytes * 100) : 0;
                        statusEl.textContent = `${stage || '上传中'}... (${i + 1}/${totalFiles}) ${file.name} ${pct}% [总进度 ${overallPct}%]`;
                    };
                    try {
                        if (file.size <= CHUNK_SIZE_BROWSER) {
                            await uploadOneFileDirect(file, report);
                        } else {
                            await uploadOneFileResumable(file, report);
                        }
                        overallBytes += file.size;
                        success++;
                    } catch (err) {
                        fail++;
                        failedNames.push(file.name + ' (' + err.message + ')');
                    }
                }
                if (fail === 0) {
                    statusEl.textContent = `全部上传成功！(${success}个文件)`;
                    window.location.reload();
                } else {
                    statusEl.textContent = `上传完成：成功${success}个，失败${fail}个 → ${failedNames.join('; ')}`;
                    if (success > 0) setTimeout(() => window.location.reload(), 1500);
                }
            }

            function handleDragUpload(fileList) {
                if (!fileList || fileList.length === 0) return;
                uploadFiles(fileList);
            }

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
                zone.addEventListener('drop', (e) => {
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
    </head>
    <body>
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

        <p style="font-size: 0.8em; margin-bottom: 10px;">文件托管： <code>curl --upload-file file.txt {url_head}/file.txt</code></p>
        
        <div id="uploadZone" style="margin: 20px 0; padding: 30px; border: 2px dashed #ccc; background: #f9f9f9; text-align: center; border-radius: 8px; transition: border-color 0.3s, background 0.3s;">
            <p style="margin: 0 0 10px 0; color: #999;">拖拽文件到此处上传</p>
            <input type="file" id="formFile" onchange="uploadFiles(this.files)" style="display:none;" multiple>
            <button type="button" onclick="document.getElementById('formFile').click()" style="cursor:pointer; padding:6px 18px; border:1px solid #ccc; background:#fff; border-radius:4px;">选择文件</button>
            <span id="formUploadStatus" style="display:block; margin-top:8px; font-size:0.85em; color:#999;"></span>
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
            html += f'''
            <li>
                <a href="{file_url}" target="_blank">{f}</a> 
                <span class="actions">
                    <a href="{file_url}" download>下载</a>
                    <button class="btn-delete" onclick="deleteFile('{f}')" title="删除">🗑️</button>
                </span>
            </li>
            '''
    
    html += """
        </ul>
    </body>
    </html>
    """
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
    final_path = os.path.join(upload_dir, filename)
    if os.path.exists(final_path) and os.path.getsize(final_path) == size:
        # 进行秒传校验（按同一分片大小重算指纹，流式读取不占内存）
        if hash_algo == "sha256":
            existing_hash = await asyncio.to_thread(chunk_fingerprint, final_path, chunk_size)
            if existing_hash == file_hash:
                url = f"{url_head}/{filename}"
                log_line(f"秒传命中 file={filename} size={size} chunks={total_chunks}")
                return JSONResponse({"skip": True, "url": url})
    upload_id = make_upload_id(filename, size, hash_algo, file_hash)
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
            "uploaded_chunks": set(uploaded)
        }
    
    return JSONResponse({
        "uploadId": upload_id,
        "uploaded": sorted(uploaded),
        "totalChunks": total_chunks,
        "chunkSize": chunk_size,
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
        
        # 移动到最终位置
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
        
        url = f"/obs/{quote(filename)}"
        log_line(f"上传完成(分片) file={filename} size={size} chunks={total_chunks} -> {final_path}")
        return JSONResponse({"ok": True, "url": url, "filename": filename})
    except HTTPException as he:
        log_line(f"上传失败(分片合并) file={filename} status={he.status_code} detail={he.detail}")
        raise
    except Exception as e:
        log_line(f"上传失败(分片合并) file={filename} error={e}")
        raise HTTPException(status_code=500, detail=f"合并失败: {str(e)}")

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
    filename = unquote(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
        
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    save_path = os.path.join(upload_dir, filename)
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
                
        file_url = f"{url_head}/{filename}"
        log_line(f"上传完成(PUT) file={filename} bytes={total_written} -> {save_path}")
        return Response(content=file_url, media_type="text/plain", status_code=201)
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
    filename = unquote(filename)
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    
    if os.path.exists(file_path) and os.path.isfile(file_path):
        try:
            os.remove(file_path)
            return Response(content="Deleted", status_code=200)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")
    else:
        raise HTTPException(status_code=404, detail="File not found")

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
