"""
测试脚本：验证参照版本 b8756f8 的功能
"""
import pytest
import httpx
import asyncio
import os
import time

BASE_URL = "http://127.0.0.1:8088"
TIMEOUT = 10.0

@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as client:
        # 等待服务启动
        for _ in range(30):
            try:
                resp = client.get("/health")
                if resp.status_code == 200:
                    break
            except:
                pass
            time.sleep(1)
        yield client

def test_health(client):
    """测试健康检查"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "OK"

def test_homepage_exists(client):
    """测试首页能访问"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "文件托管" in resp.text

def test_homepage_has_sort_controls(client):
    """测试首页有排序控件"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "?sort=time" in resp.text
    assert "?sort=ext" in resp.text

def test_homepage_has_upload_form(client):
    """测试首页有上传表单"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'type="file"' in resp.text
    assert 'name="file"' in resp.text

def test_homepage_has_notice_board(client):
    """测试首页有公告板"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "notice-content" in resp.text
    assert "ws-status-indicator" in resp.text

def test_notice_get(client):
    """测试获取公告"""
    resp = client.get("/notice")
    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data

def test_notice_update(client):
    """测试更新公告"""
    test_content = "测试公告内容 " + str(time.time())
    resp = client.post("/notice", json={"content": test_content})
    assert resp.status_code == 200

    # 验证更新
    resp = client.get("/notice")
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == test_content

def test_save_notice(client):
    """测试保存公告到文件"""
    # 先设置公告内容
    test_content = "保存测试公告"
    client.post("/notice", json={"content": test_content})

    # 保存公告
    resp = client.post("/save_notice")
    # 如果公告为空会失败，所以需要先设置内容
    if resp.status_code == 400:
        pytest.skip("公告内容为空")

    assert resp.status_code == 200
    data = resp.json()
    assert "filename" in data

def test_upload_form(client, tmp_path):
    """测试表单上传"""
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello from test")

    with open(test_file, "rb") as f:
        files = {"file": ("test.txt", f, "text/plain")}
        resp = client.post("/", files=files)

    assert resp.status_code == 201
    assert "http://obs.dimond.top/test.txt" in resp.text or "OK" in resp.text

def test_curl_style_upload(client, tmp_path):
    """测试 curl 风格的上传（PUT 方法）"""
    test_file = tmp_path / "curl_test.txt"
    test_content = "Curl style upload test"
    test_file.write_text(test_content)

    with open(test_file, "rb") as f:
        resp = client.put("/curl_test.txt", content=f.read())

    assert resp.status_code == 201
    assert "http://obs.dimond.top/curl_test.txt" in resp.text

def test_download(client):
    """测试文件下载"""
    # 先上传一个文件
    test_content = b"Download test content"
    client.put("/download_test.txt", content=test_content)

    # 下载
    resp = client.get("/download_test.txt")
    assert resp.status_code == 200
    assert resp.content == test_content

def test_delete(client):
    """测试文件删除"""
    # 先上传一个文件
    test_content = b"Delete test"
    client.put("/delete_me.txt", content=test_content)

    # 删除
    resp = client.delete("/delete_me.txt")
    assert resp.status_code == 200

def test_chunked_upload_init(client):
    """测试分片上传初始化"""
    data = {
        "filename": "chunk_test.txt",
        "size": 20971520,  # 20MB
        "hash_algo": "sha256",
        "hash": "a" * 64,  # fake hash
        "total_chunks": 2,
        "chunk_size": 10485760
    }
    resp = client.post("/upload/init", json=data)
    assert resp.status_code == 200
    result = resp.json()
    assert "upload_id" in result

def test_sort_by_ext(client):
    """测试按扩展名排序"""
    resp = client.get("/?sort=ext")
    assert resp.status_code == 200

def test_sort_by_time(client):
    """测试按时间排序"""
    resp = client.get("/?sort=time")
    assert resp.status_code == 200

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
