import os
import shutil
import json
import asyncio
import threading
from datetime import datetime
from urllib.parse import quote, unquote
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import uvicorn
import aiofiles
from contextlib import asynccontextmanager

# 加载环境变量
load_dotenv()
load_dotenv("env")
load_dotenv("asset/.env")

# 服务器配置
PORT = int(os.environ.get("PORT", 8088))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "obs")

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

# 内存存储 Notice 内容
NOTICE_CONTENT = ""
NOTICE_LOCK = asyncio.Lock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Ensure upload directory exists
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    yield
    # Shutdown

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
async def save_notice_file():
    content = await get_notice()
    if not content:
        raise HTTPException(status_code=400, detail="Notice is empty")
    
    # Generate filename: YYYYMMDDHHMMSS公告板.txt
    filename = datetime.now().strftime("%Y%m%d%H%M%S") + "公告板.txt"
    save_path = os.path.join(UPLOAD_DIR, filename)
    
    try:
        async with aiofiles.open(save_path, 'w', encoding='utf-8') as f:
            await f.write(content)
        return {"status": "ok", "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save notice: {str(e)}")

@app.get("/")
async def homepage(sort: str = Query("time", enum=["time", "ext"])):
    # 获取文件列表
    files_list = []
    if os.path.exists(UPLOAD_DIR):
        try:
            raw_files = [f for f in os.listdir(UPLOAD_DIR) if not f.startswith('.')]
            
            if sort == 'ext':
                # 按扩展名排序 (A-Z)
                raw_files.sort(key=lambda x: (os.path.splitext(x)[1].lower(), x))
            else:
                raw_files.sort(key=lambda x: os.path.getmtime(os.path.join(UPLOAD_DIR, x)), reverse=True)
                
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

            async function chunkedUpload(inputEl) {
                const file = inputEl.files && inputEl.files[0];
                if (!file) {
                    alert('请先选择文件');
                    return;
                }
                const filename = file.name;
                const total = file.size;
                let offset = 0;
                try {
                    while (offset < total) {
                        const end = Math.min(offset + CHUNK_SIZE_BROWSER, total);
                        const blob = file.slice(offset, end);
                        const resp = await fetch(`/${encodeURIComponent(filename)}`, {
                            method: 'PUT',
                            body: await blob.arrayBuffer(),
                        });
                        if (resp.status !== 201) {
                            const text = await resp.text();
                            throw new Error(`分片上传失败: ${resp.status} ${text}`);
                        }
                        offset = end;
                    }
                    alert('分片上传成功');
                    window.location.reload();
                } catch (err) {
                    alert('分片上传出错: ' + err.message);
                }
            }

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
    html = html.replace("{time_active}", time_active).replace("{ext_active}", ext_active)
    
    host = "obs.dimond.top"
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
            
        save_path = os.path.join(UPLOAD_DIR, filename)
        
        async with aiofiles.open(save_path, 'wb') as out_file:
            # 使用 10MB 分片读取并写入；若设置了 MAX_UPLOAD_SIZE，则进行累计校验
            total_written = 0
            while content := await upload_file.read(UPLOAD_CHUNK_SIZE):
                await out_file.write(content)
                total_written += len(content)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")
        
        return Response(content=f"文件上传成功: http://obs.dimond.top/{filename}", media_type="text/plain", status_code=201)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@app.put("/{filename}")
async def upload_file_put(filename: str, request: Request):
    filename = unquote(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
        
    save_path = os.path.join(UPLOAD_DIR, filename)
    try:
        async with aiofiles.open(save_path, 'wb') as out_file:
            # 保持与客户端流大小一致；若设置了 MAX_UPLOAD_SIZE，则进行累计校验
            total_written = 0
            async for chunk in request.stream():
                await out_file.write(chunk)
                total_written += len(chunk)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")
                
        file_url = f"http://obs.dimond.top/{filename}"
        return Response(content=file_url, media_type="text/plain", status_code=201)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

@app.get("/{filename}")
async def download_file(filename: str, request: Request):
    filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, filename)
    
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
            # 使用 40MB 分片进行完整流式下载
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
async def delete_file(filename: str):
    filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, filename)
    
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
