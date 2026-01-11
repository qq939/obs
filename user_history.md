1→# User History
2→
3→## 2026-01-05
4→**User Request:** 要求文件名不再固定，固定可存入100个文件，如果超过了需要滚动删除。主页显示每一个文件的url。
5→**Action:** Starting implementation. Created requirements.txt, ref.txt, tips.txt, and user_rules.md.
6→**Action:** Modified `server.py` to support rolling deletion (max 100 files) and list files on homepage.
7→**Action:** Created `test_server.py` and verified the functionality (2 tests passed).
8→
9→**User Request:** 文件上限改成20个，如果是上传文件与旧文件同名的话，删除原来的文件，放新的文件。
10→**Action:** Modified `server.py` to change file limit to 20. Updated rolling deletion logic to check if file already exists; if so, skip deletion (simple overwrite).
11→**Action:** Updated `test_server.py` to test limit 20 and overwrite scenario. Verified with 3 tests passing.
12→
13→## 2026-01-11
14→**User Request:** 1、对存储文件数量不再做上限限制，不再设置滚动删除机制。2、如果上传同名文件，则实施替换。3、在网页端每个文件旁边增加垃圾桶图标，能够删除文件。
15→**Action:** Updating `server.py` to remove file limits and rolling deletion. Ensuring overwrite on name collision. Adding delete endpoint and UI.
