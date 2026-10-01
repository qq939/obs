#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：视频页第三页设置区排版
  - 视频数量 / 随播 / 自播 三项放到同一行
  - 视频数量只显示数字，不带「视频数量」四个字
  - 随机播放 -> 随播；自动播放 -> 自播

超时机制: 60秒超时
"""
import os
import re
import sys
import signal

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(ROOT, "src", "obs", "video_static", "index.html")
STYLE_CSS = os.path.join(ROOT, "src", "obs", "video_static", "style.css")
APP_JS = os.path.join(ROOT, "src", "obs", "video_static", "app.js")
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


def get_settings_row(html: str) -> str:
    """取出 class 含 setting-row-inline 的那一行（div 配对计数，不用第三方解析库）"""
    m = re.search(r'<div class="setting-row setting-row-inline">', html)
    assert m, "找不到同一行的容器 div.setting-row.setting-row-inline"
    start = m.start()
    depth = 0
    i = start
    while i < len(html):
        nxt_open = html.find("<div", i)
        nxt_close = html.find("</div>", i)
        if nxt_close == -1:
            break
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 6
            if depth == 0:
                return html[start:i]
    raise AssertionError("setting-row-inline 的 div 未正确闭合")


def strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment)


# ---------------------------------------------------------------- 1/6
def test_three_items_same_row():
    """[1/6] 视频数量 / 随播 / 自播 三项在同一个 setting-row 容器内（同一行）"""
    print("\n[1/6] 三项同一行...")
    html = requests.get(BASE_URL + "/video", timeout=10).text
    row = get_settings_row(html)

    for item in ('id="videoCount"', 'id="randomSwitch"', 'id="autoplaySwitch"'):
        assert item in row, f"{item} 不在同一行的容器内"
    assert row.count("<div") == 1, f"该行里出现了额外的 div 嵌套（会换行）: {row.count('<div')} 个"
    print("   ✓ 三个控件都在同一个 setting-row-inline 容器里，且无额外 div 嵌套")

    # 不能残留成三个独立的 setting-row（旧版就是三行）
    assert not re.search(r'<div class="setting-row"><span>随机播放</span>', html), "旧的「随机播放」整行仍在"
    assert not re.search(r'<div class="setting-row"><span>自动播放</span>', html), "旧的「自动播放」整行仍在"
    assert not re.search(r'<div class="setting-row"><span>视频数量</span>', html), "旧的「视频数量」整行仍在"
    print("   ✓ 旧的三个整行 setting-row 已移除")

    css = requests.get(BASE_URL + "/video/style.css", timeout=10).text
    m = re.search(r"\.setting-row-inline \.inline-item \{[^}]*\}", css)
    assert m, "缺少 .setting-row-inline .inline-item 样式"
    assert "inline-flex" in m.group(0), "inline-item 未用 inline-flex（无法同行排列）"
    print("   ✓ CSS 里 .inline-item 使用 inline-flex，三项同行排列")


# ---------------------------------------------------------------- 2/6
def test_count_is_number_only():
    """[2/6] 视频数量只显示数字，不带「视频数量」四个字"""
    print("\n[2/6] 数量只显示数字...")
    html = requests.get(BASE_URL + "/video", timeout=10).text
    row = get_settings_row(html)

    assert "视频数量" not in html, "页面上仍出现「视频数量」四个字"
    m = re.search(r'<b id="videoCount">(\d+)</b>', row)
    assert m, "同一行里找不到只含数字的 #videoCount 节点"
    print(f"   ✓ #videoCount 渲染内容为纯数字「{m.group(1)}」，页面已无「视频数量」字样")

    # 整行去掉标签后应当只剩：数字 + 随播 + 自播
    text = re.sub(r"\s+", "", strip_tags(re.sub(r'<label class="switch">.*?</label>', "", row, flags=re.S)))
    assert text == f"{m.group(1)}随播自播", f"该行文本应为「0随播自播」，实际「{text}」"
    print("   ✓ 整行文本恰为「数字 + 随播 + 自播」")


# ---------------------------------------------------------------- 3/6
def test_labels_abbreviated():
    """[3/6] 「随机播放」简写成「随播」，「自动播放」简写成「自播」"""
    print("\n[3/6] 文案简写...")
    html = requests.get(BASE_URL + "/video", timeout=10).text
    row = get_settings_row(html)

    assert "随播" in row, "缺少「随播」"
    assert "自播" in row, "缺少「自播」"
    assert "随机播放" not in html, "页面上仍出现「随机播放」"
    assert "自动播放" not in html, "页面上仍出现「自动播放」"
    print("   ✓ 文案为「随播」「自播」，旧文案「随机播放」「自动播放」已移除")

    # 开关与文字要对应：随播 = randomSwitch，自播 = autoplaySwitch
    assert re.search(r"随播<label class=\"switch\"><input type=\"checkbox\" id=\"randomSwitch\"", row), \
        "「随播」未绑定 randomSwitch"
    assert re.search(r"自播<label class=\"switch\"><input type=\"checkbox\" id=\"autoplaySwitch\"", row), \
        "「自播」未绑定 autoplaySwitch"
    print("   ✓ 随播 -> randomSwitch，自播 -> autoplaySwitch，绑定正确")


# ---------------------------------------------------------------- 4/6
def test_switch_behavior_unchanged():
    """[4/6] 开关行为不变：app.js 仍按 id 取元素并生效"""
    print("\n[4/6] 开关行为回归...")
    js = requests.get(BASE_URL + "/video/app.js", timeout=10).text
    assert "getElementById('videoCount')" in js, "app.js 未取 videoCount"
    assert "getElementById('randomSwitch')" in js, "app.js 未取 randomSwitch"
    assert "getElementById('autoplaySwitch')" in js, "app.js 未取 autoplaySwitch"
    assert re.search(r"videoCount\.textContent\s*=\s*videos\.length", js), "videoCount 未绑定视频数量"
    assert re.search(r"randomSwitch\.addEventListener\('change'", js), "randomSwitch 监听丢失"
    assert re.search(r"autoplaySwitch\.addEventListener\('change'", js), "autoplaySwitch 监听丢失"
    print("   ✓ 三个控件仍按原 id 被 app.js 使用，监听与赋值逻辑完好")

    html = requests.get(BASE_URL + "/video", timeout=10).text
    assert 'id="randomSwitch" checked' in html and 'id="autoplaySwitch" checked' in html, "默认勾选状态被改动"
    print("   ✓ 默认仍为勾选状态")


# ---------------------------------------------------------------- 5/6
def test_source_files_consistent():
    """[5/6] 源码与线上下发一致（确认改的是源码而不是只改了容器）"""
    print("\n[5/6] 源码一致性...")
    for path, url in ((INDEX_HTML, "/video"), (STYLE_CSS, "/video/style.css"), (APP_JS, "/video/app.js")):
        local = open(path, "r", encoding="utf-8").read()
        remote = requests.get(BASE_URL + url, timeout=10).text
        assert local.strip() == remote.strip(), f"{os.path.basename(path)} 与线上下发内容不一致"
    print("   ✓ index.html / style.css / app.js 与线上下发逐字节一致")


# ---------------------------------------------------------------- 6/6
def test_regression():
    """[6/6] 回归：标题跳转 / 删除按钮 / 播放速度 / 倒放 / 自动切下一个 / obs 上传接口"""
    print("\n[6/6] 回归...")
    home = requests.get(BASE_URL + "/", timeout=10).text
    m = re.search(r'<h1[^>]*>\s*(?:<a[^>]*>)?([^<]*)', home)
    assert m, "首页标题结构异常"
    assert re.search(r'<a[^>]+href="/video"[^>]*>', home), "首页标题跳转 /video 丢失"
    print("   ✓ 首页标题仍跳转 /video")

    html = requests.get(BASE_URL + "/video", timeout=10).text
    assert re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html) == ["1", "2", "7"], "档位应为 1/2/7"
    assert 'id="btnDeleteCurrent"' in html and 'id="fabUpload"' in html, "删除/上传按钮丢失"
    print("   ✓ 速度档位 1/2/7、删除与上传按钮均在")

    js = requests.get(BASE_URL + "/video/app.js", timeout=10).text
    assert re.search(r"video\.loop\s*=\s*false", js), "video.loop 应为 false"
    assert re.search(r"video\.addEventListener\('ended'", js), "ended 自动切下一个丢失"
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "第一页 -3x 倒放丢失"
    print("   ✓ 自动切下一个 / -3x 倒放 正常")

    assert requests.get(f"{BASE_URL}/health", timeout=10).json().get("status") == "ok", "/health 异常"
    name = "test_settings_row_reg.tmp"
    try:
        assert requests.put(f"{BASE_URL}/{name}", data=b"regression", timeout=10).status_code == 201, "PUT 直传异常"
        assert requests.get(f"{BASE_URL}/{name}", timeout=10).content == b"regression", "直传内容不一致"
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)
    assert 'id="uploadZone"' in home, "首页拖拽上传区丢失"
    print("   ✓ /health、PUT 直传、首页拖拽区 正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：视频页 视频数量/随播/自播 同一行 + 文案简写")
    print("=" * 60)
    try:
        test_three_items_same_row()
        test_count_is_number_only()
        test_labels_abbreviated()
        test_switch_behavior_unchanged()
        test_source_files_consistent()
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
