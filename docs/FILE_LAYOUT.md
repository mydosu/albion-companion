# 文件分类与迁移规则

| 位置 | 放什么 | Git 策略 |
|---|---|---|
| 根入口、`shell/`、`pet/`、`tts/` | 当前生产源码、构建配置、运行入口、许可 | 跟踪源码；模型、环境、构建产物忽略 |
| `tests/pet/`、`tests/tts/`、`tests/pi-brain/` | 独立测试、诊断、复现支持脚本 | 跟踪 |
| `tests/pet/data/`、TTS golden JSON | 经过审查的固定输入/预期值 | 跟踪；不放真实私密会话 |
| `tests/tts/eval/`、`tests/tts/probes/` | 需真实服务的 A/B 与语音探针 | 脚本跟踪，产物忽略 |
| `tests/output/<组件>/<运行名>/` | 截图、录音、测量结果 | 全部忽略 |
| `tools/`、`pet/tools/` | 可重复使用的维护、校验、模型处理工具 | 跟踪，使用前阅读副作用 |
| `教程/` | 面向他人的搭建教程（只讲方法，不含模型、声音、人设和凭据） | 跟踪 |
| `docs/history/` | 已确认失效、只作考古的设计与上线记录 | 跟踪；不当作现行说明，引用前先看文件头的失效说明 |
| `archive/legacy/` | 旧补丁、旧发布工具、过期实验脚本 | 仅不含凭据的历史参考；2026-10-24 已清空现有内容（回看用 git 历史），今后新的一次性脚本才放这里 |
| `backup/code/<日期>/<原相对路径>` | 旧生产副本、回滚目录、手工 `.bak` | 全部忽略，保留磁盘内容 |
| `backup/profile-archives/`、`backup/` 原个人回档目录 | 私密 profile 与个人回档点 | 全部忽略，禁止强制提交 |
| `config/rebuild/` | 无凭据的版本清单、权重路径/哈希、adapter 配置 | 跟踪 |
| `integrations/pi-brain/` | WSL 脑（`/root/albion-pi/`）的源码快照：宿主、扩展、自检 | 跟踪；私人 `.env`、SOUL、`state/` 不入库。原 `integrations/hermes/` 已于 2026-10-16 删除，见 git 历史 |
| `logs/`、`_tmp/`、`.local/` | 日志、临时草稿、私人配置和下载 | 全部忽略 |
| `her-home/`、本地模型/训练/venv 目录 | 个人作品、外部资产与环境 | 全部忽略 |

框架例外：AIRI 的 `*.test.ts`、`*.browser.test.ts` 继续与 package 源码相邻，由既有 Vitest 配置发现；
它们是测试源码，不是启动入口。搬动这类测试涉及大量相对 import 与上游 workspace 约定，
本次不改变它们的发现机制。未来独立探针必须放 `tests/`，不要散进 `src/`。

原 `pet/archive/` 是忽略的本地历史资产区，已于 2026-10-24 删除（不在 Git 内，无法从历史恢复）；
新的代码备份统一进根 `backup/code/`。
已有 `_tmp/`、`tts/_tmp/`、`shell/_tmp/` 是隔离的本地工作区，不把其全部内容当正式测试。
被正式测试调用的 `credit_repro.py` 已从临时区迁入 `tests/tts/support/`。
已关闭的历史日志已于 2026-10-24 全部迁入 `logs/history/tts-p8/`
（`prod_irodori.log`、`prod_stream.log`、`prod_bridge.log`），原 `tts/_p8_logs/` 目录已删除。

每次迁路径要做到：

1. 查入口、import、cwd、fixture、输出目录和文档引用，记录原路径及新路径。
2. 修改引用与目录深度；不要只做全局文本替换。Python 从 `__file__` 定位根；Node 的相对文件访问要核实 cwd。
3. 不移动正在运行的生产源码、模型或被占用日志。bat 保留原编码与 CRLF。
4. 从根目录和其他 cwd 验证测试发现/导入，运行分类门禁；核对生产 MD5 或语义差异。
5. 明确哪些是 Git 能重建的、哪些必须恢复本地资产。提交前检查暂存区敏感内容。

本次迁移逐文件清单为 `layout-moves-20261002.json`。其中 `local-backup`、`local-rollback`、
`private-backup`、`test-output` 是本机记录，新克隆不会包含它们；生产源码不依赖这些目录。
