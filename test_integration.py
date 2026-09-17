#!/usr/bin/env python3
"""
集成测试脚本 - 测试 obs 服务
超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import requests
from datetime import datetime

# 超时装饰器
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

BASE_URL = "http://127.0.0.1:80"
TEST_FILE = "/tmp/test_obs_file.txt"


def setup():
    """初始化测试环境"""
    print("=" * 60)
    print("OBS 集成测试")
    print("=" * 60)

    # 创建测试文件
    with open(TEST_FILE, 'w') as f:
        f.write(f"Test file created at {datetime.now().isoformat()}")
    print(f"[1/8] 创建测试文件: {TEST_FILE}")


def test_homepage():
    """测试首页"""
    print("\n[2/8] 测试首页...")
    try:
        resp = requests.get(BASE_URL + "/", timeout=5)
        assert resp.status_code == 200, f"首页返回状态码 {resp.status_code}"
        assert "文件托管" in resp.text, "首页缺少标题"
        print("   ✓ 首页正常")
    except Exception as e:
        print(f"   ✗ 首页测试失败: {e}")
        raise


def test_video_page():
    """测试视频页面"""
    print("\n[3/8] 测试视频页面...")
    try:
        resp = requests.get(BASE_URL + "/video", timeout=5)
        assert resp.status_code == 200, f"视频页面返回状态码 {resp.status_code}"
        assert "OBS 视频流" in resp.text or "video" in resp.text.lower(), "视频页面内容异常"
        print("   ✓ 视频页面正常")
    except Exception as e:
        print(f"   ✗ 视频页面测试失败: {e}")
        raise


def test_videos_api():
    """测试视频列表 API"""
    print("\n[4/8] 测试视频列表 API...")
    try:
        resp = requests.get(BASE_URL + "/videos", timeout=5)
        assert resp.status_code == 200, f"视频列表 API 返回状态码 {resp.status_code}"
        data = resp.json()
        assert "videos" in data, "视频列表响应缺少 videos 字段"
        for video in data["videos"][:1]:  # 只检查第一个
            assert "duration" in video, f"视频对象缺少 duration 字段: {video.keys()}"
        print(f"   ✓ 视频列表 API 正常, 视频数: {len(data['videos'])}")
        if data["videos"]:
            v = data["videos"][0]
            print(f"   ✓ 第一个视频 duration: {v.get('duration', 'N/A')} 秒")
    except Exception as e:
        print(f"   ✗ 视频列表 API 测试失败: {e}")
        raise


def test_video_static_resources():
    """测试视频静态资源"""
    print("\n[5/8] 测试视频静态资源...")
    resources = [
        "/video/style.css",
        "/video/app.js",
        "/video/vendor/hls.min.js"
    ]
    for res in resources:
        try:
            resp = requests.get(BASE_URL + res, timeout=5)
            assert resp.status_code == 200, f"{res} 返回状态码 {resp.status_code}"
            print(f"   ✓ {res} 正常")
        except Exception as e:
            print(f"   ✗ {res} 测试失败: {e}")
            raise


def test_reverse_playback_on_page0():
    """
    测试第一页（info 页）永远是 -3 倍速（倒放），且必须是「直接负速率倒放」实现。

    1) REVERSE_RATE = 3 常量存在
    2) 直接给 video.playbackRate 赋负值：video.playbackRate = -REVERSE_RATE
    3) 有负速率支持性探测（赋值 -1 后读回，抛异常则判定不支持），
       不支持时才退化为「冻结时间轴 + 定时向后 seek 3x」兜底
    4) currentPage === 0 分支调用 startReverse()，非第一页 stopReverse() + 受
       第三页「播放速度」UI（playbackSpeed）控制
    5) 倒到开头回到结尾，保持连续倒放
    """
    print("\n[6/8] 测试第一页 -3 倍速倒放实现...")
    try:
        resp = requests.get(BASE_URL + "/video/app.js", timeout=5)
        assert resp.status_code == 200, f"/video/app.js 返回状态码 {resp.status_code}"
        js = resp.text

        assert re.search(r"REVERSE_RATE\s*=\s*3\b", js), "缺少 REVERSE_RATE = 3 倒放常量"
        print("   ✓ 存在 -3 倍速常量 REVERSE_RATE = 3")

        # 核心：直接负速率倒放（不再用「正向 1x + 多退 4x」的绕法）
        assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), \
            "第一页没有直接设置 video.playbackRate = -REVERSE_RATE（负速率真倒放）"
        print("   ✓ 直接以负播放速率倒放：video.playbackRate = -3")

        assert re.search(r"function supportsNegativeRate\(\)", js), \
            "缺少负速率支持性探测函数"
        assert re.search(r"video\.playbackRate\s*=\s*-1\b", js), \
            "探测函数未尝试赋值 -1 以判断是否支持负速率"
        assert re.search(r"catch\s*\(_?\)\s*\{[\s\S]{0,120}?nativeReverse\s*=\s*false", js), \
            "赋值负数抛异常时未退化为兜底模式"
        print("   ✓ 支持负速率则原生倒放，不支持才退化兜底")

        m = re.search(r"function applyPagePlayback\(\)\s*\{(.*?)\n    \}", js, re.S)
        assert m, "未找到 applyPagePlayback 函数"
        body = m.group(1)

        assert re.search(r"currentPage\s*===\s*0\s*\)\s*\{[\s\S]*?startReverse\(\)", body), \
            "第一页分支没有调用 startReverse()"
        print("   ✓ 第一页分支调用 startReverse()（永远 -3x 倒放）")

        assert "stopReverse()" in body, "非第一页分支没有调用 stopReverse()"
        assert re.search(r"fastSpeed\s*\?\s*5\s*:\s*playbackSpeed", body), \
            "第二/三页未受第三页播放速度 UI（playbackSpeed）控制"
        print("   ✓ 非第一页调用 stopReverse()，且受第三页播放速度 UI 控制")

        # 兜底模式：时间轴冻结 + 定时向后 seek 精确 3 倍
        assert re.search(r"setInterval\(reverseTick", js), "兜底模式缺少 reverseTick 定时器"
        assert re.search(r"currentTime\s*-\s*REVERSE_RATE\s*\*\s*dt", js), \
            "兜底模式未按 REVERSE_RATE * dt 向后 seek"
        assert re.search(r"dur\s*-\s*0\.2", js), "倒到开头未回到结尾保持连续倒放"
        print("   ✓ 兜底模式冻结时间轴并按精确 3x 向后 seek，到开头回到结尾")
    except Exception as e:
        print(f"   ✗ 倒放实现测试失败: {e}")
        raise


def test_speed_options_only_137():
    """
    测试第三页「播放速度」UI 只保留 1x / 3x / 7x 三档。

    1) /video 页面 speed-options 里只有 3 个 .speed-btn
    2) data-speed 依次为 1 / 3 / 7（旧的 0.5/0.8/1.5/2/5 已移除）
    3) 默认档位 3x 带 active 高亮
    4) app.js 中 playbackSpeed 默认值为 3（与 UI 默认档一致）
    """
    print("\n[7/8] 测试播放速度档位只保留 1x/3x/7x...")
    try:
        resp = requests.get(BASE_URL + "/video", timeout=5)
        assert resp.status_code == 200, f"视频页面返回状态码 {resp.status_code}"
        html = resp.text

        btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
        assert btns == ["1", "3", "7"], f"播放速度档位应为 1/3/7，实际为 {btns}"
        print(f"   ✓ 播放速度档位: {btns}")

        for old in ["0.5", "0.8", "1.5", "2", "5"]:
            assert f'data-speed="{old}"' not in html, f"仍残留旧档位 data-speed=\"{old}\""
        print("   ✓ 已移除 0.5x/0.8x/1.5x/2x/5x 旧档位")

        assert re.search(r'class="speed-btn active"[^>]*data-speed="3"', html), \
            "默认档位 3x 未标记 active 高亮"
        print("   ✓ 默认档位 3x 已高亮")

        js = requests.get(BASE_URL + "/video/app.js", timeout=5).text
        assert re.search(r"let\s+playbackSpeed\s*=\s*3\b", js), \
            "app.js 中 playbackSpeed 默认值不是 3"
        print("   ✓ playbackSpeed 默认值为 3，与 UI 默认档一致")
    except Exception as e:
        print(f"   ✗ 播放速度档位测试失败: {e}")
        raise


def test_upload():
    """测试文件上传"""
    print("\n[8/8] 测试文件上传...")
    try:
        with open(TEST_FILE, 'rb') as f:
            resp = requests.put(
                BASE_URL + "/test_upload.txt",
                data=f,
                timeout=10
            )
        assert resp.status_code == 201, f"上传返回状态码 {resp.status_code}"
        print("   ✓ 文件上传正常")

        # 验证文件存在
        resp = requests.get(BASE_URL + "/obs/test_upload.txt", timeout=5)
        assert resp.status_code == 200, "上传的文件无法访问"
        print("   ✓ 上传文件可访问")

        # 清理测试文件
        requests.delete(BASE_URL + "/obs/test_upload.txt", timeout=5)
        print("   ✓ 测试文件已清理")
    except Exception as e:
        print(f"   ✗ 文件上传测试失败: {e}")
        raise


def cleanup():
    """清理测试环境"""
    if os.path.exists(TEST_FILE):
        os.remove(TEST_FILE)


@timeout(60)
def main():
    setup()
    try:
        test_homepage()
        test_video_page()
        test_videos_api()
        test_video_static_resources()
        test_reverse_playback_on_page0()
        test_speed_options_only_137()
        test_upload()
        print("\n" + "=" * 60)
        print("所有测试通过!")
        print("=" * 60)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"测试失败: {e}")
        print("=" * 60)
        sys.exit(1)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
