#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  第三页「播放速度」UI 的档位改为 1x / 3x / 7x（替换原来的 3x / 5x / 7x）。

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

EXPECTED = ["1", "3", "7"]


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


def test_speed_options_are_137():
    """[1/4] 服务端下发的 /video 只有 1x / 3x / 7x 三档"""
    print("\n[1/4] /video 播放速度档位 = 1x/3x/7x...")
    html = requests.get(BASE_URL + "/video", timeout=5).text
    btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
    assert btns == EXPECTED, f"播放速度档位应为 {EXPECTED}，实际为 {btns}"
    assert len(re.findall(r'class="speed-btn', html)) == 3, "档位按钮数量应为 3"
    print(f"   ✓ 档位为 {btns}，共 3 个按钮")

    labels = re.findall(r'class="speed-btn[^"]*"[^>]*data-speed="([^"]+)"[^>]*>([^<]+)<', html)
    for speed, text in labels:
        assert text.strip() == f"{speed}x", f"按钮文案与档位不一致: data-speed={speed} 文案={text}"
    print("   ✓ 按钮文案与 data-speed 一一对应")

    assert 'data-speed="5"' not in html, "旧的 5x 档位仍残留在页面上"
    print("   ✓ 旧 5x 档位已移除")


def test_default_speed_in_range():
    """[2/4] 默认档位在 1/3/7 之内，且 UI 高亮与 app.js 默认值一致"""
    print("\n[2/4] 默认档位一致性...")
    html = requests.get(BASE_URL + "/video", timeout=5).text
    active = re.findall(r'class="speed-btn active"[^>]*data-speed="([^"]+)"', html)
    assert len(active) == 1, f"应有且仅有 1 个默认高亮档位，实际 {active}"
    assert active[0] in EXPECTED, f"默认高亮档位 {active[0]} 不在 {EXPECTED} 中"

    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"let\s+playbackSpeed\s*=\s*([\d.]+)", js)
    assert m, "app.js 中缺少 playbackSpeed 定义"
    assert m.group(1) == active[0], \
        f"UI 高亮 {active[0]}x 与 app.js 默认 {m.group(1)}x 不一致"
    print(f"   ✓ 默认档位 {active[0]}x，UI 高亮与 app.js 默认值一致")


def test_source_files_only_137():
    """[3/4] 源码层面档位只声明 1/3/7，且点击逻辑仍按 data-speed 生效"""
    print("\n[3/4] 源码档位与点击逻辑...")
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()
    btns = re.findall(r'data-speed="([^"]+)"', html)
    assert btns == EXPECTED, f"index.html 档位应为 {EXPECTED}，实际为 {btns}"
    print(f"   ✓ index.html 档位 {btns}")

    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    assert re.search(r"playbackSpeed\s*=\s*parseFloat\(btn\.dataset\.speed\)", js), \
        "点击档位后未把 data-speed 赋给 playbackSpeed"
    assert re.search(r"video\.playbackRate\s*=\s*playbackSpeed", js), \
        "未把 playbackSpeed 应用到 video.playbackRate"
    print("   ✓ 点击档位 -> playbackSpeed -> video.playbackRate 链路完好")


def test_video_page_regression():
    """[4/4] 回归：-3x 第一页倒放 / 保活 / 播放完成自动切下一个"""
    print("\n[4/4] 视频页回归...")
    assert requests.get(BASE_URL + "/video", timeout=5).status_code == 200, "/video 回归失败"
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页未负速率倒放"
    assert re.search(r"currentPage\s*===\s*0\s*\)\s*\{[\s\S]*?startReverse\(\)", js), "第一页未走 startReverse()"
    assert re.search(r"video\.play\(\)\.catch", js), "缺少保活 play 调用"
    assert re.search(r"video\.loop\s*=\s*false", js), "video.loop 应为 false（自动切下一个）"
    print("   ✓ -3x 倒放 / 保活 / 自动切下一个 均正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：播放速度档位改为 1x / 3x / 7x")
    print("=" * 60)
    try:
        test_speed_options_are_137()
        test_default_speed_in_range()
        test_source_files_only_137()
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
