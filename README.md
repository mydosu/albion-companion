# albion-companion

阿尔比恩桌宠 —— 桌面壳、语音链路、运行侧脚本与文档，合并到一个仓库。

想自己搭一个类似的 AI 陪伴，先看 [教程](教程/README.md)：讲一些方法和踩过的坑。
仓库里没有角色模型、配音、自训权重和人设；本机地址、Key 写在不进 Git 的配置里，外壳的见 `shell/apps/stage-tamagotchi/.env.example`。

维护入口：[项目脉络](docs/PROJECT_MAP.md) · [文件分类](docs/FILE_LAYOUT.md) ·
[从仓库重建](docs/REBUILD.md) · [使用说明](使用说明.md) · [测试命令](tests/README.md) · [agent 规则](AGENTS.md)。
私人 profile、记忆和外部模型不随 Git 恢复，重建前先读资产清单与私人备份要求。

## 结构

| 目录 | 内容 |
|---|---|
| `shell/` | AIRI 桌面壳（本地 fork）：Spine 3.8 与 Live2D 两条展示链路、托盘与布局持久化 |
| `pet/` | 运行侧：眼睛采集端、耳朵 STT、模型与工具脚本 |
| `tts/irodori/` | Irodori-TTS（上游 fork，分支 `local`）：语音后端与桥 |
| `tts/GPT-sovits/` | GPT-SoVITS：另一音色的后端（保留） |
| `tts/stream/` | 流式切句服务 |
| `docs/` | 设计记录：TTS 全流程、ASR 路径清单、打包与语音服务；已失效的旧设计与纪要在 `docs/history/` |
| `tests/` | 独立测试、探针、固定材料；产物在忽略的 `tests/output/` |
| `tools/` | 仓库检查与权重恢复工具 |
| `config/rebuild/` | 无凭据的环境版本、权重路径/哈希及 LoRA 配置 |
| `integrations/pi-brain/` | WSL 里她的脑（Pi 版，生产在 `/root/albion-pi/`）的源码快照与自检 |
| `archive/` | 历史存档规则；旧一次性脚本已删除（回看用 git 历史），不再作为入口或参考实现 |
| `backup/` | 本机回档与私密归档，不入 Git |

根目录脚本：`一键启动.bat` 拉起全部服务并自检，`一键关闭.bat` 收工；
`一键启动-构建版.bat` 走打包版壳。

## 起法

```
双击 一键启动.bat
```

服务默认全部关闭，需要时手动拉起；各服务的端口与自检项见 `docs/`。

## 权重

本仓自己训练的产物（多音色 LoRA 适配器 + 说话人嵌入，共 32 个文件、约 1.28 GB）
不公开，在我私有仓库的 Releases（`weights-v1`），`tools/restore_weights.py` 按 `config/rebuild/assets.json` 校验恢复。

第三方基础模型、角色模型资产**不在本仓**，请从各自来源获取。

## 许可

`shell/` 与 `tts/irodori/` 各自继承上游项目的历史与许可，见对应目录内的 LICENSE；
本仓其余部分为本项目自有的运行脚本与文档。
