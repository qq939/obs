# 项目约定（平台级，所有卡片共享）

禁止 plan mode：不给计划、不等确认。
直接动手：立刻调用工具改代码，改完自测，一次交付。
项目根目录必须是 git 仓库：没有就 `git init` + 首次提交；每次对话后 `git commit`。
建卡/交付必须同时具备 **webapp（人用的页面）** 和 **api（程序调用的接口）**，缺一不算完成。
远程仓库名必须以 `harness` 开头（如 `harness-xxx`）；已有远程不合规先改名，再 `git remote set-url`。
（容器默认没有 git 身份，先 `git config user.name/user.email`，详见 skill `harness-git`）
`claudeagent.md`（同目录）是本卡片自己的注入文件（核心目标/职责/对外接口）：由用户在面板「任务」按钮维护，开工前读它了解自己的目标。
本仓库均为自有可信代码，放心编辑。
输出规范：给用户看任何信息（状态、诊断结果、结论）时，必须把实际执行的命令原样一起展示出来，方便用户学习。
改卡片必须留痕：每次对某张卡片的代码/配置动手后，把「目标 / 设计理由 / 改动文件 / 踩坑 / 验证方式 / 已知限制」追加到该卡片的 logs/CHANGES.md —— 宿主机路径 logs/<容器名>/CHANGES.md，容器内路径 project/logs/CHANGES.md（注意 project/logs 是独立挂载，写在 workspaces/<卡片>/logs/ 下容器里看不到），供下一个接手的 AI 直接读。
