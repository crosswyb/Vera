# ZSJH 本机音频与桌面宿主

独立于知了完整 App 的本机音频核心与自有桌面入口。SwiftPM 提供三个产品：`AudioCore` library 处理采音/本地识别；`AudioHelper` AppKit 宿主在同一进程加载核心，通过 JSONL 接收控制；`DesktopHost` 用 AppKit + WKWebView 展示工作台，启动随包本机服务并桥接 AudioHelper。运行时不启动知了 App。

支持范围：Apple Silicon，macOS 26+，单场会议，明确选择的中文音源。当前实机验证是 macOS 27.0.1；macOS 26、第二台机器及长会仍需单独验收。Web 工作台由本项目 `templates/` 和 `static/` 提供。分发打包脚本与新用户步骤见 [安装与首次使用](DISTRIBUTION.md)。腾讯语音输出和虚拟麦克风尚未实现。

## 桌面工作台

后端使用 PyInstaller onedir 随包提供 Python runtime，使用者不需要额外安装 Python。先由项目后端构建脚本生成冻结目录，再打包：

```sh
/bin/zsh tools/build_workbench_backend.sh
/bin/zsh native/scripts/build-desktop.sh
python3 native/scripts/probe_desktop_backend.py
```

开发 App：`native/build/Vera.app`。通过 Finder 双击打开；首次自动创建私有工作区，WKWebView 自动登录本机服务。页面提供开始/停止收听和模型状态，原生“音频”菜单保留相同操作作为备用入口。先创建或选择真实会议，再显式开始收听；示例会议不采集真实音频。退出 App 会关闭本机服务和音频组件；重新打开不会自动恢复采音。

本机安装版为 `/Applications/真实交互.app`，可从“应用程序”中打开。图标原稿位于 `Assets/AppIcon/AppIcon.png`，由 ppimage 生成；`build-desktop.sh` 自动调用 `scripts/build-icon.sh` 生成各分辨率 ICNS 并加入签名包，重建后不会丢失图标。安装副本使用同一应用标识及上述私有工作区，会议记录与配置保存在用户目录。当前为本机临时签名安装，未作 Developer ID 公证分发。

原生标题栏保留标准窗口按钮，初始内容尺寸 1280×820，最小窗口 980×680。设置提供跟随系统、浅色和深色，原生窗口与网页同步切换；桌面偏好保存在 UserDefaults，跨重启保留。网页访问码仍仅保存在临时 WebKit 会话中。

桌面 App 使用自己的路径，不读取项目根目录的 `.env.live`：

- 状态与访问凭据：`~/Library/Application Support/ZSJH/Workbench/`。
- 可选私有服务配置：`~/Library/Application Support/ZSJH/Workbench/workbench.env`。
- 会议文档：`~/Documents/ZSJH/Meetings/`。

新工作区默认没有模型/搜索凭据，不联网研究，不自动采音。`--audio` 仅启用本机音频事件接口。打包资源仅包含程序、静态资源、运行时与许可证，不打包个人 env、访问凭据、数据库或会议录音。首次配置与支持边界见 [安装与首次使用](DISTRIBUTION.md)。

音频桥接事件顺序投递，队列上限 256 条，每条最多重试 3 次；通道失败或积压会停采并显示缺口。原始 PCM 在识别前同步写入本场目录的 CAF 文件，事件通道仍没有磁盘 spool，不能承诺未确认尾部转写事件全部保留。

每场的基础产物为会议录音、`02-全量转录.md` 和 `03-会议纪要.md`（后续修订另存版本）。实时录音时，电脑声音和麦克风分轨写入 CAF；结束会议后在本机逐轨转为 M4A（AAC，每声道 64 kbps）。归档前完整解码验证帧数、采样率和声道，成功登记 M4A 后才清理 CAF。“设置 → 录音归档 → 额外保留原始 CAF 录音”从下次开始收听生效。中断的录音保留可用原件；转码失败时可在会议文件中播放 CAF 并重试归档。已导入的文件始终按原格式保留。正式会议录音、转录和衍生资料长期保留，只有用户主动确认删除会议时清除；外部导入源文件不随会议删除。

## 音频核心构建与检查

```sh
swift build --package-path native
/bin/zsh native/scripts/build-app.sh
/bin/zsh native/scripts/smoke.sh
swift test --package-path native
```

没有外部 Swift 包依赖。`swift test` 需要能提供 XCTest 的完整 Xcode 环境；本机 Command Line Tools 缺少 XCTest/Testing，因此该命令受环境阻断。`smoke.sh` 包含公开 AudioCore 契约检查和独立 CAF 原样读写检查（9600 个 PCM 样本、采样率、权限与拒绝覆盖），不把它冒充完整 XCTest 通过。

生成的开发宿主：`native/build/ZSJHAudioHelper.app/Contents/MacOS/AudioHelper`。`.app` 含正式音频/麦克风用途声明、Cicada 许可证及来源记录，使用临时签名；尚未完成 Developer ID 签名、公证及分发验证。不要将裸 SwiftPM 二进制的权限结果当成分发 App 的验收结果。

## 生命周期与本地模型

默认只等待 stdin 命令，不自动收音。stdout 只输出 JSONL；框架诊断和错误消息走 stderr。以下示例中的每一行是一个独立命令，在同一进程中交互发送：

```json
{"command":"status"}
{"command":"model_status","locale":"zh-CN"}
{"command":"start","meeting_id":"m-example","sources":["system"],"locale":"zh-CN"}
{"command":"stop"}
{"command":"quit"}
```

`source:"system"` 单值写法也可用。允许的 `sources` 为 `system`、`microphone`，也可显式同时选择两者。麦克风使用系统默认输入，首次需系统授权；双音源中的回声、重复识别、重叠发言尚未验收，不能当成腾讯参会者身份或人数。相同配置的重复 start 幂等；变更会议、语言或音源前先 stop。

桌面安装版已提供“电脑声音／麦克风／电脑声音＋麦克风”选择，在创建会议或暂停后切换，并保留上次选择。收听时分别显示两路实际电平及等待输入、安静、有声音状态；收到所有所选音源的启动确认后才显示正在收听。麦克风权限拒绝会显示中文提示与系统设置入口。2026-11-16 本机安装版已观察到麦克风最终转写、双源分别标注的转写与停止事件；这不代表外放回声消除或真人多人识别质量验收通过。双路收听建议使用耳机。

`model_status` 会返回 `installed/downloadable/downloading/unsupported/unknown`。正常 start 只接受已安装本地模型；没有模型时失败并停止，不自动下载，不退到云识别。显式下载安装命令为：

```json
{"command":"install_model","locale":"zh-CN","allow_download":true}
```

该命令调用 Apple AssetInventory，可能使用系统已有资源或下载模型。模型权重不随本项目打包。只查询状态可执行：

```sh
native/build/ZSJHAudioHelper.app/Contents/MacOS/AudioHelper --model-status
```

stdin EOF 或 quit 停止采音并退出。stop 在首次异步等待前释放输入设备，随后处理已接收的尾部音频与转写；3 秒后仍未完成则发出 `audio.gap` 并请求系统识别取消。框架取消本身没有在这里证明硬实时上限。睡眠、输出设备/采样率变化及输入/输出缓冲耗尽均报告 gap 并停止，重启必须显式 start，且产生新 epoch。

## AudioEvent v1

```json
{"protocol_version":1,"meeting_id":"m-example","capture_epoch":"EPOCH","stream_id":"system-EPOCH","seq":8,"type":"transcript.final","source":"system","occurred_at":"2026-11-15T05:30:24Z","segment_id":"s1","revision":3,"supersedes_revision":2,"audio_start_ms":0,"audio_end_ms":2300,"text":"请查一下智能体工作流的定义"}
```

- 基础字段：`protocol_version, meeting_id, capture_epoch, stream_id, seq, type, source, occurred_at`。
- 文字类型：`transcript.interim`、`transcript.final`、`transcript.retracted`；附 `segment_id, revision, audio_start_ms, audio_end_ms, text`，必要时附 `supersedes_revision`。
- 撤回的 `text` 是空字符串且有效。interim→final 和相同起点的更正保留 segment ID、递增 revision；替换其他重叠范围会先撤回被替换片段。停止时未完成的草稿也显式撤回。
- 状态类型：`audio.status, audio.ready, audio.stopped, audio.gap, audio.error, audio.level, audio.model_status`。状态可缺少片段、文字和时间范围；音量含 `rms, frames_received, non_silent_frames, sample_rate, audio_end_ms`。
- `seq` 在每个 `capture_epoch + stream_id` 内从 1 单增，状态也占序号。去重使用会议、epoch、stream、seq。`stream_id` 含 epoch，避免新采集的 `s1` 与旧采集冲突；控制事件使用 `control-EPOCH`。
- 时间范围是各识别输入流从开始接收音频起的样本时钟，不能把睡眠/重启拼成无缝连续录音。`occurred_at` 是 UTC 事件产生时间。
- 第一次 start 前的状态/模型事件 `meeting_id` 为空，不应作为会议转写入库。桥接端确定会议绑定，不能允许模型或网页指定另一会议。

纠错保留最近 256 个片段；超出窗口的旧更正报告错误/gap 并停采，避免发明冲突版本。缓冲是有界的：原始采样 128 个、SpeechAnalyzer 输入 128 个、识别结果 256 个，stdout 待写预算 2 MB。溢出停采，不能声称断线期间全部音频仍被保留。桌面 start 显式传入 `recording_directory` 与 `capture_epoch`，核心在设备回调之外写入原采样率 PCM CAF；写盘失败停采并报告 `recording_failed`。关闭文件后发送 `audio.recording`，带 `saved/partial/missing` 状态；接收端登记路径并处理清理。独立使用核心且不传录音目录时不录音。

`audio.ready` 只说明设备操作启动，不能证明授权或识别成功。持续零样本可能是静音或权限原因，不使用私有 TCC 探测，也不直接显示“权限已拒绝”。非静音样本和完成转写才是本轮实际处理的证据。

## 合成系统播放验证

已批准下载模型时执行：

```sh
python3 native/scripts/probe_synthetic.py --install-model --seconds 20
```

模型已安装后移除 `--install-model`。脚本用本机 `say` 生成中文、用独立 `afplay` 播放，tap 只包含这个合成播放器进程，避免录入其他 App 的声音。输出在 `native/build/synthetic-probe-时间/`，含事件、摘要和合成 AIFF；不读取用户会议录音，不调用付费服务，不将转写发送到文字模型。

仅排查采集、不启用识别时：

```sh
python3 native/scripts/probe_synthetic.py --capture-only --seconds 10
```

增加 `--record` 可同时验证合成采音写盘与 CAF 可读性；不会打开全局音源，仅采集脚本创建的播放器。

该模式使用显式 `capture_probe` 命令，必须提供 `meeting_id, process_id, seconds(1..30)`，只报告帧数和音量，ready/status 的 code 为 `capture_only`。正常 start 也支持可选 `process_id`，用于限制到已知测试播放器；日常不传该参数则收听系统输出。音频源全程仅在本机处理；只有显式模型安装会调用 Apple 模型服务。

## 来源与剩余验收

固定上游与修改清单见 `third_party/cicada/UPSTREAM.md`。提取的是 SystemAudioTap、PCMConverter、现代 Analyzer，未嵌入完整 ListenEngine/AudioRouter；删除了原 UI、翻译、云 fallback、凭据、声纹、旧系统兼容和私有权限预检。原知了 App 不作为运行依赖。

仍需验证：macOS 26 与第二台 Apple Silicon、真人麦克风/双源回声、蓝牙设备切换、系统睡眠、权限拒绝/撤销、断管/高压缓冲、60–120 分钟连续运行、真实语音识别质量、完整 XCTest，以及桌面整包下真实音频和联网研究联动。自有桌面壳已验证本机离线合成流程，不能据此宣称完整真人会议通过。腾讯会议双向语音、虚拟麦克风和 AI 播报不属于本核心已经完成的能力。
