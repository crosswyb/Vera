# 真实交互工作台 · ZSJH

一个本机 macOS 会议工作台：收听麦克风和电脑声音，或导入音频；使用 Apple 本地模型转写，识别讨论中值得查证的问题，搜索资料，生成参考回答和会议文件。

## 安装和开始使用

分发包支持 **Apple Silicon、macOS 26 或更新版本**，无需另外安装 Python、Node.js 或知了 App。

1. 把 App 拖入应用程序，打开“设置”。
2. 检查并安装 Apple 中文模型，等待实时和文件转写均就绪。
3. 配置文字模型的服务协议、Base URL、模型名、API Key，测试并保存。
4. 配置 DeepSeek Search 或 Tavily 的地址与独立 Key，测试并保存。
5. 导入一段测试音频或开始收听。真实会议按场授权；没有云配置也可本机录音与转写。

详细流程、权限及免费分发的系统安全检查见 [安装与首次使用](native/DISTRIBUTION.md)。当前包为本机临时签名，未公证；第二台干净 Mac 的完整安装尚需验收。

## 会后资料

每场会议独立保存到 `~/Documents/ZSJH/Meetings/`：录音、`02-全量转录.md`、`03-会议纪要.md`，以及按时间命名的查证 Markdown。实时录音默认归档 M4A，可选择额外保留 CAF；导入源文件不修改。

资料长期保留，**没有七天自动删除**。删除会议须先查看影响并确认。没有收到音频或 AI 总结失败会明确显示；仅有本机摘录时不冒充完成的 AI 纪要。结束会议后可继续恢复或重试查证。

主题可在设置中选择跟随系统、浅色或深色，App 重启后仍保留。

## 开发与构建

需要 Python 3.11+、支持 macOS 26 SDK 的 Xcode 命令行工具，以及 Node.js（仅用于 JavaScript 语法检查）。

```sh
python3 -m venv .venv-live
.venv-live/bin/pip install -r requirements-build.txt
.venv-live/bin/python tools/collect_dependency_notices.py
.venv-live/bin/python tools/verify_meeting_workbench.py --native
/bin/zsh tools/build_workbench_backend.sh
/bin/zsh native/scripts/build-desktop.sh
/bin/zsh native/scripts/package-distribution.sh
```

`requirements-build.lock` 记录验证构建所用的依赖版本。App 产物为 `native/build/ZSJHWorkbench.app`，分发包位于 `native/build/distribution/`；都不提交源码仓库。

仅启动本机 Web 后端进行开发：

```sh
.venv-live/bin/python -m live.workbench_cli init
.venv-live/bin/python -m live.workbench_cli serve
```

网页地址是 `http://127.0.0.1:8767/workbench`。访问码保存在私有 `.zsjh/workbench-pilot/credentials.json`，不能提交。浏览器开发入口不提供原生文件选择、采音及 Apple 模型安装能力。桌面用户直接使用设置，不需要编辑环境文件。

## 项目结构和验证范围

[实现与验收](docs/implementation.md) · [本次发布验收](docs/release-2026-11-19.md) · [目录归档说明](docs/archive-layout.md) · [原生开发说明](native/README.md)

当前主路线只包含工作台。旧回放、独立 RTC 会议房等路线已移到同级 `back1119`，不参与构建。共享存储模块保留历史数据结构以兼容本机现有资料。

本机识别由 Apple 提供；文本模型支持 DeepSeek 和 OpenAI Chat Completions 兼容服务；搜索支持 DeepSeek/Tavily。应用免费，第三方 API 费用由用户的服务账户承担。

自动测试主要覆盖合成输入、权限、调度恢复、错误诊断和资料保存；不能代替实际转写准确率、查证语义质量、长会设备恢复、第二台机器和腾讯会议内双向语音验收。查证结果是带来源的参考资料，由人判断和采纳。

第三方许可与改编记录见 `third_party/`，图标许可见 `static/LUCIDE-LICENSE.txt`。
