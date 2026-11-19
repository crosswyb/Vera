# 工作台主路线与维护

## 运行结构

DesktopHost（macOS App）启动随包提供的 Python 后端与 AudioHelper。AudioCore 通过 Apple 本地语音能力处理麦克风、电脑声音或导入文件。后端按增量转录识别待查问题，两个查证槽并行搜索、组织回答并保存 Markdown；纪要单独在会后整理。

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
```

这些探针只使用独立临时工作区和合成服务，不读取真实会议或发送实际云请求。语义样本位于 `tests/fixtures/`；离线通过不代表模型识别质量、原文命中率或真实会议价值已验收。第二台 Mac、长会设备恢复和腾讯会议内双向发声仍需独立验收。
