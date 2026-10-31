#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「上传文件夹」的压缩模式从全局变量改成显式参数，避免取消选择后污染下一次上传。"""
import ast
import io
import sys

PATH = "src/obs/server.py"

REPL = [
    # A) 去掉全局变量
    ("""            let folderZipOnce = false;

            function chooseFolderUploadMode() {""",
     """            function chooseFolderUploadMode() {"""),

    # B) 模式随选择器传下去
    ("""            function pickFolderMode(mode) {
                closeFolderMode();
                folderZipOnce = (mode === 'zip');
                // 复用原有选择器（原生目录选择 / webkitdirectory 回退）
                openFolderPicker(false);
            }""",
     """            function pickFolderMode(mode) {
                closeFolderMode();
                // 上传方式跟着选择器一起传下去（不用全局变量，避免用户取消后污染下一次上传）
                openFolderPicker(false, mode === 'zip' ? 'zip' : 'files');
            }"""),

    # C) showFolderConfirm 接收 zipMode
    ("""            async function showFolderConfirm(groups, looseFiles, structureLost, append) {
                // 用户在「上传文件夹」里选了压缩上传：不再逐文件确认，直接打包成一个 tar.gz
                if (folderZipOnce) {
                    folderZipOnce = false;
                    try { return await zipAndUpload(groups, looseFiles); }""",
     """            async function showFolderConfirm(groups, looseFiles, structureLost, append, zipMode) {
                // 用户在「上传文件夹」里选了压缩上传：不再逐文件确认，直接打包成一个 tar.gz
                if (zipMode) {
                    try { return await zipAndUpload(groups, looseFiles); }"""),

    # D) openFolderPicker 带上 uploadMode，webkitdirectory 回退时把方式写进 input.dataset
    ("""            async function openFolderPicker(addMore) {
                if (!addMore) { folderPending = null; folderPickAppend = false; }
                // 非安全上下文或浏览器不支持该 API 时，直接同步触发 input，避免丢失用户手势
                if (!window.isSecureContext || typeof window.showDirectoryPicker !== 'function') {
                    folderPickAppend = !!addMore;
                    const inp = document.getElementById('folderInput');
                    if (inp) { try { inp.value = ''; } catch (e) {} inp.click(); }
                    return;
                }
                const nativeHandles = await pickDirectoriesNative();
                if (nativeHandles === null) {
                    folderPickAppend = !!addMore;
                    const inp = document.getElementById('folderInput');
                    if (inp) { try { inp.value = ''; } catch (e) {} inp.click(); }
                    return;
                }
                if (!nativeHandles.length) return;  // 用户取消
                let groups = [];
                try { groups = await collectGroupsFromDirectoryHandles(nativeHandles); } catch (e) { groups = []; }
                if (groups.length) showFolderConfirm(groups, [], false, !!addMore);
            }""",
     """            // 触发 webkitdirectory 原生选择器；上传方式挂在 input 的 dataset 上，由 onchange 带回
            function pickFolderViaInput(addMore, uploadMode) {
                folderPickAppend = !!addMore;
                const inp = document.getElementById('folderInput');
                if (!inp) return;
                try { inp.value = ''; } catch (e) {}
                try { inp.dataset.uploadMode = (uploadMode === 'zip') ? 'zip' : 'files'; } catch (e) {}
                inp.click();
            }
            async function openFolderPicker(addMore, uploadMode) {
                if (!addMore) { folderPending = null; folderPickAppend = false; }
                const zipMode = (uploadMode === 'zip');
                // 非安全上下文或浏览器不支持该 API 时，直接同步触发 input，避免丢失用户手势
                if (!window.isSecureContext || typeof window.showDirectoryPicker !== 'function') {
                    pickFolderViaInput(addMore, uploadMode);
                    return;
                }
                const nativeHandles = await pickDirectoriesNative();
                if (nativeHandles === null) {
                    pickFolderViaInput(addMore, uploadMode);
                    return;
                }
                if (!nativeHandles.length) return;  // 用户取消
                let groups = [];
                try { groups = await collectGroupsFromDirectoryHandles(nativeHandles); } catch (e) { groups = []; }
                if (groups.length) showFolderConfirm(groups, [], false, !!addMore, zipMode);
            }"""),

    # E) input 回退路径读回上传方式
    ("""            function uploadFolderFromInput(fileList) {
                if (!fileList || !fileList.length) return;
                const groups = groupsFromFileList(fileList);
                showFolderConfirm(groups, [], false, folderPickAppend);
                folderPickAppend = false;
                const inp = document.getElementById('folderInput');
                if (inp) { try { inp.value = ''; } catch (e) {} }
            }""",
     """            function uploadFolderFromInput(fileList, uploadMode) {
                if (!fileList || !fileList.length) return;
                const groups = groupsFromFileList(fileList);
                showFolderConfirm(groups, [], false, folderPickAppend, uploadMode === 'zip');
                folderPickAppend = false;
                const inp = document.getElementById('folderInput');
                if (inp) { try { inp.value = ''; delete inp.dataset.uploadMode; } catch (e) {} }
            }"""),

    # F) 内联 onchange 把上传方式带进来
    ("""onchange="uploadFolderFromInput(this.files)\"""",
     """onchange="uploadFolderFromInput(this.files, this.dataset.uploadMode)\""""),
]


def main():
    src = io.open(PATH, encoding="utf-8").read()
    for old, new in REPL:
        if src.count(old) != 1:
            print("[FAIL] 命中 %d 次：%r" % (src.count(old), old[:70]))
            return 1
        src = src.replace(old, new)
        print("[OK] %r" % old.strip().splitlines()[0][:60])
    try:
        ast.parse(src)
        print("AST OK")
    except SyntaxError as e:
        print("[FAIL] python 语法错误:", e)
        return 1
    if "folderZipOnce" in src:
        print("[FAIL] 仍有 folderZipOnce 残留")
        return 1
    io.open(PATH, "w", encoding="utf-8", newline="\n").write(src)
    print("written")
    return 0


sys.exit(main())
