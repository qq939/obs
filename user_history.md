# User History

## 2026-01-05
**User Request:** 要求文件名不再固定，固定可存入100个文件，如果超过了需要滚动删除。主页显示每一个文件的url。
**Action:** Starting implementation. Created requirements.txt, ref.txt, tips.txt, and user_rules.md.
**Action:** Modified `server.py` to support rolling deletion (max 100 files) and list files on homepage.
**Action:** Created `test_server.py` and verified the functionality (2 tests passed).
