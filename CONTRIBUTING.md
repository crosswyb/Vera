# 参与 Vera 开发

报告问题时，请提供触发步骤、实际结果和期望结果。公开 issue 和 PR 使用合成示例，避免贴出会议原文、密钥或本机资料路径。

调查可以共用源码。开始修改时，从最新的 `origin/main` 为每项独立任务建立一个分支和一个 Git worktree。继续同一任务时复用它的分支和工作目录，避免两个会话同时改同一份文件。

修复完成后说明如何复现、如何验证以及尚未覆盖的情况。需要提交评审时，将这些信息写进 PR。其他会话可用 PR 编号接手。多个已验证的修复可以先后合入 `main`，由维护者另行决定何时发布。

每个会话提交代码前都要使用仓库的公开提交身份。先在当前 worktree 执行 `git config user.name "Vera Contributors"` 和 `git config user.email "dev@vera.invalid"`，再检查 `git log -1 --format='%an <%ae> | %cn <%ce>'`。GitHub 只允许使用 **Rebase and merge** 合入 `main`，禁止创建 GitHub merge commit 或使用 squash merge，因为它们可能把个人邮箱写入可达历史并使发布隐私检查失败。

本地界面验证使用工作目录内构建的 `.app`。需要安装、分发测试包或正式发布时，分别按[任务授权约定](AGENTS.md)和[正式发布流程](native/RELEASING.md)执行。
