#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  1) 视频页两个文件（video_static/index.html、video_static/app.js）
     回退到 commit 95d2d7df81bab5415584e9e335f80ef2f852eef6 的内容；
  2) 在回退后的基础上，第三页「播放速度」档位设为 1x / 2x / 7x，默认 1x；
  3) 非视频改动（url_head、/health、首页拖拽上传区）必须保留。

超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import subprocess
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
INDEX_HTML = os.path.join(ROOT, "src", "obs", "video_static", "index.html")
BASE_URL = "http://127.0.0.1:80"

RESET_COMMIT = "95d2d7df81bab5415584e9e335f80ef2f852eef6"
OPTIONS = ["1", "2", "7"]
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


def git_show(path):
    """读取指定 commit 中的文件内容"""
    out = subprocess.check_output(
        ["git", "show", f"{RESET_COMMIT}:{path}"], cwd=ROOT, stderr=subprocess.STDOUT)
    return out.decode("utf-8")


def test_video_files_reverted():
    """[1/4] 视频页两文件已回退到 95d2d7d（仅档位相关处允许不同）"""
    print(f"\n[1/4] 视频页文件回退到 {RESET_COMMIT[:7]}...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    base_js = git_show("src/obs/video_static/app.js")

    # 回退特征：loop 恢复 true，auto-next 的 ended 监听消失
    assert re.search(r"video\.loop\s*=\s*true", js), "app.js 未回退（video.loop 应为 true）"
    assert "addEventListener('ended'" not in js, "app.js 未回退（仍残留 ended 自动切下一个监听）"
    print("   ✓ app.js 已回到 95d2d7d（loop=true、无 ended 自动切下一个）")

    # 逐行比对：除 playbackSpeed 注释行外应与 95d2d7d 完全一致
    cur_lines, base_lines = js.splitlines(), base_js.splitlines()
    assert len(cur_lines) == len(base_lines), \
        f"app.js 行数与 95d2d7d 不一致（当前 {len(cur_lines)} vs {len(base_lines)}）"
    diffs = [(i + 1, b, c) for i, (b, c) in enumerate(zip(base_lines, cur_lines)) if b != c]
    assert len(diffs) <= 1, f"app.js 与 95d2d7d 差异过多: {diffs[:5]}"
    for lineno, b, c in diffs:
        assert "playbackSpeed" in b and "playbackSpeed" in c, f"第 {lineno} 行非预期差异: {b!r} -> {c!r}"
    print(f"   ✓ app.js 与 95d2d7d 逐行一致（仅 {len(diffs)} 处档位注释差异）")

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()
    # 回退特征：8 档里的 0.5/0.8/1.5 应当回来过（现已改档位），这里校验结构块来源一致
    assert 'id="speedOptions"' in html and "<span>播放速度</span>" in html, "index.html 缺少播放速度 UI"
    print("   ✓ index.html 播放速度 UI 结构完好")


def test_speed_options_127_live():
    """[2/4] 线上 /video：档位为 1x / 2x / 7x，默认高亮 1x"""
    print("\n[2/4] 线上档位 = 1x/2x/7x，默认 1x...")
    html = requests.get(BASE_URL + "/video", timeout=5).text
    btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
    assert btns == OPTIONS, f"档位应为 {OPTIONS}，实际为 {btns}"
    print(f"   ✓ 档位集合 {btns}")

    active = re.findall(r'class="speed-btn active"[^>]*data-speed="([^"]+)"', html)
    assert active == [DEFAULT], f"默认高亮应为唯一 {DEFAULT}x，实际 {active}"
    print(f"   ✓ 默认高亮档位 {active[0]}x")

    for old in ["0.5", "0.8", "1.5", "3", "5"]:
        assert f'data-speed="{old}"' not in html, f"仍残留旧档位 data-speed=\"{old}\""
    print("   ✓ 已移除 0.5x/0.8x/1.5x/3x/5x 旧档位")


def test_source_and_default_consistency():
    """[3/4] index.html / app.js 源码档位一致，默认值 1 与 UI 高亮一致"""
    print("\n[3/4] 源码档位与默认值一致性...")
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        src = f.read()
    btns = re.findall(r'data-speed="([^"]+)"', src)
    assert btns == OPTIONS, f"index.html 档位应为 {OPTIONS}，实际为 {btns}"
    assert re.search(r'class="speed-btn active"\s+data-speed="1"', src), "1x 未标记 active"
    print(f"   ✓ index.html 档位 {btns}，1x 为默认高亮")

    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"let\s+playbackSpeed\s*=\s*([\d.]+)", js)
    assert m, "app.js 中缺少 playbackSpeed 定义"
    assert m.group(1) == DEFAULT, f"playbackSpeed 默认值应为 {DEFAULT}，实际 {m.group(1)}"
    assert re.search(r"1x\s*/\s*2x\s*/\s*7x", js), "app.js 档位注释未更新为 1x / 2x / 7x"
    assert re.search(r"playbackSpeed\s*=\s*parseFloat\(btn\.dataset\.speed\)", js), \
        "点击档位后未把 data-speed 赋给 playbackSpeed"
    print("   ✓ playbackSpeed 默认 1，注释已更新，点击链路完好")


def test_non_video_changes_kept():
    """[4/4] 非视频改动保留：url_head、/health、首页拖拽上传区、-3x 倒放回归"""
    print("\n[4/4] 非视频改动保留 + 回归...")
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        srv = f.read()
    assert re.search(r'^url_head\s*=\s*"http://obs\.dimond\.top"', srv, re.M), "url_head 丢失"
    assert re.search(r'@app\.get\("/health"\)', srv), "/health 路由丢失"
    print("   ✓ url_head / /health 保留")

    assert requests.get(BASE_URL + "/health", timeout=5).json().get("status") == "ok", "/health 异常"
    home = requests.get(BASE_URL + "/", timeout=5).text
    assert 'id="uploadZone"' in home, "首页拖拽上传区丢失"
    print("   ✓ /health 正常，首页拖拽上传区保留")

    js = requests.get(BASE_URL + "/video/app.js", timeout=5).text
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页 -3x 倒放丢失"
    assert re.search(r"const\s+REVERSE_RATE\s*=\s*3\b", js), "REVERSE_RATE 应为 3"
    assert re.search(r"video\.play\(\)\.catch", js), "缺少保活 play 调用"
    print("   ✓ 第一页 -3x 倒放 / 保活 回归正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：视频页回退 95d2d7d + 档位改为 1x/2x/7x（默认 1x）")
    print("=" * 60)
    try:
        test_video_files_reverted()
        test_speed_options_127_live()
        test_source_and_default_consistency()
        test_non_video_changes_kept()
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
