const { spawn, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

// 单轮对话最长跑多久（小时）。48h 是上限，不是目标；卡死时靠下面的超时兜底。
const TIMEOUT_HOURS = Number(process.env.CLAUDE_TIMEOUT_HOURS || 48);
const TIMEOUT_MS = TIMEOUT_HOURS * 3600 * 1000;
// 卡死判定：只看「连续多久没有 stdout/stderr」——不要用 CPU 判断活着，
// 因为卡住的进程（ping 循环 / 死循环）照样烧 CPU，那样永远抓不到。
// 默认：60 分钟没输出只「标记卡死」（面板亮红灯），不杀进程；CLAUDE_IDLE_MARK_MINUTES=0 关掉标记。
// 需要「卡死就杀」的卡片，显式设 CLAUDE_IDLE_KILL_MINUTES（默认 0 = 不杀）。
const IDLE_MARK_MINUTES = Number(process.env.CLAUDE_IDLE_MARK_MINUTES || 60);
const IDLE_KILL_MINUTES = Number(process.env.CLAUDE_IDLE_KILL_MINUTES || 0);
const IDLE_MARK_MS = IDLE_MARK_MINUTES * 60 * 1000;
const IDLE_KILL_MS = IDLE_KILL_MINUTES * 60 * 1000;
const STATE_FILE = path.join(process.env.HOME || '/home/agent',
    '.claude/workspace/project/logs/agent_state.json');

function writeState(state, extra) {
    try {
        fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
        fs.writeFileSync(STATE_FILE, JSON.stringify(Object.assign({
            state: state,
            at: Date.now(),
            at_text: localTimestamp(),
            pid: process.pid,
        }, extra || {})));
    } catch (e) { /* 状态文件写不了不影响主流程 */ }
}
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
// ---- 死法取证 -----------------------------------------------------------------
// 容器里 /sys/fs/cgroup 就是本容器的 cgroup（cgroup v2: memory.events / memory.peak）。
// 注意：如果是「宿主机」内存不足，内核杀的是 VM 里的进程，容器自己的 oom_kill 不会 +1，
// 这种情况只能靠宿主机 dmesg / docker 事件看，所以这里把「没 +1」也明确写进日志。
function readOomKill() {
    for (const f of ['/sys/fs/cgroup/memory.events',
                     '/sys/fs/cgroup/memory/memory.oom_control']) {
        try {
            const m = fs.readFileSync(f, 'utf8').match(/^oom_kill\s+(\d+)/m);
            if (m) return Number(m[1]);
        } catch (e) { /* 下一个 */ }
    }
    return null;
}
function readMemStat() {
    const out = {};
    const pairs = [
        ['peak', '/sys/fs/cgroup/memory.peak'],
        ['max_usage', '/sys/fs/cgroup/memory/memory.max_usage_in_bytes'],
        ['current', '/sys/fs/cgroup/memory.current'],
        ['limit', '/sys/fs/cgroup/memory.max'],
        ['limit_v1', '/sys/fs/cgroup/memory/memory.limit_in_bytes'],
    ];
    for (const [k, f] of pairs) {
        try {
            const v = fs.readFileSync(f, 'utf8').trim();
            out[k] = (v === 'max') ? 'max' : Number(v);
        } catch (e) { /* 忽略 */ }
    }
    return out;
}
function mb(v) {
    return (typeof v === 'number' && isFinite(v)) ? Math.round(v / 1048576) + 'MB' : String(v);
}

const startedAt = Date.now();
const oomKillAtStart = readOomKill();
const memAtStart = readMemStat();

const child = spawn('claude', claudeArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],
    shell: false,
    cwd: WORKSPACE_DIR,
    env: Object.assign({}, process.env, { ANTHROPIC_DISABLE_PREFLIGHT: '1' })
});

let timedOut = false;
console.error(`[RUN] claude started pid=${child.pid} timeout=${TIMEOUT_HOURS}h idle_mark=${IDLE_MARK_MINUTES}m idle_kill=${IDLE_KILL_MINUTES}m oom_kill=${oomKillAtStart} mem=${mb(memAtStart.current)}`);
writeState('running', { timeout_hours: TIMEOUT_HOURS, idle_mark_minutes: IDLE_MARK_MINUTES, idle_kill_minutes: IDLE_KILL_MINUTES, started_at: startedAt });

// 看门狗：只看「多久没有 stdout/stderr」。
//   到 IDLE_MARK_MS → 标记卡死（写 state，面板亮红灯），进程继续跑
//   到 IDLE_KILL_MS（默认 0 = 关）→ 才真的杀
let lastOutputAt = Date.now();
let stuck = false;
// 检查频率跟着阈值走：小阈值（测试/激进配置）也能及时触发，默认 30 秒一次
const _idleBase = Math.min(IDLE_MARK_MS > 0 ? IDLE_MARK_MS : Infinity,
                           IDLE_KILL_MS > 0 ? IDLE_KILL_MS : Infinity);
const idleTick = Math.max(2000, Math.min(30000, Math.floor(_idleBase / 4) || 30000));
const idleTimer = setInterval(() => {
    if (stuck) return;
    const idle = Date.now() - lastOutputAt;
    const markMs = IDLE_MARK_MS;
    const killMs = IDLE_KILL_MS;
    if (markMs > 0 && idle >= markMs) {
        stuck = true;
        const line = `[STUCK] 已 ${Math.round(idle / 60000)} 分钟没有输出（标记阈值 ${IDLE_MARK_MINUTES} 分钟）`
            + `，仅标记卡死、不杀进程（pid=${child.pid}，已运行 ${Math.round((Date.now() - startedAt) / 1000)}s）`;
        console.error(line);
        try { fs.appendFileSync(LOG_FILE, `\n${line}\n`); } catch (e) {}
        writeState('stuck', { idle_ms: idle, idle_minutes: IDLE_MARK_MINUTES, killed: false });
        if (killMs > 0 && idle >= killMs) {
            const killLine = `[STUCK] 超过强杀阈值 ${IDLE_KILL_MINUTES} 分钟，杀进程（pid=${child.pid}）`;
            console.error(killLine);
            try { fs.appendFileSync(LOG_FILE, `\n${killLine}\n`); } catch (e) {}
            writeState('stuck', { idle_ms: idle, idle_minutes: IDLE_KILL_MINUTES, killed: true });
            try { child.kill('SIGTERM'); } catch (e) {}
            setTimeout(() => { try { child.kill('SIGKILL'); } catch (e) {} }, 5000);
        }
    } else if (killMs > 0 && idle >= killMs) {
        stuck = true;
        const killLine = `[STUCK] 已 ${Math.round(idle / 60000)} 分钟没有输出，超过强杀阈值 ${IDLE_KILL_MINUTES} 分钟，杀进程`;
        console.error(killLine);
        try { fs.appendFileSync(LOG_FILE, `\n${killLine}\n`); } catch (e) {}
        writeState('stuck', { idle_ms: idle, idle_minutes: IDLE_KILL_MINUTES, killed: true });
        try { child.kill('SIGTERM'); } catch (e) {}
        setTimeout(() => { try { child.kill('SIGKILL'); } catch (e) {} }, 5000);
    }
}, idleTick);
if (idleTimer.unref) idleTimer.unref();

const timeout = setTimeout(() => {
    timedOut = true;
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    const line = `[TIMEOUT] Claude process killed after ${TIMEOUT_HOURS} hours`
        + `（已运行 ${elapsed}s，pid=${child.pid}，先 SIGTERM 再 SIGKILL）`;
    console.error(line);
    try { fs.appendFileSync(LOG_FILE, `\n${line}\n`); } catch (e) {}
    child.kill('SIGTERM');
    setTimeout(() => child.kill('SIGKILL'), 5000);
}, TIMEOUT_MS);

if (child.stdout) {
    child.stdout.on('data', (data) => {
        const text = data.toString();
        lastOutputAt = Date.now();
        try { fs.appendFileSync(LOG_FILE, text); } catch (e) {}
        if (CAPTURE_STDIO) process.stdout.write(text);
    });
}

if (child.stderr) {
    child.stderr.on('data', (data) => {
        const text = data.toString();
        lastOutputAt = Date.now();
        try { fs.appendFileSync(LOG_FILE, text); } catch (e) {}
        if (CAPTURE_STDIO) process.stderr.write(text);
    });
}

child.on('close', (code, signal) => {
    clearTimeout(timeout);
    const elapsed = Math.round((Date.now() - startedAt) / 1000);

    if (idleTimer) clearInterval(idleTimer);
    if (timedOut) {
        // 超时分支上面已经写过 [TIMEOUT]，这里只补一个退出码
        console.error(`[TIMEOUT] claude exited code=${code} signal=${signal || '-'} elapsed=${elapsed}s`);
        writeState('timeout', { elapsed_s: elapsed, exit_code: code, signal: signal || null });
    } else if (stuck) {
        // 卡死已被看门狗杀掉，这里只补一条收尾日志（别再报 KILLED/OOM 误导）
        const oomNow = readOomKill();
        const line = `[STUCK] claude exited signal=${signal || '-'} elapsed=${elapsed}s（无输出超过 ${IDLE_MARK_MINUTES} 分钟，已标记卡死）`;
        console.error(line);
        try { fs.appendFileSync(LOG_FILE, `\n${line}\n`); } catch (e) {}
        writeState('stuck', { elapsed_s: elapsed, signal: signal || null,
                              oom_kill_before: oomKillAtStart, oom_kill_after: oomNow });
    } else if (signal === 'SIGKILL' || signal === 'SIGTERM') {
        const oomNow = readOomKill();
        const mem = readMemStat();
        const oomHit = (oomKillAtStart !== null && oomNow !== null && oomNow > oomKillAtStart);
        const kind = oomHit ? 'OOM' : 'KILLED';
        const why = oomHit
            ? `容器 cgroup oom_kill ${oomKillAtStart}->${oomNow}（内存超限，容器内 OOM）`
            : `容器 cgroup oom_kill 没变（${oomKillAtStart}->${oomNow}）：不是容器内存超限，`
              + `多半是宿主机内存不足被内核杀 / 或外部 kill -9（要定死就看宿主机 dmesg 的 Out of memory）`;
        const line = `[${kind}] Claude process killed by ${signal} after ${elapsed}s`
            + `（peak=${mb(mem.peak || mem.max_usage)} limit=${mb(mem.limit || mem.limit_v1)}）\n`
            + `[${kind}] ${why}`;
        console.error(line);
        try { fs.appendFileSync(LOG_FILE, `\n${line}\n`); } catch (e) {}
        writeState(oomHit ? 'oom' : 'killed',
                   { elapsed_s: elapsed, signal: signal, oom_kill_before: oomKillAtStart, oom_kill_after: oomNow });
    } else if (code !== 0) {
        const line = `[EXIT] claude exited code=${code} signal=${signal || '-'} elapsed=${elapsed}s`;
        console.error(line);
        try { fs.appendFileSync(LOG_FILE, `\n${line}\n`); } catch (e) {}
        writeState('error', { elapsed_s: elapsed, exit_code: code });
    } else {
        writeState('done', { elapsed_s: elapsed, exit_code: code });
    }

    // 被杀时 code 是 null，不能当成功
    if (code !== null && code !== undefined) process.exit(code);
    process.exit(signal === 'SIGKILL' ? 137 : signal === 'SIGTERM' ? 143 : 1);
});

child.on('error', (err) => {
    clearTimeout(timeout);
    console.error('[ERROR]', err.message);
    writeState('error', { reason: err.message });
    process.exit(1);
});

child.stdin.write(message);
child.stdin.end();
