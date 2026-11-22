# 自动下载与确认安装验证

日期：2026-11-23。变更基于 `0b0085f`，只涉及源码和测试，没有发布新版本或替换已安装 App。

## 行为

- 启动后及运行期间定期检查更新；发现正式新版即自动下载。下载失败可在「设置 → 通用 → 软件更新」重试。
- 通过大小、SHA-256、应用身份、版本、同团队 Apple 签名和 Gatekeeper 校验后，提示「安装并重新打开」或「稍后」。设置中保留安装入口和进度。
- 录音、导入、查证、总结或文档保存未完成时禁止安装。无法读取任务状态时也禁止安装。确认安装后暂停界面及原生任务入口，再检查一次任务状态。
- 下载完成时已有窗口面板会延后提示。确认窗口中的候选版本发生变化时需要重新确认。
- 安装助手等待父进程退出，把 App 复制到目标卷、重新校验后替换；启动命令失败时恢复旧版，由助手重开一次。会议资料和设置在 App 外保存。

## 已运行的验证

| 命令或验证入口 | 结果与覆盖 |
| --- | --- |
| `swift build --package-path native` | 通过，构建 DesktopHost 和 AudioHelper。保留已有工具链路径及 QuickTime 闭包捕获警告。 |
| `/bin/zsh native/scripts/test-release-check.sh --live --live-download` | 通过。真实 GitHub latest 为 `v0.1.1-build58`，通过正式代码下载官方 ZIP，并完成所有包校验。只读取现有 App 的签名团队，未启动或替换它。 |
| `/bin/zsh native/scripts/test-release-check.sh` | 通过。覆盖自动下载、失败重试、6 小时间隔、并发请求、保留已下载候选，以及不可信发布资产拒绝。 |
| `/bin/zsh native/scripts/test-update-install.sh` | 通过。本机 HTTP 真下载、进度、404、截断、超量、非白名单重定向、大小和哈希错误；中文分发 ZIP、只提取 App、路径穿越拒绝；临时目录内真实复制替换、校验失败保留旧版、启动失败恢复、中文空格路径及应用外合成会议文件保持不变。 |
| `/bin/zsh native/scripts/test-capture-finish.sh` | 通过。现有录音退出、停止超时和显式重试的回归测试。 |
| `python -m unittest tests.test_workbench_updates tests.test_workbench_update_readiness tests.test_workbench_frontend_contract tests.test_workbench_layout tests.test_workbench_daily_use tests.test_speech_sessions -q` | 56 项通过。日志中的 SQLite OperationalError 来自现有故障注入场景，测试整体成功。 |
| Ego Lite，真实模板与静态脚本，隔离合成数据库和原生桥 | 通过。顶部入口进入通用设置；45% 和已传输 MB；受阻原因和禁用安装；允许安装后仅发送 `installUpdate`；失败重试后 ready；安装失败保留重试；installing 使主界面 inert；980px 无横向溢出，并查看截图。build 59 仅为合成界面场景。 |
| `node --check static/workbench.js`、`git diff --check` | 通过。 |
| `python tools/release_privacy.py --source-root .` | 通过，无发现。 |

真实官方包：22,349,050 字节，SHA-256 为 `1e0e6409b6ea2f1272d62f27f620f4911861ba249034a005dd6319542858a311`。正式下载使用 GitHub 发布元数据中的资源 ID 构造固定仓库的资源 API 请求，无需用户 GitHub 凭据；重定向限定为 GitHub 的 HTTPS 资源域名。解包使用系统 `bsdtar` 保留中文路径，仅提取 App；分发包中的 Applications 链接和安装说明不会进入安装目录。

## 证据边界

- 隔离安装测试中的签名验证和 App 启动使用注入闭包。真实包的签名和 Gatekeeper 则已由正式下载路径单独验证。
- 未在已安装 App 上执行确认安装、退出、替换和新版工作区启动的完整流程；未正式签名、打包、公证或创建 Release。
- 启动命令成功只表示系统接受了打开请求，不能证明新版会议服务持续正常，也不能保证所有启动后故障自动回滚。
- 目标 App 或其目录不可写时会报错；当前不提供提权安装服务。
- CodeGraph 未建立索引，本次通过源码和调用位置检查替代结构查询。

重复验证可运行以上命令；离线总验证入口 `tools/verify_meeting_workbench.py --native` 已加入安装与下载 smoke。
