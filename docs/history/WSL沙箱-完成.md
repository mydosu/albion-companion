> **本文记录 2026-09-18 那次搬家的结果，部分内容已过时。**
> 现行状态：脑是 WSL `/root/albion-pi/` 的 Pi 版（宿主 `:8650`，启动脚本 `_启动脑Pi.ps1`）；
> Hermes `albion` profile（2026-10-07 删除）与 `_启动脑WSL.ps1`（2026-10-16 删除）都已不在，
> 见 `docs/PROJECT_MAP.md`、`integrations/pi-brain/README.md`。
> 下文「回退」里的 `wsl --unregister Ubuntu` 会连现在的 Pi 脑一起删掉，**不要再按它回退**；
> 现行回退按生产变更流程做。
> **2026-10-20 已归档到 `docs/history/`**，不再作为现行说明维护。

# 沙箱 —— 已完成（2026-09-18 21:10）

阿尔比恩的脑已从 Windows 搬进 **WSL2 (Ubuntu)**，她看不见 C/D 盘，但有自己完整的家。

## 结果

| | |
|---|---|
| 她的脑 | WSL 里的 `hermes -p albion gateway run`（监听 `127.0.0.1:8650`）|
| 她的家 | `/root/家`（Linux 里的目录，`terminal.cwd` 已指过去）|
| **沙箱边界** | `mount \| grep -c drvfs` = **0** ✓ ；`/mnt/c` 条目数 = **0** ✓ |
| **Windows 程序** | `cmd.exe...: command not found` ✓（interop 也关了）|
| 命脉（从笼子里） | 模型 `:3050` ✓ / 桥 `:9881` ✓ / 耳 `:9882` ✓ / 眼 `:8790` ✓ 全 200 |
| 壳 | **一个字没改**（连的还是 `127.0.0.1:8650`，镜像网络把她转过去了）|
| 钥匙 | 壳里配的 key 与她 `.env` 的 `API_SERVER_KEY` 逐字一致 ✓ |

## 接线

`一键启动*.bat` 里她那块换成了 `_启动脑WSL.ps1`（幂等）；（2026-10-16 补记：脑早已换成 Pi 版，该脚本已删，现在是 `_启动脑Pi.ps1`）
**旧版 Windows 脑的启动命令留在上面一行注释里，回退 = 改一行。**

## 顺手修的三个真问题（都不是沙箱引起的）

1. `tts\denia` 被改名成 `tts\GPT-sovits` ⇒ **12 个文件还指着旧路径**（耳朵、眼睛、一键启动、check_all 全跑不起来）⇒ 已逐个改对，`.bat` 保持 GBK+CRLF
2. 那个 runtime 里**缺 `mss`**（眼睛跑不了）⇒ 已装
3. 我自己的测试启动器用了 `UseShellExecute=$false` ⇒ 壳继承我的管道 ⇒ 我这边一退它就 `EPIPE` 崩 ⇒ 已改 `Start-Process`

## 回退

`wsl --unregister Ubuntu` + 删 `%USERPROFILE%\.wslconfig`，并把一键启动里那行注释取消。
Windows 侧的六个服务和她原来的脑全程没动过（她那份 profile 也还在原处）。
