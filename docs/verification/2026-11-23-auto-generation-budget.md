# 自动生成额度验证

## 变更行为

新建文字模型配置的生成额度与输出上限参数均默认为「自动」。数值输入只在选择「自定义」后显示；旧配置中的数值、参数名和各模型的独立设置继续保留。环境变量允许 `ZSJH_LIVE_MODEL_MAX_TOKENS=auto`，已有数字继续按原规则解析。

额度沿用 `ModelService.payload()` 的任务分流，不增加服务调用或重试：

| 自动场景 | 单次生成额度 |
| --- | ---: |
| 普通生成、快速识别与快速路由 | 8192 |
| 已识别的 OpenAI 推理模型、显式设置推理强度的兼容模型 | 32768 |
| DeepSeek 官方思考模式 | 65536 |
| DeepSeek 协议的其他地址启用思考 | 32768 |

OpenAI 官方接口和已识别的 OpenAI 推理模型默认发送 `max_completion_tokens`；DeepSeek 和未识别的兼容模型默认发送 `max_tokens`。未知兼容模型也可以显式选择参数名。使用 `max_completion_tokens` 的未知模型按推理生成预留额度；旧 GPT-4 / GPT-3.5 模型在默认强度下继续使用 8192。

自定义模式保留原有任务边界：查证回答使用填写的数值；纪要至少使用 8192；快速识别和快速路由使用 8192 至 16384。截断诊断记录的是最终发送的额度，不会读取 `auto` 作为请求字段，也不会因截断自动重发付费请求。

## 验证结果

- 修改前，`test_provider_settings.py` 的 38 项测试通过。
- 修改后，以下 7 组共 130 项测试通过：`test_provider_settings`、`test_workbench_execution`、`test_workbench_research_progress`、`test_workbench_failure_diagnostics`、`test_workbench_frontend_contract`、`test_workbench_layout`、`test_workbench_model_recovery`。新增用例覆盖自动额度进入请求、旧数值配置、环境变量解析、保存与重启、无效数值拒绝、参数覆盖、识别继承及截断诊断。
- `node --check static/workbench.js`、修改的 Python 文件的 Ruff 正确性检查（E9/F63/F7/F82）和 `git diff --check` 通过。
- `tools/release_privacy.py --source-root . --git-ref HEAD` 通过，源码与已有历史未报告隐私问题。
- PyInstaller 后端构建成功；`swift build --package-path native --product DesktopHost -c debug` 成功；`native/scripts/build-desktop.sh` 构建工作目录内的候选 App 成功，其隐私检查和本机临时签名校验通过。
- `native/scripts/probe_desktop_backend.py` 对新打包后端的离线验证通过，包括自动默认值、自定义额度保存、模型切换恢复、重新启动后的配置读取。JSON 结果保存在 `outputs/auto-generation-budget/frozen-backend.json`。
- 使用 Ego Lite 浏览器连接同一新打包后端的独立合成工作区。确认默认两项「自动」、数值输入隐藏且不参与表单校验、自定义 4200 保存与重新载入、切回自动后保存为 null，以及已保存 Flash / Pro 配置切换时分别恢复自定义与自动模式。结果与截图保存在 `outputs/auto-generation-budget/browser-check.json` 和 `outputs/auto-generation-budget/automatic-settings.png`。

测试命令：

```sh
python - <<'PY'
import unittest
names = [
    'test_provider_settings', 'test_workbench_execution',
    'test_workbench_research_progress', 'test_workbench_failure_diagnostics',
    'test_workbench_frontend_contract', 'test_workbench_layout',
    'test_workbench_model_recovery',
]
suite = unittest.TestSuite(
    unittest.defaultTestLoader.discover('tests', pattern=name + '.py')
    for name in names
)
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
PY
python native/scripts/probe_desktop_backend.py \
  --backend native/build/backend-dist/zsjh-workbench-backend/zsjh-workbench-backend \
  --output outputs/auto-generation-budget/frozen-backend.json
```

## 依据与验证边界

[DeepSeek 官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion) 当前说明：非思考模式默认 8K，思考模式默认 64K，max 强度默认 128K。本次应用策略在思考模式统一使用现有配置允许的 64K 上限；32K 是应用的初始策略，不是所有模型支持能力或实际消耗的保证。

OpenAI 推理指南与 GPT-5 Chat 模型页在本机抓取时返回 HTTP 403，官方 Python SDK 原始文件抓取被连接重置；本次没有从这些失败请求取得新证据。没有进行付费模型实测、真实问题的额度和耗时分布统计，或所有兼容接口的验证。未知服务的特殊限制仍需使用高级设置覆盖。

浏览器验证覆盖打包资源提供的设置页，没有启动候选 App 的 macOS 主窗口，也未执行录音、安装替换、正式签名、公证或发布。Swift 构建成功时仍有既有的捕获引用和工具链路径警告。
