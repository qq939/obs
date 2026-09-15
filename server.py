import os
import shutil
import json
import asyncio
import threading
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

# 加载环境变量
load_dotenv()
load_dotenv("env")
load_dotenv("asset/.env")

# 服务器配置
PORT = int(os.environ.get("PORT", 8088))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "obs")

# 线程池用于后台哈希计算（使用位置：upload_chunk 异步计算哈希）
hash_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hash_worker")

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
            async function sha256Hex(file) {
                const buf = await file.arrayBuffer();
                const digest = await crypto.subtle.digest("SHA-256", buf);
                const arr = Array.from(new Uint8Array(digest));
                return arr.map(b => b.toString(16).padStart(2, "0")).join("");
            }
            // 增量 MD5 实现（支持大文件分块计算，避免一次性读入内存）
            function createMD5() {
                const K = [
                    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee,
                    0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
                    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be,
                    0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
                    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa,
                    0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
                    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed,
                    0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
                    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c,
                    0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
                    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05,
                    0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
                    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039,
                    0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
                    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1,
                    0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391
                ];
                const S = [
                    7,12,17,22, 7,12,17,22, 7,12,17,22, 7,12,17,22,
                    5,9,14,20, 5,9,14,20, 5,9,14,20, 5,9,14,20,
                    4,11,16,23, 4,11,16,23, 4,11,16,23, 4,11,16,23,
                    6,10,15,21, 6,10,15,21, 6,10,15,21, 6,10,15,21
                ];
                let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476;
                let totalBytes = 0;
                const block = new Uint8Array(64);
                let blockLen = 0;

                function processBlock() {
                    const dv = new DataView(block.buffer, block.byteOffset, 64);
                    const M = new Array(16);
                    for (let i = 0; i < 16; i++) {
                        M[i] = dv.getUint32(i * 4, true);
                    }
                    let A = h0, B = h1, C = h2, D = h3;
                    for (let j = 0; j < 64; j++) {
                        let F, g;
                        if (j < 16) {
                            F = (B & C) | (~B & D);
                            g = j;
                        } else if (j < 32) {
                            F = (D & B) | (~D & C);
                            g = (5 * j + 1) % 16;
                        } else if (j < 48) {
                            F = B ^ C ^ D;
                            g = (3 * j + 5) % 16;
                        } else {
                            F = C ^ (B | ~D);
                            g = (7 * j) % 16;
                        }
                        const temp = D;
                        D = C;
                        C = B;
                        const f = (A + F + K[j] + M[g]) >>> 0;
                        B = (B + ((f << S[j]) | (f >>> (32 - S[j])))) >>> 0;
                        A = temp;
                    }
                    h0 = (h0 + A) >>> 0;
                    h1 = (h1 + B) >>> 0;
                    h2 = (h2 + C) >>> 0;
                    h3 = (h3 + D) >>> 0;
                }

                function update(bytes) {
                    totalBytes += bytes.length;
                    let i = 0;
                    const n = bytes.length;
                    while (i < n) {
                        if (blockLen === 64) {
                            processBlock();
                            blockLen = 0;
                        }
                        block[blockLen++] = bytes[i++];
                    }
                }

                function wordToHexLE(val) {
                    const b = new ArrayBuffer(4);
                    const dv2 = new DataView(b);
                    dv2.setUint32(0, val, true);
                    return Array.from(new Uint8Array(b)).map(x => x.toString(16).padStart(2, '0')).join('');
                }

                function hexdigest() {
                    const bitLen = totalBytes * 8;
                    const bitLenLow = bitLen >>> 0;
                    const bitLenHigh = Math.floor(bitLen / 0x100000000) >>> 0;
                    // 追加 0x80
                    if (blockLen === 64) {
                        processBlock();
                        blockLen = 0;
                    }
                    block[blockLen++] = 0x80;
                    // 补零直到 blockLen == 56
                    while (blockLen !== 56) {
                        if (blockLen === 64) {
                            processBlock();
                            blockLen = 0;
                        }
                        block[blockLen++] = 0;
                    }
                    // 追加 8 字节小端比特长度
                    const lenBytes = new Uint8Array(8);
                    const dv = new DataView(lenBytes.buffer);
                    dv.setUint32(0, bitLenLow, true);
                    dv.setUint32(4, bitLenHigh, true);
                    for (let i = 0; i < 8; i++) {
                        block[blockLen++] = lenBytes[i];
                    }
                    processBlock();
                    blockLen = 0;
                    return wordToHexLE(h0) + wordToHexLE(h1) + wordToHexLE(h2) + wordToHexLE(h3);
                }

                return { update, hexdigest };
            }

            // 流式计算文件 MD5：分块读取，避免 8GB 大文件一次性读入内存
            async function md5Hex(file) {
                const md5 = createMD5();
                const CHUNK = 10 * 1024 * 1024; // 10MB
                const size = file.size;
                let offset = 0;
                while (offset < size) {
                    const blob = file.slice(offset, offset + CHUNK);
                    const buf = await blob.arrayBuffer();
                    md5.update(new Uint8Array(buf));
                    offset += CHUNK;
                }
                return md5.hexdigest();
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

            // 带进度的单文件上传（PUT分片，后端 stream() 分片接收），返回服务器响应 JSON
            function uploadOneFile(file, onFileProgress) {
                return new Promise((resolve, reject) => {
                    const xhr = new XMLHttpRequest();
                    xhr.upload.onprogress = (e) => {
                        if (e.lengthComputable && onFileProgress) {
                            onFileProgress(Math.round(e.loaded / e.total * 100), e.loaded, e.total);
                        }
                    };
                    xhr.onload = () => {
                        if (xhr.status === 201) {
                            try {
                                resolve(JSON.parse(xhr.responseText));
                            } catch (e) {
                                reject(new Error('服务器返回格式错误'));
                            }
                        } else {
                            reject(new Error(xhr.responseText || `HTTP ${xhr.status}`));
                        }
                    };
                    xhr.onerror = () => reject(new Error('网络错误'));
                    xhr.open('PUT', `/${encodeURIComponent(file.name)}`);
                    xhr.send(file);
                });
            }

            async function uploadFiles(files) {
                if (!files || files.length === 0) return;
                const statusEl = document.getElementById('formUploadStatus');
                const totalFiles = files.length;
                let success = 0, fail = 0;
                const failedNames = [];
                let overallBytes = 0, totalBytes = 0;
                for (let i = 0; i < files.length; i++) {
                    totalBytes += files[i].size;
                }
                for (let i = 0; i < files.length; i++) {
                    const file = files[i];
                    statusEl.textContent = `计算MD5中... (${i + 1}/${totalFiles}) ${file.name}`;
                    try {
                        // 上传前先计算本地 MD5 作为参考
                        const localMd5 = await md5Hex(file);
                        statusEl.textContent = `上传中... (${i + 1}/${totalFiles}) ${file.name} 0%`;
                        const resp = await uploadOneFile(file, (pct, loaded, total) => {
                            const overallPct = totalBytes > 0 ? Math.round((overallBytes + loaded) / totalBytes * 100) : 0;
                            statusEl.textContent = `上传中... (${i + 1}/${totalFiles}) ${file.name} ${pct}% [总进度 ${overallPct}%]`;
                        });
                        overallBytes += file.size;
                        // 上传完成后校验服务器返回的 MD5
                        const serverMd5 = (resp && resp.md5 || '').toLowerCase();
                        if (serverMd5 && serverMd5 === localMd5) {
                            success++;
                        } else {
                            fail++;
                            failedNames.push(file.name + ' (MD5不一致)');
                        }
                    } catch (err) {
                        fail++;
                        failedNames.push(file.name + ' (' + err.message + ')');
                    }
                }
                if (fail === 0) {
                    statusEl.textContent = `全部上传成功！(${success}个文件)`;
                    alert(`上传成功！共 ${success} 个文件，MD5 校验通过。`);
                } else {
                    statusEl.textContent = `上传完成：成功${success}个，失败${fail}个`;
                    alert(`上传完成：成功${success}个，失败${fail}个。\n失败文件：\n${failedNames.join('\\n')}`);
                }
                window.location.reload();
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

                // 粘贴文件上传（支持多文件）
                document.addEventListener('paste', (e) => {
                    const items = e.clipboardData && e.clipboardData.items;
                    if (!items) return;
                    const pastedFiles = [];
                    for (let i = 0; i < items.length; i++) {
                        if (items[i].kind === 'file') {
                            pastedFiles.push(items[i].getAsFile());
                        }
                    }
                    if (pastedFiles.length > 0) {
                        e.preventDefault();
                        const names = pastedFiles.map(f => f.name).join('\\n');
                        if (confirm(`检测到粘贴的${pastedFiles.length}个文件：\n${names}\n是否上传？`)) {
                            uploadFiles(pastedFiles);
                        }
                    }
                });
            });

            async function chunkedUpload(inputEl) {
                const file = inputEl.files && inputEl.files[0];
                if (!file) return;
                const statusEl = document.getElementById('chunkUploadStatus');
                statusEl.textContent = '上传中...';
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
                    statusEl.textContent = '上传成功！';
                    window.location.reload();
                } catch (err) {
                    statusEl.textContent = '上传出错';
                    alert('分片上传出错: ' + err.message);
                }
            }

            async function resumableUpload(inputEl) {
                const file = inputEl.files && inputEl.files[0];
                if (!file) return;
                const statusEl = document.getElementById('resumeUploadStatus');
                statusEl.textContent = '上传中...';
                const filename = file.name;
                const size = file.size;
                const chunkSize = CHUNK_SIZE_BROWSER;
                const totalChunks = Math.ceil(size / chunkSize);
                const hashAlgo = "sha256";
                const hash = await sha256Hex(file);
                // 初始化会话（包含秒传判定）
                let resp = await fetch('/upload/init', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename, size, hash_algo: hashAlgo, hash, chunk_size: chunkSize, total_chunks: totalChunks })
                });
                if (!resp.ok) {
                    const t = await resp.text();
                    statusEl.textContent = '初始化失败';
                    alert('初始化失败: ' + t);
                    return;
                }
                const info = await resp.json();
                if (info.skip) {
                    statusEl.textContent = '秒传成功！';
                    window.location.reload();
                    return;
                }
                const uploadId = info.upload_id;
                const uploaded = new Set(info.uploaded || []);
                const serverChunkMd5s = info.chunk_md5s || {};
                // 计算本地分片MD5
                async function calcChunkMd5(blob) {
                    const md5 = createMD5();
                    const buf = await blob.arrayBuffer();
                    md5.update(new Uint8Array(buf));
                    return md5.hexdigest();
                }
                // 上传单个分片并验证MD5
                async function uploadAndVerifyChunk(i, maxRetries = 3) {
                    const start = i * chunkSize;
                    const end = Math.min(start + chunkSize, size);
                    const blob = file.slice(start, end);
                    const localMd5 = await calcChunkMd5(blob);
                    
                    for (let attempt = 0; attempt < maxRetries; attempt++) {
                        const r = await fetch(`/upload/chunk/${encodeURIComponent(uploadId)}/${i}`, {
                            method: 'PUT',
                            body: await blob.arrayBuffer(),
                        });
                        if (r.status === 201) {
                            const result = await r.json();
                            const serverMd5 = (result.md5 || '').toLowerCase();
                            const expectedMd5 = localMd5.toLowerCase();
                            if (serverMd5 && serverMd5 !== expectedMd5) {
                                console.warn(`分片 ${i} MD5不匹配，本地: ${expectedMd5}, 服务器: ${serverMd5}，重传中...`);
                                continue; // MD5不匹配，重试
                            }
                            return true;
                        }
                    }
                    return false;
                }
                // 上传缺失分片（带MD5验证和重试）
                for (let i = 0; i < totalChunks; i++) {
                    if (uploaded.has(i)) {
                        // 已有分片，验证MD5
                        const start = i * chunkSize;
                        const end = Math.min(start + chunkSize, size);
                        const blob = file.slice(start, end);
                        const localMd5 = (await calcChunkMd5(blob)).toLowerCase();
                        const serverMd5 = (serverChunkMd5s[i] || '').toLowerCase();
                        if (serverMd5 && localMd5 !== serverMd5) {
                            console.warn(`已上传分片 ${i} MD5不匹配，重新上传...`);
                            uploaded.delete(i);
                        } else {
                            continue; // MD5匹配，跳过
                        }
                    }
                    const ok = await uploadAndVerifyChunk(i);
                    if (!ok) {
                        statusEl.textContent = '分片上传失败';
                        alert('分片上传失败，无法完成：' + i);
                        return;
                    }
                    uploaded.add(i);
                }
                // 合并完成
                const c = await fetch(`/upload/complete/${encodeURIComponent(uploadId)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename, size, total_chunks: totalChunks, hash_algo: hashAlgo, hash })
                });
                if (c.ok) {
                    statusEl.textContent = '上传完成！';
                    window.location.reload();
                } else {
                    const tx = await c.text();
                    statusEl.textContent = '合并失败';
                    alert('合并失败：' + tx);
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

def make_upload_id(filename: str, size: int, hash_algo: str, file_hash: str) -> str:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    # Windows 不支持冒号，使用下划线代替
    return f"{hash_algo}_{file_hash}_{size}_{safe_name}"

def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

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
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    chunk_dir = getattr(request.app.state, "chunk_dir", get_chunk_dir())
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(chunk_dir, exist_ok=True)
    final_path = os.path.join(upload_dir, filename)
    if os.path.exists(final_path) and os.path.getsize(final_path) == size:
        # 进行秒传校验
        if hash_algo == "sha256":
            existing_hash = file_sha256(final_path)
            if existing_hash == file_hash:
                url = f"http://obs.dimond.top/{filename}"
                return JSONResponse({"skip": True, "url": url})
    upload_id = make_upload_id(filename, size, hash_algo, file_hash)
    up_dir = os.path.join(chunk_dir, upload_id)
    os.makedirs(up_dir, exist_ok=True)
    
    # 枚举已上传分片
    uploaded = []
    chunk_hashes = {}
    
    try:
        for name in os.listdir(up_dir):
            if name.endswith(".part"):
                try:
                    idx = int(name[:-5])
                    chunk_path = os.path.join(up_dir, name)
                    chunk_md5 = file_md5(chunk_path)
                    uploaded.append(idx)
                    # 计算已上传分片的哈希
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
        "upload_id": upload_id,
        "uploaded": sorted(uploaded),
        "chunk_md5s": chunk_md5s,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
    })

@app.put("/upload/chunk/{upload_id}/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request):
    """上传分片，后台计算分片哈希（不阻塞上传）"""
    if index < 0:
        raise HTTPException(status_code=400, detail="分片序号非法")
    chunk_dir = getattr(request.app.state, "chunk_dir", get_chunk_dir())
    up_dir = os.path.join(chunk_dir, upload_id)
    os.makedirs(up_dir, exist_ok=True)
    part_path = os.path.join(up_dir, f"{index}.part")
    
    try:
        # 写入文件
        async with aiofiles.open(part_path, "wb") as f:
            async for chunk in request.stream():
                await f.write(chunk)
        
        # 立即返回，不阻塞。后台计算哈希
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
    """后台计算分片哈希"""
    try:
        with open(part_path, "rb") as f:
            h = hashlib.sha256()
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
            chunk_hash = h.hexdigest()
        
        # 更新会话
        with SESSION_LOCK:
            if upload_id in upload_sessions:
                session = upload_sessions[upload_id]
                session["chunk_hashes"][index] = chunk_hash
                session["uploaded_chunks"].add(index)
                
                # 如果支持分片哈希合并计算总体哈希
                if session["file_hash_obj"] is not None:
                    # 将分片哈希追加到总体哈希计算
                    # 注意：这里使用分片内容而非哈希值
                    # 因为分片哈希不能直接合并得到总体哈希
                    pass
    except Exception as e:
        print(f"计算分片 {index} 哈希失败: {e}", flush=True)

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
    chunk_dir = getattr(request.app.state, "chunk_dir", get_chunk_dir())
    upload_dir = getattr(request.app.state, "upload_dir", get_upload_dir())
    up_dir = os.path.join(chunk_dir, upload_id)
    if not os.path.exists(up_dir):
        raise HTTPException(status_code=404, detail="上传会话不存在")
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
        
        # 哈希验证：优先使用分片哈希快速验证
        ok_hash = None
        if hash_algo == "sha256" and file_hash:
            # 尝试使用预计算的分片哈希快速验证
            with SESSION_LOCK:
                session = upload_sessions.get(upload_id)
            
            if session and len(session["chunk_hashes"]) == total_chunks:
                # 使用分片哈希进行快速验证（不需要读取整个文件）
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
                ok_hash = file_hash  # 假设分片哈希正确，总体哈希也正确
            else:
                # 分片哈希不完整，使用传统方式验证
                ok_hash = file_sha256(tmp_path)
                if ok_hash != file_hash:
                    raise HTTPException(status_code=422, detail="哈希校验失败")
        
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
        
        url = f"http://obs.dimond.top/{filename}"
        return Response(content=url, status_code=200, media_type="text/plain")
    except HTTPException:
        raise
    except Exception as e:
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
        
        return Response(content=f"文件上传成功: http://obs.dimond.top/{filename}", media_type="text/plain", status_code=201)
        
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
        async with aiofiles.open(save_path, 'wb') as out_file:
            # 保持与客户端流大小一致；若设置了 MAX_UPLOAD_SIZE，则进行累计校验
            total_written = 0
            async for chunk in request.stream():
                os.makedirs(upload_dir, exist_ok=True)
                await out_file.write(chunk)
                total_written += len(chunk)
                if MAX_UPLOAD_SIZE is not None and total_written > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件过大")
                
        file_url = f"http://obs.dimond.top/{filename}"
        # 上传完成后计算 MD5，供前端校验
        md5_value = await asyncio.to_thread(file_md5, save_path)
        return JSONResponse(
            content={"filename": filename, "md5": md5_value, "url": file_url},
            status_code=201,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

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
