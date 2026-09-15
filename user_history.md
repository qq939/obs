# User History - OBS Project

## 2024-09-15

### 实现分片上传和分片哈希功能

**任务描述：**
实现分片上传、分片哈希。但是哈希不要阻塞上传。

**完成的工作：**

1. **分析现有代码结构**
   - 查看了 server.py 的分片上传实现
   - 理解现有的 /upload/init, /upload/chunk, /upload/complete 接口

2. **创建测试脚本**
   - test_chunked_hash.py: 测试分片上传基本功能
   - test_server_chunk_hash.py: 测试服务端分片哈希计算

3. **实现服务端分片哈希计算**
   - 添加了 ThreadPoolExecutor 用于后台哈希计算
   - 在 upload_init 时初始化上传会话
   - 在 upload_chunk 时后台计算分片哈希（不阻塞上传）
   - 添加 upload_status 接口查询分片哈希和进度
   - 在 upload_complete 时使用分片哈希快速验证

4. **创建 docker-compose.yml**
   - 配置 obs 服务（8088端口）
   - 配置 obs-video-app 服务（80端口）
   - 共享挂载同一个 obs 和 obs-shards 目录

5. **测试验证**
   - 所有测试通过（9个测试用例）
   - 分片哈希计算不阻塞上传
   - 使用分片哈希快速验证合并

**技术要点：**
- 使用 ThreadPoolExecutor.run_in_executor 实现异步哈希计算
- 上传会话管理（upload_sessions）存储分片哈希和总体哈希
- 合并时优先使用分片哈希快速验证，无需重新读取整个文件
- 并发上传时能正确计算所有分片的哈希

**下一步：**
- 将本项目部署到 obs-video-app
- 修改 obs-video-app 使用本项目的上传服务
