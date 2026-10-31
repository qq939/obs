#!/usr/bin/env python3
import io
import re

html = io.open('/tmp/page2.html', encoding='utf-8', errors='replace').read()
idx = html.find('\x00')
print('NUL at', idx, 'of', len(html))
print('context:', repr(html[max(0, idx - 160):idx + 160]))
nl = html.count('\x00')
print('total NUL:', nl)
# 同时看看源码里还有哪些 \u / \x 转义可能被 Python 解释
src = io.open('src/obs/server.py', encoding='utf-8').read()
for m in re.finditer(r'\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\0', src):
    line = src[:m.start()].count('\n') + 1
    print('escape at line', line, repr(m.group(0)), '|', src[max(0, m.start() - 60):m.end() + 20].replace('\n', ' ')[-90:])
