#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：视频页第三页播放速度排版
  - 「速」+ 1x / 2x / 7x 放到同一行
  - 「播放速度」简写成「速」

注：上一任务（数量/随播/自播 同一行）的断言已并入本脚本回归组。

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


def html_block(html: str, opening: str) -> str:
    """按 div 配对计数取出某段 HTML 片段（不用第三方解析库）"""
    idx = html.index(opening)
    depth, i = 0, idx
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
                return html[idx:i]
    raise AssertionError(f"片段未正确闭合: {opening}")


def css_block(css: str, selector: str) -> str:
    if selector.startswith(".") and not any(c in selector for c in " >:["):
        pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    else:
        pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    m = re.search(pattern, css)
    assert m, f"CSS 里找不到 {selector}"
    return m.group(1)


# ---------------------------------------------------------------- 1/6
def test_label_and_options_same_row():
    """[1/6] 「速」与 1x/2x/7x 在同一行的同一个容器里"""
    print("\n[1/6] 「速」+ 三档同一行...")
    html = requests.get(BASE_URL + "/video", timeout=10).text
    row = html_block(html, '<div class="setting-row speed-row">')

    assert re.search(r"<span>\s*速\s*</span>", row), "同一行里找不到「速」标签"
    assert '<div class="speed-options" id="speedOptions">' in row, "档位容器不在同一行"
    assert "1x" in row and "2x" in row and "7x" in row, "三档不在同一行"
    assert row.count("<div") == 2, \
        f"该行里 div 数量应为 2（外层 + speed-options），实际 {row.count('<div')} 个（多余嵌套会换行）"
    print("   ✓ 「速」与 1x/2x/7x 都在同一个 speed-row 容器内，无多余嵌套")

    text = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", row))
    assert text == "速1x2x7x", f"该行文本应为「速1x2x7x」，实际「{text}」"
    print("   ✓ 整行文本恰为「速1x2x7x」")


# ---------------------------------------------------------------- 2/6
def test_label_abbreviated():
    """[2/6] 「播放速度」简写成「速」"""
    print("\n[2/6] 文案简写...")
    html = requests.get(BASE_URL + "/video", timeout=10).text
    assert "播放速度" not in html, "页面上仍出现「播放速度」四个字"
    assert re.search(r"<span>\s*速\s*</span>", html), "缺少「速」标签"
    print("   ✓ 文案已是「速」，页面已无「播放速度」字样")


# ---------------------------------------------------------------- 3/6
def test_css_horizontal():
    """[3/6] CSS 确为横向：speed-row / speed-options 一行，档位宽度自适应"""
    print("\n[3/6] CSS 横向布局...")
    for source, css in (("源码", open(STYLE_CSS, "r", encoding="utf-8").read()),
                        ("线上", requests.get(BASE_URL + "/video/style.css", timeout=10).text)):
        row = css_block(css, ".speed-row")
        assert "flex-direction: row" in row or "flex-direction:row" in row, \
            f"{source} .speed-row 不是横向: {row.strip()}"
        assert "column" not in row, f"{source} .speed-row 仍为纵向: {row.strip()}"
        opts = css_block(css, ".speed-options")
        assert "flex-direction: row" in opts or "flex-direction:row" in opts, \
            f"{source} .speed-options 不是横向: {opts.strip()}"
        btn = css_block(css, ".speed-btn")
        assert re.search(r"width:\s*auto", btn), f"{source} .speed-btn 未改为自适应宽度: {btn.strip()}"
        assert not re.search(r"width:\s*100%", btn), f"{source} .speed-btn 仍占满整行: {btn.strip()}"
    print("   ✓ 源码与线上：.speed-row / .speed-options 横向，.speed-btn 宽度自适应")


# ---------------------------------------------------------------- 4/6
def test_speed_behavior_unchanged():
    """[4/6] 档位与播放行为不变：1x/2x/7x、默认 1x、点击切换 playbackRate"""
    print("\n[4/6] 档位与行为回归...")
    html = requests.get(BASE_URL + "/video", timeout=10).text
    btns = re.findall(r'speed-btn[^>]*data-speed="([^"]+)"', html)
    assert btns == ["1", "2", "7"], f"档位应为 1/2/7，实际 {btns}"
    assert re.search(r'class="speed-btn active"[^>]*data-speed="1"', html), "默认档位未高亮 1x"
    assert len(re.findall(r'class="speed-btn[^"]*active', html)) == 1, "active 档位应唯一"
    print(f"   ✓ 档位 {btns}，active 唯一且落在 1x")

    js = requests.get(BASE_URL + "/video/app.js", timeout=10).text
    assert "getElementById('speedOptions')" in js, "app.js 未取 speedOptions"
    assert re.search(r"let playbackSpeed = 1\b", js), "playbackSpeed 默认值应为 1"
    assert re.search(r"playbackSpeed = ", js) and "playbackRate = playbackSpeed" in js, \
        "点击档位未作用到 playbackRate"
    print("   ✓ app.js 仍按 speedOptions 取档位，默认 1x，点击作用到 playbackRate")


# ---------------------------------------------------------------- 5/6
def test_source_consistent():
    """[5/6] 源码与线上下发一致"""
    print("\n[5/6] 源码一致性...")
    for path, url in ((INDEX_HTML, "/video"), (STYLE_CSS, "/video/style.css"), (APP_JS, "/video/app.js")):
        local = open(path, "r", encoding="utf-8").read()
        remote = requests.get(BASE_URL + url, timeout=10).text
        assert local.strip() == remote.strip(), f"{os.path.basename(path)} 与线上下发不一致"
    print("   ✓ index.html / style.css / app.js 与线上下发逐字节一致")


# ---------------------------------------------------------------- 6/6
def test_regression():
    """[6/6] 回归：上一任务的数量/随播/自播同行 + 标题跳转 + 删除按钮 + 自动切下一个 + 倒放 + 上传"""
    print("\n[6/6] 回归...")
    html = requests.get(BASE_URL + "/video", timeout=10).text

    # 上一任务：数量（只显示数字）/ 随播 / 自播 同一行
    row = html_block(html, '<div class="setting-row setting-row-inline">')
    for item in ('id="videoCount"', 'id="randomSwitch"', 'id="autoplaySwitch"'):
        assert item in row, f"回归失败：{item} 不在同一行"
    assert row.count("<div") == 1, "回归失败：数量行出现多余 div 嵌套"
    assert "随播" in row and "自播" in row, "回归失败：「随播」「自播」丢失"
    assert "视频数量" not in html and "随机播放" not in html and "自动播放" not in html, "回归失败：旧文案回来了"
    assert re.search(r'<b id="videoCount">\d+</b>', row), "回归失败：数量未显示为纯数字"
    print("   ✓ 数量（纯数字）/ 随播 / 自播 仍在同一行")

    home = requests.get(BASE_URL + "/", timeout=10).text
    assert re.search(r'<a[^>]+href="/video"[^>]*>', home), "回归失败：首页标题跳转 /video 丢失"
    assert 'id="uploadZone"' in home, "回归失败：首页拖拽上传区丢失"
    assert 'id="btnDeleteCurrent"' in html and 'id="fabUpload"' in html, "回归失败：删除/上传按钮丢失"
    print("   ✓ 标题跳转 /video、拖拽上传区、删除与上传按钮 均在")

    js = requests.get(BASE_URL + "/video/app.js", timeout=10).text
    assert re.search(r"video\.loop\s*=\s*false", js), "回归失败：video.loop 应为 false"
    assert re.search(r"video\.addEventListener\('ended'", js), "回归失败：ended 自动切下一个丢失"
    assert re.search(r"video\.playbackRate\s*=\s*-\s*REVERSE_RATE", js), "回归失败：-3x 倒放丢失"
    print("   ✓ 自动切下一个 / -3x 倒放 正常")

    assert requests.get(f"{BASE_URL}/health", timeout=10).json().get("status") == "ok", "回归失败：/health"
    name = "test_speed_row_reg.tmp"
    try:
        assert requests.put(f"{BASE_URL}/{name}", data=b"regression", timeout=10).status_code == 201, "PUT 直传异常"
        assert requests.get(f"{BASE_URL}/{name}", timeout=10).content == b"regression", "直传内容不一致"
    finally:
        requests.delete(f"{BASE_URL}/{name}", timeout=10)
    print("   ✓ /health、PUT 直传 正常")


@timeout(60)
def main():
    print("=" * 60)
    print("任务测试：「速」+ 1x/2x/7x 同一行 + 文案简写")
    print("=" * 60)
    try:
        test_label_and_options_same_row()
        test_label_abbreviated()
        test_css_horizontal()
        test_speed_behavior_unchanged()
        test_source_consistent()
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
