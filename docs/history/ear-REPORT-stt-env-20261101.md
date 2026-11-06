# 耳朵独立 uv 环境实测（步骤 #4 运行时合并实测）

> 归档说明：本文原为 `_tmp/ear-env/REPORT.md`。2026-11-05 清理 `_tmp/ear-env/`（1.24 GB 环境副本，生产用 `ear/.venv`）时移到此处，正文保持原样，文中 `_tmp/ear-env/` 路径只是当时的记录。

日期：2026-11-01/02　目录：`_tmp/ear-env/`（`_tmp/` 不入库）　生产服务 9882 全程未停未重启。

## 结论

**paraformer（生产默认引擎）可以完全脱离 7.5 GB 的 `tts/GPT-sovits/runtime`**，用 Python 3.10 + CPU 版 torch 的独立 uv 环境跑，
转写文本与旧 runtime **逐字一致**，热转写还略快（0.30s vs 0.40s）；代价是加载慢 ~0.9s、首句慢 ~0.9s（见下）。
whisper 引擎在同环境 CPU 能跑；GPU 不行（缺 cublas64_12.dll，见第 5 节）。

## 1. 最终 pyproject.toml（本目录，`uv.lock` 已生成）

```toml
[project]
name = "albion-ear-env"
version = "0.1.0"
description = "Standalone CPU venv for pet/whisper_serve.py (paraformer + faster-whisper)"
requires-python = "==3.10.*"
dependencies = [
    "torch>=2.2",
    "funasr>=1.2",
    "modelscope>=1.20",
    "faster-whisper>=1.1",
    "fastapi>=0.112",
    "uvicorn>=0.30",
    "python-multipart>=0.0.9",
    "kaldi-native-fbank>=1.20",
]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = [{ index = "pytorch-cpu" }]
```

锁定版本：torch 2.14.1+cpu、numpy 2.2.6、funasr 1.4.19、modelscope 1.40.2、faster-whisper 1.2.1、
ctranslate2 4.8.4、fastapi 0.162.0、uvicorn 0.54.0、python-multipart 0.0.32、kaldi-native-fbank 1.22.5、transformers 5.20.2。

## 2. 体积 / 时间

| 项目 | 数值 |
| --- | --- |
| `.venv` 体积 | **1,236 MB / 34,124 文件**（旧 runtime 7,460 MB / 59,000 文件，约 1/6） |
| 安装的发行包 | 104 个（`importlib.metadata`），uv.lock 106 条 |
| `uv lock` | 12.2 s（107 包） |
| `uv sync` 首次 | 53 s（其中下载+构建 31 s、安装 21 s，torch CPU 轮 118 MB） |
| 硬链接复用 | **0**：缓存在 C:、`.venv` 在 D:，uv 报 "Failed to hardlink files; falling back to full copy"。放 C: 可近零开销；也可设 `UV_LINK_MODE=copy` 消警告 |
| uv 缓存 | `C:\Users\24346\AppData\Local\uv\cache`，18.9 GB |

体积大头：torch 466 MB、llvmlite 116 MB、scipy 103 MB、av.libs 62 MB、ctranslate2 61 MB。

## 3. probe 输出原文（`_tmp/ear-env/probe.py`，未起服务，进程内加载三件套）

音频：`tests/pet/data/_zh_test.wav`（300,686 B，22.05 kHz 单声道 6.8 s）。

新环境（含 torchaudio 的那一版，`pyproject.with-torchaudio.toml`）：
```
[probe] python 3.10.20
[probe] torch=2.14.1+cpu torchaudio=2.11.1+cpu numpy=2.2.6 funasr=1.4.19 modelscope=1.40.2 faster_whisper=1.2.1 ctranslate2=4.8.4
[probe] 加载耗时 2.80s
[probe] 第一次转写 0.31s -> '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。'
[probe] 第二次转写 0.30s -> '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。'
```
```
[probe] 加载耗时 3.06s / 第一次 0.29s / 第二次 0.27s   （第二次跑）
```

旧 runtime（`tts\GPT-sovits\runtime\python.exe _tmp\ear-env\probe.py`）：
```
[probe] python 3.9.13
[probe] torch=2.7.0+cu128 torchaudio=2.7.0+cu128 numpy=1.23.4 funasr=1.0.27 modelscope=1.32.0 faster_whisper=1.1.1 ctranslate2=4.6.0
[probe] 加载耗时 1.95s
[probe] 第一次转写 0.51s -> '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。'
[probe] 第二次转写 0.40s -> '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。'
```
```
[probe] 加载耗时 1.89s / 第一次 0.36s / 第二次 0.35s   （第二次跑）
```

最终配置（torchaudio 换成 kaldi-native-fbank 后，跑 3 次）：
```
[probe] RESULT text='指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=2.65s first=1.24s hot=0.30s
[probe] RESULT text='指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=2.86s first=1.30s hot=0.31s
[probe] RESULT text='指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=2.66s first=1.29s hot=0.30s
```

格式兼容（生产把上传字节写进 `.wav` 临时文件，内容可能是 mp3/webm）：
```
new  zh_mp3_aswav.wav   '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=2.95s first=0.38s hot=0.45s
old  zh_mp3_aswav.wav   '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=1.95s first=0.37s hot=0.38s
new  zh_webm_aswav.wav  '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=2.74s first=0.47s hot=0.46s
old  zh_webm_aswav.wav  '指挥官，我是阿尔比恩，今天也一起把家里的事做好吧。' load=2.06s first=0.78s hot=1.00s
```

`import whisper_serve` 在最终环境里也通过（`IMPORT OK paraformer`，路由齐全）。

## 4. 遇到的冲突和怎么解的

1. **torch / torchaudio 版本错配**：只用 `[[tool.uv.index]] pytorch-cpu + [tool.uv.sources]` 解析出
   torch 2.14.1+cpu 与 torchaudio 2.11.1+cpu（torchaudio 的依赖不是精确 pin）。两者 import 都过，但
   `torchaudio.load()` 在 2.11 已改走 TorchCodec，直接 `ImportError: TorchCodec is required`。
   耳朵链路不调 `torchaudio.load`（funasr 用 soundfile/librosa 解码），所以没炸。
   旧 runtime 是 2.7.0+cu128 一对，`torchaudio.load` 正常。
2. **funasr 1.4.19 的 fbank 后端**：删掉 torchaudio 后 `funasr.utils.fbank` 报
   `torchaudio is not installed and neither is the kaldi-native-fbank fallback backend`。
   解决：加 `kaldi-native-fbank`（1.22.5，1 MB 级），funasr 优先走 knf，torchaudio 不再需要——
   同时把第 1 条的版本错配、以及"torchaudio 必须与 torch 同版本"的坑整个绕开。
   代价：knf 首次 fbank 有一次性初始化 ~+1.0 s（first 1.24-1.30s vs 0.29-0.38s），第二句起一致（0.30s）。
   缓解办法：`_load_model` 里用 0.3 s 静音跑一次 `generate` 预热即可（本轮未改生产文件）。
3. **numpy 2 兼容**：没冲突。funasr 1.4.19 + numpy 2.2.6 + torch 2.14 直接跑通，不需要降到 numpy<2，
   也不需要降 funasr（旧 pin 1.0.27 反而不支持 py3.10/numpy2 这个组合）。
4. **`torch` 解析到 CPU 轮**：`explicit = true` 让 pytorch-cpu 索引只服务 torch，numpy 等仍走 PyPI，符合预期。

## 5. whisper 备选（faster-whisper）结论

- CPU：`WhisperModel(local HF 缓存的 faster-whisper-large-v3, device='cpu', compute_type='int8', local_files_only=True)`
  → 加载 6.9-7.2 s，6.8 s 音频转写 7.7-8.0 s，文本 `'指挥官,我是阿尔比恩。今天也一起把家里的事做好吧。'`。可用但慢。
  本地没有 `small`，未下载任何模型。
- CUDA：**不可用**。模型能建，`transcribe` 时
  `RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`——
  CPU 版 torch 不带 CUDA 运行库，ct2 4.8.4 自己也找不到 cublas（`get_cuda_device_count()` 仍报 1）。
  要在新环境里跑 GPU whisper，得再装 `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`（各几百 MB），env 会明显变大。
  结论：独立小环境适合 paraformer（CPU）；多语种 whisper 想走 GPU，就仍用旧 runtime 或另外补 CUDA 轮子。

## 6. 建议

1. 采用新 uv 环境跑 paraformer（耳朵默认引擎），**不并入 irodori venv**：irodori 是 torch 2.10+cu128 + 一整套 TTS 依赖，
   合并既不能减体积，也会把耳朵和 TTS 的升级/回滚绑死。方案 B 因此没必要做（本轮未执行）。
2. 落地位置建议放仓库内 `pet/ear-env/`（或 `tts/ear-env/`），LOCK + pyproject 入库，`.venv` 本地不入库；
   改 `pet/start_ear.bat` 第 8 行的解释器路径即可，其他启动器不动。
3. 落地时把 `_load_model` 的预热改成"加载完再跑一次 0.3 s 静音"（或 `ALBION_STT_PRELOAD=0` 时不预热），
   消掉 knf 首次 1.3 s。
4. 旧 runtime 还留给眼睛启动器与 GSV；确认耳朵切换后它不再被耳朵引用，再考虑瘦身。
5. `.venv` 放 C: 可用硬链接几乎不占空间；放 D: 就是 1.2 GB 实体拷贝（本机实测走的是拷贝）。

## 文件清单（均在 `_tmp/ear-env/`，不入库）

- `pyproject.toml` / `uv.lock` / `.venv/`（最终版）
- `pyproject.with-torchaudio.toml`：含 torchaudio 的对照版（对应第 3 节第一组数据）
- `probe.py`：paraformer 三件套进程内探针（`PROBE_LOG=<路径>` 输出 UTF-8 日志，控制台是 GBK 会糊）
- `probe_whisper.py`：faster-whisper CPU 冒烟
- `audio/`：mp3 / webm(opus) 内容伪装成 `.wav` 的格式兼容样本
- `probe-*.log`：全部原始日志
