================================================================================
              Hermit-Claw 容器内规范（精简版）  ·  读者：容器内 Agent
================================================================================

先看这 5 条硬要求（最常被违反；违反会导致卡片之间无法协作、面板功能失效）：

1) Git：项目根目录必须是 git 仓库。没有就 `git init` + 首次提交；**每次对话后必须提交**。
   容器里默认没有 git 身份，先设本地身份（`--local`，别用 `--global`），否则 commit 直接失败。
   细节见 skill `hermit-git`。面板版本下拉框显示「非Git项目」时，点它就会给你下这条指令。
2) 调 Claude 一律走 `run_claude.js`，不要直接调 claude CLI（否则问答不进 logs/agent_tui.log）。
3) 每个容器都要有 `server.js` 提供 `/ask/claude` + `/health`（内部端口 8082）：
   其他容器卡片就是靠这个接口跟你对话的。
4) 想被别的卡片发现/调用，必须注册到 19081 Hub（面板点「注册」会给你下指令，按规范自报接口）。
5) 一次做完并落盘：代码 + README/SKILL + logs 记录；不要中途等确认、不要只给计划。

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

```bash
curl "http://localhost:8082/ask/claude?q=你好，请介绍一下自己"
curl "http://localhost:8082/ask/claude?q=$(echo '复杂内容' | base64)"
```

================================================================================
三、注册到 19081 Hub（让别的卡片找得到你）
================================================================================

Hub 首页 http://dimond.top:19081 是平台工具知识库：pills 切换工具、iframe 预览工具 Web UI、
Docs 查看器看文档，内置 Hub 公共接口文档与示例（obs）范本。

**面板「注册」按钮 = 下达指令**：点一下，control 会给你一条指令 —— 读 skill `hermit-tools-hub`
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
git config user.name "hermit-agent"        # 容器默认没有身份，不设会 commit 失败
git config user.email "agent@hermit.local"
[ -d .git ] || { git init; git add -A; git commit -m "Initial commit"; }

# 每次对话结束：提交本次变更，并把 commit 记进 logs/commit.txt
git add -A && git commit -m "描述本次变更"
echo "$(git rev-parse --short HEAD) 描述本次变更" >> logs/commit.txt
```

`.gitignore` 至少包含：`logs/  node_modules/  .DS_Store  __pycache__/  *.log  .env  uploads/  dist/  build/`
（完整格式与示例见 skill `hermit-git`）

================================================================================
规范索引（按需查阅 /agent-config/skills/）
================================================================================

| Skill | 说明 |
|------|------|
| hermit-git | **git init / 每次对话后提交 / logs/commit.txt / .gitignore** |
| hermit-tools-hub | **注册到 19081 Hub：接口自报 + 持久化位置** |
| hermit-paths | 工作目录、日志目录、启动脚本、配置挂载路径 |
| hermit-logging | start.log / agent_tui.log / run.log / ollama.log |
| hermit-ports | 8082 内部端口、19081-19999 宿主机端口规范 |
| hermit-workflow | 推荐工作流：开发 → 调试 → 更新 README → 总结会话 |
| hermit-config | 容器启动时的配置注入流程 |
| hermit-agent-types | claude / ollama / openclaw 路径差异 |
| hermit-user | agent (uid=501) 用户与 sudo 权限 |
| hermit-init | 新会话收到的初始指令 |
| hermit-env | CLAUDE_CODE_* 环境变量与 API 配置 |
| hermit-supabase | Supabase 安装、连接池地址与客户端示例 |

首次启动至少看：hermit-git、hermit-paths、hermit-ports、hermit-workflow。
