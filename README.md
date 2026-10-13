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
- **文件预览**：点击图片/PDF/HTML/音视频/文本(CSV)/Office 等后缀先进 **`/preview/...`**，右上角固定「⬇ 下载」按钮（见 §3.2）

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

依赖清单见 `requirements.txt`（fastapi / uvicorn[standard] / aiofiles / python-dotenv / python-multipart 等）。
若容器内缺失（`ModuleNotFoundError: No module named 'fastapi'`），可联网时执行：

```bash
pip3 install --break-system-packages fastapi "uvicorn[standard]" aiofiles python-dotenv python-multipart
```

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
| GET | `/obs/{filename:path}` | 直接访问 obs 目录文件（内联展示；预览页内嵌用） |
| GET | `/preview/{filename:path}` | **文件预览页**（图片/PDF/HTML/音视频/文本/CSV/Office…），右上角固定「⬇ 下载」按钮（见 §3.2） |
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

- **下载 / 打开**：页面加载后用已缓存的 `obsBase()` 结果**一次性改写**所有 `a[data-name]` 的 `href`
  （`applyDownloadHosts()`）：可达 `http://localhost:19082` → 用 localhost；不可达 → 回退 `http://ocs.dimond.top`；都不可达 → 相对当前 origin。
  **不在点击回调里做异步探测**——程序化 `a.click()` 发生在用户手势之外会被浏览器当弹窗拦截（曾导致「点击没反应、必须复制链接粘贴」）；
  改写 href 后点击走**原生跳转**，服务端响应带 `Content-Disposition: attachment`，原生跳转即直接下载。
- **上传 / 接口**：首页与 `/video` 页内 `obsBase()` 在页面加载时预热探测，缓存首个可达 host（`localhost:19082` 优先，回退 `ocs.dimond.top`，都不可达则用当前页 origin）；
  `obsFetch(url, opts)` 在其上拼路径。直传（`PUT /<file>`）、分片上传（`/upload/init`、`/upload/chunk/...`、`/upload/complete/...`）、删除（`DELETE`）均经此发出。
- **跨域**：页面 origin 与请求 host 可能不同（如从 `ocs.dimond.top` 载入却请求 `localhost:19082`），
  故服务端启用了 `CORSMiddleware`（`allow_origins=["*"]`），否则分片上传的预检（`PUT` + `X-Chunk-SHA256`）会被浏览器拦截。

> `ocs.dimond.top` = `dimond.top:19082` = 宿主机 `localhost:19082` = 容器内 `localhost:8082`，四者指向同一服务。
> 页面文字（首页标题/说明、`/video` 设置页）统一展示域名 `http://ocs.dimond.top`。
> 服务端生成的链接/示例前缀 `url_head = "http://ocs.dimond.top"`。

### 3.2 文件预览页（`/preview/{filename:path}`）

首页文件列表中，**可预览后缀的文件名点击后先进预览页**（不再是直接下载/新标签打开），页面**右上角固定一个醒目的蓝色「⬇ 下载」按钮**。
不可预览的类型（如 `.zip`/`.bin`）仍维持原行为（`data-name` 链接，走上方回退策略）。

| 类型 | 后缀 | 渲染方式 |
|------|------|----------|
| 图片 image | `jpg jpeg png gif webp bmp svg ico avif jfif` | `<img>` 自适应居中 |
| 音频 audio | `mp3 wav ogg oga m4a aac flac opus` | `<audio controls autoplay>` |
| 视频 video | `mp4 webm mov m4v ogv mkv` | `<video controls autoplay>` |
| PDF | `pdf` | `<iframe>` 内嵌 |
| HTML | `html htm` | `<iframe sandbox="allow-scripts allow-popups">`（无 `allow-same-origin`，隔离同源） |
| 文本/表格 | `txt csv tsv json md log xml yml yaml ini py js ts css sh sql …` | 前端 `fetch` 后渲染：CSV/TSV → 表格（支持引号与转义），其余 → `<pre>` 等宽 |
| Office/其他 | `doc docx xls xlsx ppt pptx odt ods odp rtf` 及所有其它后缀 | 占位卡片 + 大号下载按钮（提示「无法在线预览，请下载」） |

实现要点（`src/obs/server.py`）：

- 扩展名集合 `PREVIEW_*_EXTS` 与 `PREVIEW_EXTS`；`is_previewable()` / `preview_kind()` 判定类型。
- 页面内容统一走 **内联地址 `/obs/{filename}`**（同源相对路径，不涉及主机回退）；**下载按钮 href 指向 `/{filename}`**（服务端强制 `attachment`）。
- **路径穿越防护**：`safe_upload_path()` 用 `os.path.realpath` 校验目标必须落在 `obs/` 内，越界（`../`）返回 404。
- 路由 `@app.get("/preview/{filename:path}")` 必须声明在泛匹配 `/{filename}` **之前**。

```bash
# 预览页（返回 HTML，右上角带下载按钮）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/preview/report.csv   # 200
# 页面内下载按钮目标（attachment）
curl -sI http://127.0.0.1:8082/report.csv | grep -i content-disposition            # attachment; filename*=UTF-8''report.csv
```

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
| 2026-09-27 09:13~09:51 | R7 | 迭代细化域名/回退（上传下载统一先 `localhost:19082` 再域名；`obsBase()/obsFetch()`；新增 `CORSMiddleware`）；最后删除首页两行冗余说明文字 |
| 2026-09-27 10:06 | R8 | **rebase 到新远端**（未 push）：并入上游粘贴上传/实时速度/HLS 04:00 定时/`/video` 删除按钮 |
| 2026-09-27 19:29 | R9 | **再次 rebase**（未 push）：解决 `tools/`（宿主机只读 9p 挂载）被跟踪导致的卡壳——`reset --soft` 压提交、`git rm --cached tools`、写入 `.gitignore` 的 `/tools/` |
| 2026-10-01 16:50 | R10 | **rebase + 上传失败排查**：rebase 至 `38ebafa`（未 push）；定位 `/video` 上传器协议不符（camelCase/缺 `X-Chunk-SHA256`/无 complete body/`currentAbort` 空值/2MB≠10MB），重写修复（`614e537`）；首页分片循环补网络异常重试（`0196f0f`） |
| 2026-10-02 07:35 | R11 | **下载点击无反应排查**：`downloadWithFallback` 先 `preventDefault` 再异步 `a.click()`，丢了用户手势被弹窗拦截；改为加载期改写 `a[data-name]` href（`applyDownloadHosts`，`adc4192`） |
| 2026-10-13 02:11 | R12 | **文件预览页 + 推送分支 `ocs`**：可预览后缀点击先进 `/preview/...`（右上角醒目下载按钮），图片/音视频/PDF/HTML/文本(CSV 表格)/Office 占位；`git push` 到新分支 `ocs` |

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

**最后 3 轮对话总结（R10 / R11 / R12）：**

- **R10（rebase + 上传失败排查，2026-10-01）**：rebase 到上游 `38ebafa`（未 push）。用户问「为啥频繁上传失败、有日志吗」——
  结论：**有日志**（`logs/start.log` 是 uvicorn access log，`logs/server.log` 是业务事件），但**客户端/网络侧失败根本到不了服务器**，故 access log 看不到；
  真正原因是 `/video` 上传器 `app.js:uploadOneFile` 与服务端协议**完全对不上**（camelCase 参数→400、缺 `X-Chunk-SHA256`→422、complete 无 body→400、`currentAbort` 为 `null` 却取 `.signal`→TypeError、分片 2MB 与服务端 10MB 不一致）。
  修复：重写上传器（snake_case + 逐片哈希 + 纯 JS SHA-256 + 合并带 body + 重试 + 空值保护，`614e537`）；首页分片循环补 `try/catch` 网络异常重试（`0196f0f`）。
- **R11（下载点击无反应排查，2026-10-02）**：链接正确但点击无反应、必须复制粘贴到地址栏。根因：`downloadWithFallback()` 先 `ev.preventDefault()`
  再异步 HEAD 探测，最后在 `.then()` 里 `a.click()`——**程序化点击发生在用户手势之外被浏览器当弹窗拦截**；粘贴到地址栏属直接导航故能下。
  修复（`adc4192`）：不再在点击里做任何事，改为页面加载后用 `obsBase()` 结果**一次性改写**所有 `a[data-name]` 的 `href`（`applyDownloadHosts()`），点击走**原生跳转**即直接下载。
- **R12（文件预览页 + 推送 `ocs` 分支，2026-10-13）**：要求点击 `csv/xls/xlsx/doc/docx/pdf/html/jpeg/png/mp3` 等后缀先进入**预览页**，右上角有醒目下载按钮；并把成果 `git push` 到新分支 `ocs`。
  落地：新增 `/preview/{filename:path}` + 各类型渲染（图片/音视频/PDF/HTML/文本/CSV 表格/Office 占位）、`safe_upload_path()` 路径穿越防护、首页可预览项改链 `/preview/...`（`data-preview` 属性，避免被下载回退逻辑改写）。

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
| `ModuleNotFoundError: No module named 'fastapi'` | 容器内 Python 依赖缺失（PEP 668 externally-managed） | `pip3 install --break-system-packages fastapi "uvicorn[standard]" aiofiles python-dotenv python-multipart`（见 §1） |
| 预览页 404 / 下载按钮无反应 | 文件不在 `obs/`、或下载链接指向了跨域主机 | 预览页内联内容用同源 `/obs/...`、下载按钮用同源 `/{filename}`（不涉及主机回退）；越界路径由 `safe_upload_path()` 拦为 404 |
| 无法运行 `test_*.py` | 容器未安装 pytest（且无外网） | 有网环境 `pip install -r requirements.txt` 后 `pytest -q` |

---

## 8. 规范参考

- `systemreadme.md`：容器内规范（`run_claude.js` / `/ask/claude` + `/health` / 19081 Hub 注册 / Git）
- `SKILL.md`：本项目的开发与运维技能（启动、日志、Git、接口、变更流程）
- `user_history.md`：obs 与 obs-video 二合一的需求与特性记录
- `tools/obs-video/README.md`、`tools/obs-video/SKILL.md`：视频流应用的详细功能文档
