> 已失效（2026-10-24 归档）：2026-10-02 一次性整理交付报告；现行分类看 `docs/FILE_LAYOUT.md`、路径看 `docs/PROJECT_MAP.md`。

# 阿尔比恩项目整理交付（2026-10-02）

按本轮明确授权，先提交并推送现有生产改动：
`f59d4bbaa6de35426e9a459ac22cafa461f8f78d`。
本次整理不重打包、不重启现有服务、不修改 WSL 生产部署；生产代码主目录保持原位。

文件迁移清单 315 条：115 条测试/材料、34 条 Spine 诊断、2 条 A/B、1 条语音探针、
1 条复现支持；33 条一次性旧补丁/工具；50 条本地代码备份、45 条 rollback 文件、
32 条私密 profile 归档和 2 条测试产物。另有 1 份已关闭日志迁入 `logs/history/tts-p8/`；
2 份被进程占用的日志保留原位。没有删除旧版本。

根 AGENTS 与 archive/tests 规则、项目脉络、分类、重建、测试入口、迁移清单均已补齐。
修复了 TTS 子目录整仓忽略，退出 Git 跟踪 5 个 `.env`，磁盘原件保持不变；空值/既有示例模板继续入库。
私密归档、配置、个人数据、权重、环境、日志不入 Git。历史设计文档中的迁移路径已更新。

重建补充：8 个 WSL 定制代码/补丁/测试文件、32 个 Release 权重的目标路径及 SHA256、
13 个必需 LoRA adapter 配置、3 个基础/角色资产哈希，以及 3 份环境包版本清单。
WSL profile、SOUL、key、记忆数据库和作品没有复制进仓库。

| 自检命令或脚本 | 结果 |
|---|---|
| Irodori Python `-B -m unittest discover -s <测试目录> -p test_*.py` | 迁移前 224 项，0 失败；迁移后 224 项，0 失败 |
| 独立源码快照、实际从 GitHub 浅克隆各运行 TTS tests | 两处均 224 项，222 通过、2 跳过、0 失败；使用现有 Python 环境，跳过因无外部基础权重 |
| `python tools/check_repository.py` | 本机与独立源码快照分类错误均 0；检查 Git 根、私密/备份入库、旧目录引用 |
| `python tools/restore_weights.py` | 32/32 Release 权重本机 SHA256 一致，3/3 基础/角色资产一致，安装/覆盖数 0 |
| `verify_final.py`（工作区证据脚本） | 5142 个原文件逐一定位，无丢失；5097 个 MD5 相同，45 个为规则/文档/测试路径与输出目录变更 |
| 同上：本地备份保护 | 127/127 代码/rollback/profile 归档文件 MD5 未变 |
| 同上：生产源码保护 | 壳业务源码与根启动器未变；仅 stream_server.py 注释中测试路径改变，Python AST 相同 |
| 同上：语法检查 | Python 56/56、Node 81/81，通过；未执行归档补丁副作用 |
| 同上：WSL MD5 | 11/11 受保护部署文件未变，含 profile 配置/身份、语音插件及两个 gateway 源文件 |
| A/B 两个脚本 `--help` | 2/2 导入与参数入口通过，未请求生成语音 |
| `check_hermes_patch.py` | 从固定上游 commit 提取原文件，在隔离目录 `git apply --check`，退出码 0 |
| Gitleaks 8.30.1 Git 历史（脱敏） | 32 条原始命中，逐项为上游样例/公开 URL，按精确历史 fingerprint 登记；未发现未处理真实凭据 |
| Gitleaks 暂存区（脱敏） | 0 条未处理发现，退出码 0 |
| 完整源码快照 Gitleaks | 16 条与已审查上游 blob 逐字节相同的样例/公开 URL；只在隔离快照使用相应精确忽略后退出码 0，未扩大生产仓库忽略 |
| `pnpm typecheck` | 默认并行耗尽内存；相同 filters 串行、6GB Node heap 后报 1 个未使用变量错误，退出码 2；后续 workspace 未全部检查 |
| `pnpm lint`（不带 fix） | 11368 errors、4 warnings，退出码 1；未自动修改源码 |

串行类型错误位于 `shell/packages/stage-pages/src/pages/settings/providers/index.vue:5`，
`isCustomProvidersDisabled` 未使用；本轮没有修改该文件。lint 涉及全仓既有格式/目录规则，
没有因整理顺手修复。没有将这两项报告为通过。

未验证：整机从零安装、Release 实际重新下载、GPU 真推理、真实 STT/摄像头、
完整 UI/语音/对话端到端、重新打包 exe、WSL 补丁在新引擎中的 pytest、所有旧桌面探针。
源码快照与 GitHub 克隆使用现有 Irodori Python 环境，因此不证明全新依赖安装成功。
完整重建需要私人离线资产/profile 备份，详见 `REBUILD.md`。

原始证据与只读工具保存在本机 `D:\AI\codex\workspace\albion-organize-20261002`，
没有把私密日志、扫描匹配文本或凭据写入仓库报告。检查失败不影响这次文件分类本身的完成，
但新环境发布仍需补做上述未验证项。
