# 声音数据准备 & 换角色清单

## 1. 数据整理：`build_dataset.py`
音频目录 + 台词表（csv/json：`file,text[,start,end]`）→ HF `audiofolder` 数据集（`train/*.wav` + `metadata.csv`）。
只用标准库；非 wav 走 PATH 里的 ffmpeg。自检：`python -I tools/voice_data/build_dataset.py --selftest`（产物在 `tests/output/voice_data_selftest/`）。
`tools/relabel_albion.py` 是作者私用的重标注，不是本脚本的前置。

## 2. 翻译桥通用线路
`TTS_TRANSLATE_BACKEND=api` + `TTS_TRANSLATE_API_STYLE=chat`，`TTS_TRANSLATE_BASE/MODEL/KEY` 指向任意 OpenAI 兼容服务（打 `/chat/completions`）。
不设 STYLE 时行为与以前一字不差（Responses API）。自检：`python -I tests/tts/test_translate_api_chat.py`。
注意：api 线路的首腿仍会先问脑（Magpie / Command Code）；纯外部线路请再设 `TTS_BRAIN_ROUTE=0`。

## 3. 换角色
设环境变量 `TTS_DEFAULT_VOICE=<新名>`（默认 albion）：`bridge.py`、`irodori_serve.py`、`render_queue.py` 三处默认音色随之变化，
桥的 `VOICES` 自动收录该名；在 `tts/irodori/ships.json` 加同名一条（lora / inv）即可，热载不用重启。

### 仍须手改的硬编码（本次不在授权范围或属私有资产）
- `tts/irodori/bridge.py` `VOICES` 元组里的其余船名；`irodori_serve.py` `SHIPS` / `LORA_NAME` / `INV_NAME` 的代码内默认（新角色走 ships.json 覆盖即可）。
- `tts/irodori/render_queue.py` `MALE_GUARD_SHIPS = ("albion",)`：新角色是否要查男声需手动决定。
- `tts/irodori/bridge.py` `TRANSLATE_PROMPT` / `EMOTION_PROMPT`：台词风格写死「アルビオン」，换角色必须重写。
- `brain/accept.sh:83` 的 `"id": "albion"`（brain/ 的 /v1/models 模型 id；换个模型 id 时两边一起改）；pi-brain 侧模型名 `albion` 同理。
- `tools/sync-shell-private-assets.ps1` 里的 `albion.zip` / `albion-spine-38.zip` / `albion-old-aerbien3.zip`，以及 `shell/` 里引用这些 zip 的代码（shell 不在本次范围）。
- `tests/tts/**`、`tests/pi-brain/**` 探针里大量 `voice="albion"` / `"model": "albion"`（多数有 `--voice` 参数，其余需手改）。
- `tts/stream/stream_server.py:1279` 的模型 id `albion-stream`；`tts/ref_audio/albion` 音频目录、`data_lora/albion_*` 数据路径、`tools/relabel_albion.py`。
