#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务（与左右方向键互换同批）：
  第三页（设置页）的「播放速度」改为竖向排布：
    「播放速度」四个字占一行；
    「1x」占一行；
    「2x」占一行；
    「7x」占一行。
  即 .speed-row 变成纵向（标签在上、档位在下），.speed-options 也纵向、
  每个档位按钮各占一整行（满宽）。

超时机制: 每个用例 60 秒超时
"""
import os
import re
import signal

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STYLE_CSS = os.path.join(ROOT, "src", "obs", "video_static", "style.css")
INDEX_HTML = os.path.join(ROOT, "src", "obs", "video_static", "index.html")
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


def css_block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"找不到 CSS 规则: {selector}"
    return m.group(1)


def assert_speed_vertical(css: str, source: str):
    """播放速度标签与档位都竖向排列，每个档位各占一行"""
    speed_row = css_block(css, ".speed-row")
    assert re.search(r"flex-direction\s*:\s*column", speed_row), \
        f"{source} .speed-row 未改为纵向（flex-direction: column）: {speed_row.strip()}"
    assert not re.search(r"justify-content\s*:\s*space-between", speed_row), \
        f"{source} .speed-row 仍保留横向 space-between: {speed_row.strip()}"

    opts = css_block(css, ".speed-options")
    assert re.search(r"flex-direction\s*:\s*column", opts), \
        f"{source} .speed-options 未改为纵向（每个档位各占一行）: {opts.strip()}"
    assert not re.search(r"flex-wrap\s*:\s*wrap", opts), \
        f"{source} .speed-options 仍允许 wrap 横排: {opts.strip()}"

    # 每个档位按钮占满整行
    btn = css_block(css, ".speed-btn")
    assert re.search(r"width\s*:\s*100%", btn), \
        f"{source} .speed-btn 未设置 width:100%（未占满一行）: {btn.strip()}"


# ---------------------------------------------------------------- 1/4
@timeout(60)
def test_source_speed_vertical():
    """[1/4] 源码：播放速度改成竖向（标签一行 + 每个档位一行）"""
    print("\n[1/4] 源码播放速度竖向排布...")
    assert_speed_vertical(read(STYLE_CSS), "源码")
    print("   ✓ .speed-row / .speed-options 纵向，.speed-btn 满宽")


# ---------------------------------------------------------------- 2/4
@timeout(60)
def test_live_speed_vertical():
    """[2/4] 线上 /video/style.css 已下发竖向样式"""
    print("\n[2/4] 线上 style.css 竖向排布...")
    assert_speed_vertical(fetch("/video/style.css"), "线上")
    print("   ✓ 线上 style.css 播放速度为竖向")


# ---------------------------------------------------------------- 3/4
@timeout(60)
def test_speed_dom_unchanged():
    """[3/4] DOM 结构不变：仍是一个「播放速度」标签 + 1x/2x/7x 三个档位"""
    print("\n[3/4] 档位 DOM 与档位值...")
    html = read(INDEX_HTML)
    m = re.search(r'<div class="setting-row speed-row">(.*?)</div>\s*</div>', html, re.S)
    assert m, "找不到 speed-row 结构"
    row = m.group(1)
    assert re.search(r"<span>\s*播放速度\s*</span>", row), "「播放速度」标签丢失或文字改变"
    live = fetch("/video")
    assert re.search(r"<span>\s*播放速度\s*</span>", live), "线上「播放速度」标签丢失"
    for s in ('data-speed="1"', 'data-speed="2"', 'data-speed="7"'):
        assert s in live, f"档位丢失: {s}"
    assert re.search(r'data-speed="1"[^>]*>\s*1x', live), "1x 档位文案异常"
    assert re.search(r'data-speed="2"[^>]*>\s*2x', live), "2x 档位文案异常"
    assert re.search(r'data-speed="7"[^>]*>\s*7x', live), "7x 档位文案异常"
    print("   ✓ 「播放速度」+ 1x/2x/7x 三档结构不变")


# ---------------------------------------------------------------- 4/4
@timeout(60)
def test_regression():
    """[4/4] 回归：左右键互换仍在、滑动/布局/进度条/保活/倒放/上传兜底不回归"""
    print("\n[4/4] 回归...")
    js = fetch("/video/app.js")
    # 同批任务：左右方向键已互换
    left = re.search(r"case\s+'ArrowLeft'\s*:(.*?)break;", js, re.S)
    right = re.search(r"case\s+'ArrowRight'\s*:(.*?)break;", js, re.S)
    assert left and re.search(r"setPage\(currentPage\s*-\s*1\)", left.group(1)), "ArrowLeft 未互换"
    assert right and re.search(r"setPage\(currentPage\s*\+\s*1\)", right.group(1)), "ArrowRight 未互换"
    print("   ✓ 左右方向键互换保持")

    # 滑动逻辑语义未动
    swipe = re.search(r"function finishSwipe\([\s\S]*?\n    \}", js)
    assert swipe and re.search(r"dx\s*<\s*0\s*\?\s*currentPage\s*\+\s*1\s*:\s*currentPage\s*-\s*1", swipe.group(0)), \
        "滑动翻页方向语义被改动"
    assert "setPage(" in swipe.group(0), "滑动翻页未走 setPage"
    print("   ✓ 滑动逻辑未被改动")

    html = fetch("/video")
    assert len(re.findall(r'<section class="page"', html)) == 3, "页面布局被改动"
    css = fetch("/video/style.css")
    for sel in (".seek-track", ".seek-track::before", ".seek-fill"):
        b = css_block(css, sel)
        hm = re.search(r"height\s*:\s*(\d+)px", b)
        assert hm and int(hm.group(1)) >= 100, f"{sel} 高度被改回"
    assert "function ensurePlaying" in js, "切页保活丢失"
    assert "REVERSE_RATE" in js, "-3x 倒放丢失"
    home = fetch("/")
    assert "HAS_SUBTLE" in home and "sha256HexJS" in home, "http 哈希兜底丢失"
    assert requests.get(BASE_URL + "/health", timeout=10).json()["status"] == "ok"
    assert os.path.exists(os.path.join(ROOT, "logs", "server.log")), "logs 挂载丢失"
    print("   ✓ 布局/进度条保活/倒放/上传兜底/health/logs 全部正常")


def main():
    print("=" * 60)
    print("任务测试：第三页播放速度竖向排布")
    print("=" * 60)
    for t in (
        test_source_speed_vertical,
        test_live_speed_vertical,
        test_speed_dom_unchanged,
        test_regression,
    ):
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
