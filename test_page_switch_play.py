#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  视频页「从第 x 页切换到第 y 页一定要保证视频在播放，不要暂停」。
  现状问题：
    1) setPage() 只调用 applyPagePlayback()，而后者仅在 `playing === true` 时才 play()；
       一旦播放态被置为暂停（点击暂停 / 自动播放被拒），切页后视频仍是暂停的。
    2) applyPagePlayback 与 canplay 都是裸 `video.play().catch(()=>{})`，
       没有自动播放策略兜底（被拒后不会静音重试），恰好卡在切页/切源后 → 一直暂停。

超时机制: 每个用例 60 秒超时
"""
import os
import re
import signal
import subprocess
import tempfile

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
STYLE_CSS = os.path.join(ROOT, "src", "obs", "video_static", "style.css")
BASE_URL = "http://127.0.0.1:80"


def timeout(seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            def timeout_handler(signum, frame):
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds}s")
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result
        return wrapper
    return decorator


def fetch(path: str) -> str:
    r = requests.get(BASE_URL + path, timeout=10)
    assert r.status_code == 200, f"{path} 返回 {r.status_code}"
    return r.text


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_func(src: str, name: str) -> str:
    """按大括号配对，从 IIFE 里抠出一个函数定义"""
    m = re.search(r"function\s+%s\s*\(" % re.escape(name), src)
    assert m, f"找不到函数 {name}"
    start = src.index("{", m.end() - 1)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"{name} 的大括号不配对")


JS_HARNESS = r"""
const R = [];
function assert(c, m) { R.push((c ? 'PASS' : 'FAIL') + ' :: ' + m); if (!c) process.exitCode = 1; }

async function runScenario(o) {
    // ---- 与 app.js 顶层状态一一对应的变量（改名为真实同名，供被抠出的函数直接使用）----
    let currentPage = o.page;
    let playing = o.playing;
    let playbackSpeed = 1;
    let fastSpeed = false;
    let nativeReverse = null;
    let reverseActive = false;
    let reverseTimer = null;
    let reverseLastTs = 0;
    const REVERSE_RATE = 3;
    const REVERSE_TICK_MS = 120;
    const videos = [{ name: 'v1' }];
    let playCalls = 0;
    let pauseCalls = 0;
    const video = {
        playbackRate: 1, currentTime: 10, duration: 100, paused: true, muted: false,
        play() {
            playCalls++;
            if (o.rejectUnmutedPlay && !this.muted) {
                return Promise.reject(new Error('NotAllowedError: autoplay policy'));
            }
            this.paused = false;
            return Promise.resolve();
        },
        pause() { pauseCalls++; this.paused = true; },
    };
    const viewport = { dataset: { page: String(o.page) } };
    const pagesEl = { style: { setProperty() {} } };
    function buildPageDots() {}
    function recordActivePosition() {}

__FUNCS__

    setPage(o.target);
    // 让 ensurePlaying 里的 play() Promise 链（含静音兜底）跑完
    for (let k = 0; k < 5; k++) await Promise.resolve();
    return { playCalls, pauseCalls, paused: video.paused, muted: video.muted,
             rate: video.playbackRate, page: currentPage, playing };
}

(async () => {
// 1) 暂停态下 1 -> 2：切页必须恢复播放
let r = await runScenario({ page: 1, playing: false, target: 2 });
assert(r.page === 2, '1->2 页面已切换');
assert(r.paused === false, '1->2 暂停态切页后视频在播放（不被暂停）');
assert(r.playCalls >= 1, '1->2 调用了 video.play()');

// 2) 播放态 1 -> 2
r = await runScenario({ page: 1, playing: true, target: 2 });
assert(r.paused === false, '1->2 播放态切页后仍在播放');
assert(r.rate === 1, '1->2 速率回到 playbackSpeed(1)');

// 3) 1 -> 0（信息页）：必须播放且为 -3x 倒放
r = await runScenario({ page: 1, playing: true, target: 0 });
assert(r.paused === false, '1->0 切页后视频在播放');
assert(r.rate === -3, '1->0 使用 -3x 倒放速率，实际 ' + r.rate);

// 4) 暂停态 0 -> 2（倒放页返回设置页）：必须恢复播放且速率恢复正常
r = await runScenario({ page: 0, playing: false, target: 2 });
assert(r.paused === false, '0->2 暂停态切回后视频在播放');
assert(r.rate === 1, '0->2 速率恢复为 playbackSpeed(1)，实际 ' + r.rate);

// 5) 自动播放策略拒绝（未静音 play 被拒）：静音兜底继续播放，绝不暂停
r = await runScenario({ page: 2, playing: true, target: 1, rejectUnmutedPlay: true });
assert(r.paused === false, '自动播放被拒时静音兜底继续播放');
assert(r.muted === true, '自动播放被拒后进入静音播放');
assert(r.playCalls >= 2, '自动播放被拒后重试了静音 play()，实际 ' + r.playCalls);
assert(r.pauseCalls === 0, '切页全程未调用 video.pause()');

// 6) 暂停态 2 -> 0：切页后仍必须播放
r = await runScenario({ page: 2, playing: false, target: 0 });
assert(r.paused === false, '2->0 暂停态切页后视频在播放');
assert(r.rate === -3, '2->0 进入 -3x 倒放，实际 ' + r.rate);

console.log(JSON.stringify(R));
})();
"""


def run_js_harness(src: str):
    funcs = "\n\n".join(extract_func(src, n) for n in (
        "setPage", "applyPagePlayback", "ensurePlaying",
        "startReverse", "stopReverse", "reverseTick", "supportsNegativeRate",
    ))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "harness.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(JS_HARNESS.replace("__FUNCS__", funcs))
        out = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"node 行为测试失败:\n{out.stdout}\n{out.stderr}"
        return out.stdout.strip().splitlines()[-1]


# ---------------------------------------------------------------- 1/4
@timeout(60)
def test_source_page_switch_forces_play():
    """[1/4] 源码：setPage 强制恢复播放；applyPagePlayback 有 ensurePlaying"""
    print("\n[1/4] 源码切页保活实现...")
    src = read(APP_JS)

    assert "function ensurePlaying" in src, "缺少 ensurePlaying（统一保播放 + 自动播放策略兜底）"
    ensure = extract_func(src, "ensurePlaying")
    assert "video.play()" in ensure, "ensurePlaying 未调用 video.play()"
    assert re.search(r"catch\s*\(", ensure) and "muted = true" in ensure, \
        "ensurePlaying 缺少静音兜底（自动播放被拒后会一直暂停）"
    assert "video.pause" not in ensure, "ensurePlaying 不应暂停视频"
    print("   ✓ ensurePlaying：play + 静音兜底，且不含 pause")

    set_page = extract_func(src, "setPage")
    assert re.search(r"playing\s*=\s*true", set_page), \
        "setPage 未强制置为播放态（暂停态切页后仍是暂停）"
    assert "ensurePlaying()" in set_page, "setPage 未调用 ensurePlaying()"
    assert "video.pause" not in set_page, "setPage 不应暂停视频"
    print("   ✓ setPage：强制 playing=true + ensurePlaying()")

    apply_pb = extract_func(src, "applyPagePlayback")
    assert "ensurePlaying()" in apply_pb, "applyPagePlayback 未走 ensurePlaying()"
    assert "video.pause" not in apply_pb, "applyPagePlayback 不应暂停视频"
    print("   ✓ applyPagePlayback 走 ensurePlaying()，不暂停")

    # 手机主要翻页方式（左右滑动）必须与 setPage 走同一条保活路径，
    # 不能再自己复制一份「applyPagePlayback + updatePlayback」（后者在暂停态会 pause）
    swipe = extract_func(src, "finishSwipe")
    assert "setPage(" in swipe, "滑动翻页未走 setPage()（缺少强制保活）"
    assert not re.search(r"applyPagePlayback\(\)\s*;\s*\n\s*updatePlayback\(\)", swipe), \
        "滑动翻页仍在裸调用 applyPagePlayback + updatePlayback（暂停态会被 pause）"
    print("   ✓ finishSwipe 滑动翻页统一走 setPage()")

    # 裸 play()（无兜底）必须在切页/切源路径上绝迹
    assert not re.search(r"video\.play\(\)\.catch\(\(\)\s*=>\s*\{\s*\}\)", apply_pb), \
        "applyPagePlayback 仍有裸 video.play()（无自动播放兜底）"
    assert "ensurePlaying()" in read(APP_JS).split("addEventListener('canplay'")[1][:400], \
        "canplay 未走 ensurePlaying()（切源后可能一直暂停）"
    print("   ✓ canplay 也走 ensurePlaying()")


# ---------------------------------------------------------------- 2/4
@timeout(60)
def test_live_page_switch_forces_play():
    """[2/4] 线上 /video/app.js 已下发切页保活实现"""
    print("\n[2/4] 线上 app.js 切页保活...")
    src = fetch("/video/app.js")
    assert "function ensurePlaying" in src, "线上缺少 ensurePlaying"
    assert "ensurePlaying()" in extract_func(src, "setPage"), "线上 setPage 未保活"
    assert re.search(r"playing\s*=\s*true", extract_func(src, "setPage")), \
        "线上 setPage 未强制播放态"
    print("   ✓ 线上 app.js 已包含 setPage 强制保活 + ensurePlaying")


# ---------------------------------------------------------------- 3/4
@timeout(60)
def test_behavior_harness():
    """[3/4] node 跑真实被抠出的 setPage/applyPagePlayback：6 个切页场景都必须保持在播"""
    print("\n[3/4] node 行为验证（真实函数，全 6 个切页方向）...")
    src = read(APP_JS)
    result_line = run_js_harness(src)
    import json
    results = json.loads(result_line)
    fails = [x for x in results if x.startswith("FAIL")]
    for x in results:
        print("   " + ("✓ " if x.startswith("PASS") else "✗ ") + x.split(" :: ", 1)[1])
    assert not fails, f"以下场景失败: {fails}"
    print(f"   ✓ {len(results)} 条行为断言全部通过（含暂停态切页、自动播放被拒兜底）")


# ---------------------------------------------------------------- 4/4
@timeout(60)
def test_regression():
    """[4/4] 回归：进度条 100px / -3x 倒放 / 1-2-7 档位 / http 哈希兜底 / logs 挂载"""
    print("\n[4/4] 回归...")

    # 上一任务：进度条 100px
    css = fetch("/video/style.css")
    for sel in (".seek-track", ".seek-track::before", ".seek-fill"):
        block = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        assert block, f"CSS 缺少 {sel}"
        m = re.search(r"height\s*:\s*(\d+)px", block.group(1))
        assert m and int(m.group(1)) >= 100, f"{sel} 高度不足 100px: {block.group(1)}"
    print("   ✓ 进度条仍为 100px")

    js = fetch("/video/app.js")
    html = fetch("/video")
    assert "REVERSE_RATE" in js, "-3x 倒放丢失"
    assert "-REVERSE_RATE" in js, "负速率倒放实现丢失"
    for s in ('data-speed="1"', 'data-speed="2"', 'data-speed="7"'):
        assert s in html, f"播放速度档位丢失: {s}"
    print("   ✓ -3x 倒放 + 1/2/7 档位正常")

    home = fetch("/")
    assert "HAS_SUBTLE" in home and "sha256HexJS" in home, "http 哈希兜底丢失"
    assert requests.get(BASE_URL + "/health", timeout=10).json()["status"] == "ok"
    assert os.path.exists(os.path.join(ROOT, "logs", "server.log")), "logs/server.log 挂载丢失"
    print("   ✓ http 哈希兜底 + /health + logs 挂载正常")


def main():
    print("=" * 60)
    print("任务测试：页间切换必须保持播放（不暂停）")
    print("=" * 60)
    for t in (
        test_source_page_switch_forces_play,
        test_live_page_switch_forces_play,
        test_behavior_harness,
        test_regression,
    ):
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
