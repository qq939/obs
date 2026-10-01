const { spawn, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const TIMEOUT_MS = 3600 * 1000;
const CLAUDE_MSG = process.env.CLAUDE_MSG;
const CLAUDE_IMG = process.env.CLAUDE_IMG;  // 可选，图片base64编码
const LOG_FILE = path.join(process.env.HOME || '/home/agent', '.claude/workspace/project/logs/agent_tui.log');
const CAPTURE_STDIO = process.env.CLAUDE_CAPTURE_STDIO === '1';
const WORKSPACE_DIR = path.join(process.env.HOME || '/home/agent', '.claude/workspace/project');

if (!CLAUDE_MSG) {
    console.error('[ERROR] CLAUDE_MSG environment variable is required');
    process.exit(1);
}

let message = Buffer.from(CLAUDE_MSG, 'base64').toString('utf8');
const timestamp = new Date().toISOString().replace('T', ' ').substring(0, 19);

const logEntry = `\n[${timestamp}] $ ${message}\n`;

try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, logEntry);
} catch (e) {
    console.error('[WARN] Failed to write to log file:', e.message);
}

// ---------------------------------------------------------------------------
// 本卡片自己的注入：project/claudeagent.md
//   - CLAUDE.md 是平台级注入（所有卡片共享，平台维护）
//   - claudeagent.md 是**这一张卡片自己的**注入（写本卡片的核心目标/职责/对外接口），
//     由卡片 agent 维护，control 建卡片时会生成模板
// 这里把它注入到每次调用的消息前面，于是无论走面板「发送」还是容器 /ask/claude，
// Claude 都带着本卡片的核心目标在干活。
// ---------------------------------------------------------------------------
const AGENT_PROFILE_FILE = path.join(WORKSPACE_DIR, 'claudeagent.md');
const AGENT_PROFILE_MAX_CHARS = 4000;
let agentProfile = '';
try {
    if (fs.existsSync(AGENT_PROFILE_FILE)) {
        agentProfile = fs.readFileSync(AGENT_PROFILE_FILE, 'utf8').trim();
    }
} catch (e) {
    console.error('[WARN] Failed to read claudeagent.md:', e.message);
}
if (agentProfile) {
    if (agentProfile.length > AGENT_PROFILE_MAX_CHARS) {
        agentProfile = agentProfile.slice(0, AGENT_PROFILE_MAX_CHARS) + '\n...(已截断)';
    }
    message = `【本卡片的核心目标 · claudeagent.md】\n${agentProfile}\n\n----------\n\n${message}`;
} else {
    console.error('[WARN] claudeagent.md 不存在或为空；建议先写清本卡片的核心目标');
}

// 处理图片（可选）
// CLAUDE_IMG 可以是文件路径或布尔标记：
//   - 路径（如 /home/agent/.claude/workspace/project/tmp.png）：优先使用该路径
//   - 非空值：默认使用项目根目录下的 tmp.png
if (CLAUDE_IMG) {
    let imgPath;
    if (CLAUDE_IMG.includes('/') && fs.existsSync(CLAUDE_IMG)) {
        imgPath = CLAUDE_IMG;  // 显式路径
    } else {
        imgPath = path.join(WORKSPACE_DIR, 'tmp.png');  // 默认
    }
    if (fs.existsSync(imgPath)) {
        console.error(`[IMG] Image found at: ${imgPath}`);
        // 使用 file:// 绝对路径引用图片（不用 base64）
        message += `\n\n![image](file://${imgPath})`;
    } else {
        console.error(`[IMG] WARN: CLAUDE_IMG set but image not found at: ${imgPath}`);
    }
}

// --permission-mode: 从环境变量 CLAUDE_PERMISSION_MODE 读取，默认 bypassPermissions
// --system-prompt: 从环境变量 CLAUDE_SYSTEM_PROMPT 读取，覆盖 system prompt
// shell: false: 避免 shell 包装导致信号问题
// cwd: 明确工作目录，让 Claude 在正确目录执行
const permissionMode = process.env.CLAUDE_PERMISSION_MODE || 'bypassPermissions';
const systemPrompt = process.env.CLAUDE_SYSTEM_PROMPT || '';
const claudeArgs = ['--permission-mode', permissionMode, '--continue', '--print', '-'];
if (systemPrompt) {
    claudeArgs.unshift('--system-prompt', systemPrompt);
}
const child = spawn('claude', claudeArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],
    shell: false,
    cwd: WORKSPACE_DIR,
    env: Object.assign({}, process.env, { ANTHROPIC_DISABLE_PREFLIGHT: '1' })
});

const timeout = setTimeout(() => {
    console.error('[TIMEOUT] Claude process killed after 48 hours');
    child.kill('SIGTERM');
    setTimeout(() => child.kill('SIGKILL'), 5000);
}, TIMEOUT_MS);

if (child.stdout) {
    child.stdout.on('data', (data) => {
        const text = data.toString();
        try { fs.appendFileSync(LOG_FILE, text); } catch (e) {}
        if (CAPTURE_STDIO) process.stdout.write(text);
    });
}

if (child.stderr) {
    child.stderr.on('data', (data) => {
        const text = data.toString();
        try { fs.appendFileSync(LOG_FILE, text); } catch (e) {}
        if (CAPTURE_STDIO) process.stderr.write(text);
    });
}

child.on('close', (code) => {
    clearTimeout(timeout);
    process.exit(code);
});

child.on('error', (err) => {
    clearTimeout(timeout);
    console.error('[ERROR]', err.message);
    process.exit(1);
});

child.stdin.write(message);
child.stdin.end();
