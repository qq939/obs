import os
import http.server
import shutil
import socketserver
import json
import pymysql
import traceback
import asyncio
import threading
import websockets
from dotenv import load_dotenv
from urllib.parse import urlparse, unquote, quote, parse_qs
from http import HTTPStatus

# 加载环境变量
load_dotenv()
load_dotenv("env")
load_dotenv("asset/.env")

# 服务器配置
PORT = int(os.environ.get("PORT", 8088))  # 端口号（可修改，如 8080）
WS_PORT = PORT + 1  # WebSocket 端口
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "obs")  # 上传文件保存目录

# 数据库配置
DB_HOST = os.environ.get("MYSQL_HOST", "localhost")
DB_PORT = int(os.environ.get("MYSQL_PORT", 3306))
DB_USER = os.environ.get("MYSQL_USER", "root")
DB_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
DB_NAME = os.environ.get("MYSQL_DATABASE", "obs")

# Mock DB for testing
MOCK_DB_DATA = {"content": ""}

# WebSocket 客户端集合
connected_clients = set()

def get_db_connection():
    if os.environ.get("MOCK_DB") == "true":
        return None
    try:
        return pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
    except Exception as e:
        print(f"Database connection failed: {e}", flush=True)
        print(traceback.format_exc(), flush=True)  # 打印完整堆栈
        return None

def init_db():
    if os.environ.get("MOCK_DB") == "true":
        return

    conn = get_db_connection()
    if not conn:
        print("Skipping DB initialization due to connection failure.", flush=True)
        return
    try:
        with conn.cursor() as cursor:
            # 创建表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notice_board (
                    id INT PRIMARY KEY DEFAULT 1,
                    content TEXT
                )
            """)
            # 确保有一行数据
            cursor.execute("SELECT * FROM notice_board WHERE id=1")
            if not cursor.fetchone():
                default_text = ""
                cursor.execute("INSERT INTO notice_board (id, content) VALUES (1, %s)", (default_text,))
        conn.commit()
        print("Database initialized successfully.", flush=True)
    except Exception as e:
        print(f"Database initialization failed: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
    finally:
        conn.close()

def get_notice():
    if os.environ.get("MOCK_DB") == "true":
        return MOCK_DB_DATA["content"]

    conn = get_db_connection()
    if not conn:
        return "Database Error"
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT content FROM notice_board WHERE id=1")
            result = cursor.fetchone()
            return result['content'] if result else ""
    finally:
        conn.close()

def update_notice(content):
    if os.environ.get("MOCK_DB") == "true":
        MOCK_DB_DATA["content"] = content
        return True

    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE notice_board SET content=%s WHERE id=1", (content,))
        conn.commit()
        return True
    finally:
        conn.close()

async def ws_handler(websocket):
    # 注册客户端
    connected_clients.add(websocket)
    try:
        # 发送当前公告内容给新连接的客户端
        current_content = get_notice()
        await websocket.send(json.dumps({"type": "init", "content": current_content}))
        
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "update":
                new_content = data.get("content", "")
                # 更新数据库
                update_notice(new_content)
                # 广播给其他客户端
                broadcast_msg = json.dumps({"type": "update", "content": new_content})
                for client in connected_clients:
                    if client != websocket:
                        try:
                            await client.send(broadcast_msg)
                        except websockets.exceptions.ConnectionClosed:
                            pass
                            
            elif msg_type == "reset":
                default_text = ""
                # 更新数据库
                update_notice(default_text)
                # 广播给所有客户端（包括发送者）
                broadcast_msg = json.dumps({"type": "update", "content": default_text})
                for client in connected_clients:
                    try:
                        await client.send(broadcast_msg)
                    except websockets.exceptions.ConnectionClosed:
                        pass
                        
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # 注销客户端
        connected_clients.remove(websocket)

def run_ws_server():
    """在单独线程中运行 WebSocket 服务器"""
    async def serve():
        async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
            print(f"WebSocket 服务器启动: ws://localhost:{WS_PORT}", flush=True)
            await asyncio.Future()  # 永久运行

    # 创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(serve())

# 初始化数据库
init_db()

# 启动 WebSocket 服务器线程
ws_thread = threading.Thread(target=run_ws_server, daemon=True)
ws_thread.start()

# 创建上传目录（如果不存在）
os.makedirs(UPLOAD_DIR, exist_ok=True)


class FileHandler(http.server.SimpleHTTPRequestHandler):
    def _handle_file_save(self, filename, file_data):
        """通用 通用文件保存逻辑
        """
        if not filename:
            return False, "文件名不能为空"

        # 确保目录存在
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        
        save_path = os.path.join(UPLOAD_DIR, filename)

        try:
            with open(save_path, "wb") as f:
                f.write(file_data)
            file_url = f"http://{self.headers.get('Host', 'obs.dimond.top')}/{filename}"
            return True, file_url
        except Exception as e:
            return False, f"保存失败: {str(e)}"
    def do_PUT(self):
        """处理 PUT 请求（对应 curl --upload-file 上传）"""
        # 解析 URL 中的文件名
        parsed_path = urlparse(self.path)
        filename = os.path.basename(parsed_path.path)
        filename = unquote(filename)

        if not filename:
            self.send_error(HTTPStatus.BAD_REQUEST, "文件名不能为空")
            return

        # 读取请求体（文件内容）并写入本地
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            self._handle_file_save(filename, self.rfile.read(content_length))

            # 上传成功，返回文件访问 URL
            file_url = f"http://obs.dimond.top/{filename}"
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(file_url.encode("utf-8"))

        except Exception as e:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"上传失败: {str(e)}")

    def do_GET(self):
        """处理 GET 请求（访问已上传的文件）"""
        # 处理公告板获取
        if self.path == '/notice':
            content = get_notice()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"content": content}).encode('utf-8'))
            return

        # 解析请求的文件名
        parsed_path = urlparse(self.path)
        filename = os.path.basename(parsed_path.path)
        filename = unquote(filename)
        
        if not filename:
            # 根路径返回文件列表
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            
            # 解析排序参数
            query_params = parse_qs(parsed_path.query)
            sort_by = query_params.get('sort', ['time'])[0]

            # 获取文件列表
            files_list = []
            if os.path.exists(UPLOAD_DIR):
                try:
                    raw_files = [f for f in os.listdir(UPLOAD_DIR) if not f.startswith('.')]
                    
                    if sort_by == 'ext':
                        # 按扩展名排序 (A-Z)
                        raw_files.sort(key=lambda x: (os.path.splitext(x)[1].lower(), x))
                    else:
                        # 默认：按时间倒序排序
                        raw_files.sort(key=lambda x: os.path.getctime(os.path.join(UPLOAD_DIR, x)), reverse=True)
                        
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
                        height: 100px;
                        border: 1px solid #ccc;
                        resize: vertical;
                        font-family: monospace;
                        box-sizing: border-box; /* ensure padding doesn't overflow */
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
                </style>
                <script>
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

                    // Notice Board Logic
                    document.addEventListener('DOMContentLoaded', () => {
                        const noticeArea = document.getElementById('notice-content');
                        
                        // WebSocket connection
                        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                        // 使用当前页面端口 + 1 连接 WebSocket
                        const wsPort = parseInt(window.location.port || (window.location.protocol === 'https:' ? 443 : 80)) + 1;
                        const wsUrl = `${wsProtocol}//${window.location.hostname}:${wsPort}`;
                        
                        let ws;
                        let isConnected = false;

                        function connect() {
                            ws = new WebSocket(wsUrl);

                            ws.onopen = () => {
                                console.log('WebSocket connected');
                                isConnected = true;
                            };

                            ws.onmessage = (event) => {
                                const data = JSON.parse(event.data);
                                if (data.type === 'init' || data.type === 'update') {
                                    // 只有当内容不同时才更新，避免光标跳动（虽然 textarea 会重置光标，但这是基本优化）
                                    // 如果当前用户正在输入，尽量不要覆盖，除非是强制同步
                                    // 简单的协同编辑策略：直接覆盖
                                    if (noticeArea.value !== data.content) {
                                         // 保存当前光标位置
                                        const start = noticeArea.selectionStart;
                                        const end = noticeArea.selectionEnd;
                                        
                                        noticeArea.value = data.content;
                                        
                                        // 如果当前元素聚焦，尝试恢复光标（可能会有些问题，但比直接跳到最后好）
                                        if (document.activeElement === noticeArea) {
                                            noticeArea.setSelectionRange(start, end);
                                        }
                                    }
                                }
                            };

                            ws.onclose = () => {
                                console.log('WebSocket disconnected, reconnecting...');
                                isConnected = false;
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
                <h1>文件托管列表</h1>
                
                <!-- 公告板模块 -->
                <div class="notice-board">
                    <button class="btn-close-notice" onclick="resetNotice()" title="重置公告">x</button>
                    <textarea id="notice-content" placeholder="公告板..."></textarea>
                </div>

                <p>上传命令示例: <code>curl --upload-file file.txt http://obs.dimond.top/file.txt</code></p>
                <div style="margin: 20px 0; padding: 10px; border: 1px solid #eee; background: #f9f9f9;">
                    <h3>上传文件</h3>
                    <form action="/" method="post" enctype="multipart/form-data">
                        <input type="file" name="file" required>
                        <input type="submit" value="上传">
                    </form>
                </div>
                
                <div class="sort-controls">
                    排序方式: 
                    <a href="?sort=time" class="{time_active}">按时间 (最新)</a>
                    <a href="?sort=ext" class="{ext_active}">按扩展名 (A-Z)</a>
                </div>

                <ul>
            """
            
            # 动态设置 active 类
            time_active = "active" if sort_by != 'ext' else ""
            ext_active = "active" if sort_by == 'ext' else ""
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
            self.wfile.write(html.encode('utf-8'))
            return

        # 读取并返回文件
        file_path = os.path.join(UPLOAD_DIR, filename)
        
        # DEBUG
        import sys
        print(f"DEBUG: Checking {file_path}", file=sys.stderr)
        if os.path.exists(UPLOAD_DIR):
            print(f"DEBUG: Dir content: {os.listdir(UPLOAD_DIR)}", file=sys.stderr)
            
        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                f = open(file_path, 'rb')
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return

            try:
                self.send_response(HTTPStatus.OK)
                ctype = self.guess_type(file_path)
                self.send_header("Content-Type", ctype)
                
                fs = os.fstat(f.fileno())
                self.send_header("Content-Length", str(fs[6]))
                self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
                # 强制下载，解决 JSON 等文件在浏览器直接打开的问题
                # 使用 RFC 5987 标准支持非 ASCII 文件名
                encoded_filename = quote(filename)
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_filename}")
                self.end_headers()
                
                shutil.copyfileobj(f, self.wfile)
            finally:
                f.close()
            return
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "not a file")

    def do_DELETE(self):
        """处理 DELETE 请求"""
        parsed_path = urlparse(self.path)
        filename = os.path.basename(parsed_path.path)
        filename = unquote(filename)

        if not filename:
             self.send_error(HTTPStatus.BAD_REQUEST, "文件名不能为空")
             return
             
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                os.remove(file_path)
                self.send_response(HTTPStatus.OK)
                self.end_headers()
                self.wfile.write(b"Deleted")
            except Exception as e:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Delete failed: {str(e)}")
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")

    def do_POST(self):
        """处理 POST 请求"""
        # 处理公告板更新
        if self.path == '/notice':
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                if 'content' in data:
                    update_notice(data['content'])
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok"}).encode())
                else:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Missing content")
            except Exception as e:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
            return

        """处理 POST 上传（手动解析 multipart/form-data）"""
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "ONLY SUPPORT multipart/form-data")
            return

        # 提取 boundary（分隔符）
        boundary = content_type.split("boundary=")[-1].strip()
        if not boundary:
            self.send_error(HTTPStatus.BAD_REQUEST, "LACK of boundary segment")
            return
        boundary = f"--{boundary}".encode("utf-8")  # 完整边界（前加 --）

        # 读取请求体数据
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "request body is empty")
            return
        data = self.rfile.read(content_length)

        # 分割数据为多个部分（每个部分对应一个表单字段）
        parts = data.split(boundary)
        file_data = None
        filename = None

        for part in parts:
            if not part.strip():
                continue  # 跳过空部分

            # 分割头部和内容（头部以 \r\n\r\n 结束）
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue  # 无效部分，跳过

            header = part[:header_end].decode("utf-8")
            content = part[header_end + 4:-2]  # 去除末尾的 \r\n

            # 从头部提取文件名（找 Content-Disposition 中的 filename）
            if "Content-Disposition" in header:
                for line in header.split("\r\n"):
                    if "filename=" in line:
                        # 提取文件名（处理引号包裹的情况，如 filename="test.txt"）
                        filename = line.split("filename=")[-1].strip('"\'')
                        filename = os.path.basename(filename)  # 过滤路径
                        file_data = content  # 记录文件内容
                        break

        if not filename or file_data is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "no file find")
            return

        # 保存文件
        success, msg = self._handle_file_save(filename, file_data)
        if success:
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"文件上传成功: {msg}".encode("utf-8"))
        else:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, msg)

# 启动服务器
if __name__ == "__main__":
    # 使用 ThreadingTCPServer 支持多线程并发处理请求
    class ThreadingServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
    with ThreadingServer(("", PORT), FileHandler) as httpd:
        print(f"文件托管服务器启动: http://localhost:{PORT}", flush=True)
        print(f"上传命令示例: curl --upload-file your-file.wav http://obs.dimond.top/your-file.wav", flush=True)
        print(f"文件保存目录: {os.path.abspath(UPLOAD_DIR)}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止", flush=True)