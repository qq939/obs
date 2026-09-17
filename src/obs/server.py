import os
import shutil
import json
import asyncio
import subprocess
import threading
from datetime import datetime
from urllib.parse import quote, unquote
from typing import List, Optional, Dict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount, Route
from dotenv import load_dotenv
import uvicorn
import aiofiles
import hashlib

# 加载环境变量
load_dotenv()
load_dotenv("env")
load_dotenv("asset/.env")

# 服务器配置
PORT = int(os.environ.get("PORT", 8088))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "obs")
HLS_DIR = os.environ.get("HLS_DIR", "hls")
HLS_GEN_VERSION = int(os.environ.get("HLS_GEN_VERSION", "4"))
HLS_SEGMENT_BYTES = int(os.environ.get("HLS_SEGMENT_BYTES", str(4 * 1024 * 1024)))  # 4MB

# 线程池用于后台哈希计算和视频处理
hash_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hash_worker")
video_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video_worker")

# 内存存储 Notice 内容
NOTICE_CONTENT = ""
NOTICE_LOCK = asyncio.Lock()

# 上传会话管理器
upload_sessions: Dict[str, dict] = {}
SESSION_LOCK = threading.Lock()

# 视频处理任务管理
video_tasks: Dict[str, dict] = {}
TASK_LOCK = threading.Lock()

# 全局性能参数（使用位置见行内注释）
# MAX_UPLOAD_SIZE: 上传大小限制（None 表示无限制）
MAX_UPLOAD_SIZE = None
# UPLOAD_CHUNK_SIZE: 表单上传读取分片大小（10MB）
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024
# RANGE_DOWNLOAD_CHUNK_SIZE: Range 分片下载时的单次读取分片大小（10MB）
RANGE_DOWNLOAD_CHUNK_SIZE = 10 * 1024 * 1024
# STREAM_DOWNLOAD_CHUNK_SIZE: 完整流式下载分片大小（40MB）
STREAM_DOWNLOAD_CHUNK_SIZE = 40 * 1024 * 1024
# UVICORN 运行参数
UVICORN_CONFIG = {
    "limit_concurrency": 1000,
    "limit_max_requests": 10000,
    "timeout_keep_alive": 300,
    "backlog": 2048,
}

def get_upload_dir() -> str:
    return os.path.abspath(UPLOAD_DIR)

def get_chunk_dir() -> str:
    return os.path.abspath(os.path.join(get_upload_dir(), ".chunks"))

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

def make_upload_id(filename: str, size: int, hash_algo: str, file_hash: str) -> str:
    safe_name = filename.replace("/", "_")
    return f"{hash_algo}:{file_hash}:{size}:{safe_name}"

# 视频相关辅助函数
VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mkv"}

def list_video_files() -> List[dict]:
    """列出所有视频文件"""
    upload_dir = get_upload_dir()
    videos = []
    try:
        for name in os.listdir(upload_dir):
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTS:
                p = os.path.join(upload_dir, name)
                if os.path.isfile(p):
                    videos.append({
                        "name": name,
                        "size": os.path.getsize(p),
                        "time": datetime.fromtimestamp(os.path.getmtime(p)).isoformat(),
                        "has_hls": hls_exists(name)
                    })
    except Exception:
        pass
    videos.sort(key=lambda x: x["time"], reverse=True)
    return videos

def safe_name(name: str) -> str:
    """安全处理文件名"""
    return "".join(c for c in name if c.isalnum() or c in "._-")

def run_ffmpeg(args: List[str], cwd: Optional[str] = None, timeout: int = 600) -> str:
    """运行 ffmpeg 命令，返回 stderr 输出"""
    proc = subprocess.run(
        ["ffmpeg"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd
    )
    return proc.stderr

def run_ffprobe(args: List[str], timeout: int = 10) -> dict:
    """运行 ffprobe 命令，返回 JSON 结果"""
    proc = subprocess.run(
        ["ffprobe"] + args,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr}")
    return json.loads(proc.stdout)

def hls_duration_sync(name: str) -> float:
    """从 HLS playlist 读取总时长"""
    playlist_path = os.path.join(HLS_DIR, name, "index.m3u8")
    if not os.path.exists(playlist_path):
        return 0.0
    try:
        with open(playlist_path, "r") as f:
            content = f.read()
        total = 0.0
        for line in content.split("\n"):
            if line.startswith("#EXTINF:"):
                duration = float(line.split(":")[1].split(",")[0])
                total += duration
        return total
    except Exception:
        return 0.0

def hls_exists(name: str) -> bool:
    """检查 HLS 是否存在且有效"""
    dir_path = os.path.join(HLS_DIR, name)
    if not os.path.exists(os.path.join(dir_path, "index.m3u8")):
        return False
    meta_path = os.path.join(dir_path, "meta.json")
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
        if meta.get("version") != HLS_GEN_VERSION:
            return False
        src_path = os.path.join(UPLOAD_DIR, name)
        if not os.path.exists(src_path):
            return False
        return meta.get("size") == os.path.getsize(src_path)
    except Exception:
        return False

def invalidate_hls(name: str):
    """删除 HLS 目录"""
    hls_path = os.path.join(HLS_DIR, name)
    if os.path.exists(hls_path):
        shutil.rmtree(hls_path)

async def generate_hls_background(filename: str):
    """后台生成 HLS"""
    with TASK_LOCK:
        video_tasks[filename] = {"status": "processing", "progress": 0}

    try:
        src_path = os.path.join(UPLOAD_DIR, filename)
        hls_dir = os.path.join(HLS_DIR, filename)
        os.makedirs(hls_dir, exist_ok=True)

        # 生成 HLS
        args = [
            "-y", "-i", src_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-vf", f"scale='min(1920,iw)':-2",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "hls",
            "-hls_time", "5",
            "-hls_list_size", "0",
            "-hls_segment_filename", os.path.join(hls_dir, "segment_%03d.ts"),
            os.path.join(hls_dir, "index.m3u8")
        ]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(video_executor, lambda: run_ffmpeg(args))

        # 写入 meta.json
        with open(os.path.join(hls_dir, "meta.json"), "w") as f:
            json.dump({
                "version": HLS_GEN_VERSION,
                "size": os.path.getsize(src_path)
            }, f)

        with TASK_LOCK:
            video_tasks[filename] = {"status": "ready", "progress": 100}
    except Exception as e:
        with TASK_LOCK:
            video_tasks[filename] = {"status": "error", "error": str(e)}

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.upload_dir = get_upload_dir()
    app.state.chunk_dir = get_chunk_dir()
    os.makedirs(get_upload_dir(), exist_ok=True)
    os.makedirs(get_chunk_dir(), exist_ok=True)
    os.makedirs(HLS_DIR, exist_ok=True)
    yield

app = FastAPI(title="OBS", lifespan=lifespan)

# ============================================================================
# WebSocket 连接管理器（公告板）
# ============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str, exclude: Optional[WebSocket] = None):
        for connection in self.active_connections:
            if connection != exclude:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass

manager = ConnectionManager()

async def get_notice():
    global NOTICE_CONTENT
    return NOTICE_CONTENT

async def update_notice(content: str):
    global NOTICE_CONTENT
    async with NOTICE_LOCK:
        NOTICE_CONTENT = content
    return True

# ============================================================================
# WebSocket 和公告板路由
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
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
                    broadcast_msg = json.dumps({"type": "update", "content": new_content})
                    await manager.broadcast(broadcast_msg, exclude=websocket)

                elif msg_type == "reset":
                    default_text = ""
                    await update_notice(default_text)
                    broadcast_msg = json.dumps({"type": "update", "content": default_text})
                    await manager.broadcast(broadcast_msg)

            except json.JSONDecodeError:
                pass
            except Exception as e:
                print(f"Error: {e}", flush=True)

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        manager.disconnect(websocket)

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

# ============================================================================
# 首页（参照版本 b8756f8 的界面）
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request, sort: str = Query("time", enum=["time", "ext"])):
    files_list = []
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    if os.path.exists(upload_dir):
        try:
            raw_files = [f for f in os.listdir(upload_dir) if not f.startswith('.')]

            if sort == 'ext':
                raw_files.sort(key=lambda x: (os.path.splitext(x)[1].lower(), x))
            else:
                raw_files.sort(key=lambda x: os.path.getmtime(os.path.join(upload_dir, x)), reverse=True)

            files_list = raw_files
        except Exception:
            files_list = []

    time_active = "active" if sort != 'ext' else ""
    ext_active = "active" if sort == 'ext' else ""

    host = "obs.dimond.top"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>文件托管服务</title>
        <style>
            body {{ font-family: sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
            h1 {{ color: #333; }}
            ul {{ list-style: none; padding: 0; }}
            li {{ padding: 10px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; }}
            a {{ text-decoration: none; color: #007bff; }}
            a:hover {{ text-decoration: underline; }}
            .empty {{ color: #999; font-style: italic; }}
            .actions {{ display: flex; gap: 10px; }}
            .btn-delete {{ cursor: pointer; background: none; border: none; font-size: 1.2em; }}
            .btn-delete:hover {{ opacity: 0.7; }}
            .sort-controls {{ margin-bottom: 20px; }}
            .sort-controls a {{ margin-right: 15px; font-weight: bold; }}
            .sort-controls a.active {{ color: #333; cursor: default; text-decoration: none; }}

            /* 公告板样式 */
            .notice-board {{
                margin: 20px 0;
                padding: 10px;
                border: 1px solid #eee;
                background: #f9f9f9;
                position: relative;
            }}
            .notice-board textarea {{
                width: 100%;
                height: 150px;
                border: 1px solid #ccc;
                border-bottom: none;
                resize: vertical;
                font-family: monospace;
                box-sizing: border-box;
            }}
            .notice-copy-btn {{
                display: block;
                margin-top: 0;
                padding: 4px 12px;
                border: 1px solid #ccc;
                border-top: none;
                background: #81D8D0;
                color: #fff;
                cursor: pointer;
                flex: 20;
            }}
            .notice-copy-btn:hover {{ background: #73cbc3; }}
            .notice-save-btn {{
                display: block;
                padding: 4px 12px;
                border: 1px solid #ccc;
                border-top: none;
                background: #fff;
                color: #333;
                cursor: pointer;
                flex: 1;
            }}
            .notice-save-btn:hover {{ background: #f2f2f2; }}
            .notice-copy-bar {{
                position: static;
                padding: 0;
                display: flex;
                gap: 6px;
            }}
            .btn-close-notice {{
                position: absolute;
                top: 5px;
                right: 5px;
                border: none;
                background: transparent;
                cursor: pointer;
                font-size: 16px;
                color: #999;
            }}
            .btn-close-notice:hover {{ color: #333; }}
            #ws-status-indicator {{
                position: absolute;
                top: 5px;
                left: 5px;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                background-color: red;
                border: 1px solid #ccc;
            }}
            .notice-tools {{
                position: absolute;
                bottom: 8px;
                right: 8px;
                z-index: 2;
            }}
            .notice-tools button {{
                cursor: pointer;
                background: none;
                border: none;
                font-size: 16px;
                color: #999;
            }}
            .notice-tools button:hover {{ color: #333; }}
        </style>
        <script>
            const CHUNK_SIZE_BROWSER = 10 * 1024 * 1024;

            async function sha256Hex(file) {{
                const buf = await file.arrayBuffer();
                const digest = await crypto.subtle.digest("SHA-256", buf);
                const arr = Array.from(new Uint8Array(digest));
                return arr.map(b => b.toString(16).padStart(2, "0")).join("");
            }}

            async function deleteFile(filename) {{
                if (!confirm(`确定要删除 ${{filename}} 吗？`)) return;
                try {{
                    const response = await fetch(`/${{filename}}`, {{ method: 'DELETE' }});
                    if (response.ok) {{
                        window.location.reload();
                    }} else {{
                        alert('删除失败');
                    }}
                }} catch (e) {{
                    alert('删除出错: ' + e);
                }}
            }}

            async function chunkedUpload(inputEl) {{
                const file = inputEl.files && inputEl.files[0];
                if (!file) {{
                    alert('请先选择文件');
                    return;
                }}
                const filename = file.name;
                const total = file.size;
                let offset = 0;
                try {{
                    while (offset < total) {{
                        const end = Math.min(offset + CHUNK_SIZE_BROWSER, total);
                        const blob = file.slice(offset, end);
                        const resp = await fetch(`/${{encodeURIComponent(filename)}}`, {{
                            method: 'PUT',
                            body: await blob.arrayBuffer(),
                        }});
                        if (resp.status !== 201) {{
                            const text = await resp.text();
                            throw new Error(`分片上传失败: ${{resp.status}} ${{text}}`);
                        }}
                        offset = end;
                    }}
                    alert('分片上传成功');
                    window.location.reload();
                }} catch (err) {{
                    alert('分片上传出错: ' + err.message);
                }}
            }}

            async function resumableUpload(inputEl) {{
                const file = inputEl.files && inputEl.files[0];
                if (!file) {{
                    alert('请先选择文件');
                    return;
                }}
                const filename = file.name;
                const size = file.size;
                const chunkSize = CHUNK_SIZE_BROWSER;
                const totalChunks = Math.ceil(size / chunkSize);
                const hashAlgo = "sha256";
                const hash = await sha256Hex(file);
                let resp = await fetch('/upload/init', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ filename, size, hash_algo: hashAlgo, hash, chunk_size: chunkSize, total_chunks: totalChunks }})
                }});
                if (!resp.ok) {{
                    const t = await resp.text();
                    alert('初始化失败: ' + t);
                    return;
                }}
                const info = await resp.json();
                if (info.skip) {{
                    alert('文件已存在，已秒传：' + info.url);
                    window.location.reload();
                    return;
                }}
                const uploadId = info.upload_id;
                const uploaded = new Set(info.uploaded || []);
                for (let i = 0; i < totalChunks; i++) {{
                    if (uploaded.has(i)) continue;
                    const start = i * chunkSize;
                    const end = Math.min(start + chunkSize, size);
                    const blob = file.slice(start, end);
                    let ok = false;
                    for (let attempt = 0; attempt < 3 && !ok; attempt++) {{
                        const r = await fetch(`/upload/chunk/${{encodeURIComponent(uploadId)}}/${{i}}`, {{
                            method: 'PUT',
                            body: await blob.arrayBuffer(),
                        }});
                        ok = r.status === 201;
                    }}
                    if (!ok) {{
                        alert('分片上传失败，无法完成：' + i);
                        return;
                    }}
                }}
                const c = await fetch(`/upload/complete/${{encodeURIComponent(uploadId)}}`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ filename, size, total_chunks: totalChunks, hash_algo: hashAlgo, hash }})
                }});
                if (c.ok) {{
                    const url = await c.text();
                    alert('上传完成：' + url);
                    window.location.reload();
                }} else {{
                    const tx = await c.text();
                    alert('合并失败：' + tx);
                }}
            }}

            async function saveNotice() {{
                try {{
                    const response = await fetch('/save_notice', {{ method: 'POST' }});
                    if (response.ok) {{
                        const data = await response.json();
                        alert(`公告已保存为: ${{data.filename}}`);
                        window.location.reload();
                    }} else {{
                        const err = await response.json();
                        alert('保存失败: ' + (err.detail || '未知错误'));
                    }}
                }} catch (e) {{
                    alert('保存出错: ' + e);
                }}
            }}

            function copyNoticeToClipboard() {{
                try {{
                    const contentEl = document.getElementById('notice-content');
                    const content = contentEl.value || '';
                    if (navigator.clipboard && navigator.clipboard.writeText) {{
                        navigator.clipboard.writeText(content).catch(() => legacyCopy(contentEl, content));
                    }} else {{
                        legacyCopy(contentEl, content);
                    }}
                }} catch (e) {{
                    alert('复制出错: ' + e);
                }}
            }}

            function legacyCopy(el, text) {{
                try {{
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.style.position = 'fixed';
                    ta.style.top = '-1000px';
                    document.body.appendChild(ta);
                    ta.focus();
                    ta.select();
                    const ok = document.execCommand('copy');
                    document.body.removeChild(ta);
                    if (!ok) alert('复制失败，请手动选择文本后复制');
                }} catch (err) {{
                    alert('复制失败，请手动选择文本后复制');
                }}
            }}

            document.addEventListener('DOMContentLoaded', () => {{
                const noticeArea = document.getElementById('notice-content');
                const statusIndicator = document.getElementById('ws-status-indicator');

                const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${{wsProtocol}}//${{window.location.host}}/ws`;

                let ws;
                let isConnected = false;

                function connect() {{
                    statusIndicator.style.backgroundColor = 'yellow';
                    ws = new WebSocket(wsUrl);

                    ws.onopen = () => {{
                        isConnected = true;
                        statusIndicator.style.backgroundColor = 'green';
                    }};

                    ws.onmessage = (event) => {{
                        try {{
                            const data = JSON.parse(event.data);
                            if (data.type === 'init' || data.type === 'update') {{
                                if (noticeArea.value !== data.content) {{
                                    const start = noticeArea.selectionStart;
                                    const end = noticeArea.selectionEnd;
                                    noticeArea.value = data.content;
                                    if (document.activeElement === noticeArea) {{
                                        noticeArea.setSelectionRange(start, end);
                                    }}
                                }}
                            }}
                        }} catch (e) {{}}
                    }};

                    ws.onclose = () => {{
                        isConnected = false;
                        statusIndicator.style.backgroundColor = 'red';
                        setTimeout(connect, 3000);
                    }};

                    ws.onerror = () => {{
                        ws.close();
                    }};
                }}

                connect();

                noticeArea.addEventListener('input', () => {{
                    if (ws && isConnected) {{
                        ws.send(JSON.stringify({{
                            type: 'update',
                            content: noticeArea.value
                        }}));
                    }}
                }});

                window.resetNotice = function() {{
                    if (ws && isConnected) {{
                        ws.send(JSON.stringify({{ type: 'reset' }}));
                    }} else {{
                        alert('未连接到服务器，无法重置');
                    }}
                }};
            }});
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

        <p style="font-size: 0.8em; margin-bottom: 10px;">文件托管： <code>curl --upload-file file.txt http://obs.dimond.top/file.txt</code></p>

        <div style="margin: 20px 0; padding: 10px; border: 1px solid #eee; background: #f9f9f9;">
            <form action="/" method="post" enctype="multipart/form-data">
                <input type="file" name="file" required>
                <input type="submit" value="上传">
            </form>
            <div style="margin-top:8px;">
                <input type="file" id="chunkFile">
                <button onclick="chunkedUpload(document.getElementById('chunkFile'))">分片上传(10MB)</button>
            </div>
            <div style="margin-top:8px;">
                <input type="file" id="resumeFile">
                <button onclick="resumableUpload(document.getElementById('resumeFile'))">断点续传(10MB+秒传)</button>
            </div>
        </div>

        <div class="sort-controls">
            排序方式:
            <a href="?sort=time" class="{time_active}">按时间 (最新)</a>
            <a href="?sort=ext" class="{ext_active}">按扩展名 (A-Z)</a>
        </div>

        <ul>
    """

    html = html.replace("{time_active}", time_active).replace("{ext_active}", ext_active)

    if not files_list:
        html += '<li class="empty">暂无文件</li>'
    else:
        for f in files_list:
            file_url = f"http://{host}/{f}"
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

# ============================================================================
# 视频页面（使用 obs-video-app 的 UI）
# ============================================================================

# 获取 video_static 目录的绝对路径
def get_video_static_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_static")

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

@app.get("/video/hls.min.js")
async def video_hls_js():
    """HLS.js 库"""
    static_dir = get_video_static_dir()
    hls_path = os.path.join(static_dir, "hls.min.js")
    if os.path.exists(hls_path):
        with open(hls_path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="application/javascript")
    return Response(content="Not found", status_code=404)

@app.get("/video/play/{filename}")
async def video_play(filename: str):
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="视频不存在")
    return FileResponse(file_path, media_type="video/mp4")

@app.get("/hls/{filename}/{path:path}")
async def hls_playlist(filename: str, path: str):
    """HLS 播放列表和分片 - 兼容 obs-video-app"""
    hls_dir = os.path.join(HLS_DIR, filename)
    file_path = os.path.join(hls_dir, path)

    # 安全检查
    if not os.path.exists(os.path.join(hls_dir)):
        raise HTTPException(status_code=404, detail="HLS 目录不存在")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    if path.endswith(".m3u8"):
        return FileResponse(file_path, media_type="application/vnd.apple.mpegurl")
    else:
        return FileResponse(file_path, media_type="video/mp2t")

@app.get("/video/hls/{filename}/{path:path}")
async def video_hls(filename: str, path: str):
    """HLS 播放列表和分片"""
    hls_dir = os.path.join(HLS_DIR, filename)
    file_path = os.path.join(hls_dir, path)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    if path.endswith(".m3u8"):
        return FileResponse(file_path, media_type="application/vnd.apple.mpegurl")
    else:
        return FileResponse(file_path, media_type="video/MP2T")

# ============================================================================
# 视频 API（兼容 obs-video-app）
# ============================================================================

@app.get("/videos")
async def videos_list():
    """获取视频列表 - 兼容 obs-video-app"""
    return JSONResponse({"videos": list_video_files()})

@app.get("/video/api/list")
async def video_list():
    """获取视频列表"""
    return JSONResponse({"videos": list_video_files()})

@app.post("/video/api/generate-hls/{filename}")
async def generate_hls(filename: str):
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="视频不存在")

    if hls_exists(filename):
        return JSONResponse({"status": "ready", "message": "HLS 已存在"})

    asyncio.create_task(generate_hls_background(filename))
    return JSONResponse({"status": "processing", "message": "HLS 生成中"})

@app.post("/compress/{filename}")
async def compress_video(filename: str):
    """压缩视频（转码为 H.264 + AAC + faststart）"""
    filename = unquote(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="视频不存在")

    before_size = os.path.getsize(file_path)
    tmp_out = os.path.join(upload_dir, f".comp-{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4")

    try:
        # 执行压缩
        args = [
            "-y", "-i", file_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-vf", f"scale='min(1920,iw)':-2",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            tmp_out
        ]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(video_executor, lambda: run_ffmpeg(args))

        if not os.path.exists(tmp_out):
            raise HTTPException(status_code=500, detail="压缩失败")

        after_size = os.path.getsize(tmp_out)

        if after_size >= before_size:
            # 压缩后没有变小，保留原文件
            os.remove(tmp_out)
            return JSONResponse({"ok": True, "skipped": True, "before": before_size, "after": after_size, "savedPct": 0})

        # 替换原文件
        os.replace(tmp_out, file_path)

        # 失效并重新生成 HLS
        invalidate_hls(filename)
        asyncio.create_task(generate_hls_background(filename))

        saved = before_size - after_size
        saved_pct = round((1 - after_size / before_size) * 100)

        return JSONResponse({"ok": True, "skipped": False, "before": before_size, "after": after_size, "saved": saved, "savedPct": saved_pct})

    except HTTPException:
        raise
    except Exception as e:
        # 清理临时文件
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except:
                pass
        raise HTTPException(status_code=500, detail=f"压缩失败: {str(e)}")

@app.get("/video/api/status/{filename}")
async def video_status(filename: str):
    with TASK_LOCK:
        task = video_tasks.get(filename, {"status": "not_found"})
    return JSONResponse(task)

# ============================================================================
# 文件上传接口（表单上传、curl 上传）
# ============================================================================

@app.post("/", response_class=HTMLResponse)
async def upload_file_form(request: Request):
    try:
        form = await request.form()

        upload_file = None
        for key, value in form.items():
            if hasattr(value, "filename") and hasattr(value, "file"):
                upload_file = value
                break

        if not upload_file:
            raise HTTPException(status_code=422, detail="No file field found")

        filename = upload_file.filename
        if not filename:
            raise HTTPException(status_code=400, detail="Filename is empty")

        upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
        save_path = os.path.join(upload_dir, filename)

        os.makedirs(upload_dir, exist_ok=True)
        async with aiofiles.open(save_path, 'wb') as out_file:
            total_written = 0
            while content := await upload_file.read(UPLOAD_CHUNK_SIZE):
                await out_file.write(content)
                total_written += len(content)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")

        return Response(content=f"文件上传成功: http://obs.dimond.top/{filename}", media_type="text/plain", status_code=201)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@app.put("/{filename}")
async def upload_file_put(filename: str, request: Request):
    filename = unquote(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    save_path = os.path.join(upload_dir, filename)
    try:
        os.makedirs(upload_dir, exist_ok=True)
        async with aiofiles.open(save_path, 'wb') as out_file:
            total_written = 0
            async for chunk in request.stream():
                await out_file.write(chunk)
                total_written += len(chunk)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")

        file_url = f"http://obs.dimond.top/{filename}"
        return Response(content=file_url, media_type="text/plain", status_code=201)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

@app.delete("/{filename}")
async def delete_file(filename: str):
    filename = unquote(filename)
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)

    if os.path.exists(file_path) and os.path.isfile(file_path):
        try:
            os.remove(file_path)
            invalidate_hls(filename)
            return Response(content="Deleted", status_code=200)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")
    else:
        raise HTTPException(status_code=404, detail="File not found")

# ============================================================================
# 分片上传接口
# ============================================================================

@app.post("/upload/init")
async def upload_init(request: Request):
    data = await request.json()
    filename = data.get("filename")
    size = int(data.get("size", 0))
    hash_algo = data.get("hash_algo", "sha256")
    file_hash = data.get("hash")
    total_chunks = int(data.get("total_chunks", 0))
    chunk_size = int(data.get("chunk_size", 0))

    if not filename or not size or not file_hash:
        raise HTTPException(status_code=400, detail="缺少必要参数")

    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    chunk_dir = getattr(app.state, "chunk_dir", get_chunk_dir())
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(chunk_dir, exist_ok=True)

    final_path = os.path.join(upload_dir, filename)
    if os.path.exists(final_path) and os.path.getsize(final_path) == size:
        if hash_algo == "sha256":
            existing_hash = file_sha256(final_path)
            if existing_hash == file_hash:
                url = f"http://obs.dimond.top/{filename}"
                return JSONResponse({"skip": True, "url": url})

    upload_id = make_upload_id(filename, size, hash_algo, file_hash)
    up_dir = os.path.join(chunk_dir, upload_id)
    os.makedirs(up_dir, exist_ok=True)

    uploaded = []
    chunk_hashes = {}
    try:
        for name in os.listdir(up_dir):
            if name.endswith(".part"):
                try:
                    idx = int(name[:-5])
                    uploaded.append(idx)
                    part_path = os.path.join(up_dir, name)
                    with open(part_path, "rb") as f:
                        h = hashlib.sha256()
                        while chunk := f.read(1024 * 1024):
                            h.update(chunk)
                        chunk_hashes[idx] = h.hexdigest()
                except Exception:
                    pass
    except Exception:
        uploaded = []

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
        "upload_id": upload_id,
        "uploaded": sorted(uploaded),
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
    })

@app.put("/upload/chunk/{upload_id}/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request):
    if index < 0:
        raise HTTPException(status_code=400, detail="分片序号非法")

    chunk_dir = getattr(app.state, "chunk_dir", get_chunk_dir())
    up_dir = os.path.join(chunk_dir, upload_id)
    os.makedirs(up_dir, exist_ok=True)
    part_path = os.path.join(up_dir, f"{index}.part")

    try:
        async with aiofiles.open(part_path, "wb") as f:
            async for chunk in request.stream():
                await f.write(chunk)

        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            hash_executor,
            _compute_chunk_hash_background,
            upload_id,
            index,
            part_path
        )

        return Response(content="OK", status_code=201)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分片写入失败: {str(e)}")

def _compute_chunk_hash_background(upload_id: str, index: int, part_path: str):
    try:
        with open(part_path, "rb") as f:
            h = hashlib.sha256()
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
            chunk_hash = h.hexdigest()

        with SESSION_LOCK:
            if upload_id in upload_sessions:
                session = upload_sessions[upload_id]
                session["chunk_hashes"][index] = chunk_hash
                session["uploaded_chunks"].add(index)
    except Exception as e:
        print(f"计算分片 {index} 哈希失败: {e}", flush=True)

@app.post("/upload/status/{upload_id}")
async def upload_status(upload_id: str):
    with SESSION_LOCK:
        if upload_id not in upload_sessions:
            raise HTTPException(status_code=404, detail="上传会话不存在")

        session = upload_sessions[upload_id]
        chunk_hashes = []
        for i in range(session["total_chunks"]):
            if i in session["chunk_hashes"]:
                chunk_hashes.append(session["chunk_hashes"][i])
            else:
                chunk_hashes.append(None)

        return JSONResponse({
            "upload_id": upload_id,
            "total_chunks": session["total_chunks"],
            "uploaded_chunks": list(session["uploaded_chunks"]),
            "chunk_hashes": chunk_hashes,
            "overall_hash": session["overall_hash"],
            "overall_hash_computed": session["overall_hash_computed"]
        })

@app.post("/upload/complete/{upload_id}")
async def upload_complete(upload_id: str, request: Request):
    data = await request.json()
    filename = data.get("filename")
    size = int(data.get("size", 0))
    total_chunks = int(data.get("total_chunks", 0))
    hash_algo = data.get("hash_algo", "sha256")
    file_hash = data.get("hash")

    if not filename or not size or total_chunks <= 0:
        raise HTTPException(status_code=400, detail="缺少必要参数")

    chunk_dir = getattr(app.state, "chunk_dir", get_chunk_dir())
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    up_dir = os.path.join(chunk_dir, upload_id)

    if not os.path.exists(up_dir):
        raise HTTPException(status_code=404, detail="上传会话不存在")

    for i in range(total_chunks):
        if not os.path.exists(os.path.join(up_dir, f"{i}.part")):
            raise HTTPException(status_code=409, detail=f"缺少分片 {i}")

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

        real_size = os.path.getsize(tmp_path)
        if real_size != size:
            raise HTTPException(status_code=422, detail="合并后大小不匹配")

        ok_hash = None
        if hash_algo == "sha256" and file_hash:
            with SESSION_LOCK:
                session = upload_sessions.get(upload_id)

            if session and len(session["chunk_hashes"]) == total_chunks:
                all_hashes_match = True
                for i in range(total_chunks):
                    part_path = os.path.join(up_dir, f"{i}.part")
                    with open(part_path, "rb") as f:
                        h = hashlib.sha256()
                        while chunk := f.read(1024 * 1024):
                            h.update(chunk)
                        actual_chunk_hash = h.hexdigest()

                    expected_chunk_hash = session["chunk_hashes"].get(i)
                    if expected_chunk_hash and actual_chunk_hash != expected_chunk_hash:
                        all_hashes_match = False
                        break

                if not all_hashes_match:
                    raise HTTPException(status_code=422, detail="分片哈希校验失败")

                print(f"使用分片哈希快速验证通过（upload_id: {upload_id}）", flush=True)
                ok_hash = file_hash
            else:
                ok_hash = file_sha256(tmp_path)
                if ok_hash != file_hash:
                    raise HTTPException(status_code=422, detail="哈希校验失败")

        final_path = os.path.join(upload_dir, filename)
        os.replace(tmp_path, final_path)

        try:
            for i in range(total_chunks):
                os.remove(os.path.join(up_dir, f"{i}.part"))
            if os.path.exists(os.path.join(up_dir, "__merge.tmp")):
                os.remove(os.path.join(up_dir, "__merge.tmp"))
            os.rmdir(up_dir)
        except Exception:
            pass

        with SESSION_LOCK:
            if upload_id in upload_sessions:
                del upload_sessions[upload_id]

        url = f"http://obs.dimond.top/{filename}"
        return Response(content=url, status_code=200, media_type="text/plain")
    except HTTPException:
        raise

# ============================================================================
# 健康检查
# ============================================================================

@app.get("/health")
async def health():
    return Response(content="OK")

# ============================================================================
# 启动服务器
# ============================================================================

if __name__ == "__main__":
    print(f"文件托管服务器启动: http://localhost:{PORT}", flush=True)
    print(f"上传命令示例: curl --upload-file your-file.wav http://obs.dimond.top/your-file.wav", flush=True)
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
