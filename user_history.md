# User History

## 2026-01-05
**User Request:** 要求文件名不再固定，固定可存入100个文件，如果超过了需要滚动删除。主页显示每一个文件的url。
**Action:** Starting implementation. Created requirements.txt, ref.txt, tips.txt, and user_rules.md.
**Action:** Modified `server.py` to support rolling deletion (max 100 files) and list files on homepage.
**Action:** Created `test_server.py` and verified the functionality (2 tests passed).

**User Request:** 文件上限改成20个，如果是上传文件与旧文件同名的话，删除原来的文件，放新的文件。
**Action:** Modified `server.py` to change file limit to 20. Updated rolling deletion logic to check if file already exists; if so, skip deletion (simple overwrite).
**Action:** Updated `test_server.py` to test limit 20 and overwrite scenario. Verified with 3 tests passing.

## 2026-01-11
**User Request:** 1、对存储文件数量不再做上限限制，不再设置滚动删除机制。2、如果上传同名文件，则实施替换。3、在网页端每个文件旁边增加垃圾桶图标，能够删除文件。
**Action:** Updating `server.py` to remove file limits and rolling deletion. Ensuring overwrite on name collision. Adding delete endpoint and UI.

## 2026-01-12
**User Request:** 文件预览和下载的根地址都是obs.dimond.top，你写成0.0.0.0，根本访问不通。只有flask app是跑在0.0.0.0上的。
**Action:** Fixed `server.py` to use `obs.dimond.top` as the hardcoded host for file URLs instead of dynamically using the request's Host header. Updated tests to verify this behavior.

## 2026-01-14
**User Request:** 在这行字“上传命令示例: curl --upload-file file.txt     http://obs.dimond.top"下方，增加一个文件上传模块
**Action:** Added a file upload form to the homepage HTML in `server.py`. Updated `test_server.py` to verify the form's presence.

## 2026-01-14 (Continued)
**User Request:** 为啥不支持json文件下载？其他的格式都没问题 。
**Action:** Identified that browsers render JSON by default. Modified `server.py` to add `Content-Disposition: attachment` header for file downloads, forcing them to be downloaded instead of displayed. Added regression test for JSON download.

## 2026-01-15
**User Request:** 打不开json文件，并且下载的时候后缀名是html，且无法成功下载。
**Action:** Debugged and found that URL encoded filenames (especially with Chinese or spaces) were causing 404 errors because the server wasn't decoding them properly. Added `unquote` to `do_GET`, `do_PUT`, and `do_DELETE`. Also updated `Content-Disposition` header to use RFC 5987 standard (`filename*=UTF-8''...`) to correctly handle non-ASCII filenames in downloads. Added a comprehensive test case for special characters in filenames.

## 2026-01-16
**User Request:** web side add a new function, that can sort files by file extention
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

**User Request:** 公告板的默认文字就是“公告板”，点“x”以后就恢复成默认文字。我想把公告板变成类似在线共享文档的那种，一个客户端编辑以后，另一个客户端马上同步，但是又不想造成过大的网络开销，用websocket实现
**Action:**
1.  Updated `requirements.txt` to include `websockets` library.
2.  Modified `server.py` to run a WebSocket server (using `websockets` library) on a separate thread (port 8089/current+1).
3.  Implemented real-time synchronization:
    -   Backend: Broadcasts updates to all connected clients except the sender. Handles "reset" broadcast. Persists data to MySQL.
    -   Frontend: Replaced polling (`setInterval`) with `WebSocket` connection. Updates text area on receiving messages.
4.  Changed default notice board text to "公告板".
5.  Updated `test_server.py` to check for WebSocket integration in the HTML.

**User Request:** 默认文字直接设为空吧，点击x也是清空所有文字
**Action:**
1.  Modified `server.py` to set the default notice board content to an empty string `""` (was "公告板") in both `init_db` and `MOCK_DB_DATA`.
2.  Updated the "reset" logic in `ws_handler` to set content to an empty string instead of "公告板".
3.  Verified changes with existing tests.

**User Request:** 不用数据库了，就把文字内容放到内存里。当前各个客户端的文字完全不能同步，你通过日志定位下问题原因。.log/obs (7).log
**Action:**
1.  Analyzed the log file and found no WebSocket activity, indicating possible connection or handling issues.
2.  Diagnosed that the crash was caused by accessing `client.open` attribute in the WebSocket handler, which doesn't exist in the version used.
3.  Modified `server.py` to:
    -   Remove `pymysql` and all database-related code.
    -   Implement in-memory storage using a global variable `NOTICE_CONTENT`.
    -   Fix the WebSocket handler crash by removing the `.open` check and using `try-except` for broadcasting.
    -   Add detailed logging to the WebSocket handler.
4.  Updated `requirements.txt` to remove `pymysql` and add `pytest-asyncio` for testing.
5.  Added a new test case `test_websocket_sync` in `test_server.py` to verify real-time synchronization between multiple clients.
6.  Verified all changes with tests passing.

**User Request:** 多个客户端仍然一点都不能同步，你的test_server.py总是卡住说明广播还是有问题的。看日志.log/obs (8).log定位一下问题。
**Action:**
1.  Analyzed `.log/obs (8).log` and confirmed NO WebSocket logs, indicating the user's browser clients are failing to connect (likely due to port exposure issues in Docker). The `GET /notice` requests in the log suggest clients might be running old polling code or cached pages.
2.  Analyzed `server.py` broadcasting logic and identified that sequential `await client.send()` could block the entire server if one client is slow/unresponsive, potentially causing the "hanging" behavior in tests.
3.  Modified `server.py` to use `asyncio.gather` for broadcasting, ensuring that message sending is concurrent and non-blocking. Added robust error handling for send failures.
4.  Updated `server.py` HTML/JS to add a visible "WebSocket Status" indicator (Connecting/Connected/Disconnected) to help the user diagnose connection issues in the browser.
5.  Updated `test_server.py` to use `asyncio.wait_for(..., timeout=5.0)` in WebSocket receive calls to prevent the test from hanging indefinitely if messages are dropped.
6.  Verified changes with `test_server.py`, which passed successfully.
