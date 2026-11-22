# macOS 正式发布

正式分发使用 Developer ID Application 证书、Hardened Runtime、安全时间戳，以及 Apple 公证与票据装订。DMG/ZIP 分发不使用 Developer ID Installer 证书。本项目安装包、密钥、证书凭据、会议数据库和现场调试产物不放进 Git；安装包作为GitHub 仓库的 Release 附件发布。

产品英文名为 **Vera（薇拉）**，取意于 *veritas*（真实）。App 构建目录和归档内的应用均为 `真实交互.app`，安装后的应用名为“真实交互”；安装包统一使用 `Vera-版本-build构建号-apple-silicon` 前缀，磁盘卷名为 Vera。签名与发布入口检查中文应用名，防止包名再次分歧；旧 bundle identifier 和本机资料目录用于兼容已有用户。

## 发布前

1. 使用仓库本地的 `Vera Contributors <dev@vera.invalid>` 提交身份，检查并提交本次有效源码，使 `git status --short` 为空。跨版本持续递增 `native/DesktopInfo.plist` 的构建号后再提交；桌面构建会把当前提交写入 `ZSJHSourceRevision`。
2. 运行离线验证：`.venv-live/bin/python tools/verify_meeting_workbench.py --native`。根据实际改动执行已有前端或原生复验。真实服务验收应单独说明。
3. 在本机钥匙串准备有效的 Developer ID Application 签名身份和 `notarytool` 凭据 profile。可用独立钥匙串，避免修改用户默认签名身份。钥匙串和 Apple 凭据由发布者管理，不能放入仓库、命令示例或安装包。
4. 用 `security find-identity -v -p codesigning` 检查身份，用 `xcrun notarytool history --keychain-profile <profile>` 验证账户访问。Apple 账户协议错误必须先由账户持有人处理；不能跳过公证后宣称发布成功。
5. 用 `.venv-live/bin/python tools/release_privacy.py --source-root . --git-ref HEAD` 检查当前文件、全部可达历史与提交身份。检查发现个人路径、非示例邮箱、密钥或私有资料文件就停止。额外的本机敏感词可放在 `~/.config/vera/release-private-terms.txt`（每行一个），也可通过 `VERA_PRIVATE_TERMS_FILE` 指定；该文件不进入仓库和安装包。文档不得复制真实会议名称或原话，使用合成示例。

## 构建、签名与公证

```sh
/bin/zsh tools/build_workbench_backend.sh
/bin/zsh native/scripts/build-desktop.sh
python3 native/scripts/sign-distribution.py \
  --app native/build/真实交互.app \
  --output-dir native/build/releases/build55 \
  --identity '<Developer ID Application 身份或 SHA-1>' \
  --keychain '<签名钥匙串绝对路径>' \
  --notary-profile '<已验证的 notarytool profile>'
```

不使用独立钥匙串时可省略 `--keychain`。每次必须选择空的输出目录，脚本拒绝覆盖已有发布。证书材料的导入与 profile 建立由发布者在本机完成，脚本不读取私钥或 Apple 密码。

脚本复制构建包，依次签名内嵌 Mach-O、音频组件与外层 App。主 App 和音频组件仅增加 Hardened Runtime 的麦克风输入 entitlement；不增加 App Sandbox、JIT 或禁用库验证等权限。

原生组件按 Release 编译，映射本机源码前缀并在签名前去掉调试符号。第三方许可收集只保留许可文本，清除上次收集残留的模块与缓存。打包和正式签名都调用同一个隐私检查器：不只检查文件名，还检查二进制字符串、冻结 Python 压缩内容、ZIP 元数据和允许的合成语音样本。冻结内容无法读取时停止，不把“没读到”算作通过。正式签名还要求源码已提交、构建内记录的提交与当前提交一致。

Developer ID 证书持有人的姓名和团队身份可由签名读取，第三方许可也保留原作者署名。这两类是预期的公开身份信息；隐私检查通过不代表签名匿名，也不能自动判断任意一句自然语言是否涉及私人业务。

App ZIP 公证成功后装订 App，再制作含已装订 App 的 DMG/ZIP。DMG 单独签名、公证和装订。脚本只有在两个公证结果均为 `Accepted`，装订验证和 Gatekeeper 检查均通过后，才写入 `release-verification.json`。若公证超时或失败，先检查已有提交编号和日志，避免无意义地重复上传。

## 发包验证与上传

1. 只读挂载 DMG，再核验挂载包的 `codesign --verify --deep --strict`、`xcrun stapler validate` 和 `spctl --assess --type execute --verbose=2`。
2. 针对挂载包执行 `native/scripts/probe_desktop_backend.py` 的隔离探针，确认 Hardened Runtime 下的 Python、动态扩展、TLS 初始化、本机鉴权和会议文件流程可运行。探针使用临时工作区，不读取真实会议或调用付费服务。
3. 在发布记录写入构建号、源码提交、两个公证提交编号、SHA-256 和验证边界。第一次正式分发仍需要第二台干净 Mac 验证安装、Apple 模型和麦克风/屏幕录制授权。
4. 推送已验证源码与对应版本标签，使用下面的发布入口。它重新检查完整历史、两个最终归档和公证回执，限定四个上传附件，确认公开发布目标为官方仓库 `crosswyb/Vera`、远端标签与源码一致后才创建 Release，并复核服务器附件哈希。不要直接上传整个发布目录；里面的原始 Apple 日志和本机检查报告不应公开。

```sh
.venv-live/bin/python native/scripts/publish-release.py \
  --release-dir native/build/releases/build55 \
  --repo '<owner/repository>' \
  --notes-file docs/release-build55.md
```

源码检查也由 GitHub Actions 执行；新的个人邮箱提交或本机路径进入历史时会报错。已经发布的历史信息需要改写历史并撤下旧包，新增一个清理提交不足以完成清除。

Apple 官方参考：

- https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input
- https://developer.apple.com/library/archive/technotes/tn2206/_index.html
