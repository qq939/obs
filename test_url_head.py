#!/usr/bin/env python3
"""
TDD 测试脚本 - obs server 抽象 url_head 全局参数
需求：src/obs/server.py 第一行抽象出 url_head，用于渲染替换 http://obs.dimond.top
      的文字和下载前缀。
超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import requests

SERVER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "obs", "server.py")
BASE_URL = "http://127.0.0.1:80"
URL_HEAD = "http://obs.dimond.top"
TEST_NAME = "test_url_head_tmp.txt"


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


def test_source_url_head_defined_at_top():
    """1) server.py 最上边（前 10 行内）定义 url_head 全局参数"""
    print("\n[1/4] 检查 url_head 是否定义在 server.py 第一行区域...")
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    idx = None
    for i, line in enumerate(lines[:10]):
        if re.match(r'^url_head\s*=\s*"http://obs\.dimond\.top"\s*$', line):
            idx = i + 1
            break
    assert idx is not None, f"server.py 前 10 行内未找到 url_head = \"{URL_HEAD}\" 定义"
    assert re.search(r"使用位置", "\n".join(lines[:12])), "url_head 注释未标注具体使用位置"
    print(f"   ✓ 第 {idx} 行定义 url_head，且注释标注了使用位置")


def test_source_no_hardcoded_url_left():
    """2) 源码中除定义行外，不再残留硬编码 http://obs.dimond.top"""
    print("\n[2/4] 检查源码中是否还有硬编码 URL...")
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    leftovers = [
        (i + 1, line.strip())
        for i, line in enumerate(lines)
        if URL_HEAD in line and not re.match(r'^url_head\s*=', line)
    ]
    assert not leftovers, f"仍残留硬编码 URL: {leftovers}"
    print("   ✓ 无残留硬编码 URL，全部改为引用 url_head")


def test_homepage_renders_url_head():
    """3) 首页 curl 示例与文件下载前缀由 url_head 渲染"""
    print("\n[3/4] 检查首页渲染的 URL...")
    resp = requests.get(BASE_URL + "/", timeout=5)
    assert resp.status_code == 200, f"首页返回状态码 {resp.status_code}"
    html = resp.text

    assert f"curl --upload-file file.txt {URL_HEAD}/file.txt" in html, \
        "首页 curl 上传示例未使用 url_head"
    print("   ✓ 首页 curl 示例使用 url_head")

    # 上传一个文件，确认列表中的下载前缀为 url_head
    body = b"url_head tdd test"
    try:
        up = requests.put(f"{BASE_URL}/{TEST_NAME}", data=body, timeout=10)
        assert up.status_code == 201, f"PUT 上传返回状态码 {up.status_code}"

        html = requests.get(BASE_URL + "/", timeout=5).text
        assert f'href="{URL_HEAD}/{TEST_NAME}"' in html, "首页文件下载链接前缀不是 url_head"
        print("   ✓ 首页文件下载链接前缀为 url_head")
    finally:
        requests.delete(f"{BASE_URL}/{TEST_NAME}", timeout=5)


def test_upload_response_uses_url_head():
    """4) PUT 上传响应体返回 url_head 前缀的完整 URL"""
    print("\n[4/4] 检查 PUT 上传响应 URL...")
    try:
        resp = requests.put(f"{BASE_URL}/{TEST_NAME}", data=b"url_head tdd", timeout=10)
        assert resp.status_code == 201, f"PUT 上传返回状态码 {resp.status_code}"
        assert resp.text == f"{URL_HEAD}/{TEST_NAME}", \
            f"上传响应 URL 不等于 url_head 前缀: {resp.text}"
        print(f"   ✓ 上传响应: {resp.text}")
    finally:
        requests.delete(f"{BASE_URL}/{TEST_NAME}", timeout=5)


@timeout(60)
def main():
    print("=" * 60)
    print("url_head 抽象测试")
    print("=" * 60)
    try:
        test_source_url_head_defined_at_top()
        test_source_no_hardcoded_url_left()
        test_homepage_renders_url_head()
        test_upload_response_uses_url_head()
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
