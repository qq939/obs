#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修 tar 头终止符：\u0000 会被 Python 先解释成真正的 NUL 字节，HTML 解析器再换成 U+FFFD，
   导致 tar 头形如 '0000644\xef\xbf\xbd'，tar 判定「不是 tar 包」。改用 String.fromCharCode(0)。"""
import ast
import io
import sys

PATH = "src/obs/server.py"

REPL = [
    (r"""            // 纯 JS 生成 tar（ustar），再交给 CompressionStream('gzip') 压缩
            function tarOctal(value, len) {
                const s = value.toString(8);
                return s.padStart(len - 1, '0') + '\u0000';
            }""",
     r"""            // 纯 JS 生成 tar（ustar），再交给 CompressionStream('gzip') 压缩
            // 注意：不能用 '\u0000' 字面量——这段 JS 在 Python 字符串里，会被 Python 先解释成真正的
            // NUL 字节，HTML 解析器再把 NUL 换成 U+FFFD，tar 头就坏了（报「不是 tar 包」）。
            const TAR_NUL = String.fromCharCode(0);
            function tarOctal(value, len) {
                const s = value.toString(8);
                return s.padStart(len - 1, '0') + TAR_NUL;
            }"""),

    (r"""                put('ustar\u000000', 257, 8);""",
     r"""                put('ustar' + TAR_NUL + '00', 257, 8);"""),

    (r"""                put(sum.toString(8).padStart(6, '0') + '\u0000 ', 148, 8);""",
     r"""                put(sum.toString(8).padStart(6, '0') + TAR_NUL + ' ', 148, 8);"""),
]


def main():
    src = io.open(PATH, encoding="utf-8").read()
    for old, new in REPL:
        n = src.count(old)
        if n != 1:
            print("[FAIL] 命中 %d 次：%r" % (n, old.strip().splitlines()[0][:60]))
            return 1
        src = src.replace(old, new)
        print("[OK]", old.strip().splitlines()[0][:60])
    try:
        ast.parse(src)
        print("AST OK")
    except SyntaxError as e:
        print("[FAIL] 语法:", e)
        return 1
    io.open(PATH, "w", encoding="utf-8", newline="\n").write(src)
    print("written")
    return 0


sys.exit(main())
