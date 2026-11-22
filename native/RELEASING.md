# macOS 正式发布

正式分发使用 Developer ID Application 证书、Hardened Runtime、安全时间戳，以及 Apple 公证与票据装订。DMG/ZIP 分发不使用 Developer ID Installer 证书。本项目安装包、密钥、证书凭据、会议数据库和现场调试产物不放进 Git；安装包作为私有 GitHub 仓库的 Release 附件发布。

## 发布前

1. 检查并提交本次有效源码，使 `git status --short` 为空。递增 `native/DesktopInfo.plist` 的构建号后再提交；桌面构建会把当前提交写入 `ZSJHSourceRevision`。
2. 运行离线验证：`.venv-live/bin/python tools/verify_meeting_workbench.py --native`。根据实际改动执行已有前端或原生复验。真实服务验收应单独说明。
3. 在本机钥匙串准备有效的 Developer ID Application 签名身份和 `notarytool` 凭据 profile。可用独立钥匙串，避免修改用户默认签名身份。钥匙串和 Apple 凭据由发布者管理，不能放入仓库、命令示例或安装包。
4. 用 `security find-identity -v -p codesigning` 检查身份，用 `xcrun notarytool history --keychain-profile <profile>` 验证账户访问。Apple 账户协议错误必须先由账户持有人处理；不能跳过公证后宣称发布成功。

## 构建、签名与公证

```sh
/bin/zsh tools/build_workbench_backend.sh
/bin/zsh native/scripts/build-desktop.sh
python3 native/scripts/sign-distribution.py \
  --app native/build/ZSJHWorkbench.app \
  --output-dir native/build/releases/build51 \
  --identity '<Developer ID Application 身份或 SHA-1>' \
  --keychain '<签名钥匙串绝对路径>' \
  --notary-profile '<已验证的 notarytool profile>'
```

不使用独立钥匙串时可省略 `--keychain`。每次必须选择空的输出目录，脚本拒绝覆盖已有发布。证书材料的导入与 profile 建立由发布者在本机完成，脚本不读取私钥或 Apple 密码。

脚本复制构建包，依次签名内嵌 Mach-O、音频组件与外层 App。主 App 和音频组件仅增加 Hardened Runtime 的麦克风输入 entitlement；不增加 App Sandbox、JIT 或禁用库验证等权限。

App ZIP 公证成功后装订 App，再制作含已装订 App 的 DMG/ZIP。DMG 单独签名、公证和装订。脚本只有在两个公证结果均为 `Accepted`，装订验证和 Gatekeeper 检查均通过后，才写入 `release-verification.json`。若公证超时或失败，先检查已有提交编号和日志，避免无意义地重复上传。

## 发包验证与上传

1. 只读挂载 DMG，再核验挂载包的 `codesign --verify --deep --strict`、`xcrun stapler validate` 和 `spctl --assess --type execute --verbose=2`。
2. 针对挂载包执行 `native/scripts/probe_desktop_backend.py` 的隔离探针，确认 Hardened Runtime 下的 Python、动态扩展、TLS 初始化、本机鉴权和会议文件流程可运行。探针使用临时工作区，不读取真实会议或调用付费服务。
3. 在发布记录写入构建号、源码提交、两个公证提交编号、SHA-256 和验证边界。第一次正式分发仍需要第二台干净 Mac 验证安装、Apple 模型和麦克风/屏幕录制授权。
4. 确认远端仓库仍为 Private，再推送源码和带版本的 Release 附件。不要将二进制直接加进 Git 历史。发布后核对远端提交、附件大小及附件 SHA-256。

Apple 官方参考：

- https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input
- https://developer.apple.com/library/archive/technotes/tn2206/_index.html
