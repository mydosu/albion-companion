# Windows 复现脚本

补 `教程/00-复现向导.md` 阶段 6 和阶段 4 里原来标「手动建环境」的两步。两个脚本都只建环境，不启动、不重启任何服务。

## 耳朵/眼睛的 Python 3.9（阶段 6）

`tts/GPT-sovits/runtime/python.exe` 是耳朵 `pet/whisper_serve.py` 和眼睛启动器共用的解释器（参考部署 Python 3.9.13）。重建：

```powershell
# 先看要做什么，不下载、不写文件
powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1 -DryRun

# 建到默认位置 tts/GPT-sovits/runtime
powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1

# 已存在时默认不覆盖；要重建才加 -Force（原目录改名成 runtime.bak-<时间>，可改回）
powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1 -Force
```

- 默认下载官方 `python-3.9.13-embed-amd64.zip` 解到目标目录，启用 `import site`，用 `get-pip.py` 装 pip。
- `-Source venv` 用本机已有的 3.9（`py -3.9` 或 `uv python find 3.9`）建 venv；`-Source auto` 有 3.9 就用 venv，没有就用 embeddable。
- 默认只装耳朵实际 import 的最小集（`funasr`、`faster-whisper`、`ctranslate2`、`fastapi`、`uvicorn`、`python-multipart`，以及 funasr 运行所需的 `torch`/`torchaudio`/`numpy`），版本取 `config/rebuild/stt-environment.json`。眼睛只用标准库和 .NET，不需要额外包。
- 某个包没有对应平台的 wheel 时只记录、继续装后面的包；最后统一列出失败项，并在核心包或 import 验证失败时返回非零。
- `-Full` 再按同一份快照逐包重放全部 252 项（`--no-deps`，失败项同样只记录不中断）。
- torch 默认从 `https://download.pytorch.org/whl/cu128` 取；离线或换源用 `-TorchWheel <本地 whl>` / `-TorchIndex <url>` / `-PipIndex <镜像>`。
- 重建前先停耳朵和采集端（它们正用这个 `python.exe`）；启动仍按 `教程/00-复现向导.md` 用 `_silent_run.ps1`，不要在本脚本里拉起服务。

## TTS 环境（阶段 4）

`tts/irodori/.venv` 用 uv 建：

```powershell
# 只打印将执行的命令
powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_tts_env.ps1 -DryRun

powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_tts_env.ps1
```

- 先检查 `uv` 在 PATH、驱动可见（`nvidia-smi`）；不是 RTX 50 系会打印提示：`uv.lock` 和 `--extra cu128` 是照参考部署的 50 系锁的，换卡先确认 wheel 的 CUDA 版本与驱动匹配，不要直接改生产里的 `uv.lock`。
- 实际执行 `uv sync --frozen --extra cu128 --python 3.10`，再用 `uv pip install --python .venv\Scripts\python.exe` 装服务层依赖（uv 建的 venv 默认没有 pip），版本取 `config/rebuild/tts-environment.json`。
- `-SkipServiceDeps` 只做 `uv sync`；`-Extra`、`-Python` 可覆盖默认值。
- 建完环境后按 `教程/00-复现向导.md` 阶段 4 用 `_silent_run.ps1` 起后端和桥；本脚本不会重启正在跑的服务。

## 相关的路径参数化

这几处原来写死作者机器的值，现在都可以用环境变量覆盖，默认值不变：

| 文件 | 变量 | 默认 |
|---|---|---|
| `pet/widget/AlbionStatus.vbs` | `ALBION_PYTHONW` | 先找 PATH 里的 `pythonw.exe`/`pyw.exe`/`py.exe`，再退回 `%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe` |
| `pet/widget/albion_status_widget.pyw` | `ALBION_WSL_DISTRO` | `Ubuntu`（拼成 `\\wsl.localhost\<发行版>\root\albion-pi\state\widget.json`） |
| 同上 | `ALBION_WIDGET_SNAPSHOT` | 整个快照路径，优先于 `ALBION_WSL_DISTRO` |
| `pet/tools/eyes/capture_agent.py` | `ALBION_FFMPEG` | `D:/ffmpeg-2026-03-22-git-9c63742425-full_build/bin/ffmpeg.exe`（不存在时用 PATH 里的 `ffmpeg`） |
| 同上 | `ALBION_CAM` | `FHD Camera` |
| 同上 | `ALBION_CAM_ROTATE` | `90`（只接受 0/90/180/270） |

挂件的 Python 依赖见 `pet/widget/requirements.txt`：Pillow 与参考部署一致（12.3.0）；numpy 本机装的是 1.26.4，但它在 Python 3.13 上连 import 都会失败，所以清单里按解释器分叉——3.13 用 2.5.4（已实测 `--check` 通过），旧解释器仍锁 1.26.4。
