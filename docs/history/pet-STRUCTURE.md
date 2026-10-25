> 已失效（2026-10-24 归档）：运行侧目录规范已被 `docs/FILE_LAYOUT.md` 取代；现行看后者。

# 运行侧目录规范

以根 `AGENTS.md`、`docs/FILE_LAYOUT.md` 为准，这是 2026-10-02 当前分类。

| 位置 | 内容 |
|---|---|
| pet 根入口 | start_all*.bat、start_ear.bat、whisper_serve.py、check_all.py，保持原位 |
| pet/tools/eyes、pet/widget | 当前采集端与摆件 |
| pet/tools | 可重复使用的资产处理、停止壳、bat 格式校验工具 |
| pet/models | 本机模型，忽略入库 |
| 根 tests/pet | 桌面/CDP/模型诊断、STT 验收；固定材料在 data/ |
| 根 tests/pet/spine-official | 独立 npm 诊断工程，外部 sr38 不入库 |
| 根 archive/legacy/pet | 旧一次性补丁/发布脚本，不可批量执行 |
| 根 backup/code/20261002/pet | 摆件与眼睛旧备份，忽略入库 |
| pet/archive | 已有本地历史素材，忽略并保持原位 |

新测试不写源码，生成产物进根 `tests/output/pet/`；新代码副本进根 `backup/code/`。
运行入口调用的自检保持原位。2026-09-17 的旧数量及分类不再作为当前依据。
