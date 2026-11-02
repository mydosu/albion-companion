# Windows 复现脚本

补 `教程/00-复现向导.md` 阶段 6 和阶段 4 里原来标「手动建环境」的两步，外加一个把它们（和 WSL 脑）串起来的总入口。所有脚本都只准备环境，不启动、不重启任何服务。

## 一键安装（先 `-DryRun` 演练）

`tools/setup/install_all.ps1` 按阶段跑；根目录 `一键安装.bat` 是薄壳，只把参数原样转给这个 ps1（GBK + CRLF，和 `一键启动.bat` 一致）。生产机上也请先 `-DryRun`，确认要动哪些路径再真跑。

| 阶段 | 做什么 | 已装好时 |
|---|---|---|
| `preflight` | 查 git、node/pnpm、uv、nvidia-smi、wsl 和 WSL 发行版；缺的给官方下载地址。uv 是 TTS 阶段的硬前提，WSL/发行版是脑阶段的硬前提 | 照常检查，不装系统软件 |
| `stt` | 调 `setup_stt_runtime.ps1` 建 `tts/GPT-sovits/runtime` | 打印 `[skip]` |
| `tts` | 调 `setup_tts_env.ps1` 建 `tts/irodori/.venv` | venv 依赖齐全时 `[skip]` |
| `brain` | 在 WSL 里跑 `integrations/pi-brain/install.sh` 和 `deploy.sh` | 两个脚本自身幂等，会打印 `[skip]`/`未变` |
| `templates` | `config/local.env.example` -> `config/local.env`；脑模板由 `deploy.sh` 在目标缺失时放 | 目标已存在就 `[skip]`，不覆盖 |

```powershell
# 只演练：不下载、不写仓库文件、不执行 WSL 安装
.\一键安装.bat -DryRun

# 只跑部分阶段（逗号分隔；带空格的列表整体加引号；阶段名：preflight, stt, tts, brain, templates）
.\一键安装.bat -DryRun -Only preflight,stt,tts
.\一键安装.bat -DryRun -Skip brain

# 真装（install.sh 要 root；默认 WSL 用户不是 root 时加 -WslUser root）
.\一键安装.bat -WslUser root
```

- `-DryRun` 会把有 `-DryRun` 的子脚本真的以 `-DryRun` 调起，方便看它们的计划；`deploy.sh` 即使 `DRY_RUN=1` 也会 `mkdir` 目标目录，所以总入口在 `-DryRun` 下只打印脑的命令、不执行。
- `-Only` / `-Skip` 同时给时，先取 `Only` 再减 `Skip`；`preflight` 也是可选阶段，用 `-Only` 时想检查就把它列进去。
- `-Distro`（默认 `Ubuntu`）、`-WslUser`、`-BrainHome`（默认 `/root/albion-pi`）按需覆盖。
- 脑阶段用 `/mnt/<盘>/...` 路径调 WSL 里的脚本。如果 WSL 的 `/etc/wsl.conf` 关了 `automount`（`/mnt/c`、`/mnt/d` 不是 Windows 盘），脚本会先警告/报错；先从 Windows 路径能访问的位置跑，或打开自动挂载。
- 任一阶段失败会打印 `[FAIL] 阶段 <名字>：<原因>` 并停住，不继续后面的阶段。
- 脚本不装 Git/Node/WSL/显卡驱动，不构建桌面壳，也不拉起服务。收尾会打印还要手动做的事：壳看 `docs/SHELL_FIRST_RUN.md`，OpenViking 看 `config/templates/ov.conf.example`，视觉机看 `deploy/vision/README.md`，权重看 `docs/REBUILD.md`。
- `config/local.env` 是 Windows 侧唯一环境变量文件（不入库）：`launch/load_env.bat` 会在每个服务启动 bat 开头加载它，真环境变量优先；改完重启对应服务即可（细节见 `docs/REBUILD.md`）。
- `一键安装.bat` 不带参数时就是真装；只想演练务必带 `-DryRun`。

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
- 重建前先停耳朵和采集端（它们正用这个 `python.exe`）；启动仍按 `教程/00-复现向导.md` 用 `launch/silent_run.ps1`，不要在本脚本里拉起服务。

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
- 建完环境后按 `教程/00-复现向导.md` 阶段 4 用 `launch/silent_run.ps1` 起后端和桥；本脚本不会重启正在跑的服务。

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
