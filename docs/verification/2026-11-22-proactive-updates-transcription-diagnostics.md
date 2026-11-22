# 主动检查更新与转写异常提示验证

## 结果

基于 `origin/main` 的 `d5ea220`，在独立分支 `codex/proactive-updates-transcription-status` 完成三项源码修改：

1. App 页面加载成功后自动检查正式版；运行期间每 6 小时检查一次，回到前台时也会检查是否已到间隔。发现新版时，顶部出现可点击的提示。
2. 「设置 → 通用」首项新增「软件更新」，显示当前版本和构建号、检查状态、上次成功检查时间、手动检查按钮、发布页入口和新版发布说明。菜单栏沿用同一个检查结果。
3. 转写缺口改为「转写缺失或中断」，展示音源、影响时段、故障原因和错误码。已有会议通过持久化的 ASR 事件还原原因链，无须迁移或重写会议资料；提供对应原录音入口、语音设置入口及进行中会议的补充文字入口。

## 需要维护者处理

当前源码修改和验证不需要额外决定。按维护者要求提交 PR，检查通过后合入 `main`；安装测试版或正式发布需要另行指定范围。

## 查看顺序

1. 审阅 `native/Sources/DesktopHost/VeraRelease.swift` 和 `DesktopHost.swift` 的自动检查、状态发布及界面命令。
2. 审阅 `templates/workbench.html` 与 `static/workbench.js` 的通用设置和顶部提示。
3. 审阅 `live/speech_workbench.py` 与 `static/workbench-speech.js` 的故障原因还原、展示及录音绑定。

## 行为与边界

- 自动检查复用既有 GitHub Releases 接口和正式版验证。草稿、预发布、命名不符或缺少对应 DMG 的结果继续被拒绝。
- 菜单、定时检查和设置按钮共享一个正在进行的请求；手动重试可越过自动检查间隔。刷新失败会保留已发现的新版与发布说明。
- 发布说明按纯文本显示。更新通知是界面提示；下载后仍由用户替换 App。
- 原因链按会议、录音轮次和音源关联。识别恢复成功后，旧错误不会被关联到后续缺口。历史事件超过最近 40 条的活动列表时仍可还原。
- 没有时间信息时明确显示「影响时段未确认」。没有具体原因时保留未知状态，不推断为错词或低置信度。
- 「打开原录音」只对相同音源、相同录音轮次的已保存或部分保存录音启用。它复用已有播放器入口；用户按显示的时间回听。
- 本次修复的是异常展示、原因保留与操作入口，不代表云端积压或 Apple 识别故障本身已消除。

## 验证证据

### 已运行

- 基线：`python -m unittest tests.test_workbench_frontend_contract tests.test_speech_sessions -q`，32 项通过。
- 修改前新增的症状用例实际失败：原提示仍是「转写需要核对」，原详情没有原因链；旧快照没有 `speech_gaps`。
- 修改后：`python -m unittest tests.test_workbench_frontend_contract tests.test_speech_sessions tests.test_workbench_updates tests.test_workbench_layout tests.test_workbench_daily_use tests.test_workbench_transcript_history tests.test_workbench_audio_import -q`，67 项通过。测试输出中的一次 `OperationalError` 来自既有的数据库故障注入用例，整组结果为通过。
- `swift build --package-path native`，构建通过。编译器仍有既有的框架搜索路径和捕获所有权提示。
- `native/scripts/test-release-check.sh --live`，正式版验证、自动间隔、手动重试、失败后保留提示、旧版本判断、并发请求去重通过；真实 GitHub 请求返回 `v0.1.0-build57`。此请求执行于 2026-11-22，不能视作未来版本状态。
- `native/scripts/test-capture-finish.sh`，实际 DesktopHost 事件处理保留 ASR 错误说明，恢复后清除旧错误，缺口不会抹掉识别状态；既有录音收尾与显式重试测试通过。
- `node --check static/workbench.js`、`node --check static/workbench-speech.js`、`git diff --check` 通过。

### 页面交互

通过 Ego Lite 的独立任务空间，连接临时本机后端和合成会议。网页使用本分支实际模板、脚本与后端；原生桥和更新状态为明确注入的合成数据，未调用真实播放器、未下载更新。

- 点击「设置 → 通用 → 检查更新」，观察检查中与发现新版状态，展开发布说明。
- 点击顶部新版提示，回到通用设置；点击发布页按钮，观察到 `openUpdateRelease` 命令。
- 打开转写缺口详情，观察到「阿里云音频积压 → Apple 无法启动」、错误码和 `01:20–01:32`。
- 点击原录音，观察到指向本场 `run-1:system` 的 `openMeetingAudio` 命令；点击补充文字后焦点进入编辑框；点击检查语音设置后选中语音转写页。
- 在 980 像素宽度下，页面滚动宽度为 980，两颗更新按钮均在视口内。

本机截图保存在忽略目录：`.zsjh/update-settings.png`、`.zsjh/update-settings-980.png` 和 `.zsjh/speech-gap-details.png`。图中的 build 58 为合成新版场景，不是实际发布记录；图片不纳入 Git。

### 尚未验证

- 未在完整 App 中进行真实会议、真实故障切换、QuickTime 实际播放或系统休眠后恢复测试。
- 6 小时间隔使用可控时间验证，未等待真实 6 小时持续运行。
- 未安装或替换用户当前 App，未生成正式分发包，未签正式证书、公证或发布。PR 与合并状态以 GitHub 记录为准。
