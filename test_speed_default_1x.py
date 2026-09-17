#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  第三页「播放速度」默认档位改为 1x（不再是 3x）。
  档位集合仍为 1x / 3x / 7x。

超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
INDEX_HTML = os.path.join(ROOT, "src", "obs", "video_static", "index.html")
BASE_URL = "http://127.0.0.1:80"

OPTIONS = ["1", "3", "7"]
DEFAULT = "1"


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


def test_default_speed_is_1x_live():
    """[1/4] 线上 /video：默认高亮档位为 1x"""
    print("\n[1/4] 线上默认档位 = 1x...")
    html = requests.get(BASE_URL + "/video", timeout=5).text

    btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
    assert btns == OPTIONS, f"档位集合应为 {OPTIONS}，实际为 {btns}"
    print(f"   ✓ 档位集合 {btns}")

    active = re.findall(r'class="speed-btn active"[^>]*data-speed="([^"]+)"', html)
    assert len(active) == 1, f"应有且仅有 1 个默认高亮档位，实际 {active}"
    assert active[0] == DEFAULT, f"默认档位应为 {DEFAULT}x，实际高亮 {active[0]}x"
    print(f"   ✓ 默认高亮档位 {active[0]}x")

    assert re.search(r'class="speed-btn active"[^>]*data-speed="1"[^>]*>\s*1x\s*<', html), \
        "1x 按钮未带 active 高亮（或文案不匹配）"
    print("   ✓ 高亮落在 1x 按钮上")


def test_app_js_default_is_1():
    """[2/4] app.js 的 playbackSpeed 默认值为 1，与 UI 高亮一致"""
    print("\n[2/4] app.js 默认值 = 1...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"let\s+playbackSpeed\s*=\s*([\d.]+)", js)
    assert m, "app.js 中缺少 playbackSpeed 定义"
    assert m.group(1) == DEFAULT, f"playbackSpeed 默认值应为 {DEFAULT}，实际 {m.group(1)}"
    print(f"   ✓ let playbackSpeed = {m.group(1)}")

    assert not re.search(r"let\s+playbackSpeed\s*=\s*3\b", js), \
        "app.js 中仍残留旧的 3x 默认值"
    print("   ✓ 旧的 3x 默认值已移除")


def test_index_html_default_is_1():
    """[3/4] index.html 源码：1x 带 active，3x 不再 active"""
    print("\n[3/4] index.html 默认高亮 = 1x...")
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        src = f.read()
    btns = re.findall(r'data-speed="([^"]+)"', src)
    assert btns == OPTIONS, f"index.html 档位应为 {OPTIONS}，实际为 {btns}"
    assert re.search(r'class="speed-btn active"\s+data-speed="1"', src), "1x 未标记 active"
    assert not re.search(r'class="speed-btn active"\s+data-speed="3"', src), \
        "3x 仍被标记为 active（默认档位没换过来）"
    print("   ✓ 1x 为默认高亮，3x 已取消高亮")

    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    assert re.search(r"playbackSpeed\s*=\s*parseFloat\(btn\.dataset\.speed\)", js), \
        "点击档位后未把 data-speed 赋给 playbackSpeed"
    print("   ✓ 点击切换档位逻辑保持完好")


def test_video_page_regression():
    """[4/4] 回归：-3x 第一页倒放 / 保活 / 播放完成自动切下一个"""
    print("\n[4/4] 视频页回归...")
    assert requests.get(BASE_URL + "/video", timeout=5).status_code == 200, "/video 回归失败"
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页未负速率倒放"
    assert re.search(r"const\s+REVERSE_RATE\s*=\s*3\b", js), "第一页倒放倍速应为 3x，不受默认档位影响"
    assert re.search(r"currentPage\s*===\s*0\s*\)\s*\{[\s\S]*?startReverse\(\)", js), "第一页未走 startReverse()"
    assert re.search(r"video\.play\(\)\.catch", js), "缺少保活 play 调用"
    assert re.search(r"video\.loop\s*=\s*false", js), "video.loop 应为 false（自动切下一个）"
    print("   ✓ -3x 倒放 / 保活 / 自动切下一个 均正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：播放速度默认档位改为 1x")
    print("=" * 60)
    try:
        test_default_speed_is_1x_live()
        test_app_js_default_is_1()
        test_index_html_default_is_1()
        test_video_page_regression()
        print("\n" + "=" * 60)
        print("所有测试通过!")
        print("=" * 60)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"测试失败: {e}")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
