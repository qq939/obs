================================================================================
              Harness-Claw 容器内规范（精简版）  ·  读者：容器内 Agent
================================================================================

先看这 6 条硬要求（最常被违反；违反会导致卡片之间无法协作、面板功能失效）：

1) Git：项目根目录必须是 git 仓库。没有就 `git init` + 首次提交；**每次对话后必须提交**。
   容器里默认没有 git 身份，先设本地身份（`--local`，别用 `--global`），否则 commit 直接失败。
   细节见 skill `harness-git`。面板版本下拉框显示「非Git项目」时，点它就会给你下这条指令。
2) 调 Claude 一律走 `run_claude.js`，不要直接调 claude CLI（否则问答不进 logs/agent_tui.log）。
3) 每个容器都要有 `server.js` 提供 `/ask/claude` + `/health`（内部端口 8082）：
   其他容器卡片就是靠这个接口跟你对话的。
4) 想被别的卡片发现/调用，必须注册到 19081 Hub（面板点「注册」会给你下指令，按规范自报接口）。
5) 一次做完并落盘：代码 + README/SKILL + logs 记录；不要中途等确认、不要只给计划。
6) 每张卡片有自己的注入文件 `project/claudeagent.md`（写**本卡片自己的核心目标**，见第五节）：
   内容由用户在面板卡片上点「任务」按钮维护；它会自动注入你的每次调用（`/ask/claude` 同样生效）。

所有 skill 在 `/agent-config/skills/`（索引见文末）。

================================================================================
一、run_claude.js — 唯一的 Claude 调用入口
================================================================================

所有 Claude 调用都走它，问答才会统一记录到 `logs/agent_tui.log`。

```javascript
const { spawn } = require('child_process');
const WORKSPACE_DIR = '/home/agent/.claude/workspace/project';

const child = spawn('node', [WORKSPACE_DIR + '/run_claude.js'], {
  cwd: WORKSPACE_DIR,
  stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env,
    ANTHROPIC_DISABLE_PREFLIGHT: '1',                 // 必填：跳过预检
    CLAUDE_CAPTURE_STDIO: '1',                        // 必填：捕获输出
    CLAUDE_MSG: Buffer.from(msg).toString('base64'),  // 必填：base64 消息
    // CLAUDE_IMG: '1',                               // 可选：图文模式
  },
});
```

| 变量 | 必须 | 说明 |
|------|------|------|
| ANTHROPIC_DISABLE_PREFLIGHT | 是 | 设为 '1' 跳过启动预检 |
| CLAUDE_CAPTURE_STDIO | 是 | 设为 '1' 捕获 Claude 输出 |
| CLAUDE_MSG | 是 | base64 编码的完整消息 |
| CLAUDE_IMG | 否 | 设为 '1' 触发图文模式：把图片写到 project/tmp.png |

================================================================================
二、/ask/claude — 每个容器必备的 HTTP 服务（8082）
================================================================================

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/ask/claude?q=...` | 向 Claude 提问，返回纯文本 |
| GET | `/health` | 健康检查 |

编码识别：`q` 含空格或长度 < 50 → URL 解码；否则 → base64 解码。
成功返回 200 纯文本；失败返回 500 + 错误信息。

> `/ask/claude` 内部走 `run_claude.js`，所以它**自动带上本卡片的 claudeagent.md**（见第五节）：
> 别的卡片调你时，Claude 已经知道你的核心目标是什么。

```bash
curl "http://localhost:8082/ask/claude?q=你好，请介绍一下自己"
curl "http://localhost:8082/ask/claude?q=$(echo '复杂内容' | base64)"
```

================================================================================
三、注册到 19081 Hub（让别的卡片找得到你）
================================================================================

Hub 首页 http://dimond.top:19081 是平台工具知识库：pills 切换工具、iframe 预览工具 Web UI、
Docs 查看器看文档，内置 Hub 公共接口文档与示例（obs）范本。

**面板「注册」按钮 = 下达指令**：点一下，control 会给你一条指令 —— 读 skill `harness-tools-hub`
→ 摸清本项目**真实**对外接口（功能性 API + 必带的 `/ask/claude`）→ **自己**提交到 Hub：

```
POST http://host.docker.internal:19081/api/tools
```

- `name` 用容器名派生（`19083-email` → `email`），面板据此显示「已注册」；同名重复注册 = 覆盖（更新）。
- `doc_md` 必须写清：功能概览、API 端点表（方法/路径/用途）、curl 调用示例
  （地址统一 `http://dimond.top:<你的宿主机端口>`），并包含 `/ask/claude`。
- 再点一次「注册」= 更新自己的注册信息；Alt+点击 = 注销。
- 也可以直接 POST：完整记录（`name`/`doc_md`/`port`）或简化记录（`container_name`/`host_port`/`agent_type`）。

查询/注销：`GET|DELETE http://host.docker.internal:19081/api/tools[/<name>]`
卡片之间互相调用统一走 `http://dimond.top:19xxx`。

================================================================================
四、Git 规范（最常被忽略，必须做）
================================================================================

```bash
cd /home/agent/.claude/workspace/project
git config user.name "harness-agent"        # 容器默认没有身份，不设会 commit 失败
git config user.email "agent@harness.local"
[ -d .git ] || { git init; git add -A; git commit -m "Initial commit"; }

# 每次对话结束：提交本次变更，并把 commit 记进 logs/commit.txt
git add -A && git commit -m "描述本次变更"
echo "$(git rev-parse --short HEAD) 描述本次变更" >> logs/commit.txt
```

`.gitignore` 至少包含：`logs/  node_modules/  .DS_Store  __pycache__/  *.log  .env  uploads/  dist/  build/`
（完整格式与示例见 skill `harness-git`）

远程仓库（GitHub）名字**必须以 `harness` 开头**，形如 `harness-<项目>`；新建仓库时直接这样命名。
已有远程不合规：先在 GitHub 改名（旧地址会自动重定向），再 `git remote set-url origin <新地址>`。
自检：

```bash
git remote get-url origin | sed -E 's#.*/##; s#\.git$##' | grep -q '^harness' \
  || echo "远程仓库名不合规：必须以 harness 开头"
```

================================================================================
五、每张卡片自己的注入：project/claudeagent.md
================================================================================

两个注入文件分工别搞混：

| 文件 | 作用范围 | 谁维护 | 内容 |
|------|---------|--------|------|
| `CLAUDE.md`（同目录） | **平台级**：所有卡片共享 | 平台 | 平台通用约定（不许 plan mode、必须 git 等） |
| **`claudeagent.md`** | **本卡片自己** | **用户**（面板「任务」按钮） | **本卡片的核心目标**、职责边界、对外接口 |

机制：`run_claude.js` 每次调用都会把 `claudeagent.md` 注入到消息最前面（超过 4000 字符截断），
所以面板「发送」、容器 `/ask/claude`、面板下发的注册/git init 指令，Claude 都带着这份目标开工。

要求：
- **默认是空文件**，不注入；内容由用户在面板卡片上点「任务」按钮实时编辑保存（也可用 API）。
- 只写**本卡片自己**的事，用一两句话讲清核心目标，别抄平台规范（那是 CLAUDE.md 的活）。
- 目标变了、对外接口变了，就更新它。
- 面板读取/写入：卡片上的「任务」按钮，或
  `GET|PUT http://host.docker.internal:19080/api/agents/<容器名>/claudeagent`
- 你可以**读**这个文件来确认自己的目标（`project/claudeagent.md`），但不要擅自改写用户写的内容。

================================================================================
六、给卡片发图片（面板 → 容器）
================================================================================

面板上每张卡片都支持发图：**粘贴（Ctrl+V，会弹框确认是否上传剪切板图片）/ 把图片拖进卡片 / 点「图片」选择文件**。

路线（一层层落到哪）：

```
浏览器（粘贴/拖拽/选择文件）→ dataURL
  → POST http://<control>:19080/api/agents/<容器名>/image     # control 解码 base64
  → 宿主机 workspaces/<容器名>/tmp.png
    = 容器内 /home/agent/.claude/workspace/project/tmp.png     # chown 501:20，agent 可读
  → 点「发送」：control 以 CLAUDE_IMG=1 + CLAUDE_MSG=... 调 run_claude.js
  → run_claude.js 把 ![image](file:///home/agent/.claude/workspace/project/tmp.png) 追加到消息
  → claude CLI 读该文件（图文模式）
```

要点：

- 图片固定落在 `tmp.png`（run_claude.js 的默认路径）；发送一次即视为已消费，面板会清掉待发送状态。
- 「移除图片」按钮 = `DELETE /api/agents/<容器名>/image`（删 tmp.png）。
- 你也可以直接读 `project/tmp.png` 处理用户刚发的图；`tmp.png` 别提交进 git（写进 .gitignore）。

================================================================================
规范索引（按需查阅 /agent-config/skills/）
================================================================================

| Skill | 说明 |
|------|------|
| harness-git | **git init / 每次对话后提交 / logs/commit.txt / .gitignore** |
| harness-tools-hub | **注册到 19081 Hub：接口自报 + 持久化位置** |
| harness-paths | 工作目录、日志目录、启动脚本、配置挂载路径 |
| harness-logging | start.log / agent_tui.log / run.log / ollama.log |
| harness-ports | 8082 内部端口、19081-19999 宿主机端口规范 |
| harness-workflow | 推荐工作流：开发 → 调试 → 更新 README → 总结会话 |
| harness-config | 容器启动时的配置注入流程 |
| harness-agent-types | claude / ollama / openclaw 路径差异 |
| harness-user | agent (uid=501) 用户与 sudo 权限 |
| harness-init | 新会话收到的初始指令 |
| harness-env | CLAUDE_CODE_* 环境变量与 API 配置 |
| harness-supabase | Supabase 安装、连接池地址与客户端示例 |

首次启动至少看：harness-git、harness-paths、harness-ports、harness-workflow。
