# OBS - Object Storage & Video Platform

二合一项目：OBS 文件存储 + 视频播放平台

## 架构

```
├── src/
│   ├── obs/           # OBS 文件存储服务 (8088端口)
│   └── obs-video-app/ # 视频播放应用 (80端口)
├── obs/               # 文件存储目录
├── obs_shards/        # HLS 分片存储
├── logs/              # 日志目录
├── docker-compose.yml # Docker Compose 配置
├── Dockerfile.obs     # OBS 服务 Dockerfile
└── Dockerfile.obs-video-app # 视频应用 Dockerfile
```

## 快速开始

```bash
# 启动所有服务
docker-compose up -d

# 查看服务状态
docker-compose ps

# 查看日志
docker-compose logs -f
```

## 服务说明

- **OBS 服务**: http://localhost:8088 - 文件上传和存储
- **视频应用**: http://localhost/ - 视频播放和列表

## 开发

```bash
# 本地开发
cd src/obs
python -m uvicorn server:app --reload

# 运行测试
pytest test_*.py -v
```
