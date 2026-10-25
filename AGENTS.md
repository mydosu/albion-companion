# 阿尔比恩长期迭代规则

先读 `docs/PROJECT_MAP.md`、`docs/FILE_LAYOUT.md`；重建机器读 `docs/REBUILD.md`。
这是公开仓库 `mydosu/albion-companion`，唯一 Git 根是本目录。私人配置（`.env.local`、token、人设）只留本机；自训权重在作者的私有仓库 `mydosu/Albion-Companion-Wife` 的 Releases。
`shell/`、`pet/`、`tts/` 不是独立项目仓库。子目录已有 AGENTS.md 仍适用。

- 执行任务时按同席给的任务目标和项目长期授权放手做：为达成目标需要改的都可以改，顺手发现的问题直接修；但不要借整理重构无关业务、换模型、改身份或同步 Windows 与 WSL skills。
- 生产：`shell/apps/`、`shell/packages/`、`pet/whisper_serve.py`、`pet/tools/eyes/`、`pet/widget/`、`tts/irodori/`、`tts/stream/`。根启动入口保留原位。
- 独立测试与探针：`tests/<组件>/`；固定材料放其 `data/` 或随测试文件的已登记 golden JSON；生成结果只写 `tests/output/`。
- AIRI 已有 `*.test.ts` / `*.browser.test.ts` 继续由所在 package 的 Vitest 管理；这是框架单元测试的明确例外。新增独立脚本不得放进 `shell/*/src`。
- 可重复使用的维护工具放 `tools/` 或 `pet/tools/`；一次性改码脚本放 `archive/legacy/`，不可直接批量执行归档脚本。
- 旧生产副本放 `backup/code/<日期>/<原相对路径>`，不入 Git；私密 profile 归档放 `backup/profile-archives/`。回滚源码优先用明确 Git commit，在独立 worktree 验证。
- 日志放 `logs/`，临时草稿放 `_tmp/`；现有仍在写的日志路径等进程自然停止后再迁，不能为了整理杀进程。个人数据 `her-home/`、模型/环境/训练数据保持本地。
- 修改路径前搜索引用，包括文档、启动器、Python 导入、Node import、fixture 路径与输出目录；同一变更更新引用和 `docs/layout-moves-20261002.json` 的后续迁移记录。保留 bat 原编码与 CRLF。
- WSL 生产位置及仓库快照的边界见 `integrations/pi-brain/README.md`。修改应落在仓库源码/补丁并验证，再按任务授权部署；禁止把 profile 全目录复制进仓库。
- `.env`、API key、token、私钥、聊天数据库、个人配置、日志与私密报告不入 Git，也不输出到工具结果或文档。示例只填空值；不要读取 `D:\AI\agent-keys.json` 并打印。
- 提交前用 `python tools/check_repository.py` 检查分类/旧路径，再用 Gitleaks 扫描暂存区。`git add` 指定路径；禁止 `git add -f` 私密/备份目录。发现真实凭据先停止推送并修复，不通过宽泛忽略规则绕过扫描。
- 审计任务只读：开始记录目标 MD5，结束核对。收尾任务按任务目标做（包括提交、推送）。实现任务记录基线、验证命令、通过/失败数字和未验证项。
- push 按用户本轮要求或同席项目长期授权进行；禁止 force push 或擅自重写历史。
