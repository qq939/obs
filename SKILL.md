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
| 域名 | **`https://ocs.dimond.top`**（= `http://dimond.top:19082` = 宿主机 `http://localhost:19082` = 容器内 `http://localhost:8082`） |
| 端口 | `8082`（容器内部） |
| 主程序 | `src/obs/server.py`（FastAPI） |
| 默认页 `/` | **文件托管页面**（OBS 上传/下载/列表/公告板）；上传框 `#uploadZone` 内文案自上而下：「拖拽文件/文件夹到此处上传，或在页面按 Ctrl+V / Cmd+V 粘贴上传」→ `文件托管上传： curl --upload-file ...` → 「选择文件」「上传文件夹」按钮；**前两行均为小号字 `font-size: 0.8em`**（style 完全一致） |
| `/video` | **视频流播放页面**（竖屏翻页，HLS/直连） |
| 上传/下载策略 | 先试直连 `http://localhost:19082`（加速），失败回退域名 `https://ocs.dimond.top`：下载用 `applyDownloadHosts()`（加载期改写 `a[data-name]` 的 `href`，点击走原生跳转）；上传/接口用 `obsBase()`/`obsFetch()`（页面加载预热探测）；跨域由 `CORSMiddleware` 放行。网页只展示域名 |
| 文件预览 | 可预览后缀点击进 `/preview/{filename:path}`（右上角醒目「⬇ 下载」）：图片/音视频/PDF/HTML/文本(CSV 表格)/**Office（纯 Python 转 HTML 后 iframe 内嵌）**；**`txt`/`md` 还有「✎ 编辑」**，编辑后 `PUT /api/text/{filename:path}` 保存（UTF-8、≤5MB、原子替换）；内容用同源 `/obs/...`，下载用同源 `/{filename}`；`safe_upload_path()` 防穿越；路由须在 `/{filename}` 之前 |
| Office 预览 | **不依赖 LibreOffice/Office 套件**：`docx→mammoth`、`xlsx/xlsm→openpyxl`、`xls→xlrd`、`pptx→python-pptx`，转 HTML 经 `/preview-content/{filename:path}` 供 `<iframe>` 内嵌；仅这 5 种后缀且 ≤25MB 可转，其余退化占位（思路参考 `qq939/interview-jobfinder`） |
| 文件夹上传 | **网盘式、支持多选**：每个顶层文件夹**独立一个会话、分别打包一个 zip**。站点 `https://ocs.dimond.top` 为安全上下文，优先用 `showDirectoryPicker({mode:'read'})`；但 **Web 标准不支持目录多选**（`multiple` 只属于文件选择器），一次只能选一个目录，故弹窗内「＋ 继续添加文件夹」累积、或直接把多个文件夹拖入页面；选单个父目录时还可在弹窗勾选其多个**子文件夹**分别打包。非安全上下文（局域网 http）回退 `webkitdirectory`。逐文件暂存 `obs/.folder-staging/<folder_id>/data/` → 全部传完后后台打包 zip → 删除暂存、首页只留 zip；小文件走 `PUT /api/folder/{id}/file/{relpath}`，>10MB 复用 `/upload/*` 并带 `folder_id`+`relpath`；支持续传与 200GB 配额清理（配额大小读 manifest 声明值，避免遍历大目录） |
| ffmpeg | 内置 `bin/ffmpeg`、`bin/ffprobe`（`user_start.sh` 加入 PATH；缺失用 `scripts/install_ffmpeg.sh` 获取） |
| 启动脚本 | `user_start.sh`（被 `start.sh` 调用） |
| 启动日志 | `logs/start.log`；运行日志 `logs/server.log`；问答日志 `logs/agent_tui.log` |
| 存储目录 | `obs/`（文件 + `.chunks/` 分片 + `.folder-staging/` 文件夹暂存）、`obs_shards/`（HLS 产物） |
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
POST /upload/complete/{id}                合并完成（文件夹上传时 body 带 folder_id+relpath）
POST /api/folder/init                     文件夹会话 -> {folder_id}（可带 resume_folder_id 续传）
GET  /api/folder/{id}/manifest            文件夹上传进度（done/partial/bytes/empty_dirs）
PUT  /api/folder/{id}/file/{relpath:path} 文件夹内单文件直传（≤10MB，可选 X-File-SHA256）
POST /api/folder/{id}/zip                 后台打包成一个 zip（{zip_name?}）
GET  /api/folder/{id}/zip/status          打包状态（queued/running/done/failed）
POST /                                    表单上传
PUT  /{filename}                          简单上传（流式）
GET  /obs/{filename}                      访问/播放 obs 下文件（内联；预览页内嵌用）
GET  /preview/{filename:path}             文件预览页（图片/PDF/HTML/音视频/文本(CSV)/Office），右上角下载按钮
GET  /preview-content/{filename:path}     Office（docx/xlsx/xls/pptx）转换后的 HTML（text/html），供预览页 iframe 内嵌
PUT  /api/text/{filename:path}           在线编辑保存 txt/md（body=UTF-8；≤5MB；临时文件+os.replace 原子替换）
GET  /{filename}                          下载（Range 206；Content-Disposition: attachment）
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
并按本项目追加 `/obs/ /obs_shards/ sessions/ /tools/`（`/tools/` 为宿主机只读 9p 挂载、上游不含，纳入版本控制会导致 rebase 冲突，故整体忽略）。

## 变更流程（推荐）

1. 改 `src/obs/server.py` / `src/obs/video_static/*` → `python3 -m py_compile src/obs/server.py`。
2. `kill "$(pgrep -f '[s]rc/obs/server.py')"; ./user_start.sh`，看 `logs/start.log` 是否就绪。
3. 功能自测：`/health` → `/`（文件页）→ `/video` → `/videos` → `PUT /x` → `GET /obs/x` → `DELETE /x`；
   预览：`GET /preview/<可预览文件>` 返回 200 且含 `class="btn-download"`，`GET /preview/..%2F..%2Fetc%2Fpasswd` 返回 404；
   Office：`GET /preview-content/<docx|xlsx|xls|pptx>` 返回 `text/html`（含 `<table>`/正文），不支持的 office 后缀返回 404。
   txt/md 编辑：`GET /preview/<x.txt|md>` 含 `#editBtn`/`#editorWrap` → `PUT /api/text/<x.txt>` 返回 `{"ok":true}` → `GET /obs/<x.txt>` 内容一致；
   非 txt/md 返回 415、文件不存在/越界 404、非 UTF-8 400。
   文件夹：`POST /api/folder/init` → `PUT /api/folder/{id}/file/...`（小文件）/`/upload/*`（>10MB 带 `folder_id`+`relpath`）→ `GET /manifest` 计数一致 →
   `POST /zip` → 轮询 `/zip/status` 到 `done` → 解压核对层级/文件数/空目录/md5，并确认 `obs/.folder-staging/{id}` 已删除、首页只剩 zip。
   多选：同时 `init` 两个会话并行上传/打包，确认生成**两个互不覆盖**的 zip（同名时自动加时间戳后缀）、各自暂存目录都被删除。
4. 跑回归：`python3 -m pytest test_server.py test_resumable.py src/obs/test_*.py -q`（需 ffmpeg 的用例可能跳过）。
5. 更新 `README.md` / `SKILL.md`；`git add -A && git commit`，记录 `logs/commit.txt`。

## 扩展指引

- **新增接口**：在 `src/obs/server.py` 用 `@app.get/post/put/delete` 定义；**注意路由顺序**——
  泛匹配的 `/{filename}`（GET/PUT/DELETE）必须放在 `/video`、`/ask/claude` 等具体路由之后。
- **Claude 问答**：沿用 `ask_claude` → `asyncio.create_subprocess_exec('node', run_claude.js)` +
  `CLAUDE_MSG`(base64) + `CLAUDE_CAPTURE_STDIO=1`，勿绕开 `run_claude.js`。
- **视频前端**：改 `src/obs/video_static/`；由 `get_video_static_dir()` 定位，`/video` 路由直接读文件。
- **域名 / 上传下载回退**：站点域名 `https://ocs.dimond.top`；**页面文字只展示该域名**，链接前缀常量 `url_head = "https://ocs.dimond.top"`（`src/obs/server.py` 顶部），
  驱动首页 curl 示例、文件列表下载链接与上传响应 URL。**页面上传/下载请求为加速先试直连 `http://localhost:19082`，不可达回退域名 `https://ocs.dimond.top`**：
  下载/打开用 `applyDownloadHosts(base)`（**加载期**改写所有 `a[data-name]` 的 `href`，点击走原生跳转即直接下载；
  **切勿**改成在点击回调里 `preventDefault` + 异步探测 + `a.click()`——会丢用户手势被弹窗拦截）；上传/接口（PUT、`/upload/init|chunk|complete`、DELETE）用
  `obsBase()`（页面加载预热探测）/`obsFetch()`，跨域由 `CORSMiddleware` 放行（常量 `DOWNLOAD_HOST_PRIMARY` / `DOWNLOAD_HOST_FALLBACK`）。
- **文件预览**：可预览后缀集合 `PREVIEW_*_EXTS`（`src/obs/server.py`），`is_previewable()`/`preview_kind()` 分类；
  预览页内联内容用**同源** `/obs/{filename}`，下载按钮用**同源** `/{filename}`（不涉及主机回退）；
  新增路由 `/preview/{filename:path}` 必须放在泛匹配 `/{filename}` **之前**；对外文件名一律经 `safe_upload_path()` 校验在 `obs/` 内。
- **txt/md 在线编辑**：`EDITABLE_TEXT_EXTS={"txt","md"}`、`MAX_EDIT_TEXT_BYTES=5MB`；预览页由 `render_preview_page()` 决定是否输出 `#editorWrap`，
  前端 `#editBtn` 切换只读 `<pre>` 与 `<textarea>`，`Ctrl/Cmd+S`/「💾 保存」调 `PUT /api/text/{filename:path}`；
  后端**只允许已存在文件**，依次校验 `safe_upload_path()`（越界 404）→ 后缀（415）→ 旧文件/新内容体积（413）→ UTF-8（400），
  先写同目录 `*.edit.tmp` 再 `os.replace` 原子替换；保存后前端原地更新 `#previewText` 与 `#fileMeta`，有未保存修改离开页面会 `beforeunload` 确认。
  **新增可编辑后缀**：把后缀加入 `EDITABLE_TEXT_EXTS` 即可（同时应属于 `PREVIEW_TEXT_EXTS`），不要放宽后端校验顺序。
- **Office → HTML（纯 Python，勿引入 Office 套件）**：`office_to_html()` 按后缀分发到解析器产出 HTML 片段（表首行作表头、最多 `OFFICE_MAX_ROWS=3000` 行/表、整数浮点去 `.0`）；
  **Excel（`xlsx/xlsm/xls`）逐 sheet 渲染**：`_sheet_sections()` 给每张表生成 `<h3 id="sheet-N">` 锚点区块，多表时顶部加 sticky `sheet-nav` 切换条（单表不加），保证每个 sheet 页都能单独查看；
  `office_html_cached()` 按 `mtime+size` 内存缓存（失败也缓存），转换经 `asyncio.to_thread` 下放；仅 `OFFICE_CONVERTIBLE_EXTS`（`docx xlsx xlsm xls pptx`）且 ≤`OFFICE_MAX_BYTES=25MB` 才转换。
  `GET /preview-content/{filename:path}` 把片段包成独立 HTML 文档（`text/html`）供 `<iframe>` 内嵌；无解析器的 `doc/ppt/odt/ods/odp/rtf` 一律退化为下载占位。
  依赖：`pip3 install --break-system-packages mammoth openpyxl xlrd python-pptx`（**不要**再装 LibreOffice）。思路参考 `qq939/interview-jobfinder`（服务端转 HTML + 前端 iframe）。
- **文件夹上传（多选）**：前端按**顶层文件夹**分组——`groupsFromFileList()`（`webkitRelativePath` 首段分组）、`collectEntriesFromRoots()`（拖拽/粘贴 Entry 递归）、
  `collectGroupsFromDirectoryHandles()`（`showDirectoryPicker({mode:'read'})` 的 `FileSystemDirectoryHandle.values()` 异步遍历，空目录也记录）；
  **注意：Web 标准不支持目录多选**（`multiple` 仅属于 `showOpenFilePicker`），目录选择器一次只返回一个 handle；多选靠「＋ 继续添加文件夹」累积、拖拽多个目录，
  或**子文件夹拆分**：选中单个父目录时 `subfolderGroups()` 列出其一级子目录，`toggleSplitFolder()` 勾选后 `applyFolderSplit()` 把每个勾选子目录转成独立上传组
  （`FILE_REL_OVERRIDES` WeakMap 把 `Root/sub/x` 改写成 `sub/x`，不改动原分组；根目录散文件转 `pending.looseFiles` 直接上传，未勾选的子目录不上传）；
  待上传态为 `folderPending = {groups, looseFiles, splitGroups, splitSelected, structureLost}`，`addGroupsToPending()` 按 `folderName|文件数|总字节|首个 relpath` 去重；
  `renderFolderConfirm()` 逐项渲染 `#folderGroupList`（名称/文件数/大小/空目录，`×` 走 `removeFolderGroup()` 可单独移除）、
  `#folderPickHint` 说明当前页面多选方式、`#folderSplitBox`/`#folderSubList` 子文件夹勾选、`#folderAddMoreBtn` 高亮显示「＋ 继续添加文件夹（已选 N 个）」、
  `#folderConfirmBtn` 显示「N 个文件夹」或拆分时的「N 个 zip」。
  `uploadFolder()` 逐个 group 调 `uploadFolderGroup()`：各自 `POST /api/folder/init` → `uploadFiles(files, {folderId, noReload:true})` → `POST /zip` → 轮询 `/zip/status`，最后统一 reload 一次；
  `showDirectoryPicker` 需**安全上下文**（HTTPS / localhost）；`https://ocs.dimond.top` 满足条件，局域网 http 下自动回退 `webkitdirectory`（同步 `input.click()` 保用户手势）。
  `uploadFiles(files, {folderId, noReload})` 复用单文件直传/分片上传，路径取 `fileRelPath(file)`（`FILE_REL_OVERRIDES` 覆盖 → `_relPath` → `webkitRelativePath` → `name`）；
  目录遍历用 `entryFile`/`readAllEntries`/`walkEntry`（`readEntries()` 必须循环读到空数组）；后端暂存 `_staging_*` 系列函数 + `manifest.json`，`_safe_relpath()` 防穿越，
  `_staging_entry_size()` 优先读 manifest `total_bytes`（并按需加 `.packing.zip.tmp`），**不要让配额检查遍历整个暂存目录**（数 GB 会话会让 `init` 卡十几秒）；
  `_pack_folder_sync()` 用 `zipfile` 写 `.packing.zip.tmp` 并校验后 `os.replace` 到 `_unique_zip_path()`（同名加时间戳后缀），**成功才** `rmtree` 暂存；
  `_enforce_staging_quota()` 按最久未活动清理且保护活跃/打包中会话；新路由 `/api/folder/...` 为多段路径，仍须放在泛匹配 `/{filename}` 之前声明。
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
| 下载走错地址 | 跨网访问时直连 `http://localhost:19082` 不可达 | 页面 `applyDownloadHosts()` 加载时已把 href 改写为可达主机（不可达回退 `https://ocs.dimond.top`），无需手工改链接 |
| 点击下载「没反应」 | 在点击回调里 `preventDefault` 后异步探测再 `a.click()`，丢失用户手势被弹窗拦截 | 不要这么做；改为页面加载期改写 href（`applyDownloadHosts()`），点击走原生跳转 |
| 预览页 404 | 文件不在 `obs/`，或名含 `../` 被 `safe_upload_path()` 拦截 | 确认文件已上传到 `obs/`；跨目录名会被判越界（这是有意的防护） |
| Office 预览显示「暂不支持在线预览」 | 后缀非 `docx/xlsx/xlsm/xls/pptx`、文件 >25MB、或缺转换库导致转换失败 | 属预期降级（`doc/ppt/odt/ods/odp/rtf` 无纯 Python 解析器，一律占位）；装依赖 `pip3 install --break-system-packages mammoth openpyxl xlrd python-pptx` |
| `/preview-content` 返回 404/415 | 后缀不可转换（404）或解析不出内容（415） | 404 属预期；415 多为文件损坏或非真正 Office 格式 |
| Office 预览变慢 | 每次转换未命中缓存（`mtime`/`size` 变化） | 首次转换后会进内存缓存，重复访问走缓存；大表受 `OFFICE_MAX_ROWS=3000` 截断 |
| txt/md 没有「编辑」按钮 | 后缀不在 `EDITABLE_TEXT_EXTS`（仅 `txt`/`md`）或文件 >5MB | 属预期：加后缀改 `EDITABLE_TEXT_EXTS`；超大文件先下载编辑后再上传 |
| 编辑保存返回 415 / 413 / 400 / 404 | 非 txt/md / 内容或原文件超 5MB / 非 UTF-8 / 文件不存在或路径越界 | 按状态码修正；保存按 UTF-8 编码，含二进制请勿用在线编辑 |
| 编辑保存后内容回退 | 保存请求未成功（状态提示为红字）或并发覆盖 | 看页面「保存失败」提示与 `logs/server.log`；原子替换保证不会写半截文件 |
| `ModuleNotFoundError: No module named 'fastapi'` | 容器内缺少 Python 依赖 | `pip3 install --break-system-packages fastapi "uvicorn[standard]" aiofiles python-dotenv python-multipart` |
| 8082 不通 | 进程退出/端口占用 | 看 `logs/start.log`；`kill "$(pgrep -f '[s]rc/obs/server.py')"` 后 `./user_start.sh` |
| `/video` 404 | 缺 `src/obs/video_static/` | 补齐 `index.html/app.js/style.css/vendor/hls.min.js` |
| `/ask/claude` 500 | claude CLI / 环境未就绪 | 查 `logs/agent_tui.log`；确认 `ANTHROPIC_*` 与 `run_claude.js` |
| 页面错乱 | 路由顺序被 `/{filename}` 抢占 | 具体路由必须定义在泛匹配 `/{filename}` 之前 |
| 文件夹上传 507 | 暂存超 `FOLDER_STAGING_LIMIT`（默认 200GB），剩余会话均活跃/打包中 | 等上传/打包完成再试或调大上限；查 `obs/.folder-staging/` 遗留会话 |
| 文件夹打包 409 | `done_files < total_files` | 查 `/api/folder/{id}/manifest`，先补齐缺失文件再 `POST /zip` |
| 文件夹 relpath 400 | 含 `..`/绝对路径/盘符/NUL | 前端传 `webkitRelativePath` 或 `getAsEntry()` 递归得到的相对路径 |
| 文件夹打包 `failed` | zip 校验失败（条目数不符/损坏/空）或磁盘不足 | 暂存保留，修复后重新 `POST /zip`；日志见 `logs/server.log` |
| 文件夹刷新后重传 | `ocs-folder:*` localStorage 或服务端会话丢失/被清理 | 重新选同一文件夹走 `resume_folder_id`；会话被配额清理则只能重传 |
| 「上传文件夹」一次只能选一个目录 | **Web 标准不支持目录多选**：`showDirectoryPicker` 一次只返回一个目录，`webkitdirectory multiple` 同样只选一个（`multiple` 仅属于 `showOpenFilePicker`） | 属浏览器限制，非 bug：选完点高亮的「＋ 继续添加文件夹（已选 N 个）」累积，或直接拖拽多个文件夹到页面；也可只选父目录后在弹窗勾选多个子文件夹分别打包 |
| `/api/folder/init` 卡十几秒 | 暂存目录很大时旧实现在配额检查里遍历全部字节 | 已改为读 manifest `total_bytes`（`_staging_entry_size()`）；若仍慢，检查是否有 manifest 缺失的遗留会话 |
| 多选后 zip 同名被覆盖 | 旧实现直接写目标 zip | 已用 `_unique_zip_path()` 加时间戳后缀，原 zip 保留；请求名与最终名以 `/zip/status` 的 `zip_name` 为准 |

## 参考

- `README.md`：项目总览、目录结构、API、构建细节、最近对话总结
- `systemreadme.md`：容器内规范（run_claude.js / 8082 / Hub / Git）
- `tools/obs-video/README.md`、`tools/obs-video/SKILL.md`：上游视频应用完整文档
