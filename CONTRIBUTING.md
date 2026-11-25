# 参与 Vera 开发

报告问题时，请提供触发步骤、实际结果和期望结果。公开 issue 和 PR 使用合成示例，避免贴出会议原文、密钥或本机资料路径。

调查可以共用源码。开始修改时，从最新的 `origin/main` 为每项独立任务建立一个分支和一个 Git worktree。继续同一任务时复用它的分支和工作目录，避免两个会话同时改同一份文件。

修复完成后说明如何复现、如何验证以及尚未覆盖的情况。需要提交评审时，将这些信息写进 PR。其他会话可用 PR 编号接手。多个已验证的修复可以先后合入 `main`，由维护者另行决定何时发布。

每个会话提交代码前都要使用仓库的公开提交身份。先在当前 worktree 执行 `git config user.name "Vera Contributors"` 和 `git config user.email "dev@vera.invalid"`，再检查 `git log -1 --format='%an <%ae> | %cn <%ce>'`。GitHub 的 merge commit、rebase merge 和 squash merge 都可能使用维护者账号写入个人邮箱，因此合入 `main` 不使用 GitHub 的合并按钮。PR 检查通过后，由维护者在本地用 `git merge --ff-only origin/<pr-branch>` 快进 `main`，再推送到远端。仓库隐私检查会继续扫描完整可达历史，作为合入后的保护门。

推荐的合入步骤如下。先确认 PR 的 base 仍是最新 `main`，再在干净 worktree 中设置公开提交身份，执行 `git fetch origin`、`git switch main`、`git pull --ff-only origin main` 和 `git merge --ff-only origin/<pr-branch>`，最后执行 `git push origin HEAD:main`。如果无法快进，应先更新 PR 分支并重新检查，不能改用 GitHub 的其他合并方式。

本地界面验证使用工作目录内构建的 `.app`。需要安装、分发测试包或正式发布时，分别按[任务授权约定](AGENTS.md)和[正式发布流程](native/RELEASING.md)执行。
