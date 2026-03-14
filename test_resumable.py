import os
import threading
import time
import requests
import pytest
import hashlib

# 测试端口与目录
TEST_PORT = 8092
TEST_DIR = "test_obs_resumable"
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR

from server import app

BASE_URL = f"http://localhost:{TEST_PORT}"

def run_server():
    import uvicorn
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

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def test_resumable_upload_and_complete():
    filename = "resumable.bin"
    data = b"0123456789ABCDEF" * 1024  # ~16KB
    size = len(data)
    chunk_size = 4096
    total_chunks = (size + chunk_size - 1) // chunk_size
    file_hash = sha256_hex(data)
    # 初始化
    resp = requests.post(f"{BASE_URL}/upload/init", json={
        "filename": filename,
        "size": size,
        "hash_algo": "sha256",
        "hash": file_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }, timeout=10)
    assert resp.status_code == 200
    info = resp.json()
    upload_id = info["upload_id"]
    assert isinstance(upload_id, str)
    # 上传所有分片
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, size)
        chunk = data[start:end]
        r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=chunk, timeout=10)
        assert r.status_code == 201
    # 合并
    r = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json={
        "filename": filename,
        "size": size,
        "total_chunks": total_chunks,
        "hash_algo": "sha256",
        "hash": file_hash
    }, timeout=20)
    assert r.status_code == 200
    url = r.text
    # 验证下载内容
    d = requests.get(f"{BASE_URL}/{filename}", timeout=10)
    assert d.status_code == 200
    assert d.content == data

def test_resume_missing_chunk_and_skip_when_exists():
    filename = "resume.bin"
    data = b"A" * 15000  # ~15KB
    size = len(data)
    chunk_size = 5000
    total_chunks = (size + chunk_size - 1) // chunk_size
    file_hash = sha256_hex(data)
    # 初始化并上传前两个分片
    resp = requests.post(f"{BASE_URL}/upload/init", json={
        "filename": filename,
        "size": size,
        "hash_algo": "sha256",
        "hash": file_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }, timeout=10)
    assert resp.status_code == 200
    upload_id = resp.json()["upload_id"]
    for i in range(total_chunks - 1):  # 留一个分片未传
        start = i * chunk_size
        end = min(start + chunk_size, size)
        chunk = data[start:end]
        r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=chunk, timeout=10)
        assert r.status_code == 201
    # 重新初始化，检查已上传分片列表
    resp2 = requests.post(f"{BASE_URL}/upload/init", json={
        "filename": filename,
        "size": size,
        "hash_algo": "sha256",
        "hash": file_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }, timeout=10)
    assert resp2.status_code == 200
    info2 = resp2.json()
    assert len(info2.get("uploaded", [])) == total_chunks - 1
    # 上传剩余分片
    i = total_chunks - 1
    start = i * chunk_size
    end = min(start + chunk_size, size)
    chunk = data[start:end]
    r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=chunk, timeout=10)
    assert r.status_code == 201
    # 合并完成
    r = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json={
        "filename": filename,
        "size": size,
        "total_chunks": total_chunks,
        "hash_algo": "sha256",
        "hash": file_hash
    }, timeout=20)
    assert r.status_code == 200
    # 再次初始化（应触发秒传）
    resp3 = requests.post(f"{BASE_URL}/upload/init", json={
        "filename": filename,
        "size": size,
        "hash_algo": "sha256",
        "hash": file_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }, timeout=10)
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3.get("skip") is True
    assert data3.get("url", "").endswith(f"/{filename}")
