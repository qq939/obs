#!/usr/bin/env python3
"""
TDD 测试脚本 - 本次任务：
  在 OBS 首页新增「粘贴上传」——除拖拽与选择文件外，用户在页面右键粘贴
  （或 Ctrl/Cmd+V）时，若剪贴板里有文件，弹出对话框让用户确认是否上传。

要求：
  1) 监听 document 的 paste 事件；
  2) 剪贴板无文件（纯文本）时必须放行，不能干扰公告板输入框等默认粘贴；
  3) 有文件时弹出自定义确认对话框，展示文件名与大小，含「确认上传 / 取消」；
  4) 确认后复用既有 uploadFiles（小文件直传 / 大文件分片）链路；
  5) 剪贴板图片常无文件名，需按 MIME 兜底生成名字。

超时机制: 每个用例 60 秒超时
"""
import json
import os
import re
import signal
import subprocess
import tempfile

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(ROOT, "src", "obs", "server.py")
BASE_URL = "http://127.0.0.1:80"


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


def homepage() -> str:
    return requests.get(f"{BASE_URL}/", timeout=30).text


# ---------------------------------------------------------------- JS 抽取
def brace_slice(src: str, i: int) -> str:
    """从 src[i] == '{' 开始，按大括号配对抠出完整块"""
    assert src[i] == "{", f"起点不是 '{{': {src[i-20:i+20]!r}"
    depth = 0
    j = i
    in_s = None
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


JS_HARNESS = r"""
const R = [];
function assert(c, m) { R.push((c ? 'PASS' : 'FAIL') + ' :: ' + m); if (!c) process.exitCode = 1; }

__FUNCS__

// --- 造假的 clipboardData ---
function fakeDataTransfer(files, items) {
    return { files: files || [], items: items || [] };
}
function fakeItem(kind, file) {
    return { kind: kind, getAsFile: () => file };
}

// 1) 有 files：原样返回
{
    const f1 = { name: 'a.txt', size: 10, type: 'text/plain' };
    const got = collectPasteFiles(fakeDataTransfer([f1], []));
    assert(got.length === 1 && got[0].name === 'a.txt', '有 files 时原样返回');
}
// 2) files 为空、items 里 kind=file：走 getAsFile
{
    const f1 = { name: 'img.png', size: 20, type: 'image/png' };
    const got = collectPasteFiles(fakeDataTransfer([], [fakeItem('file', f1), fakeItem('string', null)]));
    assert(got.length === 1 && got[0].name === 'img.png', 'files 空时回退 items.getAsFile()');
}
// 3) 纯文本粘贴：返回空数组（必须放行默认行为）
{
    const got = collectPasteFiles(fakeDataTransfer([], [fakeItem('string', null)]));
    assert(got.length === 0, '纯文本粘贴返回空数组（不拦截）');
}
// 4) 空/异常输入不报错
{
    assert(collectPasteFiles(null).length === 0, 'null 剪贴板返回空数组');
    assert(collectPasteFiles({}).length === 0, '无 files/items 返回空数组');
    assert(collectPasteFiles(fakeDataTransfer([], [{ kind: 'file' }])).length === 0,
           'kind=file 但 getAsFile 缺失时不报错');
}
// 5) files 里多于一个：全部返回（多文件粘贴）
{
    const fs = [{ name: '1.png', size: 1, type: 'image/png' },
                { name: '2.png', size: 2, type: 'image/png' }];
    assert(collectPasteFiles(fakeDataTransfer(fs, [])).length === 2, '多文件粘贴全部返回');
}
// 6) 图片无文件名 → 按 MIME 兜底生成名字
{
    const n1 = guessPasteName('image/png', 0);
    assert(/\.png$/.test(n1) && /粘贴文件/.test(n1), 'image/png → .png 且带中文前缀: ' + n1);
    assert(/\.jpg$/.test(guessPasteName('image/jpeg', 0)), 'image/jpeg → .jpg');
    assert(/\.mp4$/.test(guessPasteName('video/mp4', 0)), 'video/mp4 → .mp4');
    assert(/\.pdf$/.test(guessPasteName('application/pdf', 0)), 'application/pdf → .pdf');
    // 未知 MIME 用 / 后半段；完全未知回退 bin
    assert(/\.xyz$/.test(guessPasteName('application/xyz', 0)), '未知 MIME 用 / 后半段');
    assert(/\.bin$/.test(guessPasteName('', 0)), '空 MIME 回退 .bin');
    // 多文件时序号不同，避免重名覆盖
    assert(guessPasteName('image/png', 0) !== guessPasteName('image/png', 1), '同批多文件名字不重复');
}
// 7) 大小格式化
{
    assert(fmtSize(0) === '0B', 'fmtSize(0) → 0B，实际 ' + fmtSize(0));
    assert(fmtSize(512) === '512B', 'fmtSize(512) → 512B，实际 ' + fmtSize(512));
    assert(fmtSize(1024) === '1.0KB', 'fmtSize(1024) → 1.0KB，实际 ' + fmtSize(1024));
    assert(fmtSize(1536) === '1.5KB', 'fmtSize(1536) → 1.5KB，实际 ' + fmtSize(1536));
    assert(fmtSize(5 * 1024 * 1024) === '5.0MB', 'fmtSize(5MiB) → 5.0MB，实际 ' + fmtSize(5 * 1024 * 1024));
}

console.log(JSON.stringify(R));
"""


def run_harness(src: str):
    funcs = "\n\n".join(extract_func(src, n)
                        for n in ("collectPasteFiles", "guessPasteName", "fmtSize"))
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


# ---------------------------------------------------------------- 1/4
@timeout(60)
def test_source_paste_listener_and_modal():
    """[1/4] 源码：paste 监听 + 文件提取 + 确认弹窗 + 文本放行 + 上传区提示"""
    print("\n[1/4] 源码：粘贴监听与确认对话框...")
    src = read()

    # paste 监听
    assert re.search(r"document\.addEventListener\('paste'", src), "缺少 document 的 paste 监听"
    print("   ✓ 监听 document 的 paste 事件（右键粘贴 / Ctrl+V 都会触发）")

    # 提取函数 + 无文件放行
    assert re.search(r"function\s+collectPasteFiles\s*\(", src), "缺少 collectPasteFiles"
    assert re.search(r"function\s+guessPasteName\s*\(", src), "缺少 guessPasteName（无文件名兜底）"
    assert re.search(r"function\s+fmtSize\s*\(", src), "缺少 fmtSize（展示文件大小）"
    listener = re.search(r"document\.addEventListener\('paste'[\s\S]*?\n\s*\}\);", src)
    assert listener, "抠不出 paste 监听回调"
    body = listener.group(0)
    assert "collectPasteFiles" in body, "paste 回调未调用 collectPasteFiles"
    assert re.search(r"length\s*===\s*0\s*\)\s*return", body), \
        "无文件时应直接 return（放行纯文本粘贴，不干扰公告板）"
    assert "preventDefault()" in body, "有文件时应 preventDefault 阻止浏览器默认行为"
    print("   ✓ collectPasteFiles 提取文件；无文件 return 放行、有文件 preventDefault")

    # 确认对话框
    assert 'id="pasteConfirmModal"' in src, "缺少确认对话框容器 pasteConfirmModal"
    assert 'id="pasteFileList"' in src, "缺少待上传文件列表 pasteFileList"
    assert re.search(r"function\s+showPasteConfirm\s*\(", src), "缺少 showPasteConfirm"
    assert re.search(r"function\s+cancelPasteUpload\s*\(", src), "缺少 cancelPasteUpload"
    assert re.search(r"function\s+confirmPasteUpload\s*\(", src), "缺少 confirmPasteUpload"
    assert "确认上传" in src, "对话框缺少「确认上传」按钮文案"
    assert "取消" in src, "对话框缺少「取消」按钮文案"
    print("   ✓ 确认对话框：文件列表 + 「确认上传」/「取消」按钮")

    # 确认后复用既有上传链路
    confirm = re.search(r"function\s+confirmPasteUpload\s*\(\)[\s\S]*?\n\s*\}", src).group(0)
    assert "uploadFiles(" in confirm, "confirmPasteUpload 未复用 uploadFiles（拖拽/选择同一链路）"
    assert "cancelPasteUpload(" in confirm or "display" in confirm, "确认后未关闭对话框"
    cancel = re.search(r"function\s+cancelPasteUpload\s*\(\)[\s\S]*?\n\s*\}", src).group(0)
    assert re.search(r"display\s*=\s*'none'", cancel), "cancelPasteUpload 未隐藏对话框"
    print("   ✓ 确认→uploadFiles；取消→隐藏对话框")

    # 上传区文案提示粘贴
    zone = re.search(r'<div id="uploadZone"[\s\S]*?</div>', src).group(0)
    assert "粘贴" in zone, "上传区未提示可粘贴上传"
    print("   ✓ 上传区文案已提示「粘贴上传」")


# ---------------------------------------------------------------- 2/4
@timeout(60)
def test_node_behavior():
    """[2/4] node 跑真实 JS：剪贴板提取 / 纯文本放行 / 文件名兜底 / 大小格式化"""
    print("\n[2/4] node 行为验证（真实 JS 函数）...")
    results = run_harness(read())
    for x in results:
        print("   " + ("✓ " if x.startswith("PASS") else "✗ ") + x.split(" :: ", 1)[1])
    fails = [x for x in results if x.startswith("FAIL")]
    assert not fails, f"以下断言失败: {fails}"
    print(f"   ✓ {len(results)} 条行为断言全部通过")


# ---------------------------------------------------------------- 3/4
@timeout(60)
def test_page_serves_modal():
    """[3/4] HTTP：页面真实下发对话框 DOM，且不影响既有上传入口"""
    print("\n[3/4] 页面下发与既有上传入口...")
    html = homepage()
    assert 'id="pasteConfirmModal"' in html, "页面未下发 pasteConfirmModal"
    assert 'id="pasteFileList"' in html, "页面未下发 pasteFileList"
    assert 'id="pasteConfirmCount"' in html, "页面未下发数量占位 pasteConfirmCount"
    assert "showPasteConfirm" in html and "confirmPasteUpload" in html and "cancelPasteUpload" in html, \
        "页面 JS 未下发粘贴相关函数"
    assert "addEventListener('paste'" in html, "页面 JS 未下发 paste 监听"
    print("   ✓ 页面下发对话框 DOM 与粘贴 JS")

    # 既有上传入口不能因本次改动消失
    for needle, who in [("function uploadFiles(", "拖拽/选择文件统一入口"),
                        ("handleDragUpload", "拖拽处理"),
                        ('id="formFile"', "选择文件 input"),
                        ("uploadOneFileDirect", "小文件直传"),
                        ("uploadOneFileResumable", "大文件分片")]:
        assert needle in html, f"既有上传入口丢失：{who}"
    print("   ✓ 拖拽 / 选择文件 / 小文件直传 / 大文件分片 入口均保留")


# ---------------------------------------------------------------- 4/4
@timeout(60)
def test_regression():
    """[4/4] 回归：首页 200、公告板/排序/播放页/日志/接口都正常"""
    print("\n[4/4] 回归...")
    r = requests.get(f"{BASE_URL}/", timeout=30)
    assert r.status_code == 200, f"首页返回 {r.status_code}"
    html = r.text
    for needle, who in [('id="notice-content"', "公告板"), ("sort=time", "排序"),
                        ('id="ws-status-indicator"', "WebSocket 状态灯")]:
        assert needle in html, f"首页缺少{who}"
    assert requests.get(f"{BASE_URL}/health", timeout=20).status_code == 200
    assert requests.get(f"{BASE_URL}/video", timeout=30).status_code == 200
    assert requests.get(f"{BASE_URL}/videos", timeout=60).status_code == 200
    print("   ✓ 首页/公告板/排序/health/video/videos 均正常")

    # 上传落盘 + logs 挂载仍正常（用 PUT 直传最小文件）
    import uuid
    name = f"test_paste_reg_{uuid.uuid4().hex[:8]}.txt"
    up = requests.put(f"{BASE_URL}/{name}", data=b"paste-reg", timeout=30)
    assert up.status_code in (200, 201), f"PUT 上传失败: {up.status_code}"
    assert requests.get(f"{BASE_URL}/{name}", timeout=30).content == b"paste-reg"
    requests.delete(f"{BASE_URL}/{name}", timeout=30)
    print("   ✓ PUT 上传 / 下载 / 删除 正常（logs 挂载沿用）")


if __name__ == "__main__":
    tests = [
        test_source_paste_listener_and_modal,
        test_node_behavior,
        test_page_serves_modal,
        test_regression,
    ]
    print("=" * 60)
    print("任务测试：首页粘贴上传（右键/Ctrl+V 粘贴文件 → 弹窗确认）")
    print("=" * 60)
    ok = 0
    for t in tests:
        t()
        ok += 1
    print("\n" + "=" * 60)
    print("所有测试通过!")
    print("=" * 60)
