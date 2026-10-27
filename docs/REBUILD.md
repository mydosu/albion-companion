# 从仓库重建阿尔比恩

仓库恢复源码、测试及不含凭据的定制代码；自训权重只在作者的私有仓库 `mydosu/Albion-Companion-Wife` 的 Releases，别人需要自己训练（见 `教程/`）。
基础模型、角色模型、参考音频、环境和她的私人数据需要另外准备。
文件与 SHA256 在 `config/rebuild/assets.json`，实际包版本在同目录 environment JSON。
本次核实当前机器、源码、权重哈希及测试，没有抹掉环境做整机重装。

## 克隆与选择版本

新机器需要 Git、GitHub CLI，使用本机已授权登录；不要把 token 写进命令或 URL。
已有生产目录时先另选验证目录，不覆盖生产。

```powershell
gh repo clone mydosu/albion-companion D:\AI\Wife
Set-Location D:\AI\Wife
git log -5 --oneline
# 复现交付时：git checkout <目标提交SHA>
python tools/check_repository.py
```

当前生产仍有 `D:\AI\Wife`、`%USERPROFILE%` 和 WSL `/root/` 的绝对路径。
本机重建按这些路径部署；若换根目录，搜索各路径的斜杠/反斜杠拼写，逐一配置实际入口并验证。
不要把迁移测试目录等同于实现了任意目录安装。

## 桌面壳

本机 Node `22.23.2`、pnpm `10.32.1`；`shell/package.json` 声明 `pnpm@11.24.0`。
此差异已登记，未修改包管理器与 lockfile。干净环境以仓库声明和锁文件为准；
若声明版本不可用，先在隔离目录核实兼容性，不能盲目重写锁文件。
先恢复下一节的角色资产，然后执行：

```powershell
Set-Location D:\AI\Wife\shell
pnpm install --frozen-lockfile
pnpm -F @proj-airi/stage-tamagotchi build:unpack
```

产物预期在 `shell/apps/stage-tamagotchi/dist/win-unpacked/airi.exe`。
`node_modules`、`out`、`dist` 不在 Git；克隆后重新安装/构建。
本次没有重打包或替换正在运行的 exe。

## 权重与 Windows 环境

Release 附件名与本地路径不同；工具按清单恢复 33 个权重并核验 SHA256。
LoRA 到 `outputs/<训练名>/checkpoint_<步骤>/adapter_model.safetensors`；
speaker 到 `outputs/<训练名>/checkpoint_<步骤>.speaker.safetensors`。
必需的 `adapter_config.json` 已保存到 `config/rebuild/adapters/`，安装时一起恢复。

```powershell
Set-Location D:\AI\Wife
python tools/restore_weights.py --download --install
python tools/restore_weights.py
```

下载缓存在忽略的 `.local/downloads/weights-v1/`。只安装缺失文件，拒绝覆盖不同版本。
仅自训权重成功不代表全部资产已齐，遇到基础/角色资产缺失仍返回非零。
以下从你的合法私人离线备份或对应来源恢复：

| 位置 | 恢复内容 |
|---|---|
| `tts/irodori/checkpoints/model.safetensors` | 当前基础 checkpoint，3,064,295,596 字节；核对清单 SHA256，不能直接用 README 最新模型替换 |
| `shell/packages/stage-ui/src/assets/live2d/models/albion.zip` | 本机角色 Live2D 包 |
| `shell/packages/stage-ui/src/assets/spine/models/albion-spine-38.zip` | 本机角色 Spine 包 |
| `pet/models/modelscope/iic/` | paraformer ASR、fsmn VAD、ct-punc，目录名由 `pet/whisper_serve.py` 指定 |
| `tts/GPT-sovits/audio/<角色>/` | 参考音频；后端优先 `004.wav`、`068.wav`、`072.wav`、`003.wav`，缺时取已有 WAV |
| `tts/GPT-sovits/runtime/` | STT/眼睛启动器目前依赖此 `python.exe`；本机 Python 3.9.13 |
| `tts/irodori/.venv/` | 本机 Python 3.10.20，torch 2.10.0+cu128；恢复环境或重新安装 |
| Hugging Face 缓存 | checkpoint 所需 codec/tokenizer，可按上游源码下载或恢复离线缓存 |

Irodori 提供 `pyproject.toml` 与 `uv.lock`。匹配 CUDA 环境示例：

```powershell
Set-Location D:\AI\Wife\tts\irodori
uv sync --frozen --extra cu128 --python 3.10
# 服务层额外依赖，版本参照本机清单
& .\.venv\Scripts\python.exe -m pip install fastapi==0.129.2 uvicorn==0.41.0 httpx websockets==16.1.1
```

命令未在新机器执行，平台 wheel、驱动与网络还需验证。
environment JSON 是实际安装状态快照，不保证跨平台按全表 pip 安装成功。
STT 当前默认 CPU paraformer，需要 `funasr==1.0.27` 等对应依赖与三套模型。
备用 GPT-SoVITS 的完整第三方目录/权重没有随 Git 保存；使用 Denia 时再按其安装文档恢复。

## WSL 脑与私人配置

**2026-10-14 更正：WSL 上现在跑的是 Pi 版脑**（`/root/albion-pi/`，宿主 :8650），2026-10-07 起就是它；
Hermes 那份已从 WSL 删除。重建脑照 `integrations/pi-brain/README.md`：把仓库 `integrations/pi-brain/`
放成 `/root/albion-pi/`，恢复私人的 `agent/.env`、`agent/SOUL.md`、`agent/models.json` 里的号池 key、
`state/`（活动库与心事本），再 `bash start.sh`；验收 `bash accept.sh`。
号池（grok2api `:8000`，画图/联网搜/看图转述都靠它）在 WSL `/root/grok-pool/` 用 `docker compose` 跑，开机由 `/etc/wsl.conf` 的 `[boot]` 调 `/usr/local/bin/grok-pool-boot.sh` → `grok-pool-up.sh` 拉起；这两个脚本只在 WSL、不入库。
`[boot]` 环境没有 PATH，`grok-pool-up.sh` 开头必须 `export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`，
否则 dockerd 报 `userland-proxy-path is not set` 起不来（2026-10-24 踩过）。
SQLite 从停止后的完整备份或 backup API 快照恢复，不能只复制正在写的主 db。
没有私人备份时只能从空 `state/` 起，源码仓不能恢复过去的身份/会话/记忆。
WSL 里她的技能与 Windows 主 Hermes 的技能树独立，不导入 Windows 通用技能树。
非敏感模型快照在 `config/rebuild/runtime.json`，不会自动覆盖 `agent/`；部署时核实实际配置。

**Hermes 退路已删除（2026-10-16）**：`integrations/hermes/`（gateway 补丁与 `albion-voice` 插件）、
`tests/hermes/`、`config/rebuild/hermes-environment.json`、根目录 `_启动脑WSL.ps1` 都从仓库撤掉了。
真要回到 Hermes，从 git 历史取：`git show 5f755e83:integrations/hermes/README.md`（引擎基线
`NousResearch/hermes-agent@afc3b7c6f397c6d21fd2c129ca17b18b544dd8dc`，profile `/root/.hermes/profiles/albion`），
私人 profile 在 WSL `/root/albion-pi/archive/hermes-profile-20261007/`。

Windows 翻译桥默认 `TTS_TRANSLATE_BACKEND=codex`；这是历史分支名，普通与情绪翻译主路均为 Command Code，
不是直接调用 Codex CLI，也不要求 `TTS_TRANSLATE_KEY`。主路沿用现有 Command Code 凭据来源：
`TTS_TRANSLATE_FALLBACK_KEY` / `TTS_TRANSLATE_FALLBACK_KEY2`，或已有本机密钥文件与 Hermes 私密配置。
Codex CLI 仅作最终退路；`CODEX_BIN` 指向的文件不存在时立即跳过、只提示一次，不起进程或设置失败冷却。
退路模型默认 `deepseek/deepseek-v4.1-flash`，可用 `TTS_TRANSLATE_FALLBACK_MODEL` 覆盖；`reflection/beam:free` 仅作手动备选。
部署时清理上轮留下的 `TTS_TRANSLATE_BACKEND=api` 环境覆盖，或显式设为 `codex`；启动 bat 不再设置该变量。
只有显式选择 `api` 才需独立 key：进程环境 `TTS_TRANSLATE_KEY` 优先，否则从 `HERMES_ENV_FILE` 指向的本地
私密文件读取 `DEEPSEEK_API_KEY`（默认文件 `%USERPROFILE%\AppData\Local\hermes\.env`）。
该模式默认 `TTS_TRANSLATE_BASE=https://api.deepseek.com/v1`、`TTS_TRANSLATE_MODEL=deepseek-flash`；
启动器只在未定义时设置这两个值。当前机器未配置该模式所需 key，不应作为默认线路。
`config/local.env.example` 是空值模板，现有启动器不会自动加载它。

`pet/check_all.py` 从 WSL 的 `/root/albion-pi/agent/.env`（`API_SERVER_KEY`）读探活 key；Windows 侧旧 Albion `.env` 已不再读，两边不会自动同步。
新机器要核实此条件，不能因自检缺 key 把网关误判为宕机。
壳内 provider、key、布局与角色选项在 Electron 私人用户数据中，恢复备份或手动配置：
脑宿主 :8650（Pi 版）、流式 TTS :9885、STT :9882、采集端 :8790；鉴权值需与服务匹配，禁止写进仓库。

## 启动与验收

环境与配置全部准备好后，在新部署执行；现有运行机器不要为了验收重复拉起服务。

```powershell
Set-Location D:\AI\Wife
New-Item -ItemType Directory -Path logs -Force
& '.\一键启动-构建版.bat' show
& .\tts\irodori\.venv\Scripts\python.exe -B -m unittest discover -s tests/tts -p 'test_*.py'
python tools/check_repository.py
python tools/restore_weights.py
& .\tts\GPT-sovits\runtime\python.exe pet/check_all.py 0
```

还要实测：壳显示角色、中文输入/回复、STT、日语发声与切句播放、眼睛、摆件、WSL profile 隔离。
单元测试、端口可连或哈希一致不能代替端到端验收。
本轮验证及未验证项见 `docs/history/ORGANIZATION_REPORT.md`。
