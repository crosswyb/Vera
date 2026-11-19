# 2026-11-19 主路线整理

主路线为真实交互工作台。旧回放程序、LiveKit 试验、过往方案/验证资料、旧虚拟环境和旧配置文件已搬到项目同级 `../back1119/legacy-20261119/`。归档不参与构建或 GitHub 上传。

归档的 `MOVE-MANIFEST.json` 逐文件记录原路径、字节数和 SHA-256，移动后重新核对：旧路线共 8,227 个文件，历史运行证据另有 2,102 个文件移到 `../back1119/local-evidence-20261119/`。旧入口裁剪前的副本在 `entrypoint-snapshots/`。敏感环境文件仅本地保留，不能把整个归档目录上传。

保留在主线的目录：

| 路径 | 用途 |
| --- | --- |
| `native/` | 原生界面、音频核心、打包脚本及测试 |
| `live/` | 工作台调度、搜索、模型、存储和归档 |
| `templates/`、`static/` | 唯一工作台页面、样式及图标许可 |
| `tests/`、`tools/` | 工作台回归、合成验收和构建工具 |
| `third_party/` | Cicada 来源记录和依赖许可 |
| `docs/` | 当前实现、归档说明、发布验收记录 |

`.zsjh/`、`native/build/`、`native/.build/` 与 `.venv-live/` 是本机验证/构建工作目录，受 Git ignore 排除。已安装 App 的用户资料位于用户 Library/Application Support 与 Documents，不依赖开发目录及归档目录。

历史 Git 初始提交保留；当前工作区原为已有 bare 仓库的 worktree，并非全新目录。避免重建 `.git` 或破坏其它分支。
