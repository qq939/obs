# 使用官方的 Ubuntu 镜像作为基础镜像
FROM ubuntu:latest


# 避免在安装过程中出现交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# 确保 /app 目录存在
RUN mkdir -p /app

# 工作目录
WORKDIR /app

# 复制当前目录下的文件和文件夹到工作目录
COPY . /app


# 确保 /app 目录有足够的权限
RUN chmod -R 755 /app

# 更新系统软件包列表并安装必要的软件包
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# 设置 Python3 为默认的 Python 版本
RUN ln -s /usr/bin/python3 /usr/bin/python

# 设置 Pip3 为默认的 Pip 版本
RUN rm -rf /usr/bin/pip && ln -s /usr/bin/pip3 /usr/bin/pip

# Create a virtual environment
RUN cd /app && python3 -m venv /app/venv

# Create log directory and set permissions
RUN mkdir -p /var/log/ && chmod 777 /var/log/

# Activate the virtual environment and install any needed packages specified in requirements.txt
RUN cd /app && /app/venv/bin/pip install --upgrade pip && \
    /app/venv/bin/pip install -r /app/requirements.txt

# Make port 8088 available to the world outside this container
EXPOSE 8088

# Use the Python interpreter from the virtual environment to run the application
CMD ["/bin/sh", "-c", "/app/venv/bin/python /app/server.py > /var/log/obs.log 2>&1"]
    
