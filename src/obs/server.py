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
    raw = f"{filename}:{size}:{hash_algo}:{file_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16] + "-" + str(int(datetime.now().timestamp()))

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
# 静态文件和主页
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    chunk_dir = getattr(app.state, "chunk_dir", get_chunk_dir())
    files = []
    try:
        for name in os.listdir(upload_dir):
            p = os.path.join(upload_dir, name)
            if os.path.isfile(p) and not name.startswith("."):
                files.append({
                    "name": name,
                    "size": os.path.getsize(p),
                    "time": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
                })
    except Exception:
        pass
    files.sort(key=lambda x: x["time"], reverse=True)
    total_size = sum(f["size"] for f in files)
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>文件托管服务</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ color: #333; margin-bottom: 20px; text-align: center; }}
        .upload-area {{ background: white; border-radius: 8px; padding: 40px; text-align: center; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .upload-area.dragover {{ border: 2px dashed #007bff; background: #f0f7ff; }}
        .file-input {{ display: none; }}
        .upload-btn {{ background: #007bff; color: white; border: none; padding: 12px 32px; border-radius: 6px; cursor: pointer; font-size: 16px; }}
        .upload-btn:hover {{ background: #0056b3; }}
        .progress-bar {{ width: 100%; height: 4px; background: #e0e0e0; border-radius: 2px; margin-top: 16px; display: none; }}
        .progress-fill {{ height: 100%; background: #007bff; border-radius: 2px; width: 0%; transition: width 0.3s; }}
        .stats {{ text-align: center; margin-bottom: 20px; color: #666; font-size: 14px; }}
        .file-list {{ background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .file-item {{ padding: 16px; border-bottom: 1px solid #eee; display: flex; align-items: center; }}
        .file-item:last-child {{ border-bottom: none; }}
        .file-icon {{ width: 40px; height: 40px; background: #e3f2fd; border-radius: 8px; display: flex; align-items: center; justify-content: center; margin-right: 16px; font-size: 20px; }}
        .file-info {{ flex: 1; }}
        .file-name {{ font-weight: 500; color: #333; word-break: break-all; }}
        .file-meta {{ font-size: 12px; color: #999; margin-top: 4px; }}
        .file-actions {{ display: flex; gap: 8px; }}
        .action-btn {{ padding: 6px 12px; border: 1px solid #ddd; background: white; border-radius: 4px; cursor: pointer; font-size: 12px; }}
        .action-btn:hover {{ background: #f5f5f5; }}
        .nav {{ display: flex; gap: 20px; margin-bottom: 20px; justify-content: center; }}
        .nav a {{ padding: 10px 20px; background: white; border-radius: 6px; text-decoration: none; color: #333; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .nav a:hover {{ background: #007bff; color: white; }}
        .empty {{ text-align: center; padding: 60px; color: #999; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📦 文件托管服务</h1>
        <div class="nav">
            <a href="/">📁 文件管理</a>
            <a href="/video">🎬 视频播放</a>
        </div>
        <div class="upload-area" id="uploadArea">
            <div>拖拽文件到此处，或</div>
            <input type="file" class="file-input" id="fileInput" multiple>
            <button class="upload-btn" onclick="document.getElementById('fileInput').click()">选择文件</button>
            <div class="progress-bar" id="progressBar">
                <div class="progress-fill" id="progressFill"></div>
            </div>
        </div>
        <div class="stats">共 {len(files)} 个文件，总计 {total_size / 1024 / 1024:.2f} MB</div>
        <div class="file-list">
            {"".join(f'''
            <div class="file-item" data-name="{f["name"]}">
                <div class="file-icon">📄</div>
                <div class="file-info">
                    <div class="file-name">{f["name"]}</div>
                    <div class="file-meta">{f["time"]} · {f["size"] / 1024:.1f} KB</div>
                </div>
                <div class="file-actions">
                    <button class="action-btn" onclick="copyLink('{quote(f["name"])}')">复制链接</button>
                    <button class="action-btn" onclick="deleteFile('{quote(f["name"])}')">删除</button>
                </div>
            </div>
            ''' for f in files) if files else '<div class="empty">暂无文件，上传一个吧</div>'}
        </div>
    </div>
    <script>
        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');
        const progressBar = document.getElementById('progressBar');
        const progressFill = document.getElementById('progressFill');

        uploadArea.addEventListener('dragover', e => {{ e.preventDefault(); uploadArea.classList.add('dragover'); }});
        uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
        uploadArea.addEventListener('drop', e => {{
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            uploadFiles(e.dataTransfer.files);
        }});
        fileInput.addEventListener('change', () => uploadFiles(fileInput.files));

        async function uploadFiles(files) {{
            for (const file of files) {{
                await uploadFile(file);
            }}
        }}

        async function uploadFile(file) {{
            progressBar.style.display = 'block';
            progressFill.style.width = '0%';

            const formData = new FormData();
            formData.append('file', file);

            const xhr = new XMLHttpRequest();
            xhr.upload.onprogress = e => {{
                if (e.lengthComputable) {{
                    progressFill.style.width = (e.loaded / e.total * 100) + '%';
                }}
            }};

            await new Promise((resolve, reject) => {{
                xhr.onload = () => {{
                    progressBar.style.display = 'none';
                    if (xhr.status === 201) {{
                        location.reload();
                    }} else {{
                        alert('上传失败: ' + xhr.responseText);
                    }}
                    resolve();
                }};
                xhr.onerror = () => {{ reject(); }};
                xhr.open('POST', '/upload');
                xhr.send(formData);
            }});
        }}

        async function copyLink(name) {{
            const url = location.origin + '/' + name;
            await navigator.clipboard.writeText(url);
            alert('链接已复制');
        }}

        async function deleteFile(name) {{
            if (!confirm('确定删除 ' + name + '？')) return;
            const resp = await fetch('/delete/' + name, {{ method: 'DELETE' }});
            if (resp.ok) location.reload();
            else alert('删除失败');
        }}
    </script>
</body>
</html>"""
    return html

# ============================================================================
# 视频页面
# ============================================================================

@app.get("/video", response_class=HTMLResponse)
async def video_page():
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    videos = []
    VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mkv"}
    
    try:
        for name in os.listdir(upload_dir):
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTS:
                p = os.path.join(upload_dir, name)
                videos.append({
                    "name": name,
                    "size": os.path.getsize(p),
                    "time": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M"),
                    "has_hls": hls_exists(name)
                })
    except Exception:
        pass
    
    videos.sort(key=lambda x: x["time"], reverse=True)
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>视频播放</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: white; min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        h1 {{ text-align: center; margin-bottom: 20px; }}
        .nav {{ display: flex; gap: 20px; margin-bottom: 20px; justify-content: center; }}
        .nav a {{ padding: 10px 20px; background: #16213e; border-radius: 6px; text-decoration: none; color: white; }}
        .nav a:hover {{ background: #0f3460; }}
        .nav a.active {{ background: #e94560; }}
        .video-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; }}
        .video-card {{ background: #16213e; border-radius: 12px; overflow: hidden; transition: transform 0.2s; }}
        .video-card:hover {{ transform: translateY(-4px); }}
        .video-thumb {{ width: 100%; aspect-ratio: 16/9; background: #0f3460; display: flex; align-items: center; justify-content: center; font-size: 48px; cursor: pointer; }}
        .video-info {{ padding: 16px; }}
        .video-name {{ font-size: 14px; margin-bottom: 8px; word-break: break-all; }}
        .video-meta {{ font-size: 12px; color: #888; }}
        .video-badge {{ display: inline-block; padding: 2px 8px; background: #e94560; border-radius: 4px; font-size: 10px; margin-left: 8px; }}
        .empty {{ text-align: center; padding: 100px; color: #888; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 视频库</h1>
        <div class="nav">
            <a href="/">📁 文件管理</a>
            <a href="/video" class="active">🎬 视频播放</a>
        </div>
        <div class="video-grid">
            {"".join(f'''
            <div class="video-card">
                <div class="video-thumb" onclick="playVideo('{quote(f["name"])}', {str(f["has_hls"]).lower()})">▶️</div>
                <div class="video-info">
                    <div class="video-name">{f["name"]}{'<span class="video-badge">HLS</span>' if f["has_hls"] else ''}</div>
                    <div class="video-meta">{f["time"]} · {f["size"] / 1024 / 1024:.1f} MB</div>
                </div>
            </div>
            ''' for f in videos) if videos else '<div class="empty">暂无视频</div>'}
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script>
        function playVideo(name, hasHls) {{
            const url = '/video/play/' + name;
            const win = window.open('', '_blank', 'width=1280,height=720');
            if (hasHls) {{
                win.document.write(`
                    <video id="video" controls style="width:100%;height:100%;background:black;" autoplay></video>
                    <script>
                        fetch('/video/hls/' + name + '/index.m3u8')
                            .then(r => r.text())
                            .then(hlsContent => {{
                                if (Hls.isSupported()) {{
                                    const hls = new Hls();
                                    hls.loadSource(URL.createObjectURL(new Blob([hlsContent])));
                                    hls.attachMedia(document.getElementById('video'));
                                }} else {{
                                    document.getElementById('video').src = '/video/play/' + name;
                                }}
                            }});
                    </script>
                `);
            }} else {{
                win.document.write('<video src="' + url + '" controls autoplay style="width:100%;height:100%;background:black;"></video>');
            }}
        }}
    </script>
</body>
</html>"""
    return html

@app.get("/video/play/{filename}")
async def video_play(filename: str):
    """直接播放视频（不支持 Range 请求时降级）"""
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="视频不存在")
    return FileResponse(file_path, media_type="video/mp4")

@app.get("/video/hls/{filename}/{path:path}")
async def video_hls(filename: str, path: str):
    """HLS 分片流"""
    hls_dir = os.path.join(HLS_DIR, filename)
    file_path = os.path.join(hls_dir, path)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    if path.endswith(".m3u8"):
        return FileResponse(file_path, media_type="application/vnd.apple.mpegurl")
    else:
        return FileResponse(file_path, media_type="video/MP2T")

@app.get("/video/api/list")
async def video_list():
    """获取视频列表"""
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    videos = []
    VIDEO_EXTS = {".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mkv"}
    
    try:
        for name in os.listdir(upload_dir):
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTS:
                p = os.path.join(upload_dir, name)
                videos.append({
                    "name": name,
                    "size": os.path.getsize(p),
                    "time": datetime.fromtimestamp(os.path.getmtime(p)).isoformat(),
                    "has_hls": hls_exists(name)
                })
    except Exception:
        pass
    
    return JSONResponse({"videos": videos})

@app.post("/video/api/generate-hls/{filename}")
async def generate_hls(filename: str):
    """触发 HLS 生成"""
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="视频不存在")
    
    if hls_exists(filename):
        return JSONResponse({"status": "ready", "message": "HLS 已存在"})
    
    asyncio.create_task(generate_hls_background(filename))
    return JSONResponse({"status": "processing", "message": "HLS 生成中"})

@app.get("/video/api/status/{filename}")
async def video_status(filename: str):
    """查询视频处理状态"""
    with TASK_LOCK:
        task = video_tasks.get(filename, {"status": "not_found"})
    return JSONResponse(task)

# ============================================================================
# 文件上传接口
# ============================================================================

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    os.makedirs(upload_dir, exist_ok=True)
    
    filename = file.filename or "unnamed"
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
    dest = os.path.join(upload_dir, safe_name)
    
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return Response(content="OK", status_code=201)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete/{filename}")
async def delete_file(filename: str):
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        os.remove(file_path)
        invalidate_hls(filename)
        return Response(content="OK")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{filename}")
async def download_file(filename: str):
    upload_dir = getattr(app.state, "upload_dir", get_upload_dir())
    file_path = os.path.join(upload_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, filename=filename)

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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
