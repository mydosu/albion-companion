# albion-pi —— 阿尔比恩的大脑（Pi 版）

WSL(Ubuntu) 里的阿尔比恩原来寄宿在 Hermes Agent 的 `albion` profile 里，现在换成
[Pi](https://github.com/earendil-works/pi-coding-agent) 当 harness。
2026-10-07 又把**宿主那一层搬空**：人格、每轮约定、回忆、守卫、活动库、模型通路都在 Pi 自己身上
（`agent/`），宿主只剩"把壳说的 OpenAI-HTTP 翻成 Pi 的 RPC"这一件不可替代的事。
**直跑 `pi` 得到的就是她本人**，不需要宿主参与。
2026-10-14：她有了自己的眼睛（`look` 工具）、能自己定下次醒来的时间（`next_wake`），
改文件与否以**事实账本**为准（一轮前后对她的目录做快照比对，见 `albion-loopguard.ts`）。
2026-10-16：主动对话归她——多久开口一次她自己定，宿主的补催只剩「0 字补一句」，说话守卫精简成不叫模型的整理器，
换身子从活动库接上刚才的对话；壳端的主动定时器、`[[LOOK]]`、草稿守卫撤掉，壳只当门铃和播放器。

```
壳 airi.exe :9335 ─┐   门铃：每 30 秒 GET /albion/wake
同席 local-agent-hub ─┴─OpenAI 兼容─> 127.0.0.1:8650  ← 宿主（bin/server.py：协议转接 + 醒来门铃）
                                          │
                                          └─ pi --mode rpc 子进程（长驻，提着她的全部）
                                               ├─ 人格      agent/SYSTEM.md（由 SOUL.md 生成）
                                               ├─ 每轮约定 + 回忆 + 活动库 + 处境  albion-turn.ts
                                               ├─ 说话整理器  albion-guard.ts（不叫模型）
                                               ├─ 复读断路/坏工具/改文件快照账本  albion-loopguard.ts
                                               ├─ 她自己的时间  albion-self.ts → state/self.json、state/notebook.json
                                               ├─ 她自己的眼睛  albion-look.ts（his/mine 原图或转文字；cam 只走本地视觉模型）
                                               ├─ 模型通路  albion-relay.ts（本机转发 :8798 → 上游）
                                               ├─ 壳插件工具  albion-shell-tools.ts（插件随加随卸，按 tool_calls 交壳执行）
                                               └─ 工具      openviking / delegate / websearch / 她自己加的
```

## 文件

| 路径 | 作用 |
|---|---|
| `bin/server.py` | 宿主：:8650 的 OpenAI 兼容端点、会话与进程生命周期（会话 id 认 body 的 `session_id` 或壳的 `X-Hermes-Session-Id` 头）、换身子从活动库接上对话、扩展热重载、SSE 切句、0 字补一句；门铃 `GET /albion/wake`（规则在 `bin/wake.py`） |
| `bin/pi_client.py` | 常驻 `pi --mode rpc` 子进程的 JSONL 客户端（交付以 Pi 的定稿消息为准） |
| `bin/shell_tools.py` | 壳插件工具桥（2026-10-20）：读壳的 `X-Albion-Toolset` 头，只桥接头里点名的插件工具；她调用时把 OpenAI `tool_calls` 回给壳，壳带 `role=tool` 结果再来时接上那一轮。壳 `ALBION_SHELL_TOOL_TIMEOUT`（默认 120 秒）不回就按失败交还；扩展取清单走 `GET /albion/shell-tools`、调用走 `POST /albion/shell-tools/call`，都要 `API_SERVER_KEY` |
| `agent/shell-tools-bridge.ts` + `agent/extensions/albion-shell-tools.ts` | 每轮开跑前取清单：新插件注册成代理工具、卸载的改成 hidden（Pi 工具不能注销），不用换身子。和她自己的工具重名时插件那件改叫 `shell_<名>`，她的永远优先；插件规则按壳提供的 owner/id 各放一个系统提示区块，不再混入工具描述；取不到清单就全部撤下。自定义 SYSTEM.md 下额外补入当前可见工具的 promptGuidelines 与全局规则，不改人格正文 |
| `bin/voice_tidy.ts` | 整理器的命令行壳（给 Python 探针 `tests/pi-brain/check_speech.py` 用；规则直接 import 扩展，不另写一份） |
| `bin/memory.py` | OpenViking 回忆 / 写入 / 检索（格式与 Hermes 时代逐字一致） |
| `bin/wake.py` | 她自己醒来：三条底线（夜里安静、他刚说过话不打断、连着不回就退避）+ 落实她定的开口节奏；门铃问的就是它 |
| `bin/test_guard_ext.ts` | 整理器与每轮约定自检（钩子接线、r35 多轮姿态序列、英文拒绝、表态小句删除与误删面、0 次网络请求、约定拼装；Windows/WSL 都能跑） |
| `bin/test_loopguard.ts` | 复读断路/坏工具/快照账本自检（39 项，临时目录里造文件，不写生产日志） |
| `bin/check_extensions.ts` | 用 Pi 自己的加载器整批加载 `agent/extensions/*.ts`，查加载错误和工具 `parameters` 形状；不叫模型 |
| `bin/test_brain.py` | 端到端自检：两个回合 + 多轮连续性 + ACT 标记 |
| `accept.sh` | 可复跑验收（直跑 pi 是不是她／记忆与约定／整理器与复读断路自检／两个壳入口／活动库） |
| `agent/SYSTEM.md` | **生成物**：启动时从 `SOUL.md` 复制，人格只有一个源 |
| `agent/APPEND_SYSTEM.md` | 附在系统提示后的增补（会话开始时一次）：边界、能做的事、她自己的时间、说话、改文件的事实、看图 |
| `agent/skills/*/SKILL.md` | Pi 原生技能：系统提示里只放名字和"什么时候用"，用到才读全文。`albion-work`（干活的规矩与交付）、`albion-teaching`（讲功课、给做法）、`albion-build-tool`（给自己写扩展）。2026-10-17 从 SOUL/APPEND 里拆出来，人格那份只留她是谁、怎么说话、想给他什么 |
| `agent/extensions/albion-turn.ts` | `context` 钩子注入约定+回忆+处境（处境放最末尾）；`tool_result` 每调完一个工具就写一行活动库，`agent_end` 再写 user/assistant 和账本补记的路径；写类工具按工具结果是否报错记 `verified`（精度限制见源码 `shortcut:` 注释） |
| `agent/extensions/albion-loopguard.ts` | 复读断路（同一调用 + 文件没变才算复读）、坏工具摘除、一轮前后目录快照账本（给摆件和下一轮约定） |
| `agent/extensions/albion-self.ts` | `next_wake`（下次什么时候醒、想做什么）、`notebook`（心事本） |
| `agent/extensions/albion-look.ts` | `look`：his/mine 取屏幕原图交主模型（主模型不收图就原图交 grok 转述）；cam 只交本地视觉模型转文字 |
| `agent/extensions/albion-guard.ts` | `message_end` 钩子上的说话整理器：ACT 补竖线、心声收括号、英文拒绝只留表情、泄漏标记截掉、姿态句与括号动作小句各自计额度、对他的话表态的整句/整小句删掉（`pledge_dropped`） |
| `agent/extensions/albion-relay.ts` | 本机转发 :8798（见下"为什么要转发"）；Pi 一启动它就起 |
| `agent/extensions/openviking.ts` | 一行薄壳：加载官方 OpenViking 记忆扩展（自动捕获 + 16 个 `openviking_*` 工具）。主体在 `agent/openviking/`，来源与配置见那里的说明 |
| `agent/openviking/` | 官方扩展 `openviking-pi-extension` 0.4.13（**不 vendor 源码**，只记来源）；`来源与配置.md` 写清了装法、开关与回退 |
| `agent/models.json` | provider 指向本机转发；`commandcode`（Command Code 第一把 key，daily one）与 `grokpool`（号池 key）都只在 WSL 里，**不入库** |
| `agent/settings.json` | Pi 设置：默认 commandcode/deepseek/deepseek-v4-flash（脑本体），原思考设置保留、模型能力按注册项声明，`images.autoResize: false`（图片原字节直传，本机不缩） |
| `agent/extensions/albion-lunar.ts` | `lunar_calendar`：农历/节气/干支/宜忌，`lunar-javascript` 本机算——**她自己造的**（2026-10-14） |
| `state/state.db` | 活动库（Hermes state.db 的最小兼容表） |
| `bin/widget_snapshot.py` → `state/widget.json` | 宿主后台线程每 5 秒把活动库查询结果原子写成快照；Windows 桌面摆件每 5 秒读一次这份（超过 60 秒没更新就显示"上次成功 X 前"）。`--selftest` 自检 |
| `state/last-context.json` | 每轮最终注入的内容（可复核"回忆/约定真进了消息"） |
| `state/self.json` | 她自己定的下次醒来时间、想做的事、开口节奏 `speak_every_hours`（`albion-self.ts` 写，`bin/wake.py` 读） |
| `state/voice.json` | 整理器的姿态句与括号动作额度（每会话的 replies、posture_at、gesture_at；旧状态兼容） |
| `state/notebook.json` | 心事本：没划掉的惦记，醒来时念给她 |
| `logs/brain.log` | 每轮请求、耗时 |
| `logs/guard.log` | 整理器动过手的那几条（只记动作和会话，不记正文；`ALBION_GUARD_LOG` 可改去别处） |
| `logs/loopguard.log` | 复读断路、坏工具摘除、账本；自检和探针设 `ALBION_LOOPGUARD_LOG` 写去别处，别灌进生产这份 |

## 记忆（OpenViking）

- **召回**（每轮自动）：`albion-turn.ts` 走 `bin/memory.py --call recall`，拼进这一轮 user 消息，顺序是 **当前原文 → 回忆 → 约定收尾**。
- **捕获**（每轮自动）：官方扩展把回合写进 OpenViking session 并 commit（8 轮或 20000 token 到点即交），抽取器异步产记忆。
- **工具面**：官方 16 个 `openviking_*`（`find` / `search` / `read` / `remember` / `write` / `forget` / …）；我们手写的 `viking_search`/`viking_remember` 已并入它、不再单独维护。
- 官方的**自动召回已关**（`agent/.env` 里 `OPENVIKING_AUTO_RECALL=false`）：不然一轮里会出现两份回忆，而且它注入在我们"约定收尾"**之后**，把那口径打乱。
- **Context takeover 保持默认关闭**。
- 池子：`viking://user/albion/memories`。装法、参数与回退见 `agent/openviking/来源与配置.md`。
- **召回只取她自己的记忆**（2026-10-14）：同一个 OpenViking 里还有开发者会话记忆、开发报告资源、Hermes 时期
  通用助手的空间（1400 多条，混着开发调试、别的项目、别人的微信）。`bin/memory.py` 默认只放行
  `viking://user/albion/`，外加 `state/recall-allow.txt` 白名单里的 Hermes 时期条目（她和指挥官的生活，
  模型分类 + 规则复筛得来，去掉了开发味、别人的、以及当年拒绝亲密的记录）。`OPENVIKING_RECALL_SCOPES`、
  `OPENVIKING_RECALL_ALLOW_FILE` 可改。她用 `openviking_*` 工具主动搜不受限制。
- **只有真对话会话写记忆**：`bin/server.py` 给 `albion-desktop`、`albion-cli` 以外的会话（探针、验收、自检）
  启动 pi 时设 `OPENVIKING_BYPASS_SESSION=1`，官方扩展整段跳过；`accept.sh` 同样。改名单用 `ALBION_MEMORY_SESSIONS`。
  当天整理删掉的 76 条（开发会话、测试痕迹、探针对话）全文备份在 WSL `/root/ov-tidy/backup-20261014/`。

## 她自己的时间（醒来循环）

- **时间由她自己定**：一轮里调用 `next_wake`（10–360 分钟、醒来想做什么）→ `state/self.json`；
  `notebook`（`state/notebook.json`）是她的心事本，醒来时宿主把没划掉的念给她。
- **壳只当门铃**：每 30 秒问一次 `GET /albion/wake`；到点了壳发一条只有标记的消息（不带内容），
  宿主把它换成她醒来时该看到的：现在几点、他多久没说话、她自己定的事、心事本、这次能不能出声。
  看不看、说不说、做什么都由她；她的回复照常走流式 → 字幕／表情／语音／聊天记录。
- **多久主动开口一次也由她定**（2026-10-16 起）：`next_wake` 的 `speak_every_hours`（1–6 小时，不填沿用上次，
  从没定过是 3 小时——10-15 指挥官嫌每小时都出声太勤时定的）。没到她定的间隔，这轮只留表情、心声也不显示；
  说多说少不再卡字数。
- **三条底线（为他的作息，她改不了，常量在 `bin/wake.py`）**：北京时间 00:30–09:00 不醒（顺延到早上）；
  他 5 分钟内说过话不醒（别打断对话）；她主动说的话他连着没回就退避（间隔按 2 的幂拉长，最多 8 倍；
  连着 3 次没回至少隔 3 小时）。两次醒来至少隔 10 分钟是 `next_wake` 的下限。
- 没定下次醒来就按 30 分钟后算；宿主不再补问（原来的【定下次醒来】已撤）。
- 壳端原来那套主动定时器（`settings/albion/self-wake = '0'` 退路）已于 2026-10-16 撤掉，主动对话只走这条门铃。

## 她自己的眼睛（`look`）

- 三路：`his`＝指挥官那台电脑的屏幕、`mine`＝她自己所在那台、`cam`＝这台电脑的摄像头（真实世界）。
  **his/mine 取原图直接交主模型自己看**（字和界面看得更准）；**`cam` 的照片只交给局域网里的本地视觉模型**
  （Qwen3VL），她拿到的是文字描述——照片不进主模型、不出局域网（指挥官定）。
- **主模型不收图时**（DS 文本模型；pi 会把图整张丢掉）his/mine 原图交号池 grok-4.7 转述，她拿文字描述；
  grok 不成再退到本地视觉模型。本机不缩不转（指挥官定）。取图上限 60 秒，grok 转述上限 75 秒。
- 视觉模型在另一台机器上（Tailscale `<视觉机 Tailscale IP>:8080`，Docker 里的 llama.cpp）。那台的
  `/usr/local/bin/docker-user-rules.sh` 会在 Docker 启动时重写 `DOCKER-USER`，8080 只放行局域网和 tailnet
  （100.64.0.0/10，2026-10-15 补上；之前重启后 tailnet 被挡，她报「摄像头坏了」）。
- 硬底线（她改不了）：同一路 60 秒内不重复取；摄像头永远只走本地视觉模型。
- 配置在 `agent/.env`（不进仓库），每次调用现读、改了不用重启：
  `ALBION_EYES_HIS_URL/ALBION_EYES_HIS_TOKEN`、`ALBION_EYES_MINE_URL/ALBION_EYES_MINE_TOKEN`、`ALBION_EYES_VLM`。
  `ALBION_EYES_HIS_URL` 可写多个、逗号分隔（局域网在前、Tailscale 在后），每次取图前 TCP 探 3 秒挑第一个通的，
  都不通就用最后一个。
- 敏感窗口在采集端（`pet/tools/eyes/capture_agent.py`）就涂黑了，交给谁看的都是涂过的图。
- 壳里原来的 `[[LOOK:his]]` 标记退路已于 2026-10-16 撤掉，只剩 `look` 工具这一条路。

## 启停

```bash
bash /root/albion-pi/start.sh      # 幂等；8650 被别的东西占着会明确报错
bash /root/albion-pi/stop.sh
bash /root/albion-pi/accept.sh     # 验一遍：直跑 pi 是不是她、两个壳入口还活着
```

Windows 侧：`D:\AI\Wife\_启动脑Pi.ps1`（`一键启动.bat`、`一键启动-dev.bat`、`start_all.bat` 第 4 步都指向它）。

### 看门狗（WSL 整机停掉后自动把脑拉回来）

2026-10-24 00:44 前后疑似待机事件把 WSL 虚拟机整个停了，01:40 又起来，但没人拉起脑，桌面和聊天断了一个多小时。
`tools/albion_brain_watch.ps1` 是最小的补丁：`airi.exe` 不在就直接退出（用户按「一键关闭.bat」关了全部后不该再偷偷拉起脑）；
8650 的 health 正常（内容含 `"brain": "pi"`）也不写日志；只有不通时才跑幂等的 `start.sh`，再把一行
「时间 + ok/fail + start.sh 输出最后一行」追加到 `D:\AI\Wife\logs\brain-watch.log`（`logs/` 不入 Git）。
它不会 `wsl --shutdown`，也不调 `_启动脑Pi.ps1`（那条路会重进 WSL）。

计划任务名 `AlbionBrainWatch`，每 2 分钟一次、只在用户登录时运行，经 `_静默启动.vbs` 起隐藏窗口（登录的 PowerShell 里执行一次）：

```powershell
$act = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '"D:\AI\Wife\_静默启动.vbs" "powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\Wife\tools\albion_brain_watch.ps1"'
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'AlbionBrainWatch' -Action $act -Trigger $trg -Settings $set -Description 'WSL 停掉后把 Pi 脑拉回来'
```

`-RepetitionDuration ([TimeSpan]::MaxValue)` 存进去是 `P99999999D…`，任务 XML 不认，会报「任务 XML 包含格式不正确或超出范围的值」；
用 `New-TimeSpan -Days 3650`（约 10 年）即可。2026-10-24 本机就是这么注册的。

改完重注册：`Unregister-ScheduledTask -TaskName AlbionBrainWatch -Confirm:$false` 再跑上面的块。

## 为什么要本地转发（不是多此一举）

上游 `api.commandcode.ai` 在本机建连要 ~10s，而 Node/undici 的默认建连超时正好是 10s，
Pi（Node 程序）直连会稳定超时（`Request timed out.`）。转发把这一步变成 localhost 连接。
2026-10-14 查明那 ~10s 其实是 DNS：查 IPv6（AAAA）卡 11s 才超时，只查 IPv4 是 0.2s。转发原来自己也用 `fetch`、
同样吃 10s 上限，冷连接必挂（主力换 DS 后宿主重启后第一个会话全 502）；现在用 `node:https` 只走 IPv4 + 长连接复用。
实测直连 3 次全超时、走转发 3 次成功。上游还有 Cloudflare 按 UA 拦，转发带 `User-Agent: curl/8.5.0`。
现在这条转发由 `albion-relay.ts` 提供：脑停着、两个端口都空着时直跑 `pi`，它会自己把 8798 起起来。

## 主力模型（2026-10-14 起是 DeepSeek）

手动备选：`commandcode/reflection/beam:free`（已在 `agent/models.json` 注册，免费线路可能 429，用前查 `/models` 与 [官方模型目录](https://commandcode.ai/models)）；没有自动切换。
翻译退路 `tts/irodori/bridge.py` 默认 `deepseek/deepseek-v4.1-flash`。
不要用仓库的 `agent/models.json` 示例覆盖生产文件，生产文件保留私密 provider 与现有 key 引用。
注意：生产 `models.json` 里 commandcode 的 `apiKey` 是明文写死的，pi 实际用它而不是 `.env`；换 key 要 `agent/.env` 和 `agent/models.json` 两处一起改，只改 `.env` 脑照样报额度不足。

- 脑本体默认 `commandcode/deepseek/deepseek-v4-flash`（`bin/server.py` 的 `PROVIDER`/`MODEL`；`agent/settings.json` 同步，v4 好调教语言）。
  其余入口（`cli.sh`、`delegate.ts`、TTS 翻译退路）用 `deepseek/deepseek-v4.1-flash`。
  同一套真实接口实测：自己造工具 DS 一次做成、grok-4.7 五次全失败（复读、谎称造好、写出加载不了的扩展）；聊天 DS 更自然、更快。
- 回 grok：`ALBION_PROVIDER=grokpool ALBION_MODEL=grok-4.7 bash start.sh`。另起一只对比脑：再加 `ALBION_BRAIN_PORT=8651`。
- 说出口之前只过一道**整理器**（`albion-guard.ts`，2026-10-16 起；原来的严格裁判、同路由重生成、哨兵、`ALBION_DS_PATCHES` 开关都撤了）：
  ACT 补竖线、心声标签收回括号、英文拒绝稿只留表情、泄漏的工具调用标记截掉、陪伴/等候姿态句每 8 条回复最多放过一次
  （`ALBION_POSTURE_EVERY` 可调）；闭合括号里的自身动作/身体反应每 6 条最多留一次，超额只删命中的小句、保留念头
  （`ALBION_GESTURE_EVERY` 可调，整条删空则原样留并用掉额度）。两种额度各自记在 `state/voice.json`，不叫模型、不发请求。
- 兜底只剩一条：他说了话她却 0 字 → 在同一会话补一句只用文字的【接着说完】（英文拒绝被换成表情后也走这条）。
  原来的【想起来去做】（去玩没玩、说做好了没做）已撤：做不做由她；改没改文件，下一轮约定里会告诉她账本上的事实。
- 她写坏的扩展换身子前自检时先跳过、记进 `state/broken-extensions.json`，下一轮约定里告诉她去修。

## 她自己长手脚

- 扩展放 `agent/extensions/*.ts`。宿主每收一次请求都会比对目录签名，**文件一变就换一具身子**
  。壳每轮只发最新一句，新身子第一轮由宿主从活动库 `state/state.db` 接上这个会话两天内最近 40 条对话
  （2026-10-16 起；之前换身子就忘了刚才在聊什么）。换身子是整批加载，一个坏扩展会让整批起不来，
  所以存完先跑 `node bin/check_extensions.ts`。
- "怎么给自己加工具"写在技能 `agent/skills/albion-build-tool/SKILL.md`（要用时才读；技能文件改了也算进换身子的签名）；
  更细的三条路与纪律见 `给她加本事.md`。
- `server.py` 里 `TOOLS = ["*"]`：**必须显式给**——RPC/print 模式不给 `--tools` 就一件工具都没有；
  用 `*` 通配才能让内置四件和她自己注册的扩展工具同时可用。

## 回退

**退回守卫精简之前（2026-10-16 之前）的脑**：从仓库提交 `5f755e83` 取 `integrations/pi-brain/` 的
`bin/`、`agent/extensions/`、`agent/APPEND_SYSTEM.md`、`accept.sh` 覆盖回 `/root/albion-pi/` 对应位置
（先在独立目录 `git worktree add` 出来核对），再 `bash stop.sh && bash start.sh`。`state/voice.json` 留着无害。
壳端要配套退回同一提交的 `shell/`，否则旧脑的 `[[LOOK]]`/壳内主动没有对应的壳。

**回到 Hermes**：Hermes 已于 2026-10-07 从 WSL 删除，仓库里的回退快照（`integrations/hermes/`、`_启动脑WSL.ps1`）
也于 2026-10-16 撤掉；要回去从 `git show 5f755e83:integrations/hermes/README.md` 起照做，
私人 profile 在 `archive/hermes-profile-20261007/`。

## 她自己的 node

`/usr/local/bin/node` 原来指向 Hermes 自带的那份；Hermes 删掉后已把整份 node 搬到
`runtime/node/`（229M），`node`/`npm`/`npx` 三个符号链接指过去——**别删 `runtime/`，Pi 靠它跑**。
