#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打散上传改成真正的「逐个文件进文件列表」（不再服务端打包 zip）；
   上传方式用字符串 'flat' / 'zip' 贯穿，不再用布尔 zipMode。"""
import ast
import io
import sys

PATH = "src/obs/server.py"

REPL = [
    # 1) 顶部说明
    ("""            // ------------------------------------------------------------------
            // 上传文件夹的两种方式（使用位置：chooseFolderUploadMode / showFolderConfirm）
            //   files：打散上传（原来的逐文件逻辑）
            //   zip  ：整个文件夹打包成 .tar.gz 后当一个文件上传
            // ------------------------------------------------------------------""",
     """            // ------------------------------------------------------------------
            // 上传文件夹的两种方式（使用位置：chooseFolderUploadMode / showFolderConfirm）
            //   flat：打散上传——遍历出每个文件，逐个直接进文件列表（不打包）
            //   zip ：压缩上传——整个文件夹打包成 .tar.gz，作为一个文件上传（保留目录结构）
            // 注意：拖拽/粘贴文件夹仍走原来的「暂存 → 服务端打包 zip」流程，不受这里影响。
            // ------------------------------------------------------------------"""),

    # 2) 模式名统一
    ("""                openFolderPicker(false, mode === 'zip' ? 'zip' : 'files');""",
     """                openFolderPicker(false, mode === 'zip' ? 'zip' : 'flat');"""),

    # 3) 打散上传的实现 + showFolderConfirm 按方式分发
    ("""            async function showFolderConfirm(groups, looseFiles, structureLost, append, zipMode) {
                // 用户在「上传文件夹」里选了压缩上传：不再逐文件确认，直接打包成一个 tar.gz
                if (zipMode) {
                    try { return await zipAndUpload(groups, looseFiles); }
                    catch (e) { alert('压缩上传失败：' + e.message); return; }
                }""",
     """            // 打散上传：把选中的文件逐个直接放进文件列表（不打包、不压缩）
            // 同名文件自动用「目录__文件名」区分，避免互相覆盖；File 不能改名，所以包一层新 File
            async function flatUpload(groups, looseFiles) {
                const statusEl = document.getElementById('formUploadStatus');
                const originals = [];
                for (const g of (groups || [])) {
                    for (const f of (g.files || [])) originals.push(f);
                }
                for (const f of (looseFiles || [])) originals.push(f);
                if (!originals.length) return;
                const used = new Set();
                const files = [];
                for (const f of originals) {
                    const rel = (fileRelPath(f) || f.name || '').replace(/^\\/+/, '');
                    const base = rel.split('/').pop() || f.name;
                    let name = base;
                    if (used.has(name)) name = rel.replace(/\\//g, '__');
                    let n = 2;
                    while (used.has(name)) name = rel.replace(/\\//g, '__') + '(' + (n++) + ')';
                    used.add(name);
                    let nf = f;
                    if (name !== f.name) {
                        try { nf = new File([f], name, { type: f.type, lastModified: f.lastModified || Date.now() }); }
                        catch (e) { nf = f; }
                    }
                    try { nf._relPath = name; } catch (e) {}
                    files.push(nf);
                }
                if (statusEl) statusEl.textContent = '逐个上传 ' + files.length + ' 个文件…';
                return await uploadFiles(files, {});
            }

            async function showFolderConfirm(groups, looseFiles, structureLost, append, uploadMode) {
                // 「上传文件夹」选定方式后不再逐文件确认：压缩 → 打成一个 tar.gz；打散 → 逐个直接上传
                if (uploadMode === 'zip') {
                    try { return await zipAndUpload(groups, looseFiles); }
                    catch (e) { alert('压缩上传失败：' + e.message); return; }
                }
                if (uploadMode === 'flat') {
                    try { return await flatUpload(groups, looseFiles); }
                    catch (e) { alert('打散上传失败：' + e.message); return; }
                }"""),

    # 4) webkitdirectory 回退把方式名改成 flat
    ("""                try { inp.dataset.uploadMode = (uploadMode === 'zip') ? 'zip' : 'files'; } catch (e) {}""",
     """                try { inp.dataset.uploadMode = (uploadMode === 'zip') ? 'zip' : 'flat'; } catch (e) {}"""),

    # 5) openFolderPicker 透传方式字符串
    ("""            async function openFolderPicker(addMore, uploadMode) {
                if (!addMore) { folderPending = null; folderPickAppend = false; }
                const zipMode = (uploadMode === 'zip');""",
     """            async function openFolderPicker(addMore, uploadMode) {
                if (!addMore) { folderPending = null; folderPickAppend = false; }"""),
    ("""                if (groups.length) showFolderConfirm(groups, [], false, !!addMore, zipMode);""",
     """                if (groups.length) showFolderConfirm(groups, [], false, !!addMore, uploadMode);"""),

    # 6) input 回退路径透传方式字符串
    ("""                showFolderConfirm(groups, [], false, folderPickAppend, uploadMode === 'zip');""",
     """                showFolderConfirm(groups, [], false, folderPickAppend, uploadMode);"""),

    # 7) 弹窗文案
    ("""                    请选择处理方式。压缩上传会在浏览器里打包成 <code>.tar.gz</code>，只产生一个文件。""",
     """                    请选择处理方式：打散上传把每个文件单独放进文件列表；压缩上传把整个文件夹打成一个包。"""),
    ("""                    <b>打散上传</b><br><span style="font-size:12px; color:#888;">遍历出每一个文件，逐个上传（保留目录结构）</span>""",
     """                    <b>打散上传</b><br><span style="font-size:12px; color:#888;">遍历出每个文件逐个上传，文件列表里看到的是一个个文件（不打包；同名自动加目录前缀）</span>"""),
    ("""                    <b>压缩上传</b><br><span style="font-size:12px; color:#888;">整个文件夹打成 <code>.tar.gz</code>，作为一个文件上传</span>""",
     """                    <b>压缩上传</b><br><span style="font-size:12px; color:#888;">整个文件夹打成 <code>.tar.gz</code>，作为一个文件上传（保留目录结构）</span>"""),
]


def main():
    src = io.open(PATH, encoding="utf-8").read()
    for old, new in REPL:
        n = src.count(old)
        if n != 1:
            print("[FAIL] 命中 %d 次：%r" % (n, old.strip().splitlines()[0][:70]))
            return 1
        src = src.replace(old, new)
        print("[OK]", old.strip().splitlines()[0][:70])
    try:
        ast.parse(src)
        print("AST OK")
    except SyntaxError as e:
        print("[FAIL] 语法:", e)
        return 1
    for bad in ("folderZipOnce", "'files')", "zipMode"):
        if bad in src:
            print("[WARN] 残留:", bad)
    io.open(PATH, "w", encoding="utf-8", newline="\n").write(src)
    print("written")
    return 0


sys.exit(main())
