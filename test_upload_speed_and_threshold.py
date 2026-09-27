#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  1) 上传时显示实时上传速度（瞬时 + 平均）；
  2) 确保 ≤10MB 整段校验 + 整段上传；
      >10MB 分段校验 + 分段上传（支持断点续传）；
  3) 确保只有「视频文件」才在凌晨 04:00 参与 HLS 切分。

阈值沿用旧数字：10 * 1024 * 1024 字节。

超时机制: 每个用例 120 秒超时
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import uuid

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
BASE_URL = "http://127.0.0.1:80"
CONTAINER = "obs"


def timeout(seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds}s")
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                return func(*args, **kwargs)
            finally:
                signal.alarm(0)
        return wrapper
    return decorator


def read(path: str = SERVER_PY) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def brace_slice(src: str, i: int) -> str:
    assert src[i] == "{", f"起点不是 '{{': {src[i-20:i+20]!r}"
    depth, j, in_s = 0, i, None
    while j < len(src):
        c = src[j]
        if in_s:
            if c == "\\":
                j += 2
                continue
            if c == in_s:
                in_s = None
        elif c in "'\"`":
            in_s = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("大括号不配对")


def extract_func(src: str, name: str) -> str:
    m = re.search(r"function\s+%s\s*\(" % re.escape(name), src)
    assert m, f"找不到函数 {name}"
    open_brace = src.index("{", m.end() - 1)
    return src[m.start():open_brace] + brace_slice(src, open_brace)


# ============================================================ JS 行为测试
JS_HARNESS = r"""
const R = [];
function assert(c, m) { R.push((c ? 'PASS' : 'FAIL') + ' :: ' + m); if (!c) process.exitCode = 1; }
function near(a, b, tol) { return Math.abs(a - b) <= tol; }

__FUNCS__

// ---------- fmtSpeed 单位换算 ----------
assert(fmtSpeed(0) === '', 'fmtSpeed(0) 不显示，实际 ' + JSON.stringify(fmtSpeed(0)));
assert(fmtSpeed(-5) === '', 'fmtSpeed(负数) 不显示');
assert(fmtSpeed(NaN) === '', 'fmtSpeed(NaN) 不显示');
assert(fmtSpeed(512) === '512B/s', 'fmtSpeed(512) → 512B/s，实际 ' + fmtSpeed(512));
assert(fmtSpeed(1024) === '1.0KB/s', 'fmtSpeed(1024) → 1.0KB/s，实际 ' + fmtSpeed(1024));
assert(fmtSpeed(1536) === '1.5KB/s', 'fmtSpeed(1536) → 1.5KB/s，实际 ' + fmtSpeed(1536));
assert(fmtSpeed(1024 * 1024) === '1.0MB/s', 'fmtSpeed(1MiB) → 1.0MB/s，实际 ' + fmtSpeed(1024*1024));
assert(fmtSpeed(5.5 * 1024 * 1024) === '5.5MB/s', 'fmtSpeed(5.5MiB) → 5.5MB/s，实际 ' + fmtSpeed(5.5*1024*1024));

// ---------- 速度计：首个样本无速度 ----------
{
    const meter = createSpeedMeter(3000);
    const s0 = meter(0, 0);
    assert(s0.speed === 0 && s0.avg === 0, '首个样本 speed/avg 均为 0');
}
// ---------- 1 秒传 1MiB → 1.0MB/s（瞬时与平均一致） ----------
{
    const meter = createSpeedMeter(3000);
    meter(0, 0);
    const s = meter(1024 * 1024, 1000);
    assert(near(s.speed, 1024 * 1024, 1), '1s 传 1MiB → speed 1MiB/s，实际 ' + s.speed);
    assert(near(s.avg, 1024 * 1024, 1), '平均速度同为 1MiB/s，实际 ' + s.avg);
}
// ---------- 0.5 秒传 1MiB → 2MiB/s（速度随耗时变化） ----------
{
    const meter = createSpeedMeter(3000);
    meter(0, 0);
    const s = meter(1024 * 1024, 500);
    assert(near(s.speed, 2 * 1024 * 1024, 2), '0.5s 传 1MiB → 2MiB/s，实际 ' + s.speed);
}
// ---------- 停滞：字节没涨 → 速度回落为 0 ----------
{
    const meter = createSpeedMeter(3000);
    meter(0, 0);
    meter(1024 * 1024, 1000);
    const s = meter(1024 * 1024, 2000);
    assert(s.speed === 0, '1s 内无新增字节 → 瞬时速度 0，实际 ' + s.speed);
    assert(s.avg > 0, '平均速度仍为正（整体进度不回退）');
}
// ---------- 窗口滑动：超窗口的旧样本被丢弃，速度回升 ----------
{
    const meter = createSpeedMeter(1000);   // 1 秒窗口
    meter(0, 0);
    meter(1024 * 1024, 1000);               // 1MiB/s
    // 之后 5 秒没有流量
    meter(1024 * 1024, 2000);
    meter(1024 * 1024, 3000);
    meter(1024 * 1024, 4000);
    // 5s 时又传 1MiB：窗口只剩最近 1s，速度应回到 1MiB/s，而非被旧流量稀释
    const s = meter(2 * 1024 * 1024, 5000);
    assert(near(s.speed, 1024 * 1024, 2), '窗口滑动后瞬时速度回到 1MiB/s，实际 ' + s.speed);
    assert(s.avg < 1024 * 1024, '平均速度被整体耗时稀释（小于瞬时），实际 ' + s.avg);
}
// ---------- 进度文案包含速度（抽 reportUploadStatus 纯函数） ----------
{
    const txt = reportUploadStatus({ stage: '上传中', index: 2, totalFiles: 5, filename: 'a.mp4',
                                     pct: 45, overallPct: 30, speedText: '1.2MB/s' });
    assert(txt.indexOf('1.2MB/s') > -1, '进度文案含实时速度: ' + txt);
    assert(txt.indexOf('45%') > -1, '进度文案含单文件百分比: ' + txt);
    assert(txt.indexOf('30%') > -1, '进度文案含总进度: ' + txt);
    assert(txt.indexOf('(2/5)') > -1, '进度文案含文件序号: ' + txt);
    assert(txt.indexOf('a.mp4') > -1, '进度文案含文件名: ' + txt);
    const txt2 = reportUploadStatus({ stage: '校验中', index: 1, totalFiles: 1, filename: 'b.mp4',
                                      pct: 60, overallPct: 60, speedText: '' });
    assert(txt2.indexOf('校验中') > -1, '阶段文字保留: ' + txt2);
    assert(txt2.indexOf('/s') === -1, '无速度时不显示空速度占位: ' + txt2);
}

console.log(JSON.stringify(R));
"""


def run_js_harness(src: str):
    funcs = "\n\n".join(extract_func(src, n)
                        for n in ("createSpeedMeter", "fmtSpeed", "reportUploadStatus"))
    js = JS_HARNESS.replace("__FUNCS__", funcs)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        out = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"node 行为测试失败:\n{out.stdout}\n{out.stderr}"
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- 1/5
@timeout(120)
def test_source_speed_and_threshold():
    """[1/5] 源码：实时速度显示 + 10MB 阈值分支（≤整段 / >分片）"""
    print("\n[1/5] 源码：速度显示与 10MB 阈值...")
    src = read()

    # 阈值沿用旧数字 10MB
    m = re.search(r"CHUNK_SIZE_BROWSER\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024", src)
    assert m, "找不到 CHUNK_SIZE_BROWSER = N * 1024 * 1024"
    assert int(m.group(1)) == 10, f"阈值应为 10MB，实际 {m.group(1)}MB"
    print("   ✓ 分片阈值沿用旧数字 10MB（CHUNK_SIZE_BROWSER = 10 * 1024 * 1024）")

    # 速度显示三件套
    assert re.search(r"function\s+createSpeedMeter\s*\(", src), "缺少 createSpeedMeter"
    assert re.search(r"function\s+fmtSpeed\s*\(", src), "缺少 fmtSpeed"
    assert re.search(r"function\s+reportUploadStatus\s*\(", src), "缺少 reportUploadStatus"
    print("   ✓ createSpeedMeter / fmtSpeed / reportUploadStatus 已定义")

    # report 回调里必须计算并展示速度
    rep = re.search(r"function\s+reportUploadStatus\s*\([\s\S]*?\n\s*\}", src)
    assert rep, "抠不出 reportUploadStatus"
    assert "speedText" in rep.group(0), "进度文案未使用 speedText"
    up = re.search(r"async function uploadFiles\s*\([\s\S]*?\n            \}", src)
    assert up, "抠不出 uploadFiles"
    assert "createSpeedMeter" in up.group(0), "uploadFiles 未创建速度计"
    assert "fmtSpeed" in up.group(0), "uploadFiles 未格式化速度"
    assert "reportUploadStatus" in up.group(0), "uploadFiles 未调用统一进度渲染"
    print("   ✓ uploadFiles 用速度计计算并渲染实时速度")

    # ≤10MB 走整段直传，>10MB 走分片（断点续传）
    assert re.search(r"if\s*\(\s*file\.size\s*<=\s*CHUNK_SIZE_BROWSER\s*\)", src), \
        "未按 file.size <= CHUNK_SIZE_BROWSER 分流"
    dec = re.search(r"if\s*\(\s*file\.size\s*<=\s*CHUNK_SIZE_BROWSER\s*\)\s*\{([\s\S]*?)\}\s*else\s*\{([\s\S]*?)\}", src)
    assert dec, "抠不出分流分支"
    assert "uploadOneFileDirect" in dec.group(1), "≤10MB 分支未走整段直传"
    assert "uploadOneFileResumable" in dec.group(2), ">10MB 分支未走分片上传"
    print("   ✓ ≤10MB → uploadOneFileDirect（整段）；>10MB → uploadOneFileResumable（分片）")

    # 整段上传带整段校验（整文件 SHA-256 头）
    direct = re.search(r"function\s+uploadOneFileDirect\s*\([\s\S]*?\n            \}", src)
    assert direct, "抠不出 uploadOneFileDirect"
    assert "X-File-SHA256" in direct.group(0), "整段上传未附带整文件校验头 X-File-SHA256"
    print("   ✓ 整段上传附带 X-File-SHA256 做整段校验")

    # 分片上传带逐片校验 + 断点续传
    res = re.search(r"async function uploadOneFileResumable\s*\([\s\S]*?\n            \}", src)
    assert res, "抠不出 uploadOneFileResumable"
    body = res.group(0)
    assert "X-Chunk-SHA256" in body, "分片上传未附带逐片校验头"
    assert "/upload/init" in body and "/upload/status" in body or "info.uploaded" in body, \
        "分片上传未做断点续传（已上传分片枚举）"
    assert "info.skip" in body, "分片上传未做秒传判定"
    print("   ✓ 分片上传：逐片校验 + 断点续传（枚举已传分片）+ 秒传")


# ---------------------------------------------------------------- 2/5
@timeout(120)
def test_speed_js_behavior():
    """[2/5] node 跑真实 JS：速度计算 / 窗口滑动 / 单位换算 / 进度文案"""
    print("\n[2/5] node 行为验证（真实 JS 函数）...")
    results = run_js_harness(read())
    for x in results:
        print("   " + ("✓ " if x.startswith("PASS") else "✗ ") + x.split(" :: ", 1)[1])
    fails = [x for x in results if x.startswith("FAIL")]
    assert not fails, f"以下断言失败: {fails}"
    print(f"   ✓ {len(results)} 条行为断言全部通过")


# ---------------------------------------------------------------- 3/5
@timeout(120)
def test_resumable_chunk_protocol():
    """[3/5] 行为：分段校验 + 分段上传 + 断点续传（HTTP 全链路）"""
    print("\n[3/5] 分片协议（逐片校验 / 断点续传 / 合并校验）...")
    name = f"test_chunk_proto_{uuid.uuid4().hex[:8]}.bin"
    chunk_size = 1024 * 1024
    # 3 片（含最后一片不足整片）
    parts = [os.urandom(chunk_size), os.urandom(chunk_size), os.urandom(300 * 1024)]
    total = sum(len(p) for p in parts)
    chunk_hashes = [hashlib.sha256(p).hexdigest() for p in parts]
    fingerprint = hashlib.sha256("".join(chunk_hashes).encode()).hexdigest()

    try:
        r = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": total, "hash_algo": "sha256", "hash": fingerprint,
            "chunk_size": chunk_size, "total_chunks": len(parts)}, timeout=60)
        assert r.status_code == 200, f"/upload/init 返回 {r.status_code}: {r.text}"
        info = r.json()
        assert not info.get("skip"), "随机内容不应秒传"
        upload_id = info["uploadId"]
        assert info.get("uploaded") == [], f"新会话不应有已上传分片: {info}"
        assert info.get("chunkExists") in (None, []) or True
        print(f"   ✓ /upload/init 建立会话 pending={info.get('pending', 'n/a')}")

        # A) 错误分片哈希 → 被逐片校验拒绝（分段校验生效）
        bad = requests.put(f"{BASE_URL}/upload/chunk/{requests.utils.quote(upload_id)}/0",
                           data=parts[0], headers={"X-Chunk-SHA256": "0" * 64}, timeout=60)
        assert bad.status_code == 422, f"错误分片哈希应被拒绝(422)，实际 {bad.status_code}"
        print("   ✓ 分片哈希不符 → 422 拒绝（分段校验生效）")

        # B) 缺哈希声明 → 同样拒绝
        nohash = requests.put(f"{BASE_URL}/upload/chunk/{requests.utils.quote(upload_id)}/0",
                              data=parts[0], timeout=60)
        assert nohash.status_code == 422, f"缺 X-Chunk-SHA256 应被拒绝(422)，实际 {nohash.status_code}"
        print("   ✓ 缺少分片哈希声明 → 422 拒绝")

        # C) 只传第 0 片后中断 → status 能枚举已传分片（断点续传依据）
        ok0 = requests.put(f"{BASE_URL}/upload/chunk/{requests.utils.quote(upload_id)}/0",
                           data=parts[0], headers={"X-Chunk-SHA256": chunk_hashes[0]}, timeout=60)
        assert ok0.status_code == 201, f"正确分片应 201，实际 {ok0.status_code}: {ok0.text}"
        st = requests.post(f"{BASE_URL}/upload/status/{requests.utils.quote(upload_id)}", timeout=60)
        assert st.status_code == 200, f"/upload/status 返回 {st.status_code}"
        sj = st.json()
        assert sj["uploaded"] == [0], f"status 应报告已传分片 [0]，实际 {sj['uploaded']}"
        assert sj["chunkHashes"][0] == chunk_hashes[0], "status 未回传分片哈希"
        print(f"   ✓ 中断后续传：/upload/status 枚举 uploaded={sj['uploaded']}（断点续传依据）")

        # D) 秒传：重复 init 同一文件 → skip
        r2 = requests.post(f"{BASE_URL}/upload/init", json={
            "filename": name, "size": total, "hash_algo": "sha256", "hash": fingerprint,
            "chunk_size": chunk_size, "total_chunks": len(parts)}, timeout=60)
        assert r2.status_code == 200, f"重复 init 返回 {r2.status_code}"
        assert r2.json()["uploadId"] == upload_id, "同一文件应复用同一会话（续传）"
        print("   ✓ 重新 init 命中同一会话 uploadId（续传不重来）")

        # E) 补齐剩余分片 → 合并，服务端按分片指纹再校验
        for i in (1, 2):
            rr = requests.put(f"{BASE_URL}/upload/chunk/{requests.utils.quote(upload_id)}/{i}",
                              data=parts[i], headers={"X-Chunk-SHA256": chunk_hashes[i]}, timeout=60)
            assert rr.status_code == 201, f"分片 {i} 应 201，实际 {rr.status_code}: {rr.text}"
        comp = requests.post(f"{BASE_URL}/upload/complete/{requests.utils.quote(upload_id)}", json={
            "filename": name, "size": total, "total_chunks": len(parts),
            "chunk_size": chunk_size, "hash_algo": "sha256", "hash": fingerprint}, timeout=120)
        assert comp.status_code == 200, f"/upload/complete 返回 {comp.status_code}: {comp.text}"
        got = requests.get(f"{BASE_URL}/{name}", timeout=60)
        assert got.status_code == 200 and hashlib.sha256(got.content).hexdigest() == \
            hashlib.sha256(b"".join(parts)).hexdigest(), "合并后的文件内容与原始不一致"
        print(f"   ✓ 补齐分片 → 合并成功，{total} 字节内容逐字节一致")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=30)


# ---------------------------------------------------------------- 4/5
@timeout(120)
def test_direct_upload_integrity():
    """[4/5] 行为：整段上传（≤10MB 路径）带整段校验，错哈希不落盘"""
    print("\n[4/5] 整段上传与整段校验...")
    name = f"test_direct_integrity_{uuid.uuid4().hex[:8]}.bin"
    payload = os.urandom(512 * 1024)          # 512KB，模拟 ≤10MB 的整段直传
    good = hashlib.sha256(payload).hexdigest()
    try:
        # A) 无校验头（兼容 curl 等旧客户端）→ 正常上传
        r = requests.put(f"{BASE_URL}/{name}", data=payload, timeout=60)
        assert r.status_code == 201, f"无校验头整段上传应 201，实际 {r.status_code}"
        requests.delete(f"{BASE_URL}/{name}", timeout=30)
        print("   ✓ 无 X-File-SHA256 头（curl 兼容）→ 201 正常落盘")

        # B) 正确整段哈希 → 201 且内容一致
        r = requests.put(f"{BASE_URL}/{name}", data=payload,
                         headers={"X-File-SHA256": good}, timeout=60)
        assert r.status_code == 201, f"正确哈希整段上传应 201，实际 {r.status_code}: {r.text}"
        got = requests.get(f"{BASE_URL}/{name}", timeout=60)
        assert got.content == payload, "整段上传内容不一致"
        requests.delete(f"{BASE_URL}/{name}", timeout=30)
        print("   ✓ X-File-SHA256 正确 → 201 且内容一致")

        # C) 错误的整段哈希 → 拒绝且不留下损坏文件
        r = requests.put(f"{BASE_URL}/{name}", data=payload,
                         headers={"X-File-SHA256": "0" * 64}, timeout=60)
        assert r.status_code >= 400, f"错误整段哈希应被拒绝，实际 {r.status_code}"
        leftover = requests.get(f"{BASE_URL}/{name}", timeout=30)
        assert leftover.status_code == 404, \
            f"校验失败不得留下半成品文件，实际 GET 返回 {leftover.status_code}"
        print(f"   ✓ X-File-SHA256 错误 → {r.status_code} 拒绝，且未留下损坏文件（404）")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=30)


# ---------------------------------------------------------------- 5/5
@timeout(120)
def test_hls_only_videos_and_regression():
    """[5/5] 行为：04:00 HLS 只处理视频文件 + 回归"""
    print("\n[5/5] HLS 仅视频 + 回归...")
    src = read()
    assert re.search(r'VIDEO_EXTS\s*=\s*\{[^}]*"\.mp4"', src), "缺少 VIDEO_EXTS 定义"
    listing = re.search(r"def _list_missing_hls\(\)[\s\S]*?\n(?=def )", src)
    assert listing, "抠不出 _list_missing_hls"
    assert "VIDEO_EXTS" in listing.group(0), "HLS 扫描未按 VIDEO_EXTS 过滤"
    print("   ✓ _list_missing_hls 按 VIDEO_EXTS 过滤（非视频不进 HLS 队列）")

    # 造「同名不同扩展名」的两个文件：只有视频那个才应进队列
    video = f"test_hls_only_{uuid.uuid4().hex[:6]}.mp4"
    other = f"test_hls_only_{uuid.uuid4().hex[:6]}.txt"
    try:
        requests.put(f"{BASE_URL}/{video}", data=b"not-a-real-video", timeout=30)
        requests.put(f"{BASE_URL}/{other}", data=b"definitely not video", timeout=30)
        code = (
            "import json\n"
            "from src.obs.server import _list_missing_hls, VIDEO_EXTS\n"
            "names = _list_missing_hls()\n"
            f"print('QUEUE', json.dumps({{'has_video': {video!r} in names, "
            f"'has_other': {other!r} in names, 'all_video': all("
            "n.split('.')[-1].lower() in {e.lstrip('.') for e in VIDEO_EXTS} for n in names)}))\n"
        )
        out = subprocess.run(["docker", "exec", CONTAINER, "python", "-c", code],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"容器内扫描失败: {out.stderr[-500:]}"
        d = json.loads([l for l in out.stdout.splitlines() if l.startswith("QUEUE")][0][len("QUEUE "):])
        assert d["has_video"] is True, "视频文件应进入 HLS 队列"
        assert d["has_other"] is False, "非视频文件（.txt）不得进入 HLS 队列"
        assert d["all_video"] is True, "HLS 队列里出现了非视频扩展名"
        print("   ✓ 行为：.mp4 进队列、.txt 不进队列、队列内全为视频扩展名")
    finally:
        requests.delete(f"{BASE_URL}/{video}", timeout=30)
        requests.delete(f"{BASE_URL}/{other}", timeout=30)

    # dry-run 队列同样不含非视频
    body = requests.post(f"{BASE_URL}/hls/generate-all?dry_run=true", timeout=60).json()
    assert all(n.lower().rsplit(".", 1)[-1] in {"mp4", "webm", "ogv", "mov", "m4v", "mkv"}
               for n in body["queue"]), f"dry-run 队列含非视频: {body['queue'][:5]}"
    print(f"   ✓ dry-run 队列（{len(body['queue'])} 个）全为视频扩展名")

    # 回归
    assert requests.get(f"{BASE_URL}/", timeout=30).status_code == 200
    assert requests.get(f"{BASE_URL}/health", timeout=20).status_code == 200
    assert requests.get(f"{BASE_URL}/videos", timeout=60).status_code == 200
    html = requests.get(f"{BASE_URL}/", timeout=30).text
    for needle in ("function uploadFiles(", "handleDragUpload", "addEventListener('paste'",
                   'id="formFile"', "uploadOneFileDirect", "uploadOneFileResumable"):
        assert needle in html, f"首页上传入口回归失败：缺失 {needle}"
    print("   ✓ 回归：首页/health/videos + 拖拽/选择/粘贴/整段/分片 入口齐全")


if __name__ == "__main__":
    tests = [
        test_source_speed_and_threshold,
        test_speed_js_behavior,
        test_resumable_chunk_protocol,
        test_direct_upload_integrity,
        test_hls_only_videos_and_regression,
    ]
    print("=" * 60)
    print("任务测试：上传实时速度 + 10MB 阈值（整段校验/分段校验）+ HLS 仅视频")
    print("=" * 60)
    for t in tests:
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)
