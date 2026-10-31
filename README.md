# albion-companion

阿尔比恩桌宠 —— 桌面壳、语音链路、运行侧脚本与文档，合并到一个仓库。

想自己搭一个类似的 AI 陪伴：

- **人看**：[教程](教程/README.md)，讲方法和踩过的坑，按复现顺序编号，可以只看卡住的那篇。
- **让 agent 复现**：让你的 coding agent 读 [`教程/00-复现向导.md`](教程/00-复现向导.md)，它按阶段一步步做、每阶段有验收命令，遇到必须由你提供的东西会停下问你。

仓库里没有角色模型、配音、自训权重和人设；本机地址、Key 写在不进 Git 的配置里，外壳的见 `shell/apps/stage-tamagotchi/.env.example`。

维护入口：[项目脉络](docs/PROJECT_MAP.md) · [文件分类](docs/FILE_LAYOUT.md) ·
[从仓库重建](docs/REBUILD.md) · [使用说明](使用说明.md) · [测试命令](tests/README.md) · [agent 规则](AGENTS.md)。
私人 profile、记忆和外部模型不随 Git 恢复，重建前先读资产清单与私人备份要求。

## 我的硬件配置

阿尔比恩的服务几乎全在一台笔记本上跑；另有一台日常用的台式机只负责「让她看我的屏幕」，一台带老显卡的机器只负责摄像头那一路的视觉模型。以下是 2026-10-31 的实测。

| | 本机（阿尔比恩住的笔记本） | 主力机（我日常用的台式机） | 视觉机（局域网里另一台） |
|---|---|---|---|
| CPU | Intel Core Ultra 5 225H，14 核 14 线程 | AMD Ryzen 7 9700X，8 核 16 线程 | Intel i5-9400F |
| 内存 | 32 GB | 32 GB | 16 GB |
| 显卡 | NVIDIA RTX 5060 Laptop 8 GB（驱动 610.88，CUDA 13.3）+ Intel Arc 130T 核显 | NVIDIA RTX 5070 12 GB | NVIDIA GTX 1660 Ti 6 GB |
| 存储 | 1 TB NVMe | — | — |
| 系统 | Windows 11 家庭版 26200 + WSL2 Ubuntu | Windows 11 专业版 | 跑 Docker |
| 屏幕 | 3072×1920 | 2560×1440 | — |
| 外设 | USB 摄像头「FHD Camera」、麦克风 Razer Kraken V3 | — | — |
| 角色 | 跑几乎全部服务，也是声音训练的机器 | 只跑采集端，不跑计算 | Docker 里 llama.cpp 跑 Qwen3-VL 4B |

WSL2 的配置：`networkingMode=mirrored`，无 CPU/内存上限，WSL 内可用约 15.4 GB。声音训练也在这块 8 GB 的笔记本显卡上做（LoRA 训练整卡峰值 5.4–6.3 GB，训练前要先关 TTS 后端）；摄像头那一路的本地视觉模型，6 GB 显存的老卡就够。更详细的最低配置建议（含推断部分）见复现向导的「硬件要求」。

## 哪台机器跑什么

| 机器 | 服务 | 端口 | 占用 |
|---|---|---|---|
| 本机 | 桌面壳 `airi.exe`（CDP 调试口） | 9335 | GPU 约 331 MB |
| 本机（WSL） | 脑：Pi 宿主 | 8650 | CPU |
| 本机 | Irodori TTS 后端 | 9889 | GPU 约 2.35 GB |
| 本机 | TTS 桥（翻译 + OpenAI 兼容语音接口） | 9881 | CPU |
| 本机 | 流式 TTS 网关 | 9885 | CPU |
| 本机 | 语音识别 `whisper_serve`（paraformer） | 9882 | CPU |
| 本机 | OpenViking 记忆服务 | 1933 | CPU，约 273 MB 内存 |
| 本机 | 采集端（只绑 127.0.0.1） | 8790 | CPU |
| 本机 | 状态挂件 | 47654（单实例锁） | CPU |
| 本机（WSL） | 脑的模型出网代理：grok 号池、Magpie、Command Code 代理 | 8000、3425、8798 | CPU |
| 主力机 | 采集端（绑 Tailscale 和局域网） | 8790 | 不跑计算 |
| 视觉机 | llama.cpp + Qwen3-VL 4B（只给摄像头那一路） | 8080 | GPU |

本机显存日常总占用约 3.8 / 8 GB（含 OBS 等非项目进程）。

外部依赖：脑的模型通过本地代理出网（类型是 Magpie、Command Code、grok 号池），TTS 翻译跟着脑的模型走、退路是 Command Code；跨机取图和视觉机走局域网优先、Tailscale 备用。选模型的话，有条件建议用 Gemini（官方 API、Pro 套餐反代、中转站都行），文本能力数一数二，见[教程 03](教程/03-口癖和人设.md)。

## 结构

| 目录 | 内容 |
|---|---|
| `shell/` | AIRI 桌面壳（本地 fork）：Spine 3.8 与 Live2D 两条展示链路、托盘与布局持久化 |
| `pet/` | 运行侧：眼睛采集端、耳朵 STT、状态挂件、模型与工具脚本 |
| `tts/irodori/` | Irodori-TTS（上游 fork，分支 `local`）：语音后端与桥 |
| `tts/GPT-sovits/` | GPT-SoVITS：另一音色的后端（保留） |
| `tts/stream/` | 流式切句服务 |
| `教程/` | 面向他人的搭建教程：踩坑笔记 01–10，加给 agent 读的复现向导 `00-复现向导.md` |
| `docs/` | 设计记录：TTS 全流程、ASR 路径清单、打包与语音服务；已失效的旧设计与纪要在 `docs/history/` |
| `tests/` | 独立测试、探针、固定材料；产物在忽略的 `tests/output/` |
| `tools/` | 仓库检查、权重恢复、WSL 脑看门狗与同步核对、OpenViking 守护等维护工具 |
| `config/rebuild/` | 无凭据的环境版本、权重路径/哈希及 LoRA 配置 |
| `integrations/pi-brain/` | WSL 里她的脑（Pi 版，生产在 `/root/albion-pi/`）的源码快照与自检 |
| `archive/` | 历史存档规则；旧一次性脚本已删除（回看用 git 历史），不再作为入口或参考实现 |
| `backup/` | 本机回档与私密归档，不入 Git |

根目录脚本：`一键启动-构建版.bat`（日常用，打包版壳）、`一键启动-dev.bat`（改壳代码用）、
`一键启动.bat`（默认 dev，前台显示各服务控制台）都转到 `start_all.bat`，拉起全部服务并自检；`一键关闭.bat` 收工。

## 起法

```
双击 一键启动-构建版.bat
```

已经在跑的服务按端口判断自动跳过；各服务的端口与自检项见 [使用说明](使用说明.md)。
从零搭一套见 [复现向导](教程/00-复现向导.md)。

## 权重

本仓自己训练的产物（多音色 LoRA 适配器 + 说话人嵌入，共 33 个文件、约 1.48 GB）
不公开，在我私有仓库的 Releases（`weights-v1`），`tools/restore_weights.py` 按 `config/rebuild/assets.json` 校验恢复。

第三方基础模型、角色模型资产**不在本仓**，请从各自来源获取。

## 许可

`shell/` 与 `tts/irodori/` 各自继承上游项目的历史与许可，见对应目录内的 LICENSE；
本仓其余部分为本项目自有的运行脚本与文档。
