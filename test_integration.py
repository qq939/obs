#!/usr/bin/env python3
"""
集成测试脚本 - 测试 obs 服务
超时机制: 30秒超时
"""
import os
import sys
import time
import json
import signal
import subprocess
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
    print(f"[1/6] 创建测试文件: {TEST_FILE}")

def test_homepage():
    """测试首页"""
    print("\n[2/6] 测试首页...")
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
    print("\n[3/6] 测试视频页面...")
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
    print("\n[4/6] 测试视频列表 API...")
    try:
        resp = requests.get(BASE_URL + "/videos", timeout=5)
        assert resp.status_code == 200, f"视频列表 API 返回状态码 {resp.status_code}"
        data = resp.json()
        assert "videos" in data, "视频列表响应缺少 videos 字段"
        # 检查视频对象是否包含 duration 字段
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
    print("\n[5/6] 测试视频静态资源...")
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

def test_upload():
    """测试文件上传"""
    print("\n[6/6] 测试文件上传...")
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

def main():
    setup()
    try:
        test_homepage()
        test_video_page()
        test_videos_api()
        test_video_static_resources()
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
