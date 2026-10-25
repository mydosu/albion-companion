# 阿尔比恩 · 交互与动画改造计划

> 依据指挥官 2026-09-14 的《问题和想法.md》。**本文件是这项工作的进度权威**，随做随更新。
> 相关：`docs/history/pet-ROADMAP.md`（历史总体进度）、`问题与想法` 原始需求。

## 状态总览

| 编号 | 项目 | 状态 | 结论 / 备注 |
|---|---|---|---|
| 问题1 | 她回复时语音不出声 | ✅ 完成 | 见下 §1 |
| 问题2 | Model Based（VAD 模型）没加载 | ✅ 完成 | 见下 §2；mic 端到端需指挥官实机确认 |
| 问题3 | 噪音触发 "voice input failed to transcribe speech" 报警 | ✅ 完成 | 见下 §3 |
| 想法1 | 动作映射 + 后端控制动作 | ✅ 完成 | 见下 §4；34 组动作表见 `MOTION-MAP.md` |
| 想法2 | 动画丝滑衔接（循环 + 切动作） | ✅ 完成（待眼验） | 见下 §5；帧差实测"连续在动、无硬跳" |

---

## §1 问题1：她回复时不出声 ✅

**现象**：她回文字，但没有声音。

**根因（两层，都不是"TTS 服务坏了"）**：
1. `settings/speech/active-provider` 被写成了 **`speech-noop`**（静音的空实现）。
2. 更关键：AIRI 对 **openai 兼容** 的 TTS 有硬逻辑 —— `Stage.vue` 里
   ```ts
   if (providerConfig?.model) model = providerConfig.model
   else { model = 'tts-1'; console.warn('...No model in provider config...') }
   ```
   **只认 provider 配置里的 `model`/`voice`**，不看语音设置页。我们的 provider 配置里只有
   `apiKey`/`baseUrl` → 每次请求发 `model='tts-1' voice='alloy'` → 桥的嗓音名单里没有 alloy → **404** → 无声。

**证据（桥日志）**：
```
改前： [req] voice='alloy' model='tts-1'  →  POST /v1/audio/speech 404 Not Found
改后： [req] voice='albion' model='albion'  →  POST /v1/audio/speech 200 OK   ×4
```

**修复**：
- `packages/stage-ui/src/albion-defaults.ts`：TTS provider 的 `config` 补上 `model`/`voice`（= `albion`）。
- 运行时同步写入 `settings/providers/configured['openai-compatible-audio-speech'].config`。
- 顺手给桥 `bridge.py` 加了一行 `[req] voice=... model=... len=...` 日志 —— **以后排查"没声音"直接看这一行**。

**顺带挖出的两个系统性坑（重要）**：
1. **角色卡会覆盖运行时选择**：`localStorage['airi-cards']` 里的卡带一份 `modules.speech / hearing / vision / consciousness` 快照，
   每次启动**回灌**到 store，把我们在 store 里改的值盖掉。改完 store 必须调用
   `airi-card.persistActiveCardModuleSelections()` 写回卡里，否则下次启动白改。
2. **非 leader 窗口会用旧值互相覆盖**：开着的 `?synced-leader=false` 设置窗（就是指挥官当时在看的那页）
   一直在用自己那份旧状态（alloy/tts-1）覆盖共享 localStorage。**遇到"设置改了不生效"，先看有没有多余窗口开着**。

---

## §2 问题2：Model Based VAD 拉到本地 ✅

**现象**：设置页 "Model Based —— Use AI models for more accurate speech detection" 起不来；
app 日志 `[Voice Input] vad-init-failed: VAD initialization failed. {"error":"Failed to fetch"}`。

**根因**：`packages/stage-ui/src/workers/vad/vad.ts` 从 HuggingFace **在线**拉
`onnx-community/silero-vad`，本机网络不可达 → 失败 → 一直退回"音量判断"（Volume Based）。

**修复**：
- 模型落到 app 自己的静态目录（**是 `src/renderer/public/`，不是 `apps/stage-tamagotchi/public/`**）：
  `apps/stage-tamagotchi/src/renderer/public/models/silero-vad/onnx/model.onnx`（2.19MB，官方 fp32）。
- worker 改为本地加载：
  ```ts
  env.allowRemoteModels = false
  env.allowLocalModels = true
  env.localModelPath = '/models/'
  await AutoModel.from_pretrained('silero-vad', { config: { model_type: 'custom' }, dtype: 'fp32' })
  ```
- **实测**：用仓库里的 transformers.js 从磁盘加载 → `VAD 本地加载: OK ✓ 9 params-keys`。

**待指挥官确认的一步**：真麦克风端到端（我无法用假麦克风验证 —— 这台 Electron 的假设备恒为静音）。
判断标志：设置页那一栏显示 **"Model Based"**（不是 "Volume Based"），说明模型已加载。

---

## §3 问题3：噪音不再弹报警 ✅

**现象**：没说话时的环境音被拾到 → 界面上弹 `voice input failed to transcribe speech`。

**根因**：`apps/stage-tamagotchi/src/renderer/pages/index.vue` 的两处回调
（`onTranscriptionEmpty` / `onTranscriptionError`）都会调 `reportVoiceInputFailure('transcribe speech', ...)`
→ 该函数是 `console.error` **+ `toast.error(...)`**（就是弹窗）。

**修复**：这两处改为**只写日志、不弹窗** —— 噪音/极短音频不再打扰；真出问题仍可在 devtools 日志里查。
（其余提示如"start listening 失败"保留 —— 那些是真故障，不是噪音。）

---

## §4 想法1：动作映射 + 后端控制 ✅

**做完了什么**：
1. **调研**：皮肤「香气来朱阁」= 阿尔比恩 2024 春节旗袍 L2D，手游里可摸（头/身体/特殊部位）、可拖动、有彩蛋；
   官方没公开"哪一下播哪个动作"→ 语义按组名 + 手游习惯推断，全部 34 组写在 **`MOTION-MAP.md`**。
2. **触摸分区**：点她 **上 1/3 = 摸头**（`touch_head`）、**中间 = 戳身体**（`touch_body`）、**下 1/3 = 特殊触摸**（`touch_special`）；
   三种情况发给她的"被碰"描述也不同，她的反应能对上。
3. **后端可控**：她的回复标记扩了 `motion` 字段 —— `<|ACT:{"emotion":"curious","motion":"touch_head"}|>`
   （写进 `profiles/albion/SOUL.md`，她知道有哪些动作；写错名字只忽略、不影响正文）。
4. 调试口：`__albionPet.play('组名')`、`__albionPet.motions`（34 条清单）。

## §5 想法2：动画丝滑衔接 ✅

**根因（两个都要治）**：
1. **SDK 找不到空闲动作**：pixi-live2d-display 把空闲组名**写死成 `"Idle"`（大写）**，
   而模型里只有小写 `idle` / `idle1..5` → 一直是"选中哪个动作就死循环哪个"。
2. **所有动作的淡入淡出都是 0**：`idle.motion3.json` 的 `Meta = {Loop: true, FadeInTime: 0.0, FadeOutTime: 0.0}`
   → 9 秒内部循环回到开头时**硬跳**，这就是"首尾肢体不在同一位置、像两个片段"的直接原因。

**怎么治**（`patch_model.py`，改模型 JSON，不动美术）：
- model3.json 加 **`Idle` 组**（含 `idle` + `idle1..5` 六个变体）→ SDK **随机挑一个播** = 手游那种"看不出重复"。
- 34 个动作全部写上 `FadeInTime`/`FadeOutTime`（空闲 1.0s、触摸/事件 0.4/0.5s）
  → 切换时**交叉淡化**，而不是硬切。
- 空闲动作 `Loop: false` → 交给 SDK 用淡化衔接上下一个变体。
- 备份：`D:/AI/Wife/pet/_albion_model_backup.zip`（想回退直接覆盖回去）。

**实测**：连拍 10 帧（1.5 秒/帧）量相邻帧差异 → 稳定 14~20%（**一直在动**），无硬切级跳变；
`__albionPet.play('Idle')` 无报错（说明补丁后的模型组确实加载了）。
**剩下的只能靠眼睛**：变体之间是不是"接得上"、1 秒淡化够不够柔 → 指挥官看一眼，嫌快/嫌慢我调 `patch_model.py` 里的
`FADE_IDLE` / `FADE_TOUCH` 两个常量重打一次即可。

## §6 耳朵归属（顺带定的）

问题2 修好后 AIRI 自己的「Model Based」能用了 → **耳朵交回 AIRI 那条路**（它才是官方链路，且带模型端点检测，
比我自己写的能量阈值更不容易被噪音骗）。
我自建那只（`albion-ear.ts`）**保留但不启动**，需要时一行打开：
```js
localStorage['settings/albion/ear-enabled'] = 'true'   // 然后刷新窗口
```
（如果哪天她说"听不见"，先把这行打开顶上，再叫我查 AIRI 那条路。）
（2026-10-16 补记：`albion-ear.ts` 已删除，上面这个开关不再起作用；听不见时直接查 AIRI 自带的听力模块，见 `使用说明.md`。）

---

## 变更文件清单

| 文件 | 改动 |
|---|---|
| `packages/stage-ui/src/albion-defaults.ts` | TTS provider 配置补 `model`/`voice` |
| `packages/stage-ui/src/workers/vad/vad.ts` | VAD 走本地模型 |
| `apps/stage-tamagotchi/src/renderer/public/models/silero-vad/onnx/model.onnx` | 新增（2.19MB） |
| `apps/stage-tamagotchi/src/renderer/pages/index.vue` | 噪音不再弹报警 |
| `D:/AI/Wife/tts/irodori/bridge.py` | 加 `[req] voice/model/len` 日志 |
| `packages/stage-ui/src/albion-pet.ts` | 34 组动作映射 + 触摸分区 + `motion` 标记 + `play()` 调试口；自建耳朵改为默认不开 |
| `pet/tools/patch_model.py` | 新增：模型补丁（Idle 组 + 淡入淡出 + 关内部循环） |
| `pet/docs/MOTION-MAP.md` | 新增：34 组动作对照表 |
| `profiles/albion/SOUL.md` | 加"动作（可选）"说明，她知道能指定 `motion` |
## §7 第二轮反馈 9 条（2026-09-14 晚，来自《问题和想法1.md》）

| # | 内容 | 状态 | 结论 |
|---|---|---|---|
| 1 | 后台开了很多个相同的 tts/桥进程，关掉不用的 | ✅ 完成 | 清掉抢不到端口的重复 `bridge.py` ×2（含我自己起的临时实例）。**教训：判断"哪份是活的"必须按端口→PID 精确核对，我一开始凭启发式杀了活的那份** |
| 2 | 鼠标点击穿透可以不用了 | ✅ 完成（需重启桌宠壳生效） | 删掉 `Ctrl+Alt+P` 注册那段（`main/index.ts`），注释里留了恢复方法 |
| 3a | 动画丝滑了，但交替时像"倒放/回溯" | ⏳ 需你选 | 淡入淡出是两个姿态的直线插值：首尾差别大时，1 秒淡化看起来就是"倒回去"。两个方案见下 |
| 3b | 点人物后不切换触摸动画 | ✅ 完成并验证 | **真凶：我把 `currentMotion` 从 setup store 里解构出来写 `.value`** → 写给了一个普通对象，"调用成功但画面不动"。改成写 store 属性后实测 `complete → touch_head` ✓ |
| 4 | 被触摸后，触摸动画应随机（这一类有很多） | ✅ 完成 | 主反应按部位命中 + 3.2 秒后随机补一个 `touch_idle1~10`；开关 `settings/albion/touch-followup` |
| 5 | 合成语音混着胡言乱语，"中文聊天+日语声音"没实现 | ✅ 完成并端到端验证 | **根因：应用端是逐 token 流式、再按句切分，括号被切到不同请求里**（日志实证：`（肩膀轻轻缩了缩，` / `）` / `让您戳。` 各是一个请求），原来的"成对括号"过滤失效 → 日语模型念舞台指示。改成跨请求状态机（30 秒超时兜底）+ 自测通过；中文进 → 干净日语出，耳朵转写回来是 `指揮官、今日もお疲れ様。…` ✓ |
| 6 | 触摸位置要匹配动作，语音也要匹配 | ✅ 完成 | 按高度三区：上 30% 摸头 `touch_head`、中间 `touch_body`、下 28% `touch_special`；发给她的"被碰"描述也分三种 ✓ |
| 7 | 触摸范围太广，点画布空白也触发 | ✅ 完成 | 加了归一化**命中框**（默认 x 0.30~0.72 / y 0.06~0.96），框外不算戳她。嫌宽嫌窄：`localStorage['settings/albion/touch-box'] = '{"x0":0.30,"x1":0.72,"y0":0.06,"y1":0.96}'` |
| 8 | 字幕不能移动、一移动就报错、还被模型挡住 | ⏳ 待做（见 §8） | 字幕是**独立窗口**（`pages/caption.vue`），层级其实比模型窗高（relativeLevel 2 vs 1），被挡多半是焦点抢占。推荐解法与 #9 合并 |
| 9 | 对话框太大，想做成 Open-LLM-VTuber 那样的小对话条 | ⏳ 待做（见 §8） | 方向明确：**把字幕 + 状态 + 输入框做成桌宠窗口内底部的小对话条** |

## §8 #8 + #9 的解法（下一步做这件事）

你给的那张参考图（Open-LLM-VTuber 桌面端）本身就是答案：**立绘 + 底部一条小对话条**在同一个窗口里。
现在 AIRI 是"立绘窗口 + 独立字幕窗口 + 独立大聊天窗口"三个，所以我打算：

1. 在桌宠窗口（`apps/stage-tamagotchi/src/renderer/pages/index.vue`）底部加一条小对话条：
   字幕行（左上）+ 状态点（`idle`/`listening`/`speaking`）+ 输入框 + 发送按钮，**宽度约窗口 90%、高约 12%**。
2. 字幕直接读现有的 caption 数据（`useCaptionItems`），**渲染在画布之上**（同窗口、z-index 高于 canvas）→ 顺带治好 #8 的"被挡住"。
3. 把独立字幕窗（`windows/caption`）默认不显示；大聊天窗只在需要翻历史时才开。
4. 样式照参考图：半透明深色圆角、字小而清晰、不抢立绘。

## §9 想法2 的"倒放感"两个方案（你挑一个，我改一个常量就行）

- **A（默认，当前）**：`Loop: false` + 1.0 秒交叉淡化 + 6 个空闲变体随机轮播 → 有变化、但切换那 1 秒是直线插值，姿态差大时像"倒回去"。
- **B**：`Loop: true`（每个空闲动作自己无缝循环，**完全没有切换动作**＝不会有倒放感）+ 每 25 秒左右换一次变体（换的时候仍会淡化一次）。
  改法：`patch_model.py` 里把 `IDLE_LIKE` 的 `Loop` 改回 `True`，重打一次（10 秒），我也可以做成一键脚本。


## §10 第三轮（2026-09-14 深夜，回答一.md 四条）

| # | 内容 | 状态 | 说明 |
|---|---|---|---|
| 4 | 后台重复的 tts/桥进程 | ✅ 按你说的"全关再拉" | 五件服务全停（含桌宠壳）→ 端口全空 → `start_all.bat` 重新拉起 → **每个端口正好一个进程**：9881/9882/9889/8650 各一个 python、9335 一个 electron。教训：我先前用"命令行匹配"杀进程时匹配到了自己的 shell，还杀错过活的那份——现在一律**先按端口确认，再按 PID 精确关** |
| 3 | 空闲动作只有一个在重复 | ✅ 已修并验证 | 根因：你之前在界面里选定的那个动作（`complete`）被写进 `settings/live2d/current-motion` 且它 `Loop:true` → 一直循环它，永远轮不到 SDK 的空闲组。清掉 `selected-runtime-motion*`、把 current-motion 设回 `{group:'Idle'}` 后：抓 30 帧实测姿态变换（与首帧距离从 13 跳到 83~91、聚类出 3 种姿态）✓ 现在是 6 个空闲变体随机轮播 + 1 秒交叉淡化 |
| 1+2 | 触摸分区要精确到头/胸/屁股/大腿/小腿/足；点到空白不触发 | ✅ 已实现并验证 | 六段部位表 + 命中判定。实测：点空白无任何反应（消息数不变）；点头部 → 播 `touch_head` 且发"摸了摸你的头"。**人物本体无法自动分割**（整张插画、无 HitAreas、整窗不透明、背景随动作摆动→方差法失败），所以给了标定接口 |

### 标定怎么用（一次就好）
后台执行 `window.__albionPet.calibrate()`，然后**依次点她 头→胸→屁股→大腿→小腿→足**各一下，自动算出并保存；
`window.__albionPet.calibrateReset()` 恢复默认。也可以直接改
`localStorage['settings/albion/touch-bands']`（6 条 `{name,y0,y1,x0,x1}`）。

### 遗留（下一轮）
- AIRI 自己的"情绪→动作"会把 `currentMotion` 写成 `Think`/`Happy` 这类**我们模型里不存在的组名**（因为该模型没有情绪动作组），
  pixi 会找不到组、白跑一次。计划：把这些名字映射到模型真实存在的组（如 `main_1~3`），或干脆让它别写动作、只走我们的姿态表情。

## §11 第四轮（2026-09-15）：命中网格 + 底部小对话条

### #1/#5 命中判定：改用模型自带的官方触摸网格 ✅（推翻我上一版的猜测式做法）
probe 发现模型里有 **19 个官方触摸网格**：`TouchHead` / `TouchBody` / `TouchSpecial` / `TouchDrag*`
（手游同款；此前从 `cdi3.Parts` 看全是 `ArtMesh135_Skinning` 才误判"没有命中数据"——
真正的位置在 **model3.json 的 drawables 名字**里）。

实现（`packages/stage-ui/src/albion-pet.ts`）：
- 命中：`model.toModelPosition(clientX, clientY)`（pixi 自带，含缩放/位移/父级变换）→ 落在任一触摸网格包围盒内 = 摸到她
- 部位：`TouchHead`→头、`TouchBody`→胸、`TouchSpecial`→屁股/特殊；其余 `TouchDrag*` 按在**网格总高**里的相对高度分 大腿/小腿/足
- 部位阈值可调：`localStorage['settings/albion/part-split']`；**标定接口、归一化命中框全部删除**
- 钩子：`Model.vue` 把模型挂到 `globalThis.__albionModel`（`watch(model, ...)`）

验证（全部自动、无需人眼）：
- 19 个网格各自中心 → `hitTest` 反查回自己的部位（往返自洽）；画布角落 → `undefined`（空白不触发）✓
- 合成一次点击（点头部网格中心）→ `hit: 'head'` → 播放 `touch_head` ✓
- 旧毛病（点空白也触发、位置靠猜）根治：拖到哪、放多大都跟着模型走

### #8/#9 底部小对话条 ✅
`pages/index.vue` 底部加了一条半透明对话条（渲染在本窗口画布之上 → 顺带解决"字幕被立绘挡住"）：
- 字幕 = **她最近说的一句**（从 chat session 取，剥掉 `<|ACT:…|>` 标记）
- 输入框 + 发送按钮（走同一个 `chatStore.send`）
- 验证：底栏输入「阿尔比恩，在吗？」→ 收到回复 → 底栏显示她最新那句 ✓（截图 `_bar2.png`）

### 遗留 / 未做
- AIRI 自己的"情绪→动作"仍会写 `Think`/`Happy` 这类不存在的组名 → pixi 空跑一次、无副作用。
  **决定先不做别名映射**（不确定 `main_1~3` 长什么样，乱映射可能更差）；等看清这几组动作再定。
- 字幕最多 4 行（`max-h-16`）；嫌长改一个 class 即可。
- 旧 localStorage 键 `touch-box` / `touch-bands` 已废弃（无害，未清）。
- 独立字幕窗（`pages/caption.vue`）仍在；底栏不依赖它。要彻底去掉那个窗口再说一声。

## §12 第五轮（2026-09-15 深夜）：字幕还原 + 独立消息框 + 点她立刻出声

### #1 字幕窗：还原成 AIRI 原样 ✅
上一轮把输入框塞进了字幕窗——**那是对着坏东西加东西**。原版毛病在对窗口的管理：
`isFollowing` 默认 true，而 `applyIgnoreMouseEvents(win, isFollowing)` 在跟随时把窗口设成
**鼠标穿透**（`setIgnoreMouseEvents(true,{forward:true})`）→ 点不动、拖不动，且拖动把手
只在 `!attached` 时显示。已改：默认 `isFollowing: false`（代码 3 处 + 已存配置），
`caption.vue` 按上游原样还原（5696 字节逐字节一致）。

### #2 消息框：新开一个独立窗口（不再借用字幕窗）✅
- `main/windows/chatbox/index.ts`：460×64 透明无边框、可缩放、置顶、不穿透；
  发送必须挂 `setupChatWindowElectronInvokes`（独立窗口不是 leader，缺这套 chatStore.send 静默失败）
- `renderer/pages/chatbox.vue`：白 55% 半透明条，**整条框是拖拽区**（输入框/按钮 no-drag），
  左边细灰线只是提示
- `main/index.ts` + `tray/index.ts`：注册 + 托盘菜单「消息框」
- 位置/大小记 `windows-chatbox-config.json`（move/resize 停 0.4s 写盘，夹回可见区）
- 验证：`pnpm build` EXIT=0；实测窗口开在种子坐标、拖动后文件与窗口几何逐位一致；
  在窗口里发消息 → 她收到并回复（"消息框这边收到了，链路是通的"）

### #3 点她：手游手感（即时反馈，不等 LLM）✅
`poke()` 里加 `applyEmotion(part.pose)` + 该部位一句日语（`part.jp`），走桥 :9881 出声。
坑：桥首句要 19s、之后 2~5s，"点了立刻出声"本来是假的 → 加**开局预热 6 句**
（`warmTouchVoices()`，只合成不播）。实测点到出声 **4ms**。关掉：`settings/albion/touch-voice=false`。

### 还挂着的
- AIRI 自动播不存在的动作组 `Think`（日志每次都有 "Setting motion: Think"）→ 空跑，未映射
- 真实鼠标点击：只验过合成事件（用户侧目测可用）
- 消息框托盘标签硬编码中文、未进 i18n

## 13. 命中判定重做（2026-09-15）：放弃官方网格，改用模型坐标系分区

**为什么放弃官方 `Touch*` 网格**：逐帧量过（`_zones.png`）——这套 zip 的网格和美术对不上：
`TouchHead` 落在右下裙摆、`TouchBody` 落在背景、点她脸上反而没有网格。所以"点哪儿都同一个动作"
不是逻辑 bug，是数据错位，怎么调阈值都调不出来。

**现在的做法**（`albion-pet.ts`）：把点击换算进**模型坐标系**（`toModelPosition`，与渲染同源），
再查一张 6 段分区表 → 头/胸/腰/臀/大腿/小腿。因为分区坐标钉在**模型**上，
**窗口挪动、缩放、换机器都不受影响**（官方 SDK 也是这个原理，只是数据换成我们自己量的）。
范围外返回 undefined（点背景不响应）。默认值：

```
y0 1820（头顶以上） y1 3640（脚底以下） x0 1430（左缘） x1 2740（右缘）
bands: head<2090 < chest 2450 < waist 2760 < hip 2960 < thigh 3120 < leg
```

覆盖：`localStorage['settings/albion/zones'] = '{"x0":…,"bands":[…]}'`（配坏自动回默认）。

**验证**（自动化，不靠肉眼）：20×20 采样 6 段各就其位、左右背景与头顶/地板均为空；
合成点击头部 → 发出「（指挥官摸了摸你的头）」；点背景 → 消息数不变。

**语音与台词**：官方**同一部位固定同一动作**（我们照抄），但官方那批触摸语音我们手上没有，
现在用她那套音色 TTS 合成的日语 + LLM 的对话回复。

## 14. 她的说话方式（2026-09-15）：从游戏原台词学语感

`profiles/albion/SOUL.md` 追加「阿尔比恩的语感样本」：11 条原台词 + 中文语感 + 8 条说话要点
（第三人称自称「阿尔比恩」、敬语口语化、慌时口吃重复、轻声收尾、确认型提问、擅长家务、偶尔端架子）。
系统提示按会话缓存 → 改完要清 `state.db` 的 `system_prompt_hash` 才生效（8 个会话已清）。

实测（走网关一句话）：自称「阿尔比恩」✓ 轻声收尾 ✓ 关切早饭 ✓ 端架子一句就破功 ✓
