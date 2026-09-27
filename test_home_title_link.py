#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  首页顶部的「文件托管服务」标题要可以点击，点击后跳转到 /video 页面。

说明：当前首页只有浏览器标签的 <title>文件托管服务</title>，
     页面可见区域没有该标题元素；本任务在 <body> 内新增可见标题并做成链接。

超时机制: 每个用例 60 秒超时
"""
import os
import re
import signal

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
BASE_URL = "http://127.0.0.1:80"


def timeout(seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds}s")
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                return func(*args, **kwargs)
            finally:
                signal.alarm(0)
        return wrapper
    return decorator


def read(path: str = SERVER_PY) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def homepage() -> str:
    return requests.get(f"{BASE_URL}/", timeout=30).text


# ---------------------------------------------------------------- 1/3
@timeout(60)
def test_source_title_link():
    """[1/3] 源码：body 内有可见标题「文件托管服务」，且链接指向 /video"""
    print("\n[1/3] 源码：标题元素与跳转链接...")
    src = read()

    body_i = src.index("<body>")
    # 标题必须在 body 内（不能只是 <head> 里的 <title>）
    visible = re.search(r"<h1[^>]*>\s*<a[^>]*href=\"/video\"[^>]*>\s*文件托管服务\s*</a>\s*</h1>", src[body_i:])
    assert visible, "body 内缺少可点击的「文件托管服务」标题（<h1><a href=\"/video\">…</a></h1>）"
    print("   ✓ body 内有 <h1><a href=\"/video\">文件托管服务</a></h1>")

    # 标题样式：可点击但不该是默认蓝色下划线（保持标题观感）
    assert re.search(r"\.site-title\b", src), "缺少 .site-title 标题样式类"
    link_css = re.search(r"\.site-title\s+a[^{]*\{([^}]*)\}", src)
    assert link_css, "缺少 .site-title a 链接样式"
    assert re.search(r"color\s*:", link_css.group(1)), ".site-title a 未定义颜色"
    assert re.search(r"text-decoration\s*:\s*none", link_css.group(1)), \
        ".site-title a 未去掉默认下划线（不像标题）"
    print("   ✓ 标题有 .site-title 样式（保持标题观感，非默认蓝链接）")

    # 别把公告板等既有内容挤掉
    assert 'class="notice-board"' in src, "公告板模块丢失"
    print("   ✓ 公告板等既有内容仍在")


# ---------------------------------------------------------------- 2/3
@timeout(60)
def test_page_serves_clickable_title():
    """[2/3] HTTP：首页真实下发可点击标题，且 /video 可达"""
    print("\n[2/3] 页面下发与目标可达...")
    html = homepage()
    body = html[html.index("<body>"):]
    m = re.search(r"<h1[^>]*>\s*<a[^>]*href=\"/video\"[^>]*>\s*文件托管服务\s*</a>\s*</h1>", body)
    assert m, "首页未下发可点击的「文件托管服务」标题"
    print(f"   ✓ 首页下发标题链接：{m.group(0)[:70]}")

    r = requests.get(f"{BASE_URL}/video", timeout=30)
    assert r.status_code == 200, f"/video 返回 {r.status_code}"
    assert "OBS" in r.text or "viewport" in r.text, "/video 内容异常"
    print("   ✓ /video 可访问（200）")

    # 标题链接不能在列表区域乱入
    assert html.count("文件托管服务") <= 3, "「文件托管服务」出现次数异常"
    print("   ✓ 标题未重复插入")


# ---------------------------------------------------------------- 3/3
@timeout(60)
def test_regression():
    """[3/3] 回归：首页既有功能与接口不受影响"""
    print("\n[3/3] 回归...")
    r = requests.get(f"{BASE_URL}/", timeout=30)
    assert r.status_code == 200, f"首页返回 {r.status_code}"
    html = r.text
    for needle, who in [('id="notice-content"', "公告板"),
                        ("sort=time", "排序"),
                        ('id="uploadZone"', "上传区"),
                        ("addEventListener('paste'", "粘贴上传"),
                        ("function uploadFiles(", "上传入口"),
                        ('id="ws-status-indicator"', "WebSocket 状态灯")]:
        assert needle in html, f"首页缺少{who}"
    assert requests.get(f"{BASE_URL}/health", timeout=20).status_code == 200
    assert requests.get(f"{BASE_URL}/videos", timeout=60).status_code == 200
    assert requests.get(f"{BASE_URL}/video/app.js", timeout=20).status_code == 200
    print("   ✓ 公告板/排序/上传/粘贴/health/videos/video 均正常")


if __name__ == "__main__":
    tests = [
        test_source_title_link,
        test_page_serves_clickable_title,
        test_regression,
    ]
    print("=" * 60)
    print("任务测试：首页标题「文件托管服务」点击跳转 /video")
    print("=" * 60)
    for t in tests:
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)
