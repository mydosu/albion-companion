# 如何用 Irodori 把长台本合成成亲密/喘息向音频（本机实测方法）

> 适用：本机 `D:\AI\Wife`，Irodori-TTS 后端 `:9889`，五船单进程。
> 本文所有命令、字段、数字都来自 2026-10-28 在本机的实际运行（不是设计稿）。
> 标「未确认」的地方是没实测、不能当结论的。
> 台本与成品音频是成人向私用素材，**不入 Git**；本方法只描述链路。

---

## 0. 一句话版本

把台本按句子切开（每段 ≤60 字）→ 逐段调 `POST /v2/plan`（`plan_version=4`，带上一段返回的
`prev` 和前 400 字原文）拿到 `family / caption / cfg / tier` → 逐段调 `POST /v2/render`
（SSE 回 WAV）→ 按顺序拼成一个文件，段间留 0.25s 静音。

---

## 1. 环境与链路

```
台本 txt
  └─ 切句（scene_plan._segments + 标点段合并 + 60 字上限）
       └─ POST http://127.0.0.1:9889/v2/plan     # 纯 CPU，只决定"怎么说"
            └─ POST http://127.0.0.1:9889/v2/render/open
                 POST http://127.0.0.1:9889/v2/render        # SSE，单 GPU 队列
                 POST http://127.0.0.1:9889/v2/render/close
                      └─ 拼接（wave，0.25s 静音）→ 整段 WAV
```

| 项 | 值 | 出处 |
|---|---|---|
| 后端 | `irodori_serve.py`，`:9889`，5 船一个进程 | `tts/irodori/irodori_serve.py:206` |
| 启动 | `tts/irodori/启动Irodori后端.bat` → `.venv\Scripts\python.exe irodori_serve.py` | 同目录 bat |
| 规划 | `POST /v2/plan`，纯 CPU、不加载模型 | `tts/irodori/scene_plan.py:1013` |
| 渲染 | `POST /v2/render`，进程内全局单 GPU 队列 | `tts/irodori/render_queue.py:650` |
| 底模 | `checkpoints/model.safetensors`，bf16 | `irodori_serve.py:23,143` |
| 采样 | 40 步（`IRODORI_STEPS` 可改） | `irodori_serve.py:150` |
| 随机性 | 默认**不传 seed**，每次采样都不同；要配对实验才显式给 `seed` | `irodori_serve.py:137,151` |
| 输出 | 48 kHz / 16-bit / 单声道 WAV（实测四份都是这个格式） | 本次实测 |
| 时长上限 | `max_seconds=30.0`（单段音频硬上限） | `irodori_tts/inference_runtime.py:217` |

### 船与音色（`ships.json`，热更新）

| 台本 | voice | LoRA | 嵌入 | 额外 |
|---|---|---|---|---|
| 阿尔比恩 | `albion` | `albion_lora_rich_ck500` | — | — |
| 布雷斯特 | `brest` | `brest_lora_emo_spk` | — | — |
| 大凤 | `taihou` | `taihou_lora_clean200` | `taihou_inv_clean` | **joint_cfg=4.5**（覆盖计划里的 cfg） |
| 金狮 | `jinshi` | `jinshi_lora_clean200` | `jinshi_inv_clean` | — |

> 注意：`docs/阿尔比恩TTS全流程.md` 里 albion 的 LoRA 名还是旧的 `albion_lora_emo_spk`，
> **以现在 `tts/irodori/ships.json` 为准**。
> 大凤走 `--cfg-scale 4.5 --cfg-guidance-mode joint`（`irodori_serve.py:160-164`），
> 所以大凤的计划 cfg（3.2/3.5/4.5）**不是最终生效值**；effective cfg 恒为 4.5。

---

## 2. 台本怎么切

### 2.1 先切自然段/句

`scene_plan._segments()`（`scene_plan.py:175-190`）：

1. 按换行切；空行丢掉；
2. `[[...]]` 整行是场景标记，不朗读，只当作场景边界（`breaks`）；
3. **整篇只剩一行时**，退化成按 `。！？!?` 切句。

四份 persona 实际上都是"一行长文"（大凤文件里第二个换行是行尾），所以都走第 3 条，
切成 30 / 58 / 59 / 78 个自然段。

### 2.2 合并纯标点段

`……。` 这类写法会被句末切分切出一个只有「。」的段。单独合成只会得到一声空响/爆音，
所以把**不含任何文字字符的段并回前一句**（开头则并到下一句）。本次实测每份有 2–5 个这种段。

### 2.3 再切到模型吃得下的长度

三个上限，取最严的：

| 限制 | 值 | 出处 | 超了会怎样 |
|---|---|---|---|
| 文本 token | 255（含 BOS 共 256） | `scene_plan.py:883`、`render_queue.py:580-592` | tokenizer 静默右截断，尾部整句丢 |
| 单段字符 | 200 | `render_queue.py:578` | `/v2/render` 直接 400 |
| **单段时长** | **30s** | `inference_runtime.py:217` | 预测时长超 30s 会截断/赶完 = 吞句 |

本次实测亲密档约 **3.0–3.8 字/秒**；气声多时更慢，按 2.5 字/秒留余量，
所以切分取 **60 字/段**（≈15–24s）。切点优先落在 `。！？!?…‥、，,；;` 上；
只有单个标点块自己就超 60 字时才按字符硬切（会明显不自然，尽量避免）。

> 反例（实测）：按 200 字切时，385 字的段落被切成 200+185 字两段，两段渲染结果都**正好
> 30.00 秒**——触到 30s 上限，文字被赶/截。改成 60 字后，同样的段落每段 13–16 秒，无截断。

### 2.4 本次实际切分

| 台本 | 字数 | 自然段 | 渲染片段（≤60 字） |
|---|---|---|---|
| 阿尔比恩 | 1280 | 30 | 37 |
| 布雷斯特 | 1287 | 58 | 60 |
| 大凤 | 1320 | 59 | 61 |
| 金狮 | 1411 | 78 | 78 |

---

## 3. 逐段规划：`POST /v2/plan`

### 3.1 请求体（照抄）

```json
{
  "units": [{"unit_id": "albion-005-0", "text": "……", "advance": true}],
  "context_before": "本段之前最近 400 字日语原文",
  "plan_version": 4,
  "emotion_policy": "cascade",
  "prev": { "family": "intimate", "level": 0.67, "tier": "strong",
            "scene_id": 3, "held": 0, "delivery": null, "delivery_trusted": false }
}
```

- `prev` 是**上一段响应的 `state` 原样回传**（第一段不传）。滞回、强度衰减、场景号都靠它。
- `context_before` 取最近 400 字（生产 `stream_server.py:44` 的 `EMOTION_CTX_CHARS`）。
- `advance`：新自然句为 `true`；同一句被软切/硬切出来的续片为 `false`（不重复衰减）。
- `emotion`（可选）：上游 LLM 的 `{phonation, arousal, intensity, valence}`；日语直通没有这一项。
- 服务端在 `plan_version=4` 时默认：`weak_delivery=on`、`refusal_guard=on`、`cue_scope=off`、
  `ja_stage_cue=off`（v6 才默认开）。见 `scene_plan.py:1054-1080`。

### 3.2 响应（每段一条）

```json
{"plan_version": 4, "codebook_version": 2, "weak_delivery": true,
 "units": [{"unit_id": "...", "family": "intimate", "caption": "柔らかく、優しく囁くように",
            "cfg": 4.5, "tier": "weak", "level": 0.34, "scene_id": 4,
            "codebook_version": 2, "style": "moan", "reason": "style=moan level=0.34 tier=weak"}],
 "state": {"family": "intimate", "level": 0.34, "tier": "weak", "scene_id": 4, "held": 0}}
```

### 3.3 v3/v4 的"逐句滞回"（不是整篇一个味）

规则顺序（`scene_plan.py:695-830`）：

1. **显式关键词命中** → 立刻切到对应族，`level` 按文本强度推进（步长上限 0.34）。
2. 关键词没命中、但 LLM hint 判出了非 neutral 族 → 用 hint 的族。
3. 都没信号 → **继承上一族**，`level` 每句减 `0.17`（`_HOLD_DECAY`）；
   连续 `8` 句没信号（`_HOLD_MAX`）就释放回 neutral。
4. 强弱档用双阈值：升档 ≥0.58，降档 <0.42（`emotion_codebook.py:52`），
   避免在 0.5 附近来回抖。

> 这就是"长段落不漂"的来源：有信号立刻切，没信号慢慢衰减，而不是整篇粘在一个档上。

### 3.4 那条 40% 规则是什么、为什么现在不用了

v2 的 `_plan_styles()`（`scene_plan.py:202-205`）：

```python
if 整篇 intimate 族占比 >= 0.40:
    styles = ["tremble" if 族 == "neutral" else x for x in styles]
```

即：整篇有 40% 以上的句子是亲密族时，把所有 neutral 句硬并进 `tremble`（亲密族），
再按"连续同族"合并场景。**v2 时代**这么做是为了救"单句判不准"：只要整篇是亲密戏，
宁可把所有平静句也念成亲密，避免一句一个味。

v3 起**删掉了**这条（`scene_plan.py:415-420` 的注释写明原因）：
它在实测的四份 persona 上**全部触发**，把本来该平静/该低落的句子也拖进亲密档，
恰恰是"整段一个味"的来源。v3 改用逐句滞回承担同一职责，因此 **v4 生产不会再整篇合并**。
（`plan_units()` 这条 v2 旧路径还保留该规则，只用于等价性回归；`plan_units_v3` 不调用它。）

---

## 4. 风格表与 cfg

### 4.1 生产在用的码本（v3/v4，7 句训练锚，5 族）

出处：`tts/irodori/emotion_codebook.py:19-41`。

| 族 | 弱档 caption（level 低） | 强档 caption（level 高） | cfg |
|---|---|---|---|
| `neutral` | `落ち着いた、静かな声で` | `自然な声で、丁寧に` | 3.0 |
| `bright` | `明るく、弾んだ声で` | `感情的で、声を張り上げて、早口に` | 3.2 |
| `sad` | `低い声で、沈んだ調子に` | 同左（单锚，不换档） | 3.5 |
| `flirty` | `柔らかく、優しく囁くように` | `息を荒くして、甘く喘ぐように、近い距離で` | 4.0 |
| `intimate` | `柔らかく、優しく囁くように` | `息を荒くして、甘く喘ぐように、近い距離で` | **4.5** |

- 这 7 句是 LoRA 训练清单里**真实出现过**的 caption；码本外的描述属于分布外条件，
  模型只能"猜"（`emotion_codebook.py:2-14`）。
- `intimate` 与 `flirty` 共用两条锚，只有 cfg 不同。
- v4 码本 v2 额外放行 `neutral + 柔らかく、優しく囁くように`（cfg 3.0），
  给"平静但耳语"用（`emotion_codebook.py:45-49`）。
- cfg 是**引导强度**，不是情绪强度旋钮；实测同一描述下 cfg 2/3/4/6 时长完全相同
  （`voice_caption.py:97-99`）。所以强度靠换锚（弱/强）和措辞，不靠调 cfg。

### 4.2 旧 v2 风格表（仅 `plan_version=2` 用，留作对照）

`scene_plan.py:38-49`：

| 风格 | caption | cfg | 族 |
|---|---|---|---|
| `moan` | 息が荒く、甘く喘ぐように、声を漏らして、とろけた声で | 4.5 | intimate |
| `tremble` | 息混じりで、声を震わせて、途切れ途切れに、甘く | 4.0 | intimate |
| `shy` | 恥ずかしそうに、声を震わせて、小さく照れながら | 3.5 | intimate |
| `flirty` | 甘く誘うように、近い距離感で、色っぽく囁いて | 4.0 | flirty |
| `happy` | 嬉しそうに、明るく弾んだ声で、楽しげに | 3.2 | bright |
| `surprised` | 驚いたように、息を呑んで、慌てた声で | 3.5 | bright |
| `sad` | 悲しそうに、声を落として、切なげに | 3.5 | sad |
| `soft` | 柔らかく、優しく、落ち着いた声で | 2.5 | neutral |
| `calm` | 落ち着いた自然な声で、丁寧に | 3.0 | neutral |

> v2 的 `shy/tremble` 在 v3 以后都折进 `intimate` 族，**这些 caption 本身已不在生产码本里**。

---

## 5. 一句台词怎么被判到某一档

判定是"规则瀑布"，顺序即优先级（`scene_plan.py:88-107`）：

1. **emoji**（`EMOJI_MAP`，`scene_plan.py:16-22`）
   - 😳😖🥺`///` → shy
   - 😊😄😆✨🎉♪ → happy
   - 😏💕♥❤💗💦 → flirty
   - 😭😢🥲 → sad
   - 😱😨❗⁉ → surprised
2. **拟声/喘息**（`_moanish`，`scene_plan.py:81-85`）
   - 拟声词表 `ONO`：`んっ あっ はぁ ふぅ ひゃ んぅ はあ ぁ… あ… ん… ふ… ちゅ ちゅぱ ぺろ んぐ うぅ うっ くぅ くっ ふっ すぅ ひぃ あぁ んぁ はふ ふぁ`
   - 命中 **≥2 个** → moan；命中 1 个且句子里还有 `だめ/敏感/そこ/弱い/もっと/奥/ビク/とろけ/感じ/濡れ/熱い/震え/締ま/激し/欲し/やめ/いや/痛い` → moan。
   - 拟声必须落在词/句边界（`_ONO_BOUNDARY`），`あったかい` 里的 `あっ` 不算。
3. `SHY` 正则：恥ずかし/照れ/はにか/うつむ/赤面/もじもじ/顔が熱/何でもない/`//`
4. `FLIRTY` 正则：わがまま/近い/色っぽ/誘う/甘え/好き/大好き/会いたい/離さな/独り占め…
5. `HAPPY` / `SAD` / `SURPRISED` / `SOFT` 正则（顺序见 `scene_plan.py:30-37`）
6. 都没命中 → `calm`（neutral 族）。

补充：

- **纯拟声短句**（去掉拟声词和标点后 ≤2 字）才会直接进 `moan`（`_is_interjection`，`scene_plan.py:113-118`）。
- 没有信号的句子，v2 会看相邻行（`_ctx_style`）并粘滞（`_sticky`），v3/v4 **不看邻句**，
  改为继承 `prev` + 衰减。
- **括号舞台提示**：`（小声で）`/`囁くように`/`息を荒くして` 这类日语表层提示，
  只有 v6 的 `ALBION_JA_STAGE_CUE` 才认（`scene_plan.py:563-565`）。
  **v4 生产不认**——实测 `（小声で）今日は…` 在 v4 下仍是 `neutral/weak`。
- 中文括号提示（`（气息不稳）` 等）走桥的 `src_cues`，需要 `ALBION_CUE_SCOPE` + 桥的情绪腿；
  日语直通没有这一路。

### 5.1 emoji：底模能力 vs 链路用法（别混）

底模自带 emoji 控制（`tts/irodori/checkpoints/EMOJI_ANNOTATIONS.md`），与亲密/喘息相关的：

| emoji | 日文释义 | 链路里用没用 |
|---|---|---|
| 👂 | 囁き、耳元の音 | **没用**（底模支持） |
| 😮‍💨 | 吐息、溜息、寝息 | 只在桥开启 `ALBION_VOICE_EMOJI=1` 时作为**句末**非语言 emoji 追加（白名单 `🤭 😮‍💨 🥱 😭`，每条回复最多 1 个） |
| 🥵 | 喘ぎ、うめき声、唸り声 | **没用** |
| 🌬️ | 息切れ、荒い息遣い、呼吸音 | **没用** |
| 👅 | 舐める音、咀嚼音、水音 | **没用** |
| 💋 | リップノイズ | **没用** |
| 🤐 | 口を塞がれて | **没用** |
| 🥺 | 声を震わせながら、自信のなさげに | 只用于**输入分类**（→ shy），不写进文本 |

要点：

- `scene_plan.EMOJI_MAP` 是**读**输入里的 emoji 来判风格，不是往文本里**写** emoji。
- 桥的句末 emoji（`voice_caption.py:689-752`）只收笑声/叹气/哈欠/真哭四个，
  **不收** 👂🥵🌬️👅💋 这些亲密向的。
- 所以"亲密/喘息"目前完全靠 **caption 锚 + cfg**，底模的 👂🥵🌬️ 能力在链路里**没被用上**。
  想试的话，得在日语正文里手工插 emoji（走底模原生能力），但那是另一条路，未在本链路验证。

---

## 6. 渲染：`POST /v2/render`

### 6.1 生命周期

```text
POST /v2/render/open   {"voice": "albion"}
  → {"session_id": "...", "generation": 1, "epoch": "...", "render_lifecycle_version": 2}

POST /v2/render        （每个片段一次）
  → SSE：event: audio_chunk / data: {"audio_base64": "..."}
          event: done

POST /v2/render/close  {"session_id": "...", "generation": 1}
```

`session_id` 必须每次运行唯一；同一 `(session_id, generation)` 关闭后不能复用。

### 6.2 渲染请求体（照抄）

```json
{
  "session_id": "render-20261028-...",
  "generation": 1,
  "plan_version": 4,
  "voice": "albion",
  "format": "wav",
  "units": [{
    "unit_id": "albion-005-0",
    "sub_index": 0,
    "text": "……",
    "family": "intimate",
    "caption": "柔らかく、優しく囁くように",
    "cfg": 4.5,
    "scene_id": 4,
    "tier": "weak",
    "level": 0.34,
    "codebook_version": 2
  }]
}
```

- `plan_version=4` 时 **必须带 `codebook_version: 2`**，否则 400（`render_queue.py:627-629`）。
- `family/caption/cfg` 必须落在码本白名单里（`render_queue.py:630-637`）。
- 单段 `text` ≤200 字符、≤255 token（`render_queue.py:578-592`）。
- 想用训练集外的自由描述：加 `"caption_mode": "free"`，caption ≤120 字、cfg 1.5–7.0
  （`render_queue.py:517-546`）。这是评测口，**生产默认不走**；本次四份成品也没用。

### 6.3 输出

服务端不落盘：音频以 SSE 帧返回，单段一份完整 WAV。客户端自己写文件/拼接。
实测格式：48 kHz、16-bit、单声道。

---

## 7. 拼接

- 段间留 **0.25s** 静音（`ab_stream_v4.py:110-123` 同款做法）。
- **不套** `tts/stream/gap_shaper.py`：生产流式会按 -40dB/-55dB 裁句尾再补静音，
  批次离线渲染不裁，能保留模型自然的呼吸尾巴（亲密段尤其需要）。
- 四份成品都是"逐段 WAV → 顺序拼接"，没有做响度归一化/压限。

---

## 8. 踩过的坑（每条一句话 + 出处）

| 坑 | 结论 | 出处 |
|---|---|---|
| 拒绝句被念得很享受 | v5 起 `refusal_guard` 把"高唤醒=bright"落到拒绝/制止/安抚句时换成 sad/neutral 并压回弱档；显式 `style_override` 不拦 | `tts/irodori/refusal_guard.py`；`scene_plan.py:787-826`；`docs/情感条件化-v4-上线记录.md:43-47` |
| 长段落一个味 | v3 删掉"整篇 40% 合并"，改逐句滞回（有信号立即切、没信号 0.17/句衰减、8 句释放） | `scene_plan.py:415-420,437-439` |
| 长段吞句/截断 | 单段 30s 音频上限 + 255 token 文本上限，超了静默截断；按 60 字切最稳 | `irodori_tts/inference_runtime.py:217`；`scene_plan.py:883`；`render_queue.py:580-592` |
| 200 字硬校验 | `/v2/render` 单段 >200 字符直接 400 | `render_queue.py:578` |
| cfg 4.5 的来由 | 是 `intimate` 族的固定引导强度（与 flirty 4.0、sad 3.5 同表），**不是情绪强度**；大凤还会被 joint_cfg 4.5 统一覆盖 | `emotion_codebook.py:41`；`irodori_serve.py:160-164`；`docs/history/情感条件化方案-astra.md:61` |
| 声音发糙/发飘 | 大凤去掉 joint 引导后整篇"发糙/发飘"（用户判定），所以保留 joint=4.5 | `irodori_serve.py:37` |
| 电音/沙哑 | 金狮/大凤改用手挑干净子集重训的嵌入后消失；低步数 LoRA（200 步）漂移小 | `irodori_serve.py:32,42` |
| 句尾被切（です/ます 的「す」） | 旧 -40dB 判据把句尾清化音当静音裁掉；现在按 -55dB + 保留 30ms 衰减 + 条件淡出 | `tts/stream/gap_shaper.py:10,34-44` |
| 慢句被超时砍掉=吞句 | 单句 GPU 时间涨到 30–51s 时 45s 软超时会丢整句；批量渲染要给足超时（本次 600s/段） | `stream_server.py:143-148`；`irodori_serve.py:183` |
| 空译文整句静音 | 桥的译文任何一段为空就整体失败，不静默滤掉（否则就是吞句） | `bridge.py:299` |
| v4 规划异常 | v4/v5 版本隔离下宁可 400 也不悄悄退回 v2（否则记录不带 codebook_version，render 也拒） | `scene_plan.py:1084-1096` |
| 200 字符可达 801 token | token 计数失败不许放过，否则等于让底模静默右截断 | `docs/情感条件化-v4-上线记录.md:17`；`render_queue.py:583-589` |

---

## 9. 本次实测结果

生成时间 2026-10-28，后端 `:9889`（五船单进程），plan_version=4，40 步，未指定 seed。

| 台本 | 片段数 | 音频时长 | 文件大小 | 族/档分布 |
|---|---|---|---|---|
| 阿尔比恩 | 37 | 344.4s（5:44） | 31.5 MB | intimate/strong 32、neutral/weak 4、intimate/weak 1 |
| 布雷斯特 | 60 | 336.9s（5:37） | 30.8 MB | intimate/weak 20、bright/weak 17、intimate/strong 17、neutral/weak 6 |
| 大凤 | 61 | 380.8s（6:21） | 34.9 MB | intimate/strong 18、flirty/weak 17、bright/weak 14、intimate/weak 9、neutral/weak 2、bright/strong 1 |
| 金狮 | 78 | 410.7s（6:51） | 37.6 MB | intimate/strong 24、neutral/weak 16、bright/weak 13、intimate/weak 11、sad/weak 6、flirty/weak 5、bright/strong 2、neutral/strong 1 |

音频 QC（整段）：48 kHz / 16-bit / 单声道；峰值 1.000；近静音占比 37–41%
（含段间 0.25s 与句内换气）；削顶样本 15–45 个/份（约 1e-6 量级）。
**没有做人耳听感验收**——字段正确不等于自然，这一步只能由人来听。

---

## 10. 复现命令

```powershell
# 1) 确认后端在跑（三选一）
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9889/health

# 2) 整批渲染（四份台本 → tests/output/tts-persona-full-20261028/*.wav）
cd D:\AI\Wife
$env:PYTHONIOENCODING='utf-8'
D:\AI\Wife\tts\irodori\.venv\Scripts\python.exe -u -B `
  tests\output\tts-persona-full-20261028\render_persona.py

# 3) 只跑一份
... render_persona.py --only albion
```

`render_persona.py` 的关键参数：`PLAN_VERSION=4`、`SEG_CHAR_LIMIT=60`、`GAP_S=0.25`、
`context_before` 取最近 400 字、每段 `/v2/render` 超时 600s。
脚本只读台本、不打印原文。

单段手调（curl/httpx 等价）：

```powershell
$plan = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9889/v2/plan `
  -ContentType 'application/json' -Body (@{units=@(@{unit_id='u1';text='はぁ……んっ……';advance=$true});plan_version=4;emotion_policy='cascade'} | ConvertTo-Json -Depth 5)
$plan.units[0] | ConvertTo-Json -Depth 5
```

---

## 11. 边界与未确认

- **人耳听感未验收**：本文只证明链路与字段正确，不证明"好听/自然"。
- **随机采样**：没传 seed，重跑结果不同；要可复现就显式给 `seed`。
- **大凤 effective cfg**：joint_cfg=4.5 覆盖计划 cfg，计划里的 3.2/3.5 不会生效（字段仍照实记录）。
- **自由 caption 路径**（`ALBION_VOICE_CAPTION=1` + 桥）没有参与本次四份成品；
  生产启动脚本默认开着它，走壳/桥时条件可能变成自由描述（≤120 字、cfg 3.0）。
  本方法直接打 `:9889`，拿到的是码本锚。
- **v6 能力未开**：日语舞台提示（`（小声で）`）、跨句指令、hint 自发声、换族 level 重置
  都是 v6 项，v4 不认。
- **30s 截断**是从 `max_seconds=30.0` 与实测"正好 30.00s"推断的；没有逐字核对被截掉多少。
- **底模 emoji（👂🥵🌬️ 等）未接入链路**，只在 `EMOJI_ANNOTATIONS.md` 里有能力说明。

---

## 改动限制

本文与 `tts/persona/` 下的成品音频、渲染脚本，按用户 2026-10-28 的直接指令生成。
未经用户明确同意，任何委派方/自动化代理不得改动、覆盖、重新发布或挪作他用。
成人向合成音频只供本机私用。
