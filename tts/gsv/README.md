# Denia 适配层（GPT-SoVITS 中文角色）

Denia 是**中文**角色：不过假名过滤、原样送中文，走独立的 GPT-SoVITS 后端 `:9880`，
再由 `tts/irodori/bridge.py` 以 `voice=denia` 调它。上游项目是
[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)；**上游代码、runtime、权重都不在本仓库**，
本目录只放「我们的适配」。

她是一种**可切换的 TTS 引擎**：`config/local.env` 的 `ALBION_TTS_ENGINE` 在 Irodori（日语特化，:9889）
和 Denia/GSV（中文，:9880）之间二选一，一键启动只拉起选中的那一个。

## 仓库里有什么

| 文件 | 作用 |
|---|---|
| `denia_launch.py` | 包住上游 `api_v2.py`：GSV 便携 runtime 的 `python39._pth` 会忽略 `PYTHONPATH`，直接跑上游脚本会 `ModuleNotFoundError: No module named 'tools'`；这里先把 GSV 根加进 `sys.path` 再 `chdir` 过去执行。同时按 `ALBION_GSV_DEVICE` 覆盖 `custom.device`/`is_half`（写临时 yaml，入库的 yaml 不动，见下） |
| `tts_infer_denia.yaml` | 推理配置（上游 `api_v2.py -c` 的格式）：`version: v2ProPlus`、`device: cpu`、`is_half: false`，权重指向 `GPT_weights_v2ProPlus/…ckpt` 与 `SoVITS_weights_v2ProPlus/…pth`。**入库的这份保持 cpu**，启动时按环境变量在临时副本里换 |
| `start_denia.bat` | 双击即起 `:9880`（无窗口起法见下） |

## GSV 安装目录

默认 `<仓库根>\tts\GPT-sovits`，整目录**忽略不入库**；换机器/换位置在 `config/local.env` 里设：

```ini
ALBION_GSV_ROOT=D:\AI\Wife\tts\GPT-sovits
```

目录里至少要有（上游安装 + 我们的权重）：

```
<ALBION_GSV_ROOT>\
├─ api_v2.py                      上游
├─ GPT_SoVITS\                    上游（含 pretrained_models\ 底模）
├─ runtime\python.exe             GSV 便携 runtime（只给 Denia 用；耳朵/眼睛不再用它）
├─ GPT_weights_v2ProPlus\Denia_1-e15.ckpt
└─ SoVITS_weights_v2ProPlus\Denia_1_e25_s1175.pth
```

参考音在 **`tts/ref_audio/output.wav_0009342720_0009558400.wav`**（6.74s，站点级资产，不入库）。
`bridge.py` 的 `DENIA_REF` 默认就指这里，换位置用 `ALBION_DENIA_REF` 覆盖。

## 起 9880

单独起（会占一个窗口；调试用）：

```bat
tts\gsv\start_denia.bat
```

无窗口起（和生产同一套）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\Wife\launch\silent_run.ps1 -Bat D:\AI\Wife\tts\gsv\start_denia.bat -Log D:\AI\Wife\logs\denia.log
```

第一次加载较慢（CPU / 显存里还要装底模），看到 9880 在监听再起桥/说话。

一键启动要不要拉它，由 `config/local.env` 的 **`ALBION_TTS_ENGINE`** 决定（和 Irodori 二选一）：

```ini
# irodori（默认）= :9889 Irodori 五船；gsv = :9880 Denia
ALBION_TTS_ENGINE=gsv
```

`launch/start_all.bat` 按它只拉起一个 TTS 后端（gsv 模式起 9880、不起 9889），桥和流式网关两种模式都起。

## 设备：GPU 还是 CPU

推理配置里 `custom.device` / `custom.is_half` 由环境变量覆盖（**入库的 yaml 保持 cpu，不改**）：

```ini
# cuda（默认，is_half 自动开）或 cpu（is_half 自动关）
ALBION_GSV_DEVICE=cuda
```

`denia_launch.py` 会按这个值生成一份临时 yaml（`%TEMP%\albion_denia_<设备>.yaml`）再交给
上游 `api_v2.py -c`；`tts_infer_denia.yaml` 本体不动。上游 `TTS_Config` 只读 `custom` 段，
所以只换 custom 的 device/is_half，其余版本段原样。

**CPU 首句约 52 秒**；换到 GPU 后首句也要等底模加载，之后每句才快。跑 GPU 时确认显存够
（和别的程序共用 8 GB 卡时容易把显存挤到共享内存，反而更慢）。

## 与桥的约定

- 桥固定打 `http://127.0.0.1:9880/tts`，参数 `text` / `text_lang=zh` / `ref_audio_path` / `prompt_text` / `prompt_lang=zh`。
- 中文原文直接送，不做假名过滤；Denia 也**不支持流式**（桥会退回整条 wav）。
- 流式网关（`tts/stream`）在 `ALBION_TTS_ENGINE=gsv` 时**不翻译、不规划**：每个切出的单元把中文原文
  交给桥 `POST /v1/audio/speech {"input": 文本, "voice": "denia"}`，拿整条 WAV 按网关原来的下发格式
  （一组 `sentence.start` → 一个 WAV 二进制帧 → `sentence.end`）送回壳，字幕仍是中文原文。壳不改。
- 端口/参考音都可用 `config/local.env` 覆盖：`ALBION_DENIA_PORT`（bat 里用）、`ALBION_DENIA_REF`（桥里用）。

## 上游文件为什么不在这里

`api.py`、`api_v2.py`、`webui.py`、`batch_inference.py`、`config.py`、`install.ps1/sh`、
`go-webui.*`、`requirements.txt`、`extra-req.txt`、上游 `README.md`/`LICENSE` 都是**原封不动的上游文件**，
已于 2026-11-01 从跟踪里移出（本地文件保留、目录整体忽略）。要升级就按上游仓库重装一份，
本目录的三个适配文件不需要跟着改（`tts_infer_denia.yaml` 只在权重/底模路径变化时才要动）。
