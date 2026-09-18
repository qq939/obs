#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务（1 + 3）：
  1) 修复前端字段名：/upload/init 返回 uploadId，前端却读 info.upload_id；
     并让 /upload/complete 校验 session 是否匹配。
  3) 分片上传加真正的逐片校验：客户端带上分片哈希，服务端逐片验证后才落账，
     校验失败必须删除该分片并拒绝。

超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import hashlib
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
BASE_URL = "http://127.0.0.1:80"
CHUNK_HEADER = "X-Chunk-SHA256"


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


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_frontend_upload_id_field():
    """[1/5] 前端读取 init 返回的 uploadId（原为 info.upload_id，实际是 undefined）"""
    print("\n[1/5] 前端字段名 uploadId...")
    html = requests.get(BASE_URL + "/", timeout=5).text
    assert "info.uploadId" in html, "前端未读取 init 返回的 uploadId 字段"
    assert "info.upload_id" not in html, "前端仍残留错误的 info.upload_id 字段名"
    print("   ✓ 前端已按 uploadId 取会话 id")


def test_frontend_sends_chunk_hash():
    """[2/5] 前端分片 PUT 带 X-Chunk-SHA256 头"""
    print("\n[2/5] 前端分片携带分片哈希...")
    html = requests.get(BASE_URL + "/", timeout=5).text
    m = re.search(r"async function uploadOneFileResumable\(.*?\n            \}", html, re.S)
    assert m, "未找到 uploadOneFileResumable 函数"
    body = m.group(0)
    assert CHUNK_HEADER in body, f"分片 PUT 未携带 {CHUNK_HEADER} 头"
    assert re.search(r"sha256Hex\(blob\)", body), "分片哈希未基于分片内容计算"
    assert re.search(r"headers:\s*\{\s*['\"]?X-Chunk-SHA256", body), "分片哈希未放进 PUT 请求头"
    print(f"   ✓ 每个分片 PUT 携带 {CHUNK_HEADER}")


def test_server_verifies_each_chunk():
    """[3/5] 服务端逐片校验：正确哈希 201 落账，错误哈希 422 且丢弃该分片"""
    print("\n[3/5] 服务端逐片校验（真实走协议）...")
    name = "test_chunk_verify_tmp.bin"
    payload = os.urandom(120)
    chunk_size = 40
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
    total = len(chunks)
    whole_hash = sha256_hex(payload)

    try:
        r = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": len(payload), "hash_algo": "sha256",
            "hash": whole_hash, "chunk_size": chunk_size, "total_chunks": total,
        }, timeout=10)
        assert r.status_code == 200, f"/upload/init 返回 {r.status_code}"
        info = r.json()
        upload_id = info.get("uploadId")
        assert upload_id, f"/upload/init 未返回 uploadId: {info}"
        print(f"   ✓ init 返回 uploadId={upload_id[:40]}...")

        # 分片 0：正确哈希 -> 201
        r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/0", data=chunks[0],
                         headers={CHUNK_HEADER: sha256_hex(chunks[0])}, timeout=10)
        assert r.status_code == 201, f"正确哈希的分片应 201，实际 {r.status_code} {r.text}"
        print("   ✓ 分片 0 哈希正确 -> 201")

        # 分片 1：错误哈希 -> 422，且不得落账
        r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/1", data=chunks[1],
                         headers={CHUNK_HEADER: sha256_hex(b"wrong")}, timeout=10)
        assert r.status_code == 422, f"错误哈希的分片应 422，实际 {r.status_code} {r.text}"
        st = requests.post(f"{BASE_URL}/upload/status/{upload_id}", timeout=10).json()
        assert 1 not in st.get("uploaded", []), f"校验失败的分片被错误落账: {st.get('uploaded')}"
        print("   ✓ 分片 1 哈希错误 -> 422 且未落账（失败分片已丢弃）")

        # 分片 1 重传（正确哈希）-> 201；分片 2 -> 201
        for i in (1, 2):
            r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=chunks[i],
                             headers={CHUNK_HEADER: sha256_hex(chunks[i])}, timeout=10)
            assert r.status_code == 201, f"分片 {i} 重传失败: {r.status_code} {r.text}"
        print("   ✓ 分片 1 重传 / 分片 2 上传均 201")

        r = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json={
            "filename": name, "size": len(payload), "total_chunks": total,
            "hash_algo": "sha256", "hash": whole_hash,
        }, timeout=15)
        assert r.status_code in (200, 201), f"合并失败: {r.status_code} {r.text}"
        got = requests.get(f"{BASE_URL}/{name}", timeout=10).content
        assert got == payload, "合并后文件内容与原始不一致"
        assert sha256_hex(got) == whole_hash
        print("   ✓ 合并成功且内容/整文件 sha256 一致")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=5)


def test_complete_verifies_whole_file_hash():
    """[4/5] complete 必须真校验整文件哈希：声明错误哈希 -> 422"""
    print("\n[4/5] complete 整文件哈希校验...")
    name = "test_whole_hash_tmp.bin"
    payload = b"whole-file-hash-check" * 10
    chunk_size = 50
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
    total = len(chunks)
    wrong_hash = sha256_hex(b"not-the-file")

    try:
        r = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": len(payload), "hash_algo": "sha256",
            "hash": wrong_hash, "chunk_size": chunk_size, "total_chunks": total,
        }, timeout=10)
        assert r.status_code == 200, f"/upload/init 返回 {r.status_code}"
        upload_id = r.json()["uploadId"]
        for i, c in enumerate(chunks):
            r = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{i}", data=c,
                             headers={CHUNK_HEADER: sha256_hex(c)}, timeout=10)
            assert r.status_code == 201, f"分片 {i} 上传失败: {r.status_code}"

        r = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json={
            "filename": name, "size": len(payload), "total_chunks": total,
            "hash_algo": "sha256", "hash": wrong_hash,
        }, timeout=15)
        assert r.status_code == 422, \
            f"整文件哈希不匹配应 422（旧实现会跳过校验直接通过），实际 {r.status_code}"
        print("   ✓ 整文件哈希不匹配 -> 422（真实校验，不再跳过）")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=5)


def test_regression():
    """[5/5] 回归：直传 / 首页上传区 / health / 视频页（倒放+档位+自动切下一个）"""
    print("\n[5/5] 回归...")
    name = "test_chunk_verify_reg.tmp"
    try:
        r = requests.put(f"{BASE_URL}/{name}", data=b"regression", timeout=10)
        assert r.status_code == 201, f"PUT 直传返回 {r.status_code}"
        print("   ✓ PUT 直传正常")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=5)

    assert requests.get(f"{BASE_URL}/health", timeout=5).json().get("status") == "ok", "/health 异常"
    assert 'id="uploadZone"' in requests.get(BASE_URL + "/", timeout=5).text, "首页拖拽上传区丢失"
    print("   ✓ /health 正常，首页拖拽上传区保留")

    js = requests.get(BASE_URL + "/video/app.js", timeout=5).text
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页 -3x 倒放丢失"
    assert re.search(r"video\.loop\s*=\s*false", js), "video.loop 应为 false（自动切下一个）"
    assert re.search(r"video\.addEventListener\('ended'", js), "ended 自动切下一个丢失"
    html = requests.get(BASE_URL + "/video", timeout=5).text
    assert re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html) == ["1", "2", "7"], "档位应为 1/2/7"
    print("   ✓ 视频页：-3x 倒放 / 自动切下一个 / 1-2-7 档位 均正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：修复 uploadId 字段 + 分片逐片校验")
    print("=" * 60)
    try:
        test_frontend_upload_id_field()
        test_frontend_sends_chunk_hash()
        test_server_verifies_each_chunk()
        test_complete_verifies_whole_file_hash()
        test_regression()
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
