import os
import threading
import time
import requests
import pytest
import hashlib

# 测试端口与目录
TEST_PORT = 8093
TEST_DIR = "test_obs_chunk_md5"
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

def test_chunk_upload_returns_md5():
    """测试后端上传分片后返回分片的MD5值"""
    filename = "chunk_md5_test.bin"
    data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 100  # ~2.6KB
    size = len(data)
    chunk_size = 1000  # 1KB per chunk
    total_chunks = (size + chunk_size - 1) // chunk_size
    file_hash = sha256_hex(data)
    
    # 初始化上传会话
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
    
    # 上传每个分片，验证返回的MD5
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, size)
        chunk_data = data[start:end]
        expected_md5 = hashlib.md5(chunk_data).hexdigest()
        
        r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=chunk_data, timeout=10)
        assert r.status_code == 201
        # 验证返回JSON包含md5字段
        result = r.json()
        assert "md5" in result, f"分片 {i} 应返回 md5 字段"
        assert result["md5"] == expected_md5, f"分片 {i} MD5不匹配: 期望 {expected_md5}, 得到 {result['md5']}"
        print(f"✓ 分片 {i} MD5验证通过: {expected_md5}")

def test_chunk_md5_mismatch_triggers_retry():
    """测试分片MD5不匹配时触发重传"""
    filename = "md5_mismatch_test.bin"
    data = b"Test data for mismatch" * 100  # ~1.8KB
    size = len(data)
    chunk_size = 500  # 500 bytes per chunk
    total_chunks = (size + chunk_size - 1) // chunk_size
    file_hash = sha256_hex(data)
    
    # 初始化上传会话
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
    
    # 上传第一个分片
    chunk_data = data[0:chunk_size]
    correct_md5 = hashlib.md5(chunk_data).hexdigest()
    
    r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/0", data=chunk_data, timeout=10)
    assert r.status_code == 201
    result = r.json()
    assert result["md5"] == correct_md5
    print(f"✓ 正确分片MD5: {correct_md5}")
    
    # 上传一个错误的分片（篡改数据）
    wrong_chunk = b"WRONG_DATA_INSTEAD"
    wrong_md5 = hashlib.md5(wrong_chunk).hexdigest()
    r2 = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/1", data=wrong_chunk, timeout=10)
    assert r2.status_code == 201
    result2 = r2.json()
    assert result2["md5"] == wrong_md5
    print(f"✓ 错误分片MD5: {wrong_md5}")
    
    # 模拟前端重新上传正确的分片
    correct_chunk_1 = data[chunk_size:2*chunk_size]
    correct_md5_1 = hashlib.md5(correct_chunk_1).hexdigest()
    r3 = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/1", data=correct_chunk_1, timeout=10)
    assert r3.status_code == 201
    result3 = r3.json()
    assert result3["md5"] == correct_md5_1
    print(f"✓ 修正后分片1 MD5: {correct_md5_1}")

def test_full_upload_with_chunk_md5_verification():
    """完整的分片MD5校验上传流程"""
    filename = "full_md5_test.bin"
    data = os.urandom(5000)  # 5KB random data
    size = len(data)
    chunk_size = 1000  # 1KB per chunk
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
    upload_id = resp.json()["upload_id"]
    
    # 上传所有分片并验证MD5
    all_md5s = []
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, size)
        chunk_data = data[start:end]
        expected_md5 = hashlib.md5(chunk_data).hexdigest()
        all_md5s.append(expected_md5)
        
        r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=chunk_data, timeout=10)
        assert r.status_code == 201
        assert r.json()["md5"] == expected_md5
    
    print(f"✓ 所有 {total_chunks} 个分片MD5验证通过")
    
    # 合并
    r = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json={
        "filename": filename,
        "size": size,
        "total_chunks": total_chunks,
        "hash_algo": "sha256",
        "hash": file_hash
    }, timeout=20)
    assert r.status_code == 200
    
    # 验证下载内容
    d = requests.get(f"{BASE_URL}/{filename}", timeout=10)
    assert d.status_code == 200
    assert d.content == data
    print("✓ 文件内容验证通过")
