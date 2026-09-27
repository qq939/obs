# OBS Web App（端口 8082）— 文件托管 + 视频流二合一

容器内 Web App，监听 **8082**，把 **OBS 文件托管**（图床/存储桶/断点续传/公告板）与
**obs-video 竖屏视频流**（`/video`）合二为一，并内建平台必备的
**`/ask/claude`**（走 `run_claude.js`）与 **`/health`** 接口。

- **技术栈**：Python 3 + FastAPI + uvicorn（`src/obs/server.py`），前端原生 JS + hls.js
- **项目根目录**：`/home/agent/.claude/workspace/project`
- **域名 / 端口**：对外访问 **`http://ocs.dimond.top`**（= 宿主机 `http://localhost:19082` = `http://dimond.top:19082`；容器内 `http://localhost:8082`）
- **启动脚本**：`user_start.sh`（由 `start.sh` 在容器启动时调用）
- **启动日志**：`logs/start.log`；问答日志：`logs/agent_tui.log`；应用日志：`logs/server.log`
- **上传/下载策略**：页面请求**先试直连 `http://localhost:19082`（加速）**，不可用自动回退域名 **`http://ocs.dimond.top`**；网页文字只展示域名（见 §3.1）

---

## 1. 快速开始

```bash
cd /home/agent/.claude/workspace/project

# 启动（幂等：8082 已有健康实例则跳过），全部输出写入 logs/start.log
./user_start.sh

# 验证
curl -s http://127.0.0.1:8082/health          # -> {"status":"ok"}
curl -s http://127.0.0.1:8082/videos          # -> {"videos":[...]}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/        # 200（文件托管首页）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/video   # 200（视频流页面）
```

浏览器：**`http://ocs.dimond.top/`**（文件托管，默认页） / **`http://ocs.dimond.top/video`**（视频流）。
容器内自测可用 `http://127.0.0.1:8082/`。

依赖已随容器预装（fastapi 0.141.1 / uvicorn 0.54.0 / aiofiles / python-dotenv / python-multipart / requests），无需联网安装。

---

## 2. 目录结构

```
project/
├── src/obs/
│   ├── server.py            # ★ 应用主体（FastAPI）：OBS 托管 + /video + /ask/claude + /health
│   ├── video_static/        # 视频流前端（index.html / app.js / style.css / vendor/hls.min.js）
│   ├── requirements.txt     # Python 依赖清单
│   ├── Dockerfile           # 源仓库自带镜像定义
│   └── test_*.py            # 单测（需 pytest）
├── bin/                     # 内置静态 ffmpeg / ffprobe（运行时，已 gitignore；scripts/install_ffmpeg.sh 获取）
├── scripts/                 # install_ffmpeg.sh（获取 ffmpeg/ffprobe）、restart_obs.sh（重启 8082）
├── user_start.sh            # ★ 启动脚本 → python3 src/obs/server.py，日志写 logs/start.log
├── start.sh                 # 容器入口：调用 user_start.sh
├── run_claude.js            # ★ 唯一 Claude 调用入口（问答写入 logs/agent_tui.log）
├── Dockerfile.obs           # 镜像定义（Python 3.11-slim + ffmpeg）
├── docker-compose.yml       # 容器编排
├── requirements.txt         # 根依赖清单
├── obs/                     # 文件/视频存储（运行时，已 gitignore，保留 .gitkeep）
│   └── .chunks/             # 分片上传临时目录（断点续传会话）
├── obs_shards/              # HLS 产物 m3u8 + ts（运行时，已 gitignore）
├── logs/                    # 日志（已 gitignore）：start.log / server.log / agent_tui.log
├── systemreadme.md          # 容器内规范
├── CLAUDE.md / AGENTS.md    # 项目与 Agent 约定
├── test_*.py                # 集成/回归测试（根目录，需 pytest）
├── .gitignore
├── README.md                # 本文件
├── SKILL.md                 # 开发/运维技能说明
├── tools/                   # 平台工具集合（obs 图床 / hub / email / github / obs-video 上游仓库）
└── sessions/                # Agent 会话记录（已 gitignore）
```

> 8082 服务由 **`src/obs/server.py`** 提供；上游 Node 版视频应用源码保留在 `tools/obs-video/`（独立 git 仓库，仅供参照）。

---

## 3. API 端点

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/health` | 健康检查 → `{"status":"ok"}` |
| GET/POST | `/ask/claude?q=...` | 向 Claude 提问（走 `run_claude.js`）；`q` 含空格或长度<50 → URL 解码，否则 → base64 解码 |
| GET | `/` | 文件托管首页（列表，`?sort=time\|ext`） |
| POST | `/` | 表单上传（multipart） |
| PUT | `/{filename}` | 简单上传（流式） |
| GET | `/{filename}` | 下载（支持 `Range` → 206） |
| DELETE | `/{filename}` | 删除文件 |
| POST | `/upload/init` | 分片上传初始化（sha256 秒传/断点续传） |
| PUT | `/upload/chunk/{upload_id}/{index}` | 上传分片 |
| POST | `/upload/status/{upload_id}` | 查询分片上传进度 |
| POST | `/upload/complete/{upload_id}` | 合并分片完成上传 |
| GET | `/obs/{filename:path}` | 直接访问 obs 目录文件（视频流用） |
| GET | `/videos` | 视频列表（含时长） |
| GET | `/video` `/video/style.css` `/video/app.js` `/video/vendor/hls.min.js` | 视频流页面与静态资源 |
| POST | `/hls/generate-all` | 批量生成 HLS（后台串行，可 `?dry_run=true`） |
| GET | `/hls/cron` | 查询定时 HLS 任务状态 |
| GET | `/hls/{filename}/{path:path}` | 提供 HLS 产物（m3u8/ts） |
| POST | `/compress/{filename}` | 服务端 ffmpeg 压缩（依赖 ffmpeg） |
| GET/POST | `/notice`、`POST /save_notice` | 公告板读/写/存档 |
| WS | `/ws` | 公告板实时广播 |

### 3.1 上传 / 下载地址回退（直连加速 vs 域名）

页面所有**上传与下载**请求都遵循同一策略：**先试直连 `http://localhost:19082`（可能更快），不可达再回退域名 `http://ocs.dimond.top`**。
两者指向同一服务，仅为加速；**页面文字只展示域名 `http://ocs.dimond.top`**，直连地址不出现在网页上。

- **下载 / 打开**：文件列表的「文件名 / 下载」链接点击时调用 `downloadWithFallback()`：
  1. 以 `fetch(url, {method:'HEAD', mode:'no-cors'})` 探测 `http://localhost:19082`；
  2. 成功 → 用 `http://localhost:19082/<文件名>` 下载/打开；失败 → 回退 `http://ocs.dimond.top/<文件名>`。
- **上传 / 接口**：首页与 `/video` 页内 `obsBase()` 在页面加载时预热探测，缓存首个可达 host（`localhost:19082` 优先，回退 `ocs.dimond.top`，都不可达则用当前页 origin）；
  `obsFetch(url, opts)` 在其上拼路径。直传（`PUT /<file>`）、分片上传（`/upload/init`、`/upload/chunk/...`、`/upload/complete/...`）、删除（`DELETE`）均经此发出。
- **跨域**：页面 origin 与请求 host 可能不同（如从 `ocs.dimond.top` 载入却请求 `localhost:19082`），
  故服务端启用了 `CORSMiddleware`（`allow_origins=["*"]`），否则分片上传的预检（`PUT` + `X-Chunk-SHA256`）会被浏览器拦截。

> `ocs.dimond.top` = `dimond.top:19082` = 宿主机 `localhost:19082` = 容器内 `localhost:8082`，四者指向同一服务。
> 页面文字（首页标题/说明、`/video` 设置页）统一展示域名 `http://ocs.dimond.top`。
> 服务端生成的链接/示例前缀 `url_head = "http://ocs.dimond.top"`。

### 调用示例

```bash
# 平台问答
curl "http://127.0.0.1:8082/ask/claude?q=你好，请介绍一下自己"
curl "http://127.0.0.1:8082/ask/claude?q=$(echo '复杂中文内容' | base64)"

# 文件上传 / 下载 / 删除
curl -X PUT --data-binary @a.txt http://127.0.0.1:8082/a.txt
curl http://127.0.0.1:8082/a.txt
curl -X DELETE http://127.0.0.1:8082/a.txt
```

---

## 4. 运行与数据

- **存储**：上传文件 → `obs/`；分片临时数据 → `obs/.chunks/`；HLS 产物 → `obs_shards/`。
- **日志**：
  - `logs/start.log`：`user_start.sh` 启动过程（目录准备 / PID / 健康检查）。
  - `logs/server.log`：应用 `log_line()` 输出的业务日志（同时打到 stdout）。
  - `logs/agent_tui.log`：所有 `/ask/claude` 问答（`run_claude.js` 追加）。
- **幂等启动**：`user_start.sh` 先探测 `/health`，已就绪则不重复拉起。

---

## 5. 项目构建结构与细节（`logs/agent_tui.log` 梳理）

`logs/agent_tui.log` 是 Claude 调用的统一入口记录（`run_claude.js` 以 `[时间] $ <提问>` 追加）。当前内容：

| 时间 | 轮次 | 内容摘要 |
|------|------|----------|
| 2026-09-27 16:07:14 | R1 | 初始交付指令：非回合交付、禁止 plan、8082 Web App、检查/创建 `user_start.sh`（日志 `logs/start.log`）、整理 `agent_tui.log`、更新 `README.md`/`SKILL.md` |
| 2026-09-27 08:07:14 | R2 | 同上指令重发（容器时钟回拨/重试导致的重复记录） |
| 2026-09-27 08:07:33 | R3 | **「8082 端口部署：`https://github.com/qq939/obs.git` 的 main 分支」** |
| 2026-09-27 08:13:40 | R4 | **纠正默认页**：「默认页面是文件存储页面，`/video` 才是视频流页面」 |
| 2026-09-27 08:14:22 | R5 | `hi`（探活） |
| 2026-09-27 09:08:11 | R6 | **域名与下载策略**：域名 `http://ocs.dimond.top`；网页文字改用域名；下载先试 `http://localhost:19082`、失败回退 `http://ocs.dimond.top` |

**构建结构（由日志、`user_history.md` 与仓库状态还原）：**

1. **项目定位**：任务要求把 `qq939/obs.git` 的 obs 应用部署到 8082。本项目即该 obs 应用的容器内落地：
   根目录即部署目录，`src/obs/server.py` 为应用主体，`user_start.sh` 负责拉起。
2. **二合一背景**（见 `user_history.md`）：将 **obs（文件托管）** 与 **obs-video（竖屏视频流）** 合并——
   obs 主页为 `/`，视频流集成到 **`/video`**，两者共享同一份 `obs/` 存储与 `/videos`、HLS 能力。
3. **平台契约**：容器须提供 `/ask/claude` + `/health`（8082）。原 obs 应用仅有 `/health`，
   本次为其**新增 `/ask/claude`**（`@app.api_route("/ask/claude")`，spawn `run_claude.js`，编码规则同上）；
   同时把 `PORT` 默认值由 `8088` 改为 **`8082`**。
4. **启动链路**：容器 `start.sh` → `user_start.sh` → `python3 src/obs/server.py`（env：`PORT=8082 UPLOAD_DIR=obs HLS_DIR=obs_shards LOG_DIR=logs`）
   → 轮询 `/health` 就绪。
5. **Git 契约**：项目根目录须为 git 仓库，每次对话后提交；容器无默认身份，需 `git config --local`。

**最后 3 轮对话总结（R4 / R5 / R6）：**

- **R4（纠正默认页）**：明确 `/` 是**文件存储页**、`/video` 才是视频流页；首页标题/路由据此核对。
- **R5（`hi`）**：连通性探活，无功能变更。
- **R6（域名与下载策略）**：对外域名为 **`http://ocs.dimond.top`**（= `dimond.top:19082` = 宿主机 `localhost:19082` = 容器内 `localhost:8082`）。
  本轮落地：① 网页文字改为展示域名（首页 `OBS 文件托管服务` + 访问地址、`/video` 设置页「域名」行）；
  ② 文件下载/打开改为**先探测 `http://localhost:19082`、失败回退 `http://ocs.dimond.top`**（`downloadWithFallback()`）；
  ③ 顺带修复 `_do_generate_hls()` 相对路径导致 ffmpeg 找不到源文件的问题，并内置 `bin/ffmpeg`/`bin/ffprobe`（`scripts/install_ffmpeg.sh`）。

**R1~R3 回顾：** R1/R2 为初始交付指令（非回合、一次交付，落盘 `user_start.sh` + README/SKILL + logs）；
R3 要求把 **`qq939/obs.git` main 分支部署到 8082** —— 直连 github.com 被拦截，改经 `ghproxy.net` 镜像取得 main 源码，
落地为 `src/obs/server.py`，并补齐平台契约 `/ask/claude` + `/health`、把 `PORT` 默认值改为 `8082`。

---

## 6. `tools/` 工具集（平台侧）

| 目录 | 说明 |
|------|------|
| `tools/obs/` | OBS 图床/文件托管源（FastAPI `server.py`，默认 8082；分片上传、公告板 WebSocket、Hub 注册） |
| `tools/obs-video/` | 竖屏视频流上游仓库（Node `server.js` + `public/`，独立 git：`qq939/obs-video-app.git`） |
| `tools/hub/` | 19081 Tools Hub（Flask）：工具知识库、`/api/tools` 注册/查询/注销、`/p/<name>` 代理 |
| `tools/email/` | 邮件收发（FastAPI，白名单收件 + 任意发件） |
| `tools/github/` | Git/GitHub 操作技能（`SKILL.md` + 命令映射 + 脚本） |

---

## 7. 已知限制与故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| HLS 报 `ffmpeg 退出码 1: ... No such file or directory` | `UPLOAD_DIR` 为相对路径时 ffmpeg 以 `cwd=tmp_dir` 运行导致相对源路径失效 | 已在 `_do_generate_hls()` 用 `os.path.abspath()` 修正（本仓库已修复） |
| `ffmpeg`/`ffprobe` 不可用（HLS、时长、压缩降级） | 容器 apt 无 ffmpeg 包，`static-ffmpeg`/`ffmpeg-static` 需访问 github.com（被拦截） | 已内置：`bin/ffmpeg`、`bin/ffprobe`（`user_start.sh` 自动加入 PATH）；缺失时执行 `bash scripts/install_ffmpeg.sh`（走可达的 npm registry） |
| `git clone github.com/qq939/obs.git` 直连超时 | 容器**无法直连 github.com（443 拦截）** | 走镜像：`https://ghproxy.net/https://github.com/qq939/obs/archive/refs/heads/main.tar.gz`（本次即以此取得 main 分支源码） |
| 8082 无响应 | 进程退出/端口占用 | `ss -ltnp \| grep 8082`；`kill <pid>` 后 `./user_start.sh`；看 `logs/start.log` |
| 健康检查失败 | 依赖缺失或语法错误 | `python3 -m py_compile src/obs/server.py`；确认已装 fastapi/uvicorn |
| 无法运行 `test_*.py` | 容器未安装 pytest（且无外网） | 有网环境 `pip install -r requirements.txt` 后 `pytest -q` |

---

## 8. 规范参考

- `systemreadme.md`：容器内规范（`run_claude.js` / `/ask/claude` + `/health` / 19081 Hub 注册 / Git）
- `SKILL.md`：本项目的开发与运维技能（启动、日志、Git、接口、变更流程）
- `user_history.md`：obs 与 obs-video 二合一的需求与特性记录
- `tools/obs-video/README.md`、`tools/obs-video/SKILL.md`：视频流应用的详细功能文档
