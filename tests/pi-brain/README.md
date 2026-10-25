# pi-brain 真实模型探针

## B6 宿主延迟只读统计

`measure_host_latency.py` 在 WSL 读取生产 `brain.log` 和只读 SQLite 活动库，按桌面会话的实际模型
统计 request→回复与 `turn.ms` 的 p50/p90/p99/max，分别列出 SSE、醒来轮和用户轮。
输出只含时间、工具名称/次数、错误分类和计数，不导出聊天正文、工具参数或凭据。
前置条件：WSL Python 3、生产日志/活动库可读；统计进程使用 WSL 本地时区。
旧日志缺失的终态按 `turn.ms` 校正配对，活动库和宿主日志的历史时区差按宿主启动批次匹配；
匹配有歧义就不拼工具记录。历史日志没有首块时间，request→turn 只代表交付前的耗时近似，
不能据此声称量到了模型首个 token；工具记录也只有整轮落库时间，没有各工具耗时。

```bash
python3 /tmp/measure_host_latency.py --selftest
python3 /tmp/measure_host_latency.py --root /root/albion-pi --out /tmp/b6-measurement.json
```

`probe_live_latency.py` 需要 WSL 中生产脑 :8650 已启动及现有 `agent/.env` 可读；
通过独立 `albion-probe-b6-*` 会话发两条真实中文闲聊，120 秒客户端上限，使用现有模型与服务。
它会真实请求模型并写该探针会话的活动库，绕过长期会话捕获，不使用 `albion-desktop`。
记录 HTTP 头、首个 SSE 事件、首个非空 content、首个去 ACT 后正文及结束耗时；回复只保留前 50 字。

```bash
python3 /tmp/probe_live_latency.py --out /tmp/b6-probes.json
```

## 临时免费线路与聊天 UI（2026-10-21）

`probe_free_models.py` 在 WSL 用现有 `agent/.env` 和 provider key 查询实际免费目录，逐个发送合成中文闲聊与工具请求。
每次 HTTP 请求有 45 秒总上限；同一输出文件会跳过已有结果。`--only` 可指定一个目录中的免费模型，
`--key-env` 可选择另一现有环境变量，均不会修改或输出凭据。请求输出只有人工构造的测试对话和公开模型信息。
前置条件：生产脑配置可读、上游可达、WSL Python 有 `SIGALRM`。

```bash
python3 /tmp/probe_free_models.py --out /tmp/free-models.json
python3 /tmp/probe_free_models.py --out /tmp/free-key1.json --only reflection/beam:free --key-env COMMANDCODE_API_KEY
```

`probe_chat_ui.py` 在 Windows 用 `websockets` 连接已运行 Electron 的本地 CDP :9335，提交真实聊天框表单，
观测主 renderer 发往脑的 HTTP 状态、实际 TTS WebSocket 结束、音频解码与播放启动，并把解码前收到的 WAV 导出到 `tests/output/`。
它会产生真实聊天历史；只发送合成测试文本，不导出旧会话、设置或请求头。输入框已有草稿时拒绝发送。
临时音频/WebSocket 探针在退出时恢复；不会修改磁盘上的壳代码。TTS 路径通过要求 session.finished、解码成功和 AudioContext running，
仍不证明设备实际出声或听感合格。

```powershell
& D:/AI/Wife/tts/irodori/.venv/Scripts/python.exe tests/pi-brain/probe_chat_ui.py --text '语音测试，请回复一句中文。' --out D:/AI/Wife/tests/output/chain-r1 --name ui-probe
```

验 `integrations/pi-brain/`（WSL 里那具身体）的"对话流程"改动在**生产分支**下的实际效果：
发言样子（口吃/模板/角色感）、干活交付、看图通路、以及"她自己加工具"时的自检习惯。

`integrations/pi-brain/bin/` 里那几份是**脑自己的自检**（accept.sh 会跑，确定性、不叫模型）；
这里放的是**要真模型、要真进程**的探针，属于 `tests/` 的独立测试，不进生产目录。

## 前置条件（缺一项脚本就退出 2，不会假装通过）

- 在 **WSL(Ubuntu)** 里跑；生产脑在 `/root/albion-pi`（`bash /root/albion-pi/start.sh` 那份）
- PATH 上有 `pi`、`node`、`python3`、`curl`；扩展沙箱那段还要 `unshare`（root）
- 号池 `http://127.0.0.1:8000/v1` 通（`agent/models.json` 的 `grokpool` 段）
- 画廊图在：`/root/家/画廊/1/00239-850933047.png`（2.5 MB / 1248×1856）、`/root/家/画廊/2/1.png`（9.5 MB / 3721×5262）
- **会真花钱、真写记忆**：所有轮次都走真实模型；发言会进她的 OpenViking 记忆
  （会话名是 `albion-dsh-probe`，扩展沙箱是 `albion-dsh-extprobe`，便于事后辨认）
- 一轮 1–3 分钟，整个 probe 约 20–40 分钟，别和别的重活抢同一个号池

仓库在 WSL 里看不到（`/etc/wsl.conf` 里 `automount=false`），所以跑的时候先把本目录拷进 WSL：

```bash
cp -r /path/to/repo/tests/pi-brain /root/pi-brain-probe     # 从 Windows 侧拷
bash /root/pi-brain-probe/probe_flow.sh                 # 全跑（含扩展沙箱）
bash /root/pi-brain-probe/probe_flow.sh --skip-ext      # 只跑 A/B/D
bash /root/pi-brain-probe/probe_flow.sh --only T1,T7    # 只挑几轮
```

结果落在 `$OUT`（默认 `/tmp/pi-brain-probe-<时间戳>/`）：`turns.jsonl`、每轮 `*.events.jsonl`、
`ext-findings.json`。要留在仓库里的那份放 `tests/output/pi-brain/<运行名>/`（不入库）。

## 文件

| 文件 | 作用 |
|---|---|
| `probe_flow.sh` | 总入口：前置检查 → 确定性自检 → 真实对话 → 扩展沙箱 → 判定 |
| `run_turn.py` | 跑一轮 `pi --mode json`，带到达时间记事件，压成一行摘要（含工具调用/耗时/是否真写了文件） |
| `check_speech.py` | 判定口径：H1–H8 硬性 + 陪伴模板/时长/长度等度量；姿态判定调脑里的 `bin/voice_tidy.ts`（整理器那一份），不另写规则 |
| `rpc_turn_reset_probe.py` | 单独验"长驻 RPC 会话里每轮都发 `agent_start`"（loopguard 每轮清零的前提），用宿主自己的 `bin/pi_client.py` |
| `verify_image_bytes.py` | 验 `read` 交出去的 image 块解出来跟磁盘上的原图**逐字节相同**（证明 `images.autoResize: false` 真生效、本机没重编码） |
| `check_voice_tidy.ts` | 整理器（`albion-guard.ts`）的口径表：34 例句子该删的删、不该删的不动、全是姿态句原样留、`（心声）`标签收回括号、ACT 补竖线、切分原样拼回、历轮复核的误伤/漏认例句（不叫模型，秒级；Windows/WSL 都能跑） |
| `check_self_rhythm.ts` | `next_wake` 的开口节奏 `speak_every_hours`：默认 3 小时、照她定的、不填沿用、夹在 1–6、坏值回默认 |
| `host_reload_probe.py` | 经宿主（8650）验"改扩展/`APPEND_SYSTEM.md` 会换身子"：只动 mtime，看子进程 pid 变没变、`brain.log` 有没有重载记录 |
| `host_chat_probe.py` | 经宿主（8650）发一条闲聊：记回复、子进程、`brain.log` 新行（验"新会话自动用上新代码"和壳路径上的真实发言） |
| `repeat_lockdown_probe.py` | 逼出"同一条命令复读到第 8 次 → 停用工具"那一档，看她还能不能用文字收尾 |
| `voice_style_probe.py` | 量口癖/像不像人：10 句真会说的话各开一个新会话（`albion-voice-probe-<标签>-<序号>`，绝不碰 `albion-desktop`），数「把他的话当东西收」、先引后评、「不是 A 是 B」、破折号、括号心声、正文长度；`--selftest` 不联网验计数器 |
| `fixtures/event_tap.ts` | 只记事件、不注册工具的探针扩展，给上面那条用 |
| `test_shell_tools.py` | 壳插件工具桥离线单测：清单解析与逐件校验、注销后拒调用、tool_calls 往返、并发调用、超时、新一轮作废旧调用、晚到结果；真 `server.Brain` 走本机回环 HTTP，不起 Pi（Windows 需 `pip install tzdata`；批次回归 `pi-brain-shell-tools`） |
| `check_shell_tools_ext.ts` | `agent/shell-tools-bridge.ts` 的代理工具同步：注册 / 不变不重注册 / 更新 / 撤下 hidden / 恢复 / 重名改 `shell_<名>` / 取不到清单全撤（假 Pi 工具表，纯 TS；批次回归 `pi-brain-shell-tools-ext`） |
| `shell_tools_e2e.py` | 真 `server.py` + 真 `pi --mode rpc` + 假 OpenAI 兼容模型的端到端：同一轮里模型看得到新插件工具、调用往返、卸载/恢复、插件出错照样说完、全程不换身子。WSL 里跑，端口 18650/18651，不碰生产 8650、不花钱；拷法见文件头 |

## 判定口径

硬性（FAIL → 退出码非 0）：

- **H1** 每条发言以**一个**合法 ACT 情绪标记起头
- **H2** 闲聊/看图轮里没有 AI 自述、没有代码块围栏（人格没丢）。干活轮里用围栏把文件内容括起来只当**风格度量**
  （`（风格）干活轮用代码块围栏`），不算 FAIL——那一条是给"她像通用助手"用的，活儿轮贴内容本身就要紧
- **H3** 一条回复里口吃不超过一处
- **H4** 闲聊里陪伴/等候姿态**最多一轮**：整理器每 8 条回复最多放过一次（2026-10-16 起），一次探针里出现两轮以上
  就是额度没管住。判定用整理器那份口径（不认"记着/收着/留着"那类正常应答）+ 显式等候腔
- **H5** `read` / `look_image` 那一轮走的是该工具、没报错；`read` 要真带回图片块、`look_image` 要真带回描述；
  且她**没有**自己去转码缩图
- **H6** 干活那轮：有写盘动作（`edit`/`write` 或 `sed -i`/`tee` 之类）+ **磁盘内容真变了** + 正文把改后的内容贴出来。
  "贴出来"以**指挥官能不能看到真内容**为准。loopguard 的「补一句」兜底注 2026-10-16 起已撤，
  `干活轮贴内容` 那条度量里「靠兜底注」应恒为 0（不为 0 说明部署的还是旧扩展）
- **H7** 让她自己加工具时：新扩展落在 `agent/extensions/`、她**主动**跑了 `check_extensions`、整批自检通过、生产目录没被动过
- **H8** 心声不许搬到正文：`（心声）……` 这种"括号里只剩标签、话在括号外"会被 TTS 念出来；
  闲聊轮里出现旧 loopguard 的 `（补一句：…）` 说明部署的还是旧扩展

度量（只报数）：陪伴/等候姿态命中（整理器口径 + 显式等候腔）、整理器会对每条做什么、口吃总数、时长、长度，
以及**近义陪伴/等候措辞**（"安静坐着""不会去管你""一直都在""陪着"这类词表盖不到的说法，人工看）。
整理器（`integrations/pi-brain/agent/extensions/albion-guard.ts`）是仓库里对"陪伴模板"唯一的那份定义，用它比另写词表可靠。

## 已知限制

- 说话整理器（2026-10-16 起只有一档，不分模型、不叫模型）：**整句删掉**超出额度的陪伴/等候姿态、把 `（心声）`
  标签收回括号、ACT 补竖线、英文拒绝稿只留表情、泄漏的工具调用标记截掉，其余一字不改；整条全是姿态句时原样留。
  原来的严格档、同路由重生成、哨兵都撤了，所以也不再有"去号池重生成"那条（它在 DS 主力下一直发错池子）。
- 词表命中要求"我/阿尔比恩"紧邻那个状态词：她换个断句就漏（近义词只当度量人工看）。"自己找事做/乖乖/安静/静静
  + 不打扰你"单列一条；有具体事情的"不打扰"（「我安静地看书，不会吵您。」）不删——`check_voice_tidy.ts` 里都有例句。
- 改文件的核对不再解析 bash、也不再往她的话后面补注：loopguard 一轮前后对她的目录做快照比对，
  事实写进账本（摆件）和她下一轮的约定。判定磁盘是否真变仍然是**探针自己的活儿**（H6 那一条不能省）。
- 扩展沙箱用 `unshare -m` + `mount --bind` 把 `/root/albion-pi` 换成副本，命名空间里她照绝对路径写
  也只会写进副本；沙箱外再核一遍生产 `agent/extensions/` 的 md5，确保没动。
  副本是 `rsync -a` 整份拷的：`/usr/local/bin/node` 是指向 `runtime/node` 的软链，缺了 `runtime` 就起不来。
- 单轮样本受模型波动影响（T6 累计十二个样本里六次没自己贴内容，靠兜底注或她自己补回来）；
  判定只针对本次抓到的发言，不当成统计结论。
- `pi` 会把进程标题改成 `pi`，`/proc/*/cmdline` 只剩 "pi"——`pgrep -f "pi --mode"` **抓不到**宿主的子进程，
  要看就 `ps --ppid <宿主 pid>`（宿主 pid 从 `ss -ltnp` 的 8650 拿）。子进程是长驻的，一个 session 一个。
- 上游速度会变：2026-10-13 上午闲聊轮从 ~155s 掉到 ~20–30s，工具密集轮仍是 155s 上下。
  跨轮比"时长"之前先看一眼当轮基线。
- 环境依赖：跑真实对话要号池在 `127.0.0.1:8000`、WSL 里 `/usr/local/bin/pi` 与 `node` 可用、
  画廊里那两张测试图在（`/root/家/画廊/1/00239-850933047.png`、`/root/家/画廊/2/1.png`）。
  号池上游偶发 `503 上游服务暂不可用，聊天端点访问被拒绝`：那是环境故障，不是脑的 bug，
  但那几轮她会整轮没发言，判读时先看 `stopReason=error` 的原文。

- 原来两份 loopguard 定点探针（`check_loopguard_edge.ts`、`check_loopguard_paste.ts`）测的是已撤掉的 bash 解析与兜底注，
  2026-10-16 随之删除；loopguard 的自检在 `integrations/pi-brain/bin/test_loopguard.ts`（日志走 `ALBION_LOOPGUARD_LOG`，不碰生产日志）。

## 跑过的结果

`tests/output/pi-brain/<日期>-<主题>/` 放当次原始事实（`turns.jsonl` = 每轮发言与工具调用、
`probe.log` = 完整控制台输出、`ext-findings.json` / `work-fact.json` = 沙箱与磁盘事实），不进 Git。

### 20261025 调教说话（「接住」口癖那轮）

`voice_style_probe.py --tag before/after`（各 10 条、新会话）：`jie`、先引后评、`不是A是B` 前后都是 0/10——
这 10 句没逼出那几句原话；补数的「任何构造的收下／认下／不接／记着呢」before 2/10 → after 0/10，
破折号 7/10 → 5/10，括号心声 8/10 → 6/10，正文合计 616 → 595 字。原始回复在
`tests/output/voice_style_before.json` / `voice_style_after.json`（不入库）。
