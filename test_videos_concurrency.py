#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：修复「多个客户端同时用 video 会卡顿」。

根因（已实测）：
  GET /videos 是 async 路由，但内部 **同步调用** list_video_files()，
  其中对每个「没有 HLS」的视频都要 subprocess.run(["ffprobe", ...], timeout=4) 探测时长。
  本项目 42 个视频中 28 个无 HLS → 单次 /videos ≈ 2.3~3.1s，且**全程阻塞事件循环**。
  3 个客户端同时打开 video 页 → 84 次 ffprobe 串行执行，
  实测期间 /health 从 0.009s 恶化到 **17.68s**，所有正在播放的视频流（Range 请求）全被拖住 → 卡顿。

修复：
  1) /videos 改为 await asyncio.to_thread(list_video_files) —— 不再阻塞事件循环；
  2) 时长探测加内存缓存（key = 路径+size+mtime+hls签名），避免重复 ffprobe；
  3) 未命中缓存的时长探测用线程池并发，缩短首次加载时间。

超时机制: 每个用例 150 秒超时
"""
import json
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
BASE_URL = "http://127.0.0.1:80"


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


def read_server() -> str:
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def timed_get(path, headers=None, timeout_s=120):
    t = time.time()
    r = requests.get(BASE_URL + path, headers=headers or {}, timeout=timeout_s)
    return round(time.time() - t, 3), r


def restart_container():
    subprocess.run(["docker", "restart", "obs"], capture_output=True, text=True, timeout=120)
    for _ in range(60):
        try:
            if requests.get(BASE_URL + "/health", timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError("容器重启后未就绪")


# ---------------------------------------------------------------- 1/5
@timeout(150)
def test_videos_offloaded_to_thread():
    """[1/5] 源码：/videos 走 asyncio.to_thread，不再同步阻塞事件循环"""
    print("\n[1/5] 源码 /videos 不再阻塞事件循环...")
    src = read_server()

    m = re.search(r"async def videos_list\(\):(.*?)(?=\n@app\.|\n# ===|\nasync def |\ndef )", src, re.S)
    assert m, "找不到 videos_list 路由"
    body = m.group(1)
    assert re.search(r"asyncio\.to_thread\(\s*list_video_files", body), \
        f"/videos 未用 asyncio.to_thread 包裹 list_video_files（会阻塞事件循环）: {body.strip()}"
    assert not re.search(r"JSONResponse\(\{[^}]*list_video_files\(\)", body), \
        "/videos 仍直接同步调用 list_video_files()"
    print("   ✓ /videos 已 offload 到线程池")


# ---------------------------------------------------------------- 2/5
@timeout(150)
def test_duration_cache_and_parallel_probe():
    """[2/5] 源码：时长探测有缓存 + 未命中时并发探测"""
    print("\n[2/5] 源码时长缓存与并发探测...")
    src = read_server()

    assert "_duration_cache" in src, "缺少时长缓存 _duration_cache"
    assert re.search(r"def probe_duration_sync\(file_path.*?\):", src, re.S), "找不到 probe_duration_sync"
    probe = re.search(r"def probe_duration_sync\([\s\S]*?\n(?=def |\n# )", src).group(0)
    assert "_duration_cache" in probe, "probe_duration_sync 未接入缓存"
    assert "mtime" in probe and "st_size" in probe, "缓存 key 未包含 mtime/size（文件变更后会读到脏值）"
    print("   ✓ 时长探测已缓存（key 含 size/mtime）")

    assert re.search(r"DURATION_PROBE_WORKERS\s*=\s*\d+", src), "缺少时长探测并发度常量"
    lvf = re.search(r"def list_video_files\(\)[\s\S]*?\n(?=def )", src).group(0)
    assert re.search(r"ThreadPoolExecutor|executor|\.map\(", lvf), \
        "list_video_files 未并发探测时长（首次加载仍会串行 28 次 ffprobe）"
    print("   ✓ 未命中缓存的时长探测并发执行")


# ---------------------------------------------------------------- 3/5
@timeout(150)
def test_hot_cache_fast_and_data_correct():
    """[3/5] 行为：热缓存后 /videos 显著变快，且数据完整正确"""
    print("\n[3/5] 热缓存加速 + 数据正确性...")
    # 预热
    warm_t, warm = timed_get("/videos")
    assert warm.status_code == 200
    first = json.loads(warm.text)
    n = len(first["videos"])

    hot_t, hot = timed_get("/videos")
    assert hot.status_code == 200
    second = json.loads(hot.text)

    assert hot_t < 0.5, f"热缓存 /videos 仍耗时 {hot_t}s（应 < 0.5s）"
    assert len(second["videos"]) == n, "缓存后视频数量不一致"
    # 缓存不得破坏字段
    for v in second["videos"]:
        for k in ("name", "size", "mtime", "duration", "url", "hls", "hlsReady"):
            assert k in v, f"返回项缺少字段 {k}"
    assert any(v["duration"] > 0 for v in second["videos"]), "所有视频 duration 都为 0，探测异常"
    print(f"   ✓ 预热 {warm_t}s → 热缓存 {hot_t}s（{n} 个视频，字段完整）")


# ---------------------------------------------------------------- 4/5
@timeout(150)
def test_cold_start_does_not_block_event_loop():
    """[4/5] 行为：冷启动（重启容器清缓存）下并发 /videos 不阻塞其它请求"""
    print("\n[4/5] 冷启动并发不阻塞（重启容器清缓存）...")
    restart_container()
    print("   - 容器已重启，缓存为空")

    def do_videos():
        return timed_get("/videos")

    def do_health():
        return timed_get("/health")

    health_lat = None
    with ThreadPoolExecutor(6) as ex:
        fv = [ex.submit(do_videos) for _ in range(3)]
        time.sleep(0.05)                     # 让 /videos 先跑起来
        fh = ex.submit(do_health)            # 同时打一个轻量请求
        health_lat, health_resp = fh.result()
        videos = [f.result() for f in fv]

    assert health_resp.status_code == 200
    assert all(r.status_code == 200 for _, r in videos), "并发 /videos 有失败"
    # 修复前实测 17.68s；修复后应远小于此
    assert health_lat < 3.0, \
        f"并发 /videos 期间 /health 耗时 {health_lat}s（事件循环疑似仍被阻塞，修复前为 17.68s）"
    print(f"   ✓ 3 并发 /videos（{[t for t, _ in videos]}s）期间 /health 仅 {health_lat}s")


# ---------------------------------------------------------------- 5/5
@timeout(150)
def test_regression():
    """[5/5] 回归：视频 Range 流 / 首页 / 上传 / 进度条 / 保活 / 倒放 / 档位 均正常"""
    print("\n[5/5] 回归...")
    # 视频 Range 流
    lat, r = timed_get("/obs/2042.mp4", headers={"Range": "bytes=0-1048575"})
    assert r.status_code == 206 and len(r.content) == 1048576, f"Range 请求异常: {r.status_code}"
    print(f"   ✓ /obs Range 1MB 正常（{lat}s）")

    home = requests.get(BASE_URL + "/", timeout=15).text
    assert "文件托管" in home
    assert "HAS_SUBTLE" in home and "sha256HexJS" in home, "http 哈希兜底丢失"
    print("   ✓ 首页 + http 哈希兜底正常")

    html = requests.get(BASE_URL + "/video", timeout=15).text
    assert len(re.findall(r'<section class="page"', html)) == 3, "页面布局被改动"
    for s in ('data-speed="1"', 'data-speed="2"', 'data-speed="7"'):
        assert s in html, f"档位丢失: {s}"
    css = requests.get(BASE_URL + "/video/style.css", timeout=15).text
    for sel in (".seek-track", ".seek-track::before", ".seek-fill"):
        b = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        m = re.search(r"height\s*:\s*(\d+)px", b.group(1))
        assert m and int(m.group(1)) >= 100, f"{sel} 进度条高度异常"
    js = requests.get(BASE_URL + "/video/app.js", timeout=15).text
    assert "function ensurePlaying" in js, "切页保活丢失"
    assert "REVERSE_RATE" in js, "-3x 倒放丢失"
    print("   ✓ 三页布局 / 进度条 100px / 保活 / 倒放 / 1-2-7 档位正常")

    # 上传
    name = "test_concurrency_reg_tmp.txt"
    try:
        r = requests.put(f"{BASE_URL}/{name}", data=b"reg", timeout=15)
        assert r.status_code == 201
        assert requests.get(f"{BASE_URL}/{name}", timeout=15).content == b"reg"
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)
    assert os.path.exists(os.path.join(ROOT, "logs", "server.log")), "logs 挂载丢失"
    print("   ✓ 上传 + logs 挂载正常")


def main():
    print("=" * 60)
    print("任务测试：修复多客户端并发播放卡顿（/videos 阻塞事件循环）")
    print("=" * 60)
    for t in (
        test_videos_offloaded_to_thread,
        test_duration_cache_and_parallel_probe,
        test_hot_cache_fast_and_data_correct,
        test_cold_start_does_not_block_event_loop,
        test_regression,
    ):
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
