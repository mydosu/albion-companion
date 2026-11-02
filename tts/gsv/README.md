# Denia 适配层（GPT-SoVITS 中文角色）

Denia 是**中文**角色：不过假名过滤、原样送中文，走独立的 GPT-SoVITS 后端 `:9880`，
再由 `tts/irodori/bridge.py` 以 `voice=denia` 调它。上游项目是
[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)；**上游代码、runtime、权重都不在本仓库**，
本目录只放「我们的适配」。

## 仓库里有什么

| 文件 | 作用 |
|---|---|
| `denia_launch.py` | 包住上游 `api_v2.py`：GSV 便携 runtime 的 `python39._pth` 会忽略 `PYTHONPATH`，直接跑上游脚本会 `ModuleNotFoundError: No module named 'tools'`；这里先把 GSV 根加进 `sys.path` 再 `chdir` 过去执行 |
| `tts_infer_denia.yaml` | 推理配置（上游 `api_v2.py -c` 的格式）：`version: v2ProPlus`、`device: cpu`、`is_half: false`，权重指向 `GPT_weights_v2ProPlus/…ckpt` 与 `SoVITS_weights_v2ProPlus/…pth` |
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

第一次加载较慢（CPU），看到 9880 在监听再起桥/说话（Denia 是可选音色，默认不随一键启动拉起，调试时按上述方式手动启动）。

## 与桥的约定

- 桥固定打 `http://127.0.0.1:9880/tts`，参数 `text` / `text_lang=zh` / `ref_audio_path` / `prompt_text` / `prompt_lang=zh`。
- 中文原文直接送，不做假名过滤；Denia 也**不支持流式**（桥会退回整条 wav）。
- 端口/参考音都可用 `config/local.env` 覆盖：`ALBION_DENIA_PORT`（bat 里用）、`ALBION_DENIA_REF`（桥里用）。

## 上游文件为什么不在这里

`api.py`、`api_v2.py`、`webui.py`、`batch_inference.py`、`config.py`、`install.ps1/sh`、
`go-webui.*`、`requirements.txt`、`extra-req.txt`、上游 `README.md`/`LICENSE` 都是**原封不动的上游文件**，
已于 2026-11-01 从跟踪里移出（本地文件保留、目录整体忽略）。要升级就按上游仓库重装一份，
本目录的三个适配文件不需要跟着改（`tts_infer_denia.yaml` 只在权重/底模路径变化时才要动）。
