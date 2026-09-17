(function () {
    'use strict';

    const CHUNK_SIZE = 2 * 1024 * 1024;
    // FEED_COPIES: ghost copies for infinite scroll illusion (3 = [ghost | real | ghost])
    const FEED_COPIES = 3;
    // WINDOW_SIZE: 固定 5 格播放窗口（前 2 + 当前 + 后 2），滑动后空缺随机填充
// 索引：0,1 = 前 2 个；2 = 当前播放；3,4 = 后 2 个
    const WINDOW_SIZE = 5;

    // ---------------------------------------------------------------- DOM refs
    const viewport = document.getElementById('viewport');
    const pagesEl = document.getElementById('pages');
    const feeds = [
        document.getElementById('feed0'),
        document.getElementById('mainFeed'),
        document.getElementById('feed2')
    ];
    const pageDot = document.getElementById('pageDot');
    const videoContainer = document.getElementById('videoContainer');
    const videoLabel = document.getElementById('videoLabel');
    const videoOverlayBtns = document.getElementById('videoOverlayBtns');
    const overlayCompress = document.getElementById('overlayCompress');
    const overlayDelete = document.getElementById('overlayDelete');
    const edgeHintLeft = document.getElementById('edgeHintLeft');
    const edgeHintRight = document.getElementById('edgeHintRight');
    const infoName = document.getElementById('infoName');
    const infoSize = document.getElementById('infoSize');
    const infoTime = document.getElementById('infoTime');
    const infoProgress = document.getElementById('infoProgress');
    const infoIndex = document.getElementById('infoIndex');
    const videoCount = document.getElementById('videoCount');
    const randomSwitch = document.getElementById('randomSwitch');
    const autoplaySwitch = document.getElementById('autoplaySwitch');
    const speedOptions = document.getElementById('speedOptions');
    const seekTrack = document.getElementById('seekTrack');
    const seekFill = document.getElementById('seekFill');
    const seekThumb = document.getElementById('seekThumb');
    const seekLabel = document.getElementById('seekLabel');
    const playlistStrip = document.getElementById('playlistStrip');
    const uploadModal = document.getElementById('uploadModal');
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const progressArea = document.getElementById('progressArea');
    const progressTitle = document.getElementById('progressTitle');
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const cancelBtn = document.getElementById('cancelBtn');
    const fabUpload = document.getElementById('fabUpload');

    // ---------------------------------------------------------------- state
    let videos = [];
    let activeIndex = 0;
    // playlistWindow: 固定5格「播放信息」队列（索引2=当前播放，0-1=前2，3-4=后2）
    // 每个元素为 {uuid, t} —— uuid 指向 videos[] 中的 name，t 为该视频当前播放进度（秒）
    // 上滑：每个位置从右侧邻居接收 (uuid, t)，最末格补随机 (uuid, t)
    // 下滑：每个位置从左侧邻居接收 (uuid, t)，最首格补随机 (uuid, t)
    let playlistWindow = [];
    let currentPage = 1;      // 0=info, 1=main, 2=settings
    let playing = true;
    let random = true;
    let autoplay = true;
    let playbackSpeed = 1;
    let currentAbort = null;
    let longPressTimer = null;
    let longPressMoved = false;
    const positions = new Map();  // 内存缓存，刷新丢失
    const POS_KEY = 'obs-play-pos-v1';  // localStorage key

    // 从 localStorage 恢复进度（容器存续内持久化）
    function localStorage2positions() {
        try {
            const raw = localStorage.getItem(POS_KEY);
            if (!raw) return;
            const obj = JSON.parse(raw);
            if (obj && typeof obj === 'object') {
                for (const [k, v] of Object.entries(obj)) positions.set(k, v);
            }
        } catch (_) {}
    }

    // 进度写入 localStorage
    function positions2localStorage() {
        try {
            const obj = Object.fromEntries(positions.entries());
            localStorage.setItem(POS_KEY, JSON.stringify(obj));
        } catch (_) {}
    }

    // ──────────────────────────── playlist window（5格播放信息队列）────────────────────────
    // playlistWindow = 固定 5 格 (uuid, t) 队列。
    // 索引 0,1 = 前 2 个；索引 2 = 当前播放；索引 3,4 = 后 2 个。
    // 上滑切下一个（shiftWindow(+1)）：位置 i ← 位置 i+1，索引 0 pop 掉，索引 4 补新随机
    // 下滑切上一个（shiftWindow(-1)）：位置 i ← 位置 i-1，索引 4 pop 掉，索引 0 补新随机
    // 随机补位：从 videos 随机选一个 uuid + 随机进度 t（0..duration）
    // 进度 t 的含义：该位置视频当时播放到的时间点；切换后会 seek 到该 t

    let _fillPool = [];  // 当前随机候选池（videos 数组）

    // 随机生成一个 (uuid, t) —— uuid 随机，t 在 duration 范围内随机
    function _makeRandomEntry() {
        if (videos.length === 0) return null;
        if (_fillPool.length === 0) {
            _fillPool = videos.slice();
            _shufflePool(_fillPool);
        }
        const v = _fillPool.pop();
        return { uuid: v ? v.name : null, t: _timeFor(v) };
    }

    // 进度 t：仅用于"刚创建一个新 entry"的初始值。
    // entry.t 一旦写下去就跟这个 entry 绑定，不能被 positions 覆盖。
    // 原因：同一视频可能在 5 格中以多个 entry 出现，每个 entry 各自有独立的播放历史；
    // 共享 positions 会让所有 entry 退化成同一个 t。
    function _timeFor(v) {
        if (!v) return 0;
        // 新 entry 永远从 0 ~ duration 随机初始化
        const dur = (Number.isFinite(v.duration) && v.duration > 0) ? v.duration : 60;
        return Math.max(0, Math.random() * Math.max(1, dur - 0.5));
    }

    // 取一个随机 uuid 的视频对象（不消耗 _fillPool）
    function _randomVideo() {
        if (videos.length === 0) return null;
        return videos[Math.floor(Math.random() * videos.length)];
    }

    function _shufflePool(pool) {
        for (let i = pool.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [pool[i], pool[j]] = [pool[j], pool[i]];
        }
        return pool;
    }

    function _makeFillPool() {
        // 候选池：videos 全集；_makeRandomEntry 自己从池中 pop（耗尽自动重新洗）
        _fillPool = videos.slice();
        _shufflePool(_fillPool);
    }

    function _nextFill() {
        if (_fillPool.length === 0) _makeFillPool();
        return _fillPool.pop() || null;
    }

    // 初始化队列：5 格 (uuid, t) 全部随机填充；中央格也是随机的（不强插 videos[activeIndex]）
    // 之后再用 playlistWindow[2].uuid 反查得到 activeIndex（见 _syncActiveIndexFromWindow）
    function initPlaylistWindow() {
        if (videos.length === 0) { playlistWindow = []; _fillPool = []; syncAppState(); return; }
        const w = new Array(WINDOW_SIZE);
        for (let i = 0; i < WINDOW_SIZE; i++) w[i] = _makeRandomEntry();
        playlistWindow = w;
        _syncActiveIndexFromWindow();
        syncAppState();
    }

    // 上滑(+1)/下滑(-1)切换到下一个/上一个视频（5 格队列）
    //
    // 索引布局：0,1 = 前 2 个；2 = 当前；3,4 = 后 2 个。
    //
    // 上滑（切下一个）：
    //   0 pop 掉，1 → 0，2 → 1，3 → 2，4 → 3，索引 4 补新随机 (uuid, t)
    //   中央 (索引 2) = 旧的 playlistWindow[3]（原样平移）—— 不重新选视频
    //
    // 下滑（切上一个）：
    //   4 pop 掉，3 → 4，2 → 3，1 → 2，0 → 1，索引 0 补新随机 (uuid, t)
    //   中央 (索引 2) = 旧的 playlistWindow[1]（原样平移）
    //
    // 中央 entry 是 playlistWindow 内部"原样平移"得到的，不再被 videos[activeIndex] 覆盖。
    // activeIndex 由 playlistWindow[2].uuid 反查得到（见 _activeIndexFromWindow）。
    function shiftWindow(delta) {
        _shiftWindowBySteps(delta > 0 ? 1 : (delta < 0 ? -1 : 0));
    }

    // 由 playlistWindow[2].uuid 反查 videos 数组里的索引，写入 activeIndex
    function _syncActiveIndexFromWindow() {
        if (videos.length === 0) return;
        const curUuid = playlistWindow[2] && playlistWindow[2].uuid;
        if (!curUuid) return;
        const idx = videos.findIndex(v => v.name === curUuid);
        if (idx >= 0) activeIndex = idx;
    }

    // 把 playlistWindow 向指定方向平移 steps 步（steps>0 上滑 / steps<0 下滑）
    // 每一步都按 shiftWindow 的规则：单端 pop + 单端补新随机 entry。
    // 中央 entry 永远是 playlistWindow 内部"原样平移"得到的，不重新选视频。
    function _shiftWindowBySteps(steps) {
        if (!steps || videos.length === 0) return;
        if (!playlistWindow || playlistWindow.length !== WINDOW_SIZE) {
            initPlaylistWindow();
            return;
        }
        // 在 shift 之前先把 video.currentTime 写回当前中央 entry 的 t
        // （切走后旧 playlistWindow[2] 会变成 playlistWindow[1]，其 t 必须保留最后播放时刻）
        if (isFinite(video.currentTime) && video.currentTime > 0.5
            && playlistWindow[2] && typeof playlistWindow[2] === 'object') {
            playlistWindow[2].t = video.currentTime;
        }
        if (steps > 0) {
            // 上滑 steps 次：每次都 0 pop、4 补新随机；连续执行 steps 次即可
            for (let s = 0; s < steps; s++) {
                const next = new Array(WINDOW_SIZE);
                for (let i = 0; i < WINDOW_SIZE - 1; i++) next[i] = playlistWindow[i + 1];
                next[WINDOW_SIZE - 1] = _makeRandomEntry();
                playlistWindow = next;
            }
        } else {
            const k = -steps;
            // 下滑 k 次：每次都 4 pop、0 补新随机
            for (let s = 0; s < k; s++) {
                const next = new Array(WINDOW_SIZE);
                for (let i = 1; i < WINDOW_SIZE; i++) next[i] = playlistWindow[i - 1];
                next[0] = _makeRandomEntry();
                playlistWindow = next;
            }
        }
        _syncActiveIndexFromWindow();
        syncAppState();
    }

    // 重建窗口：5 格全部随机填充（与 initPlaylistWindow 等价，targetIdx 参数不再使用）
    // 保留仅为兼容历史调用；播放列表顺序由 playlistWindow 自身维护，videos 数组的索引不再是切换依据
    function setPlaylistWindow(targetIdx) {
        initPlaylistWindow();
    }

    function syncAppState() {
        window._appState = {
            window: playlistWindow.map(v => v ? { uuid: v.uuid, t: v.t } : null),
            windowSize: playlistWindow.length,
        };
        window._allVideoNames = videos.map(v => v.name);
        window._playlistWindow = playlistWindow;
        window._activeIndex = activeIndex;
    }

    // ---------------------------------------------------------------- single video element (ONE instance only)
    const video = document.createElement('video');
    video.muted = false;
    video.loop = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.setAttribute('playsinline', '');
    video.setAttribute('webkit-playsinline', '');
    video.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;';
    videoContainer.appendChild(video);

    // ---------------------------------------------------------------- HLS
    const _probe = document.createElement('video');
    const _canNativeHls = !!(_probe.canPlayType && _probe.canPlayType('application/vnd.apple.mpegurl'));
    const HAS_HLSJS = !!(window.Hls && window.Hls.isSupported && window.Hls.isSupported());
    const NATIVE_HLS = _canNativeHls && /^((?!chrome|android|crios|fxios|edg).)*safari/i.test(navigator.userAgent);
    function hlsCapable(v) { return !!(v && v.hls && v.hlsReady) && (NATIVE_HLS || HAS_HLSJS); }
    let hlsInstance = null;

    const EMPTY_HTML =
        '<div class="empty-state">' +
        '<div class="empty-icon">🎬</div>' +
        '<p>还没有视频</p>' +
        '<p class="sub">长按空白处上传</p>' +
        '</div>';

    // ---------------------------------------------------------------- utils
    function fmtSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
        return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    }
    function formatTime(t) {
        if (!t) return '-';
        return new Date(t).toLocaleString('zh-CN', { hour12: false });
    }
    // 秒数 -> 播放进度文案 (mm:ss / h:mm:ss)，供 seekLabel 使用
    function fmtClock(s) {
        if (!isFinite(s) || s < 0) s = 0;
        const total = Math.floor(s);
        const hh = Math.floor(total / 3600);
        const mm = Math.floor((total % 3600) / 60);
        const ss = total % 60;
        const pad = (x) => String(x).padStart(2, '0');
        return hh > 0 ? hh + ':' + pad(mm) + ':' + pad(ss) : mm + ':' + pad(ss);
    }
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }
    function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

    // Pure-JS SHA-256
    const SHA256_K = [
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
    ];
    const SHA256_H0 = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];

    function createSha256() {
        const rotr = (x,n) => (x>>>n)|(x<<(32-n));
        const h = SHA256_H0.slice(); let buf = new Uint8Array(0), totalLen = 0;
        function compress(pad, off) {
            const w = new Uint32Array(64);
            for(let j=0;j<16;j++){const o=off+j*4;w[j]=(pad[o]<<24)|(pad[o+1]<<16)|(pad[o+2]<<8)|pad[o+3];}
            for(let j=16;j<64;j++){
                const s0=rotr(w[j-15],7)^rotr(w[j-15],18)^(w[j-15]>>>3);
                const s1=rotr(w[j-2],17)^rotr(w[j-2],19)^(w[j-2]>>>10);
                w[j]=(w[j-16]+s0+w[j-7]+s1)|0;
            }
            let a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
            for(let j=0;j<64;j++){
                const S1=rotr(e,6)^rotr(e,11)^rotr(e,25);
                const ch=(e&f)^(~e&g);
                const t1=(hh+S1+ch+SHA256_K[j]+w[j])|0;
                const S0=rotr(a,2)^rotr(a,13)^rotr(a,22);
                const maj=(a&b)^(a&c)^(b&c);
                const t2=(S0+maj)|0;
                hh=g;g=f;f=e;e=(d+t1)|0;d=c;c=b;b=a;a=(t1+t2)|0;
            }
            h[0]=(h[0]+a)|0;h[1]=(h[1]+b)|0;h[2]=(h[2]+c)|0;h[3]=(h[3]+d)|0;
            h[4]=(h[4]+e)|0;h[5]=(h[5]+f)|0;h[6]=(h[6]+g)|0;h[7]=(h[7]+hh)|0;
        }
        return {
            update(chunk){
                totalLen+=chunk.length;
                if(buf.length>0){const c2=new Uint8Array(buf.length+chunk.length);c2.set(buf);c2.set(chunk,buf.length);buf=c2;}
                else buf=chunk;
                const full=Math.floor(buf.length/64)*64;
                for(let i=0;i<full;i+=64)compress(buf,i);
                buf=buf.slice(full);
            },
            digestHex(){
                const bitLen=totalLen*8,hi=Math.floor(bitLen/0x100000000),lo=bitLen>>>0;
                const rem=buf.length,padLen=rem<56?64-rem:128-rem;
                const pd=new Uint8Array(rem+padLen);pd.set(buf);pd[rem]=0x80;
                pd[pd.length-8]=(hi>>>24)&0xff;pd[pd.length-7]=(hi>>>16)&0xff;pd[pd.length-6]=(hi>>>8)&0xff;pd[pd.length-5]=hi&0xff;
                pd[pd.length-4]=(lo>>>24)&0xff;pd[pd.length-3]=(lo>>>16)&0xff;pd[pd.length-2]=(lo>>>8)&0xff;pd[pd.length-1]=lo&0xff;
                for(let i=0;i<pd.length;i+=64)compress(pd,i);
                let hex='';
                for(let i=0;i<8;i++)hex+=(h[i]>>>28&0xf).toString(16)+(h[i]>>>24&0xf).toString(16)+(h[i]>>>20&0xf).toString(16)+(h[i]>>>16&0xf).toString(16)+(h[i]>>>12&0xf).toString(16)+(h[i]>>>8&0xf).toString(16)+(h[i]>>>4&0xf).toString(16)+(h[i]&0xf).toString(16);
                return hex;
            }
        };
    }

    async function sha256(buffer) {
        if (crypto.subtle) { try { const d = await crypto.subtle.digest('SHA-256', buffer); return Array.from(new Uint8Array(d)).map(b => b.toString(16).padStart(2,'0')).join(''); } catch(e) {} }
        const ctx = createSha256(); ctx.update(new Uint8Array(buffer)); return ctx.digestHex();
    }

    async function jsonFetch(url, opts) {
        const res = await fetch(url, opts);
        const text = await res.text();
        let data; try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
        if (!res.ok) throw new Error(data.error || data.raw || ('HTTP ' + res.status));
        return data;
    }

    function shuffle(arr) {
        for (let i = arr.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [arr[i], arr[j]] = [arr[j], arr[i]];
        }
        return arr;
    }

    // ---------------------------------------------------------------- page navigation
    function setPage(n) {
        n = Math.max(0, Math.min(2, n));
        if (n === currentPage) return;
        recordActivePosition();
        currentPage = n;
        viewport.dataset.page = n;
        pagesEl.style.setProperty('--page', n);
        buildPageDots();
        applyPagePlayback();
    }

    // ---------------------------------------------------------------- playback effect per page
    let effectTimer = null;

    function applyPagePlayback() {
        clearInterval(effectTimer);
        if (videos.length === 0) return;

        if (currentPage === 0) {
            // Info page: 5x rewind via manual seek
            video.playbackRate = 1;
            effectTimer = setInterval(() => {
                if (currentPage !== 0 || videos.length === 0) { clearInterval(effectTimer); return; }
                video.currentTime = Math.max(0, video.currentTime - 0.5);
                if (video.currentTime <= 0) video.pause();
            }, 100);
        } else if (currentPage === 2) {
            // 第三页（设置页）：默认 2x 播放，长按 5x 覆盖
            video.playbackRate = fastSpeed ? 5 : 2;
        } else {
            // 第二页（主视频页）：默认 1x 播放，长按 5x 覆盖
            video.playbackRate = fastSpeed ? 5 : 1;
        }
    }

    // ---------------------------------------------------------------- render feed (placeholder items only — NO video elements)
    function renderFeeds() {
        feeds.forEach(f => { f.innerHTML = ''; });

        // 从 localStorage 恢复进度（容器存续内持久化，刷新页面后仍生效）
        localStorage2positions();

        if (videos.length === 0) {
            feeds.forEach(f => { f.innerHTML = EMPTY_HTML; });
            videoLabel.textContent = '';
            video.src = '';
            destroyHls();
            updateInfo();
            playlistWindow = [];
            if (playlistStrip) playlistStrip.innerHTML = '';
            syncAppState();
            return;
        }

        initPlaylistWindow();  // 5 格 (uuid, t) 全部随机；activeIndex 由 playlistWindow[2].uuid 反查
        loadVideoForIndex(2);  // 中间格

        feeds.forEach(feed => {
            for (let c = 0; c < FEED_COPIES; c++) {
                playlistWindow.forEach((entry) => {
                    const item = document.createElement('div');
                    item.className = 'video-item';
                    item._vid = entry ? entry.uuid : null;  // video name for lookup
                    feed.appendChild(item);
                });
            }
        });

        feeds.forEach(f => scrollToIndex(f, 2));
        buildPageDots();
        loadVideoForIndex(2);
        renderPlaylistStrip();  // 设置页上方播放列表条
        updateInfo();
        feedReady = true;  // 初始化完成，启用 scroll handler
    }

    // ---------------------------------------------------------------- feed scroll
    // feed 渲染 playlistWindow × 3 副本（共 15 格）。中间副本的当前视频（窗口索引 2）
    // 始终在 scrollTop = playlistWindow.length * h = 5 * h（初始定位）。
    // 后续 scroll 由 vertAnimateTo 接管，applyIndex 不再调用 scrollToIndex。
    const MIDDLE_CURRENT_TOP = WINDOW_SIZE;
    function scrollToIndex(feed, idx) {
        if (videos.length === 0) { feed.scrollTop = 0; return; }
        feed._progScrollUntil = Date.now() + 60;
        feed.scrollTo({ top: MIDDLE_CURRENT_TOP * feed.clientHeight, behavior: 'instant' });
    }

    let feedReady = false;  // false=初始化期间，跳过 scroll handler 的 applyIndex

    feeds.forEach(feed => {
        feed.addEventListener('scroll', () => {
            if (videos.length === 0 || Date.now() < (feed._progScrollUntil || 0)) return;
            clearTimeout(feed._scrollTimer);
            feed._scrollTimer = setTimeout(() => {
                if (!feedReady) return;  // 初始化期间不响应
                const h = Math.max(1, feed.clientHeight);
                const n = playlistWindow.length;
                let vis = Math.round(feed.scrollTop / h);
                // 无限滚动副本跳转：只修正 scrollTop，不重建窗口（窗口重建由 vertAnimateTo 处理）
                if (vis < n) { feed._progScrollUntil = Date.now() + 60; feed.scrollTo({ top: (vis + n) * h, behavior: 'instant' }); return; }
                if (vis >= 2 * n) { feed._progScrollUntil = Date.now() + 60; feed.scrollTo({ top: (vis - n) * h, behavior: 'instant' }); return; }
            }, 120);
        });
    });

    function applyIndex(delta) {
        // delta: +1 切到下一个视频（按 playlistWindow 上滑 1 次：playlistWindow[3] 落到中央）
        // delta: -1 切到上一个视频（按 playlistWindow 下滑 1 次：playlistWindow[1] 落到中央）
        // 中央 entry 是 playlistWindow 内部"原样平移"得到的，不再依赖 videos 数组的 activeIndex。
        if (!delta || videos.length === 0) return;
        recordActivePosition();
        shiftWindow(delta);
        playing = autoplay;
        // 翻页动画（300ms）：旧视频翻出 → 切换 src → 新视频从另一侧翻入。
        // 视频源切换由 _videoFlipPage 内部负责（80ms 后），不要在这里预加载。
        _videoFlipPage(delta);
        updateInfo();
        updatePlayback();
        // 5 格重新平移 + 单端补新随机 → 必须重渲染 strip，不能只调 updatePlaylistStripActive
        // （后者只更新 active 状态和时间/进度，不重建 DOM）
        renderPlaylistStrip();
    }

    // 把 playlistWindow（5 格 (uuid, t)）渲染到设置页上方的播放列表条
    // 每格 = 一个队列位置：uuid = 该位置的视频名，t = 该视频被播到的时刻
    // 中央格（playlistWindow[2]）= 当前播放；0,1 = 前 2；3,4 = 后 2
    // 视觉上呈现"完整播放队列视图"，每个条目都展示自己的播放信息
    // （文件名、已播时长 mm:ss、总时长 mm:ss、进度条、状态徽标）
    function renderPlaylistStrip() {
        if (!playlistStrip) return;
        playlistStrip.innerHTML = '';
        if (!playlistWindow || playlistWindow.length === 0) return;

        // 用 name -> video 对象 建索引，用于查 duration / size 等元信息
        const videoByName = new Map();
        for (const v of videos) videoByName.set(v.name, v);

        playlistWindow.forEach((entry, idx) => {
            const item = document.createElement('div');
            const isActive = idx === 2;
            item.className = 'pl-item' + (isActive ? ' active' : '');
            item.dataset.widx = String(idx);
            // 位置标签：前 2 / 当前 / 后 2
            const posLabel = idx < 2 ? '前 ' + (2 - idx) : idx === 2 ? '当前' : '后 ' + (idx - 2);

            const v = entry && entry.uuid ? videoByName.get(entry.uuid) : null;
            const nameStr = entry && entry.uuid ? entry.uuid : '(空)';
            // 当前播放进度（中央格走实时 video.currentTime；其他格用 playlistWindow[i].t）
            const liveT = (isActive && isFinite(video.currentTime)) ? video.currentTime : 0;
            const t = isActive ? liveT : (entry && typeof entry.t === 'number' && isFinite(entry.t)) ? entry.t : 0;
            const dur = v && Number.isFinite(v.duration) && v.duration > 0 ? v.duration : 0;
            const pct = dur > 0 ? Math.max(0, Math.min(100, (t / dur) * 100)) : 0;

            // 状态：未播（t 几乎为 0）/ 已看到 X / 播放中
            let statusText, statusClass;
            if (isActive) {
                statusText = '播放中'; statusClass = 'pl-status-playing';
            } else if (t <= 0.5) {
                statusText = '未播'; statusClass = 'pl-status-unwatched';
            } else {
                statusText = '看到 ' + fmtClock(t); statusClass = 'pl-status-watched';
            }

            const timeText = dur > 0
                ? fmtClock(t) + ' / ' + fmtClock(dur)
                : fmtClock(t) + ' / --:--';

            item.title = nameStr + '\n' + timeText + (dur > 0 ? '  (' + Math.round(pct) + '%)' : '') + '\n' + statusText;
            item.innerHTML =
                '<div class="pl-head">' +
                    '<span class="pl-pos">' + posLabel + '</span>' +
                    '<span class="pl-status ' + statusClass + '">' + escapeHtml(statusText) + '</span>' +
                '</div>' +
                '<div class="pl-row">' +
                    '<span class="pl-name">' + escapeHtml(nameStr) + '</span>' +
                    '<span class="pl-time">' + escapeHtml(timeText) + '</span>' +
                '</div>' +
                '<div class="pl-bar"><div class="pl-bar-fill" style="width:' + pct.toFixed(1) + '%"></div></div>';

            item.addEventListener('click', () => {
                const targetIdx = Number(item.dataset.widx);
                if (targetIdx === 2) return;  // 点击当前格不做任何事
                // 用户点击的是 playlistWindow 中的某个 entry —— 按 shift 算法把它"平移"到中央。
                // 中央 entry 是 playlistWindow 内部"原样平移"得到的，不重新选视频、不复制 t：
                //   点 idx=3 → 上滑 1 次（playlistWindow[3] 原样落到 playlistWindow[2]，旧 playlistWindow[0] 被 pop 掉）
                //   点 idx=4 → 上滑 2 次
                //   点 idx=1 → 下滑 1 次
                //   点 idx=0 → 下滑 2 次
                // 远端补新随机 entry：idx=4 时 playlistWindow[4] 也要补新一次（idx=0 时 playlistWindow[0] 也要补新一次）
                const steps = targetIdx - 2;
                recordActivePosition();
                _shiftWindowBySteps(steps);
                // 翻页动画：steps ≠ 0 时按方向翻一次（即便 |steps|>1 也只翻 1 张卡片，
                // 用户能从 playlistWindow 列表里看到 playlistWindow[2] 的变化幅度）
                if (steps !== 0) _videoFlipPage(steps);
                else updateInfo();
                updatePlayback();
                renderPlaylistStrip();
            });
            playlistStrip.appendChild(item);
        });
        scrollActiveStripIntoView();
    }

    // 切换高亮 + 平滑滚到中间（让中央格始终在可视范围内）
    function updatePlaylistStripActive() {
        if (!playlistStrip) return;
        const items = playlistStrip.querySelectorAll('.pl-item');
        // 用 name -> video 对象 建索引，用于查 duration
        const videoByName = new Map();
        for (const v of videos) videoByName.set(v.name, v);

        items.forEach((it, i) => {
            if (i === 2) it.classList.add('active');
            else it.classList.remove('active');
            // 同时把 playlistWindow 自身的最新 t 同步回条目（中央格每 500ms 实时 t 已更新）
            const entry = playlistWindow[i];
            if (!entry) return;
            const v = entry.uuid ? videoByName.get(entry.uuid) : null;
            const dur = v && Number.isFinite(v.duration) && v.duration > 0 ? v.duration : 0;
            // 中央格：实时 video.currentTime；其他格：playlistWindow[i].t
            const t = (i === 2 && isFinite(video.currentTime))
                ? video.currentTime
                : (typeof entry.t === 'number' && isFinite(entry.t) && entry.t > 0) ? entry.t : 0;
            const pct = dur > 0 ? Math.max(0, Math.min(100, (t / dur) * 100)) : 0;

            const tEl = it.querySelector('.pl-time');
            if (tEl) {
                const timeText = dur > 0
                    ? fmtClock(t) + ' / ' + fmtClock(dur)
                    : fmtClock(t) + ' / --:--';
                tEl.textContent = timeText;
            }
            const barEl = it.querySelector('.pl-bar-fill');
            if (barEl) barEl.style.width = pct.toFixed(1) + '%';

            // 中央格的状态徽标始终是"播放中"，其他格根据 t 重新评估
            const statusEl = it.querySelector('.pl-status');
            if (statusEl && i !== 2) {
                if (t <= 0.5) {
                    statusEl.textContent = '未播';
                    statusEl.className = 'pl-status pl-status-unwatched';
                } else {
                    statusEl.textContent = '看到 ' + fmtClock(t);
                    statusEl.className = 'pl-status pl-status-watched';
                }
            }
        });
        scrollActiveStripIntoView();
    }

    function scrollActiveStripIntoView() {
        if (!playlistStrip) return;
        const active = playlistStrip.querySelector('.pl-item.active');
        if (!active) return;
        const stripRect = playlistStrip.getBoundingClientRect();
        const itemRect = active.getBoundingClientRect();
        // 把 .active 滚到 strip 竖直中间（竖向布局后中央格是 playlistWindow[2]）
        const offset = (itemRect.top - stripRect.top) - (stripRect.height / 2 - itemRect.height / 2);
        playlistStrip.scrollBy({ top: offset, behavior: 'smooth' });
    }

    // 切源时翻页动画（仅动 video 元素一次，300ms）：
    //   - delta > 0（上滑切下一个）：旧视频 translateY(-100%) + opacity 0 翻出，
    //     新视频从 translateY(100%) 翻入到 0
    //   - delta < 0（下滑切上一个）：旧视频 translateY(100%) + opacity 0 翻出，
    //     新视频从 translateY(-100%) 翻入到 0
    // 翻页动画等价于"翻了一张卡片"——用户能从单次翻页清楚看到"翻了一页"。
    // 连击 N 次 = 翻 N 张卡片；队列机制让连击动画连贯（不会中断正在进行的那一次）。
    let _flipNextTid = null;
    let _flipDoneTid = null;
    let _flipQueue = [];
    function _videoFlipPage(delta) {
        if (!delta) return;
        // 累积到队列：每次切源都入队，调度器逐个执行
        _flipQueue.push(delta);
        if (_flipNextTid) return;  // 已有动画在跑，新入队的等下一次启动
        _flipNextTid = setTimeout(() => { _flipNextTid = null; _flipPageStep(); }, 0);
    }

    // 执行一次完整的翻页动画（300ms）
    function _flipPageStep() {
        const delta = _flipQueue.shift();
        if (!delta) return;

        const dir = delta > 0 ? 'up' : 'down';
        // 1) 旧视频翻出（video 元素本身 translateY 翻出 + opacity 0）
        videoContainer.classList.remove('is-flip-up', 'is-flip-down', 'is-flip-pre-up', 'is-flip-pre-down', 'is-flipping-in');
        videoContainer.classList.add('is-flip-' + dir);

        // 2) 视频源切换在 80ms 后触发（旧视频已开始翻出，视觉上看起来是同一张翻页）
        setTimeout(() => {
            loadVideoForIndex(2);
            // 3) 切到新视频后立即把 videoContainer 放到另一侧（屏幕外），下一帧翻入
            videoContainer.classList.remove('is-flip-up', 'is-flip-down');
            videoContainer.classList.add('is-flip-pre-' + dir);
            requestAnimationFrame(() => requestAnimationFrame(() => {
                videoContainer.classList.remove('is-flip-pre-up', 'is-flip-pre-down');
                videoContainer.classList.add('is-flipping-in');
            }));
        }, 80);

        // 4) 收尾（380ms）：清理动画 class；继续队列里下一个
        _flipDoneTid = setTimeout(() => {
            videoContainer.classList.remove('is-flip-up', 'is-flip-down', 'is-flip-pre-up', 'is-flip-pre-down', 'is-flipping-in');
            _flipDoneTid = null;
            if (_flipQueue.length > 0) {
                // 队列里还有，继续翻下一页
                _flipPageStep();
            }
        }, 380);
    }

    // ---------------------------------------------------------------- single video player
    function loadVideoForIndex(idx) {
        if (videos.length === 0) return;
        // 当前视频来自 playlistWindow 中间格（索引 2）的 (uuid, t)
        const entry = playlistWindow[2];
        if (!entry || !entry.uuid) return;
        const v = videos.find(x => x.name === entry.uuid) || videos[idx];
        if (!v) return;

        destroyHls();

        // Restore position: 严格取 playlistWindow entry 自己的 t（不再读 positions）
        // 因为 entry.t 是 entry 自身的属性，不能跨 entry 共享
        const seekT = (typeof entry.t === 'number' && isFinite(entry.t) && entry.t > 0)
            ? entry.t : 0;
        if (seekT > 0) {
            video._pendingSeek = seekT;
        }

        if (v.hls && v.hlsReady && hlsCapable(v)) {
            video.src = '';
            if (video.children.length) video.innerHTML = '';
            const sHls = document.createElement('source');
            sHls.src = v.hls; sHls.type = 'application/vnd.apple.mpegurl';
            video.appendChild(sHls);
            if (NATIVE_HLS) {
                video.src = v.hls;
            } else if (HAS_HLSJS) {
                attachHls(v);
            }
        } else {
            if (video.children.length) video.innerHTML = '';
            const sDirect = document.createElement('source');
            sDirect.src = v.url;
            const ext = (v.name.split('.').pop() || '').toLowerCase();
            sDirect.type = ext === 'webm' ? 'video/webm' : ext === 'mov' ? 'video/quicktime' : ext === 'mkv' ? 'video/x-matroska' : ext === 'm4v' ? 'video/x-m4v' : ext === 'ogv' ? 'video/ogg' : 'video/mp4';
            video.appendChild(sDirect);
            video.src = v.url;
        }

        videoLabel.textContent = escapeHtml(v.name) + '  ·  ' + fmtSize(v.size);
        video.preload = 'metadata';
        // 切源后保持用户选择的 playbackSpeed（不继承上一源的 5x 长按残留）
        fastSpeed = false;
        video.playbackRate = playbackSpeed;
        applyPagePlayback();
    }

    function attachHls(v) {
        if (!window.Hls || !window.Hls.isSupported()) return;
        if (hlsInstance) { try { hlsInstance.destroy(); } catch(e){} hlsInstance = null; }
        hlsInstance = new window.Hls({ maxBufferLength: 120 });
        hlsInstance.loadSource(v.hls);
        hlsInstance.attachMedia(video);
        let netR = 0, medR = 0;
        hlsInstance.on(window.Hls.Events.ERROR, (evt, data) => {
            if (!data || !data.fatal) return;
            if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR && netR < 1) { netR++; hlsInstance.startLoad(); }
            else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && medR < 1) { medR++; hlsInstance.recoverMediaError(); }
            else { destroyHls(); video.src = v.url; }
        });
    }

    function destroyHls() {
        if (hlsInstance) { try { hlsInstance.destroy(); } catch(e){} hlsInstance = null; }
    }

    function recordActivePosition() {
        // 把 video.currentTime 写回 playlistWindow[2].t —— 这是"当前中央 entry 自己"的进度
        // 不写 positions Map：positions 是跨 entry 全局共享的，会污染同名 entry 的独立历史
        if (videos.length === 0) return;
        const entry = playlistWindow[2];
        if (!entry) return;
        if (isFinite(video.currentTime) && video.currentTime > 0.5) {
            entry.t = video.currentTime;
            // 持久化（仍可走 positions2localStorage，但 data 应是 playlistWindow[2] 当前 t）
            // 这里保守不动：上一版的 positions 用法现在不被任何地方读取，等下一次清理 PR 再删
        }
    }

    // ---------------------------------------------------------------- playback
    function updatePlayback() {
        if (videos.length === 0) return;

        // 进度完全由 entry.t 主导（loadVideoForIndex 已经写入 video._pendingSeek，
        // 在 loadedmetadata / canplay 时 seek）。这里不再读 positions —— 否则会把
        // 同名视频的"全局最后位置"强加给当前 entry，破坏 entry 之间进度独立。
        const v = videos[activeIndex];
        if (!v) return;

        if (playing) {
            video.muted = false;
            const p = video.play();
            if (p && p.catch) p.catch(() => {
                // 自动播放策略拒绝：静音兜底继续播放，绝不中途暂停；
                // 声音在用户下一次手势触发 updatePlayback 时恢复
                video.muted = true;
                const p2 = video.play();
                if (p2 && p2.catch) p2.catch(() => {});
            });
        } else {
            video.pause();
        }
    }

    // Progress bar update + 实时同步 playlistWindow[2].t 为当前播放进度
    // 切换视频源/上滑/下滑时会把这个 t 写入即将离开的位置（见 shiftWindow / loadVideoForIndex），
    // 保证 5 格队列里每一格的 t 永远反映"该视频当时被播放到的时刻"
    setInterval(() => {
        if (videos.length === 0) return;
        if (video.duration && isFinite(video.duration) && video.duration > 0) {
            const pct = video.currentTime / video.duration;
            const pctStr = Math.round(pct * 100) + '%';
            infoProgress.textContent = pctStr;
            seekFill.style.width = pctStr;
            seekThumb.style.left = pctStr;
            seekLabel.textContent = fmtClock(video.currentTime) + ' / ' + fmtClock(video.duration);
            // 实时保存当前播放进度到 playlistWindow 当前格 (uuid, t)（5 格队列，索引 2 = 当前）
            if (playlistWindow.length === WINDOW_SIZE && playlistWindow[2]) {
                const t = video.currentTime;
                if (isFinite(t) && t >= 0) {
                    if (Math.abs((playlistWindow[2].t || 0) - t) >= 0.1) {
                        playlistWindow[2].t = t;
                    }
                }
            }
            // 刷新设置页播放列表条：中央格的 pl-time / pl-bar-fill 跟着 currentTime 走
            updatePlaylistStripActive();
        }
    }, 500);

    // ---------------------------------------------------------------- info
    function updateInfo() {
        if (videos.length === 0) {
            infoName.textContent = '-'; infoSize.textContent = '-';
            infoTime.textContent = '-'; infoProgress.textContent = '0%';
            infoIndex.textContent = '-'; videoCount.textContent = '0';
            return;
        }
        // 当前播放的视频：始终由 playlistWindow[2].uuid 决定（不要直接信任 activeIndex）
        _syncActiveIndexFromWindow();
        const curUuid = playlistWindow[2] && playlistWindow[2].uuid;
        const v = videos.find(x => x.name === curUuid) || videos[activeIndex];
        if (!v) return;
        infoName.textContent = v.name;
        infoSize.textContent = fmtSize(v.size);
        infoTime.textContent = formatTime(v.mtime);
        infoIndex.textContent = (activeIndex + 1) + ' / ' + videos.length;
        videoCount.textContent = videos.length;
    }

    // ---------------------------------------------------------------- page dots
    function buildPageDots() {
        pageDot.innerHTML = '';
        for (let i = 0; i < 3; i++) {
            const span = document.createElement('span');
            if (i === currentPage) span.className = 'on';
            pageDot.appendChild(span);
        }
    }

    // ---------------------------------------------------------------- load
    async function loadFeed() {
        const data = await jsonFetch('/videos');
        let list = data.videos || [];
        if (random) shuffle(list);
        videos = list;
        activeIndex = 0;
        positions.clear();
        renderFeeds();
    }

    // ---------------------------------------------------------------- gestures
    // 横向翻页阈值：相对视口 8.75%（最小 60px），视口 1280 时 = 112
    const SWIPE_THRESHOLD = Math.max(60, Math.round(window.innerWidth * 0.0875));
    const DRAG_START = 12;          // 横向拖拽起步距离
    const AXIS_LOCK_DIST = 18;      // 位移超过该值后锁定主轴（斜向滑动防误判）
    const EDGE_ZONE = 60;
    const CLOSE_THRESHOLD = 80;
    const VELOCITY_THRESHOLD = 0.5; // px/ms：快速轻扫豁免横向阈值
    const LONG_PRESS_MS = 800;
    const PAGE_COUNT = 3;

    let swipeStartX = 0, swipeStartY = 0, swipeMoved = false;
    let verticalMoved = false;   // true if user scrolled vertically (skip tap)
    let swipeStartTime = 0;
    let axisLock = null;         // null | 'h' | 'v'：位移超过 AXIS_LOCK_DIST 后锁定主轴
    // Rubber-band resistance at page edges
    function dragOffset(dx) {
        if ((dx < 0 && currentPage < PAGE_COUNT - 1) || (dx > 0 && currentPage > 0)) return dx;
        return dx / 3;
    }

    // Live drag-follow: disable CSS transition so pages track finger 1:1
    function beginDrag(dx) {
        swipeMoved = true;
        pagesEl.style.transition = 'none';
        pagesEl.style.transform = 'translateX(calc(-1 * var(--page) * (100% / 3) + ' + dragOffset(dx) + 'px))';
    }

    // Resolve swipe end -> decide target page, animate back
    function finishSwipe(endX, endY) {
        if (!swipeMoved) return false;  // treat as tap
        const dx = endX - swipeStartX;
        const dy = endY - swipeStartY;
        // 快速轻扫（有意翻页）豁免：位移 ≥ 60% 阈值且速度 > VELOCITY_THRESHOLD 即翻页
        const dt = Math.max(1, Date.now() - swipeStartTime);
        const fast = Math.abs(dx) >= SWIPE_THRESHOLD * 0.6 && Math.abs(dx) / dt > VELOCITY_THRESHOLD;
        if (Math.abs(dx) < SWIPE_THRESHOLD && !fast) {
            pagesEl.style.transition = '';
            pagesEl.style.transform = '';
            updatePlayback();  // 滑动失败也触发播放
            return false;
        }
        const target = Math.max(0, Math.min(PAGE_COUNT - 1, dx < 0 ? currentPage + 1 : currentPage - 1));
        pagesEl.style.transform = '';            // clear inline so CSS var takes effect
        if (target !== currentPage) {
            recordActivePosition();
            currentPage = target;
        }
        viewport.dataset.page = currentPage;     // 与 setPage 保持状态同步
        pagesEl.style.setProperty('--page', currentPage);
        pagesEl.style.transition = '';
        buildPageDots();
        applyPagePlayback();
        updatePlayback();  // 滑动成功也触发播放
        return true;
    }

    // ---- Douyin-style vertical paging (finger-follow + snap-back animation) ----
    let vertStartY = 0, vertBaseTop = 0, vertAnim = null;

    function cancelVertAnim() {
        if (vertAnim) { cancelAnimationFrame(vertAnim.raf); vertAnim = null; }
    }

    function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

    // 跟手：视觉中心固定在 MIDDLE_CURRENT_TOP，手指拖动产生视觉偏移
    function vertFollow(dy) {
        const feed = feeds[1];
        if (videos.length === 0) return;
        feed._progScrollUntil = Date.now() + 120;
        feed.scrollTop = vertBaseTop - dy;
    }

    // 松手：dy<0 上滑→下一视频；dy>0 下滑→上一视频
    function vertRelease(dy) {
        const feed = feeds[1];
        const h = Math.max(1, feed.clientHeight);
        if (videos.length === 0) return;
        const moved = Math.abs(dy);
        const dt = Math.max(1, Date.now() - swipeStartTime);
        const vel = dy / dt;   // px/ms
        let delta = 0;
        if (dy < 0 && (moved > h * 0.25 || vel < -0.5)) delta = 1;    // 上滑
        else if (dy > 0 && (moved > h * 0.25 || vel > 0.5)) delta = -1; // 下滑
        if (delta === 0) {
            // 未超过阈值，回弹到中心
            vertAnimateTo(vertBaseTop);
            return;
        }
        const newTop = vertBaseTop - delta * h;
        vertAnimateTo(newTop, delta);
    }

    // 平滑吸附动画：rAF + easeOutCubic，结束时仅做清理（已立即调用 applyIndex 启动翻页）
    function vertAnimateTo(top, delta) {
        const feed = feeds[1];
        cancelVertAnim();
        let from = feed.scrollTop;
        if (Math.abs(from - top) < 1) {
            if (delta) applyIndex(delta);
            return;
        }
        // 立即启动 playlistWindow 切换 + 视频翻页动画（与滚动动画并行）
        if (delta) applyIndex(delta);
        feed.scrollTop = from + (top - from) * 0.02;
        from = feed.scrollTop;
        const dur = 320;   // 320ms + easeOutCubic：起步快收尾缓，跟手感强；太快像跳，太慢像拖泥带水
        feed._progScrollUntil = Date.now() + dur + 120;
        vertAnim = { from, to: top, delta, start: performance.now(), dur, raf: 0 };
        const step = (now) => {
            if (!vertAnim) return;
            const t = Math.min(1, (now - vertAnim.start) / vertAnim.dur);
            const v = easeOutCubic(t);
            feed.scrollTop = vertAnim.from + (vertAnim.to - vertAnim.from) * v;
            if (t < 1) {
                vertAnim.raf = requestAnimationFrame(step);
            } else {
                vertAnim = null;
                feed._progScrollUntil = Date.now() + 120;
                feed.scrollTop = MIDDLE_CURRENT_TOP * feed.clientHeight;
                vertBaseTop = feed.scrollTop;   // 吸附结束后刷新基线，键盘/滚轮可以继续累加
            }
        };
        vertAnim.raf = requestAnimationFrame(step);
    }

    // wheel：上下翻页（deltaY>0 上滑→下一视频，deltaY<0 下滑→上一视频）
    // 走 vertAnimateTo 与 touch/keyboard 同一条吸附动画路径，保证视觉一致
    feeds[1].addEventListener('wheel', (e) => {
        if (playlistWindow.length === 0) return;
        e.preventDefault();
        if (vertAnim) return;
        const dir = e.deltaY > 0 ? 1 : -1;
        const h = Math.max(1, feeds[1].clientHeight);
        vertAnimateTo(vertBaseTop - dir * h, dir);
    }, { passive: false });

    // 切换播放/暂停：点击视频、空格键共用
    function togglePlayPause() {
        if (videos.length === 0) return;
        playing = !playing;
        updatePlayback();
    }

    // Treat tap on a side-panel page as "go back to main"
    function handleTap(e) {
        const target = e.target;
        if (!target || !target.closest) return;
        if (target.closest('.speed-btn') || target.closest('.cancel-btn') ||
            target.closest('.modal') || target.closest('.side-panel') ||
            target.closest('.v-delete') || target.closest('.v-compress') ||
            target.closest('.switch') || target.closest('.upload-opt')) return;

        if (currentPage !== 1) { setPage(1); return; }

        // 第 1 页（视频）：点击/长按 = 播放/暂停。上下翻页交由 swipe 手势接管。
        // （长按视频期间已被 beginFastSpeed 接管为 5x 倍速，松手后由 endFastSpeed 恢复；
        //   长按空白处不再触发上传弹窗，避免误触。）
        if (longPressTimer && !longPressMoved) {
            clearTimeout(longPressTimer);
            longPressTimer = null;
        }
        togglePlayPause();
    }

    // ---- Touch ----
    // 长按视频=5倍速；长按空白=settings/feed 空白处=上传。区分方法：起点是否在 videoContainer 内
    function isOnVideo(clientX, clientY) {
        if (currentPage !== 1) return false;
        const el = document.elementFromPoint(clientX, clientY);
        return !!(el && el.closest && el.closest('#videoContainer'));
    }
    let fastSpeed = false;  // 长按 5 倍速期间为 true
    function beginFastSpeed() {
        if (fastSpeed) return;
        fastSpeed = true;
        video.playbackRate = 5;
    }
    function endFastSpeed() {
        if (!fastSpeed) return;
        fastSpeed = false;
        video.playbackRate = playbackSpeed;
    }

    viewport.addEventListener('touchstart', (e) => {
        const t = e.touches[0];
        swipeStartX = t.clientX; swipeStartY = t.clientY;
        swipeStartTime = Date.now(); swipeMoved = false;
        verticalMoved = false;
        axisLock = null;
        vertStartY = t.clientY;
        vertBaseTop = feeds[1].scrollTop;
        cancelVertAnim();
        longPressMoved = false;
        const onVideo = isOnVideo(t.clientX, t.clientY);
        longPressTimer = setTimeout(() => {
            longPressTimer = null;
            longPressMoved = true;
            if (onVideo) {
                // 长按视频 → 5 倍速（播放/暂停仍由松手时的 handleTap 处理）
                beginFastSpeed();
            }
            // 长按空白处：什么都不做（之前会触发上传弹窗，现已移除）
        }, LONG_PRESS_MS);

        // Edge hints
        if (swipeStartX < EDGE_ZONE) edgeHintLeft.style.opacity = '1';
        if (swipeStartX > window.innerWidth - EDGE_ZONE) edgeHintRight.style.opacity = '1';
    }, { passive: true });

    viewport.addEventListener('touchmove', (e) => {
        const t = e.touches[0];
        const dx = t.clientX - swipeStartX;
        const dy = t.clientY - swipeStartY;

        // Cancel long-press on any movement
        if (longPressTimer && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
            clearTimeout(longPressTimer); longPressTimer = null;
        }

        // 方向锁定：首次显著位移后锁定主轴，后续只响应主轴（斜向/微抖不再误判）
        if (!axisLock && (Math.abs(dx) > AXIS_LOCK_DIST || Math.abs(dy) > AXIS_LOCK_DIST)) {
            axisLock = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
        }

        if (axisLock === 'h') {
            if (Math.abs(dx) > DRAG_START) beginDrag(dx);
            edgeHintLeft.style.opacity = '0'; edgeHintRight.style.opacity = '0';
        } else if (axisLock === 'v') {
            verticalMoved = true;
            vertFollow(dy);
            edgeHintLeft.style.opacity = '0'; edgeHintRight.style.opacity = '0';
        }
    }, { passive: true });

    viewport.addEventListener('touchend', (e) => {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
        endFastSpeed();  // 松手恢复 1x 倍速（或用户设置的 playbackSpeed）
        const t = e.changedTouches[0];
        edgeHintLeft.style.opacity = '0'; edgeHintRight.style.opacity = '0';
        const dy = t.clientY - vertStartY;
        if (verticalMoved && Math.abs(dy) > 12) {   // 纵向手势松手 → 平滑吸附动画
            vertRelease(dy);
            return;
        }
        const handled = finishSwipe(t.clientX, t.clientY);
        if (!handled && !verticalMoved) handleTap(e);
    }, { passive: true });

    viewport.addEventListener('touchcancel', () => {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
        endFastSpeed();
    }, { passive: true });

    // ---- Mouse ----
    let mouseDown = false;
    viewport.addEventListener('mousedown', (e) => {
        mouseDown = true;
        swipeStartX = e.clientX; swipeStartY = e.clientY;
        swipeStartTime = Date.now(); swipeMoved = false;
        verticalMoved = false;
        axisLock = null;
        vertStartY = e.clientY;
        vertBaseTop = feeds[1].scrollTop;
        cancelVertAnim();
        const onVideo = isOnVideo(e.clientX, e.clientY);
        longPressTimer = setTimeout(() => {
            longPressTimer = null;
            if (onVideo) {
                // 长按视频 → 5 倍速（松手后由 endFastSpeed 恢复，播放/暂停由 mouseup->handleTap 处理）
                beginFastSpeed();
            }
            // 长按空白处：什么都不做（之前会触发上传弹窗，现已移除）
        }, LONG_PRESS_MS);
    });
    viewport.addEventListener('mousemove', (e) => {
        if (!mouseDown) return;
        const dx = e.clientX - swipeStartX;
        const dy = e.clientY - swipeStartY;
        if (longPressTimer && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
            clearTimeout(longPressTimer); longPressTimer = null;
        }
        // 方向锁定（与 touchmove 一致）
        if (!axisLock && (Math.abs(dx) > AXIS_LOCK_DIST || Math.abs(dy) > AXIS_LOCK_DIST)) {
            axisLock = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
        }
        if (axisLock === 'h') {
            if (Math.abs(dx) > DRAG_START) beginDrag(dx);
        } else if (axisLock === 'v') {
            verticalMoved = true; vertFollow(dy);
        }
    });
    viewport.addEventListener('mouseup', (e) => {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
        endFastSpeed();
        if (!mouseDown) return;
        mouseDown = false;
        const dy = e.clientY - vertStartY;
        if (verticalMoved && Math.abs(dy) > 12) {   // 纵向手势松手 → 平滑吸附动画
            vertRelease(dy);
            return;
        }
        if (!finishSwipe(e.clientX, e.clientY) && !verticalMoved) handleTap(e);
    });
    viewport.addEventListener('mouseleave', () => {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
        endFastSpeed();
        mouseDown = false;
    });

    // First interaction unlocks audio
    ['pointermove','wheel','scroll','touchmove','keydown'].forEach(ev => {
        document.addEventListener(ev, function first() { updatePlayback(); }, { once: true, passive: true });
    });

    // ---- Keyboard navigation ----
    // ArrowDown/ArrowUp: 三个页面都能切换视频
    //   - 第 1 页（主 feed）：走 vertAnimateTo 平滑吸附 + 视频渐入
    //   - 第 0 页 / 第 2 页（信息页 / 设置页）：直接 applyIndex（无纵向滚动动画，
    //     但播放列表条高亮会自动跟随 .active 状态 + 平滑滚到可视范围）
    // ArrowLeft / ArrowRight: 三页之间切换
    document.addEventListener('keydown', (e) => {
        // 输入控件里忽略（避免与表单交互冲突）
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        if (e.altKey || e.ctrlKey || e.metaKey) return;

        switch (e.key) {
            case 'ArrowDown':
                if (vertAnim) return;
                e.preventDefault();
                if (currentPage === 1) {
                    vertAnimateTo(vertBaseTop + feeds[1].clientHeight, 1);
                } else {
                    // 第 0 / 2 页：直接切换，无纵向动画
                    applyIndex(1);
                }
                break;
            case 'ArrowUp':
                if (vertAnim) return;
                e.preventDefault();
                if (currentPage === 1) {
                    vertAnimateTo(vertBaseTop - feeds[1].clientHeight, -1);
                } else {
                    applyIndex(-1);
                }
                break;
            case 'ArrowLeft':
                // 左键 = 翻到第三页（设置页）：索引 0→1→2 递增
                if (currentPage >= PAGE_COUNT - 1) return;
                e.preventDefault();
                setPage(currentPage + 1);
                break;
            case 'ArrowRight':
                // 右键 = 翻回第一页（信息页）：索引 2→1→0 递减
                if (currentPage <= 0) return;
                e.preventDefault();
                setPage(currentPage - 1);
                break;
            case ' ':
            case 'Space':
                // 空格 = 播放/暂停（与点击视频共用 togglePlayPause）
                e.preventDefault();
                togglePlayPause();
                break;
        }
    });

    // Video metadata: restore pending seek
    video.addEventListener('loadedmetadata', () => {
        if (video._pendingSeek !== undefined && isFinite(video._pendingSeek) &&
            video._pendingSeek < video.duration - 0.3) {
            video.currentTime = video._pendingSeek;
        }
        video._pendingSeek = undefined;
        // 显示进度条
        seekTrack && seekTrack.classList.remove('hidden');
        seekLabel && seekLabel.classList.remove('hidden');
    });

    // 缓冲完成后自动播放（playing=true 时才触发）
    video.addEventListener('canplay', () => {
        if (playing && !isNaN(video.duration)) {
            const p = video.play();
            if (p && p.catch) p.catch(() => {});
        }
    });

    // Seek bar drag (touch + mouse)
    let seekDragging = false;

    function doSeek(clientX) {
        if (videos.length === 0 || !seekTrack) return;
        const rect = seekTrack.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        if (video.duration && isFinite(video.duration)) {
            video.currentTime = ratio * video.duration;
        }
    }

    seekTrack && seekTrack.addEventListener('touchstart', (e) => {
        seekDragging = true;
        doSeek(e.touches[0].clientX);
        e.stopPropagation();
    }, { passive: true });
    seekTrack && seekTrack.addEventListener('touchmove', (e) => {
        if (seekDragging) { doSeek(e.touches[0].clientX); e.stopPropagation(); }
    }, { passive: true });
    seekTrack && seekTrack.addEventListener('touchend', () => { seekDragging = false; }, { passive: true });
    seekTrack && seekTrack.addEventListener('mousedown', (e) => {
        seekDragging = true;
        doSeek(e.clientX);
        e.stopPropagation();
    });
    document.addEventListener('mousemove', (e) => { if (seekDragging) doSeek(e.clientX); });
    document.addEventListener('mouseup', () => { seekDragging = false; });

    // Speed selector — applies to main feed (page 1) and settings (page 2)
    speedOptions.addEventListener('click', (e) => {
        const btn = e.target.closest('.speed-btn');
        if (!btn) return;
        playbackSpeed = parseFloat(btn.dataset.speed);
        speedOptions.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        if (currentPage === 1 || currentPage === 2) { video.playbackRate = playbackSpeed; }
    });

    // Overlay buttons (only shown on settings page)
    overlayCompress.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (videos.length === 0) return;
        const v = videos[activeIndex];
        if (!confirm('压缩 ' + v.name + ' ？\n将转为 H.264/MP4，体积更小、播放更流畅。')) return;
        overlayCompress.disabled = true; overlayCompress.textContent = '压缩中…';
        try {
            const r = await jsonFetch('/compress/' + encodeURIComponent(v.name), { method: 'POST' });
            alert(r.skipped ? '无需压缩（' + fmtSize(r.before) + '）。' : '压缩完成：' + fmtSize(r.before) + ' → ' + fmtSize(r.after) + '，节省 ' + r.savedPct + '%。');
            await loadFeed();
        } catch (err) { alert('压缩失败：' + err.message); overlayCompress.disabled = false; overlayCompress.textContent = '压缩'; }
    });

    overlayDelete.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (videos.length === 0) return;
        const v = videos[activeIndex];
        if (!confirm('删除 ' + v.name + ' ？')) return;
        try { await fetch(v.url, { method: 'DELETE' }); await loadFeed(); }
        catch (err) { alert('删除失败：' + err.message); }
    });

    window.addEventListener('resize', () => {
        feeds.forEach(f => { if (videos.length) scrollToIndex(f, activeIndex); });
    });

    // ---------------------------------------------------------------- upload
    // 上传队列：支持多文件串行上传
    let uploadQueue = [];
    let uploadCurrent = null;

    // 计算文件哈希：小文件用 crypto.subtle（快），大文件用手动实现（省内存）
    async function computeFileHash(file, onProgress) {
        const SMALL_FILE_THRESHOLD = 50 * 1024 * 1024;  // 50MB 以下用 subtle
        
        if (file.size <= SMALL_FILE_THRESHOLD && crypto.subtle && crypto.subtle.digest) {
            // 小文件：一次性读取 + crypto.subtle（快）
            if (onProgress) onProgress(50);
            const buf = await file.arrayBuffer();
            if (onProgress) onProgress(90);
            const d = await crypto.subtle.digest('SHA-256', buf);
            if (onProgress) onProgress(100);
            return Array.from(new Uint8Array(d)).map(b => b.toString(16).padStart(2, '0')).join('');
        }
        
        // 大文件：分块读取 + 手动 SHA-256（省内存）
        const SLICE = 2 * 1024 * 1024;  // 2MB per slice
        const ctx = createSha256();
        let processed = 0;
        for (let start = 0; start < file.size; start += SLICE) {
            const buf = await file.slice(start, Math.min(start + SLICE, file.size)).arrayBuffer();
            ctx.update(new Uint8Array(buf));
            processed += buf.byteLength;
            if (onProgress) onProgress(Math.round(processed / file.size * 100));
        }
        return ctx.digestHex();
    }

    async function uploadOneFile(file) {
        const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
        let hash = '', pct = 0;

        progressArea.classList.remove('hidden');
        progressTitle.textContent = file.name;
        progressFill.style.width = '0%'; progressText.textContent = '0%';

        try {
            progressText.textContent = '计算文件哈希…';
            hash = await computeFileHash(file, (p) => {
                const hp = Math.round(p * 0.05);
                if (hp !== pct) { pct = hp; progressText.textContent = '哈希 ' + hp + '%'; }
            });
            progressText.textContent = '正在连接…';
            const init = await jsonFetch('/upload/init', {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({filename:file.name, size:file.size, hash, chunkSize:CHUNK_SIZE, totalChunks}),
                signal: currentAbort.signal
            });
            if (init.skip) {
                progressFill.style.width='100%'; progressText.textContent='100%  秒传成功！';
                await sleep(800); return;
            }
            const { uploadId, uploaded = [] } = init;
            const remaining = [];
            for (let i = 0; i < totalChunks; i++) if (!uploaded.includes(i)) remaining.push(i);
            for (let i = 0; i < remaining.length; i++) {
                const idx = remaining[i];
                const start = idx * CHUNK_SIZE;
                const end = Math.min(start + CHUNK_SIZE, file.size);
                const uploadPct = Math.round((i / remaining.length) * 90) + 5;
                progressText.textContent = '上传中 ' + uploadPct + '%  (' + (i+1) + '/' + remaining.length + ' 分片)';
                progressFill.style.width = uploadPct + '%';
                await fetch('/upload/chunk/' + uploadId + '/' + idx, {method:'PUT', body:file.slice(start,end), signal:currentAbort.signal});
            }
            progressText.textContent='完成中…'; progressFill.style.width='98%';
            await jsonFetch('/upload/complete/' + uploadId, {method:'POST', signal:currentAbort.signal});
            progressFill.style.width='100%'; progressText.textContent='完成！';
            await sleep(500);
        } catch (err) {
            if (err.name === 'AbortError') progressText.textContent = '已取消';
            else { progressText.textContent = '失败: ' + err.message; alert('上传失败：' + err.message); }
            throw err;
        }
    }

    async function processUploadQueue() {
        if (uploadCurrent || uploadQueue.length === 0) return;
        uploadCurrent = uploadQueue.shift();
        progressArea.classList.remove('hidden');
        try {
            await uploadOneFile(uploadCurrent);
        } catch (err) {
            // 错误已在 uploadOneFile 中处理
        }
        uploadCurrent = null;
        if (uploadQueue.length > 0) {
            // 继续下一个
            progressText.textContent = '下一个文件…';
            await sleep(300);
            await processUploadQueue();
        } else {
            uploadModal.classList.add('hidden');
            await loadFeed();
        }
    }

    function queueFiles(files) {
        for (const f of files) {
            if (!f.type.startsWith('video/')) continue;
            uploadQueue.push(f);
        }
        if (!uploadCurrent) processUploadQueue();
    }

    async function compressVideo(file, onProgress) {
        return new Promise((resolve, reject) => {
            const v = document.createElement('video'); v.muted = true; v.preload = 'auto';
            const url = URL.createObjectURL(file);
            let chunks = [];
            v.onloadedmetadata = () => {
                const c = document.createElement('canvas');
                c.width = v.videoWidth || 1280; c.height = v.videoHeight || 720;
                const ctx = c.getContext('2d');
                const stream = c.captureStream(30);
                const mr = new MediaRecorder(stream, {mimeType:'video/webm;codecs=vp9', videoBitsPerSecond:3000000});
                mr.ondataavailable = e => { if (e.data && e.data.size > 0) chunks.push(e.data); };
                mr.onstop = () => { const blob = new Blob(chunks, {type:'video/webm'}); URL.revokeObjectURL(url); resolve(new File([blob], file.name.replace(/\.[^.]+$/, '.webm'), {type:'video/webm'})); };
                v.onerror = () => { URL.revokeObjectURL(url); reject(new Error('视频解码失败')); };
                v.play(); mr.start(100);
                const draw = () => { if (mr.state === 'recording') { ctx.drawImage(v,0,0,c.width,c.height); if (onProgress && v.duration) onProgress(Math.round(v.currentTime/v.duration*100)); requestAnimationFrame(draw); } };
                v.onended = () => { if (mr.state === 'recording') mr.stop(); };
                draw();
            };
            v.onerror = () => { URL.revokeObjectURL(url); reject(new Error('无法加载视频')); };
            v.src = url;
        });
    }

    cancelBtn.addEventListener('click', () => {
        if (currentAbort) { currentAbort.abort(); currentAbort = null; }
        uploadQueue = [];
        uploadCurrent = null;
        uploadModal.classList.add('hidden');
    });
    // Floating upload button (right edge) — open upload modal without long-press
    if (fabUpload) {
        let fabDown = false;
        let fabMoved = false;
        const onFabClick = (e) => { e.stopPropagation(); uploadModal.classList.remove('hidden'); };
        fabUpload.addEventListener('click', onFabClick);
        // Don't trigger panel swipe when tapping the FAB
        fabUpload.addEventListener('touchstart', (e) => { fabDown = true; fabMoved = false; e.stopPropagation(); }, { passive: true });
        fabUpload.addEventListener('touchmove', (e) => { fabMoved = true; e.stopPropagation(); }, { passive: true });
        fabUpload.addEventListener('touchend', (e) => { if (fabDown && !fabMoved) { e.preventDefault(); e.stopPropagation(); onFabClick(e); } fabDown = false; });
        fabUpload.addEventListener('mousedown', (e) => e.stopPropagation());
    }
    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('dragover'); queueFiles(e.dataTransfer.files); });
    fileInput.addEventListener('change', () => { queueFiles(fileInput.files); fileInput.value = ''; });

    // 单文件上传（保留兼容性，用于压缩后上传）
    async function uploadFile(file) {
        if (currentAbort) { currentAbort.abort(); currentAbort = null; }
        currentAbort = new AbortController();
        progressArea.classList.remove('hidden');
        await uploadOneFile(file);
        currentAbort = null;
    }

    async function handleUploadFile(file) {
        const cb = document.getElementById('compressBeforeUpload').checked;
        if (cb) {
            try {
                progressArea.classList.remove('hidden'); progressTitle.textContent = '压缩 ' + file.name + ' …';
                progressFill.style.width='0%'; progressText.textContent='0%';
                const c = await compressVideo(file, p => { progressText.textContent='压缩中 '+p+'%'; progressFill.style.width=Math.round(p*0.7)+'%'; });
                progressText.textContent='压缩完成，上传中…'; progressFill.style.width='70%';
                await uploadOneFile(c);
            } catch (err) { alert('压缩失败：'+err.message+'\n改用直接上传。'); await uploadOneFile(file); }
        } else { await uploadOneFile(file); }
    }

    // Settings switches
    randomSwitch.addEventListener('change', () => { random = randomSwitch.checked; loadFeed(); });
    autoplaySwitch.addEventListener('change', () => { autoplay = autoplaySwitch.checked; if (autoplay && !playing) { playing = true; updatePlayback(); } });

    // ---------------------------------------------------------------- init
    pagesEl.style.setProperty('--page', 1);
    loadFeed();

})();
