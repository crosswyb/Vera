# 真实交互 0.1.0 · build 51

历史记录。本版本由隐私整改后的 Vera build 53 取代，不再分发旧安装包。

发布验证日期：2026-11-22。适用 Apple Silicon，最低 macOS 26。

本版整理并提交了 build 25 之后的应用改动，包括云端语音设置、问题识别与恢复、查证回答呈现、会内搜索和双布局。它是本项目首次完成 Developer ID Application 签名、Apple 公证与票据装订的正式分发包。

## 下载与安装

私有仓库：https://github.com/crosswyb/ZSJH

旧发布页与安装包在 Vera build 53 验证交付后下架。请使用 [build 53 发布记录](release-build53.md) 中的新版安装包。以下内容仅为历史验证记录。

GitHub 仓库保持 Private，下载者需要相应仓库访问权限。Developer ID 签名和 Apple 公证不要求把项目源码公开。

## 正式签名与公证结果

签名类型：Developer ID Application。证书身份属于签名中的公开元数据，不在发布文档中重复列出。

App 运行代码构建自提交 `8ef93cc672946c91208ab045f59b02cf0a20fb64`，已写入包内 `ZSJHSourceRevision`。后续发布提交仅完善发布前的私有文件拦截和此记录，未更改应用运行代码。

| 项目 | 结果 |
| --- | --- |
| 8 个 Mach-O 及内嵌音频 App | Developer ID 签名、安全时间戳；App 启用 Hardened Runtime |
| 主 App 和音频 App 权限 | 仅麦克风输入 entitlement，无 App Sandbox/JIT/禁用库验证权限 |
| App 公证 | `Accepted`，`70c8e6ee-d9ae-40ab-9417-7887f702ba0c` |
| DMG 公证 | `Accepted`，`20c03ae6-ca80-48a4-acc9-3a001bcc4099` |
| 两份 Apple 公证日志 | `issues: null` |
| App、DMG 票据装订与验证 | 通过；ZIP 中也包含已装订的 App |
| App、DMG Gatekeeper | `accepted`，来源 `Notarized Developer ID` |

公证回执摘要随 Release 附件 `release-verification.json` 提供。公证由 Apple 自动检查签名与已知恶意内容，不代表 Apple 审核了会议产品效果，也不等于发布到 Mac App Store。

## 验证

- Python 离线回归：451 项，450 通过、1 项真实飞书 CLI 用例按条件跳过，失败 0。
- 三个前端 JS 文件语法检查、Swift 桌面与音频组件构建通过。
- 最终 DMG 只读挂载与 ZIP 解压后的 17 项检查通过，包括源码提交、开发者身份、Hardened Runtime、麦克风权限、嵌套签名、公证票据和 Gatekeeper。
- 从最终 DMG 内启动签名后的冻结 Python 后端，隔离工作区探针通过：Python 动态扩展、TLS 初始化、鉴权、模型/语音/飞书设置、合成问题文档、结束会议、转录/纪要、存储切换与会议管理均可执行；未使用真实会议数据或付费服务。
- 包中未发现环境凭据、会议数据库或签名私钥。另用四种合成文件验证：正式签名脚本在读取钥匙串和上传 Apple 之前，就拒绝携带 `.env.live`、`credentials.json`、`private.key` 或 SQLite 的应用包。
- 构建产物与现场调试资料留在本机并由 Git 忽略；源码提交未包含录音、私钥或服务 Token。当前安装版未因发布操作被重启或替换。

本机详细证据在 `outputs/release-build51/`；原始 Apple 回执与正式分发包在 `native/build/releases/build51/`。这些目录不进入源码 Git 历史。

## SHA-256

```text
31480e3099120ca081109a257d8a3095acd26c2a5d75372dc54dd5d6e6b71cc8  ZSJHWorkbench-0.1.0-build51-apple-silicon.dmg
c65c33c4060fd397e728e20a69c5e9be4dd08856ca9ed7febf80d6e302e88912  ZSJHWorkbench-0.1.0-build51-apple-silicon.zip
```

## 尚未验证

本次没有第二台干净 Mac；其他用户的首次安装、Apple 模型下载、麦克风/屏幕录制首次授权及真实服务配置仍需在那里验收。本轮未重新发送真实会议到云端语音、模型、搜索或飞书，也未重新测量长会转写准确率和查证语义质量。Apple 公证成功与上述产品验收是不同的结论。

正式分发流程可按 [RELEASING.md](../native/RELEASING.md) 重复执行。
