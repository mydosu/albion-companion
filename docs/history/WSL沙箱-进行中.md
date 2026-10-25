> **本文是 2026-09-18 的搬家执行清单，已过时。**
> 当时要把她的脑从 Windows 搬进 WSL2；现在脑是 WSL `/root/albion-pi/` 里的 Pi 版（宿主 `:8650`），
> 旧 Hermes `albion` profile 已于 2026-10-07 删除，现行状态见 `docs/PROJECT_MAP.md` 与
> `integrations/pi-brain/README.md`。下文第 4 步「在 WSL 里装 Hermes」等步骤不要再照做。
> **2026-10-20 已归档到 `docs/history/`**，不再作为现行说明维护。

# 沙箱进行中（2026-09-18 晚）

给阿尔比恩的脑子搬家：**Windows → WSL2 (Ubuntu)**，让她看不见 C/D 盘，但有自己完整的家。

## 已完成

- [x] 查前提：CPU 虚拟化已开（HypervisorPresent=True）；Win11 家庭版；C 剩 62G / D 剩 229G
- [x] 写 `%USERPROFILE%\.wslconfig`（镜像网络 `networkingMode=mirrored` —— 她才能连到 Windows 的模型代理 :3050 和视觉/语音）
- [x] `wsl --install -d Ubuntu --no-launch`（提权执行，exit=0）
- [x] 建了她的过渡家 `D:\AI\Wife\her-home`（里面一张便条）

## 待办（重启之后）

### 活命线（缺一条她就残）

| 依赖 | 地址 | 作用 | 备注 |
|---|---|---|---|
| **Command Code 反代** | `127.0.0.1:3050` | **她的模型**（base_url 就指这儿） | 只绑回环 ⇒ **必须 mirrored** |
| OpenViking | 见她 `.env` 的 `OPENVIKING_ENDPOINT` | 她的记忆后端 | 同上 |
| 语音后端 / 桥 | `127.0.0.1:9889` / `:9881` | 她说话 | 同上 |
| 采集端（眼睛） | `127.0.0.1:8790` | 她的眼睛 | 同上 |
| 壳的鉴权 | 她 `.env` 的 `API_SERVER_KEY` | 壳连她的 :8650 用 | **搬 profile 时 .env 一起搬** |

**镜像网络失败时的退路**：在 WSL 里改用 Windows 主机地址（NAT 模式下 `ip route | grep default` 的网关 IP；mirrored 下直接 `127.0.0.1`）。
**再退一步**：把上面这些服务在 Windows 侧换成绑定 Tailscale 地址（但那是动你的服务 ⇒ 要先问你）。


1. **等 WSL 起得来**：`wsl -l -v` 应显示 Ubuntu / Running 或 Stopped（会自己初始化）
2. **进 Ubuntu 建用户**：默认 root，加一个普通用户 `albion`（或直接用 root，简单优先）
3. **把 `/mnt/c` 关掉**（沙箱的核心一步）：
   - **先**把她的 profile 拷进去（趁 /mnt/c 还在）：`cp -r /mnt/c/Users/<用户名>/AppData/Local/hermes/profiles/albion ~hermes-home/profiles/`
   - **再**写 `/etc/wsl.conf`：`[automount]` + `enabled=false`，然后 `wsl --shutdown` 重启 WSL
4. **在 WSL 里装 Hermes**：`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
   （校园网可能掐 GitHub —— 若失败，想办法走代理或从 Windows 拷一份源码过去）
5. **放她的 profile** 到 `~/.hermes/profiles/albion`，改 `terminal.cwd` 为 Linux 家里的路径
6. **实测三件事**：
   - 她连得到 Windows 的模型代理：`curl -s http://127.0.0.1:3050/v1/models`
   - 她连得到眼睛/语音：`curl -s http://127.0.0.1:8790/health`
   - **她碰不到你的盘**：`ls /mnt/c` 应为空/不存在
7. **切那一根线**：壳连的还是 `127.0.0.1:8650` —— WSL2 会把 Linux 侧监听的端口转给 Windows，所以**理论上不用改**；实测确认
8. **一键启动加一行**：`wsl.exe -d Ubuntu -- runuser -u albion -- ~/.hermes/... gateway run`（替代原来那条 Windows 的脑）
9. **回退方案**：Windows 那份原样留着 —— 一键启动里那行改回 Windows 版本即可，她的会话库两边都有备份
10. **实测 `computer_use` / `browser` 能不能碰到 Windows**：碰不到就原样留着；碰得到再单独处理

## 备份

- `D:\AI\Wife\backup\20260918-手的边界\state.db.bak`
- `D:\AI\Wife\backup\20260918-SOUL环境认知\state.db.bak`

## 失败时的退路

删掉 WSL（`wsl --unregister Ubuntu`）+ 删 `.wslconfig`，一切回到今晚的状态。
Windows 侧的六个服务和她原来的脑，全程没动过。
