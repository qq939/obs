#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  1) 修复「http（非安全上下文）下手机上传大文件失败」：
     浏览器仅在安全上下文（https / localhost）才提供 crypto.subtle，
     通过 http://obs.dimond.top 或 http://<局域网IP> 访问时 crypto.subtle 为 undefined，
     >10MB 的文件走分片路径调用 crypto.subtle.digest 直接抛错 => 全部上传失败。
     修复：crypto.subtle 不可用时，用纯 JS SHA-256 兜底（哈希结果与 crypto.subtle 一致）。
  2) 修复「logs 目录没挂载」：服务端把日志写到 logs/server.log，
     docker-compose 挂载 logs、Dockerfile 创建 /app/logs。

超时机制: 每个用例 120 秒超时
"""
import os
import re
import sys
import json
import time
import signal
import hashlib
import tempfile
import subprocess
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
COMPOSE = os.path.join(ROOT, "docker-compose.yml")
DOCKERFILE = os.path.join(ROOT, "Dockerfile.obs")
HOST_LOG = os.path.join(ROOT, "logs", "server.log")
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


def extract_js_block(html: str) -> str:
    """抽出 常量 + sha256 系列函数 + 分片哈希/指纹函数 + 真实的分片上传函数，供 node 直接执行"""
    start = html.index("const CHUNK_SIZE_BROWSER")
    end = html.index("async function computeChunkHashes")
    tail = re.search(r"async function computeChunkHashes\([\s\S]*?\n            \}", html[end:])
    assert tail, "找不到 computeChunkHashes 结尾"
    block = html[start:end + tail.end()]
    return block + "\n" + extract_js(html, "uploadOneFileResumable")


def run_node(driver: str, *args, timeout_s=120):
    with tempfile.TemporaryDirectory() as tmp:
        driver_path = os.path.join(tmp, "driver.js")
        with open(driver_path, "w", encoding="utf-8") as f:
            f.write(driver)
        return subprocess.run(["node", driver_path, *args],
                              capture_output=True, text=True, timeout=timeout_s)


# ---------------------------------------------------------------- 1/5
@timeout(120)
def test_frontend_has_insecure_context_fallback():
    """[1/5] 首页 JS 在 crypto.subtle 缺失时有纯 JS SHA-256 兜底，且不再无条件调用 crypto.subtle"""
    print("\n[1/5] 前端 http 非安全上下文兜底...")
    html = requests.get(BASE_URL + "/", timeout=10).text

    assert "crypto.subtle" in html, "找不到 crypto.subtle（应作为首选实现）"
    # 必须有对 crypto.subtle 可用性的判断（安全上下文探测）
    assert re.search(r"HAS_SUBTLE|isSecureContext|subtle\s*&&", html), \
        "缺少 crypto.subtle 可用性判断，http 下会直接崩溃"
    # 必须有纯 JS sha256 兜底实现
    assert re.search(r"function\s+sha256HexJS|sha256Fallback|sha256_js", html), \
        "缺少纯 JS SHA-256 兜底实现"
    # sha256Hex 里必须走兜底分支
    body = re.search(r"async function sha256Hex\([\s\S]*?\n            \}", html)
    assert body, "找不到 sha256Hex"
    assert re.search(r"sha256HexJS|sha256Fallback|sha256_js", body.group(0)), \
        "sha256Hex 未接入兜底实现"
    print("   ✓ 有 crypto.subtle 探测 + 纯 JS SHA-256 兜底，sha256Hex 接入兜底")


# ---------------------------------------------------------------- 2/5
@timeout(120)
def test_insecure_context_hash_matches():
    """[2/5] node 模拟无 crypto.subtle 的 http 环境，跑真实 sha256Hex/指纹，结果与 Python 一致"""
    print("\n[2/5] node 模拟非安全上下文跑真实哈希函数...")
    html = requests.get(BASE_URL + "/", timeout=10).text
    js = extract_js_block(html)

    with tempfile.TemporaryDirectory() as tmp:
        js_path = os.path.join(tmp, "frontend.js")
        data_path = os.path.join(tmp, "data.bin")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js)

        driver = """
const fs = require('fs');
globalThis.crypto = {};   // 模拟 http 非安全上下文：没有 crypto.subtle
const src = fs.readFileSync(process.argv[2], 'utf8');
const { sha256Hex, fileFingerprint, computeChunkHashes } = new Function(
    src + '\\nreturn { sha256Hex, fileFingerprint, computeChunkHashes };')();
(async () => {
    const data = fs.readFileSync(process.argv[3]);
    const chunkSize = parseInt(process.argv[4], 10);
    const blob = new Blob([data]);
    const total = Math.ceil(blob.size / chunkSize) || 1;
    const hashes = await computeChunkHashes(blob, chunkSize, total, null);
    console.log(JSON.stringify({ fp: await fileFingerprint(hashes), first: hashes[0] }));
})().catch(e => { console.log(JSON.stringify({ error: String((e && e.message) || e) })); process.exit(1); });
"""
        cases = [
            (os.urandom(300), 100),
            (os.urandom(301), 100),
            (os.urandom(50), 100),
            (os.urandom(1000), 1000),
            (b"abc", 100),
        ]
        # 先单独验证空输入（0 字节文件走 PUT 直传，不参与分片指纹，但单次 sha256 必须正确）
        with open(data_path, "wb") as f:
            f.write(b"")
        out = run_node(driver, js_path, data_path, "100")
        assert out.returncode == 0, f"node 执行失败: {out.stderr or out.stdout}"
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert "error" not in result, f"空输入哈希抛错: {result.get('error')}"
        assert result["first"] == sha256_hex(b""), f"空输入 sha256 兜底错误: {result}"

        for payload, chunk_size in cases:
            with open(data_path, "wb") as f:
                f.write(payload)
            out = run_node(driver, js_path, data_path, str(chunk_size))
            assert out.returncode == 0, f"node 执行失败: {out.stderr or out.stdout}"
            result = json.loads(out.stdout.strip().splitlines()[-1])
            assert "error" not in result, f"无 crypto.subtle 时哈希抛错: {result['error']}"
            assert result["fp"] == chunk_fingerprint(payload, chunk_size), \
                f"兜底指纹与 Python 不一致 (size={len(payload)})"
            first = payload[:chunk_size] if payload else b""
            assert result["first"] == sha256_hex(first), \
                f"兜底分片 sha256 与 Python 不一致 (size={len(payload)})"
        print(f"   ✓ {len(cases)} 组数据（含空文件/非整除边界）兜底哈希与 Python 完全一致")


# ---------------------------------------------------------------- 3/5
@timeout(120)
def test_insecure_context_real_upload():
    """[3/5] node 模拟非安全上下文，跑真实 uploadOneFileResumable 上传 >10MB 文件（真·端到端）"""
    print("\n[3/5] node 模拟 http 环境跑真实前端上传 >10MB...")
    html = requests.get(BASE_URL + "/", timeout=10).text
    js = extract_js_block(html)
    name = "test_insecure_ctx_e2e.bin"
    payload = os.urandom(12 * 1024 * 1024 + 12345)

    with tempfile.TemporaryDirectory() as tmp:
        js_path = os.path.join(tmp, "frontend.js")
        data_path = os.path.join(tmp, "big.bin")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js)
        with open(data_path, "wb") as f:
            f.write(payload)

        driver = """
const fs = require('fs');
globalThis.crypto = {};   // 模拟 http 非安全上下文
const src = fs.readFileSync(process.argv[2], 'utf8');
const base = process.argv[3];
const origFetch = globalThis.fetch;
globalThis.fetch = (u, opts) => origFetch(new URL(u, base).toString(), opts);
const { uploadOneFileResumable } = new Function(
    src + '\\nreturn { uploadOneFileResumable };')();
(async () => {
    const data = fs.readFileSync(process.argv[4]);
    const file = new File([data], process.argv[5]);
    const stages = new Set(); let lastPct = 0;
    const res = await uploadOneFileResumable(file, (pct, loaded, stage) => {
        stages.add(stage || '上传中'); lastPct = pct;
    });
    console.log(JSON.stringify({ ok: true, result: String(res), stages: [...stages], lastPct }));
})().catch(e => { console.log(JSON.stringify({ ok: false, error: String((e && e.message) || e) })); process.exit(1); });
"""
        try:
            out = run_node(driver, js_path, BASE_URL, data_path, name)
            assert out.returncode == 0, f"node 端到端失败: {out.stderr or out.stdout}"
            result = json.loads(out.stdout.strip().splitlines()[-1])
            assert result["ok"], f"非安全上下文下前端上传报错: {result.get('error')}"
            assert result["lastPct"] == 100, f"进度未到 100%: {result}"
            print(f"   ✓ http 环境下上传成功（阶段 {result['stages']}，进度 {result['lastPct']}%）")

            got = requests.get(f"{BASE_URL}/{name}", timeout=60).content
            assert sha256_hex(got) == sha256_hex(payload), "下载内容与原始不一致"
            print(f"   ✓ 下载校验通过（{len(got)} 字节，sha256 一致）")
        finally:
            requests.delete(f"{BASE_URL}/{name}", timeout=30)


# ---------------------------------------------------------------- 4/5
@timeout(120)
def test_logs_mounted_and_written():
    """[4/5] logs 目录挂载 + 服务端把日志写到 logs/server.log（主机可见）"""
    print("\n[4/5] logs 目录挂载与日志落盘...")
    compose = open(COMPOSE, "r", encoding="utf-8").read()
    assert re.search(r"[\"']?\$\{?LOGS_DIR", compose) and "/app/logs" in compose, \
        "docker-compose 未挂载 logs 目录到 /app/logs"
    assert "LOG_DIR=/app/logs" in compose, "docker-compose 未设置 LOG_DIR=/app/logs"
    print("   ✓ docker-compose 已挂载 logs 且设置 LOG_DIR=/app/logs")

    dockerfile = open(DOCKERFILE, "r", encoding="utf-8").read()
    assert re.search(r"mkdir\s+-p[^\n]*/app/logs", dockerfile), "Dockerfile.obs 未创建 /app/logs"
    print("   ✓ Dockerfile.obs 创建 /app/logs")

    src = open(SERVER_PY, "r", encoding="utf-8").read()
    assert "LOG_DIR" in src and "server.log" in src, "服务端未实现 LOG_DIR/server.log 日志"
    print("   ✓ 服务端实现了 LOG_DIR 与 server.log 日志")

    # 真实落盘：容器启动日志 + 一次 PUT 上传都写进主机 logs/server.log
    assert os.path.exists(HOST_LOG), f"主机日志不存在: {HOST_LOG}"
    before = open(HOST_LOG, "r", encoding="utf-8", errors="ignore").read()
    assert "OBS web app running" in before, "启动日志未写入 logs/server.log"

    name = "test_logs_write_tmp.txt"
    try:
        r = requests.put(f"{BASE_URL}/{name}", data=b"log-write-probe", timeout=30)
        assert r.status_code == 201, f"PUT 上传失败: {r.status_code} {r.text}"
        time.sleep(0.5)
        after = open(HOST_LOG, "r", encoding="utf-8", errors="ignore").read()
        assert len(after) > len(before), "上传后 logs/server.log 没有新增日志行"
        assert name in after, f"上传事件未写进日志: {after[-300:]}"
        print("   ✓ 启动日志 + 上传事件均已落盘到主机 logs/server.log")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)


# ---------------------------------------------------------------- 5/5
@timeout(120)
def test_regression():
    """[5/5] 回归：PUT 直传 / 分片协议 / 秒传 / 健康检查 / 首页拖拽区 / 视频页"""
    print("\n[5/5] 回归...")
    # PUT 直传
    name = "test_reg_direct_tmp.txt"
    try:
        r = requests.put(f"{BASE_URL}/{name}", data=b"regression", timeout=15)
        assert r.status_code == 201 and r.text == f"{URL_HEAD}/{name}", f"PUT 直传异常: {r.status_code} {r.text}"
        assert requests.get(f"{BASE_URL}/{name}", timeout=15).content == b"regression"
        print("   ✓ PUT 直传正常")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)

    # 分片协议
    name = "test_reg_chunk_tmp.bin"
    payload = os.urandom(300)
    chunk_size = 100
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
    fp = chunk_fingerprint(payload, chunk_size)
    try:
        info = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": len(payload), "hash_algo": "sha256",
            "hash": fp, "chunk_size": chunk_size, "total_chunks": len(chunks)}, timeout=15).json()
        uid = info["uploadId"]

        def put(i):
            return requests.put(f"{BASE_URL}/upload/chunk/{uid}/{i}", data=chunks[i],
                                headers={"X-Chunk-SHA256": sha256_hex(chunks[i])}, timeout=20).status_code

        with ThreadPoolExecutor(max_workers=3) as pool:
            codes = list(pool.map(put, range(len(chunks))))
        assert codes == [201] * len(chunks), f"分片上传状态码异常: {codes}"
        c = requests.post(f"{BASE_URL}/upload/complete/{uid}", json={
            "filename": name, "size": len(payload), "total_chunks": len(chunks),
            "chunk_size": chunk_size, "hash_algo": "sha256", "hash": fp}, timeout=30)
        assert c.status_code in (200, 201), f"complete 异常: {c.status_code} {c.text}"
        assert requests.get(f"{BASE_URL}/{name}", timeout=15).content == payload
        print("   ✓ 分片协议端到端正常")

        # 秒传
        info2 = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": len(payload), "hash_algo": "sha256",
            "hash": fp, "chunk_size": chunk_size, "total_chunks": len(chunks)}, timeout=15).json()
        assert info2.get("skip") is True, f"秒传失效: {info2}"
        print("   ✓ 秒传命中")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)

    # 健康检查 + 首页 + 视频页
    assert requests.get(f"{BASE_URL}/health", timeout=10).json()["status"] == "ok"
    home = requests.get(f"{BASE_URL}/", timeout=10).text
    assert 'id="uploadZone"' in home, "首页拖拽上传区丢失"
    video = requests.get(f"{BASE_URL}/video/app.js", timeout=10).text
    assert "REVERSE_RATE" in video, "视频页 -3x 倒放丢失"
    print("   ✓ /health + 首页拖拽区 + 视频页正常")


def main():
    print("=" * 60)
    print("任务测试：http 非安全上下文上传修复 + logs 挂载")
    print("=" * 60)
    tests = [
        test_frontend_has_insecure_context_fallback,
        test_insecure_context_hash_matches,
        test_insecure_context_real_upload,
        test_logs_mounted_and_written,
        test_regression,
    ]
    for t in tests:
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
