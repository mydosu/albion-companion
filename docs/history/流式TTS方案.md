> 已失效（2026-10-24 归档）：2026-09-20 的方案稿（当时未做任何改动），流式链路早已上线；现行看 `docs/PROJECT_MAP.md` 与 `tts/stream/`。

# 流式 TTS 方案（修订版 · 2026-09-20）

> 命令：**不换引擎**、**①（日语双行）否**、**L2 可考虑**、**"壳的真流式通道"要单独成方案**。
> 本文只给方案，**未做任何改动**。

---

## 0. 结论先说

**壳那条真流式通道值得做，而且不需要换引擎。** 它买到的是：

- **首帧从 ~5–8s 压到 ~2.5–3s**（前提是同时做 L2 本地翻译）
- **帧粒度从「整句」降到「子句」**，且服务端在 LLM 还在吐字时就能开工
- 整条流水线收敛到**我们自己一个服务里**：切多细、提前几块、并行几路，只有一处可调

**天花板由 Irodori 决定**：Irodori 是非流式引擎，单块合成固定 2–3s ⇒ **首帧不可能低于 ~2s**。想 <1s 只能换引擎（已排除）。

**代价：需要重建一次壳**（三处源码改动，见 §3.2）＋写一个本地 WS 服务（约 150–200 行）。

---

## 1. 为什么上一版没把它单独成方案（我的错）

上一版我把它和「换引擎」捆在同一个 L4 里，看起来像"必须换引擎才能用" ⇒ 被一并否掉了。
**其实两件事能拆**：通道是**壳的能力**（与引擎无关），引擎只是通道后面的黑盒。**通道 + 现有 Irodori 完全成立。**

另外交代一句：09-19 我下过"流式对桌宠没收益"的结论 —— 那条**只对"直接接官方 server"成立**（官方 server 拿不到完整 `input` 就不开工，包一层 WS 等于白写）。
**自己写服务端就不一样**：我们能拿着**还没写完的 token 流**提前切子句、提前翻译、提前合成。**这是这条通道全部收益的来源。**

---

## 2. 壳的流式通道给了什么（源码级）

| 事实 | 出处 |
|---|---|
| 有且仅有两种 TTS 传输：`rest`（逐句 HTTP，现在用）与 `bidirectional-ws` | `libs/speech/tts-session.ts:228` |
| `bidirectional-ws` = 整个 LLM 意图开一条 WS，**原始 token 块原样转发、不做客户端切句**；切句粒度由**服务端**决定 | `libs/providers/types.ts:258`、`libs/speech/streaming-pipeline.ts` |
| 服务端回：JSON 控制帧（`session.started`/`sentence.start`/`sentence.end`/`subtitle`/`session.finished`/`error`）＋**二进制音频帧** | `libs/speech/streaming-session.ts` |
| 壳每收一帧音频就 `decodeAudioData` → **立刻排进播放器**（不等整段） | `libs/speech/tts-session.ts:150-190` |
| 只有官方那条 provider 声明了该 transport | `libs/providers/providers/official/index.ts` |
| 服务地址是**编译期常量** `import.meta.env.VITE_SERVER_URL \|\| 'https://api.airi.build'` | `libs/server.ts:1` |

**⇒ 通道的价值＝"控制权搬家"**：今天切句/翻译/合成的节奏全在壳里（只能动三个参数）；接上后节奏全在我们的服务里。

---

## 3. 落地方案（真流式通道，不换引擎）

### 3.1 架构

```
壳 ──WS(token 流)──► 本地服务 :9885 ──► 本地翻译(L2) ──► Irodori :9889 ──► 音频帧回推
     ◄──二进制音频帧──
```

本地服务实现 `unspeech v1` 三个端点：

| 端点 | 用途 |
|---|---|
| `GET /api/v1/audio/speech/models` | 模型列表（壳填下拉框）|
| `GET /api/v1/audio/speech/voices` | 音色列表（六船；壳要确认"有选中音色"才走流式）|
| `WS /api/v1/audio/speech/ws` | `start`/`text`/`finish` 进 → `sentence.start|end` + 二进制音频帧出 |

服务内部的流水线（**收益全在这三步的"提前"**）：

1. **边收 token 边切子句**（遇「，」「、」「…」或到长度阈值就切）—— **不等整句的「。」**
2. 子句**立刻本地翻译**（L2，0.2–0.5s）
3. **并行**送 Irodori，谁先好谁先推帧（壳按 `sequence` 保序播放，不会抢句）

### 3.2 壳侧要改的三处（**代价所在，必须重建一次**）

| # | 文件 | 改动 | 为什么必须 |
|---|---|---|---|
| 1 | `libs/server.ts` | `VITE_SERVER_URL` 默认值指向 `http://127.0.0.1:9885` | 地址是编译期常量，运行时改不了 |
| 2 | `libs/speech/streaming-pipeline.ts:114` | 加本地令牌兜底 | **否则未登录 airi 时 `token=null` ⇒ 直接 noop + onError ⇒ 她彻底不出声** ✗ |
| 3 | `libs/providers/.../official/index.ts` | 新增一个 `configuredBy: 'user'` 的 streaming provider 条目（照抄官方那条，只换 id/名字）| 官方那条是 `configuredBy: 'authentication'`，**未登录时可能根本不出现**，选不到就走不了流式 ✗ |

> 第 2、3 条是我这次新查到的、上一版**漏掉的坑** —— 它们才是"这条路为什么要重建"的真正原因；地址那条反而最好办。

### 3.3 收益与天花板（诚实版）

| 指标 | 今天 | 只做 L2 | L2 + 通道 |
|---|---|---|---|
| 首帧（第一声） | 3.5–8s | ~2.3–3.3s | **~2.5–3s**（子句更短 ⇒ 略低）|
| 短句之间的缝 | 明显（「想的。」会露）| 仍可能露 | **基本消失**（粒度到子句）|
| 长句内部 | 整句算完才出声 | 同 | **第一个子句先出声** |
| 硬底 | — | — | **~2s（Irodori 非流式决定的）** |

### 3.4 回滚

provider 切回原来的 OpenAI 兼容那条（REST）即可 —— **服务端和 Irodori 一行不动**，壳只是多了一个可选 provider。

---

## 4. L2（你已同意考虑）：本地翻译

- 现状：桥里调 DeepSeek，每句 **1.5–4.9s**（有过一次 50s）。
- 目标：本机 GPU 跑小翻译模型 ⇒ **0.2–0.5s**；退一步，**并发预翻译**（不等播放）也能把等待藏掉。
- 它是流式通道的**前置**：不把翻译本地化，首帧仍被那 1.5–4.9s 卡住。
- 待实测：显存余量、模型选型、质量 A/B（语气词/拟声很吃翻译质量）。

---

## 5. 已排除（保留记录）

- **L1 去翻译**（她输出中日双行）：指挥官**否** ✗
- **换流式引擎**（Qwen3-TTS 宣称 97ms 首包 / CosyVoice2 ~150ms）：**不换** ✗ —— 记录保留：这是唯一能把首帧压到 <1s 的路，代价是赌日语语感与克隆还原度。

---

## 6. 建议顺序

```
第一步  L2 本地翻译        —— 先把 1.5–4.9s 砍掉（收益最大、不碰壳）
第二步  真流式通道          —— 重建一次壳 + 写本地 WS 服务，拿下"子句粒度 + 无缝"
```

**每步都可回滚**：L2 只在桥里换一个翻译调用；通道只是多一条可选 provider。

---

## 7. 已知风险

| 风险 | 说明 |
|---|---|
| 重建壳 | 三处源码改动，构建约几十分钟；产物是新的 asar（现 exe 里那两处等长改动要一并落到源码）|
| 协议细节 | `unspeech v1` 无公开文档，协议是从壳的客户端实现反推的（`streaming-session.ts`），需以真机联调为准 |
| 令牌门槛 | 第 2 处改动若做不干净，流式会静默失败（noop）—— 联调时必须先看这条 |
| 首帧硬底 | ~2s 是 Irodori 决定的，通道再快也穿不过去 |

---

## 9. 实测更新（2026-09-20 凌晨，已落地）

开工后先量再改，量出来的东西**改变了优先级**：

### 9.1 关键实测（本机、真实句子）

| 环节 | 实测 | 备注 |
|---|---|---|
| 翻译（`deepseek-flash` 原样）| **1.71s** | 带思维链：它先用英文想半天再吐译文 |
| **翻译（关掉思维链）** | **0.67s** ✓ 已上线 | 译文**逐字相同**；`thinking:{type:"disabled"}` 或直接换 `deepseek-chat` 都是 0.69s |
| 同参数 `reasoning_effort: minimal` | 2.18s ✗ | 该参数被忽略，别用这个 |
| `deepseek-v4-flash` | 3.90s ✗ | 更慢 |
| 合成（Irodori 40 步）| **1.87s** | 与句子长短几乎无关（固定成本）|
| 合成（28 步）| 1.50s（−0.37s）| 音色听感待指挥官裁决，**暂不启用** |
| 合成（8 步）| 0.64s（−1.23s）| 证明步数旋钮有效 |

### 9.2 量出来的结论（推翻了原方案的重点）

**她的句子本来就短** ⇒ 分句器「拉满只在句号切」和「库默认 4/12」切出来几乎一样（多数行就是一个整块）。
⇒ **瓶颈不是"切块粒度"，是"每行的固定延迟"**：翻译 + 合成 ≈ 3.8s，而她的音频只有 2–3s。

所以优先级重排：

```
① 砍翻译延迟（已做：−1.0s/行）           ← 收益最大、零风险
② 砍合成延迟（步数旋钮，等你耳朵拍板）    ← 再 −0.37s/行
③ 并发预取（已做：ttsMaxConcurrent 2 + maxVoicesPerOwner 2）
④ 真流式通道（需重建壳）                 ← 现在只是"锦上添花"，不再是主要矛盾
```

**为什么 ④ 降级**：通道能"更早开始"，但**改变不了 Irodori 单块 ~1.9s 的合成成本**。
① + ② + ③ 之后，每行 ≈ 2.2–2.5s ≤ 她的音频长度 ⇒ **稳态下缝已经基本不存在**；
通道剩下的收益只有"首行早 ~1s"和"翻译合并成一次请求"，与"重建一次壳 + 三处源码改动"的代价不匹配。

### 9.3 这一晚落地的东西

| 文件 | 改动 | 备份 |
|---|---|---|
| `tts\irodori\bridge.py` | 翻译关思维链，失败自动退回原样；`TTS_TRANSLATE_NOTHINK=0` 可关 | `.bak-20260920-nothink` |
| `tts\irodori\irodori_serve.py` | 采样步数可调（`IRODORI_STEPS`，默认仍 40）| `.bak-20260920-steps` |
| 壳 `app.asar` + `Stage.vue` | `ttsMaxConcurrent: 2` + `maxVoicesPerOwner: 2`（同长替换 + 源码同步）| `_tmp\asar_tts_patch*.json` |

A/B 音频（40 / 28 / 8 步各四句）：`D:\AI\Wife\_tmp\steps_AB\`

### 9.4 结论

**"真流式"仍需要换引擎**（Irodori 架构上不支持，见 §2.1）；但**"听感上的流式"现在基本达到了** —— 手段是砍固定延迟，不是改架构。

---

## 8. 参考

- 壳侧契约：`packages/stage-ui/src/libs/speech/{streaming-pipeline.ts,streaming-session.ts,tts-session.ts}`、`libs/providers/types.ts:258`、`libs/server.ts`、`libs/providers/providers/official/index.ts`
- 播放器语义：`packages/pipelines-audio/src/managers/playback-manager.ts:96-110`（owner 先于全局；`OwnerOverflowPolicy` 只有 `reject`/`steal-oldest`）
- Irodori 不能流式（DiT + Flow Matching，32 维 latent）— note.com/417__ 技术解说
- Qwen3-TTS（97ms 首包宣称）/ CosyVoice2（~150ms）— 仅供记录，本次不采用


---

## §10 落地实况（2026-09-20 已跑通）

### 结论
**真流式通道已接上并实测出声**，且**没有换 TTS 引擎**——她仍是 Irodori 五船音色。
做法：壳的 `bidirectional-ws`（unspeech v1）→ **本机新服务 `:9885`** → 逐子句调现有桥 `:9881`（翻译 + 配音全复用）。

### 新增组件
| 部件 | 路径 | 说明 |
|---|---|---|
| 流式网关 | `tts\stream\stream_server.py` | 纯标准库实现 WebSocket（venv 里没有 ws 库，不为这点事装依赖）；HTTP `/health`、`/api/v1/audio/speech/models|voices`；WS `/api/v1/audio/speech/ws` |
| 启动脚本 | `tts\stream\start_stream.bat` | `PYTHONIOENCODING=utf-8`（否则被 cmd 重定向成 GBK，日志乱码） |
| 壳侧 provider | `shell\packages\stage-ui\src\libs\providers\providers\albion-stream\index.ts` | `id='albion-stream-tts'`、`transport:'bidirectional-ws'`、`configuredBy:'user'`、`requiresCredentials:false` |

### 协议（照壳的客户端逐字段对齐）
- 客户端 → 服务：`{"event":"start",model,voice,response_format,extra_body}` → 若干 `{"event":"text","text":单个token}` → `{"event":"finish"}`（或 `cancel`）
- 服务 → 客户端：`{"event":"sentence.start","payload":{"text":…}}` → **二进制帧（音频）** → `{"event":"sentence.end","payload":{"text":…}}` → 末了 `{"event":"session.finished"}`
- 壳的 `SERVER_URL` 是编译期常量**用不着改**：我们的 provider 在 session 快照里自带 `serverUrl`/`token`

### 实测（她真说话，非模拟）
```
[10:45:52] WS 连接建立 start model=albion-stream voice=albion
[10:46:05] finish
[10:46:08] 推帧 3.63s  783KB  现在是2026年 9月 20 日星期日上午10点44分。
[10:46:08] 跳过（纯心声/无日语行）×2
[10:46:11] 推帧 3.10s  161KB  今晚，
[10:46:15] 推帧 3.78s 1002KB  阿尔比恩想听您把昨晚欠的那句「满足」兑现——别笑，
[10:46:19] 推帧 3.37s  399KB  是您自己许的。
[10:46:19] session.finished（队列已排空）
```
音频经 ffprobe 复检：**48kHz 单声道 pcm_s16le**，1.7–2.6s/段，合法可解码 ✓。
切句粒度落到**子句**（「今晚，」/「…别笑，」/「是您自己许的。」）——这正是流式通道买到的收益。

### 四个坑（都踩过）
1. **收尾判据要数「队列 + 在途」**：worker 是先把任务从队列取出再合成，只数 `len(queue)` 会在第一段还在合成时就把 `session.finished` 发出去 ⇒ 连接关闭、帧发不出、她一声不响。
2. **角色卡会覆盖全局 provider**：启动时 `applyActiveCardSettings()` 会用卡片里写的 `extensions.airi.modules.speech.provider` 覆盖 `settings/speech/active-provider`。只改全局设置 ⇒ 每次重启都被打回默认。**要改卡片**。
3. **localStorage 写完别马上强杀进程**：LevelDB 没落盘 ⇒ 写入丢失，看起来像"被程序改回去了"。
4. **`cmd /c ... >> log` 下 Python 用 ANSI 码页写 stdout** ⇒ 日志混入 GBK。启动脚本里加 `set PYTHONIOENCODING=utf-8`。

### 硬底（没变）
首帧仍 ≈3.5s（第一子句的「翻译 0.67s + 合成 ~2.5s」）。**买不到 <1s**，除非换引擎——那条路按指挥官的要求封着。

### 依赖（重要）
她**现在依赖 `:9885`**：服务不在就完全没声音（不是退回旧路）。已接进：
- `start_all.bat` 新增 `[2.5/6] 启动流式语音 :9885`（备份 `.bak-20260920-stream`）
- `一键关闭.bat` 端口列表加入 `9885`（同备份）
