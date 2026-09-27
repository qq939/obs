---
name: web-app-8082
description: 开发、测试、排查与维护容器内 Web App 8082（OBS 文件托管 + /video 视频流二合一，FastAPI）。涉及启动脚本 user_start.sh、日志规范（start.log / server.log / agent_tui.log）、Git 提交、/ask/claude + /health 链路、上传/播放/HLS 扩展与故障排查。
---

# SKILL — Web App 8082（OBS 文件托管 + 视频流）

## 适用场景

在容器内开发 / 维护 **端口 8082** 的 Web 应用时使用。覆盖：

- 启动脚本 `user_start.sh` 的创建与维护（幂等启动 + 健康检查 + 日志）
- `src/obs/server.py`（OBS 文件托管 + `/video` 视频流 + `/ask/claude` + `/health`）的扩展与测试
- 日志规范：`logs/start.log`（启动）、`logs/server.log`（运行）、`logs/agent_tui.log`（问答）
- Git 提交与 `logs/commit.txt` 记录
- 故障排查（ffmpeg、端口、外网）

## 关键事实（速查）

| 项 | 值 |
|----|----|
| 项目根目录 | `/home/agent/.claude/workspace/project` |
| 域名 | **`http://ocs.dimond.top`**（= `http://dimond.top:19082` = 宿主机 `http://localhost:19082` = 容器内 `http://localhost:8082`） |
| 端口 | `8082`（容器内部） |
| 主程序 | `src/obs/server.py`（FastAPI） |
| 默认页 `/` | **文件托管页面**（OBS 上传/下载/列表/公告板） |
| `/video` | **视频流播放页面**（竖屏翻页，HLS/直连） |
| 上传/下载策略 | 先试直连 `http://localhost:19082`（加速），失败回退域名 `http://ocs.dimond.top`：下载用 `downloadWithFallback()`；上传/接口用 `obsBase()`/`obsFetch()`（页面加载预热探测）；跨域由 `CORSMiddleware` 放行。网页只展示域名 |
| ffmpeg | 内置 `bin/ffmpeg`、`bin/ffprobe`（`user_start.sh` 加入 PATH；缺失用 `scripts/install_ffmpeg.sh` 获取） |
| 启动脚本 | `user_start.sh`（被 `start.sh` 调用） |
| 启动日志 | `logs/start.log`；运行日志 `logs/server.log`；问答日志 `logs/agent_tui.log` |
| 存储目录 | `obs/`（文件 + `.chunks/` 分片）、`obs_shards/`（HLS 产物） |
| Claude 入口 | `run_claude.js`（**禁止直接调 claude CLI**） |
| 上游源仓库 | `https://github.com/qq939/obs.git`（obs）；`tools/obs-video/`（视频应用） |

## 启动 / 停止

```bash
cd /home/agent/.claude/workspace/project
./user_start.sh                        # 幂等启动，日志 -> logs/start.log
tail -f logs/start.log
ss -ltnp | grep 8082

# 重启（改代码后）
bash scripts/restart_obs.sh            # = 结束旧进程 + ./user_start.sh

# ffmpeg/ffprobe 缺失时（bin/ 被 gitignore，不随仓库分发）
bash scripts/install_ffmpeg.sh         # 经 npm registry 拉取静态二进制到 bin/
```

启动链路：`start.sh` → `user_start.sh` → `python3 src/obs/server.py`
（`PORT=8082 UPLOAD_DIR=obs HLS_DIR=obs_shards LOG_DIR=logs`，并把 `bin/` 加入 PATH）→ 轮询 `/health`。

## 接口清单

```text
GET  /health                              健康检查 -> {"status":"ok"}
GET/POST /ask/claude?q=...                问 Claude（URL 或 base64），走 run_claude.js
GET  /                                    ★ 文件托管默认页（?sort=time|ext）
GET  /video                               ★ 视频流页面
GET  /video/{style.css,app.js,vendor/hls.min.js}   视频页静态资源
GET  /videos                              视频列表（含 duration，随机）
POST /hls/generate-all                    批量生成缺失/过期 HLS（?dry_run=true 仅查看）
GET  /hls/{name}/index.m3u8               HLS 播放列表（懒生成）
GET  /hls/{name}/seg-NNNNN.ts             HLS 分片
POST /upload/init                         分片上传初始化（sha256+size 断点续传/秒传）
PUT  /upload/chunk/{id}/{index}           上传分片
POST /upload/status/{id}                  查询上传进度
POST /upload/complete/{id}                合并完成
POST /                                    表单上传
PUT  /{filename}                          简单上传（流式）
GET  /obs/{filename}                      访问/播放 obs 下文件
GET  /{filename}                          下载（Range 206；?download=1 强制下载）
DELETE /{filename}                        删除
GET/POST /notice, POST /save_notice       公告板读写/存文件
WS   /ws                                  公告板实时广播
```

`/ask/claude` 编码规则：`q` 含空格或长度 `<50` → URL 解码，否则 → base64 解码。成功 `200` 纯文本；失败 `500`。

## 日志规范

- `logs/start.log`：`user_start.sh` 写入（目录准备、PID、健康检查结果）。
- `logs/server.log`：应用 `log_line()` 追加（上传/删除等业务事件）。
- `logs/agent_tui.log`：`run_claude.js` 以 `\n[YYYY-MM-DD HH:MM:SS] $ <消息>\n` 追加提问与输出。

## Git 规范（必做）

```bash
cd /home/agent/.claude/workspace/project
git config --local user.name  "hermit-agent"
git config --local user.email "agent@hermit.local"
[ -d .git ] || git init
git add -A && git commit -m "描述本次变更"
echo "$(git rev-parse --short HEAD) 描述本次变更" >> logs/commit.txt
```

`.gitignore` 至少包含：`logs/ node_modules/ .DS_Store __pycache__/ *.log .env uploads/ dist/ build/`，
并按本项目追加 `obs/ obs_shards/ sessions/ tools/obs-video/`。

## 变更流程（推荐）

1. 改 `src/obs/server.py` / `src/obs/video_static/*` → `python3 -m py_compile src/obs/server.py`。
2. `kill "$(pgrep -f '[s]rc/obs/server.py')"; ./user_start.sh`，看 `logs/start.log` 是否就绪。
3. 功能自测：`/health` → `/`（文件页）→ `/video` → `/videos` → `PUT /x` → `GET /obs/x` → `DELETE /x`。
4. 跑回归：`python3 -m pytest test_server.py test_resumable.py src/obs/test_*.py -q`（需 ffmpeg 的用例可能跳过）。
5. 更新 `README.md` / `SKILL.md`；`git add -A && git commit`，记录 `logs/commit.txt`。

## 扩展指引

- **新增接口**：在 `src/obs/server.py` 用 `@app.get/post/put/delete` 定义；**注意路由顺序**——
  泛匹配的 `/{filename}`（GET/PUT/DELETE）必须放在 `/video`、`/ask/claude` 等具体路由之后。
- **Claude 问答**：沿用 `ask_claude` → `asyncio.create_subprocess_exec('node', run_claude.js)` +
  `CLAUDE_MSG`(base64) + `CLAUDE_CAPTURE_STDIO=1`，勿绕开 `run_claude.js`。
- **视频前端**：改 `src/obs/video_static/`；由 `get_video_static_dir()` 定位，`/video` 路由直接读文件。
- **域名 / 上传下载回退**：站点域名 `http://ocs.dimond.top`；**页面文字只展示该域名**，链接前缀常量 `url_head = "http://ocs.dimond.top"`（`src/obs/server.py` 顶部），
  驱动首页 curl 示例、文件列表下载链接与上传响应 URL。**页面上传/下载请求为加速先试直连 `http://localhost:19082`，不可达回退域名 `http://ocs.dimond.top`**：
  下载/打开用 `downloadWithFallback(event, el, isDownload)`；上传/接口（PUT、`/upload/init|chunk|complete`、DELETE）用
  `obsBase()`（页面加载预热探测）/`obsFetch()`，跨域由 `CORSMiddleware` 放行（常量 `DOWNLOAD_HOST_PRIMARY` / `DOWNLOAD_HOST_FALLBACK`）。
- **ffmpeg**：`bin/ffmpeg`、`bin/ffprobe` 由 `scripts/install_ffmpeg.sh` 获取；`user_start.sh` 自动加入 PATH。
- **端口**：默认 8082（`PORT` 环境变量可覆盖）。
- **Hub 注册**：按 `systemreadme.md` §三 POST 到 `http://host.docker.internal:19081/api/tools`，
  `name` 由容器名派生，`doc_md` 含功能概览 + 端点表 + curl 示例 + `/ask/claude`。

## 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| HLS 失败 `ffmpeg/ffprobe not found` | `bin/` 被 gitignore，未随仓库分发 | `bash scripts/install_ffmpeg.sh` 拉取静态二进制；`user_start.sh` 会自动把 `bin/` 加入 PATH |
| HLS `ffmpeg 退出码 1: ... No such file or directory` | `UPLOAD_DIR` 相对路径 + ffmpeg `cwd=tmp_dir` | 已在 `_do_generate_hls()` 用 `os.path.abspath()` 修复，勿改回相对路径 |
| `git clone github.com/qq939/obs.git` 直连超时 | 容器直连 github.com 被拦截（443） | 走镜像：`https://ghproxy.net/https://github.com/qq939/obs/archive/refs/heads/main.tar.gz` |
| 下载走错地址 | 跨网访问时直连 `http://localhost:19082` 不可达 | 页面 `downloadWithFallback()` 会自动回退 `http://ocs.dimond.top`，无需手工改链接 |
| 8082 不通 | 进程退出/端口占用 | 看 `logs/start.log`；`kill "$(pgrep -f '[s]rc/obs/server.py')"` 后 `./user_start.sh` |
| `/video` 404 | 缺 `src/obs/video_static/` | 补齐 `index.html/app.js/style.css/vendor/hls.min.js` |
| `/ask/claude` 500 | claude CLI / 环境未就绪 | 查 `logs/agent_tui.log`；确认 `ANTHROPIC_*` 与 `run_claude.js` |
| 页面错乱 | 路由顺序被 `/{filename}` 抢占 | 具体路由必须定义在泛匹配 `/{filename}` 之前 |

## 参考

- `README.md`：项目总览、目录结构、API、构建细节、最近对话总结
- `systemreadme.md`：容器内规范（run_claude.js / 8082 / Hub / Git）
- `tools/obs-video/README.md`、`tools/obs-video/SKILL.md`：上游视频应用完整文档
