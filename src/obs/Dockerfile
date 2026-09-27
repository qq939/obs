# 使用轻量级 Python 镜像
FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建上传目录
RUN mkdir -p obs obs/.chunks

# 容器统一 Web App 端口（server.py 的 PORT 默认值同为 8082）
EXPOSE 8082

CMD ["python", "server.py"]
