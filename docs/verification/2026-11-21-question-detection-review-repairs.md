# 会议问题识别审核修复：build 41

2026-11-21。此次交付是独立候选包，未替换 `/Applications/真实交互.app`（build 39）。在临时会议中，build 40 因来源编号类型错误而停止识别；build 41 保住同批合法问题，补回错误项，并继续识别下一条发言。

## 八项行为与验收依据

| 此前的问题 | 修正后的可观察行为 | 回归用例 |
|---|---|---|
| 单项来源编号或修订号是数组、对象时整批回滚 | 同批正确问题保存，进度推进，错误项留给补扫 | `test_malformed_refs_do_not_rollback_valid_question_or_block_new_input` |
| 补偿结果保存失败被当成模型故障 | 模拟 SQLite 和文件写入失败，问题留在待核对；没有给整场会议设置模型退避，后续发言继续处理 | `test_sqlite_failure_is_reviewed_locally`、`test_filesystem_failure_is_reviewed_locally` |
| 空补扫冲掉已知待核对项 | 可定位的提案单独纠正；空结果或无关问题不能结案。跨段提案在正确片段得到确认后可以结案 | `test_unlocated_scan_failure_and_overlap_survive_empty_retry`、`test_only_one_window_must_confirm_an_unlocated_main_proposal` |
| 修引文时换掉查证目标 | 已有效的查证事实、检索词和结构化目标受保护。已有线索的同题补扫若改变目标，提交前拦住；用原目标重试仍可建题 | `test_quote_repair_preserves_public_fact_query_and_structured_target`、`test_changed_public_target_cannot_close_an_unlocated_proposal` |
| 早发晚回的成功请求提前解除新限流 | 成功结果仍可保存，但两个识别通道均须遵守已登记的退避时间 | `test_success_from_before_rate_limit_cannot_release_either_lane_early` |
| 超过实时动作上限 | 实时上限为 4 时先处理前 4 条，安排续扫；测试中的第 5 题被补回，无重复 | `test_live_action_limit_leaves_overflow_for_durable_scan` |
| 纯标点算作依据 | 不接受纯标点；“谁？”这样的短原话可用 | `test_punctuation_cannot_ground_a_proposal_but_one_chinese_character_can` |
| 原标题超长导致纠正永远失败 | 超长或类型错误的字段可修复，其余有效字段仍受保护 | `test_invalid_prior_fields_can_be_repaired_without_weakening_valid_fields` |

## 验证记录

所有打包场景都使用临时目录和本机模拟服务。下列链接指向本轮产物。

- [旧候选包](../../outputs/detection-recovery-review-20261121/baseline-confirmed.log)：合成 D、E 同批输入，E 来源编号为数组，主识别停止，报 `request_failed`。
- [新候选包](../../outputs/detection-recovery-review-20261121/frozen-recovery.json)：未手动重试前，A、C 已完成而 B 可见地待核对；单项重试仅补 B。随后 D、E 坏引用场景通过，继续识别 F。最终 6 个不同问题、6 次查证、6 份文档。
- [密集输入](../../outputs/detection-recovery-review-20261121/frozen-execution.json)：42 个问题、42 次查证、42 份文档，5 次主识别、8 次补扫。[服务错误](../../outputs/detection-recovery-review-20261121/frozen-transport.json)：暂时故障恢复，401 不重试，持续失败在 3 次重试后停止。[桌面后端基础验证](../../outputs/detection-recovery-review-20261121/frozen-smoke.json)通过。
- [离线检查](../../outputs/detection-recovery-review-20261121/offline/report.json)：427 项 Python 用例，426 通过、1 项跳过（需要已初始化的飞书 CLI）；JS 语法、Git 差异空白、原生组件冒烟检查通过。复跑：`TZ=Asia/Shanghai .venv-live/bin/python tools/verify_meeting_workbench.py --native`。
- [包内模块回归](../../outputs/detection-recovery-review-20261121/packed-regressions.log)：测试进程直接载入候选后端内的 7 个核心模块，14 项新增回归全部通过。[候选包清单](../../outputs/detection-recovery-review-20261121/candidate-manifest.json)：这 7 个模块与本轮源码编译结果一致，3 个前端资源与源码一致，应用深度严格签名检查通过；安装版后端摘要仍与本次修复前相同。

## 正式会议前的边界

本轮没有使用生产会议、调用外部模型或进行 60 分钟真实收音。以前 build 40 的会议副本回放不能代替 build 41 的真会验收。建议在独立会议环境录制至少 60 分钟，对照人工标注核实收音连续、后续发言继续识别、有依据的问题没有漏掉、引文确实支持所查的问题，再考虑替换安装版。若磁盘持续不可写，本地仍无法持久化；本次验证的是单次保存失败不会被当作整场模型故障。
