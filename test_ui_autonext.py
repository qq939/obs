#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  1) 视频播放完成自动切换到下一个视频
  2) obs 首页上传区 UI 改成 commit 30982962c52d762abc52980f134f77df32d0d2ce
     那一版（拖拽上传区），但只改 UI，上传能力（分片 + 秒传）保持不变

超时机制: 60秒超时
"""
import os
import re
import sys
import signal
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
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


def test_url_head_regression():
    """[1/5] 回归：url_head 无残留硬编码 URL"""
    print("\n[1/5] 回归 url_head...")
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert any(re.match(r'^url_head\s*=\s*"http://obs\.dimond\.top"\s*$', l) for l in lines[:10]), \
        "server.py 顶部缺少 url_head 定义"
    leftovers = [(i + 1, l.strip()) for i, l in enumerate(lines)
                 if URL_HEAD in l and not re.match(r'^url_head\s*=', l)]
    assert not leftovers, f"仍残留硬编码 URL: {leftovers}"
    html = requests.get(BASE_URL + "/", timeout=5).text
    assert f"curl --upload-file file.txt {URL_HEAD}/file.txt" in html, "首页 curl 示例未使用 url_head"
    print("   ✓ url_head 生效且无残留硬编码")


def test_homepage_upload_zone_ui():
    """[2/5] 首页上传区 = 3098296 版 UI（拖拽上传区），旧的三个上传控件已移除"""
    print("\n[2/5] 首页拖拽上传区 UI...")
    resp = requests.get(BASE_URL + "/", timeout=5)
    assert resp.status_code == 200, f"首页返回状态码 {resp.status_code}"
    html = resp.text

    assert 'id="uploadZone"' in html, "缺少 3098296 版的 #uploadZone 拖拽上传区"
    assert "拖拽文件到此处上传" in html, "缺少拖拽提示文案"
    assert 'id="formFile"' in html and "multiple" in html, "缺少 #formFile 多选文件输入"
    assert "选择文件" in html, "缺少「选择文件」按钮"
    assert 'id="formUploadStatus"' in html, "缺少 #formUploadStatus 上传状态显示位"
    assert "border: 2px dashed" in html, "上传区不是虚线拖拽框样式"
    print("   ✓ 拖拽上传区 UI 与 3098296 版一致")

    for old in ['id="chunkFile"', 'id="resumeFile"', 'value="上传"', 'enctype="multipart/form-data"']:
        assert old not in html, f"旧上传控件仍残留: {old}"
    print("   ✓ 旧的分片/断点续传/表单上传控件已移除")


def test_upload_capability_kept():
    """[3/5] 只改 UI：分片上传 + 秒传（断点续传）能力仍保留"""
    print("\n[3/5] 上传能力保留（分片 + 秒传）...")
    html = requests.get(BASE_URL + "/", timeout=5).text
    for ep in ["/upload/init", "/upload/chunk/", "/upload/complete/"]:
        assert ep in html, f"首页 JS 缺少上传接口调用: {ep}"
    assert re.search(r"function\s+uploadFiles\s*\(", html), "缺少统一的 uploadFiles 入口"
    assert "拖拽" not in html or "handleDragUpload" in html or "dataTransfer" in html, \
        "拖拽上传区未绑定拖放处理"
    assert re.search(r"上传中\.\.\.", html), "上传过程中没有状态文案展示"
    print("   ✓ 分片 + 秒传接口调用保留，拖拽/进度状态就位")

    # 实传校验：PUT 上传 -> 首页链接可见 -> 删除
    name = "test_ui_autonext_tmp.txt"
    try:
        r = requests.put(f"{BASE_URL}/{name}", data=b"ui autonext tdd", timeout=10)
        assert r.status_code == 201, f"PUT 上传返回 {r.status_code}"
        assert f'href="{URL_HEAD}/{name}"' in requests.get(BASE_URL + "/", timeout=5).text
        print("   ✓ 上传链路（PUT）回归正常")
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=5)


def test_video_autonext_on_ended():
    """[4/5] 视频播放完成自动切换到下一个视频"""
    print("\n[4/5] 播放完成自动切下一个视频...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()

    assert re.search(r"video\.loop\s*=\s*false", js), \
        "video.loop 必须为 false，否则不会触发 ended（无法自动切下一个）"
    print("   ✓ video.loop = false（播放完成会触发 ended）")

    m = re.search(r"video\.addEventListener\('ended',\s*\(\)\s*=>\s*\{(.*?)\n    \}\);", js, re.S)
    assert m, "缺少 video 的 ended 监听"
    body = m.group(1)

    assert re.search(r"vertAnimateTo\(vertBaseTop - h,\s*1\)", body), \
        "ended 后未走上滑(下一个视频)同款吸附动画路径"
    assert re.search(r"videos\.length\s*===\s*0", body), "ended 未处理空列表"
    assert re.search(r"reverseActive", body), "第一页倒放走到尽头未做跳回结尾处理（会卡住不动）"
    print("   ✓ ended -> 切换到下一个视频（与上滑同路径），倒放场景不误触发")


def test_video_keepalive_regression():
    """[5/5] 回归：切页保活（任何页面都不暂停）与 -3x 倒放、3/5/7 档位"""
    print("\n[5/5] 视频保活 / 倒放 / 速度档位回归...")
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页未直接负速率倒放"
    assert re.search(r"currentPage\s*===\s*0\s*\)\s*\{[\s\S]*?startReverse\(\)", js), "第一页未走 startReverse()"
    assert re.search(r"video\.play\(\)\.catch", js), "缺少保活 play 调用"
    html = requests.get(BASE_URL + "/video", timeout=5).text
    btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
    assert btns == ["3", "5", "7"], f"播放速度档位应为 3/5/7，实际 {btns}"
    print("   ✓ 保活 / -3x 倒放 / 3-5-7 档位均正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：播放完成自动切下一个视频 + 首页拖拽上传区 UI")
    print("=" * 60)
    try:
        test_url_head_regression()
        test_homepage_upload_zone_ui()
        test_upload_capability_kept()
        test_video_autonext_on_ended()
        test_video_keepalive_regression()
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
