#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务（手机上传优化 A/B/C）：
  A) 分片哈希只算一次并缓存复用（重试不再重算）
  B) 去掉整文件 arrayBuffer 预扫：改「分片哈希 -> 文件指纹」，内存 O(分片)，只顺序读一遍文件
  C) 分片并发上传（UPLOAD_CONCURRENCY 路）+ 服务端阻塞哈希搬进线程池

超时机制: 90秒超时
"""
import os
import re
import sys
import json
import signal
import hashlib
import tempfile
import subprocess
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
BASE_URL = "http://127.0.0.1:80"
URL_HEAD = "http://obs.dimond.top"


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


# ---------------------------------------------------------------- 与前端/服务端同构的指纹算法
def chunk_fingerprint(data: bytes, chunk_size: int) -> str:
    h = hashlib.sha256()
    for i in range(0, len(data), chunk_size):
        h.update(hashlib.sha256(data[i:i + chunk_size]).hexdigest().encode())
    return h.hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_js(html: str, name: str) -> str:
    m = re.search(r"async function %s\([\s\S]*?\n            \}" % name, html)
    assert m, f"首页里找不到 JS 函数 {name}"
    return m.group(0)


def extract_js_for_e2e(html: str) -> str:
    """抽出常量 + 分片哈希/指纹函数 + 真实的分片上传函数，供 node 直接执行"""
    start = html.index("const CHUNK_SIZE_BROWSER")
    end = html.index("async function computeChunkHashes")
    tail = re.search(r"async function computeChunkHashes\([\s\S]*?\n            \}", html[end:])
    assert tail, "找不到 computeChunkHashes 结尾"
    block = html[start:end + tail.end()]
    return block + "\n" + extract_js(html, "uploadOneFileResumable")


# ---------------------------------------------------------------- 1/7
def test_frontend_no_whole_file_read():
    """[1/7] 前端不再把整个文件读进内存；改用分片哈希 + 指纹；分片并发"""
    print("\n[1/7] 前端上传实现（内存 / 指纹 / 并发）...")
    html = requests.get(BASE_URL + "/", timeout=10).text

    assert not re.search(r"sha256Hex\(file\)", html), "仍在对整个文件算 sha256（会 file.arrayBuffer() 读满内存）"
    assert "computeChunkHashes" in html, "缺少 computeChunkHashes（分片哈希）"
    assert "fileFingerprint" in html, "缺少 fileFingerprint（整文件指纹）"
    print("   ✓ 无整文件 arrayBuffer 预扫，改为 computeChunkHashes + fileFingerprint")

    body = re.search(r"async function uploadOneFileResumable\([\s\S]*?\n            \}\n", html)
    assert body, "未找到 uploadOneFileResumable"
    body = body.group(0)
    assert "UPLOAD_CONCURRENCY" in html and "Promise.all(workers)" in body, "缺少分片并发上传"
    assert re.search(r"headers:\s*\{\s*'X-Chunk-SHA256':\s*chunkHashes\[i\]", body), \
        "分片哈希未复用预计算结果（重试会重复计算）"
    assert "body: blob" in body, "分片请求体应是 Blob 切片（不应再 arrayBuffer 复制一份）"
    assert "chunk_size: chunkSize" in body, "complete 未带上 chunk_size（服务端无法重算指纹）"
    print("   ✓ 分片哈希复用 + 3 路并发 + complete 带 chunk_size")

    # 服务端：阻塞哈希必须走线程池，否则并发上传会被事件循环串行化
    src = open(SERVER_PY, "r", encoding="utf-8").read()
    assert "asyncio.to_thread(file_sha256" in src, "upload_chunk 的逐片哈希未走线程池"
    assert "asyncio.to_thread(chunk_fingerprint" in src, "指纹计算未走线程池"
    print("   ✓ 服务端哈希计算走 asyncio.to_thread（不阻塞事件循环）")


# ---------------------------------------------------------------- 2/7
def test_js_python_fingerprint_match():
    """[2/7] 用 node 跑首页里真实的 JS 函数，交叉验证与 Python 指纹一致"""
    print("\n[2/7] node 交叉验证 JS 指纹 == Python 指纹...")
    html = requests.get(BASE_URL + "/", timeout=10).text
    js = "\n".join(extract_js(html, n) for n in ("sha256Hex", "fileFingerprint", "computeChunkHashes"))

    with tempfile.TemporaryDirectory() as tmp:
        js_path = os.path.join(tmp, "extracted.js")
        data_path = os.path.join(tmp, "data.bin")
        driver_path = os.path.join(tmp, "driver.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js)

        driver = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const { fileFingerprint, computeChunkHashes } = new Function(
    src + '\\nreturn { sha256Hex, fileFingerprint, computeChunkHashes };')();
(async () => {
    const data = fs.readFileSync(process.argv[3]);
    const chunkSize = parseInt(process.argv[4], 10);
    const blob = new Blob([data]);
    const total = Math.ceil(blob.size / chunkSize) || 1;
    const hashes = await computeChunkHashes(blob, chunkSize, total, null);
    console.log(await fileFingerprint(hashes));
})();
"""
        with open(driver_path, "w", encoding="utf-8") as f:
            f.write(driver)

        # 覆盖多种边界：整除 / 不整除 / 单分片 / 小于一分片
        cases = [
            (os.urandom(300), 100),
            (os.urandom(301), 100),
            (os.urandom(50), 100),
            (os.urandom(1000), 1000),
        ]
        for payload, chunk_size in cases:
            with open(data_path, "wb") as f:
                f.write(payload)
            out = subprocess.run(["node", driver_path, js_path, data_path, str(chunk_size)],
                                 capture_output=True, text=True, timeout=30)
            assert out.returncode == 0, f"node 执行失败: {out.stderr}"
            js_fp = out.stdout.strip()
            py_fp = chunk_fingerprint(payload, chunk_size)
            assert js_fp == py_fp, \
                f"JS 与 Python 指纹不一致 (size={len(payload)}, chunk={chunk_size}): {js_fp} != {py_fp}"
        print(f"   ✓ {len(cases)} 组随机数据（含非整除边界）JS 指纹与 Python 完全一致")


# ---------------------------------------------------------------- 3/7
def test_node_runs_real_frontend_upload():
    """[3/7] 用 node 直接执行首页里真实的 uploadOneFileResumable，上传 >10MB 文件（真·端到端）"""
    print("\n[3/7] node 跑真实前端 JS 上传 >10MB 文件...")
    html = requests.get(BASE_URL + "/", timeout=10).text
    js = extract_js_for_e2e(html)
    name = "test_node_e2e_big.bin"
    payload = os.urandom(12 * 1024 * 1024 + 12345)   # 12MB+：必然走分片（>10MB）且末片不整除

    with tempfile.TemporaryDirectory() as tmp:
        js_path = os.path.join(tmp, "frontend.js")
        data_path = os.path.join(tmp, "big.bin")
        driver_path = os.path.join(tmp, "e2e.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js)
        with open(data_path, "wb") as f:
            f.write(payload)
        driver = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const base = process.argv[3];
const filePath = process.argv[4];
const filename = process.argv[5];
const origFetch = globalThis.fetch;
globalThis.fetch = (u, opts) => origFetch(new URL(u, base).toString(), opts);  // 相对路径 -> 绝对
const { uploadOneFileResumable } = new Function(
    src + '\\nreturn { uploadOneFileResumable };')();
(async () => {
    const data = fs.readFileSync(filePath);
    const file = new File([data], filename);
    const stages = new Set();
    let lastPct = 0;
    const res = await uploadOneFileResumable(file, (pct, loaded, stage) => {
        stages.add(stage || '上传中'); lastPct = pct;
    });
    console.log(JSON.stringify({ ok: true, result: String(res), stages: [...stages], lastPct }));
})().catch(e => {
    console.log(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
    process.exit(1);
});
"""
        with open(driver_path, "w", encoding="utf-8") as f:
            f.write(driver)

        try:
            out = subprocess.run(["node", driver_path, js_path, BASE_URL, data_path, name],
                                 capture_output=True, text=True, timeout=120)
            assert out.returncode == 0, f"node 端到端失败: {out.stderr or out.stdout}"
            result = json.loads(out.stdout.strip().splitlines()[-1])
            assert result["ok"], f"前端上传函数报错: {result.get('error')}"
            assert result["lastPct"] == 100, f"进度未走到 100%: {result}"
            assert "校验中" in result["stages"] and "上传中" in result["stages"], \
                f"进度阶段缺失: {result['stages']}"
            print(f"   ✓ 真实前端 JS 上传成功（阶段 {result['stages']}，进度 {result['lastPct']}%）")

            got = requests.get(f"{BASE_URL}/{name}", timeout=60).content
            assert len(got) == len(payload), f"下载大小不一致: {len(got)} != {len(payload)}"
            assert sha256_hex(got) == sha256_hex(payload), "下载内容 sha256 与原始不一致"
            print(f"   ✓ 下载校验通过（{len(got)} 字节，sha256 一致）")
        finally:
            requests.delete(f"{BASE_URL}/{name}", timeout=30)


# ---------------------------------------------------------------- 4/7
def _upload_chunked(name, payload, chunk_size, concurrency=3, bad_chunk=None, fingerprint=None):
    """按前端同款协议走一遍分片上传；返回 (init_json, [chunk状态码], complete响应)"""
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
    total = len(chunks)
    fp = fingerprint if fingerprint is not None else chunk_fingerprint(payload, chunk_size)

    r = requests.post(f"{BASE_URL}/upload/init", json={
        "filename": name, "size": len(payload), "hash_algo": "sha256",
        "hash": fp, "chunk_size": chunk_size, "total_chunks": total,
    }, timeout=15)
    assert r.status_code == 200, f"/upload/init 返回 {r.status_code}: {r.text}"
    info = r.json()
    if info.get("skip"):
        return info, [], None
    upload_id = info["uploadId"]

    def put(idx):
        h = sha256_hex(b"bad") if idx == bad_chunk else sha256_hex(chunks[idx])
        rr = requests.put(f"{BASE_URL}/upload/chunk/{upload_id}/{idx}", data=chunks[idx],
                          headers={"X-Chunk-SHA256": h}, timeout=20)
        return rr.status_code

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        codes = list(pool.map(put, range(total)))

    c = requests.post(f"{BASE_URL}/upload/complete/{upload_id}", json={
        "filename": name, "size": len(payload), "total_chunks": total,
        "chunk_size": chunk_size, "hash_algo": "sha256", "hash": fp,
    }, timeout=30)
    return info, codes, c


def test_protocol_end_to_end():
    """[4/7] 并发分片上传 + 指纹校验：完整协议走通，内容一致"""
    print("\n[4/7] 分片协议端到端（并发 3 路上传）...")
    name = "test_upload_stream_tmp.bin"
    payload = os.urandom(300)
    chunk_size = 100
    try:
        info, codes, c = _upload_chunked(name, payload, chunk_size)
        assert codes == [201, 201, 201], f"分片并发上传状态码异常: {codes}"
        print(f"   ✓ 3 个分片并发上传全部 201（upload_id={info['uploadId'][:32]}...）")
        assert c.status_code in (200, 201), f"complete 返回 {c.status_code}: {c.text}"
        got = requests.get(f"{BASE_URL}/{name}", timeout=20).content
        assert got == payload, "合并后内容不一致"
        print("   ✓ complete 通过指纹校验，下载内容与原始一致")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)


# ---------------------------------------------------------------- 5/7
def test_skip_upload():
    """[5/7] 秒传：同一文件（同分片大小）再次 init -> skip"""
    print("\n[5/7] 秒传（指纹命中）...")
    name = "test_upload_skip_tmp.bin"
    payload = os.urandom(250)
    chunk_size = 100
    try:
        _, _, c = _upload_chunked(name, payload, chunk_size)
        assert c.status_code in (200, 201), f"首次上传失败: {c.status_code} {c.text}"
        info, codes, _ = _upload_chunked(name, payload, chunk_size)
        assert info.get("skip") is True, f"同文件二次 init 未命中秒传: {info}"
        assert info.get("url") == f"{URL_HEAD}/{name}", f"秒传返回 url 异常: {info.get('url')}"
        print("   ✓ 同文件二次上传命中秒传（skip=True）")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)


# ---------------------------------------------------------------- 6/7
def test_negative_cases():
    """[6/7] 负例：错误分片哈希 / 错误指纹 / 缺参数 都要被拒"""
    print("\n[6/7] 负例校验...")
    name = "test_upload_neg_tmp.bin"
    payload = os.urandom(300)
    chunk_size = 100
    try:
        # 分片哈希错误 -> 422 且不落账
        info, codes, _ = _upload_chunked(name, payload, chunk_size, bad_chunk=1)
        assert 422 in codes, f"错误分片哈希未被拒绝: {codes}"
        st = requests.post(f"{BASE_URL}/upload/status/{info['uploadId']}", timeout=10).json()
        assert 1 not in st.get("uploaded", []), f"校验失败的分片被落账: {st.get('uploaded')}"
        print("   ✓ 错误分片哈希 -> 422 且未落账")

        # 指纹错误 -> complete 422
        wrong = sha256_hex(b"this-is-not-the-right-fingerprint")
        info, codes, c = _upload_chunked(name + ".2", payload, chunk_size, fingerprint=wrong)
        assert c is not None and c.status_code == 422, \
            f"错误指纹应 422（旧实现会放行），实际 {getattr(c, 'status_code', None)}"
        requests.delete(f"{BASE_URL}/{name}.2", timeout=10)
        print("   ✓ 错误指纹 -> complete 422")

        # 缺少 chunk_size -> 400
        r = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": len(payload), "hash_algo": "sha256",
            "hash": chunk_fingerprint(payload, chunk_size), "total_chunks": 3}, timeout=10)
        assert r.status_code == 400, f"init 缺 chunk_size 应 400，实际 {r.status_code}"
        r = requests.post(f"{BASE_URL}/upload/complete/{info['uploadId']}", json={
            "filename": name, "size": len(payload), "total_chunks": 3,
            "hash_algo": "sha256", "hash": chunk_fingerprint(payload, chunk_size)}, timeout=10)
        assert r.status_code == 400, f"complete 缺 chunk_size 应 400，实际 {r.status_code}"
        print("   ✓ 缺 chunk_size -> 400")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)


# ---------------------------------------------------------------- 7/7
def test_regression():
    """[7/7] 回归：直传 / health / 首页拖拽区 / 视频页（倒放+自动切下一个+1-2-7 档位）"""
    print("\n[7/7] 回归...")
    name = "test_upload_stream_reg.tmp"
    try:
        r = requests.put(f"{BASE_URL}/{name}", data=b"regression", timeout=10)
        assert r.status_code == 201, f"PUT 直传返回 {r.status_code}"
        print("   ✓ PUT 直传正常")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)

    assert requests.get(f"{BASE_URL}/health", timeout=10).json().get("status") == "ok", "/health 异常"
    assert 'id="uploadZone"' in requests.get(BASE_URL + "/", timeout=10).text, "首页拖拽上传区丢失"
    print("   ✓ /health 正常，首页拖拽上传区保留")

    js = requests.get(BASE_URL + "/video/app.js", timeout=10).text
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页 -3x 倒放丢失"
    assert re.search(r"video\.loop\s*=\s*false", js), "video.loop 应为 false"
    assert re.search(r"video\.addEventListener\('ended'", js), "ended 自动切下一个丢失"
    html = requests.get(BASE_URL + "/video", timeout=10).text
    assert re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html) == ["1", "2", "7"], "档位应为 1/2/7"
    print("   ✓ 视频页：-3x 倒放 / 自动切下一个 / 1-2-7 档位 均正常")


@timeout(90)
def main():
    print("=" * 60)
    print("任务测试：手机上传优化（A 哈希复用 / B 流式指纹 / C 并发）")
    print("=" * 60)
    try:
        test_frontend_no_whole_file_read()
        test_js_python_fingerprint_match()
        test_node_runs_real_frontend_upload()
        test_protocol_end_to_end()
        test_skip_upload()
        test_negative_cases()
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
