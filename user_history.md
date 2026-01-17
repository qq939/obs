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
16→
17→## 2026-01-12
18→**User Request:** 文件预览和下载的根地址都是obs.dimond.top，你写成0.0.0.0，根本访问不通。只有flask app是跑在0.0.0.0上的。
19→**Action:** Fixed `server.py` to use `obs.dimond.top` as the hardcoded host for file URLs instead of dynamically using the request's Host header. Updated tests to verify this behavior.
20→
21→## 2026-01-14
22→**User Request:** 在这行字“上传命令示例: curl --upload-file file.txt     http://obs.dimond.top"下方，增加一个文件上传模块
23→**Action:** Added a file upload form to the homepage HTML in `server.py`. Updated `test_server.py` to verify the form's presence.
24→
25→## 2026-01-14 (Continued)
26→**User Request:** 为啥不支持json文件下载？其他的格式都没问题 。
27→**Action:** Identified that browsers render JSON by default. Modified `server.py` to add `Content-Disposition: attachment` header for file downloads, forcing them to be downloaded instead of displayed. Added regression test for JSON download.
28→
29→## 2026-01-15
30→**User Request:** 打不开json文件，并且下载的时候后缀名是html，且无法成功下载。
31→**Action:** Debugged and found that URL encoded filenames (especially with Chinese or spaces) were causing 404 errors because the server wasn't decoding them properly. Added `unquote` to `do_GET`, `do_PUT`, and `do_DELETE`. Also updated `Content-Disposition` header to use RFC 5987 standard (`filename*=UTF-8''...`) to correctly handle non-ASCII filenames in downloads. Added a comprehensive test case for special characters in filenames.
32→
33→## 2026-01-16
34→**User Request:** web side add a new function, that can sort files by file extention
**Action:** Added sorting functionality to the web interface. Implemented `?sort=ext` parameter in `server.py` to sort files by extension (alphabetical) and added UI controls to switch between "Sort by Time" (default) and "Sort by Extension". Added `test_sort_by_extension` to `test_server.py` to verify the sorting logic.

## 2026-01-17
**User Request:** 当前OBS访问速度很慢，你看一下是哪个组件的问题，给我提供一下精简建议。
**Action:** Identified that the server was using single-threaded `TCPServer`. Switched to `socketserver.ThreadingTCPServer` to handle requests concurrently, significantly improving performance for multiple users.

**User Request:** 在上传文件模块上方增加公告板模块，支持自动保存、多端同步、数据库存储（MySQL），以及重置功能。
**Action:**
1.  Updated `requirements.txt` with `python-dotenv` and `pymysql`.
2.  Modified `server.py` to connect to MySQL (using `.env` credentials). Implemented `init_db` to ensure `notice_board` table exists.
3.  Added `/notice` API endpoints (GET/POST) to handle notice content.
4.  Implemented the Notice Board UI in `server.py` with auto-save (every 1s), auto-sync (every 2s), and reset functionality.
5.  Updated `test_server.py` with `MOCK_DB` mode to verify the API and UI without a real database connection in the test environment.

**User Request:** .log/obs (3).log 显示Database Error，但是日志完全看不出来啊
**Action:**
1.  Investigated the database connection issue and found that `MYSQL_HOST` in `.env` was set to `0.0.0.0`, which is invalid for client connections (should be `127.0.0.1` or `localhost`).
2.  Updated `.env` to set `MYSQL_HOST=127.0.0.1`.
3.  Improved logging in `server.py` by integrating the `logging` module. Configured it to write to both console and `server.log` file, including timestamps and full exception tracebacks to make future debugging easier.

**User Request:** 不要用logging，直接print flush=True
**Action:** Reverted the `logging` module changes. Replaced `logging.error/info/warning` with `print(..., flush=True)` as requested by the user. Preserved the traceback printing for better debugging.
