#!/usr/bin/env python3
"""TDD 回归测试 - 下载「快线路优先 + 公网回退」且**点击不能被劫持**：

背景：旧实现 `downloadWithFallback` 在 click 回调里 `preventDefault()` 后做异步 HEAD 探测，
再在 `.then()` 里 `a.click()`，丢失用户手势 → 浏览器拦截下载（点链接没反应，只能手粘地址栏）。

正确做法：页面加载时**预热**探测快线路（http://localhost:19082），
可达就把文件列表的下载链接同步改写到它；点击始终是浏览器原生同步导航（不拦截、不异步）。

断言：
  1) 文件列表链接带 `data-file`（供预热改链），且服务端默认 href 指向公网域名（探测未完成也可点）
  2) 预热代码存在：OBS_FAST_HOST / obsProbe(mode:'no-cors' + AbortController 超时) / DOMContentLoaded 注册
  3) 不存在点击期异步劫持：无 downloadWithFallback、下载链接无 onclick 拦截
  4) /health 是探测端点（200）

超时机制：每个用例 60 秒。
"""
import os
import re
import sys
import time
import signal
import shutil
import threading

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src", "obs"))

TEST_PORT = 8093
TEST_DIR = os.path.join(ROOT, "test_obs_fasthost")
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR
os.environ["HLS_DIR"] = os.path.join(TEST_DIR, "hls")
os.environ["LOG_DIR"] = os.path.join(TEST_DIR, "logs")
os.environ["HLS_CRON_ENABLED"] = "0"

from server import app  # noqa: E402

BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
PUBLIC_HOST = "http://ocs.dimond.top"
FAST_HOST = "http://localhost:19082"
TEST_FILE = "dl_probe.txt"

_started = False


def _timeout(seconds):
    def deco(func):
        def wrapper(*a, **k):
            def handler(signum, frame):
                raise TimeoutError(f"{func.__name__} 超时（>{seconds}s）")
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                return func(*a, **k)
            finally:
                signal.alarm(0)
        wrapper.__name__ = func.__name__
        return wrapper
    return deco


def _start_server():
    global _started
    if _started:
        return
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)
    import uvicorn

    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=TEST_PORT, log_level="warning"),
        daemon=True,
    ).start()
    for _ in range(200):
        try:
            if requests.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                _started = True
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("测试服务启动超时")


def _homepage():
    _start_server()
    assert requests.put(f"{BASE_URL}/{TEST_FILE}", data=b"dl").status_code == 201
    resp = requests.get(BASE_URL, timeout=10)
    assert resp.status_code == 200
    return resp.text


@_timeout(60)
def test_links_carry_data_file_with_public_default():
    html = _homepage()
    # 下载链接：默认指向公网域名（探测未就绪也能点），并带 data-file 供预热改链
    assert f'<a href="{PUBLIC_HOST}/{TEST_FILE}" download data-file="{TEST_FILE}">' in html, \
        "下载链接应保留公网默认 href，并带 download + data-file"
    # 预览链接同样带 data-file
    assert re.search(r'<a href="%s/%s" target="_blank" data-file="%s">' % (
        re.escape(PUBLIC_HOST), re.escape(TEST_FILE), re.escape(TEST_FILE)), html), \
        "预览链接应带 data-file"


@_timeout(60)
def test_preheat_probe_present():
    html = _homepage()
    assert f'OBS_FAST_HOST = "{FAST_HOST}"' in html, "应定义快线路常量 OBS_FAST_HOST"
    assert "function obsProbe(" in html and "mode: 'no-cors'" in html, "探测应为 no-cors"
    assert "AbortController" in html and "resolveObsFastHost" in html, "探测应有超时与预热入口"
    assert "addEventListener('DOMContentLoaded', resolveObsFastHost)" in html, \
        "预热必须在页面加载阶段注册（而不是点击时）"
    assert "obsWriteDownloadLinks" in html and 'a[data-file]' in html, "应能按 data-file 改写链接"


@_timeout(60)
def test_no_click_time_hijack():
    html = _homepage()
    assert "downloadWithFallback" not in html, "不应再存在点击期异步劫持实现"
    # 下载链接不能挂 onclick（挂了就有拦截/异步风险）
    assert not re.search(r'<a [^>]*download[^>]*onclick=', html), "下载链接不应有 onclick 拦截"


@_timeout(60)
def test_health_probe_endpoint():
    _start_server()
    assert requests.get(f"{BASE_URL}/health", timeout=5).status_code == 200, "/health 必须可探测"


if __name__ == "__main__":
    failed = 0
    for fn in (test_links_carry_data_file_with_public_default, test_preheat_probe_present,
               test_no_click_time_hijack, test_health_probe_endpoint):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
