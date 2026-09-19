#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  键盘左右方向键功能互换（只改键盘快捷键，不动滑动逻辑与页面布局）。

现状（互换前）：
  ArrowLeft  = setPage(currentPage + 1)  → 往第三页（设置页）翻
  ArrowRight = setPage(currentPage - 1)  → 往第一页（信息页）翻
要求（互换后）：
  ArrowLeft  = setPage(currentPage - 1)  → 往第一页（信息页）翻
  ArrowRight = setPage(currentPage + 1)  → 往第三页（设置页）翻

超时机制: 每个用例 60 秒超时
"""
import json
import os
import re
import signal
import subprocess
import tempfile

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
INDEX_HTML = os.path.join(ROOT, "src", "obs", "video_static", "index.html")
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


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fetch(path: str) -> str:
    r = requests.get(BASE_URL + path, timeout=10)
    assert r.status_code == 200, f"{path} 返回 {r.status_code}"
    return r.text


def brace_slice(src: str, open_idx: int) -> str:
    """从 open_idx 处的 '{' 起按大括号配对截取完整块"""
    depth = 0
    for j in range(open_idx, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:j + 1]
    raise AssertionError("大括号不配对")


def extract_func(src: str, name: str) -> str:
    m = re.search(r"function\s+%s\s*\(" % re.escape(name), src)
    assert m, f"找不到函数 {name}"
    head = src[m.start():src.index("{", m.end() - 1)]
    return head + brace_slice(src, src.index("{", m.end() - 1))


def extract_keydown_cases(src: str) -> dict:
    """抠出 keydown 监听里 switch 的各 case 分支体"""
    m = re.search(r"document\.addEventListener\('keydown'", src)
    assert m, "找不到 keydown 监听"
    body = brace_slice(src, src.index("{", m.end()))
    cases = {}
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"):
        cm = re.search(r"case\s+'%s'\s*:(.*?)break;" % key, body, re.S)
        assert cm, f"keydown 里找不到 case '{key}'"
        cases[key] = cm.group(1)
    return cases


def extract_keydown_handler(src: str) -> str:
    """抠出 keydown 监听的回调函数源码，供 node 直接执行"""
    m = re.search(r"document\.addEventListener\('keydown'\s*,\s*", src)
    assert m, "找不到 keydown 监听"
    i = src.index("(", m.end())
    arrow = src.index("=>", i)
    return src[i:arrow] + "=>" + brace_slice(src, src.index("{", arrow))


JS_HARNESS = r"""
const R = [];
function assert(c, m) { R.push((c ? 'PASS' : 'FAIL') + ' :: ' + m); if (!c) process.exitCode = 1; }

const PAGE_COUNT = 3;
let currentPage = 1;
let vertAnim = null;
const vertBaseTop = 0;
const feeds = [{ clientHeight: 100 }, { clientHeight: 100 }, { clientHeight: 100 }];
let calls = [];
function setPage(n) { calls.push(['setPage', n]); currentPage = n; }
function applyIndex(d) { calls.push(['applyIndex', d]); }
function vertAnimateTo(t, d) { calls.push(['vertAnimateTo', t, d]); }
function togglePlayPause() { calls.push(['togglePlayPause']); }

const handler = __HANDLER__;

function press(key, page) {
    currentPage = page;
    calls = [];
    handler({ key, preventDefault() {}, altKey: false, ctrlKey: false, metaKey: false, target: null });
    return calls.filter(c => c[0] === 'setPage').map(c => c[1]);
}

// 第 1 页：左键往第一页(0)，右键往第三页(2)
assert(JSON.stringify(press('ArrowLeft', 1)) === '[0]', '第1页 ArrowLeft -> 页0，实际 ' + JSON.stringify(press('ArrowLeft', 1)));
assert(JSON.stringify(press('ArrowRight', 1)) === '[2]', '第1页 ArrowRight -> 页2，实际 ' + JSON.stringify(press('ArrowRight', 1)));

// 第 0 页（最左）：左键不动作（到边界），右键往第 1 页
assert(JSON.stringify(press('ArrowLeft', 0)) === '[]', '第0页 ArrowLeft 到边界不应切换，实际 ' + JSON.stringify(press('ArrowLeft', 0)));
assert(JSON.stringify(press('ArrowRight', 0)) === '[1]', '第0页 ArrowRight -> 页1，实际 ' + JSON.stringify(press('ArrowRight', 0)));

// 第 2 页（最右）：左键往第 1 页，右键不动作（到边界）
assert(JSON.stringify(press('ArrowLeft', 2)) === '[1]', '第2页 ArrowLeft -> 页1，实际 ' + JSON.stringify(press('ArrowLeft', 2)));
assert(JSON.stringify(press('ArrowRight', 2)) === '[]', '第2页 ArrowRight 到边界不应切换，实际 ' + JSON.stringify(press('ArrowRight', 2)));

// 上下键不受影响（仍走视频切换）
currentPage = 1; calls = [];
handler({ key: 'ArrowDown', preventDefault() {}, altKey: false, ctrlKey: false, metaKey: false, target: null });
assert(calls.some(c => c[0] === 'vertAnimateTo' && c[2] === 1), 'ArrowDown 仍切下一个视频');
currentPage = 1; calls = [];
handler({ key: 'ArrowUp', preventDefault() {}, altKey: false, ctrlKey: false, metaKey: false, target: null });
assert(calls.some(c => c[0] === 'vertAnimateTo' && c[2] === -1), 'ArrowUp 仍切上一个视频');

console.log(JSON.stringify(R));
"""


def run_harness(src: str):
    handler = extract_keydown_handler(src)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "harness.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(JS_HARNESS.replace("__HANDLER__", handler))
        out = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"node 行为测试失败:\n{out.stdout}\n{out.stderr}"
        return json.loads(out.stdout.strip().splitlines()[-1])


def assert_swapped(cases: dict, source: str):
    left = cases["ArrowLeft"]
    right = cases["ArrowRight"]
    assert re.search(r"setPage\(currentPage\s*-\s*1\)", left), \
        f"{source} ArrowLeft 应为 setPage(currentPage - 1)（往第一页）: {left.strip()}"
    assert re.search(r"setPage\(currentPage\s*\+\s*1\)", right), \
        f"{source} ArrowRight 应为 setPage(currentPage + 1)（往第三页）: {right.strip()}"
    # 边界守卫也要对应互换：左键守 currentPage <= 0，右键守 currentPage >= PAGE_COUNT - 1
    assert re.search(r"currentPage\s*<=\s*0", left), f"{source} ArrowLeft 缺 currentPage <= 0 守卫: {left.strip()}"
    assert re.search(r"currentPage\s*>=\s*PAGE_COUNT\s*-\s*1", right), \
        f"{source} ArrowRight 缺 currentPage >= PAGE_COUNT - 1 守卫: {right.strip()}"


# ---------------------------------------------------------------- 1/4
@timeout(60)
def test_source_arrow_swapped():
    """[1/4] 源码：左右方向键功能已互换"""
    print("\n[1/4] 源码左右键互换...")
    src = read(APP_JS)
    cases = extract_keydown_cases(src)
    assert_swapped(cases, "源码")
    assert not re.search(r"setPage\(currentPage\s*\+\s*1\)", cases["ArrowLeft"]), \
        "ArrowLeft 仍保留旧的 currentPage + 1"
    assert not re.search(r"setPage\(currentPage\s*-\s*1\)", cases["ArrowRight"]), \
        "ArrowRight 仍保留旧的 currentPage - 1"
    print("   ✓ ArrowLeft -> currentPage - 1（第一页）；ArrowRight -> currentPage + 1（第三页）")
    print("   ✓ 边界守卫同步互换")


# ---------------------------------------------------------------- 2/4
@timeout(60)
def test_live_arrow_swapped():
    """[2/4] 线上 /video/app.js 已下发互换后的左右键"""
    print("\n[2/4] 线上 app.js 左右键互换...")
    src = fetch("/video/app.js")
    assert_swapped(extract_keydown_cases(src), "线上")
    print("   ✓ 线上 app.js 左右方向键已互换")


# ---------------------------------------------------------------- 3/4
@timeout(60)
def test_behavior_arrow_swap():
    """[3/4] node 跑真实 keydown 回调：6 个方向 + 上下键不受影响"""
    print("\n[3/4] node 行为验证（真实 keydown 回调）...")
    results = run_harness(read(APP_JS))
    for x in results:
        print("   " + ("✓ " if x.startswith("PASS") else "✗ ") + x.split(" :: ", 1)[1])
    fails = [x for x in results if x.startswith("FAIL")]
    assert not fails, f"以下断言失败: {fails}"
    print(f"   ✓ {len(results)} 条行为断言全部通过")


# ---------------------------------------------------------------- 4/4
@timeout(60)
def test_swipe_and_layout_untouched():
    """[4/4] 回归：滑动逻辑与页面布局未被改动 + 既有功能不回归"""
    print("\n[4/4] 滑动逻辑 / 页面布局 / 既有功能回归...")
    src = read(APP_JS)

    # 滑动（finishSwipe）方向语义必须原样保留：左滑 dx<0 去下一页，右滑去上一页
    swipe = extract_func(src, "finishSwipe")
    assert re.search(r"dx\s*<\s*0\s*\?\s*currentPage\s*\+\s*1\s*:\s*currentPage\s*-\s*1", swipe), \
        "滑动翻页方向语义被改动（应保持左滑 +1 / 右滑 -1）"
    assert "setPage(" in swipe, "滑动翻页未走 setPage"
    print("   ✓ 滑动翻页方向语义与保活路径未变（左滑下一页 / 右滑上一页）")

    # 纵向翻视频（滚轮/触摸）未受影响
    assert "vertAnimateTo" in src and "function vertFollow" in src, "纵向翻页逻辑丢失"
    print("   ✓ 纵向翻视频逻辑未受影响")

    # 页面布局：3 个 page + 结构不变
    html = read(INDEX_HTML)
    assert len(re.findall(r'<section class="page"', html)) == 3, "页面数量不再是 3"
    for eid in ("viewport", "pages", "mainFeed", "feed0", "feed2", "seekTrack"):
        assert f'id="{eid}"' in html, f"页面结构丢失 #{eid}"
    live_html = fetch("/video")
    assert len(re.findall(r'<section class="page"', live_html)) == 3, "线上页面结构异常"
    print("   ✓ 三页 DOM 结构与布局未改动")

    # 布局/样式文件本次不应被修改（进度条 100px 保持）
    css = read(STYLE_CSS)
    for sel in (".seek-track", ".seek-track::before", ".seek-fill"):
        block = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        assert block, f"CSS 缺少 {sel}"
        m = re.search(r"height\s*:\s*(\d+)px", block.group(1))
        assert m and int(m.group(1)) >= 100, f"{sel} 高度被改回: {block.group(1)}"
    print("   ✓ 进度条仍为 100px")

    # 既有功能回归
    live_js = fetch("/video/app.js")
    assert "function ensurePlaying" in live_js, "切页保活 ensurePlaying 丢失"
    assert "REVERSE_RATE" in live_js and "-REVERSE_RATE" in live_js, "-3x 倒放丢失"
    for s in ('data-speed="1"', 'data-speed="2"', 'data-speed="7"'):
        assert s in live_html, f"播放速度档位丢失: {s}"
    home = fetch("/")
    assert "HAS_SUBTLE" in home and "sha256HexJS" in home, "http 哈希兜底丢失"
    assert requests.get(BASE_URL + "/health", timeout=10).json()["status"] == "ok"
    assert os.path.exists(os.path.join(ROOT, "logs", "server.log")), "logs/server.log 挂载丢失"
    print("   ✓ 切页保活 / -3x 倒放 / 1-2-7 档位 / http 哈希兜底 / health / logs 挂载正常")


def main():
    print("=" * 60)
    print("任务测试：键盘左右方向键功能互换")
    print("=" * 60)
    for t in (
        test_source_arrow_swapped,
        test_live_arrow_swapped,
        test_behavior_arrow_swap,
        test_swipe_and_layout_untouched,
    ):
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
