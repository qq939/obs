import os
import http.server
import shutil
import socketserver
from urllib.parse import urlparse
from http import HTTPStatus

# 服务器配置
PORT = int(os.environ.get("PORT", 8088))  # 端口号（可修改，如 8080）
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "obs")  # 上传文件保存目录

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
        
        # 滚动删除逻辑
        try:
            files = [os.path.join(UPLOAD_DIR, f) for f in os.listdir(UPLOAD_DIR) 
                     if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
            
            if len(files) >= 100:
                files.sort(key=lambda x: os.path.getctime(x))
                while len(files) >= 100:
                    oldest_file = files.pop(0)
                    try:
                        os.remove(oldest_file)
                        print(f"滚动删除文件: {oldest_file}")
                    except Exception as e:
                        print(f"删除旧文件失败: {e}")
        except Exception as e:
            print(f"检查文件数量失败: {e}")

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
        # 解析请求的文件名
        parsed_path = urlparse(self.path)
        filename = os.path.basename(parsed_path.path)

        if not filename:
            # 根路径返回文件列表
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            
            # 获取文件列表
            files_list = []
            if os.path.exists(UPLOAD_DIR):
                try:
                    raw_files = [f for f in os.listdir(UPLOAD_DIR) if not f.startswith('.')]
                    # 按时间倒序排序
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
                    li { padding: 10px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; }
                    a { text-decoration: none; color: #007bff; }
                    a:hover { text-decoration: underline; }
                    .empty { color: #999; font-style: italic; }
                </style>
            </head>
            <body>
                <h1>文件托管列表 (Max 100)</h1>
                <p>上传命令示例: <code>curl --upload-file file.txt http://host/file.txt</code></p>
                <ul>
            """
            
            host = self.headers.get('Host', 'localhost')
            if not files_list:
                html += '<li class="empty">暂无文件</li>'
            else:
                for f in files_list:
                    file_url = f"http://{host}/{f}"
                    html += f'<li><a href="{file_url}" target="_blank">{f}</a> <span><a href="{file_url}" download>下载</a></span></li>'
            
            html += """
                </ul>
            </body>
            </html>
            """
            self.wfile.write(html.encode('utf-8'))
            return

        # 读取并返回文件
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            self.path = file_path  # 指向上传目录中的文件
            return super().do_GET()  # 调用父类方法处理文件返回
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "not a file")

    def do_POST(self):
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
    with socketserver.TCPServer(("", PORT), FileHandler) as httpd:
        print(f"文件托管服务器启动: http://localhost:{PORT}", flush=True)
        print(f"上传命令示例: curl --upload-file your-file.wav http://obs.dimond.top/your-file.wav", flush=True)
        print(f"文件保存目录: {os.path.abspath(UPLOAD_DIR)}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止", flush=True)