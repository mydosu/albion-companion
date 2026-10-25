> 已失效（2026-10-24 归档）：2026-10-07 的一次性迁移报告，结论已并入 `integrations/pi-brain/README.md`；现行看后者。

# 报告：把她的脑搬进 Pi（2026-10-07）

对应任务书 `任务书-把她的脑搬进Pi-20261007.md`。结论一句话：
**宿主那一层已经搬空，脑的语义（人格、每轮约定、回忆、守卫、活动库、模型通路）全部落在 Pi 自己的文件里；
直跑 `pi` 得到的就是她本人，不需要我的宿主参与。** 宿主只剩一件不可替代的活：把两个壳说的
OpenAI-HTTP 翻成 Pi 的 RPC。

---

## 一、现在哪一块住在哪

| 这一块 | 落在哪个文件 | 怎么生效 |
|---|---|---|
| 人格（她是谁、什么脾气） | `agent/SOUL.md` → 启动时生成 `agent/SYSTEM.md` | 重启脑 |
| 长期增补：边界、怎么给自己加本事 | `agent/APPEND_SYSTEM.md` | 下一轮 |
| 每轮说话约定 + 回忆注入 + 活动库 | `agent/extensions/albion-turn.ts`（292 行） | 存盘即换身子 |
| 说话守卫（验收 + 同路由重生成 + 三稿不过如实报失败） | `agent/extensions/albion-guard.ts`（323 行） | 存盘即换身子 |
| 模型通路（本机转发，绕开上游建连超时） | `agent/extensions/albion-relay.ts`（91 行） | 存盘即换身子 |
| 记忆读写（OpenViking） | `agent/extensions/viking.ts` ↔ `bin/memory.py` | 存盘即换身子 |
| 派活给子进程 | `agent/extensions/delegate.ts` | 存盘即换身子 |
| 她自己造的工具 | `agent/extensions/websearch.ts` 等（她自己可加新文件） | 存盘即换身子 |
| 上哪个模型/上游 | `agent/models.json` | 重启脑 |
| **宿主** | `bin/server.py`（381 行，原 648 行） | — |

宿主现在只做协议转接与身子管理：OpenAI-HTTP(:8650) ↔ 常驻 `pi --mode rpc`；会话映射、历史播种、
扩展热重载（目录签名变了就换一具新身子，历史由壳每轮重发，不会失忆）、SSE 按句切块、
以及**把守卫的哨兵变成 500**（拒绝就是不交付，绝不把被否的稿子当成功返回）。
它不再碰人格、约定、回忆、守卫、模型通路一个字。

**判据（任务书第 1 条的原话："直接连 Pi 就该是她"）**：
`pi -p "你好，用你自己的方式跟我打个招呼。" --no-session --tools "*" -e agent/extensions/*.ts`
→ `<|ACT:{"emotion":"happy"}|> 指挥官，您好。阿尔比恩今天是这样一副样子——早上在厨房把牛奶…`
（自己的记号、自己的口气；不是通用助手。）

## 二、宿主剩下的这一件为什么去不掉

两个壳都只说 OpenAI 兼容 HTTP：桌宠壳把它当 provider 打 `/v1/chat/completions`（`stream:true`、带 4 个 tools），
同席的 Rust 客户端（`albion_openai.rs`）同样打这个端点取模型目录与流式正文。Pi 说的是 RPC（JSONL 子进程）。
**翻译必须有人做**——这是唯一真正不可替代的部分，也是宿主现在唯一的内容。
谁想直连也行：直跑 `pi` 就是她（上面那条判据）。

## 三、`:8798` 的实测结论：留，但搬进 Pi 自己

- **不是冗余**：上游 `api.commandcode.ai` 在本机建连要 ~10s，而 Node/undici 默认建连超时正好 10s
  ⇒ 直连稳定 `Request timed out.`（当天实测直连 3/3 失败、走本机转发 3/3 成功）。这条转发去不得。
- **但它现在是 Pi 的扩展** `albion-relay.ts`：把脑停掉、8650 与 8798 都空着，再直跑 `pi`，
  是 **pi 进程自己**把 8798 监听起来并完成了整轮对话（实测 `ss -lntp` → `users:(("pi",pid=36911,fd=21))`），
  进程退出即释放。宿主不再碰模型通路。
- 端口被别人（比如一次性的 `pi -p`）临时占着时，它会每 5 秒重试，不会出现"端口有人占着却没人转发"的死局。

## 四、两个壳都验过了（任务书第 3 条）

| 谁 | 怎么验的 | 结果 |
|---|---|---|
| 桌宠壳 `airi :9335` | 用它自己的调试钩子 `window.__albionPet.send("在吗？一句话就好")`——内部就是壳的 `chat.send`，与语音转写走同一条管线 | 壳收到并上屏：`在的，指挥官。（这道汤的配料，等会儿再琢磨。）`；大屏 caption 页实读同句；脑日志留下 17:04:24 的 Mozilla-UA 请求（`stream:true`、`tools:4`） |
| 同席 `local-agent-hub` | `python scripts/verify_albion_pi.py`（真 Tauri App + CDP，走 UI） | **8/8 passed** |
| 同席 Rust 客户端 | `cargo test --manifest-path src-tauri/Cargo.toml -- --ignored 她的脑` | 1 passed（模型目录 + 真流式正文） |
| Windows 各链路 | `pet/check_all.py` | **7/7 就绪**（脑 :8650／TTS 桥 :9881／流式 :9885／STT :9882／眼 Qwen3VL／取图端 :8790／壳 :9335） |
| 语音链 | 未改动：壳从自己的 store 取正文再送 TTS，端点契约没变 | — |

## 五、可复跑验收（任务书第 4 条）

```bash
bash /root/albion-pi/accept.sh      # 幂等，约 5 分钟；今天是 9/9 PASS
python3 bin/test_guard.py           # 守卫判定逻辑本身：8/8 passed
node bin/test_guard_ext.ts          # 守卫扩展：6/6（不碰网络）
```

`accept.sh` 的九条断言（今天全绿）：

| # | 断言 | 依据 |
|---|---|---|
| 1 | 直跑 `pi` 的回复带她自己的 `<|ACT:…|>` | 真实输出 |
| 2 | 直跑 `pi` 的回复不是通用助手口吻（无 AI 自述、无代码块） | 真实输出 |
| 3 | 本轮注入里带着回忆块 | `state/last-context.json`（每轮覆盖写，可复核） |
| 4 | 本轮注入里带着逐轮说话约定 | 同上 |
| 5 | 守卫与逐轮约定自检 7/7 | 合法稿零裁判调用／缺标记重生成／姿态模板重生成且心声保留／三稿不过留哨兵／基础设施错误留哨兵／带工具消息放行／回忆在前且约定在末 |
| 6 | 守卫判定逻辑 8/8 | 未改动的 `guard.py` 自带测试 |
| 7 | `/v1/models` 仍只报 `albion` | 两个壳的连接判据 |
| 8 | 经宿主 `POST /v1/chat/completions` 回来的也是她 | 真实响应正文 |
| 9 | 活动库最后一条 = 她这一轮真的说出口的正文 | 摆件读的就是它 |

另外当天**真出现过一次守卫拒稿**：验收词"一句话说你在"让她的三稿都被自己的验收员判成"机械在场声明"
→ 哨兵 → 宿主 500 `voice validation failed`，日志在 `logs/guard.log`。这是"如实报失败"的实证，
不是故障（验收词已改成自然句子）。

## 六、改了什么、怎么回退（任务书第 5 条）

**新增**：`agent/extensions/albion-turn.ts`、`albion-guard.ts`、`albion-relay.ts`；`bin/test_guard_ext.ts`；`accept.sh`
**改动**：
- `bin/server.py` 648 → 381 行：删掉约定文案/回忆注入/守卫调用/活动库写入/本机转发，加"哨兵→500"
- `bin/pi_client.py`：交付以 Pi 的**定稿消息**为准（守卫替换的就是它），流式增量只用于早显示
- `bin/guard.py`：加四个单步接口（`--check/--judge-request/--judge-verdict/--repair-request`），判定逻辑一字未动
- `bin/memory.py`：加 `--call recall`

**回退**（要连扩展一起撤，否则会双重注入/双重守卫）：
```bash
cd /root/albion-pi
cp archive/brain-host-20261007/server.py archive/brain-host-20261007/pi_client.py bin/
mv agent/extensions/albion-turn.ts agent/extensions/albion-guard.ts agent/extensions/albion-relay.ts archive/
bash stop.sh && bash start.sh
```
档案（旧宿主原件）在 `/root/albion-pi/archive/brain-host-20261007/`；源码镜像在
`D:/AI/Wife/integrations/pi-brain/`（含扩展、宿主、测试、脚本，可直接 diff）。

## 七、我在这轮里发现并补掉的两处

1. **活动库记的是草稿**：Pi 要等所有 `message_end` 处理完才原地改写消息，所以守卫替换后，
   在 `message_end` 里落库拿到的是**被否掉的首稿**（甚至可能是哨兵）。已改成在 `agent_end` 落库，
   验收第 9 条现在就断言"库里 = 她真说的那句"。
2. **验收里有一条假断言**：原来第 3 条写"干巴稿被守卫纠正过"，其实她只是被加了 ACT 前缀，
   正文仍是"好的"——这条证据不成立。已换成确定性的守卫自检（6/6）+ 未改动的 `guard.py` 8/8。

## 八、给她自己那份

`/root/albion-pi/给她加本事.md`：三条加本事的路（改 `APPEND_SYSTEM.md`／写扩展（两种现成形状）／
扩展 + Python 单步命令）、四条自我改动纪律（一次一件、先读 Pi 文档、外部调用必须有超时且不能搞死这一轮、
别碰守卫判定与协议转接）、以及加完怎么验。

## 九、留着的边角（不影响使用，写给你知道）

- 直跑 `pi` 时的"原始 payload"证据只有 `state/last-context.json`（守卫扩展确实已经挂着
  `before_provider_request` 拿那份 request，需要时可临时加一行落盘）。
- 壳的主动消息在睡觉时段（00:30–09:00）会拉长间隔；早上你回来说一句就恢复。
- 万一一轮被守卫三次否掉，摆件里会显示哨兵字符串（少见；宿主那边是 500，不交付）。

## 十、她"说汤"那件事（2026-10-07 傍晚，指挥官发现）

**现象**：她主动开口和回话里反复出现"一道汤——胡萝卜放不放"。

**来路（照日志还原）**：
1. 16:52 我按验收问了一句"你还记得九月份指挥官买过什么水果吗"——回忆把 09-15/09-18 的
   草莓/冰箱片段拉进了这一轮上下文；
2. 16:53 她就顺口说"阿尔比恩这会儿正琢磨一道汤该放哪几样配料"；
3. 16:57 壳的主动开口又接着说那道汤；17:04 再说一次。
   ⇒ 关键不是她编了一句，而是**她自己的对话线程把它当成实事记住了**，然后自我延续。

**清掉的（都留了档）**：

| 清了什么 | 怎么清 | 存档 |
|---|---|---|
| `state/state.db` 里 id≥133 的 16 条（我那一小时的验收流量 + 三句汤） | 删除并把会话计数重算 | `archive/context-cleanup-20261007/messages-133+.json` |
| 她活上下文里的汤 | 换一具新身子（`touch` 扩展 → 宿主换 pi 子进程），线程清空 | — |
| OpenViking 两条"她动手做饭"的旧记忆（09-15 洗草莓并准备晚饭／09-18 草莓做牛奶提议） | `viking_forget` | `archive/context-cleanup-20261007/删掉的两条记忆.md` |

**没动的**：她自己写下的正确认知（`家中物品自检确认`：外面的杯子、冰箱碰不到）、
关于指挥官的草莓/外卖条目、食谱本与笔记本；`SOUL.md` 里"做饭和家务"是游戏官方台词来的角色设定
（还有那套「晨光里的事故」装扮）——要收窄属于改设定，等你定。

**复查**：现在问她"能下厨吗"，她答"做不了。阿尔比恩没有手，锅铲拿不起来，火也碰不到"；
以"草莓"回忆一轮，块里 `厨房=0 汤=0`，浮上来的是她自己那句"外面的杯子、冰箱，我还是碰不到"。

**这轮撞出来并补掉的两个真 bug**：

1. **并发会 502**：宿主没给会话上锁，而 `pi_client` 把"这一轮结束"认成 `agent_end`
   （实测 `agent_settled` 要等下一轮才发）⇒ 两个壳同时打、或主动消息撞上用户的话，
   后一个会拿到 `brain error: Agent is already processing`。
   修法：会话级锁 + 被拒就退一步重发（那是排队，不是错误）。实测两条并发都 200。
2. （前文第七节已记）活动库落库时机改到 `agent_end`，记的才是她真说出口的那句。

## 十二、2026-10-08：Hermes 后续挂件修改复核

指挥官反馈 Hermes 最近修改 Pi 与挂件后，挂件无法正常工作。核查 HEAD 提交 c6fa904f：只改了 albion-turn 扩展的读文件活动条目与 pet/widget/albion_status_widget.pyw 的显示逻辑，没有删文件。WSL 的 Pi 源与仓库中的 Pi 源逐项 MD5 一致。

挂件只读自检 pet/widget/albion_status_widget.pyw --check 返回 ok；实际 fetch 从 Pi 的 state.db 读出活动数据，文件动作路径和时间值均有值；挂件进程数 1、47654 监听。pet/check_all.py 7/7。另为本次添加 read_file 的路径 JSON 回归自检，确认 read_only + resolved_path，写文件仍用 files_modified。Pi 守卫与扩展检查当前为 9/9 和 9/9。

以上没有重启服务、删档或改桌宠正式会话。界面进程与数据读取链正常；未做屏幕截图验收或实际扬声器播放。

## 十一、2026-10-08：接回 Hermes 末版语气优化

指挥官反馈 Pi 版重新出现不自然表达。排查发现，Pi 已载入同一份 SOUL、逐轮说话约定和姿态模板检查；`state/last-context.json` 也确认最新自然口语与括号心声约定实际进入请求。迁移时漏接的是 Hermes 末版的**守卫调用策略**：Hermes 对普通合格首稿不再额外调用模型裁判（`run_guard(..., judge=None)`），Pi 扩展却每轮都再让同一个 DS 判断“自然不自然”，若它误判就重写合格首稿，最多造成三轮不同稿。

已对齐末版策略：Pi 扩展只运行确定性守卫；出现 ACT 格式、守候模板、未闭括号、虚构触摸等具体问题，才同路由整句重生成并重查。合格首稿原样通过，每条普通回话不再额外发语义裁判请求。心声照常留在括号中显示，模板检查仍覆盖心声内容；拒绝稿与基础设施失败仍按原协议留哨兵，不会退回错误首稿。“水果这类事，敢说的只有这一笔”和“记得一样：……”被判为记忆盘点口吻，整句重生成。

另发现 Pi 将 OpenViking 回忆块接在逐轮说话约定之后，二者落在同一条 user 消息里；检索到的旧话题可能比“只接当前输入”更靠后。已把回忆块移到口语约定之前，并明确无关回忆不主动续成新话题。回忆内容保留，未删记忆。被问过去的事实时，回答事实本身；不把检索过程说成“敢说的只有这一笔”。

改动：`agent/extensions/albion-guard.ts` 移除每轮语义裁判路径；`bin/test_guard_ext.ts` 改为断言普通稿网络调用 0 次、明确违规时只整句重生成；`accept.sh` 更新自检说明。人格、逐轮说话约定、模型和聊天历史未改。存盘后服务器按扩展目录签名在下一请求换 Pi 进程，不需杀正在运行的脑或清除会话。

复跑：node bin/test_guard_ext.ts（9 个确定性场景，不调用网络）和 python3 bin/test_guard.py（9/9）。bash /root/albion-pi/accept.sh 上次真机验收 9/9；本次增加的两类记忆盘点口吻有对应的 guard Python 与 Pi 扩展重生成用例。真机验收使用独立 albion-accept 会话，桌宠原有会话 168 条消息、14 次工具调用不变。真实扬声器试听未做。
## 十三、2026-10-13：对话流程重构这一批的收尾（claude 接力 · r1–r9）

**从哪来**：指挥官把自己与 codex 关于 Pi 版阿尔比恩的对话（`对话.md`，412 KB）交给 claude 接力，原话三条诉求：
① 多模态模型下图片要直传，别再执着本地转码缩图；② 说话方式回正——保留角色感，去掉模板和反复口吃；
③ 把"自己写代码、自己造工具"这条路修通。中途收窄为"只修 A/B/C 三条必修，再复核一轮后收尾"，
复核结论未通过（见下），文档与文件收尾交给 Hermes。

**落在哪些文件**（仓库 HEAD `45defbc5`；批内 14 个提交＝claude 9 ／ dsh 5，27 个文件 +2780/−138）：

| 这一块 | 文件 | 一句话 |
|---|---|---|
| 图片直传 | `agent/extensions/look-image.ts`（+6/−4）、`agent/settings.json` | `images.autoResize:false`，`read` 也交原字节；老注释里"read 本机重编码、≥1024px 永不返回"复核不成立，已改掉，也不再让她用 ffmpeg 缩图（旁证：`tests/pi-brain/verify_image_bytes.py`） |
| 提示词／语气 | `agent/APPEND_SYSTEM.md`（−95/+44，大删）、`agent/extensions/albion-guard.ts` | 模板与陪伴姿态收窄；grok 主力走轻量档 `strip-posture` |
| 对话流程守卫 | `agent/extensions/albion-loopguard.ts`（+429/−17）、`albion-turn.ts`、`bin/guard.py` | 复读／坏工具／没核对写入／没贴真内容，四类兜底 |
| 造工具 | `bin/check_extensions.ts`（新，+46）、`给她加本事.md`、`accept.sh` | 存完先整批加载查一遍，一个坏扩展不会带塌一整批 |
| 自检 | `bin/test_loopguard.ts`（新，+414）、`bin/test_guard.py`、`bin/test_guard_ext.ts` | 确定性，不叫模型 |
| 真模型探针 | `tests/pi-brain/`（dsh 的 `probe_flow.sh`、`check_*.py` 等 11 件探针 + README） | 要真模型真进程，生产目录一个字节不动 |

**r9 这一轮动了什么**：只有两个文件——`albion-loopguard.ts` 与 `bin/test_loopguard.ts`（提交 `45defbc5`），
按复核单只修 A、B、C：

- **A** 数字紧贴 `>` 的写入（`echo 2024>note.txt`）原来漏判；改成只排除独立成词的 fd 2（`make 2>err.log`）。
- **B**"改了主意／改了口／改天"这类不算改文件。
- **C** 只读的 `grep` 里出现 `writeFileSync` 不算写：改成按命令分段看，写字样必须落在 node／python 那一段自己的代码里。

**复核结论：未通过。** A／B／C 的主要场景都修好了，但这次改动引入两处回归。我收尾时用收进仓库的
`tests/pi-brain/check_loopguard_edge.ts` 在现网那份上自己跑过一遍——47 条计分项过 46、失败 1 条（B14）；
另有 11 条只记现象的，其中 R1–R5、B9、B10 仍是坏的。两处回归：

1. **前缀词一挡，写文件就认不出来了**（C 的副作用）。`sudo sed -i …`、`timeout 20 node -e "…writeFileSync…"`、
   `FOO=1 node -e …`、`if true; then sed -i …; fi`、`(sed -i …)`：上一版都算写入，这一版不算。
   后果是她真写了文件、说"改好了"，反被标"没有真的写过任何文件"。原因是按段判断只看段首那个词；
   修法是判断前先跳掉 `sudo`／`env`／`timeout`／`then`／`do`／`VAR=值` 这类前缀。
2. **真在说改文件却被漏掉**（B 的副作用）。"我已经改变了 note.txt 的第一行"、"我改了计划书里的标题"：
   上一版会标"没有真的写过"，这一版不标了。原因是排除"改了主意／改了计划"时排得太宽；
   修法是只排除完整搭配（"改了计划"后面紧跟句末标点或逗号）。
3. 附带一条：B14（有工具、写了没读回，她说的是"我改了主意，不改了"）仍会多追加一句"还没读回核对过"。

**按指挥官决定本轮不修的**（状态不变）：D 复读计数被一次写文件清零；E 目标紧贴重定向 `f>log`；
F 参数顺序不同不算同一条；G `mv` 到无扩展名的目标被当目录；H `$((1 << n))` 被当 heredoc；I `~/` 没展开。
观察项：`sed --in-place`、`python3 -c` 里的 `open(…,'w')`、`bash -c "sed -i …"` 认不出；
`.png`／`.docx`／`.db` 不在认可扩展名里；"已删掉""换成…了"不触发。没跑过真实模型对话。

**收尾这轮做了什么（只收尾，没碰生产）**

- 核对现网与仓库：`/root/albion-pi` 下批内改过的 14 个文件与仓库逐字节一致（仓库是 CRLF 检出，按 LF 归一后比 MD5）。
- 独立复跑 `tests/pi-brain/check_loopguard_edge.ts`，打出上面那两处回归（自己的实测，不是转述复核单）。
- `python tools/check_repository.py`：5231 个文件，0 个问题。
- 把 r9 复核用的边界用例从 WSL `/tmp/verify-r9/edge/` 收进仓库 `tests/pi-brain/check_loopguard_edge.ts`：
  复核单当时说它在仓库 `_tmp/verify-r9/`，**仓库里并没有这个路径**；不收进来，那批证据就随 `/tmp` 没了。
- 没有重跑 claude／dsh 的自检：扩展的日志路径写死成 `/root/albion-pi/logs/loopguard.log`，
  跑一次就往生产日志里灌一批合成行（现在那份 1796 行里混着 r1–r9 各轮探针留下的行）。
  要根治得让日志路径能被环境变量盖掉——那是生产代码的改动，留给实现方。
- 没有重启脑、没有动 `state/`、没有跑真模型。
- 本机没装 gitleaks（`which gitleaks` 找不到），AGENTS.md 里"提交前扫暂存区"这一步这轮没做；
  这次改动只有文档和一个测试脚本，没有任何凭据。

**回退**（要退到 r9 改前）：

```bash
cd /root/albion-pi
cp archive/claude-20261013-r9/agent/extensions/albion-loopguard.ts agent/extensions/
cp archive/claude-20261013-r9/bin/test_loopguard.ts bin/
```

扩展一变，宿主下一个请求就换身子，不用重启脑。

**归档现状**：`archive/claude-20261013/`（r1 改前的现网原样，按改前 MD5 重建，说明见其 `说明.md`）、
`archive/claude-20261013-r2/` … `-r9/`（各轮改前 `cp -p` 的原件）；验证草稿只在 WSL `/tmp/verify-r7|r8|r9`，
不入库、仓库侧不留双份。
## 十四、2026-10-14：她自己看画面、自己安排时间、改文件以事实为准（r10–r36）+ 收尾

**从哪来**：指挥官 10-13 把复读守卫那批「全权丢给claude修」，随后又加「不要留尾巴，能修尽修，
不要每次都要由我来决定」。于是先有一轮把上节复核报的两处回归、B14 与 D–I 六条一起修掉，
并按上节收尾时的建议把守卫日志路径改成可用环境变量覆盖（提交 `80c96bf5`，2 个文件）；
接着转入这批新功能（提交 `74232b26`，20 个文件 +2541/−96）：她要能自己看画面、自己安排什么时候醒，
说「改了文件」必须对得上事实。

**这批做了什么**（都已上线）

| 方面 | 改动 | 主要文件 |
|---|---|---|
| 改文件以事实为准 | 写没写成看写前写后快照；回复补注只陈述事实；事实账本供摆件记「最近动作」 | `agent/extensions/albion-loopguard.ts`、`albion-turn.ts`、`pet/widget/albion_status_widget.pyw` |
| 她自己的眼睛 | 新工具 `look`：his/mine 屏幕原图交主模型；cam 只交局域网本地视觉模型转文字 | `agent/extensions/albion-look.ts`（新） |
| 她自己安排时间 | 新工具 `next_wake`（定下次醒来）、`notebook`（心事本） | `agent/extensions/albion-self.ts`（新） |
| 醒来循环 | 宿主门铃 `GET /albion/wake`；硬底线：北京时间 00:30–09:00 不醒、最小间隔 10 分钟、他说话 5 分钟内不醒、每小时最多主动说 3 句、不回就退避、只认 `albion-desktop` 会话 | `bin/wake.py`（新）、`bin/server.py` |
| 每轮约定 | 末尾加「我的处境」：他在电脑前即在她身边；碰不到/抱不到/没画面就别说看见 | `albion-turn.ts` |
| 守卫 | 姿态口径补「这么…」「等着…指令」；grok 轻量档认英文拒绝稿→重生成一次→仍拒绝则静默 | `bin/guard.py`、`albion-guard.ts` |
| 壳 | 屏幕截图原图作附件给脑；门铃每 30 秒问脑，开着时旧的壳定时器停用（`settings/albion/self-wake`='0' 可退回；**2026-10-16 已撤**，退路见 tag `albion-pre-a6-20261016`） | `shell/packages/stage-ui/src/albion-eyes.ts`、`albion-pet.ts` |
| TTS 翻译 | Command Code key 额度用完冷却 30 分钟跳过；错误体脱敏入日志 | `tts/irodori/bridge.py` |
| 人设（只在生产） | `SOUL.md` 加「我想要的」、改「我看得见什么」；去掉会被 grok 判为越狱的对抗措辞，R18 段原样保留 | `/root/albion-pi/agent/SOUL.md`（**不进仓库**） |
| 主力机眼睛 | 启动脚本先找真 python.exe，只剩一个进程；采集口令已轮换 | 主力机 `D:\Albion-eye\eyes\启动采集端.bat` |

**提交与部署**

- 源码：`74232b26` 已在 `main` 上，工作树干净；未 push（`main` 领先 `origin/main` 21 个提交）。
- 生产：脑在 WSL `/root/albion-pi`（已重启，新代码生效）；壳已重建并替换 `dist\win-unpacked`。
- 收尾核对：`integrations/pi-brain/` 下这批动过的 **13 个文件与现网逐字节一致**
  （仓库是 CRLF 检出，按 LF 归一后比 MD5；例：`agent/extensions/albion-loopguard.ts` = `d130c069…`、
  `bin/wake.py` = `71d8b53f…`、`bin/server.py` = `985c9c57…`）。

**验证**（下面四条是我收尾时自己复跑的确定性自检，不是转述）

- `node bin/check_extensions.ts`：13 个扩展、9 件工具都能加载（look, next_wake, notebook, delegate,
  grab_page, grok_web_search, look_image, page_outline, web_search）。
- `python3 bin/test_guard.py`：13/13。
- `ALBION_LOOPGUARD_LOG=/tmp/… node bin/test_loopguard.ts`：32 件事通过。
- `node bin/test_guard_ext.ts`：A–L 全过（含 grok 轻量档的英文拒绝稿、自主醒来轮自己的约定）。
- 跑这四项前后，生产 `logs/loopguard.log` 2252 行、`logs/guard.log` 137 行都没变——自检的日志被指到了 `/tmp`。
- 其余是改动方跑过的：`accept.sh` 11/11、醒来→look→说话→next_wake 真机链路、壳 typecheck 与 albion vitest 39、
  TTS 单测 224、`check_repository` + Gitleaks 8.30.1 暂存区 0 发现。真机链路与真模型我没有重跑。

**已知小尾巴（记录，不影响使用）**

- 她醒来后有时仍靠宿主补问才定下次时间。
- 没收到画面时，个别心声还是会提到画面（约定里已写明不许，模型偶发）。
- 壳门铃到点 → 醒来 → 口播这一整条，还没在真实桌宠上等到观察过。

**回退**

| 回退谁 | 从哪拿 |
|---|---|
| 脑 | `/root/albion-pi/archive/claude-20261013-r10…r26`、`claude-20261014-r27…r36`（各轮改前原件） |
| 壳 | `backup/code/2026-10-14/`、`backup/code/2026-10-14-r2/`（旧 `win-unpacked`，不入 Git） |
| 人设 | `/root/albion-pi/agent/SOUL.md.bak-20261014-longing / -selfwake / -voice-034750` |
| 主力机 | `D:\Albion-eye\eyes\启动采集端.bat.bak-20261014` |

**收尾动作（Hermes）**

- 更新文档：`docs/PROJECT_MAP.md`（撤掉旧 Hermes 网关那格，画上 Pi 脑、门铃与她的眼睛）、本 README
  （新扩展、`bin/wake.py`、醒来循环与眼睛两节、自检例数）和本节；顺手把同样还写着「Hermes 是当前生产」的
  `docs/REBUILD.md` 与 `integrations/hermes/README.md` 补上现状（那份 Hermes 现在只是回退路径）。
- 清理一次性验证物：`_tmp/claude/`、`_tmp/claude-tts/`、`_tmp/verify-{autonomy,chains,r23,r24,r25,r33,r34,r35,tts-credit}`
  与 `_tmp/verify-typecheck*.log`，以及 WSL `/tmp/verify-r{6,7,8,9,25,33,34,35}`、`/tmp/r*stage`、`/tmp/pi-brain-probe*`。
  **没有留新回归用例**：守卫与对账的边界已经由 `bin/test_loopguard.ts`（32 件）和
  `tests/pi-brain/check_loopguard_edge.ts` 覆盖，醒来底线由 `bin/wake.py`、`bin/server.py` 自己的 selftest 覆盖，
  那些一次性探针没有独立价值。`_tmp/verify-release.sh`（9-22 的权重发布核对脚本）不是这批的东西，留着没动。
- `python tools/check_repository.py --gitleaks D:/AI/_tools/gitleaks/gitleaks.exe`：0 个问题、0 个发现。
  收尾当时没有 push；当天指挥官说「push一下」后，`main` 已同步到 `origin/main`（`75176ace`，含本批到 r36 的全部提交）。
