/**
 * OBS - Video Object Storage Web App
 * ===================================
 * Web app listening on port 8082 (bind 0.0.0.0).
 *
 * Main features:
 *   - Douyin/TikTok-style vertical video feed (frontend in public/)
 *   - Chunked upload with resume (init -> chunk* -> complete)
 *   - HTTP Range streaming for video playback (206 Partial Content)
 *   - Video list / delete
 *
 * Platform conventions (systemreadme.md):
 *   - Keep /ask/claude (calls run_claude.js) and /health endpoints.
 *   - Videos stored in obs/; upload temp data in obs/.uploads/.
 *   - Run log goes to logs/run.log (redirected by user_start.sh).
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const PORT = 8082;
const WORKSPACE_DIR = '/home/agent/.claude/workspace/project';
const OBS_DIR = path.join(WORKSPACE_DIR, 'obs');
const UPLOAD_DIR = path.join(OBS_DIR, '.uploads');
const PUBLIC_DIR = path.join(WORKSPACE_DIR, 'public');
const TIMEOUT_MS = 3600 * 1000;
// HLS output lives in its own top-level folder (sibling of obs/), so the
// generated m3u8 + ts files never pollute the obs/ video storage.
const HLS_DIR = path.join(WORKSPACE_DIR, 'hls');
const HLS_TIMEOUT_MS = 600 * 1000;  // 10min，大视频 remux 需要更长时间
// Generation version of the HLS output. Each HLS dir stores a meta.json with
// this version + the source file's size; hlsExists() only counts an HLS as
// ready when it matches. Bumping the version (or the source changing) makes
// every asset regenerate automatically — every feature change applies to all
// videos, no manual "转HLS" button needed.
const HLS_GEN_VERSION = 4;
// 4 MiB per TS segment: 与 B 站/YouTube 同水平，单段 ~5-10 秒 @ 4-8 Mbps。
// 段小 = hls.js 缓冲更稳 / seek 更准 / 缓存复用更高；段大 = 卡顿
const HLS_SEGMENT_BYTES = 4 * 1024 * 1024;

const VIDEO_EXTS = new Set(['.mp4', '.webm', '.ogv', '.mov', '.m4v', '.mkv']);

// 判断是否为视频文件
function isVideoFile(filename) {
    const ext = path.extname(filename).toLowerCase();
    return VIDEO_EXTS.has(ext);
}

const MIME = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.ogv': 'video/ogg',
    '.mov': 'video/quicktime',
    '.m4v': 'video/x-m4v',
    '.mkv': 'video/x-matroska',
    '.m3u8': 'application/vnd.apple.mpegurl',
    '.ts': 'video/mp2t',
    '.js': 'application/javascript',
    '.css': 'text/css',
    '.html': 'text/html; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.txt': 'text/plain; charset=utf-8',
    '.wasm': 'application/wasm'
};

// ---------------------------------------------------------------- helpers

function logLine(...args) {
    const ts = new Date().toISOString().replace('T', ' ').substring(0, 19);
    console.log(`[${ts}]`, ...args);
}

function sendJson(res, status, obj) {
    const body = JSON.stringify(obj);
    res.writeHead(status, {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': Buffer.byteLength(body)
    });
    res.end(body);
}

function sendText(res, status, text) {
    res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end(text);
}

function sendHtml(res, html) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Content-Length': Buffer.byteLength(html) });
    res.end(html);
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

/** 生成下载响应头：RFC 5987 编码中文文件名，强制浏览器下载。 */
function contentDisposition(name) {
    const fallback = String(name).replace(/[^\x20-\x7e]/g, '_').replace(/["\\]/g, '_');
    const encoded = encodeURIComponent(name).replace(/['()*]/g, (c) =>
        '%' + c.charCodeAt(0).toString(16).toUpperCase());
    return `attachment; filename="${fallback}"; filename*=UTF-8''${encoded}`;
}

function formatBytes(n) {
    if (!Number.isFinite(n) || n < 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

function readBody(req, limit = 10 * 1024 * 1024) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        let size = 0;
        req.on('data', (c) => {
            size += c.length;
            if (size > limit) {
                req.destroy();
                reject(new Error('body too large'));
                return;
            }
            chunks.push(c);
        });
        req.on('end', () => resolve(Buffer.concat(chunks)));
        req.on('error', reject);
    });
}

/** Safe file name: strip path separators and traversal. */
function safeName(name) {
    if (!name) return null;
    let n = String(name).trim();
    n = n.split(/[\\/]/).pop();          // strip any directory part
    n = n.replace(/\.\./g, '').replace(/[\x00-\x1f]/g, '');
    if (!n || n === '.' || n === '..') return null;
    return n;
}

/** Validate uploadId: hex/timestamp style id. */
function isUploadId(id) {
    return /^[a-zA-Z0-9-]{8,64}$/.test(id || '');
}

function isChunkIndex(i, totalChunks) {
    return Number.isInteger(i) && i >= 0 && i < totalChunks;
}

function sha256File(filePath) {
    return new Promise((resolve, reject) => {
        const hash = crypto.createHash('sha256');
        const s = fs.createReadStream(filePath);
        s.on('data', (d) => hash.update(d));
        s.on('end', () => resolve(hash.digest('hex')));
        s.on('error', reject);
    });
}

/**
 * Compress a video with ffmpeg into an H.264/AAC MP4 with the moov atom
 * moved to the front (+faststart). This is the same approach YouTube/Douyin
 * use for browser playback: broad codec compatibility + instant start.
 */
function runFfmpeg(args, opts = {}) {
    const spawnOpts = { stdio: ['ignore', 'pipe', 'pipe'] };
    if (opts.cwd) spawnOpts.cwd = opts.cwd;
    const child = spawn('ffmpeg', args, spawnOpts);
    const p = new Promise((resolve, reject) => {
        let stderr = '';
        child.stderr.on('data', (d) => { stderr += d.toString(); });
        child.on('error', (err) => reject(new Error('ffmpeg not available: ' + err.message)));
        child.on('close', (code) => {
            if (code === 0) resolve();
            else reject(new Error('ffmpeg exit ' + code + ': ' + stderr.split('\n').slice(-3).join(' ').trim()));
        });
    });
    p.kill = () => { try { child.kill('SIGKILL'); } catch(e) {} };
    return p;
}

async function compressVideo(filePath) {
    const before = fs.statSync(filePath).size;
    const tmpOut = path.join(UPLOAD_DIR, `.comp-${Date.now()}-${crypto.randomBytes(4).toString('hex')}.mp4`);
    const args = [
        '-y', '-i', filePath,
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-vf', "scale='min(1920,iw)':-2",
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        tmpOut
    ];
    try {
        await runFfmpeg(args);
    } catch (e) {
        if (fs.existsSync(tmpOut)) fs.unlinkSync(tmpOut);
        throw e;
    }
    const after = fs.statSync(tmpOut).size;
    if (after >= before) {
        // Compressed output isn't smaller (already well-compressed); keep original.
        fs.unlinkSync(tmpOut);
        return { skipped: true, before, after, saved: 0, savedPct: 0 };
    }
    fs.renameSync(tmpOut, filePath);
    return { skipped: false, before, after, saved: before - after, savedPct: Math.round((1 - after / before) * 100) };
}

// ---------------------------------------------------------------- HLS

function hlsExists(name) {
    const dir = path.join(HLS_DIR, name);
    if (!fs.existsSync(path.join(dir, 'index.m3u8'))) return false;
    // Only count an HLS as ready when it was generated by the current
    // generation version AND matches the current source file. Old or stale
    // output (e.g. generated before the rotation fix) is treated as missing
    // so the startup sweep / lazy GET regenerates it.
    const metaPath = path.join(dir, 'meta.json');
    if (!fs.existsSync(metaPath)) return false;
    try {
        const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
        if (meta.version !== HLS_GEN_VERSION) return false;
        const srcPath = path.join(OBS_DIR, name);
        if (!fs.existsSync(srcPath)) return false;
        return meta.size === fs.statSync(srcPath).size;
    } catch (e) {
        return false;
    }
}

function invalidateHls(name) {
    fs.rmSync(path.join(HLS_DIR, name), { recursive: true, force: true });
}

/** Probe the first video/audio codec of a media file via ffprobe. */
function detectCodecs(filePath) {
    return new Promise((resolve, reject) => {
        const child = spawn('ffprobe', [
            '-v', 'error',
            '-analyzeduration', '1000000',  // 1s — much faster on slow storage
            '-probesize', '1000000',
            '-show_entries', 'stream=codec_type,codec_name',
            '-of', 'json',
            filePath
        ], { stdio: ['ignore', 'pipe', 'pipe'] });
        let out = '', err = '';
        child.stdout.on('data', (d) => { out += d.toString(); });
        child.stderr.on('data', (d) => { err += d.toString(); });
        child.on('error', (e) => reject(new Error('ffprobe unavailable: ' + e.message)));
        child.on('close', (code) => {
            if (code !== 0) return reject(new Error('ffprobe failed: ' + err.split('\n').slice(-2).join(' ').trim()));
            try {
                const j = JSON.parse(out);
                let video = null, audio = null;
                for (const s of (j.streams || [])) {
                    if (s.codec_type === 'video' && !video) video = s.codec_name;
                    if (s.codec_type === 'audio' && !audio) audio = s.codec_name;
                }
                resolve({ video, audio });
            } catch (e) { reject(e); }
        });
    });
}

// 简单验证文件是否可播放（只检查能否被 ffprobe 解析）
async function probeFile(filePath) {
    return detectCodecs(filePath);
}

/**
 * Read container duration in seconds (for the /videos API).
 * Primary: sum EXTINF from the HLS playlist we already generated (no external deps).
 * Fallback: ffprobe on the source file when HLS isn't available.
 * Returns 0 if neither path yields a positive number.
 */
function probeDurationSync(filePath, name) {
    // 1) 从 HLS index.m3u8 求和 EXTINF（不依赖 ffmpeg/ffprobe，对老 HLS 也有效）
    if (name) {
        const d = hlsDurationSync(name);
        if (d > 0) return d;
    }
    // 2) ffprobe 兜底（容器里若装了 ffmpeg 才会命中）
    try {
        const out = require('child_process').execFileSync('ffprobe',
            ['-v', 'error', '-show_entries', 'format=duration', '-of', 'json', filePath],
            { stdio: ['ignore', 'pipe', 'ignore'], timeout: 4000 });
        const j = JSON.parse(out.toString());
        const d = parseFloat(j && j.format && j.format.duration);
        return Number.isFinite(d) && d > 0 ? d : 0;
    } catch (_) { return 0; }
}

/**
 * Sum EXTINF durations in <HLS_DIR>/<name>/index.m3u8 to derive total seconds.
 * Pure file read + tiny regex parse, safe to call from listVideoFiles() on every /videos hit.
 */
function hlsDurationSync(name) {
    try {
        const m3u8 = fs.readFileSync(path.join(HLS_DIR, name, 'index.m3u8'), 'utf8');
        // 形如 "#EXTINF:1.999633," —— 只匹配逗号前的浮点
        const re = /#EXTINF:([0-9.]+)/g;
        let total = 0, m;
        while ((m = re.exec(m3u8)) !== null) {
            const v = parseFloat(m[1]);
            if (Number.isFinite(v) && v > 0) total += v;
        }
        return total > 0 ? total : 0;
    } catch (_) { return 0; }
}

/**
 * Detect the display rotation (in degrees) of the first video stream.
 * Phone recordings (e.g. iPhone MOV) store a Display Matrix / rotate tag; a
 * direct `-c copy` remux to TS would carry it only as display-matrix SEI which
 * hls.js/MSE ignores, so the HLS plays rotated 90°. When rotation is non-zero
 * we force a re-encode (ffmpeg's built-in autorotation bakes it into pixels).
 */
function detectRotation(filePath) {
    return new Promise((resolve, reject) => {
        const child = spawn('ffprobe', [
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream_tags=rotate:stream_side_data=rotation',
            '-of', 'json',
            filePath
        ], { stdio: ['ignore', 'pipe', 'pipe'] });
        let out = '', err = '';
        child.stdout.on('data', (d) => { out += d.toString(); });
        child.stderr.on('data', (d) => { err += d.toString(); });
        child.on('error', (e) => reject(new Error('ffprobe unavailable: ' + e.message)));
        child.on('close', (code) => {
            if (code !== 0) return reject(new Error('ffprobe failed: ' + err.split('\n').slice(-2).join(' ').trim()));
            let rotation = 0;
            try {
                const j = JSON.parse(out);
                const s = (j.streams || [])[0];
                if (s) {
                    const tag = s.tags && s.tags.rotate;
                    if (tag) {
                        rotation = parseInt(tag, 10) || 0;
                    } else if (Array.isArray(s.side_data_list)) {
                        for (const sd of s.side_data_list) {
                            if (sd && typeof sd.rotation === 'number') { rotation = sd.rotation; break; }
                        }
                    }
                }
            } catch (e) { /* treat unparseable as no rotation */ }
            // Normalize to 0/90/180/270.
            rotation = ((Math.round(rotation) % 360) + 360) % 360;
            resolve(rotation);
        });
    });
}

/** Fast path: source already H.264 + AAC/MP3 AND upright AND a friendly container
 * (.mp4 / .m4v) can be remuxed to TS without re-encoding.
 *   - .mov / .mkv: always re-encode. QuickTime often has the moov atom at the
 *     tail (not faststart) and inconsistent codec tags; remuxing the TS keeps
 *     both issues, so playback through hls.js/MSE stutters.
 *   - webm/ogv: always re-encode (not H.264).
 */
function canRemux(srcPath, codecs, rotation) {
    if (rotation !== 0) return false;
    if (codecs.video !== 'h264') return false;
    if (codecs.audio && codecs.audio !== 'aac' && codecs.audio !== 'mp3') return false;
    const ext = path.extname(srcPath).toLowerCase();
    return ext === '.mp4' || ext === '.m4v';
}

/**
 * Build ffmpeg args. Runs with cwd=outDir so the playlist references relative
 * `seg-%05d.ts` names that resolve under /hls/<name>/ automatically.
 * VOD + hls_list_size 0 keeps ALL segments in the (finite) playlist.
 * Size-based segmentation: -hls_segment_size caps each TS at 50 MiB (GOP-aligned,
 * actual size may vary slightly). Rotated sources always re-encode: ffmpeg's
 * built-in autorotation (inserted before the -vf graph) bakes the rotation into
 * the pixels, so hls.js/MSE players see the upright image instead of a
 * 90°-rotated one.
 */
function buildHlsArgs(srcPath, codecs, rotation) {
    const mapV = ['-map', '0:v:0'];
    const mapA = codecs.audio ? ['-map', '0:a:0'] : [];
    const common = [
        '-f', 'hls',
        '-hls_segment_size', String(HLS_SEGMENT_BYTES),
        '-hls_list_size', '0',
        '-hls_playlist_type', 'vod',
        '-hls_segment_filename', 'seg-%05d.ts',
        'index.m3u8'
    ];
    if (canRemux(srcPath, codecs, rotation)) {
        return ['-y', '-i', srcPath, ...mapV, ...mapA, '-c', 'copy', ...common];
    }
    // Rotated, webm/ogv/mkv (VP9/VP8/Opus/AV1) and anything not H.264+AAC
    // -> re-encode to H.264/AAC.
    const venc = ['-c:v', 'libx264', '-preset', 'medium', '-crf', '23', '-pix_fmt', 'yuv420p', '-vf', "scale='min(1920,iw)':-2"];
    const aenc = codecs.audio ? ['-c:a', 'aac', '-b:a', '128k'] : ['-an'];
    return ['-y', '-i', srcPath, ...mapV, ...mapA, ...venc, ...aenc, ...common];
}

function countSegments(dir) {
    try {
        return fs.readdirSync(dir).filter((f) => /^seg-\d+\.ts$/.test(f)).length;
    } catch (e) { return 0; }
}

// name -> in-flight generation Promise, so concurrent /hls requests for the
// same not-yet-generated video only run one ffmpeg.
const hlsLocks = new Map();

function generateHls(name) {
    if (hlsLocks.has(name)) return hlsLocks.get(name);
    const p = doGenerateHls(name).finally(() => hlsLocks.delete(name));
    hlsLocks.set(name, p);
    return p;
}

async function doGenerateHls(name) {
    const srcPath = path.join(OBS_DIR, name);
    if (!fs.existsSync(srcPath) || !fs.statSync(srcPath).isFile()) {
        throw new Error('source missing');
    }
    if (hlsExists(name)) return;

    const tmpDir = path.join(HLS_DIR, `.tmp-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`);
    fs.mkdirSync(tmpDir, { recursive: true });
    try {
        const [codecs, rotation] = await Promise.all([
            detectCodecs(srcPath),
            detectRotation(srcPath)
        ]);
        await runFfmpeg(buildHlsArgs(srcPath, codecs, rotation), { cwd: tmpDir });
        // Source may have been deleted while ffmpeg was running.
        if (!fs.existsSync(srcPath)) throw new Error('source deleted during hls generation');
        if (hlsExists(name)) {
            fs.rmSync(tmpDir, { recursive: true, force: true });   // lost a race, keep existing
            return;
        }
        fs.rmSync(path.join(HLS_DIR, name), { recursive: true, force: true }); // clear stale
        fs.renameSync(tmpDir, path.join(HLS_DIR, name));
        // meta.json records the generation version + source size so hlsExists()
        // can detect stale output and every asset regenerates on version bumps.
        const srcStat = fs.statSync(srcPath);
        const duration = probeDurationSync(srcPath, name);  // HLS EXTINF 求和（或 ffprobe 兜底），给前端列表展示用
        fs.writeFileSync(path.join(HLS_DIR, name, 'meta.json'), JSON.stringify({
            version: HLS_GEN_VERSION,
            size: srcStat.size,
            mtime: srcStat.mtimeMs,
            rotation,
            duration
        }));
        logLine(`hls generated: ${name} (${countSegments(path.join(HLS_DIR, name))} segs, rotation ${rotation})`);
    } catch (e) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
        throw e;
    }
}

function withTimeout(p, ms, onTimeout) {
    let timer;
    return Promise.race([
        p,
        new Promise((_, rej) => { timer = setTimeout(() => { if (onTimeout) onTimeout(); rej(new Error('hls generation timeout')); }, ms); })
    ]).finally(() => clearTimeout(timer));
}

function listVideoFiles() {
    if (!fs.existsSync(OBS_DIR)) return [];
    const items = fs.readdirSync(OBS_DIR)
        .filter((f) => VIDEO_EXTS.has(path.extname(f).toLowerCase()) && !f.startsWith('.'))
        .map((name) => {
            const stat = fs.statSync(path.join(OBS_DIR, name));
            // duration: 优先读 HLS meta.json（生成时已 ffprobe），否则同步 ffprobe 一次（缓存到内存）
            let duration = 0;
            const metaPath = path.join(HLS_DIR, name, 'meta.json');
            if (fs.existsSync(metaPath)) {
                try {
                    const m = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
                    if (Number.isFinite(m.duration) && m.duration > 0) duration = m.duration;
                } catch (_) {}
            }
            if (!duration) duration = probeDurationSync(path.join(OBS_DIR, name), name);
            return {
                name, size: stat.size, mtime: stat.mtime, duration,
                url: `/obs/${encodeURIComponent(name)}`,
                hls: `/hls/${encodeURIComponent(name)}/index.m3u8`,
                hlsReady: hlsExists(name)
            };
        });
    // 随机顺序（Fisher–Yates）
    for (let i = items.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [items[i], items[j]] = [items[j], items[i]];
    }
    return items;
}

// ------------------------------------------------------- upload operations

function initUpload(body) {
    const filename = safeName(body.filename);
    const size = Number(body.size);
    const hash = String(body.hash || '').toLowerCase();
    const chunkSize = Number(body.chunkSize) || 1024 * 1024;
    const totalChunks = Number(body.totalChunks) || Math.max(1, Math.ceil(size / chunkSize));
    if (!filename || !Number.isFinite(size) || size < 0) {
        return { error: { status: 400, msg: 'invalid filename/size' } };
    }
    const destPath = path.join(OBS_DIR, filename);
    // If a file with the same hash already exists, tell client to skip.
    if (hash && fs.existsSync(destPath)) {
        // Cheap check: if sizes match too, treat as already uploaded.
        if (fs.statSync(destPath).size === size) {
            return { skip: true, url: `/obs/${encodeURIComponent(filename)}` };
        }
    }

    // Resume support: if an in-progress session with the same hash+filename
    // exists, return it so the client can continue uploading missing chunks.
    if (fs.existsSync(UPLOAD_DIR)) {
        const entries = fs.readdirSync(UPLOAD_DIR);
        for (const id of entries) {
            const metaPath = path.join(UPLOAD_DIR, id, 'meta.json');
            if (!fs.existsSync(metaPath)) continue;
            try {
                const m = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
                if (m.hash && m.hash === hash && m.filename === filename && m.size === size
                    && m.uploaded && m.uploaded.length < m.totalChunks) {
                    return { uploadId: m.uploadId, chunkSize: m.chunkSize, totalChunks: m.totalChunks, uploaded: m.uploaded, filename: m.filename, resumed: true };
                }
            } catch (e) { /* skip corrupt meta */ }
        }
    }

    const uploadId = crypto.randomBytes(8).toString('hex') + '-' + Date.now().toString(36);
    const dir = path.join(UPLOAD_DIR, uploadId);
    fs.mkdirSync(dir, { recursive: true });
    const meta = { uploadId, filename, size, hash, chunkSize, totalChunks, uploaded: [] };
    fs.writeFileSync(path.join(dir, 'meta.json'), JSON.stringify(meta, null, 2));
    return { uploadId, chunkSize, totalChunks, uploaded: [], filename };
}

function saveChunk(uploadId, index, buf) {
    const dir = path.join(UPLOAD_DIR, uploadId);
    const metaPath = path.join(dir, 'meta.json');
    if (!fs.existsSync(metaPath)) return { error: { status: 404, msg: 'upload not found' } };
    const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
    if (!isChunkIndex(index, meta.totalChunks)) return { error: { status: 400, msg: 'bad chunk index' } };
    const partPath = path.join(dir, `${index}.part`);
    fs.writeFileSync(partPath, buf);
    if (!meta.uploaded.includes(index)) meta.uploaded.push(index);
    fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2));
    return { ok: true, index, uploaded: meta.uploaded.length, total: meta.totalChunks };
}

async function completeUpload(uploadId) {
    const dir = path.join(UPLOAD_DIR, uploadId);
    const metaPath = path.join(dir, 'meta.json');
    if (!fs.existsSync(metaPath)) return { error: { status: 404, msg: 'upload not found' } };
    const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));

    // Check all chunks present
    for (let i = 0; i < meta.totalChunks; i++) {
        if (!fs.existsSync(path.join(dir, `${i}.part`))) {
            return { error: { status: 400, msg: `missing chunk ${i}` } };
        }
    }

    // Merge chunks -> obs/filename
    const destPath = path.join(OBS_DIR, meta.filename);
    const ws = fs.createWriteStream(destPath);
    for (let i = 0; i < meta.totalChunks; i++) {
        await new Promise((resolve, reject) => {
            const rs = fs.createReadStream(path.join(dir, `${i}.part`));
            rs.on('error', reject);
            rs.pipe(ws, { end: false });
            rs.on('end', resolve);
        });
    }
    await new Promise((resolve) => ws.end(resolve));

    // Verify sha256 if provided
    if (meta.hash) {
        const realHash = await sha256File(destPath);
        if (realHash !== meta.hash) {
            fs.unlinkSync(destPath);
            fs.rmSync(dir, { recursive: true, force: true });
            return { error: { status: 400, msg: 'sha256 mismatch' } };
        }
    }

    fs.rmSync(dir, { recursive: true, force: true });
    logLine(`upload complete: ${meta.filename} (${meta.size} bytes)`);

    // 方案A：对视频文件生成HLS（分片直接作为.ts）
    const isVideo = isVideoFile(meta.filename);
    if (isVideo) {
        const hlsDir = path.join(HLS_DIR, meta.filename);
        if (!fs.existsSync(hlsDir)) fs.mkdirSync(hlsDir, { recursive: true });

        const chunkSize = meta.chunkSize;
        for (let i = 0; i < meta.totalChunks; i++) {
            const dstPath = path.join(hlsDir, `${i}.ts`);
            const start = i * chunkSize;
            const end = Math.min(start + chunkSize, meta.size);
            const rs = fs.createReadStream(destPath, { start, end });
            const ws = fs.createWriteStream(dstPath);
            await new Promise((resolve, reject) => {
                rs.pipe(ws);
                rs.on('end', resolve);
                rs.on('error', reject);
                ws.on('error', reject);
            });
        }

        // 生成m3u8
        const m3u8Path = path.join(hlsDir, 'index.m3u8');
        const targetDuration = Math.max(10, Math.ceil(chunkSize / (1024 * 1024) * 8));
        let m3u8 = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:' + targetDuration + '\n#EXT-X-MEDIA-SEQUENCE:0\n';
        for (let i = 0; i < meta.totalChunks; i++) {
            m3u8 += '#EXTINF:10.0,\n' + i + '.ts\n';
        }
        m3u8 += '#EXT-X-ENDLIST\n';
        fs.writeFileSync(m3u8Path, m3u8);

        // 写入meta.json
        fs.writeFileSync(path.join(hlsDir, 'meta.json'), JSON.stringify({
            version: HLS_GEN_VERSION,
            size: meta.size,
            chunkSize: chunkSize,
            totalChunks: meta.totalChunks,
            source: 'upload_chunk_direct'
        }, null, 2));

        logLine(`HLS generated for ${meta.filename}: ${meta.totalChunks} chunks`);
        return { ok: true, url: `/hls/${encodeURIComponent(meta.filename)}/index.m3u8` };
    }

    return { ok: true, url: `/obs/${encodeURIComponent(meta.filename)}` };
}

// -------------------------------------------------------------- HTTP range

function streamFileWithRange(res, filePath, rangeHeader, downloadName) {
    const stat = fs.statSync(filePath);
    const total = stat.size;
    const mime = MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
    const disp = downloadName ? contentDisposition(downloadName) : null;

    let start = 0;
    let end = total - 1;

    if (rangeHeader) {
        const m = /bytes=(\d*)-(\d*)/.exec(rangeHeader);
        if (m) {
            if (m[1] !== '') start = parseInt(m[1], 10);
            if (m[2] !== '') end = parseInt(m[2], 10);
            if (m[1] === '' && m[2] !== '') {
                // suffix range: last N bytes
                const suffix = parseInt(m[2], 10);
                start = Math.max(0, total - suffix);
                end = total - 1;
            }
            if (start >= total || start > end) {
                res.writeHead(416, {
                    'Content-Range': `bytes */${total}`,
                    'Content-Type': mime,
                    ...(disp ? { 'Content-Disposition': disp } : {})
                });
                res.end();
                return;
            }
            end = Math.min(end, total - 1);
            res.writeHead(206, {
                'Content-Range': `bytes ${start}-${end}/${total}`,
                'Accept-Ranges': 'bytes',
                'Content-Length': end - start + 1,
                'Content-Type': mime,
                'Cache-Control': 'no-cache',
                ...(disp ? { 'Content-Disposition': disp } : {})
            });
        } else {
            res.writeHead(200, {
                'Content-Length': total,
                'Content-Type': mime,
                'Accept-Ranges': 'bytes',
                ...(disp ? { 'Content-Disposition': disp } : {})
            });
        }
    } else {
        res.writeHead(200, {
            'Content-Length': total,
            'Content-Type': mime,
            'Accept-Ranges': 'bytes',
            ...(disp ? { 'Content-Disposition': disp } : {})
        });
    }

    fs.createReadStream(filePath, { start, end }).pipe(res);
}

// ------------------------------------------------------------ file manager /obs

// 全局排序参数（使用位置：sendObsPage 列表排序判断）
// SORT_MODE: 文件列表排序模式，'time' 按时间倒序，'ext' 按扩展名正序
// 使用位置：sendObsPage listAllFiles 排序逻辑
let OBS_SORT_MODE = 'time';

/** 列出 obs/ 目录下全部文件（排除隐藏文件与目录），返回 name/size/mtime。
 * 排序规则：SORT_MODE='time' 按 mtime 倒序，SORT_MODE='ext' 按扩展名+文件名正序。 */
function listAllFiles() {
    if (!fs.existsSync(OBS_DIR)) return [];
    const raw = fs.readdirSync(OBS_DIR).filter((f) => !f.startsWith('.'));
    const entries = raw.map((name) => {
        const full = path.join(OBS_DIR, name);
        let stat;
        try { stat = fs.statSync(full); } catch (e) { return null; }
        if (!stat.isFile()) return null;
        return { name, size: stat.size, mtime: stat.mtimeMs };
    }).filter(Boolean);

    if (OBS_SORT_MODE === 'ext') {
        entries.sort((a, b) => {
            const extA = path.extname(a.name).toLowerCase();
            const extB = path.extname(b.name).toLowerCase();
            if (extA < extB) return -1;
            if (extA > extB) return 1;
            return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
        });
    } else {
        entries.sort((a, b) => b.mtime - a.mtime);
    }
    return entries;
}

/** GET /obs：文件管理页面，与 obs 项目保持一致。
 * 特性：按时间/扩展名排序、下载（相对路径）、上传（表单 POST + JS 分片）、删除。 */
function sendObsPage(res, sortParam) {
    if (sortParam === 'ext' || sortParam === 'time') OBS_SORT_MODE = sortParam;
    const files = listAllFiles();
    const rows = files.map((f) => {
        const dl = `/obs/${encodeURIComponent(f.name)}?download=1`;
        const sizeStr = formatBytes(f.size);
        const icon = f.name.match(/\.(mp4|webm|mov|m4v|mkv)$/i) ? '🎬' : '📄';
        return `<div class="file-item">
            <div class="file-icon">${icon}</div>
            <div class="file-info">
                <div class="file-name">${escapeHtml(f.name)}</div>
                <div class="file-meta">${sizeStr}</div>
            </div>
            <div class="file-actions">
                <a href="${escapeHtml(dl)}">下载</a>
                <button class="delete-btn" onclick="deleteFile('${escapeHtml(f.name.replace(/'/g, "\\'"))}')">删除</button>
            </div>
        </div>`;
    }).join('');

    const html = `<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>文件管理</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: #eee; min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
        h1 { font-size: 24px; font-weight: 600; margin-bottom: 20px; color: #fff; }
        .top-bar { display: flex; gap: 16px; align-items: center; margin-bottom: 24px; flex-wrap: wrap; }
        .upload-area { flex: 1; min-width: 300px; }
        .upload-area input[type=file] { display: none; }
        .upload-btn { display: inline-flex; align-items: center; gap: 8px; padding: 14px 28px; background: #e94560; color: #fff; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
        .upload-btn:hover { background: #ff6b6b; transform: translateY(-1px); }
        .sort-controls { display: flex; gap: 8px; }
        .sort-controls a { padding: 10px 18px; background: #16213e; color: #aaa; text-decoration: none; border-radius: 8px; font-size: 14px; transition: all 0.2s; }
        .sort-controls a:hover { background: #0f3460; color: #fff; }
        .sort-controls a.active { background: #0f3460; color: #e94560; }
        .file-list { display: grid; gap: 12px; }
        .file-item { display: flex; align-items: center; padding: 16px 20px; background: #16213e; border-radius: 12px; transition: all 0.2s; }
        .file-item:hover { background: #1f2b47; transform: translateX(4px); }
        .file-icon { font-size: 28px; margin-right: 16px; }
        .file-info { flex: 1; min-width: 0; }
        .file-name { font-size: 15px; font-weight: 500; word-break: break-all; color: #fff; }
        .file-meta { font-size: 12px; color: #888; margin-top: 4px; }
        .file-actions { display: flex; gap: 8px; }
        .file-actions a, .file-actions button { padding: 8px 14px; background: #0f3460; color: #fff; text-decoration: none; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; transition: all 0.2s; }
        .file-actions a:hover, .file-actions button:hover { background: #e94560; }
        .file-actions .delete-btn { background: #dc3545; }
        .file-actions .delete-btn:hover { background: #ff4757; }
        .empty { text-align: center; padding: 60px 20px; color: #666; }
        .empty-icon { font-size: 64px; margin-bottom: 16px; }
        /* Upload progress */
        .upload-progress { display: none; margin-top: 16px; }
        .upload-progress.active { display: block; }
        .progress-bar { height: 8px; background: #16213e; border-radius: 4px; overflow: hidden; margin-top: 8px; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #e94560, #ff6b6b); width: 0%; transition: width 0.3s; }
        .progress-text { font-size: 14px; color: #888; margin-top: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📁 文件管理</h1>
        <div class="top-bar">
            <div class="upload-area">
                <input type="file" id="fileInput" multiple>
                <button class="upload-btn" onclick="document.getElementById('fileInput').click()">
                    ⬆ 上传文件
                </button>
                <div class="upload-progress" id="uploadProgress">
                    <div class="progress-text" id="progressText">准备上传...</div>
                    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
                </div>
            </div>
            <div class="sort-controls">
                <a href="/obs?sort=time" class="${OBS_SORT_MODE === 'time' ? 'active' : ''}">按时间</a>
                <a href="/obs?sort=ext" class="${OBS_SORT_MODE === 'ext' ? 'active' : ''}">按扩展名</a>
            </div>
        </div>
        ${files.length ? `<div class="file-list">${rows}</div>` : '<div class="empty"><div class="empty-icon">📂</div><p>暂无文件</p></div>'}
    </div>
    <script>
        const CHUNK_SIZE = 10 * 1024 * 1024; // 10MB chunks
        
        async function deleteFile(filename) {
            if (!confirm('确定要删除 ' + filename + ' 吗？')) return;
            const resp = await fetch('/obs/' + encodeURIComponent(filename), { method: 'DELETE' });
            if (resp.ok) location.reload(); else alert('删除失败');
        }
        
        function showProgress(pct, text) {
            const el = document.getElementById('uploadProgress');
            el.classList.add('active');
            document.getElementById('progressFill').style.width = pct + '%';
            document.getElementById('progressText').textContent = text;
        }
        
        function hideProgress() {
            document.getElementById('uploadProgress').classList.remove('active');
        }
        
        async function uploadFile(file) {
            var filename = file.name;
            var total = file.size;
            var offset = 0;
            var totalChunks = Math.ceil(total / CHUNK_SIZE);
            
            try {
                while (offset < total) {
                    var end = Math.min(offset + CHUNK_SIZE, total) - 1;
                    var pct = Math.round(offset / total * 100);
                    showProgress(pct, '上传中 ' + pct + '% (' + Math.ceil(offset / CHUNK_SIZE) + '/' + totalChunks + ' 分片)');
                    
                    var blob = file.slice(offset, end + 1);
                    var resp = await fetch('/upload/' + encodeURIComponent(filename), {
                        method: 'PUT',
                        headers: {
                            'Content-Range': 'bytes ' + offset + '-' + end + '/' + total,
                            'Content-Type': 'application/octet-stream'
                        },
                        body: await blob.arrayBuffer()
                    });
                    
                    if (resp.status !== 200) {
                        var text = await resp.text();
                        throw new Error('上传失败: ' + resp.status + ' ' + text);
                    }
                    offset = end + 1;
                }
                showProgress(100, '上传完成！');
                setTimeout(function() { hideProgress(); location.reload(); }, 1000);
            } catch (err) {
                hideProgress();
                alert('上传出错: ' + err.message);
            }
        }
        
        document.getElementById('fileInput').addEventListener('change', function(e) {
            const files = Array.from(e.target.files);
            if (files.length === 0) return;
            
            // Upload files sequentially
            (function uploadNext(i) {
                if (i >= files.length) return;
                uploadFile(files[i]).then(() => uploadNext(i + 1));
            })(0);
            
            // Reset input for next selection
            e.target.value = '';
        });
    </script>
</body>
</html>`;
    sendHtml(res, html);
}

// ----------------------------------------------------------------- ask/claude

function handleAsk(res, searchParams, bodyText) {
    let q = searchParams.get('q');
    if (!q && bodyText) {
        try { q = JSON.parse(bodyText).q; } catch (e) { /* ignore */ }
    }
    if (!q) return sendText(res, 400, 'Missing q parameter');

    let question;
    try {
        question = (q.includes(' ') || q.length < 50) ? decodeURIComponent(q) : Buffer.from(q, 'base64').toString('utf8');
    } catch (e) {
        return sendText(res, 400, 'Invalid encoding');
    }

    const systemPrompt = 'You are a helpful assistant. Answer the question concisely. Do not use markdown or formatting.';
    const fullMessage = `${systemPrompt}\n\n${question}`;
    const msgB64 = Buffer.from(fullMessage).toString('base64');
    logLine(`/ask/claude question: ${question.slice(0, 200)}`);

    const child = spawn('node', [path.join(WORKSPACE_DIR, 'run_claude.js')], {
        cwd: WORKSPACE_DIR,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, ANTHROPIC_DISABLE_PREFLIGHT: '1', CLAUDE_CAPTURE_STDIO: '1', CLAUDE_MSG: msgB64 }
    });

    let stdout = '';
    let stderr = '';
    let settled = false;

    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });
    child.on('close', (code) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        if (code === 0) res.end(stdout.trim());
        else {
            res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
            res.end(stderr.trim() || `Exit code: ${code}`);
        }
    });
    child.on('error', (err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end(`Spawn error: ${err.message}`);
    });
    const timeout = setTimeout(() => {
        if (settled) return;
        settled = true;
        child.kill('SIGTERM');
        setTimeout(() => child.kill('SIGKILL'), 5000);
        res.writeHead(504, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('Request timeout (60 minutes)');
    }, TIMEOUT_MS);

    res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
}

// ------------------------------------------------------------------ router

const server = http.createServer(async (req, res) => {
    res.setTimeout(TIMEOUT_MS, () => {
        res.writeHead(504, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('Request timeout');
    });

    const url = new URL(req.url, `http://localhost:${PORT}`);
    const p = url.pathname;
    const method = req.method || 'GET';

    try {
        // ---- health
        if (method === 'GET' && p === '/health') return sendText(res, 200, 'OK');

        // ---- platform: /ask/claude
        if ((method === 'GET' || method === 'POST') && p === '/ask/claude') {
            if (method === 'POST') {
                const body = await readBody(req, 2 * 1024 * 1024);
                return handleAsk(res, url.searchParams, body.toString('utf8'));
            }
            return handleAsk(res, url.searchParams, null);
        }

        // ---- upload init
        if (method === 'POST' && p === '/upload/init') {
            const bodyBuf = await readBody(req, 1024 * 1024);
            const body = JSON.parse(bodyBuf.toString('utf8') || '{}');
            const result = initUpload(body);
            if (result.error) return sendJson(res, result.error.status, { error: result.error.msg });
            return sendJson(res, 200, result);
        }

        // ---- chunk upload: PUT /upload/chunk/:uploadId/:index
        const chunkMatch = p.match(/^\/upload\/chunk\/([^/]+)\/(\d+)$/);
        if (method === 'PUT' && chunkMatch) {
            const uploadId = chunkMatch[1];
            const index = parseInt(chunkMatch[2], 10);
            if (!isUploadId(uploadId)) return sendJson(res, 400, { error: 'bad uploadId' });
            const buf = await readBody(req, 200 * 1024 * 1024);
            const result = saveChunk(uploadId, index, buf);
            if (result.error) return sendJson(res, result.error.status, { error: result.error.msg });
            return sendJson(res, 200, result);
        }

        // ---- complete: POST /upload/complete/:uploadId
        const completeMatch = p.match(/^\/upload\/complete\/([^/]+)$/);
        if (method === 'POST' && completeMatch) {
            const uploadId = completeMatch[1];
            if (!isUploadId(uploadId)) return sendJson(res, 400, { error: 'bad uploadId' });
            const result = await completeUpload(uploadId);
            if (result.error) return sendJson(res, result.error.status, { error: result.error.msg });
            const fname = safeName(decodeURIComponent(result.url.replace(/^\/obs\//, '')));
            if (fname) generateHls(fname).catch((e) => logLine('hls bg gen failed:', e.message));
            return sendJson(res, 200, result);
        }

        // ---- simple upload: PUT /upload/:filename (支持断点续传分片追加)
        const simpleMatch = p.match(/^\/upload\/(.+)$/);
        if (method === 'PUT' && simpleMatch) {
            // 解码 URL 编码，保持原始 UTF-8 文件名
            let rawFilename = simpleMatch[1];
            try { rawFilename = decodeURIComponent(rawFilename); } catch (_) {}
            const filename = safeName(rawFilename);
            if (!filename) return sendJson(res, 400, { error: 'invalid filename' });
            
            const contentRange = req.headers['content-range'];
            const destPath = path.join(OBS_DIR, filename);
            
            if (contentRange) {
                // 分片上传：使用稀疏文件追加
                // Content-Range: bytes <start>-<end>/<total>
                const match = contentRange.match(/bytes (\d+)-(\d+)\/(\d+)/);
                if (match) {
                    const start = parseInt(match[1], 10);
                    const endPos = parseInt(match[2], 10);
                    const totalSize = parseInt(match[3], 10);
                    const chunkSize = endPos - start + 1;
                    const beginTime = Date.now();
                    
                    // 读取分片数据
                    const chunkData = await readBody(req, 200 * 1024 * 1024);
                    
                    // 如果 start=0 且文件存在，先删除旧文件和HLS
                    if (start === 0 && fs.existsSync(destPath)) {
                        fs.unlinkSync(destPath);
                        invalidateHls(filename);
                        logLine(`cleaned old file for new upload: ${filename}`);
                    }
                    
                    // 使用稀疏文件方式：在指定偏移位置写入分片
                    // 先扩展文件到 totalSize（稀疏方式，很快）
                    const currentSize = fs.existsSync(destPath) ? fs.statSync(destPath).size : 0;
                    if (currentSize < totalSize) {
                        // 扩展文件（稀疏扩展，很快）
                        const fd = fs.openSync(destPath, fs.existsSync(destPath) ? 'r+' : 'w+');
                        fs.ftruncateSync(fd, totalSize);
                        fs.closeSync(fd);
                    }
                    
                    // 在 start 偏移位置写入分片数据
                    fs.writeFileSync(destPath, chunkData, { flag: 'r+', start: start });
                    
                    const elapsed = Date.now() - beginTime;
                    const speedKBps = Math.round(chunkSize / elapsed);  // KB/s
                    const size = fs.statSync(destPath).size;
                    const pct = Math.round(size / totalSize * 100);
                    logLine(`upload: ${filename} ${size}/${totalSize} (${pct}%) chunk ${chunkSize}B in ${elapsed}ms (${speedKBps}KB/s)`);
                    
                    // 如果上传完成（endPos === totalSize - 1），验证并生成HLS
                    if (endPos === totalSize - 1) {
                        logLine(`upload complete: ${filename} (${size} bytes)`);
                        // 验证文件大小
                        if (size !== totalSize) {
                            logLine(`upload size mismatch: expected ${totalSize}, got ${size}, deleting...`);
                            fs.unlinkSync(destPath);
                            invalidateHls(filename);
                            return sendJson(res, 400, { error: '文件大小不匹配' });
                        }
                        // 验证文件内容（用 ffprobe 快速检查）
                        try {
                            await probeFile(destPath);
                            logLine(`upload verify ok: ${filename}`);
                        } catch (e) {
                            logLine(`upload verify failed: ${filename} - ${e.message}, deleting...`);
                            fs.unlinkSync(destPath);
                            invalidateHls(filename);
                            return sendJson(res, 400, { error: '文件损坏，无法播放' });
                        }
                        generateHls(filename).catch((e) => logLine('hls bg gen failed:', e.message));
                    }
                    
                    return sendJson(res, 200, { ok: true, url: `/obs/${encodeURIComponent(filename)}`, uploaded: endPos + 1, total: totalSize });
                }
            }
            
            // 普通上传：覆盖模式（无 Content-Range）
            const tmpPath = path.join(UPLOAD_DIR, `.simple-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`);
            await new Promise((resolve, reject) => {
                const ws = fs.createWriteStream(tmpPath);
                req.pipe(ws);
                req.on('error', reject);
                ws.on('error', reject);
                ws.on('finish', resolve);
            });
            fs.renameSync(tmpPath, destPath);
            const size = fs.statSync(destPath).size;
            logLine(`simple upload: ${filename} (${size} bytes)`);
            // 验证文件内容
            try {
                await probeFile(destPath);
                logLine(`simple upload verify ok: ${filename}`);
            } catch (e) {
                logLine(`simple upload verify failed: ${filename} - ${e.message}, deleting...`);
                fs.unlinkSync(destPath);
                invalidateHls(filename);
                return sendJson(res, 400, { error: '文件损坏，无法播放' });
            }
            generateHls(filename).catch((e) => logLine('hls bg gen failed:', e.message));
            return sendJson(res, 200, { ok: true, url: `/obs/${encodeURIComponent(filename)}` });
        }

        // ---- file manager page: GET /obs (exact, lists all files)
        if (method === 'GET' && p === '/obs') {
            return sendObsPage(res, url.searchParams.get('sort'));
        }

        // ---- video streaming: GET/HEAD /obs/:filename
        const obsMatch = p.match(/^\/obs\/(.+)$/);
        if ((method === 'GET' || method === 'HEAD') && obsMatch) {
            let filename;
            try { filename = safeName(decodeURIComponent(obsMatch[1])); }
            catch (e) { return sendText(res, 400, 'invalid filename'); }
            if (!filename) return sendText(res, 400, 'invalid filename');
            const filePath = path.join(OBS_DIR, filename);
            if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
                return sendText(res, 404, 'Not Found');
            }
            if (method === 'HEAD') {
                const stat = fs.statSync(filePath);
                res.writeHead(200, {
                    'Content-Length': stat.size,
                    'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
                    'Accept-Ranges': 'bytes'
                });
                return res.end();
            }
            const download = url.searchParams.get('download');
            return streamFileWithRange(res, filePath, req.headers.range, download ? filename : null);
        }

        // ---- video list
        if (method === 'GET' && p === '/videos') {
            return sendJson(res, 200, { videos: listVideoFiles() });
        }

        // ---- delete: DELETE /obs/:filename
        if (method === 'DELETE' && obsMatch) {
            let filename;
            try { filename = safeName(decodeURIComponent(obsMatch[1])); }
            catch (e) { return sendJson(res, 400, { error: 'invalid filename' }); }
            if (!filename) return sendJson(res, 400, { error: 'invalid filename' });
            const filePath = path.join(OBS_DIR, filename);
            if (!fs.existsSync(filePath)) return sendJson(res, 404, { error: 'Not Found' });
            fs.unlinkSync(filePath);
            invalidateHls(filename);
            logLine(`deleted: ${filename}`);
            return sendJson(res, 200, { ok: true });
        }

        // ---- compress: POST /compress/:filename (H.264 faststart transcode)
        const compressMatch = p.match(/^\/compress\/(.+)$/);
        if (method === 'POST' && compressMatch) {
            const filename = safeName(compressMatch[1]);
            if (!filename) return sendJson(res, 400, { error: 'invalid filename' });
            const filePath = path.join(OBS_DIR, filename);
            if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
                return sendJson(res, 404, { error: 'Not Found' });
            }
            logLine(`compress start: ${filename}`);
            const result = await compressVideo(filePath);
            logLine(`compress done: ${filename} ${result.before} -> ${result.after} bytes (${result.savedPct}% saved)`);
            if (!result.skipped) {
                invalidateHls(filename);
                generateHls(filename).catch((e) => logLine('hls regen after compress failed:', e.message));
            }
            return sendJson(res, 200, { ok: true, ...result });
        }

        // ---- HLS playlist (lazy generation): GET/HEAD /hls/:name/index.m3u8
        const hlsM3u8 = p.match(/^\/hls\/(.+)\/index\.m3u8$/);
        if ((method === 'GET' || method === 'HEAD') && hlsM3u8) {
            let name;
            try { name = safeName(decodeURIComponent(hlsM3u8[1])); }
            catch (e) { return sendText(res, 400, 'bad name'); }
            if (!name) return sendText(res, 400, 'invalid name');
            const srcPath = path.join(OBS_DIR, name);
            if (!fs.existsSync(srcPath) || !fs.statSync(srcPath).isFile()) return sendText(res, 404, 'Not Found');
            try {
                await withTimeout(generateHls(name), HLS_TIMEOUT_MS);
            } catch (e) {
                logLine('hls lazy gen:', e.message);
                return sendText(res, 404, 'HLS not ready');
            }
            const m3u8Path = path.join(HLS_DIR, name, 'index.m3u8');
            if (!fs.existsSync(m3u8Path)) return sendText(res, 404, 'HLS not available');
            if (method === 'HEAD') {
                const st = fs.statSync(m3u8Path);
                res.writeHead(200, { 'Content-Length': st.size, 'Content-Type': 'application/vnd.apple.mpegurl' });
                return res.end();
            }
            return streamFileWithRange(res, m3u8Path, req.headers.range);
        }

        // ---- HLS segments: GET/HEAD /hls/:name/seg-NNNNN.ts
        const hlsSeg = p.match(/^\/hls\/(.+)\/(seg-\d+\.ts)$/);
        if ((method === 'GET' || method === 'HEAD') && hlsSeg) {
            let name;
            try { name = safeName(decodeURIComponent(hlsSeg[1])); }
            catch (e) { return sendText(res, 400, 'bad name'); }
            if (!name) return sendText(res, 400, 'invalid name');
            const seg = hlsSeg[2];
            const segPath = path.join(HLS_DIR, name, seg);
            if (!segPath.startsWith(path.join(HLS_DIR, name) + path.sep)) return sendText(res, 400, 'bad path');
            if (!fs.existsSync(segPath) || !fs.statSync(segPath).isFile()) return sendText(res, 404, 'Not Found');
            if (method === 'HEAD') {
                const st = fs.statSync(segPath);
                res.writeHead(200, { 'Content-Length': st.size, 'Content-Type': 'video/mp2t', 'Accept-Ranges': 'bytes' });
                return res.end();
            }
            return streamFileWithRange(res, segPath, req.headers.range);
        }

        // No manual HLS endpoints: HLS generation is fully automatic.
        //   - upload complete / simple upload -> generateHls()
        //   - compress (non-skipped)          -> invalidateHls() + generateHls()
        //   - POST /hls/generate-all          -> manual batch trigger (recommended for first-time setup)
        //   - GET /hls/:name/index.m3u8       -> lazy generation on first hit

        // POST /hls/generate-all: sequential batch generation (one at a time, no concurrency).
        const genAll = (method === 'POST') && (p === '/hls/generate-all');
        if (genAll) {
            const pending = [];
            for (const it of listVideoFiles()) {
                if (!hlsExists(it.name)) pending.push(it.name);
            }
            if (pending.length === 0) {
                logLine(`/hls/generate-all: nothing to do (all ${listVideoFiles().length} videos have HLS`);
                return sendJson(res, 200, { total: listVideoFiles().length, pending: 0, queue: [] });
            }
            logLine(`/hls/generate-all: ${pending.length} videos to generate (sequential)`);
            const done = [], failed = [];
            async function runNext(i) {
                if (i >= pending.length) {
                    logLine(`/hls/generate-all: DONE. ${done.length} ok, ${failed.length} failed`);
                    return;
                }
                const name = pending[i];
                try {
                    await withTimeout(generateHls(name), HLS_TIMEOUT_MS);
                    logLine(`/hls/generate-all [${i+1}/${pending.length}] ${name}: ok`);
                    done.push(name);
                } catch (e) {
                    logLine(`/hls/generate-all [${i+1}/${pending.length}] ${name}: FAILED: ${e.message}`);
                    failed.push(name);
                }
                await runNext(i + 1);  // sequential
            }
            runNext(0);  // fire-and-forget
            return sendJson(res, 200, { total: listVideoFiles().length, pending: pending.length, queue: pending });
        }

        // ---- static frontend from public/
        if (method === 'GET' || method === 'HEAD') {
            let rel = p === '/' ? '/index.html' : p;
            const filePath = path.join(PUBLIC_DIR, path.normalize(rel).replace(/^([/\\])+/, ''));
            if (filePath.startsWith(PUBLIC_DIR) && fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
                const mime = MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
                // no-cache: always revalidate so frontend fixes (e.g. app.js) reach users promptly
                res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
                if (method === 'HEAD') return res.end();
                return fs.createReadStream(filePath).pipe(res);
            }
        }

        return sendText(res, 404, 'Not Found');
    } catch (err) {
        logLine('error:', err.message);
        if (!res.headersSent) return sendJson(res, 500, { error: err.message });
        res.end();
    }
});

// Ensure dirs exist
fs.mkdirSync(OBS_DIR, { recursive: true });
fs.mkdirSync(UPLOAD_DIR, { recursive: true });
fs.mkdirSync(PUBLIC_DIR, { recursive: true });
fs.mkdirSync(HLS_DIR, { recursive: true });
if (fs.existsSync(HLS_DIR)) {
    for (const f of fs.readdirSync(HLS_DIR)) {
        if (f.startsWith('.tmp-')) fs.rmSync(path.join(HLS_DIR, f), { recursive: true, force: true });
    }
}
// HLS used to live at obs/.hls; remove the legacy dir now that it is a
// separate top-level folder (only if it is empty — never delete user files).
const LEGACY_HLS = path.join(OBS_DIR, '.hls');
if (fs.existsSync(LEGACY_HLS)) {
    try {
        if (fs.readdirSync(LEGACY_HLS).length === 0) fs.rmdirSync(LEGACY_HLS);
    } catch (e) { /* ignore */ }
}

server.listen(PORT, '0.0.0.0', () => {
    logLine(`OBS web app running on port ${PORT} (obs dir: ${OBS_DIR})`);
});

// Startup sweep: every asset gets the current feature automatically. Any video
// without a current-version HLS (new upload, generation version bump, source
// changed, or HLS dir deleted) is queued for background generation — no manual
// "转HLS" button anywhere.
//
// STARTUP SWEEP DISABLED — 5 concurrent ffmpeg overwhelms slow storage (e.g. network mounts).
// Use POST /hls/generate-all to trigger manually, or rely on lazy generation on GET /hls/:name/index.m3u8.
// setImmediate(() => {
//     for (const it of listVideoFiles()) {
//         if (!hlsExists(it.name)) {
//             generateHls(it.name).catch((e) => logLine('hls startup gen failed:', e.message));
//         }
//     }
// });

// Daily cron at UTC+8 05:00: re-scan obs/ and generate HLS for any video that
// doesn't have a current-version HLS. Implemented in-process because the
// container doesn't ship crontab (and even if it did, an in-process timer
// survives restarts since user_start.sh re-launches server.js). One shot per
// day, fired once.
const CRON_TZ = 'Asia/Shanghai';   // UTC+8, no DST
const CRON_HOUR = 5;
const CRON_MIN  = 0;
let lastCronKey = '';              // 'YYYY-MM-DD' in CRON_TZ of the last fire
function partsInTz(date, tz) {
    // {y, m, d, h, mi} in the given IANA timezone, no global TZ mutation.
    const fmt = new Intl.DateTimeFormat('en-CA', {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
    });
    const parts = Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, p.value]));
    return {
        y: Number(parts.year), m: Number(parts.month), d: Number(parts.day),
        h: Number(parts.hour === '24' ? '0' : parts.hour),
        mi: Number(parts.minute),
    };
}
function dailyCronTick() {
    const p = partsInTz(new Date(), CRON_TZ);
    if (p.h !== CRON_HOUR || p.mi !== CRON_MIN) return;
    const key = `${p.y}-${p.m}-${p.d}`;
    if (key === lastCronKey) return;
    lastCronKey = key;
    logLine(`daily ${CRON_TZ} ${CRON_HOUR}:${String(CRON_MIN).padStart(2, '0')} cron: scanning obs/ for missing HLS (${key})`);
    let queued = 0;
    for (const it of listVideoFiles()) {
        if (!hlsExists(it.name)) {
            generateHls(it.name).catch((e) => logLine('hls cron gen failed:', e.message));
            queued++;
        }
    }
    logLine(`daily cron: queued ${queued} video(s) for HLS generation`);
}
// 30 s granularity is enough (cron fires within the first minute of the hour).
setInterval(dailyCronTick, 30 * 1000).unref();
