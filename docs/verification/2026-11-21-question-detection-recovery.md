# 问题识别故障隔离：实施与验收

日期：2026-11-21。范围：[批准的修改方案](../question-detection-recovery-plan.md)。

## 结果

代码已实现，已生成「真实交互」build 40 候选包。同批某条问题校验失败时，合格问题照常提交，后续新发言继续识别；失败项进入独立、有限次数的补偿，不能修正的保留为“待核对”。

本次交付止于候选版。没有替换 `/Applications/真实交互.app`，安装版仍为 build 39；没有修改事故会议的原始记录。候选包和现有工作区包含此前已存在的其他修改，本次没有提交或推送 Git。

## 需要用户提供的内容

本次代码实现和候选版工程验证不需要补充信息或批准。正式会议启用前仍需要真实长会运行及人工语义验收；这些验收没有被记为通过。

## 先做什么

1. 查看本报告及下方证据。候选包位于 `outputs/detection-recovery-20261121/真实交互-识别恢复候选.app`。
2. 如需自行复验，先运行下面的隔离检查。它自动创建临时工作区，只使用本机合成模型和搜索服务，不读取正式会议，也不发送飞书消息。
3. 正式启用前确认没有正在录音、导入或收尾的任务，备份完整工作区和会议目录，再安排安装替换与长会验收。本次未执行这个步骤。候选原生应用仍使用正式工作区路径，不应与安装版同时打开同一数据库。

在仓库根目录执行：

```bash
.venv-live/bin/python tools/probe_detection_recovery.py \
  --backend 'outputs/detection-recovery-20261121/真实交互-识别恢复候选.app/Contents/Resources/backend/zsjh-workbench-backend' \
  --output outputs/detection-recovery-20261121/manual-recheck.json
```

## 改动与行为

| 改动 | 实现位置 | 现在的行为 |
|---|---|---|
| 逐条解析 | `live/detection.py`、`live/workbench_contracts.py` | 保留合法条目和过滤前的数量。类型错误、坏编号只拒绝对应条目；无法定位时保留输入范围。非标准 JSON 数值不会进入 SQLite JSON 记录 |
| 原话编号 | `live/detection.py`、`live/workbench_runtime.py` | 模型选择当前发言的原文片段编号，程序回填文字。支持“为什么？”等短问题，旧发言仅作背景 |
| 原子提交 | `live/detection_recovery.py`、`live/workbench.py` | 合格动作、补偿任务、扫描位置在一个事务中提交。逐项版本冲突使用保存点隔离；存储失败整体回滚 |
| 持久补偿 | `live/detection_recovery.py` | 纠正问题一次，范围补扫最多两次；空纠正、换题或改范围不算成功。重复回调不重复提交。扫描同一原话时出现不同标题，保留为待核对，避免直接重建或错误合并 |
| 持续调度 | `live/workbench_runtime.py` | 补偿有独立工作线程，与实时、导入通道分开，仍受共享模型并发限制。补扫携带已提交动作、既有问题和独立待纠正项 |
| 服务故障 | `live/detection_recovery.py`、`live/workbench.py` | 临时错误沿用 2、5、15 秒退避，尊重服务端等待时间，最多等待 300 秒。会议中的补偿项共享有限服务重试，不按积压条目分别放大请求；主识别已有服务故障时补偿停止请求 |
| 操作边界 | `live/workbench.py`、`live/detection_recovery.py` | 暂停、结束、撤权阻止补偿提交；重启保留任务并遵守暂停策略。更正发言立即淘汰旧任务，未更正的范围仍保留。退出后旧任务不再补写失败状态 |
| 可见操作 | `live/workbench_api.py`、`static/workbench.js`、`templates/workbench.html`、`static/workbench.css` | 显示补充识别和待核对数量，可定位对应历史发言、单项重试；有未完成范围时不报告识别完成 |
| 降级导出 | `tools/prepare_detection_rollback.py` | 原库不动，导出独立副本，将未确认会议暂停并回退扫描位置。旧程序不认识新协议，禁止把协议标记当作自动降级保护 |

恢复任务不直接绕过已有问题版本、来源版本、会议授权和外部搜索限制。录音、转写、回答与纪要的原有严格校验继续使用原有路径。

## 确信程度

### 已检查

| 检查 | 结果 | 证据 |
|---|---|---|
| 全部工程用例、JS 语法、差异空白、Swift 框架外检查 | 413 个用例，0 失败、0 错误；其中 1 项真实飞书 CLI 检查按条件跳过。语法、差异、Swift 检查通过 | `outputs/detection-recovery-20261121/offline/report.json`、`offline/workbench-unittest.log` |
| 新的故障恢复验收 | 23 个用例通过，覆盖坏项与好项混合、短问句、非法数值、换题拒绝、过滤前饱和续扫、去重、保存失败、并发修订、退出、重启、权限与单项重试 | `tests/test_detection_recovery.py`，亦在上述全套日志中 |
| 那场真实会议，从故障位置恢复 | 读取原库的只读副本；原有 11 个问题保留。剩余 126 段全部经过识别，2 次实际模型调用，新增 3 题，扫描进度 300/300，没有未完成识别任务 | `outputs/detection-recovery-20261121/real-replay/report.json` 的 `checkpoint` |
| 那场真实会议，全量回放 | 独立空问题副本回放 300 段，4 次实际模型调用，返回 19 题；没有引文结构错误或未完成任务 | 同报告的 `full`；使用当时配置的 `deepseek-flash` |
| 原数据保护 | 原会议及其关联记录回放前后摘要一致；未调用搜索或研究任务，未向正式会议目录写输出 | 同报告的 `original_records_unchanged`、`search_calls`、`production_outputs_written` |
| 打包后故障隔离与重试 | A 合格、B 错误、随后 C 正常：A、C 各查证一次，B 单独待核对；单项重试后仅 B 追加一次查证，共 3 题、3 份文档 | `outputs/detection-recovery-20261121/frozen-recovery-final.json` |
| 桌面与窄屏浏览器 | 已加载 132 段中的历史发言并正确聚焦；暂停时重试禁用，恢复后单项重试成功；窄屏无横向溢出，浏览器未记录脚本错误 | `outputs/detection-recovery-20261121/browser.json`、`recovery-desktop.png`、`recovery-mobile.png` |
| 导入与实时通道 | 676 段合成导入未跳段，导入阻塞期间实时问题约 0.869 秒得到识别；会后新问题也完成保存 | `outputs/detection-recovery-20261121/frozen-strategies.json`；该延时是受控本机结果，不是实际会议承诺 |
| 密集输入与请求成本 | 41 个自动问题加 1 个手动问题，42 次查证、42 份文档，没有重复查证。主识别 5 次，补扫 8 次，补扫预算为 16 次 | `outputs/detection-recovery-20261121/frozen-execution.json` |
| 打包网络故障处理 | 临时故障自动恢复，401 不重试，持续故障在 3 次重试后停止 | `outputs/detection-recovery-20261121/frozen-model-recovery.json` |
| 打包基础能力 | 隔离 init/doctor/serve、认证、设置持久化、文档保存、会议删除及保存目录隔离通过 | `outputs/detection-recovery-20261121/frozen-smoke-final.json` |
| 候选包一致性 | 7 个核心 Python 模块的递归代码对象与当前源码一致；JS、CSS、HTML 与源码一致。深度严格签名检查通过；安装版后端摘要未变 | `outputs/detection-recovery-20261121/candidate-manifest.json` |

本次全部工程检查命令：

```bash
.venv-live/bin/python tools/verify_meeting_workbench.py --native \
  --output outputs/detection-recovery-20261121/offline
```

真实模型回放没有重新识别原音频。回放目录的 SQLite 和 `questions-private.json` 是本地会议数据，保留在私有目录中，不能当作公开测试样本。其模型调用使用原会议已有的文字模型授权；没有新增外部搜索或消息发送。

### 推断与适用边界

- 故障隔离可以降低同类校验错误拖停整场识别的风险；测试无法证明未来所有会议永不失败。
- 原话编号证明引用文字确实存在。它不能自动证明问题理解正确、原话充分支持标题，或所有有价值的问题都已识别。
- 扩大范围补扫依靠已有问题上下文、已提交动作和来源重叠保护减少重复。它不是经过人工标定的语义去重算法，不能声称语义重复率为零。
- 续扫有成本。上述密集合成场景多了 8 次模型请求；正常真实会议副本的这 6 次调用未触发额外补扫。后者不代表所有会议都无需补扫。

### 没有验证

- 人工标注的漏题率、问题价值和语义错引用率。300/300 只证明输入范围处理完毕。
- 连续真实会议收音、设备切换、睡眠唤醒、60 分钟长会稳定性。
- 候选原生窗口在正式工作区的安装升级、原事故会议直接恢复；本次仅做副本恢复。
- 公证、第二台 Mac 的干净安装与对外分发。本包为本机临时签名候选。
- 真实飞书 CLI 检查及真实投递。本次没有向飞书投递会议数据或发送消息。

## 降级操作边界

旧安装包不会检查 `detection_protocol`。如需降级，保留完整新版原库及其会议目录，使用下面的工具**创建新副本**，不要覆盖原库：

```bash
.venv-live/bin/python tools/prepare_detection_rollback.py \
  --source /absolute/path/to/upgraded-workbench.sqlite3 \
  --output /absolute/path/to/new-paused-rollback.sqlite3
```

工具拒绝覆盖已有目标，保留所有补偿记录，把未确认范围保守移回可重扫位置，设置暂停和待确认状态。只有完成核对并显式重试，旧调度器才能继续；不能把回退重扫当作已验证的语义无重复恢复。
