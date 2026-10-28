# OBS Web App（端口 8082）— 文件托管 + 视频流二合一

容器内 Web App，监听 **8082**，把 **OBS 文件托管**（图床/存储桶/断点续传/公告板）与
**obs-video 竖屏视频流**（`/video`）合二为一，并内建平台必备的
**`/ask/claude`**（走 `run_claude.js`）与 **`/health`** 接口。

- **技术栈**：Python 3 + FastAPI + uvicorn（`src/obs/server.py`），前端原生 JS + hls.js
- **项目根目录**：`/home/agent/.claude/workspace/project`
- **域名 / 端口**：对外访问 **`https://ocs.dimond.top`**（反代终止 TLS；http 访问会 301 跳转 https）= 宿主机 `http://localhost:19082` = 容器内 `http://localhost:8082`
- **启动脚本**：`user_start.sh`（由 `start.sh` 在容器启动时调用）
- **启动日志**：`logs/start.log`；问答日志：`logs/agent_tui.log`；应用日志：`logs/server.log`
- **上传/下载策略**：页面请求**先试直连 `http://localhost:19082`（加速）**，不可用自动回退域名 **`https://ocs.dimond.top`**；网页文字只展示域名（见 §3.1）
- **文件预览**：点击图片/PDF/HTML/音视频/文本(CSV)/Office 等后缀先进 **`/preview/...`**，右上角固定「⬇ 下载」按钮；**Office（docx/xlsx/xls/pptx）用纯 Python 库转 HTML 内嵌预览**，不依赖 Office 套件（见 §3.2）

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

浏览器：**`https://ocs.dimond.top/`**（文件托管，默认页） / **`https://ocs.dimond.top/video`**（视频流）。
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
│   ├── .chunks/             # 分片上传临时目录（断点续传会话）
│   └── .folder-staging/     # ★ 文件夹上传暂存区（<folder_id>/data + manifest.json；打包成功即删）
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
| POST | `/api/folder/init` | **文件夹上传会话**：`{folder_name,total_files,total_bytes,empty_dirs[],resume_folder_id?}` → `{folder_id}` |
| GET | `/api/folder/{folder_id}/manifest` | 查询文件夹上传进度（已完成/未完成文件、字节数、空目录） |
| PUT | `/api/folder/{folder_id}/file/{relpath:path}` | 文件夹内**单文件直传**（≤10MB；可选 `X-File-SHA256` 整段校验） |
| POST | `/api/folder/{folder_id}/zip` | 后台把暂存文件夹打包成 **一个 zip**（`{zip_name?}`） |
| GET | `/api/folder/{folder_id}/zip/status` | 查询打包进度（`queued/running/done/failed` + zip 名称/大小） |
| GET | `/obs/{filename:path}` | 直接访问 obs 目录文件（内联展示；预览页内嵌用） |
| GET | `/preview/{filename:path}` | **文件预览页**（图片/PDF/HTML/音视频/文本/CSV/Office…），右上角固定「⬇ 下载」按钮（见 §3.2） |
| GET | `/preview-content/{filename:path}` | Office（docx/xlsx/xls/pptx）转换后的 **HTML**（`text/html`），供预览页 iframe 内嵌 |
| GET | `/videos` | 视频列表（含时长） |
| GET | `/video` `/video/style.css` `/video/app.js` `/video/vendor/hls.min.js` | 视频流页面与静态资源 |
| POST | `/hls/generate-all` | 批量生成 HLS（后台串行，可 `?dry_run=true`） |
| GET | `/hls/cron` | 查询定时 HLS 任务状态 |
| GET | `/hls/{filename}/{path:path}` | 提供 HLS 产物（m3u8/ts） |
| POST | `/compress/{filename}` | 服务端 ffmpeg 压缩（依赖 ffmpeg） |
| GET/POST | `/notice`、`POST /save_notice` | 公告板读/写/存档 |
| WS | `/ws` | 公告板实时广播 |

### 3.1 上传 / 下载地址回退（直连加速 vs 域名）

页面所有**上传与下载**请求都遵循同一策略：**先试直连 `http://localhost:19082`（可能更快），不可达再回退域名 `https://ocs.dimond.top`**。
两者指向同一服务，仅为加速；**页面文字只展示域名 `https://ocs.dimond.top`**，直连地址不出现在网页上。
回退必须用 **https**：页面本身是 https，向 http 域名发 `fetch`/XHR 会被浏览器按混合内容拦截。

- **下载 / 打开**：页面加载后用已缓存的 `obsBase()` 结果**一次性改写**所有 `a[data-name]` 的 `href`
  （`applyDownloadHosts()`）：可达 `http://localhost:19082` → 用 localhost；不可达 → 回退 `https://ocs.dimond.top`；都不可达 → 相对当前 origin。
  **不在点击回调里做异步探测**——程序化 `a.click()` 发生在用户手势之外会被浏览器当弹窗拦截（曾导致「点击没反应、必须复制链接粘贴」）；
  改写 href 后点击走**原生跳转**，服务端响应带 `Content-Disposition: attachment`，原生跳转即直接下载。
- **上传 / 接口**：首页与 `/video` 页内 `obsBase()` 在页面加载时预热探测，缓存首个可达 host（`localhost:19082` 优先，回退 `ocs.dimond.top`，都不可达则用当前页 origin）；
  `obsFetch(url, opts)` 在其上拼路径。直传（`PUT /<file>`）、分片上传（`/upload/init`、`/upload/chunk/...`、`/upload/complete/...`）、删除（`DELETE`）均经此发出。
- **跨域**：页面 origin 与请求 host 可能不同（如从 `ocs.dimond.top` 载入却请求 `localhost:19082`），
  故服务端启用了 `CORSMiddleware`（`allow_origins=["*"]`），否则分片上传的预检（`PUT` + `X-Chunk-SHA256`）会被浏览器拦截。

> `ocs.dimond.top`（https，反代终止 TLS）= 宿主机 `localhost:19082` = 容器内 `localhost:8082`，指向同一服务。
> 页面文字（首页标题/说明、`/video` 设置页）统一展示域名 `https://ocs.dimond.top`。
> 服务端生成的链接/示例前缀 `url_head = "https://ocs.dimond.top"`。

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
| Office（可转换） | `docx xlsx xlsm xls pptx` | **服务端用纯 Python 库转成 HTML**，预览页 `<iframe src="/preview-content/...">` 内嵌：`docx`→mammoth，`xlsx/xlsm`→openpyxl，`xls`→xlrd，`pptx`→python-pptx；**Excel 逐 sheet 渲染**（多表带顶部 `sheet-nav` 切换条） |
| Office（其余）/其他 | `doc ppt odt ods odp rtf` 及所有其它后缀 | 占位卡片 + 大号下载按钮（提示「暂不支持在线预览，请下载」） |

实现要点（`src/obs/server.py`）：

- 扩展名集合 `PREVIEW_*_EXTS` 与 `PREVIEW_EXTS`；`is_previewable()` / `preview_kind()` 判定类型。
- 页面内容统一走 **内联地址 `/obs/{filename}`**（同源相对路径，不涉及主机回退）；**下载按钮 href 指向 `/{filename}`**（服务端强制 `attachment`）。
- **路径穿越防护**：`safe_upload_path()` 用 `os.path.realpath` 校验目标必须落在 `obs/` 内，越界（`../`）返回 404。
- 路由 `@app.get("/preview/{filename:path}")` 必须声明在泛匹配 `/{filename}` **之前**；`/preview-content/{filename:path}` 紧随其后。
- **Office → HTML（不依赖 LibreOffice / Office 套件）**：`office_to_html()` 按后缀分发到纯 Python 解析器，产出 HTML 片段（表格首行作表头、最多 `OFFICE_MAX_ROWS=3000` 行/表、整数浮点去掉 `.0`）；**Excel（`xlsx/xlsm/xls`）逐 sheet 渲染**——`_sheet_sections()` 给每张表生成 `<h3 id="sheet-N">` 锚点区块，多表时顶部加 sticky `sheet-nav` 切换条，确保每个 sheet 页都能单独查看；`office_html_cached()` 按 `mtime+size` 做内存缓存（失败结果也缓存，避免反复触发），转换经 `asyncio.to_thread` 下放线程池。仅 `OFFICE_CONVERTIBLE_EXTS` 且 ≤ `OFFICE_MAX_BYTES=25MB` 才转换，否则退化为占位卡片。**思路参考 `qq939/interview-jobfinder`（服务端转 HTML + 前端 iframe 渲染，PDF 走浏览器原生查看器）**。

```bash
# 预览页（返回 HTML，右上角带下载按钮）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/preview/report.csv   # 200
# 页面内下载按钮目标（attachment）
curl -sI http://127.0.0.1:8082/report.csv | grep -i content-disposition            # attachment; filename*=UTF-8''report.csv
# Office 预览：转换后的 HTML 片段页（iframe 内嵌用）
curl -s http://127.0.0.1:8082/preview-content/report.xlsx | head                 # text/html，含 <table>…
# 依赖（纯 Python，无需 Office 套件）
pip3 install --break-system-packages mammoth openpyxl xlrd python-pptx
```

### 3.3 文件夹上传（暂存 → 每个文件夹打包成一个 zip）

首页上传区新增「**上传文件夹（可多选）**」按钮，并支持**拖拽多个文件夹**与页面内 **Ctrl+V / Cmd+V 粘贴文件夹**。每个文件夹会先逐文件上传到服务端暂存区，
全部完成后由后台**分别打包成一个 zip**，成功后删除该暂存源目录——首页最终**只出现这些 zip**，不残留一堆散文件。

**前端（`src/obs/server.py` 内联 JS）**

- **多选目录**：站点 `https://ocs.dimond.top` 属安全上下文，可用 `showDirectoryPicker({mode:'read'})`；但 **Web 标准不支持目录多选**
  （`showDirectoryPicker` 没有 `multiple` 选项，一次只返回一个目录，`<input webkitdirectory multiple>` 同样只选一个目录）。因此提供三种多选方式：
  1. **选父文件夹 → 勾选多个子文件夹**：弹窗自动列出该文件夹下的直接子文件夹（名称/文件数/大小/空目录），勾选后**每个子文件夹分别打包一个 zip**；
     根目录下的散文件在拆分时直接上传；不勾选则整个文件夹打包成一个 zip；
  2. 点醒目的「**＋ 继续添加文件夹（已选 N 个）**」从其他位置逐个追加；
  3. 直接把**多个文件夹一次拖到页面**（`DataTransfer` 可同时携带多个目录），粘贴同理。
  非安全上下文（如 `http://<局域网IP>`）回退隐藏 input；弹窗顶部会按当前环境给出对应提示。
- **选择目录回退**：隐藏 input 选中后用 `webkitRelativePath` 作为相对路径，按首段自动分组；`FileSystemDirectoryHandle` 遍历同样按顶层目录分组。
- **拖拽目录**：`drop` 事件用 `dataTransfer.items[i].webkitGetAsEntry()` 拿到多个目录 Entry，递归 `readEntries()`（每次只返回一批，需循环读到空）遍历所有层级；
  拿不到 Entry API 时回退为扁平 `dataTransfer.files`。
- **粘贴目录**：`paste` 事件同样优先用 `clipboardData.items[i].webkitGetAsEntry()` 还原目录结构；浏览器只给文件列表、结构丢失时，
  确认弹窗会**如实提示**「粘贴只拿到 X 个文件、目录结构已丢失，是否继续扁平上传？」。纯文本粘贴保持浏览器默认行为。
- **上传**：按顶层文件夹分组，逐个创建会话并复用现有单文件直传（≤10MB，`PUT ...` + `X-File-SHA256`）与分片上传（>10MB，`/upload/init` + `/upload/chunk` + `/upload/complete`，带 `folder_id` + `relpath`）；
  进度与失败信息显示 `[i/N] 文件夹名` 与 **relpath**。顶层散文件直接上传，不参与打包。
- **确认弹窗**：展示已选文件夹**逐项列表**（名称、文件数、大小、空目录数，可点 `×` 单独移除）与**子文件夹勾选列表**（勾选项分别打包），
  以及文件总数/总大小（拆分模式下按勾选项统计）；「继续添加文件夹」高亮并显示已选数量，确认按钮显示「上传并打包（N 个文件夹 / N 个 zip）」；
  重复选择同一文件夹自动去重；扁平回退时额外显示结构丢失警告。
- **续传**：`localStorage` 以 `ocs-folder:{文件夹名}:{文件数}:{总字节}:{首个文件相对路径}` 记住每个文件夹的 `folder_id`，刷新/断网后重新选择同一文件夹会命中服务端会话继续传。

**后端（暂存区 `obs/.folder-staging/{folder_id}/`）**

| 环节 | 说明 |
|------|------|
| 建会话 | `POST /api/folder/init` 生成 `folder_id`（uuid hex），写 `obs/.folder-staging/<id>/manifest.json`（`files{relpath,size,sha256,status}`、`empty_dirs`） |
| 小文件 | `PUT /api/folder/{id}/file/{relpath:path}`：流式写暂存 `data/<relpath>`；声明了 `X-File-SHA256` 则整段校验，不一致删除并 400 |
| 大文件 | 复用 `/upload/init`、`/upload/chunk`、`/upload/complete`，请求体带 `folder_id` + `relpath`；init 秒传按**暂存路径**判定，complete 把合并结果移入暂存并标记 manifest `done` |
| 路径安全 | `_safe_relpath()` 拒绝绝对路径、盘符、`../`、NUL 字节；`_resolve_staging_target()` 用 `realpath` 保证落在该会话 `data/` 内 |
| 打包 | `POST /api/folder/{id}/zip` 后台任务：`zipfile` + `ZIP_DEFLATED` 写 `.packing.zip.tmp` → 校验（可打开、条目数与 manifest 完成数一致、`testzip()` 无损坏、大小 > 0）→ 原子 `os.replace` 到 `obs/`；**空目录写成显式目录条目**；同名 zip 自动加时间戳后缀，不静默覆盖 |
| 成功/失败 | 成功 `shutil.rmtree` 暂存目录，首页只剩 zip；失败保留暂存与源文件，可直接重试 |
| 配额 | 暂存默认上限 **200GB**（`FOLDER_STAGING_LIMIT` 可覆盖）；统计优先读 manifest 声明的 `total_bytes`（避免每次 init 遍历数 GB 暂存目录），manifest 缺失才回退实际遍历；超限按「**最久未活动优先**」清理旧会话，最近 `FOLDER_ACTIVE_GRACE_SECONDS=600s` 内活跃或正在打包的会话**绝不删除**；仅剩活跃会话仍不足时对新会话返回 **507** |
| 进度 | `GET /api/folder/{id}/manifest` 返回 `total_files/done_files/partial_files/uploaded_bytes`；`GET /api/folder/{id}/zip/status` 返回打包任务状态 |

```bash
# 1) 建会话
curl -s -X POST http://127.0.0.1:8082/api/folder/init -H 'Content-Type: application/json' \
  -d '{"folder_name":"demo","total_files":2,"total_bytes":6,"empty_dirs":["empty"]}'
# 2) 小文件直传（relpath 保留目录层级）
curl -X PUT --data-binary 'hello' http://127.0.0.1:8082/api/folder/<folder_id>/file/sub/a.txt
# 3) 打包 + 轮询
curl -s -X POST http://127.0.0.1:8082/api/folder/<folder_id>/zip -H 'Content-Type: application/json' -d '{"zip_name":"demo.zip"}'
curl -s http://127.0.0.1:8082/api/folder/<folder_id>/zip/status
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

- **存储**：上传文件 → `obs/`；分片临时数据 → `obs/.chunks/`；文件夹上传暂存 → `obs/.folder-staging/<folder_id>/`（成功打包后自动删除）；HLS 产物 → `obs_shards/`。
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
| 2026-09-27 09:08:11 | R6 | **域名与下载策略**：域名 `https://ocs.dimond.top`；网页文字改用域名；下载先试 `http://localhost:19082`、失败回退 `https://ocs.dimond.top` |
| 2026-09-27 09:13~09:51 | R7 | 迭代细化域名/回退（上传下载统一先 `localhost:19082` 再域名；`obsBase()/obsFetch()`；新增 `CORSMiddleware`）；最后删除首页两行冗余说明文字 |
| 2026-09-27 10:06 | R8 | **rebase 到新远端**（未 push）：并入上游粘贴上传/实时速度/HLS 04:00 定时/`/video` 删除按钮 |
| 2026-09-27 19:29 | R9 | **再次 rebase**（未 push）：解决 `tools/`（宿主机只读 9p 挂载）被跟踪导致的卡壳——`reset --soft` 压提交、`git rm --cached tools`、写入 `.gitignore` 的 `/tools/` |
| 2026-10-01 16:50 | R10 | **rebase + 上传失败排查**：rebase 至 `38ebafa`（未 push）；定位 `/video` 上传器协议不符（camelCase/缺 `X-Chunk-SHA256`/无 complete body/`currentAbort` 空值/2MB≠10MB），重写修复（`614e537`）；首页分片循环补网络异常重试（`0196f0f`） |
| 2026-10-02 07:35 | R11 | **下载点击无反应排查**：`downloadWithFallback` 先 `preventDefault` 再异步 `a.click()`，丢了用户手势被弹窗拦截；改为加载期改写 `a[data-name]` href（`applyDownloadHosts`，`adc4192`） |
| 2026-10-13 02:11 | R12 | **文件预览页 + 推送分支 `ocs`**：可预览后缀点击先进 `/preview/...`（右上角醒目下载按钮），图片/音视频/PDF/HTML/文本(CSV 表格)/Office 占位；`git push` 到新分支 `ocs` |
| 2026-10-13 05:06 | R13 | **Office 在线预览（纯 Python 方案）**：先按要求**回退**掉 LibreOffice 转 PDF 方案（卸载 LibreOffice），改为参考 `qq939/interview-jobfinder` 的思路——服务端用纯 Python 库把 `docx/xlsx/xls/pptx` 转成 HTML（mammoth/openpyxl/xlrd/python-pptx），预览页 iframe 内嵌 `/preview-content/...` |
| 2026-10-13 07:28 | R14 | **Excel 逐 sheet 预览**：`_sheet_sections()` 为每张 sheet 生成 `<h3 id="sheet-N">` 锚点区块，多表时顶部加 sticky `sheet-nav` 切换条；xlsx/xlsm(openpyxl) 与 xls(xlrd) 均支持（`8eb6363`） |
| 2026-10-27 11:40 | R15 | **首页上传区文案顺序 + 统一字号**：把「文件托管上传：`curl --upload-file ...`」提示移到「拖拽文件到此处上传，或在页面按 Ctrl+V / Cmd+V 粘贴上传」**下方**，两者同置于上传框 `#uploadZone` 内，且**字号统一为 `0.8em`（小号）** |
| 2026-10-27 19:00 | R16 | **文件夹上传（网盘式）**：选择/拖拽/粘贴文件夹递归上传，服务端暂存 + manifest + 断点续传，完成后后台打包成**一个 zip**、删除源目录，首页只留 zip；200GB 暂存配额按最久未活动优先清理；新增 `/api/folder/*` 端点（见 §3.3） |
| 2026-10-28 15:15 | R17 | **文件夹多选上传**：站点 `https://ocs.dimond.top`（安全上下文）用 `showDirectoryPicker({mode:'read'})`；但 **Web 标准不支持目录多选**（`multiple` 仅属于文件选择器），弹窗「＋ 继续添加文件夹（已选 N 个）」累积、逐项列出已选文件夹（可 `×` 移除），拖拽/粘贴多个文件夹按顶层分组，每个文件夹**分别**打包一个 zip；局域网 http 回退 webkitdirectory；配额统计改用 manifest 声明大小，修复暂存较大时 init 卡顿 |
| 2026-10-28 18:00 | R18 | **域名更正为 HTTPS + 子文件夹拆分多选**：确认站点为 `https://ocs.dimond.top`（`http` 301→`https`），`url_head`/回退地址/文案/测试统一为 https（https 页面请求 http 域名会被混合内容拦截）；新增「选父目录 → 勾选多个子文件夹 → 分别打包」多选方式（`subfolderGroups/toggleSplitFolder/applyFolderSplit` + `FILE_REL_OVERRIDES`），未勾选子目录不上传、根目录散文件直传；前端 Node vm 用例 44 项全过 |

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

**最后 3 轮对话总结（R16 / R17 / R18）：**

- **R16（文件夹上传 → 单个 zip，2026-10-27）**：新增网盘式文件夹上传——目录选择 `webkitdirectory`、拖拽/粘贴经 `webkitGetAsEntry()` 递归还原层级；
  服务端 `obs/.folder-staging/<folder_id>/` 暂存 + `manifest.json` 记录进度（复用现有直传/分片上传，仅额外带 `folder_id`+`relpath`），全部完成后后台
  `zipfile` 打包成一个 zip 并校验、原子落盘 `obs/`，成功即删除暂存源目录，首页只留 zip；支持刷新/断网续传与 200GB 暂存配额清理（活跃会话不删）。
  自测：后端端到端 40 项 + 配额/打包失败单测 13 项全过（含 3 级嵌套、中文路径、空目录、12MB 分片、md5 抽查、续传与秒传、路径穿越拒绝）。
- **R17（文件夹多选上传，2026-10-28）**：每个顶层文件夹独立会话、**分别打包一个 zip**。站点域名为 `https://ocs.dimond.top`（安全上下文），
  用 `showDirectoryPicker({mode:'read'})` 读取目录（`FileSystemDirectoryHandle.values()` 递归，空目录记录）；因 **Web 标准不支持目录多选**，
  弹窗内逐项列出已选文件夹（可 `×` 移除）并提供醒目的「＋ 继续添加文件夹（已选 N 个）」累积；真正的多选可一次拖入多个文件夹或粘贴；
  非安全上下文（局域网 http）回退 `webkitdirectory`。拖拽/粘贴多个目录按顶层分组，扁平回退如实提示结构丢失；重复选择按
  `folderName|文件数|总字节|首个 relpath` 去重；逐个 group 独立走 `init → 上传 → zip → 轮询`，全部结束统一刷新一次。
  同时：域名/回退地址统一改为 https（https 页面请求 http 域名会被混合内容拦截）；配额统计改读 manifest 声明的 `total_bytes`
  （后补 `.packing.zip.tmp`），不再每次 `init` 遍历数 GB 暂存——实测 `init` 从 ~20s 降到 **0.032s**。
  自测：多文件夹后端 27 项 + 完整端到端 40 项 + 配额/打包 13 项 + 前端分组/追加/移除/弹窗 Node vm 单测 26 项，全部通过（含并发两会话、同名 zip 自动加时间戳且不覆盖原 zip）。
- **R18（域名更正 HTTPS + 子文件夹拆分多选，2026-10-28）**：确认站点为 `https://ocs.dimond.top`（`http` 301→`https`，属安全上下文），`url_head`/回退地址/页面提示/测试统一为 https，修掉「https 页面请求 http 域名被混合内容拦截」的隐患；新增第三种多选方式——只选一个父文件夹，在弹窗勾选其多个**直接子文件夹**，`applyFolderSplit()` 把每个勾选项转成独立上传组，`FILE_REL_OVERRIDES`（WeakMap）把 `Root/sub/x` 改写为 `sub/x` 且不改动原分组；未勾选子目录不上传，根目录散文件直接上传。自测：前端 Node vm 单测扩到 44 项（含拆分可见性/候选/统计/标签/前缀剥离/散文件/空目录）全过。

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
| Office 预览显示「暂不支持在线预览」占位 | 后缀不在可转换集（仅 `docx xlsx xlsm xls pptx`），或文件 > 25MB，或转换库抛错 | 属预期降级；确认后缀/体积。注意 `doc/ppt/odt/ods/odp/rtf` **无纯 Python 解析器**，一律退化为下载占位（不再依赖 LibreOffice） |
| Office 预览报错 / `/preview-content` 404·415 | 缺少转换库，或转换结果为空 | 装依赖：`pip3 install --break-system-packages mammoth openpyxl xlrd python-pptx`；`415` 表示该文件解析不出内容（如损坏文档） |
| 文件夹上传返回 **507** | 暂存区超过 `FOLDER_STAGING_LIMIT`（默认 200GB），且剩余会话都处于活跃/打包中（受保护不删） | 等待当前上传/打包完成后重试；或调大 `FOLDER_STAGING_LIMIT`；`obs/.folder-staging/` 下确认遗留会话 |
| 文件夹打包返回 **409** | `done_files < total_files`，仍有文件未传完 | 查 `GET /api/folder/{id}/manifest`，让前端重传缺失文件（直传/分片均可续） |
| `/api/folder/{id}/file/...` 返回 **400** | relpath 含 `..`、绝对路径、盘符或 NUL 字节 | 属路径穿越防护；前端应传相对路径（`webkitRelativePath`/`getAsEntry()` 递归结果） |
| 打包失败（`status=failed`） | zip 校验不通过（条目数与 manifest 不符、损坏、空产物）或磁盘空间不足 | 暂存与源文件均保留，修复缺失文件后重新 `POST /api/folder/{id}/zip` 重试 |
| 刷新后文件夹上传从头开始 | localStorage 的 `ocs-folder:*` 键或服务端会话已丢失/被配额清理 | 重新选择同一文件夹会命中 `resume_folder_id`；若会话已清理则只能重传 |
| 无法运行 `test_*.py` | 容器未安装 pytest（且无外网） | 有网环境 `pip install -r requirements.txt` 后 `pytest -q` |

---

## 8. 规范参考

- `systemreadme.md`：容器内规范（`run_claude.js` / `/ask/claude` + `/health` / 19081 Hub 注册 / Git）
- `SKILL.md`：本项目的开发与运维技能（启动、日志、Git、接口、变更流程）
- `user_history.md`：obs 与 obs-video 二合一的需求与特性记录
- `tools/obs-video/README.md`、`tools/obs-video/SKILL.md`：视频流应用的详细功能文档
