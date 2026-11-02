# ear — 阿尔比恩的耳朵（中文语音识别，:9882）

OpenAI 兼容的 `POST /v1/audio/transcriptions`，AIRI 里选 openai-audio-transcription，
BaseURL 填 `http://127.0.0.1:9882/v1`。默认引擎 `paraformer`（中文专用，跑 **CPU**，不吃显存）。

| 文件 | 是什么 |
|---|---|
| `whisper_serve.py` | 服务本体：FastAPI + uvicorn，两个引擎（paraformer / faster-whisper） |
| `start_ear.bat` | 启动器（`launch/start_all.bat` 也调它）；先 `call ..\launch\load_env.bat` |
| `pyproject.toml` / `uv.lock` | 运行环境与**版本真相**（Python 3.10 + CPU 版 torch + funasr + faster-whisper） |
| `models/modelscope/iic/` | paraformer-zh、fsmn-vad、ct-punc 三件套（约 1.1 GB，**不进库**） |

## 建环境

```powershell
uv sync --directory ear          # 等价：tools\setup\setup_stt_runtime.ps1
ear\.venv\Scripts\python.exe -c "import funasr, faster_whisper"   # 冒烟
```

`.venv` 约 1.24 GB（旧的 `tts/GPT-sovits/runtime` 是 7.46 GB，那个只留给 GSV/Denia）。
步 4 实测记录在 `_tmp/ear-env/REPORT.md`：同一句中文在新旧环境转写逐字一致，热转写 0.30s（旧 0.40s）。

改依赖：改 `pyproject.toml` → `uv lock --directory ear` → 提交新的 `uv.lock`。
`tools/setup/setup_stt_runtime.ps1` 默认用 `--locked`，锁文件对不上会直接失败，不会静默换版本。

## 环境变量

`config/local.env`（不入库，模板 `config/local.env.example`）里放，真环境变量优先：

| 变量 | 默认 | 作用 |
|---|---|---|
| `ALBION_STT_ENGINE` | `paraformer` | `paraformer`（中文 CPU）或 `whisper`（多语种，要 CUDA 就补 nvidia 轮子） |
| `ALBION_STT_MODEL` | `small` | whisper 引擎的模型路径/名 |
| `ALBION_STT_DEVICE` | `auto` | `auto` / `cpu` / `cuda` |
| `ALBION_FUNASR_ROOT` | `<本目录>\models\modelscope\iic` | 三件套所在目录 |
| `ALBION_STT_PRELOAD` | `1` | 启动时后台线程预热模型（否则第一句要等几秒） |
| `ALBION_STT_PORT` | `9882` | 监听端口（只绑 127.0.0.1） |

加载完会用 0.3 秒静音跑一次预热，消掉 fbank 后端首次初始化的一秒。

## 验收

```powershell
# 服务在跑时（用系统 Python 即可，只用标准库）
python tests\ear\check_ear.py
# 不加载模型的离线单测
python tests\ear\test_ear_offline.py -v
```

模型目录换位置、端口改动、引擎切换的对照表见 `docs/ASR路径清单.md`。
