# User Rules

1. ANSWER IN CHINESE!
2. 完成所有任务清单，任务完成之前不要退出
3. 不要使用应该等模糊回答，所有结论必须有测试结果
4. ref.txt（如果没有请新建）里面是需要参考的github仓库地址，很重要一定要看完里面仓库的内容
5. 优先使用mcp工具来完成任务
6. 将user_rules.md文件中的所有规则都保存在：.trae/rules/user_rules.md中
7. 如果有git仓库，先暂存本地修改，然后git pull，然后再继续下面的步骤
8. Create python venv evironment(use command: uv), and install python packages in requirements.txt.
9. 所有对话存到项目文件夹下的user_history.md文件中。(save everything to user_history.md file, and mark the time of each question like a worknote)
10. 每次对话后都要git push(make sure success)，commit内容就是我说的那句话。user.email="939342547@qq.com", user.name="qq939", remote=https://github.com/qq939/{to_be_create_as_this_projectName}, branch=master, when push to this remote, maybe you need to create remote repository(use: gh repo create {to_be_create_as_this_projectName} --public).
11. git add .trae/rules/project_rules.md
12. git add .trae/rules/user_rules.md
13. 如果git推送到远端失败，rebase并且push --force-with-lease
14. 适当增加mac的vscode快捷键提示，和代码如何快速补全（比如输入几个字母按tab键之类），补充到tips.txt中
