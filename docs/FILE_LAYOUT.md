# 文件分类与迁移规则

| 位置 | 放什么 | Git 策略 |
|---|---|---|
| 根入口（`一键启动.bat` / `一键启动-dev.bat` / `一键关闭.bat` / `一键安装.bat`） | 一两行转发到 `launch/`，不放实现 | 跟踪 |
| `launch/` | 启动实现：`start_all.bat`、`stop_all.bat`、`silent_run.ps1`、`start_brain.ps1`、`close_consoles.ps1`、`hidden.vbs`、`load_env.bat`、`brain_watch.ps1`、`check_all.py`、`install_autostart.ps1` | 跟踪 |
| `config/local.env` | Windows 侧唯一环境变量文件（KEY=VALUE）；模板 `local.env.example` | 忽略，只入库模板 |
| `shell/`、`ear/`、`eyes/`、`widget/`、`tts/` | 当前生产源码、构建配置、运行入口、许可 | 跟踪源码；模型、环境、构建产物忽略 |
| `shell/` 里的上游件（2026-11-01 移除） | 上游文档站 `shell/docs/`（462 MB，Vitepress + 多语言内容）、聊天机器人 `shell/integrations/`、`services/computer-use-mcp`、插件样例 `shell/plugins/`、AI 技能库 `shell/.agents/`、`packages/font-departure-mono` | 已 `git rm -r` 并移出 pnpm workspace；历史仍在 git 里（`git log -- shell/docs`），需要时看上游仓库 |
| `tests/ear/`、`tests/tts/`、`tests/pi-brain/` | 独立测试、诊断、复现支持脚本 | 跟踪 |
| `tests/ear/data/`、TTS golden JSON | 经过审查的固定输入/预期值 | 跟踪；不放真实私密会话 |
| `tests/tts/eval/`、`tests/tts/probes/` | 需真实服务的 A/B 与语音探针 | 脚本跟踪，产物忽略 |
| `tests/output/<组件>/<运行名>/` | 截图、录音、测量结果 | 全部忽略 |
| `tools/` | 可重复使用的维护、校验、模型处理工具 | 跟踪，使用前阅读副作用 |
| `教程/` | 面向他人的搭建教程（只讲方法，不含模型、声音、人设和凭据） | 跟踪 |
| `docs/history/` | 已确认失效、只作考古的设计与上线记录 | 跟踪；不当作现行说明，引用前先看文件头的失效说明 |
| `archive/legacy/` | 旧补丁、旧发布工具、过期实验脚本 | 仅不含凭据的历史参考；2026-10-24 已清空现有内容（回看用 git 历史），今后新的一次性脚本才放这里 |
| `backup/code/<日期>/<原相对路径>` | 旧生产副本、回滚目录、手工 `.bak` | 全部忽略，保留磁盘内容 |
| `backup/profile-archives/`、`backup/` 原个人回档目录 | 私密 profile 与个人回档点 | 全部忽略，禁止强制提交 |
| `config/rebuild/` | 无凭据的版本清单、权重路径/哈希、adapter 配置 | 跟踪 |
| `brain/` | WSL 脑（`/root/albion-pi/`）的源码快照：宿主、扩展、自检（原 `integrations/` 下的 pi-brain，2026-11-01 整体改名搬到顶层）；`bin/chat_bridge.py` + `bin/inbox.py` 管 TG/Discord 与未回箱（2026-11-02，跑在 `chat-venv`） | 跟踪；私人 `.env`、SOUL、`state/` 不入库。原 `integrations/hermes/` 已于 2026-10-16 删除，见 git 历史 |
| `memory/` | OpenViking 记忆链的守护/观测/恢复脚本、`OPENVIKING_GUARD.md`、`templates/ov.conf.*` | 跟踪；`ov.conf` 本身不入库 |
| `tts/gsv/` | GPT-SoVITS 适配层（Denia）：`denia_launch.py`（按 `ALBION_GSV_DEVICE` 覆盖 device/is_half，写临时 yaml）、`tts_infer_denia.yaml`、`start_denia.bat`、README；上游代码/权重不在仓库 | 跟踪（不含上游文件与权重） |
| `tts/ref_audio/` | 各船参考音与 Denia 参考音（站点级资产） | 忽略，不入库 |
| `logs/`、`_tmp/`、`.local/` | 日志、临时草稿、私人配置和下载 | 全部忽略 |
| `her-home/`、本地模型/训练/venv 目录 | 个人作品、外部资产与环境 | 全部忽略 |

框架例外：AIRI 的 `*.test.ts`、`*.browser.test.ts` 继续与 package 源码相邻，由既有 Vitest 配置发现；
它们是测试源码，不是启动入口。搬动这类测试涉及大量相对 import 与上游 workspace 约定，
本次不改变它们的发现机制。未来独立探针必须放 `tests/`，不要散进 `src/`。

原 `pet` 目录下的 `archive/`（2026-11-02 拆目录前的旧位置）是忽略的本地历史资产区，已于 2026-10-24 删除（不在 Git 内，无法从历史恢复）；
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
