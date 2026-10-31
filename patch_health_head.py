#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 /health 同时支持 HEAD：面板的卡片状态灯用 HEAD 探测，GET-only 会 405。"""
import io
import sys

PATH = "src/obs/server.py"
src = io.open(PATH, encoding="utf-8").read()
old = '@app.get("/health")\nasync def health():'
new = ('@app.api_route("/health", methods=["GET", "HEAD"])\nasync def health():\n'
       '    # HEAD 也要支持：面板/其它服务用 HEAD 探活，只注册 GET 会返回 405')
print("hit:", src.count(old))
if src.count(old) != 1:
    sys.exit(1)
src = src.replace(old, new)
io.open(PATH, "w", encoding="utf-8", newline="\n").write(src)
print("written")
