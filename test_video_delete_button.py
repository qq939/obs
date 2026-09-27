#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  在 /video 页面第三页（设置页）把标题「设置」两个字替换成「删除」按钮；
  点击后删除「当前正在播放的视频文件」以及它的 HLS 文件（分片目录）。

要求：
  1) index.html 里设置页标题不再显示「设置」，改为「删除」按钮（有稳定 id）；
  2) app.js 绑定该按钮 → 删除 videos[activeIndex]（当前播放项），并刷新列表；
  3) 后端 DELETE /{filename} 除了删原视频，还要一并删除 hls/{filename}/ 目录，
     不留孤儿 HLS 分片；文件不存在仍返回 404；
  4) 首页删除按钮与播放器浮层删除按钮行为保持（同样受益于 HLS 清理）。

超时机制: 每个用例 300 秒超时（含一次真实 HLS 生成）
"""
import os
import re
import shutil
import signal
import subprocess
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
VIDEO_INDEX = os.path.join(ROOT, "src", "obs", "video_static", "index.html")
VIDEO_APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
STYLE_CSS = os.path.join(ROOT, "src", "obs", "video_static", "style.css")
HLS_DIR = os.path.join(ROOT, "obs_shards")
OBS_DIR = os.path.join(ROOT, "obs")
BASE_URL = "http://127.0.0.1:80"
CONTAINER = "obs"

TEST_VIDEO = "test_del_btn_tmp.mp4"


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


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def make_test_video():
    cmd = ["docker", "exec", CONTAINER, "ffmpeg", "-y",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", f"/app/obs/{TEST_VIDEO}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"造测试视频失败: {out.stderr[-500:]}"
    assert os.path.exists(os.path.join(OBS_DIR, TEST_VIDEO)), "测试视频未落到主机 obs/"


def cleanup():
    try:
        requests.delete(f"{BASE_URL}/{TEST_VIDEO}", timeout=30)
    except Exception:
        pass
    subprocess.run(["docker", "exec", CONTAINER, "rm", "-f", f"/app/obs/{TEST_VIDEO}"],
                   capture_output=True, timeout=30)
    shutil.rmtree(os.path.join(HLS_DIR, TEST_VIDEO), ignore_errors=True)


# ---------------------------------------------------------------- 1/4
@timeout(300)
def test_source_delete_button():
    """[1/4] 源码：设置页标题换成删除按钮 + 绑定删除当前视频 + 后端清理 HLS"""
    print("\n[1/4] 源码：删除按钮与 HLS 清理...")
    html = read(VIDEO_INDEX)

    # 「设置」两个字被删除按钮替换（精确定位设置页 settingsPanel 内的 panel-head，
    # 注意信息页也有一个 panel-head「播放信息」，不能误取）
    sp = html.index('id="settingsPanel"')
    hs = html.index('<div class="panel-head">', sp)
    he = html.index('</div>', hs) + len('</div>')
    hb = html[hs:he]
    assert "<span>设置</span>" not in hb, "设置页标题仍是「设置」二字，未替换"
    assert "删除" in hb, "设置页 panel-head 内没有「删除」按钮"
    m = re.search(r'id="([^"]*[Dd]elete[^"]*)"', hb) or re.search(r'id="(btnDeleteCurrent)"', hb)
    assert m, "删除按钮缺少稳定 id（应含 Delete）"
    assert "button" in hb, "「删除」不是 button 元素"
    print(f"   ✓ 设置页标题已换成删除按钮 id={m.group(1)}")

    # 上传按钮仍在（别把 panel-head 搞坏）
    assert 'id="fabUpload"' in html, "上传按钮丢失"
    print("   ✓ 上传按钮仍在（panel-head 结构完好）")

    js = read(VIDEO_APP_JS)
    # 绑定删除按钮
    assert re.search(r"getElementById\('btnDeleteCurrent'\)", js), \
        "app.js 未获取删除按钮引用"
    assert re.search(r"btnDeleteCurrent\.addEventListener\('click'", js), \
        "app.js 未给删除按钮绑定 click"
    # 删除的是「当前播放」项
    assert re.search(r"videos\[activeIndex\]", js), "删除逻辑未取 videos[activeIndex]（当前播放项）"
    # 调用 DELETE
    assert re.search(r"method:\s*'DELETE'", js), "app.js 未发起 DELETE 请求"
    print("   ✓ app.js 绑定删除按钮 → 删除 videos[activeIndex]（当前播放项）")

    # 后端 DELETE 需一并清理 HLS 目录
    src = read(SERVER_PY)
    dele = re.search(r'@app\.delete\("/\{filename\}"\)[\s\S]*?(?=\n# 启动服务器|@app\.)', src)
    assert dele, "抠不出 DELETE /{filename}"
    db = dele.group(0)
    assert "hls" in db.lower(), "DELETE 未涉及 HLS 目录清理"
    assert re.search(r"get_hls_dir\(\)", db), "DELETE 未使用 get_hls_dir()"
    assert re.search(r"rmtree", db), "DELETE 未用 shutil.rmtree 清理 HLS 目录"
    print("   ✓ 后端 DELETE /{filename} 会 rmtree 掉 hls/{filename}/（不留孤儿分片）")


# ---------------------------------------------------------------- 2/4
@timeout(300)
def test_page_serves_delete_button():
    """[2/4] HTTP：/video 真实下发删除按钮，且设置页其余控件未变"""
    print("\n[2/4] 页面下发...")
    html = requests.get(f"{BASE_URL}/video", timeout=30).text
    assert 'id="btnDeleteCurrent"' in html, "/video 未下发删除按钮"
    assert "<span>设置</span>" not in html, "/video 仍在显示「设置」二字"
    assert "删除" in html, "/video 未下发「删除」文案"
    assert 'id="speedOptions"' in html, "播放速度控件丢失"
    assert 'id="seekTrack"' in html, "进度条丢失"
    assert 'id="fabUpload"' in html, "上传按钮丢失"
    js = requests.get(f"{BASE_URL}/video/app.js", timeout=30).text
    assert "btnDeleteCurrent" in js, "/video/app.js 未下发删除按钮逻辑"
    print("   ✓ /video 下发删除按钮，速度档位/进度条/上传按钮均在")


# ---------------------------------------------------------------- 3/4
@timeout(300)
def test_delete_removes_file_and_hls():
    """[3/4] 行为：DELETE 同时删原视频与 HLS 分片目录（真实生成 HLS 后删）"""
    print("\n[3/4] 真实删除视频 + HLS...")
    cleanup()
    make_test_video()
    print(f"   - 已造测试视频 {TEST_VIDEO}")

    # 用生产代码路径生成 HLS
    code = (
        "import asyncio\n"
        "from src.obs.server import generate_hls\n"
        f"print('MADE', asyncio.run(generate_hls({TEST_VIDEO!r})))\n"
    )
    out = subprocess.run(["docker", "exec", CONTAINER, "python", "-c", code],
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, f"生成 HLS 失败: {out.stderr[-800:]}"
    hls_sub = os.path.join(HLS_DIR, TEST_VIDEO)
    assert os.path.isdir(hls_sub), f"HLS 目录未生成: {hls_sub}"
    assert os.path.isfile(os.path.join(hls_sub, "index.m3u8")), "缺少 index.m3u8"
    assert os.path.isfile(os.path.join(OBS_DIR, TEST_VIDEO)), "原视频不存在"
    print("   - HLS 已生成（index.m3u8 + 分片）")

    # HTTP DELETE
    r = requests.delete(f"{BASE_URL}/{TEST_VIDEO}", timeout=60)
    assert r.status_code == 200, f"DELETE 返回 {r.status_code}: {r.text}"
    print(f"   ✓ DELETE /{TEST_VIDEO} → 200")

    # 原视频消失
    assert not os.path.exists(os.path.join(OBS_DIR, TEST_VIDEO)), "原视频未被删除"
    assert requests.get(f"{BASE_URL}/{TEST_VIDEO}", timeout=30).status_code == 404, \
        "原视频仍可访问（应为 404）"
    print("   ✓ 原视频已删除（文件系统 + HTTP 均 404）")

    # HLS 目录消失
    assert not os.path.exists(hls_sub), f"HLS 目录未被删除: {hls_sub}"
    m3 = requests.get(f"{BASE_URL}/hls/{requests.utils.quote(TEST_VIDEO)}/index.m3u8", timeout=20)
    assert m3.status_code == 404, f"HLS 播放列表仍可访问（应为 404），实际 {m3.status_code}"
    print("   ✓ HLS 分片目录已随视频一并删除（/hls/... 404）")

    # 再从 /videos 列表确认不残留
    vids = requests.get(f"{BASE_URL}/videos", timeout=60).json()["videos"]
    assert TEST_VIDEO not in [v["name"] for v in vids], "/videos 仍列出已删除的视频"
    print("   ✓ /videos 列表不再包含该视频")


# ---------------------------------------------------------------- 4/4
@timeout(300)
def test_regression():
    """[4/4] 回归：删除不存在文件 404、非视频删除不报错、既有页面与接口正常"""
    print("\n[4/4] 回归...")
    # 删除不存在的文件 → 404
    r = requests.delete(f"{BASE_URL}/__not_exist_%d.mp4" % int(time.time()), timeout=30)
    assert r.status_code == 404, f"删除不存在文件应 404，实际 {r.status_code}"
    print("   ✓ 删除不存在的文件 → 404（行为未变）")

    # 非视频文件删除：不应因找不到 HLS 目录而报错
    name = f"test_del_nonvideo_{int(time.time())}.txt"
    try:
        assert requests.put(f"{BASE_URL}/{name}", data=b"hello", timeout=30).status_code == 201
        d = requests.delete(f"{BASE_URL}/{name}", timeout=30)
        assert d.status_code == 200, f"删除非视频文件应 200，实际 {d.status_code}: {d.text}"
        print("   ✓ 删除无 HLS 的非视频文件 → 200（不因缺目录报错）")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=30)

    # 既有页面/接口
    assert requests.get(f"{BASE_URL}/", timeout=30).status_code == 200
    assert requests.get(f"{BASE_URL}/health", timeout=20).status_code == 200
    assert requests.get(f"{BASE_URL}/videos", timeout=60).status_code == 200
    assert requests.get(f"{BASE_URL}/hls/cron", timeout=20).status_code == 200
    html = requests.get(f"{BASE_URL}/video", timeout=30).text
    for needle in ('id="playlistStrip"', 'id="uploadModal"', 'id="randomSwitch"',
                   'id="autoplaySwitch"', 'id="videoCount"'):
        assert needle in html, f"/video 缺少 {needle}"
    print("   ✓ 首页/health/videos/hls-cron 正常，/video 设置页控件齐全")


# ---------------------------------------------------------------- 5/5
@timeout(300)
def test_delete_and_upload_button_style_consistent():
    """[5/5] 格式一致：删除按钮与上传按钮共用同一套视觉样式，仅左右定位不同"""
    print("\n[5/5] 删除 / 上传按钮格式一致性...")
    html = read(VIDEO_INDEX)
    css = read(STYLE_CSS)

    # 1) HTML：两个按钮必须带同一个基类
    assert re.search(r'class="panel-head-btn[^"]*"[^>]*id="btnDeleteCurrent"', html) or \
           re.search(r'id="btnDeleteCurrent"[^>]*class="panel-head-btn[^"]*"', html), \
        "删除按钮未带公共基类 panel-head-btn"
    assert re.search(r'class="panel-head-btn[^"]*"[^>]*id="fabUpload"', html) or \
           re.search(r'id="fabUpload"[^>]*class="panel-head-btn[^"]*"', html), \
        "上传按钮未带公共基类 panel-head-btn"
    print("   ✓ 两个按钮共用基类 panel-head-btn")

    # 2) CSS：基类里集中所有视觉属性
    base = re.search(r"\.panel-head-btn\s*\{([^}]*)\}", css)
    assert base, "缺少 .panel-head-btn 基类样式"
    bb = base.group(1)
    for prop in ("border", "background", "color", "font-size", "font-weight",
                 "line-height", "padding", "border-radius", "cursor",
                 "box-shadow", "transition"):
        assert re.search(r"(^|[;\s])" + re.escape(prop) + r"\s*:", bb), \
            f"基类 .panel-head-btn 缺少视觉属性 {prop}"
    print("   ✓ 基类集中定义 border/background/color/字号/内边距/圆角/阴影/过渡")

    # 3) CSS：两个按钮各自只负责定位，不再重复写视觉属性
    for cls in ("btn-delete-current", "fab-upload"):
        rule = re.search(r"\.%s\s*\{([^}]*)\}" % re.escape(cls), css)
        assert rule, f"缺少 .{cls} 规则"
        body = rule.group(1)
        for prop in ("background", "color", "font-size", "font-weight",
                     "padding", "border-radius", "box-shadow"):
            assert not re.search(r"(^|[;\s])" + re.escape(prop) + r"\s*:", body), \
                f".{cls} 仍重复定义 {prop}（应与上传按钮共用基类，保持格式一致）"
    # 左右定位相反
    assert re.search(r"\.btn-delete-current\s*\{[^}]*left\s*:", css), "删除按钮未定位在左侧"
    assert re.search(r"\.fab-upload\s*\{[^}]*right\s*:", css), "上传按钮未定位在右侧"
    print("   ✓ 两个按钮仅 left/right 定位不同，视觉属性统一来自基类")

    # 4) HTTP：页面下发的样式与新结构一致
    page = requests.get(f"{BASE_URL}/video", timeout=30).text
    assert "panel-head-btn" in page, "/video 未下发公共基类"
    live_css = requests.get(f"{BASE_URL}/video/style.css", timeout=30).text
    assert ".panel-head-btn" in live_css, "/video/style.css 未下发基类样式"
    assert re.search(r"\.btn-delete-current\s*\{[^}]*left\s*:", live_css), \
        "线上 CSS 删除按钮定位异常"
    print("   ✓ 线上 /video 与 style.css 均使用统一基类")


if __name__ == "__main__":
    tests = [
        test_source_delete_button,
        test_page_serves_delete_button,
        test_delete_removes_file_and_hls,
        test_regression,
        test_delete_and_upload_button_style_consistent,
    ]
    print("=" * 60)
    print("任务测试：/video 第三页「设置」→「删除」按钮（删视频 + HLS）")
    print("=" * 60)
    for t in tests:
        t()
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)
