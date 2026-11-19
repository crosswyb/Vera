# 工作台主路线与维护

## 运行结构

DesktopHost（macOS App）启动随包提供的 Python 后端与 AudioHelper。AudioCore 通过 Apple 本地语音能力处理麦克风、电脑声音或导入文件。后端按增量转录识别待查问题，两个查证槽并行搜索、组织回答并保存 Markdown；纪要单独在会后整理。

### 实时与导入调度（build 23）

- 实时问题识别使用独立单线程，保持 0.8 秒防抖、普通 4 段和积压 16 段策略。导入及会后识别使用另一单线程，同类会议按批轮转。
- 导入按约 4000 字组批，保留完整新段落；上限 256 段，总输入仍受 12000 字约束。转写进行中不足 3000 字最多聚合等待 3 秒，转写结束立即处理尾批。模型动作达到 12 条上限时保留游标，随后缩为小批继续识别。
- 结束会议暂停未读识别与未完成查证；恢复后只扫描未提交的内容，可以新增问题、查证并保存文档，会议仍保持已结束。失败识别可以单独重试。
- 全局模型请求最多 3 个，其中导入识别、回答和纪要合计最多占 2 个，为实时问题识别预留 1 个。实时回答也使用这 2 个共享槽，因此仍可能等待长请求；已发请求不抢占。
- 查证执行保持 2 个线程。手动请求优先，实时自动问题优先于导入自动问题；实时会议可运行时，导入自动查证最多占 1 个槽。等待超过 120 秒的后台任务在连续 3 次实时领取后有轮转机会。手动请求可覆盖自动任务的预留规则。
- 游标仅在有效结果提交后前进；等待模型槽后再次检查授权与会议版本，避免结束、删除或撤权后的失效请求继续外发。

`live_api.py` 仅提供工作台的本机认证、请求边界和健康入口；`live/workbench_api.py` 注册业务 API。`live/store.py` 与 `live/contracts.py` 保留共享的持久化基础和历史数据结构，避免改变现有用户数据库。旧回放、RTC 房间与外部语音实验不再有主路线入口。

## 数据规则

会议资料长期保存，不设到期清理；关闭 App、结束会议和暂停查证均不删除已有资料。只有用户查看删除影响并确认删除会议时，清除该会登记产物。录音压缩成功后是否额外保留 CAF，由录音归档设置决定；它与会议保存期限无关。导入源文件不修改。

本机转写不需要云 API Key。文本模型负责识别待查问题、组织参考回答与会议纪要；搜索服务负责取得网页资料。两者独立配置，按场授权。兼容模型服务必须支持当前 Chat Completions 协议，不能声称任意 token 服务均可用。

## 本次修复验收

- 固定积压的错误引用应有界退避，真正修订后可继续，不能无限重跑同批。
- SQLite 暂时锁定后调度能恢复，健康状态区分故障与正常；不自动重发结果未知的付费请求。
- 长纪要在每次请求前复查授权，关闭授权后不得再发后续批或纠正请求。
- 回答完成后写盘失败，结束会议后可以补存；删除、撤权或修订仍阻止失效结果发布。
- 缺失音源逐项显示；零帧仅说明未收到音频，不推断权限故障。
- “第3条”与“第三条”可等价定位，原文版本与引用校验不放宽。
- 深浅及系统主题可手动选择、跨重启保存；两个主题都检查实际渲染。
- 新用户无开发者配置；本地模型状态、服务测试、首次会议、文档路径可走通。

## 验证命令

```sh
.venv-live/bin/python tools/verify_meeting_workbench.py --native
.venv-live/bin/python native/scripts/probe_desktop_backend.py --backend native/build/ZSJHWorkbench.app/Contents/Resources/backend/zsjh-workbench-backend --output .zsjh/release/backend.json
.venv-live/bin/python tools/probe_workbench_execution.py --backend native/build/ZSJHWorkbench.app/Contents/Resources/backend/zsjh-workbench-backend --output .zsjh/release/execution.json
.venv-live/bin/python tools/probe_research_progress.py --backend native/build/ZSJHWorkbench.app/Contents/Resources/backend/zsjh-workbench-backend --output .zsjh/release/progress.json --post-meeting
.venv-live/bin/python tools/probe_detection_strategies.py --backend native/build/ZSJHWorkbench.app/Contents/Resources/backend/zsjh-workbench-backend --output .zsjh/release/detection-strategies.json
```

这些探针只使用独立临时工作区和合成服务，不读取真实会议或发送实际云请求。语义样本位于 `tests/fixtures/`；离线通过不代表模型识别质量、原文命中率或真实会议价值已验收。第二台 Mac、长会设备恢复和腾讯会议内双向发声仍需独立验收。
