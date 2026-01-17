
## 2026-01-17 (Continued)
**User Request:** 是8088端口，和http请求共用一个端口啊，都是FASTAPI服务，你不会做不到了吧？
**Action:**
1.  Acknowledged user's request to use port 8088 for both HTTP and WebSocket, and to use FastAPI.
2.  Updated `requirements.txt` to include `fastapi`, `uvicorn`, `python-multipart`, and `aiofiles`.
3.  Refactored `server.py` to use `FastAPI` instead of `http.server` and `websockets`.
    -   Combined HTTP and WebSocket services on the same port (8088).
    -   Implemented `lifespan` context manager to ensure `upload` directory exists.
    -   Used `ConnectionManager` for handling WebSocket connections and broadcasting.
    -   Updated frontend code to connect to `ws://${window.location.host}/ws`.
4.  Updated `test_server.py` to use `uvicorn` for testing the FastAPI server and adjusted tests to work with the new architecture.
5.  Verified all functionality (file upload, delete, list, sorting, notice board sync) with `pytest`, achieving 100% pass rate.
6.  Created `.trae/reference/ref.txt` and `tips.txt` as per workspace rules.
