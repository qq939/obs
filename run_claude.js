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

// 日志时间戳统一用北京时间（容器默认 UTC，toISOString() 会差 8 小时）。
// 可用 HARNESS_TZ 覆盖；Intl 不依赖容器里的 tzdata，取不到就退回 UTC。
const LOG_TZ = process.env.HARNESS_TZ || 'Asia/Shanghai';
function localTimestamp() {
    try {
        return new Intl.DateTimeFormat('sv-SE', {
            timeZone: LOG_TZ,
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false,
        }).format(new Date()).replace('T', ' ').slice(0, 19);
    } catch (e) {
        return new Date().toISOString().replace('T', ' ').substring(0, 19);
    }
}
const timestamp = localTimestamp();

const logEntry = `\n[${timestamp}] $ ${message}\n`;

try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, logEntry);
} catch (e) {
    console.error('[WARN] Failed to write to log file:', e.message);
}

// ---------------------------------------------------------------------------
// 每轮消息的固定后缀：规范 + 终极目标
//
//   最终发给 Claude 的 message = 用户消息
//                                + （规范：initial_message.md 的内容）
//                                + （终极目标：claudeagent.md 的内容）
//
// 为什么改成「后缀」而不是「建卡时发一条初始消息」：
//   - 建卡/重启不该主动消耗一轮 token，也不该在没人看着的时候让 agent 自己开工；
//   - 但规范和目标必须每轮都在，否则聊长了 agent 会忘掉自己的职责；
//   - 放在消息末尾比放在前面更贴近"指令补丁"的语义，也不会打乱用户原话。
//
// 两个文件都在 config/rules/ 下（容器启动时会被复制到 project 目录）：
//   initial_message.md —— 平台级规范（所有卡片同一份）
//   claudeagent.md     —— 本卡片自己的终极目标（用户通过面板「任务」按钮维护）
// ---------------------------------------------------------------------------
const SPEC_FILE = path.join(WORKSPACE_DIR, 'initial_message.md');
const SPEC_MAX_CHARS = 2000;
const AGENT_PROFILE_FILE = path.join(WORKSPACE_DIR, 'claudeagent.md');
const AGENT_PROFILE_MAX_CHARS = 4000;

function readTrimmed(file, maxChars, label) {
    try {
        if (!fs.existsSync(file)) return '';
        let text = fs.readFileSync(file, 'utf8').trim();
        if (text.length > maxChars) text = text.slice(0, maxChars) + '\n...(已截断)';
        return text;
    } catch (e) {
        console.error(`[WARN] Failed to read ${label}:`, e.message);
        return '';
    }
}

const specText = readTrimmed(SPEC_FILE, SPEC_MAX_CHARS, 'initial_message.md');
const agentProfile = readTrimmed(AGENT_PROFILE_FILE, AGENT_PROFILE_MAX_CHARS, 'claudeagent.md');

const suffixParts = [];
if (specText) {
    suffixParts.push(`（规范：${specText}）`);
} else {
    console.error('[WARN] initial_message.md 不存在或为空；本轮不带规范');
}
if (agentProfile) {
    suffixParts.push(`（终极目标：${agentProfile}）`);
} else {
    console.error('[WARN] claudeagent.md 不存在或为空；本轮不带终极目标');
}
if (suffixParts.length) {
    message = `${message}\n\n${suffixParts.join('\n\n')}`;
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
