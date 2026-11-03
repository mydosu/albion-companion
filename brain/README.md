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
| `bin/pi_client.py` | 常驻 `pi --mode rpc` 子进程的 JSONL 客户端（交付以 Pi 的定稿消息为准）；`steer` 把桌面补的话插进正在跑的那一轮，一轮多段正文按顺序拼接交付 |
| `bin/shell_tools.py` | 壳插件工具桥（2026-10-20）：读壳的 `X-Albion-Toolset` 头，只桥接头里点名的插件工具；她调用时把 OpenAI `tool_calls` 回给壳，壳带 `role=tool` 结果再来时接上那一轮。壳 `ALBION_SHELL_TOOL_TIMEOUT`（默认 120 秒）不回就按失败交还；扩展取清单走 `GET /albion/shell-tools`、调用走 `POST /albion/shell-tools/call`，都要 `API_SERVER_KEY` |
| `agent/shell-tools-bridge.ts` + `agent/extensions/albion-shell-tools.ts` | 每轮开跑前取清单：新插件注册成代理工具、卸载的改成 hidden（Pi 工具不能注销），不用换身子。和她自己的工具重名时插件那件改叫 `shell_<名>`，她的永远优先；插件规则按壳提供的 owner/id 各放一个系统提示区块，不再混入工具描述；取不到清单就全部撤下。自定义 SYSTEM.md 下额外补入当前可见工具的 promptGuidelines 与全局规则，不改人格正文 |
| `bin/voice_tidy.ts` | 整理器的命令行壳（给 Python 探针 `tests/pi-brain/check_speech.py` 用；规则直接 import 扩展，不另写一份） |
| `bin/memory.py` | OpenViking 回忆 / 写入 / 检索（格式与 Hermes 时代逐字一致） |
| `bin/wake.py` | 她自己醒来：一条门铃计时（她定的时间夹在 30 分钟～3 小时，没定过 60 分钟）+ 三条底线（夜里安静、他刚说过话不打断、两次醒来至少隔 10 分钟）+ `can_speak` 决定这一轮能不能开口；门铃问的就是它 |
| `bin/test_guard_ext.ts` | 整理器与每轮约定自检（钩子接线、r35 多轮姿态序列、英文拒绝、表态小句删除与误删面、0 次网络请求、约定拼装；Windows/WSL 都能跑） |
| `bin/test_cross_channel.ts` | 跨渠道近况自检（桌面只收 Discord 群、悄悄话不进桌面、Discord 收桌面近况、水位不重复、内部提示/播种块过滤、send_message 正文落库、6 小时窗口与 12 行/1500 字上限；Windows/WSL 都能跑） |
| `bin/test_self_look.ts` | `next_wake` 一条计时与 `look` 随时可用的自检（分钟数夹在 30～180、self.json 新旧结构、did 与 life.json 20 条、旧生活轮字段不挡取图；Windows/WSL 都能跑） |
| `bin/test_loopguard.ts` | 复读断路/坏工具/快照账本自检（39 项，临时目录里造文件，不写生产日志） |
| `bin/check_extensions.ts` | 用 Pi 自己的加载器整批加载 `agent/extensions/*.ts`，查加载错误和工具 `parameters` 形状；不叫模型 |
| `bin/test_brain.py` | 端到端自检：两个回合 + 多轮连续性 + ACT 标记 |
| `accept.sh` | 可复跑验收（直跑 pi 是不是她／记忆与约定／整理器与复读断路自检／两个壳入口／活动库） |
| `agent/SYSTEM.md` | **生成物**：启动时从 `SOUL.md` 复制，人格只有一个源 |
| `agent/APPEND_SYSTEM.md` | 附在系统提示后的增补（会话开始时一次）：边界、能做的事、她自己的时间、说话、改文件的事实、看图 |
| `agent/skills/*/SKILL.md` | Pi 原生技能：系统提示里只放名字和"什么时候用"，用到才读全文。`albion-work`（干活的规矩与交付）、`albion-teaching`（讲功课、给做法）、`albion-build-tool`（给自己写扩展）。2026-10-17 从 SOUL/APPEND 里拆出来，人格那份只留她是谁、怎么说话、想给他什么 |
| `agent/extensions/albion-turn.ts` | `context` 钩子注入约定+回忆+处境（处境放最末尾）；`tool_result` 每调完一个工具就写一行活动库，`agent_end` 再写 user/assistant 和账本补记的路径；写类工具按工具结果是否报错记 `verified`（精度限制见源码 `shortcut:` 注释） |
| `agent/extensions/albion-loopguard.ts` | 复读断路（同一调用 + 文件没变才算复读）、坏工具摘除、一轮前后目录快照账本（给摆件和下一轮约定） |
| `agent/extensions/albion-self.ts` | `next_wake`（下次什么时候醒——一条计时、想做什么、这次做了什么）、`notebook`（心事本） |
| `agent/extensions/albion-look.ts` | `look`：his/mine 取屏幕原图交主模型（主模型不收图就原图交 grok 转述）；cam 只交本地视觉模型转文字；随时可看 |
| `agent/extensions/albion-guard.ts` | `message_end` 钩子上的说话整理器：ACT 补竖线、心声收括号、英文拒绝只留表情、泄漏标记截掉、姿态句与括号动作小句各自计额度、对他的话表态的整句/整小句删掉（`pledge_dropped`） |
| `agent/extensions/albion-relay.ts` | 本机转发 :8798（见下"为什么要转发"）；Pi 一启动它就起 |
| `agent/extensions/openviking.ts` | 一行薄壳：加载官方 OpenViking 记忆扩展（自动捕获 + 16 个 `openviking_*` 工具）。主体在 `agent/openviking/`，来源与配置见那里的说明 |
| `agent/openviking/` | 官方扩展 `openviking-pi-extension` 0.4.13（**不 vendor 源码**，只记来源）；`来源与配置.md` 写清了装法、开关与回退 |
| `agent/models.json` | provider 指向本机转发（`commandcode`）与本机 magpie（`magpie`，2026-10-30 起脑的默认线路）；`commandcode`（Command Code 第一把 key，daily one）与 `grokpool`（号池 key）都只在 WSL 里，**不入库**；仓库里留 `commandcode` 的 `$COMMANDCODE_API_KEY` 引用和 magpie 的本地占位 key `magpie`（回环口不校验，不是凭据） |
| `agent/settings.json` | Pi 设置：`defaultProvider`/`defaultModel` 跟着脑走——`bin/follow_settings.py` 每轮把脑实际在用的模型写回这两个键（默认 `magpie/antigravity/gemini-3.8-flash`）；`images.autoResize: false`（图片原字节直传，本机不缩） |
| `agent/extensions/albion-lunar.ts` | `lunar_calendar`：农历/节气/干支/宜忌，`lunar-javascript` 本机算——**她自己造的**（2026-10-14） |
| `state/state.db` | 活动库（Hermes state.db 的最小兼容表） |
| `bin/widget_snapshot.py` → `state/widget.json` | 宿主后台线程每 5 秒把活动库查询结果原子写成快照；Windows 桌面摆件每 5 秒读一次这份（超过 60 秒没更新就显示"上次成功 X 前"）。`--selftest` 自检 |
| `state/last-context.json` | 每轮最终注入的内容（可复核"回忆/约定真进了消息"） |
| `state/self.json` | 她自己定的一条计时（`wake_at`、`intent`、`set_at`）和开口节奏 `speak_every_hours`（`albion-self.ts` 写，`bin/wake.py` 读；旧的两线 `speak`/`life` 格式读时合成一条：取更早的 `wake_at`、intent 用「；」连起来、`set_at` 取较大值；更早的顶层格式照旧） |
| `state/life.json` | 生活记录：她做了自己的事用 `did` 记的一句「做了什么」，每条带时间，只留最近 20 条；醒来时念最近 3 条 |
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
  名单本身在 `agent/real-sessions.json`（env 优先）。当天整理删掉的 76 条（开发会话、测试痕迹、探针对话）全文备份在 WSL `/root/ov-tidy/backup-20261014/`。
- **探针会话也不写生产 state**（2026-10-29）：同一份名单决定 `isEphemeralSession`（`agent/lib/session-scope.ts`），
  ephemeral 会话的心事本/self/todo/voice/last-context 写 `ALBION_PERSONAL_STATE_DIR`（`/tmp/albion-pi-ephemeral/<会话>`），
  工具照常返回、生产文件不动；活动库 `state.db` 仍按会话 id 照常记（只追加，摆件只读桌面那条）。
  起因：10-25 口癖探针的台词「今天好累，实验做了一下午」被写进共享心事本，之后被当成他说过的事反复提。
- **提交前剥掉脑自己拼的段**（2026-10-29）：真实会话启动 pi 时设 `OPENVIKING_CAPTURE_FILTERS`（规则在
  `bin/server.py` 的 `CAPTURE_FILTERS`，sed 式 `user:d|…|` / `user:s|…|`）：自主醒来提示词、`【接着说完】`补问
  整条不提交；会话重启接上的旧记录只留 `【现在】` 之后他真打的字；`<memory-context>` 与每轮约定剥掉。
  醒来那轮因此没有 user 依据，events 抽取不会把它记成他的事实。

## 她自己的时间（醒来循环）

- **时间由她自己定**：一轮里调用 `next_wake`（下次什么时候醒、醒来想做什么）→ `state/self.json`；
  `notebook`（`state/notebook.json`）是她的心事本，醒来时宿主把没划掉的念给她。
- **一条门铃**（2026-11-02 起）：`next_wake` 的分钟数夹在 30～180 分钟（没定过 60 分钟）；
  醒来做什么由她自己选——找他、做自己的事，或者都做，门铃不分种类，也不挡 `look`。
  `self.json` 里原来分开的 `speak` / `life` 两条计时读时合成一条（取更早的 `wake_at`，intent 用「；」连起来，
  `set_at` 取较大值），写回只有一条；更早的顶层格式照旧。
- **壳只当门铃**：每 30 秒问一次 `GET /albion/wake`；到点了壳发一条只有标记的消息（不带内容），
  宿主换成 `wake_prompt()`：现在几点、他多久没说话、她自己定的事、最近自己做过的事、心事本、这一轮能不能开口。
  看不看、说不说、做什么都由她；她的回复照常走流式 → 字幕／表情／语音／聊天记录。
- **生活记录**：她做了自己的事，用 `next_wake` 的 `did` 记一句（≤120 字）→ `state/life.json`（只留最近 20 条，
  每条带时间）；醒来时把最近 3 条按「今天／昨天／N天前」念给她，想分享就挑一件说。
- **平衡记录**：每轮醒来在 `wake.json` 的 `rounds` 记一条（醒来时刻、这轮有没有新的生活记录、有没有说出口），
  只留最近 10 轮；最近 3 轮都没做自己的事时，醒来提示里会提一句她多久没碰自己的事了——只是提示，不拦她找他。
- **多久主动开口一次也由她定**（2026-10-16 起）：`next_wake` 的 `speak_every_hours`（1–6 小时，不填沿用上次，
  从没定过是 3 小时——10-15 指挥官嫌每小时都出声太勤时定的）。没到能开口的时候这轮只留表情、心声也不显示；
  连着没回的退避也只在 `can_speak` 里起作用，不推迟醒来。说多说少不再卡字数。
- **三条底线（为他的作息，她改不了，常量在 `bin/wake.py`）**：北京时间 00:30–09:00 不醒（顺延到早上）；
  他 5 分钟内说过话不醒（别打断对话）；两次醒来至少隔 10 分钟。`can_speak` 另外按她定的开口节奏、
  连着没回的退避（2 的幂，最多 8 倍；连着 3 次没回至少隔 3 小时）决定这一轮能不能开口。
- 没定下次醒来就按 60 分钟后算；宿主不再补问（原来的【定下次醒来】已撤）。
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
- 敏感窗口在采集端（`eyes/capture_agent.py`）就涂黑了，交给谁看的都是涂过的图。
- 壳里原来的 `[[LOOK:his]]` 标记退路已于 2026-10-16 撤掉，只剩 `look` 工具这一条路。

## 启停

```bash
bash /root/albion-pi/start.sh      # 幂等；8650 被别的东西占着会明确报错
bash /root/albion-pi/stop.sh
bash /root/albion-pi/accept.sh     # 验一遍：直跑 pi 是不是她、两个壳入口还活着
```

Windows 侧：`D:\AI\Wife\launch\start_brain.ps1`（只有 `launch\start_all.bat` 的第 4 步调它；两个根入口都先到 start_all）。

## 消息平台（Telegram / Discord，2026-11-02）

她的社交渠道，不是自动转发网关：只有她用 `send_message`（`agent/extensions/albion-chat.ts`）发的，他才在手机上收得到；
那一轮的回复正文照旧只回给壳（桌面），不会自动发到任何平台。

```
TG Bot API ──长轮询──┐
                     ├─ bin/chat_bridge.py（chat-venv，127.0.0.1:8660）──> POST 8650 /v1/chat/completions（【平台消息】）
Discord Gateway ─────┘        │  ▲
                              │  └─ 9881 TTS → ffmpeg → ogg/opus（语音条）
                              └─ state/inbox.json（未回箱）
```

- **桥**：`bin/chat_bridge.py`（单文件，跑在 `chat-venv`：discord.py + aiohttp）。`start.sh` / `stop.sh` 按 8660 幂等起停，日志 `logs/chat.log`；
  `GET /health` 给 `start.sh`、看门狗和自检看，`POST /send` 只给她那个扩展（Bearer = `agent/.env` 的 `API_SERVER_KEY`）。
- **凭据**：`/root/albion-pi/.env`（`ALBION_TG_BOT_TOKEN`、`ALBION_TG_OWNER_ID`、`ALBION_DISCORD_BOT_TOKEN`、`ALBION_DISCORD_OWNER_ID`、
  `ALBION_DISCORD_CHANNEL_ID`，可选 `ALBION_DISCORD_WHISPER_CHANNEL_ID`），`agent/.env` 只提供 `API_SERVER_KEY`。缺 Discord 配置就只跑 TG，日志里直说。
  外网只走代理 `127.0.0.1:7897`（aiohttp / discord.py 都显式给 proxy），回环（8650 / 9881）不走代理。
- **只认他**：TG 只收 `ALBION_TG_OWNER_ID` 的私聊；Discord 私信只收 `ALBION_DISCORD_OWNER_ID`；群只看 `ALBION_DISCORD_CHANNEL_ID`，
  他发言或 @ 她才叫醒（10 秒合批；不按频道冷却，条数由 `group_budget` 兜底），别的 bot 的话只入箱当上下文。
- **会话**：TG 私聊 / Discord 私信与桌面共用 `albion-desktop`；群用 `albion-discord`（在 `agent/real-sessions.json` 里，
  写生产 state、进 OpenViking，长期记忆共享）。
- **未回箱**：`state/inbox.json`（`bin/inbox.py`；桥写、宿主读）。他发来的消息进箱 → 桥叫醒一轮（正文以 `【平台消息】` 开头，
  宿主认它：不算醒来、不自动回）→ 她回不回都行；没回的在下一次醒来提示里念一遍，那一轮结束（`wake.after_wake`）统统了结。
- **语音条**：`send_message(voice=true)` 只对 Telegram；桥打 `127.0.0.1:9881/v1/audio/speech`（Irodori 日语）→ `ffmpeg -c:a libopus`
  → `sendVoice`，caption 是她的中文原文。`ALBION_CHAT_TTS_VOICE=denia` 走 GSV 中文、不带 caption；合成失败降级成文字。
- **主动找他**要守她自己的开口节奏（`wake.can_speak`），回复不受限；群里主动发言只受夜间安静限制。
- **悄悄话**（2026-11-02）：Discord `#悄悄话` 是她和可畏（Hermes 的 Formidable）两个人说话的频道，指挥官看得见，但不告诉她俩。
  频道 id 取 `ALBION_DISCORD_WHISPER_CHANNEL_ID`，没配就在 bot 所在服务器里按名字「悄悄话」找一次；找不到只记一条
  `whisper_channel_missing`、整块不启用，私信和群照旧。两个人的话都入箱（`channel=discord_whisper`，10 秒合批叫醒，
  走 `albion-discord` 真会话），她回话用 `send_message(channel=discord_whisper)`。条数由 `bin/whisper_budget.py` 管
  （一次 20 条、一天 60 条，只数两个 bot），`GET /whisper_budget` 给可畏那边的 hook 查；剩 0 拒发、不占群额度、
  也不受 `wake.can_speak` 限制。
- 回退：取改动前提交的 `bin/server.py`、`bin/wake.py`、`start.sh`、`stop.sh` 覆盖回去，`bash stop.sh && bash start.sh`；
  `state/inbox.json` 与 `logs/chat.log` 留着无害。


### 看门狗（WSL 整机停掉后自动把脑拉回来）

2026-10-24 00:44 前后疑似待机事件把 WSL 虚拟机整个停了，01:40 又起来，但没人拉起脑，桌面和聊天断了一个多小时。
`launch/brain_watch.ps1` 是最小的补丁：`airi.exe` 不在就直接退出（用户按「一键关闭.bat」关了全部后不该再偷偷拉起脑）；
8650 的 health 正常（内容含 `"brain": "pi"`）也不写日志；只有不通时才跑幂等的 `start.sh`，再把一行
「时间 + ok/fail + start.sh 输出最后一行」追加到 `D:\AI\Wife\logs\brain-watch.log`（`logs/` 不入 Git）。
它不会 `wsl --shutdown`，也不调 `launch\start_brain.ps1`（那条路会重进 WSL）。

计划任务名 `AlbionBrainWatch`，每 2 分钟一次、只在用户登录时运行，经 `launch\hidden.vbs` 起隐藏窗口。注册用仓库里的脚本（幂等，先 `-DryRun` 看计划）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\Wife\launch\install_autostart.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\Wife\launch\install_autostart.ps1
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

## 从零安装与部署（陌生人路径）

在 WSL(Ubuntu) 里，以 root 跑（仓库在 Windows 盘上就用 `/mnt/<盘符>/...` 路径）：

```bash
sudo bash brain/install.sh          # 装 Node 26.9.0 和 Pi 1.0.4（钉的是作者生产机版本，幂等）
bash brain/deploy.sh                # 部署到 /root/albion-pi；换目录：deploy.sh /some/dir 或 ALBION_HOME=…
```

- `deploy.sh` 只管检查器 `tools/check_pi_brain_sync.py` 管的那批：`bin/`、`agent/extensions/`、`agent/APPEND_SYSTEM.md`、`accept.sh`，
  加上 `install.sh`、`deploy.sh` 自己；`agent/.env.example`、`agent/SOUL.example.md` 只在目标里没有时才放。
  README、`start.sh`、`models.json`、`settings.json`、`.env`、`SOUL.md` 等一律不碰（第一次部署这些要自己准备，见下面）。
  覆盖前先把旧文件备份到 `<目标>/backup/deploy-<时间>/`；`DRY_RUN=1` 只列不写。
  **对已有的生产目录先 `DRY_RUN=1`**：仓库版本不一定比生产新（生产上直接改过的文件会被列成"更新"）。
- 部署完它会尝试跑 `tools/check_pi_brain_sync.py`。检查器是 Windows 侧的 Python（里面调 `wsl`），WSL 调不回 Windows 时只提示，
  到 Windows 上跑 `python tools/check_pi_brain_sync.py --prod <目标>` 即可。
- 然后 `cp agent/.env.example agent/.env`、`cp agent/SOUL.example.md agent/SOUL.md`，填完再 `bash start.sh`、`bash accept.sh`。
  `SOUL.example.md` 是骨架：人设各段自己写，「输出格式」一节（开头 `<|ACT:{"emotion":"…"}|>`）是壳依赖的，要保留。

## 没有 Magpie 怎么配

Magpie 只是作者的"模型管理器"：它往 `/root/.pi/agent/settings.json`（和 `models.json`）里写默认模型，脑每轮读到。没有它：

1. 在 `agent/models.json` 的 `providers` 下加你自己的 OpenAI 兼容 provider（`baseUrl`、`apiKey` 写环境变量名或值、`models` 列表），格式照仓库里那份示例。
2. 在 `agent/.env` 写 `ALBION_PROVIDER=<上面 providers 里的键>` 和 `ALBION_MODEL=<模型 id>`，provider 的 key 也写在这里（变量名同 `models.json` 里引用的）。
3. 重启脑（`bash stop.sh && bash start.sh`）。`.env` 里的这两项重启后依然有效，`launch/start_brain.ps1` 和看门狗不用带环境变量。
   一次性覆盖仍可 `ALBION_PROVIDER=… ALBION_MODEL=… bash start.sh`（进程环境优先于 `.env`）。

两项都不写、也没有 Magpie 的设置文件时，兜底 `commandcode/deepseek/deepseek-v4-flash`（需要 `COMMANDCODE_API_KEY`）。有 Magpie 的作者生产机不设这两项，行为不变。
`follow_settings.py` 自检：`python3 tests/pi-brain/check_follow_settings.py`（含 `.env` 有/无变量的用例）。

## 主力模型（2026-10-30 起跟着 magpie 走）

脑用哪个模型不再写死：每轮开跑前 `bin/follow_settings.py` 读 magpie 写给 pi 的默认设置
（`/root/.pi/agent/settings.json` 的 `defaultProvider`/`defaultModel`，路径可用 `ALBION_FOLLOW_SETTINGS` 覆盖），
把该 provider 的整段从同目录 `models.json` 合并进 `agent/models.json`（**只动这一个键**，原子写、LF），
再把最终在用的 `provider/model` 回写 `agent/settings.json` 的 `defaultProvider`/`defaultModel`。
**在 magpie 的「Pi · WSL Ubuntu」那一格换模型，脑下一轮就跟上**；不用改代码、不用重启。
- 优先级：`ALBION_PROVIDER`/`ALBION_MODEL` > magpie 的设置 > 兜底 `commandcode/deepseek/deepseek-v4-flash`
  （magpie 不在时脑照样说话）。
- 换模型时活着的身子先试 Pi 的 RPC `set_model`；pi 只在启动时读一次 `models.json`，还不认识新 provider 就换身子重开
  （新身子照常从活动库播种历史，见 `bin/server.py` 的 `_retarget_session`）。
- 合并时去掉模型级 `api`/`baseUrl` 覆盖，统一成 provider 级 `openai-completions`：整理器（`albion-guard.ts`）的
  context 钩子与「重说」只发 OpenAI 形状的 payload（`/chat/completions`），magpie 给 `antigravity/*` 写的覆盖是
  `google-generative-ai` + `/v1beta`。
- `cli.sh` 也跟着脑走，但**必须显式传 `--model`**：它读同一个 agent 目录 `settings.json` 的
  `defaultProvider`/`defaultModel` 再拼成 `provider/model` 传给 pi —— 续会话（`--session-id`）时 pi 会沿用会话文件里
  记着的旧模型，省略 `--model` 不等于跟设置走（2026-10-30 实测：`albion-cli` 还停在 `grokpool/grok-chat-fast`，回出乱码英文）。
  `delegate.ts` 的子进程带 `--no-session`，不传 `--model`，pi 直接吃那份 `settings.json` 的默认值。
  临时单独换一次仍可 `ALBION_CLI_MODEL` / `ALBION_DELEGATE_MODEL`。
- TTS 翻译（`tts/irodori/bridge.py`）也跟：问脑 `http://127.0.0.1:8650/health` 的 `provider`/`model`（缓存 60 秒），
  `magpie` → 首腿打本机 magpie（思考压到 `low`，实测一句 2.4~2.8s；不压是 16s），`commandcode` → 首腿走现有
  Command Code 腿 + 那个模型。原来的 DeepSeek 官方、Command Code、codex 三条退路顺序不变；`TTS_BRAIN_ROUTE=0` 关掉首腿。
- 回 grok / 手动指定一次：`ALBION_PROVIDER=grokpool ALBION_MODEL=grok-4.7 bash start.sh`（这次覆盖也会写回 `settings.json`）。
  另起一只对比脑：再加 `ALBION_BRAIN_PORT=8651`。
- 手动备选：`commandcode/reflection/beam:free`（已在 `agent/models.json` 注册，免费线路可能 429，用前查 `/models` 与
  [官方模型目录](https://commandcode.ai/models)）；没有自动切换。
- 不要用仓库的 `agent/models.json` 示例覆盖生产文件，生产文件保留私密 provider 与现有 key 引用。
  注意：生产 `models.json` 里 commandcode 的 `apiKey` 是明文写死的，pi 实际用它而不是 `.env`；换 key 要
  `agent/.env` 和 `agent/models.json` 两处一起改，只改 `.env` 脑照样报额度不足。magpie 那段是本地占位 `magpie`。
- 同一套真实接口实测（2026-10-14）：自己造工具 DS 一次做成、grok-4.7 五次全失败（复读、谎称造好、写出加载不了的扩展）；
  聊天 DS 更自然、更快。换 magpie 的 Gemini 3.8 Flash 后（10-29 实测）聊天/工具/翻译都 200，但思考贵：不压提示词一句 16s，
  压到 `reasoning_effort: low` 是 2.4~2.8s（脑的整理器钩子不受影响，见 `albion-guard.ts`）。

- 说出口之前只过一道**整理器**（`albion-guard.ts`，2026-10-16 起；原来的严格裁判、同路由重生成、哨兵、`ALBION_DS_PATCHES` 开关都撤了）：
  ACT 补竖线、心声标签收回括号、英文拒绝稿只留表情、泄漏的工具调用标记截掉、陪伴/等候姿态句每 8 条回复最多放过一次
  （`ALBION_POSTURE_EVERY` 可调）；闭合括号里的自身动作/身体反应每 6 条最多留一次，超额只删命中的小句、保留念头
  （`ALBION_GESTURE_EVERY` 可调，整条删空则原样留并用掉额度）。两种额度各自记在 `state/voice.json`，不叫模型、不发请求。
- 兜底只剩一条：他说了话她却 0 字 → 在同一会话补一句只用文字的【接着说完】（英文拒绝被换成表情后也走这条）。
  原来的【想起来去做】（去玩没玩、说做好了没做）已撤：做不做由她；改没改文件，下一轮约定里会告诉她账本上的事实。
- 她写坏的扩展换身子前自检时先跳过、记进 `state/broken-extensions.json`，下一轮约定里告诉她去修。
- **桌面会话她还在说的时候他又补一句**（2026-11-03）：第二个请求不再排队等整轮，宿主用 Pi RPC `steer`
  把这句插进正在跑的那一轮（前缀「（你还在说的时候他又说：）」），她在这轮里一并回；这一轮的交付按顺序
  拼起每一段定稿正文（`pi_client.reply_text`），第二个请求等这轮结束回空。steer 不成（Pi 已 idle/正在收尾、
  RPC 报错）就照旧排队开新一轮，消息不丢。只做桌面 `albion-desktop`；门铃、平台通知、`role=tool` 续轮都不插。
  活动库照常把插进来的那句记成他的话（`albion-turn.ts` 去掉那层说明）。
- **跨渠道近况**（2026-11-03）：桌面会话每轮从活动库取 `albion-discord` 里 Discord 群的通知行（谁说了什么）
  和她自己用 `send_message(channel=discord_group)` 发的内容；`albion-discord` 会话取桌面最近的原话、
  TG/Discord 私信通知行、她的回复正文和私信发送。只给上次注入之后、6 小时内的新增，最多 12 行/1500 字，
  每行带北京时间与渠道名；水位分文件 `state/cross-channel-desktop.json` / `state/cross-channel-discord.json`
  （首次读到旧的 `state/cross-channel.json` 会按本会话的键迁移）。**悄悄话一律不进桌面**（他看得见，但她们以为他看不到）；
  `send_message` 的正文现在会记进活动库的 tool 行（JSON `{channel,text}`），跨渠道才拿得到她说过什么。

## 她自己长手脚

- 扩展放 `agent/extensions/*.ts`。宿主每收一次请求都会比对目录签名，**文件一变就换一具身子**
  。壳每轮只发最新一句，新身子第一轮由宿主从活动库 `state/state.db` 接上这个会话两天内最近 40 条对话
  （2026-10-16 起；之前换身子就忘了刚才在聊什么）。换身子是整批加载，一个坏扩展会让整批起不来，
  所以存完先跑 `node bin/check_extensions.ts`。
- "怎么给自己加工具"写在技能 `agent/skills/albion-build-tool/SKILL.md`（要用时才读；技能文件改了也算进换身子的签名）；
  更细的三条路与纪律见 `给她加本事.md`。
- `server.py` 里 `TOOLS = ["*"]`：**必须显式给**——RPC/print 模式不给 `--tools` 就一件工具都没有；
  用 `*` 通配才能让内置四件和她自己注册的扩展工具同时可用。
- **探针/验收/自检会话只给只读工具**（2026-10-30）：判断沿用 `is_ephemeral_session`（`agent/real-sessions.json`），
  这类会话用 `READONLY_TOOLS`（`read`/`grep`/`find`/`ls`、查记忆的 `openviking_*`、查网页、农历），
  不给 `write`/`edit`/`bash`/`delegate`/`next_wake`/`notebook`/`draw`/`todo` 等；真实会话仍是 `*`，一件不少。
  起因：回放真实对话时探针会话拿到全套工具，被旧台词「坏了你就自己修一修吧」带着改写了生产 `bin/self_ring.py`。
- **一轮卡住先 abort、再换身子**（2026-10-30）：一轮跑满 420s 没定稿时，宿主发 RPC `abort`（Pi 回 abort 时已 idle，
  `bin/pi_client.py` 按命令名等这个没有 id 的回应）；abort 不成或仍忙就关掉这具身子，下一个请求重开并从活动库播种。
  原来超时后同一会话每个请求都秒回 `prompt rejected: pi stayed busy`，直到重启脑。

## 回退

**退回守卫精简之前（2026-10-16 之前）的脑**：从仓库提交 `5f755e83` 取 `brain/` 的
`bin/`、`agent/extensions/`、`agent/APPEND_SYSTEM.md`、`accept.sh` 覆盖回 `/root/albion-pi/` 对应位置
（先在独立目录 `git worktree add` 出来核对），再 `bash stop.sh && bash start.sh`。`state/voice.json` 留着无害。
壳端要配套退回同一提交的 `shell/`，否则旧脑的 `[[LOOK]]`/壳内主动没有对应的壳。

**回到 Hermes**：Hermes 已于 2026-10-07 从 WSL 删除，仓库里的回退快照（`integrations/hermes/`、`_启动脑WSL.ps1`）
也于 2026-10-16 撤掉；要回去从 `git show 5f755e83:integrations/hermes/README.md` 起照做，
私人 profile 在 `archive/hermes-profile-20261007/`。

## 她自己的 node

`/usr/local/bin/node` 原来指向 Hermes 自带的那份；Hermes 删掉后已把整份 node 搬到
`runtime/node/`（229M），`node`/`npm`/`npx` 三个符号链接指过去——**别删 `runtime/`，Pi 靠它跑**。
