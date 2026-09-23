#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  新增「每天 04:00 定时检查未生成 HLS 的视频并生成 HLS 分片」的定时任务。

现状：当前 src/obs/server.py 完全没有 HLS 生成能力（只有 hls_exists / hls_duration_sync
等只读辅助函数，历史上 obs-video-app 的 /hls/generate-all 与 cron 在二合一重写时丢失）。

要求：
  1) 进程内定时任务，每天（Asia/Shanghai）04:00 触发一次；
  2) 扫描 obs/ 下所有视频，只挑「尚无有效 HLS」的，逐个生成 HLS 分片（ffmpeg，串行）；
  3) 提供手动触发端点 POST /hls/generate-all 与状态端点 GET /hls/cron。

超时机制: 每个用例 300 秒超时（HLS 生成需要时间）
"""
import json
import os
import re
import signal
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
HLS_DIR = os.path.join(ROOT, "obs_shards")
BASE_URL = "http://127.0.0.1:80"
CONTAINER = "obs"

# 测试用视频（容器内 /app/obs 与之 bind mount 到主机 obs/）
TEST_VIDEO = "test_hls_cron_tmp.mp4"


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


def make_test_video():
    """用容器内 ffmpeg 造一个 3 秒的小视频，放到 obs/（H.264+AAC，可 remux）"""
    cmd = [
        "docker", "exec", CONTAINER, "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        f"/app/obs/{TEST_VIDEO}",
    ]
    import subprocess
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"造测试视频失败: {out.stderr[-500:]}"
    assert os.path.exists(os.path.join(ROOT, "obs", TEST_VIDEO)), "测试视频未落到主机 obs/"


def cleanup_test_video():
    import shutil
    import subprocess
    try:
        requests.delete(f"{BASE_URL}/{TEST_VIDEO}", timeout=30)
    except Exception:
        pass
    subprocess.run(["docker", "exec", CONTAINER, "rm", "-f", f"/app/obs/{TEST_VIDEO}"],
                   capture_output=True, timeout=30)
    shutil.rmtree(os.path.join(HLS_DIR, TEST_VIDEO), ignore_errors=True)


# ---------------------------------------------------------------- 1/6
@timeout(300)
def test_source_cron_registered():
    """[1/6] 源码：04:00 定时任务 + lifespan 注册后台协程 + 每日去重 + generate-all 端点"""
    print("\n[1/6] 源码定时任务与端点...")
    src = read_server()

    # 04:00 配置
    m = re.search(r'HLS_CRON_HOUR\s*=\s*int\(os\.environ\.get\([^)]*,\s*(\d+)\)', src)
    assert m, "缺少 HLS_CRON_HOUR 配置"
    assert int(m.group(1)) == 4, f"HLS_CRON_HOUR 默认应为 4，实际 {m.group(1)}"
    m = re.search(r'HLS_CRON_MINUTE\s*=\s*int\(os\.environ\.get\([^)]*,\s*(\d+)\)', src)
    assert m and int(m.group(1)) == 0, "HLS_CRON_MINUTE 默认应为 0"
    assert re.search(r'HLS_CRON_TZ\s*=\s*os\.environ\.get\([^)]*Asia/Shanghai', src), \
        "缺少 HLS_CRON_TZ 默认 Asia/Shanghai"
    print("   ✓ 定时配置：每天 04:00（Asia/Shanghai，可用环境变量覆盖）")

    # 每天只触发一次的日期键去重
    assert re.search(r"def hls_cron_key\(", src), "缺少 hls_cron_key（每日去重键）"
    assert re.search(r"_hls_cron_last_key", src), "缺少 _hls_cron_last_key 去重状态"
    tick = re.search(r"async def hls_cron_tick\(\)[\s\S]*?\n(?=async def |def |@app\.)", src)
    assert tick, "缺少 hls_cron_tick"
    assert "hls_cron_key" in tick.group(0), "hls_cron_tick 未用日期键去重"
    assert re.search(r"key\s*==\s*_hls_cron_last_key", tick.group(0)), "hls_cron_tick 未做同日去重"
    print("   ✓ 每日去重（同一天只触发一次）")

    # lifespan 里注册后台任务并在关闭时取消
    life = re.search(r"async def lifespan\(app: FastAPI\):[\s\S]*?\napp = FastAPI", src)
    assert life, "找不到 lifespan"
    assert "create_task(" in life.group(0), "lifespan 未注册后台定时协程"
    assert "cancel()" in life.group(0), "lifespan 关闭时未取消定时协程"
    print("   ✓ lifespan 注册/取消后台定时协程")

    # 手动触发 + 状态端点
    assert re.search(r'@app\.post\("/hls/generate-all"\)', src), "缺少 POST /hls/generate-all"
    assert re.search(r'@app\.get\("/hls/cron"\)', src), "缺少 GET /hls/cron"
    # 同一视频并发只跑一次 ffmpeg
    assert re.search(r"def _get_hls_lock|HLS_LOCKS", src), "缺少 per-name 生成锁"
    # /hls/generate-all 必须在 /hls/{filename}/{path} 之前声明，否则会被通配路由吞掉
    i_gen = src.index('@app.post("/hls/generate-all")')
    i_wild = src.index('@app.get("/hls/{filename}/{path:path}")')
    assert i_gen < i_wild, "/hls/generate-all 必须声明在 /hls/{filename}/{path} 之前"
    print("   ✓ POST /hls/generate-all + GET /hls/cron + per-name 生成锁 + 路由顺序正确")


# ---------------------------------------------------------------- 2/6
@timeout(300)
def test_cron_status_endpoint():
    """[2/6] 行为：GET /hls/cron 返回正确的调度信息"""
    print("\n[2/6] 调度状态端点...")
    r = requests.get(f"{BASE_URL}/hls/cron", timeout=15)
    assert r.status_code == 200, f"/hls/cron 返回 {r.status_code}"
    d = r.json()
    for k in ("enabled", "hour", "minute", "timezone", "tickSeconds", "lastFiredKey"):
        assert k in d, f"/hls/cron 缺少字段 {k}: {d}"
    assert d["hour"] == 4 and d["minute"] == 0, f"调度时间不是 04:00: {d}"
    assert d["timezone"] == "Asia/Shanghai", f"时区异常: {d['timezone']}"
    assert d["enabled"] is True, "定时任务未启用"
    assert d["tickSeconds"] == 30, f"tick 间隔异常: {d['tickSeconds']}"
    print(f"   ✓ {d['timezone']} 每天 {d['hour']:02d}:{d['minute']:02d}，tick {d['tickSeconds']}s，enabled={d['enabled']}")


# ---------------------------------------------------------------- 3/6
@timeout(300)
def test_cron_key_time_matching():
    """[3/6] 行为：在容器内注入时间验证 hls_cron_key 只在 04:00 命中且每天一个键"""
    print("\n[3/6] 定时命中逻辑（容器内注入时间）...")
    code = (
        "import json\n"
        "from datetime import datetime, timedelta, timezone\n"
        "from src.obs.server import hls_cron_key\n"
        "tz='Asia/Shanghai'\n"
        "def k(y,m,d,h,mi):\n"
        "    t=datetime(y,m,d,h,mi,tzinfo=timezone(timedelta(hours=8)))\n"
        "    return hls_cron_key(t,tz,4,0)\n"
        "print(json.dumps({\n"
        "  'at_0400': k(2026,9,20,4,0),\n"
        "  'at_0359': k(2026,9,20,3,59),\n"
        "  'at_0401': k(2026,9,20,4,1),\n"
        "  'at_1600_utc': hls_cron_key(datetime(2026,9,19,20,0,tzinfo=timezone.utc),tz,4,0),\n"
        "  'next_day': k(2026,9,21,4,0),\n"
        "}))\n"
    )
    import subprocess
    out = subprocess.run(["docker", "exec", CONTAINER, "python", "-c", code],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"容器内计算失败: {out.stderr[-500:]}"
    d = json.loads(out.stdout.strip().splitlines()[-1])
    assert d["at_0400"] == "2026-09-20", f"04:00 应命中，实际 {d['at_0400']}"
    assert d["at_0359"] is None, f"03:59 不应命中，实际 {d['at_0359']}"
    assert d["at_0401"] is None, f"04:01 不应命中，实际 {d['at_0401']}"
    assert d["at_1600_utc"] == "2026-09-20", f"UTC 20:00 = 北京 04:00 应命中，实际 {d['at_1600_utc']}"
    assert d["next_day"] == "2026-09-21", f"次日应产生新键，实际 {d['next_day']}"
    assert d["at_0400"] != d["next_day"], "每日去重键必须每天不同"
    print(f"   ✓ 04:00 命中({d['at_0400']})、03:59/04:01 不命中、UTC 换算正确、每日键不同")


# ---------------------------------------------------------------- 4/6
@timeout(300)
def test_generate_hls_produces_valid_output():
    """[4/6] 行为：真实调用 generate_hls 生成有效 HLS（小测试视频，走生产目录）"""
    print("\n[4/6] 真实生成 HLS（容器内调 generate_hls）...")
    cleanup_test_video()
    make_test_video()
    print(f"   - 已造测试视频 {TEST_VIDEO}")

    # 生成前：hlsReady 应为 false
    vids = requests.get(f"{BASE_URL}/videos", timeout=60).json()["videos"]
    tv = [v for v in vids if v["name"] == TEST_VIDEO]
    assert tv and tv[0]["hlsReady"] is False, f"测试视频初始 hlsReady 应为 False: {tv}"
    print("   - 生成前 hlsReady=False")

    import subprocess
    code = (
        "import asyncio\n"
        "from src.obs.server import generate_hls, hls_exists\n"
        f"made = asyncio.run(generate_hls({TEST_VIDEO!r}))\n"
        f"print('MADE', made, 'EXISTS', hls_exists({TEST_VIDEO!r}))\n"
        # 再次调用应当幂等（已存在 → 不重复生成）
        f"print('AGAIN', asyncio.run(generate_hls({TEST_VIDEO!r})))\n"
    )
    out = subprocess.run(["docker", "exec", CONTAINER, "python", "-c", code],
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, f"容器内 generate_hls 失败: {out.stderr[-800:]}"
    assert "MADE True EXISTS True" in out.stdout, f"生成结果异常: {out.stdout}"
    assert "AGAIN False" in out.stdout, f"重复调用未幂等: {out.stdout}"
    print("   ✓ generate_hls 生成成功且幂等（重复调用不重跑 ffmpeg）")

    # 产物校验
    d = os.path.join(HLS_DIR, TEST_VIDEO)
    m3u8 = os.path.join(d, "index.m3u8")
    meta_p = os.path.join(d, "meta.json")
    assert os.path.isfile(m3u8), "缺少 index.m3u8"
    assert os.path.isfile(meta_p), "缺少 meta.json"
    segs = [f for f in os.listdir(d) if re.match(r"^seg-\d+\.ts$", f)]
    assert segs, "没有生成任何 seg-*.ts 分片"
    content = open(m3u8, encoding="utf-8").read()
    assert content.startswith("#EXTM3U"), "index.m3u8 头部异常"
    assert "#EXT-X-ENDLIST" in content, "VOD 播放列表应有 #EXT-X-ENDLIST"
    assert "#EXTINF" in content, "index.m3u8 缺少 #EXTINF"
    meta = json.load(open(meta_p))
    assert meta.get("version") == 4, f"meta.json version 应为 4: {meta}"
    assert meta.get("size") == os.path.getsize(os.path.join(ROOT, "obs", TEST_VIDEO)), \
        "meta.json size 与源文件不一致"
    assert meta.get("duration", 0) > 0, f"meta.json duration 异常: {meta}"
    print(f"   ✓ 产物 {len(segs)} 个分片，meta version={meta['version']} duration={meta['duration']:.2f}s")

    # HTTP：hlsReady 变 true，且 /hls/ 端点可播放
    vids = requests.get(f"{BASE_URL}/videos", timeout=60).json()["videos"]
    tv = [v for v in vids if v["name"] == TEST_VIDEO][0]
    assert tv["hlsReady"] is True, f"生成后 hlsReady 应为 True: {tv}"
    assert abs(tv["duration"] - meta["duration"]) < 0.5, "列表 duration 与 HLS 时长不一致"
    m3 = requests.get(f"{BASE_URL}/hls/{requests.utils.quote(TEST_VIDEO)}/index.m3u8", timeout=20)
    assert m3.status_code == 200, f"/hls m3u8 返回 {m3.status_code}"
    assert m3.text.startswith("#EXTM3U"), "/hls 下发的 m3u8 内容异常"
    seg0 = requests.get(f"{BASE_URL}/hls/{requests.utils.quote(TEST_VIDEO)}/{segs[0]}", timeout=20)
    assert seg0.status_code == 200 and len(seg0.content) > 0, f"分片下载失败: {seg0.status_code}"
    print(f"   ✓ hlsReady=True，/hls 端点可访问（m3u8 + {segs[0]} {len(seg0.content)} 字节）")


# ---------------------------------------------------------------- 5/6
@timeout(300)
def test_sweep_and_queue_selection():
    """[5/6] 行为：sweep_missing_hls 在隔离目录中真正生成；generate-all 的 queue 与实现一致"""
    print("\n[5/6] sweep 隔离验证 + queue 一致性...")

    # A) 隔离目录里跑 sweep（与 cron 完全同一条代码路径），不触碰生产数据
    import subprocess
    base = "/tmp/hls_sweep_test"
    code = (
        "import asyncio, json, os, shutil\n"
        f"base = {base!r}\n"
        "shutil.rmtree(base, ignore_errors=True)\n"
        "os.makedirs(base + '/obs'); os.makedirs(base + '/hls')\n"
        f"shutil.copy('/app/obs/{TEST_VIDEO}', base + '/obs/{TEST_VIDEO}')\n"
        "os.environ['UPLOAD_DIR'] = base + '/obs'\n"
        "os.environ['HLS_DIR'] = base + '/hls'\n"
        "from src.obs.server import sweep_missing_hls, hls_exists\n"
        "res = asyncio.run(sweep_missing_hls('test'))\n"
        f"print('RESULT', json.dumps({{'queue': res['queue'], 'done': res['done'], 'failed': res['failed'], 'exists': hls_exists({TEST_VIDEO!r})}}))\n"
    )
    out = subprocess.run(["docker", "exec", CONTAINER, "python", "-c", code],
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, f"隔离 sweep 失败: {out.stderr[-800:]}"
    line = [l for l in out.stdout.splitlines() if l.startswith("RESULT")][0]
    res = json.loads(line[len("RESULT "):])
    assert res["queue"] == [TEST_VIDEO], f"隔离目录中应只发现 1 个待生成，实际 {res['queue']}"
    assert res["done"] == [TEST_VIDEO], f"生成应成功，实际 done={res['done']} failed={res['failed']}"
    assert res["exists"] is True, "sweep 后 hls_exists 应为 True"
    # 隔离目录里确有产物
    chk = subprocess.run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         f"ls {base}/hls/{TEST_VIDEO}/index.m3u8 {base}/hls/{TEST_VIDEO}/meta.json && "
         f"ls {base}/hls/{TEST_VIDEO}/ | grep -c '^seg-'"],
        capture_output=True, text=True, timeout=60)
    assert chk.returncode == 0, f"隔离目录产物缺失: {chk.stderr}"
    print("   ✓ 隔离 sweep：只挑缺 HLS 的视频并真实生成产物（含 index.m3u8/meta.json/分片）")

    # B) HTTP dry-run：queue 必须等于「缺有效 HLS 的视频集合」，且不启动生成
    r = requests.post(f"{BASE_URL}/hls/generate-all?dry_run=true", timeout=60)
    assert r.status_code == 200, f"generate-all dry_run 返回 {r.status_code}"
    body = r.json()
    assert body.get("started") is False, f"dry_run 不应启动生成: {body}"
    vids = requests.get(f"{BASE_URL}/videos", timeout=60).json()["videos"]
    missing = [v["name"] for v in vids if not v["hlsReady"]]
    assert sorted(body["queue"]) == sorted(missing), \
        f"queue 与「缺 HLS 的视频」不一致:\n  queue={sorted(body['queue'])}\n  missing={sorted(missing)}"
    assert TEST_VIDEO not in body["queue"], f"已生成 HLS 的视频仍被排队: {body['queue']}"
    # 源文件已删除的孤儿 HLS 目录不在 obs/ 列表内，不应入队
    orphan = "高颜丝袜颜值女神一口八个汉堡直播回放mp4.mp4"
    assert orphan not in body["queue"], "已删除源文件的孤儿 HLS 不应被排队"
    print(f"   ✓ dry-run queue == 缺 HLS 集合（{len(missing)} 个），不含已生成的与孤儿产物")


# ---------------------------------------------------------------- 6/6
@timeout(300)
def test_regression():
    """[6/6] 回归：/videos 并发不阻塞、播放页/进度条/保活/倒放/档位/箭头/上传 均正常"""
    print("\n[6/6] 回归...")
    from concurrent.futures import ThreadPoolExecutor

    def do_videos():
        return requests.get(f"{BASE_URL}/videos", timeout=120).status_code

    with ThreadPoolExecutor(4) as ex:
        fv = [ex.submit(do_videos) for _ in range(3)]
        t = time.time()
        h = requests.get(f"{BASE_URL}/health", timeout=60)
        health_lat = round(time.time() - t, 3)
        codes = [f.result() for f in fv]
    assert all(c == 200 for c in codes), f"并发 /videos 失败: {codes}"
    assert h.status_code == 200
    assert health_lat < 3.0, f"并发 /videos 期间 /health 耗时 {health_lat}s（事件循环被阻塞）"
    print(f"   ✓ 3 并发 /videos 期间 /health 仅 {health_lat}s")

    js = requests.get(f"{BASE_URL}/video/app.js", timeout=20).text
    html = requests.get(f"{BASE_URL}/video", timeout=20).text
    css = requests.get(f"{BASE_URL}/video/style.css", timeout=20).text
    assert "function ensurePlaying" in js, "切页保活丢失"
    assert "REVERSE_RATE" in js, "-3x 倒放丢失"
    left = re.search(r"case\s+'ArrowLeft'\s*:(.*?)break;", js, re.S)
    right = re.search(r"case\s+'ArrowRight'\s*:(.*?)break;", js, re.S)
    assert left and re.search(r"setPage\(currentPage\s*-\s*1\)", left.group(1)), "ArrowLeft 互换丢失"
    assert right and re.search(r"setPage\(currentPage\s*\+\s*1\)", right.group(1)), "ArrowRight 互换丢失"
    for s in ('data-speed="1"', 'data-speed="2"', 'data-speed="7"'):
        assert s in html, f"档位丢失: {s}"
    for sel in (".seek-track", ".seek-track::before", ".seek-fill"):
        b = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        hm = re.search(r"height\s*:\s*(\d+)px", b.group(1))
        assert hm and int(hm.group(1)) >= 100, f"{sel} 进度条高度异常"
    home = requests.get(f"{BASE_URL}/", timeout=20).text
    assert "HAS_SUBTLE" in home and "sha256HexJS" in home, "http 哈希兜底丢失"
    print("   ✓ 保活 / 倒放 / 箭头互换 / 1-2-7 档位 / 进度条 100px / 上传兜底 正常")

    name = "test_hls_cron_reg_tmp.txt"
    try:
        assert requests.put(f"{BASE_URL}/{name}", data=b"reg", timeout=20).status_code == 201
        assert requests.get(f"{BASE_URL}/{name}", timeout=20).content == b"reg"
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=20)
    assert os.path.exists(os.path.join(ROOT, "logs", "server.log")), "logs 挂载丢失"
    print("   ✓ 上传 + logs 挂载正常")


def main():
    print("=" * 60)
    print("任务测试：每天 04:00 自动检查并为缺 HLS 的视频生成 HLS 分片")
    print("=" * 60)
    try:
        for t in (
            test_source_cron_registered,
            test_cron_status_endpoint,
            test_cron_key_time_matching,
            test_generate_hls_produces_valid_output,
            test_sweep_and_queue_selection,
            test_regression,
        ):
            t()
    finally:
        cleanup_test_video()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
