# 阿尔比恩语音链路

2026-10-22：stream 启动器显式固定 `ALBION_PLAN_VERSION=4`，T1 只核实并对齐文档，未切 v6。
准备桥按源句并行翻译，全局最多 2 句；结果保持原序，空译重试一次仍失败则明确失败。
首单元取出即预取，下一单元在当前准备完成后预取并与渲染重叠；现有超时预算保持不变。
stream 运维日志带日期时区；正文只记录长度和 SHA256。
桥的 `_breq.log`、`_translate_err.log` 写入仓库根 `logs/tts/`，各按 1 MiB、3 份备份轮转；新记录不写原文/译文/上游错误正文。
旧日志及其轮转备份可能仍有历史正文，保持原位，不改写已有记录。

本目录源码由根 `D:\AI\Wife` 总仓管理；Irodori 与 GPT-SoVITS 已合仓，
不要按旧文档在子目录 checkout local/master 或另推子仓。

| 目录 | 当前职责 |
|---|---|
| `irodori/` | 后端 :9889、准备/翻译桥 :9881、情感规划/渲染与上游训练工具 |
| `stream/` | 壳使用的 unspeech 流式网关 :9885 |
| `GPT-sovits/` | 备用 Denia 源码，以及本机 STT/采集依赖的忽略 runtime |
| `persona/` | 角色素材，不等同于 WSL 生产 SOUL |

独立测试在根 `tests/tts/`，A/B 在 `tests/tts/eval/`；旧 bak/rollback
在根 `backup/code/20261002/tts/`。`bridge_v2.py`、`serve_v2.py` 仍属于生产。
现有临时区和在写日志保持忽略，见根文件分类文档。

在仓库根、环境准备好后执行：

```powershell
& .\tts\irodori\start_backend.bat
& .\tts\irodori\start_bridge.bat
& .\tts\stream\start_stream.bat
& .\tts\irodori\.venv\Scripts\python.exe -B -m unittest discover -s tests/tts -p 'test_*.py'
```

权重、语料、音频、环境、日志不入 Git。
完整关系及重建见根 `docs/PROJECT_MAP.md`、`docs/REBUILD.md`。

2026-10-16 只读核对的生产基线：TTS 在 **Windows**，WSL 的 `/root/albion-pi/`
是脑服务。9885 `/health` 报告 `plan_version=4`、`emotion_mode=v3`、
`emotion_policy=cascade`、`unit_budget=110`、`render_timeout=90`；`v3` 是情绪模式名，
不表示计划版本为 3。仓库跟踪 TTS 文件与生产目录源码忽略 CRLF 后一致。

后端 `/tts`、`/tts_stream`、`/v2/render` 共用单 GPU 队列；`serve_v2.py`、
`bridge_v2.py` 是当前接线，版本化端点与 v3/v5/v6 兼容分支继续由既有离线测试覆盖。
预热只在启动监听前执行，用同一份规划与推理参数，临时 WAV 在退出时清理。
旧合成端点使用完整船表：修正了接线只传默认 `albion`、导致其他合法音色被回落为
`albion` 的问题；未知音色仍按原契约回落默认船。

离线测试不启动生产服务、不合成真实语音、不调用翻译 API。工作树不含 checkpoint 时，
`test_emotion_shared_core.py` 的两个真实资产测试会跳过；其他测试使用固定材料或替身。
`test_codex_translation_timeout.py` 会创建一个短命本地 Python 子进程验证静默管道超时，
测试结束会终止并回收该进程。
