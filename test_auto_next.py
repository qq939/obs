#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  视频播放完成（ended）自动切换到下一个视频。

超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
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


def test_loop_disabled():
    """[1/4] video.loop 必须为 false，否则不会触发 ended"""
    print("\n[1/4] video.loop = false...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    assert re.search(r"video\.loop\s*=\s*false", js), \
        "video.loop 必须为 false，否则播放完成只循环、不会触发 ended"
    assert not re.search(r"video\.loop\s*=\s*true", js), "仍残留 video.loop = true"
    print("   ✓ video.loop = false")


def test_ended_listener():
    """[2/4] 存在 video 的 ended 监听，且明确切换下一个视频"""
    print("\n[2/4] ended 监听...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()

    m = re.search(r"video\.addEventListener\('ended',\s*\(\)\s*=>\s*\{(.*?)\n    \}\);", js, re.S)
    assert m, "缺少 video 的 ended 监听"
    body = m.group(1)

    assert re.search(r"videos\.length\s*===\s*0", body), "ended 未处理视频列表为空的情况"
    print("   ✓ 已绑定 ended 监听并做空列表保护")

    assert re.search(r"vertAnimateTo\(vertBaseTop\s*-\s*h,\s*1\)", body), \
        "ended 后未走上滑（下一个视频）同款吸附动画路径"
    assert re.search(r"feeds\[1\]\.clientHeight", body), "ended 未按 feed 高度计算翻页距离"
    print("   ✓ ended -> vertAnimateTo 上滑切下一个（与手动上滑同一条路径）")

    assert re.search(r"if\s*\(vertAnim\)\s*return", body), \
        "ended 未防重入：吸附动画进行中可能被重复触发"
    print("   ✓ 吸附动画进行中防重入")
    return body


def test_reverse_not_broken():
    """[3/4] 第一页 -3x 倒放走到开头触发 ended 时不能卡住（保活机制）"""
    print("\n[3/4] 第一页倒放场景保护...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"video\.addEventListener\('ended',\s*\(\)\s*=>\s*\{(.*?)\n    \}\);", js, re.S)
    assert m, "缺少 ended 监听"
    body = m.group(1)

    assert re.search(r"reverseActive", body), \
        "ended 未区分倒放状态：第一页负速率走到开头会触发 ended，不做处理会卡住不动"
    assert re.search(r"startReverse\(\)", body), "ended 倒放分支未重新进入倒放"
    assert re.search(r"video\.play\(\)\.catch", body), "ended 倒放分支未续播（保活）"
    assert re.search(r"video\.currentTime\s*=\s*Math\.max\(0,\s*dur\s*-\s*0\.2\)", body), \
        "ended 倒放分支未跳回结尾，倒放无法继续"
    print("   ✓ 倒放分支：跳回结尾 + 重新倒放 + 续播，视频不会停")


def test_live_and_regression():
    """[4/4] 线上下发一致 + 回归（-3x 倒放 / 档位 1-2-7 / 保活）"""
    print("\n[4/4] 线上校验与回归...")
    js = requests.get(BASE_URL + "/video/app.js", timeout=5).text
    assert re.search(r"video\.loop\s*=\s*false", js), "线上 app.js 未生效 video.loop=false"
    assert re.search(r"video\.addEventListener\('ended'", js), "线上 app.js 缺少 ended 监听"
    print("   ✓ 线上 app.js 已下发自动切下一个实现")

    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页 -3x 倒放丢失"
    assert re.search(r"const\s+REVERSE_RATE\s*=\s*3\b", js), "REVERSE_RATE 应为 3"
    print("   ✓ 第一页 -3x 倒放回归正常")

    html = requests.get(BASE_URL + "/video", timeout=5).text
    btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
    assert btns == ["1", "2", "7"], f"播放速度档位应为 1/2/7，实际 {btns}"
    active = re.findall(r'class="speed-btn active"[^>]*data-speed="([^"]+)"', html)
    assert active == ["1"], f"默认档位应为 1x，实际 {active}"
    print(f"   ✓ 档位 {btns}，默认 {active[0]}x 回归正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：播放完成自动切换到下一个视频")
    print("=" * 60)
    try:
        test_loop_disabled()
        test_ended_listener()
        test_reverse_not_broken()
        test_live_and_regression()
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
