import os
import threading
import time
import requests
import pytest

# 设置测试端口与目录
TEST_PORT = 8090
TEST_DIR = "test_obs_tuning"
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR

from server import app, MAX_UPLOAD_SIZE, UPLOAD_CHUNK_SIZE, RANGE_DOWNLOAD_CHUNK_SIZE, STREAM_DOWNLOAD_CHUNK_SIZE, UVICORN_CONFIG

BASE_URL = f"http://localhost:{TEST_PORT}"

def run_server():
    import uvicorn
    # 使用默认参数运行；此处仅用于启动服务，配置值通过 UVICORN_CONFIG 进行断言
    uvicorn.run(app, host="0.0.0.0", port=TEST_PORT)

@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    if os.path.exists(TEST_DIR):
        import shutil
        shutil.rmtree(TEST_DIR)
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(2)
    yield
    if os.path.exists(TEST_DIR):
        import shutil
        shutil.rmtree(TEST_DIR)

def test_constants_values():
    assert MAX_UPLOAD_SIZE is None
    assert UPLOAD_CHUNK_SIZE == 10 * 1024 * 1024
    assert RANGE_DOWNLOAD_CHUNK_SIZE == 10 * 1024 * 1024
    assert STREAM_DOWNLOAD_CHUNK_SIZE == 40 * 1024 * 1024
    assert UVICORN_CONFIG["limit_concurrency"] == 1000
    assert UVICORN_CONFIG["limit_max_requests"] == 10000
    assert UVICORN_CONFIG["timeout_keep_alive"] == 300
    assert UVICORN_CONFIG["backlog"] == 2048

def test_large_file_stream_download():
    # 生成约 60MB 数据并上传
    size_mb = 60
    data = b"A" * (size_mb * 1024 * 1024)
    filename = "large_stream_test.bin"
    resp_put = requests.put(f"{BASE_URL}/{filename}", data=data, timeout=60)
    assert resp_put.status_code == 201
    # 不带 Range 的下载应返回 200 且包含 Accept-Ranges 与 Content-Length
    resp_get = requests.get(f"{BASE_URL}/{filename}", timeout=60)
    assert resp_get.status_code == 200
    assert resp_get.headers.get("Accept-Ranges") == "bytes"
    assert int(resp_get.headers.get("Content-Length", "0")) == len(data)
