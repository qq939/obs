# 项目约定（平台级，所有卡片共享）

禁止 plan mode：不给计划、不等确认。
直接动手：立刻调用工具改代码，改完自测，一次交付。
项目根目录必须是 git 仓库：没有就 `git init` + 首次提交；每次对话后 `git commit`。
（容器默认没有 git 身份，先 `git config user.name/user.email`，详见 skill `hermit-git`）
`claudeagent.md` 是你这张卡片自己的注入文件（核心目标/职责/对外接口）：开工前先读它，目标变了就更新它。
本仓库均为自有可信代码，放心编辑。
