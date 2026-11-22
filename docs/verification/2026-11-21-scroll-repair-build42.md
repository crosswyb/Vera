# 整页异常滚动修复与安装：build 42

2026-11-21，Asia/Shanghai。安装前为 `/Applications/真实交互.app`，版本 **0.1.0，build 39**。之前的 build 41 仅为候选包。本轮已将包含识别恢复修复和滚动修复的 **0.1.0，build 42** 安装到同一路径，并打开实际应用核验。

## 原因与改动

真实前端在长转写中渲染查证便签。`static/workbench.js` 的 `makeNote()` 在便签标题中加入 `.visually-hidden` 读屏辅助文字；该类使用绝对定位。当滚动区域和其祖先均未建立定位容器时，这些屏外辅助元素的静态位置会扩张文档滚动范围。

本轮使用安装版的 HTML、CSS 和 JavaScript，以及隔离数据库中的 180 条合成发言、30 个关联问题复现。766 像素高的窗口产生了 13,698 像素的整页滚动范围；滚动 350 像素后，顶部栏移动到 -350 像素，底部出现大片空白。只保留最后一条便签辅助文字仍可复现，去除所有便签辅助文字后页面恢复为 766 像素。仅插入发言与问题列表的静态测试无法覆盖这一便签渲染问题。

本轮对产品代码仅作以下修改：

- `static/workbench.css:65`：让 `#main` 和 `.scroll-region` 建立定位容器，使绝对定位元素归属对应的滚动区域。没有隐藏根页面滚动条，没有删除读屏文字。
- `native/DesktopInfo.plist:11`：构建号从 39 更新为 42。

所用机制已对照 W3C CSS Positioned Layout 第 2 节核验；本轮抓取副本为 `outputs/scroll-repair-20261121/css-position-spec.txt`。没有新增前端依赖，也未修改此前识别恢复逻辑。

## 验证结果

| 检查 | 本轮结果 | 证据 |
|---|---|---|
| 安装版症状与最小复现 | 已复现；最后一条便签辅助文字即可撑大整页 | [初始几何](../../outputs/scroll-repair-20261121/chromium-baseline.json)、[最小化](../../outputs/scroll-repair-20261121/minimized.json)、[故障截图](../../outputs/scroll-repair-20261121/baseline.png) |
| 单变量定位实验 | 将辅助元素约束在定位容器后，整页高度恢复 | [实验记录](../../outputs/scroll-repair-20261121/cause-experiments.json) |
| WKWebView 旧版对照 | 48 个场景均检测到整页范围异常 | [旧版结果](../../outputs/scroll-repair-20261121/webkit-baseline.json) |
| WKWebView 已安装新版 | 48 个场景，0 失败 | [安装版结果](../../outputs/scroll-repair-20261121/webkit-installed.json) |
| Chromium 实际前端 | 32 项，0 失败；覆盖两种布局、缩放、自动定位、便签展开、问题与文件切换、连续追加 12 条转写、桌面与移动设置页滚动 | [浏览器结果](../../outputs/scroll-repair-20261121/source-browser.json)、[最终截图](../../outputs/scroll-repair-20261121/source-final.png) |
| 离线检查 | 427 项：426 通过、1 跳过；JS 语法、差异空白和原生组件冒烟检查通过 | [离线报告](../../outputs/scroll-repair-20261121/offline/report.json) |
| 包内识别恢复 | 合法项保存、坏引用补回、后续输入继续；最终 6 个问题、6 次查证、6 份文档 | [识别恢复](../../outputs/scroll-repair-20261121/frozen-recovery.json) |
| 包内桌面后端 | 初始化、设置持久化、会议文件、隔离存储等检查通过 | [后端检查](../../outputs/scroll-repair-20261121/frozen-smoke.json) |
| 打包与安装 | 深度严格签名通过；7 个核心模块和 4 个前端资源与源码一致；安装文件与候选一致 | [候选清单](../../outputs/scroll-repair-20261121/candidate-manifest.json)、[安装记录](../../outputs/scroll-repair-20261121/install-check.json) |
| 实际原生应用 | 运行路径为 `/Applications/真实交互.app`；已连接；现有 2,306 条转写与 50 个问题可显示；滚动后导航、顶部栏和页脚保持可见；运行中提供的 CSS 与安装包一致 | [原生核验记录](../../outputs/scroll-repair-20261121/native-ui-check.json) |

跳过的一项要求已安装并初始化的飞书 CLI，详见 `offline/workbench-unittest.log:57`。本轮未重新进行 60 分钟真实收音、外部模型质量评估或第二台机器安装验收。实际应用检查只回看已结束会议，没有启动收音、恢复自动查证或修改会议正文。

用户原附件的临时路径在本轮已不存在，因此本轮以独立复现得到的截图和几何数据为直接依据。W3C 原文已读取；另尝试读取 Bootstrap 的辅助文字样式源码时网络请求失败，未以该源码作为修复依据。

## 安装与恢复

- 当前应用：`/Applications/真实交互.app`，**0.1.0 / build 42**。
- 新版候选：`outputs/scroll-repair-20261121/真实交互-0.1.0-build42.app`。
- 旧版备份：`outputs/scroll-repair-20261121/真实交互-build39-安装前备份.app`。

安装时应用未运行。先在暂存路径复制并校验新版，再移动旧包到备份位置，最后将新版原子重命名到安装位置。安装器没有改动 Application Support、会议目录、数据库、录音或服务设置。旧包是程序备份；若以后需要回退，不能直接让旧程序接管已写入新版识别协议的数据库，应按 `docs/question-detection-recovery-plan.md` 中的方式，使用 `tools/prepare_detection_rollback.py` 导出暂停副本后核验。

## 复跑

所有浏览器和 WebKit 回归均使用 `outputs/scroll-repair-20261121/fixture.py` 创建的临时数据库与本机服务，不使用正式会议或外部服务。

1. 运行 `.venv-live/bin/python outputs/scroll-repair-20261121/fixture.py`，读取生成的 `fixture.json` 中的本机地址和会议 ID。
2. 运行 `ego-browser nodejs < outputs/scroll-repair-20261121/verify-scroll.mjs`，结束后在该脚本所用的 Ego 任务空间调用 `finish({keep:[]})`。
3. 编译 `swiftc outputs/scroll-repair-20261121/probe-webkit.swift -o outputs/scroll-repair-20261121/probe-webkit`；参数依次为 `本机地址/scroll-fixture/installed#meeting/会议ID`、`measure.js` 的绝对路径、结果 JSON 的绝对路径。程序会实际创建与宿主一致的 WKWebView；滚动范围异常时退出码为 1。
4. 停止隔离服务器。完整安装和验证汇总见 [summary.json](../../outputs/scroll-repair-20261121/summary.json)。
