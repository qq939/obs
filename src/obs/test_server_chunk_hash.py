"""
测试服务端分片哈希计算功能
测试点：
1. 服务端在接收分片时自动计算分片哈希（后台进行，不阻塞上传）
2. 可以通过API查询分片哈希
3. 合并时使用分片哈希快速验证
4. 总体哈希在所有分片上传后自动计算（后台进行）
"""
import os
import time
import shutil
import threading
import requests
import pytest
import hashlib
import json

# 设置环境变量
TEST_PORT = 8093
TEST_DIR = "test_server_chunk_hash"
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR

from server import app

BASE_URL = f"http://localhost:{TEST_PORT}"

def run_server():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=TEST_PORT)

@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    """清理测试目录并启动服务器"""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    
    # 启动服务器
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # 等待服务器启动
    time.sleep(2)
    
    yield
    
    # 测试后清理
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)

def test_server_computes_chunk_hashes_automatically():
    """
    测试：服务端在接收分片时自动计算哈希
    期望：
    1. 上传分片时，服务端不阻塞，立即返回
    2. 可以通过API查询到分片哈希
    """
    filename = "auto_hash_test.bin"
    file_content = b"A" * (15 * 1024 * 1024)  # 15MB
    chunk_size = 5 * 1024 * 1024  # 5MB 分片
    total_chunks = 3
    
    # 计算期望的分片哈希
    expected_chunk_hashes = []
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(file_content))
        chunk_data = file_content[start:end]
        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
        expected_chunk_hashes.append(chunk_hash)
    
    # 初始化上传会话
    init_data = {
        "filename": filename,
        "size": len(file_content),
        "hash_algo": "sha256",
        "hash": hashlib.sha256(file_content).hexdigest(),
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }
    
    resp = requests.post(f"{BASE_URL}/upload/init", json=init_data)
    assert resp.status_code == 200, f"初始化失败: {resp.text}"
    
    data = resp.json()
    upload_id = data["upload_id"]
    
    # 上传分片并计时
    start_time = time.time()
    
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(file_content))
        chunk_data = file_content[start:end]
        
        resp = requests.put(
            f"{BASE_URL}/upload/chunk/{upload_id}/{i}",
            data=chunk_data,
            timeout=5
        )
        assert resp.status_code == 201, f"分片 {i} 上传失败: {resp.text}"
    
    upload_time = time.time() - start_time
    
    # 上传应该很快（不超过3秒，服务端应该在后台计算哈希）
    assert upload_time < 3, f"上传时间过长 ({upload_time:.2f}秒)，可能被哈希计算阻塞"
    
    # 等待服务端完成哈希计算（给予足够时间）
    time.sleep(1)
    
    # 查询分片状态（包含哈希）
    resp = requests.post(f"{BASE_URL}/upload/status/{upload_id}")
    assert resp.status_code == 200, f"查询状态失败: {resp.text}"
    
    status_data = resp.json()
    assert "chunk_hashes" in status_data, "响应中应该包含分片哈希"
    
    # 验证分片哈希正确
    actual_hashes = status_data["chunk_hashes"]
    assert len(actual_hashes) == total_chunks, f"应该有 {total_chunks} 个分片哈希"
    
    for i in range(total_chunks):
        assert actual_hashes[i] == expected_chunk_hashes[i], \
            f"分片 {i} 哈希不匹配: {actual_hashes[i]} != {expected_chunk_hashes[i]}"

def test_overall_hash_computed_in_background():
    """
    测试：服务端在分片上传后自动计算总体哈希
    期望：合并时可以直接使用服务端计算的哈希，无需重新读取整个文件
    注意：总体哈希在所有分片上传后计算，可以通过状态接口查询
    """
    filename = "background_hash_test.bin"
    file_content = b"B" * (20 * 1024 * 1024)  # 20MB
    chunk_size = 5 * 1024 * 1024
    total_chunks = 4
    
    expected_overall_hash = hashlib.sha256(file_content).hexdigest()
    
    # 初始化
    init_data = {
        "filename": filename,
        "size": len(file_content),
        "hash_algo": "sha256",
        "hash": expected_overall_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }
    
    resp = requests.post(f"{BASE_URL}/upload/init", json=init_data)
    assert resp.status_code == 200
    upload_id = resp.json()["upload_id"]
    
    # 上传所有分片
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(file_content))
        chunk_data = file_content[start:end]
        
        resp = requests.put(
            f"{BASE_URL}/upload/chunk/{upload_id}/{i}",
            data=chunk_data
        )
        assert resp.status_code == 201
    
    # 等待后台哈希计算完成
    time.sleep(2)
    
    # 查询状态，验证分片哈希已计算
    resp = requests.post(f"{BASE_URL}/upload/status/{upload_id}")
    assert resp.status_code == 200
    
    status_data = resp.json()
    assert "chunk_hashes" in status_data, "响应中应该包含分片哈希"
    assert len(status_data["chunk_hashes"]) == total_chunks, "分片哈希数量应该等于总片数"
    
    # 验证每个分片哈希都不为空
    for chunk_hash in status_data["chunk_hashes"]:
        assert chunk_hash is not None, "每个分片哈希都应该被计算"
    
    # 完成上传
    complete_data = {
        "filename": filename,
        "size": len(file_content),
        "total_chunks": total_chunks,
        "hash_algo": "sha256",
        "hash": expected_overall_hash
    }
    
    resp = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json=complete_data)
    assert resp.status_code == 200, f"完成上传失败: {resp.text}"
    
    # 验证文件完整性
    file_path = os.path.join(TEST_DIR, filename)
    assert os.path.exists(file_path), f"文件未创建: {file_path}"
    
    with open(file_path, "rb") as f:
        saved_hash = hashlib.sha256(f.read()).hexdigest()
    
    assert saved_hash == expected_overall_hash, "文件哈希验证失败"

def test_concurrent_chunk_upload_with_hash():
    """
    测试：并发上传分片时，服务端能正确计算所有分片的哈希
    """
    filename = "concurrent_hash_test.bin"
    file_content = os.urandom(20 * 1024 * 1024)  # 20MB 随机数据
    chunk_size = 5 * 1024 * 1024
    total_chunks = 4
    
    expected_chunk_hashes = []
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(file_content))
        chunk_data = file_content[start:end]
        expected_chunk_hashes.append(hashlib.sha256(chunk_data).hexdigest())
    
    # 初始化
    init_data = {
        "filename": filename,
        "size": len(file_content),
        "hash_algo": "sha256",
        "hash": hashlib.sha256(file_content).hexdigest(),
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }
    
    resp = requests.post(f"{BASE_URL}/upload/init", json=init_data)
    assert resp.status_code == 200
    upload_id = resp.json()["upload_id"]
    
    # 并发上传分片
    import concurrent.futures
    
    def upload_chunk(i):
        start = i * chunk_size
        end = min(start + chunk_size, len(file_content))
        chunk_data = file_content[start:end]
        
        resp = requests.put(
            f"{BASE_URL}/upload/chunk/{upload_id}/{i}",
            data=chunk_data
        )
        return i, resp.status_code == 201
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(upload_chunk, range(total_chunks)))
    
    assert all(status for _, status in results), "某些分片上传失败"
    
    # 等待哈希计算
    time.sleep(2)
    
    # 验证分片哈希
    resp = requests.post(f"{BASE_URL}/upload/status/{upload_id}")
    assert resp.status_code == 200
    
    status_data = resp.json()
    actual_hashes = status_data["chunk_hashes"]
    
    for i in range(total_chunks):
        assert actual_hashes[i] == expected_chunk_hashes[i], \
            f"并发上传分片 {i} 哈希不匹配"

def test_hash_verification_on_complete():
    """
    测试：完成上传时使用分片哈希进行快速验证
    期望：服务端使用预计算的分片哈希快速合并和验证，无需重新读取整个文件
    """
    filename = "verify_hash_test.bin"
    file_content = b"C" * (25 * 1024 * 1024)  # 25MB
    chunk_size = 5 * 1024 * 1024
    total_chunks = 5
    
    expected_overall_hash = hashlib.sha256(file_content).hexdigest()
    
    # 初始化
    init_data = {
        "filename": filename,
        "size": len(file_content),
        "hash_algo": "sha256",
        "hash": expected_overall_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks
    }
    
    resp = requests.post(f"{BASE_URL}/upload/init", json=init_data)
    assert resp.status_code == 200
    upload_id = resp.json()["upload_id"]
    
    # 上传所有分片
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(file_content))
        chunk_data = file_content[start:end]
        
        resp = requests.put(
            f"{BASE_URL}/upload/chunk/{upload_id}/{i}",
            data=chunk_data
        )
        assert resp.status_code == 201
    
    # 等待后台处理
    time.sleep(2)
    
    # 完成上传并计时
    start_time = time.time()
    
    complete_data = {
        "filename": filename,
        "size": len(file_content),
        "total_chunks": total_chunks,
        "hash_algo": "sha256",
        "hash": expected_overall_hash
    }
    
    resp = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json=complete_data)
    
    complete_time = time.time() - start_time
    
    assert resp.status_code == 200, f"完成上传失败: {resp.text}"
    
    # 合并和验证应该很快（使用分片哈希而非重新读取整个文件）
    # 如果重新读取25MB文件可能需要更长时间
    assert complete_time < 3, \
        f"完成时间过长 ({complete_time:.2f}秒)，可能没有使用分片哈希快速验证"
    
    # 验证文件完整性
    file_path = os.path.join(TEST_DIR, filename)
    assert os.path.exists(file_path)
    
    with open(file_path, "rb") as f:
        saved_hash = hashlib.sha256(f.read()).hexdigest()
    
    assert saved_hash == expected_overall_hash, "文件哈希验证失败"

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
