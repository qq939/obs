#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注释里别写反斜杠-u 转义文本：Python 会把它变成 NUL，浏览器再变 U+FFFD。"""
import io
import sys

PATH = "src/obs/server.py"
src = io.open(PATH, encoding="utf-8").read()
old = "// 注意：不能用 '\\u0000' 字面量——这段 JS 在 Python 字符串里，会被 Python 先解释成真正的"
new = "// 注意：不能写 NUL 的反斜杠转义字面量——这段 JS 在 Python 字符串里，会被 Python 先解释成真正的"
print("hit:", src.count(old))
if src.count(old) != 1:
    sys.exit(1)
src = src.replace(old, new)
print("remaining escape text:", src.count("\\u0000"))
io.open(PATH, "w", encoding="utf-8", newline="\n").write(src)
print("written")
