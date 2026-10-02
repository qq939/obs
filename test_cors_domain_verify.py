#!/usr/bin/env python3
"""TDD 验收测试 - 本次任务的验收标准：

  1) CORS 预检 `OPTIONS /upload/init` → 200
  2) 上传往返 `PUT` 201 → `GET` 200 → `DELETE` 200，
     且 PUT 返回的 URL 为 `http://ocs.dimond.top/verify2.txt`

说明：
  - server.py 里没有包相对导入，直接把它所在目录加入 sys.path 后 `import server`
  - 起一个真实的 uvicorn（独立端口 8092 + 临时上传目录），用 requests 做真实 HTTP 往返
  - 超时机制：每个用例 60 秒（signal.alarm），超时即失败
"""
import os
import sys
import time
import signal
import shutil
import threading

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
# server.py 位于 src/obs/，其内部无包相对导入，直接把该目录加入 sys.path
sys.path.insert(0, os.path.join(ROOT, "src", "obs"))

TEST_PORT = 8092
TEST_DIR = os.path.join(ROOT, "test_obs_cors")
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR
os.environ["HLS_DIR"] = os.path.join(TEST_DIR, "hls")
os.environ["LOG_DIR"] = os.path.join(TEST_DIR, "logs")
os.environ["HLS_CRON_ENABLED"] = "0"   # 测试期间关闭 04:00 定时任务

from server import app  # noqa: E402  (必须在设置环境变量之后导入)

BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
EXPECTED_URL = "http://ocs.dimond.top/verify2.txt"

TIMEOUT_SECONDS = 60


def timeout(seconds):
    """每个用例的执行超时保护（超时抛 TimeoutError，用例即失败）"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise TimeoutError(f"{func.__name__} 超时（>{seconds}s）")
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                return func(*args, **kwargs)
            finally:
                signal.alarm(0)
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


_server_started = False


def _start_server():
    global _server_started
    if _server_started:
        return
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)

    import uvicorn

    def run():
        uvicorn.run(app, host="127.0.0.1", port=TEST_PORT, log_level="warning")

    threading.Thread(target=run, daemon=True).start()
    # 等待端口就绪（最多 20s）
    for _ in range(200):
        try:
            if requests.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                _server_started = True
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("测试服务启动超时")


@timeout(TIMEOUT_SECONDS)
def test_cors_preflight_upload_init():
    """CORS 预检 OPTIONS /upload/init → 200，且带 access-control-allow-origin"""
    _start_server()
    resp = requests.options(
        f"{BASE_URL}/upload/init",
        headers={
            "Origin": "http://ocs.dimond.top",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=10,
    )
    assert resp.status_code == 200, f"OPTIONS /upload/init 期望 200，实际 {resp.status_code}"
    allow_origin = resp.headers.get("access-control-allow-origin")
    assert allow_origin, "缺少 access-control-allow-origin 响应头"


@timeout(TIMEOUT_SECONDS)
def test_cors_preflight_chunk_put():
    """分片 PUT 预检 OPTIONS（带 x-chunk-sha256）→ 200"""
    _start_server()
    resp = requests.options(
        f"{BASE_URL}/upload/chunk/demo/0",
        headers={
            "Origin": "http://ocs.dimond.top",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-chunk-sha256",
        },
        timeout=10,
    )
    assert resp.status_code == 200, f"OPTIONS /upload/chunk 期望 200，实际 {resp.status_code}"
    assert resp.headers.get("access-control-allow-origin"), "缺少 access-control-allow-origin"


@timeout(TIMEOUT_SECONDS)
def test_upload_round_trip_returns_ocs_url():
    """PUT 201 → GET 200 → DELETE 200，且 PUT 返回 http://ocs.dimond.top/verify2.txt"""
    _start_server()
    payload = b"verify2-round-trip"

    put = requests.put(f"{BASE_URL}/verify2.txt", data=payload, timeout=10)
    assert put.status_code == 201, f"PUT 期望 201，实际 {put.status_code}: {put.text}"
    assert put.text.strip() == EXPECTED_URL, f"PUT 返回 URL 期望 {EXPECTED_URL}，实际 {put.text!r}"

    get = requests.get(f"{BASE_URL}/verify2.txt", timeout=10)
    assert get.status_code == 200, f"GET 期望 200，实际 {get.status_code}"
    assert get.content == payload, "GET 内容与上传不一致"

    delete = requests.delete(f"{BASE_URL}/verify2.txt", timeout=10)
    assert delete.status_code == 200, f"DELETE 期望 200，实际 {delete.status_code}"

    assert requests.get(f"{BASE_URL}/verify2.txt", timeout=10).status_code == 404, "删除后应 404"


if __name__ == "__main__":
    failures = 0
    for fn in (test_cors_preflight_upload_init, test_cors_preflight_chunk_put,
               test_upload_round_trip_returns_ocs_url):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
