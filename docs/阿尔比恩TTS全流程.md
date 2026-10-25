# 阿尔比恩 TTS 全流程（2026-09-19 版）

> 本文只写**现在真在跑**的那一套。标 ✓ 的是我刚实测过的；标（据配置文件/历史）的是从文件与旧记录读来的，我没重跑训练那一环。
> 2026-10-24 补记：壳现在先走 `:9885` 流式（`tts/stream/`，切句与播放时序），再到 `:9881` 桥；本文的四条线仍成立。
> 情感条件化与规划部分以 `docs/情感条件化-v4-上线记录.md` 为准（本文写于 v4 之前）。

---

## 0. 一张图看清（四条线）

```
① 数据线    官方日配 mp3 + 官方日文原文  →  清洗/配对/标注  →  训练清单
② 训练线    清单 → Irodori-TTS 微调（LoRA / 说话人嵌入）→ 权重
③ 推理线    :9889 Irodori 后端（单进程，5 船）  ←  LoRA 按船挂载（ships.json）
④ 使用线    壳内说话 → :9885 流式（切句/时序）→ :9881 桥（中文自动译日语）→ :9889 → WAV → 播放
            聊天语音 → 同一个桥（同一个音色）→ 语音消息
```

**端口一览**

| 端口 | 是什么 | 谁拉起 |
|---|---|---|
| `9889` | Irodori-TTS 后端（**5 船一个进程**） ✓ | `tts\irodori\启动Irodori后端.bat`（经 `start_backend.bat` 转发）|
| `9885` | **流式 TTS**：切句、播放时序与会话（壳现在走这里） | `tts\stream\start_stream.bat` |
| `9881` | **桥**：OpenAI 兼容的语音接口 + 中文→日语翻译 ✓（流式与直连都走它） | `tts\irodori\启动TTS桥.bat`（经 `start_bridge.bat` 转发）|
| `9880` | Denia 专用的旧 GPT-SoVITS 后端（**可选**，平时不启 ✓） | `tts\GPT-sovits\启动Denia后端.bat` |
| `9882` | 耳朵：中文语音识别（paraformer） ✓ | `pet\start_ear.bat` |

---

## ① 数据线：语料怎么来的

**事实**：阿尔比恩**没有官方中文配音**（CV 只有日配：白城なお）。所以走的是
**官方日文语音 + 官方日文原文**一一对应，中文只做**翻译文本**参考。

1. **抓**：从碧蓝航线 wiki（biligame）扒台词资源，每句 = 音频地址 + 紧跟其后的台词文本（页面是"文本+mp3 地址"裸文本结构，以 `</a>` 为块边界切）
2. **配对**：把"哪句话 ↔ 哪个 mp3"配成一对，抽样人工核验
3. **去标题词**：像"获取台词""自我介绍"这类标题不是念出来的，**必须剔掉**，否则会教坏模型
4. **标注**：文本**只用官方日文原文**，不许拿中文倒推；术语（`セントーア`、`大戦`、`アルビオン`…）经两轮 ASR 交叉核对 + 人工修正
5. **产物**：`audio/`（每句一对 `xxx.mp3` + `xxx.txt`）+ 索引（`metadata.jsonl` / `*.list`）
   - 首批：75 句 / 总时长 **18.4 分钟**（据旧记录）

> 训练集的铁律：**文本与音频必须一一对应且都是官方原文**；ASR 只用来核验，不用来生成标注。

---

## ② 训练线：Irodori-TTS 微调

目录：`D:\AI\Wife\tts\irodori\`（上游 Irodori-TTS 仓库 + 本项目的配置）

| 文件 | 作用 |
|---|---|
| `prepare_manifest.py` | 把 `audio/` 整理成训练清单（manifest） |
| `configs\train_*.yaml` | 训练配置（本项目用 LoRA / 说话人嵌入两条思路） |
| `quantize_checkpoint.py` | 量化检查点（给推理提速 / 降显存） |
| `infer.py` / `gradio_app*.py` | 单机推理 / 可视化界面（调试用，服务不靠它们） |

**两条微调路线**（据配置与历史记录）：
- **LoRA**：`configs\train_v4_small_lora*.yaml` —— 本项目五船大多走这条
- **说话人嵌入（speaker inversion）**：`configs\train_v4_small_speaker_inversion.yaml` —— 少数船用

**产出的权重**落在上游约定的 `outputs\` 下，按目录名引用（见 ③ 的 `ships.json`）。
详细的训练命令、踩坑与参数，见技能库 `irodori-tts-local`。

---

## ③ 推理线：`:9889` 后端（单进程 5 船）

- 启动：`tts\irodori\启动Irodori后端.bat` → 真正跑的是
  `.venv\Scripts\python.exe -u irodori_serve.py` ✓
- 它是**一个进程里装 5 条船**（省显存、省加载时间）；HTTP 路径 `/tts` ✓
- 加载约 15 秒（第一次更久），所以启动器里桥会**等它 10 秒**再起 ✓

### 船与权重的对应表：`ships.json` ✓

```json
"albion":  { "lora": "albion_lora_emo_spk",  "inv": null }
"taihou":  { "lora": "taihou_lora_clean200", "inv": "taihou_inv_clean", "joint_cfg": 4.5 }
"shinano": { "lora": "shinano_lora_emo_spk", "inv": null }
"brest":   { "lora": "brest_lora_emo_spk",   "inv": null }
"jinshi":  { "lora": "jinshi_lora_clean200", "inv": "jinshi_inv_clean" }
```

**这张表热更新**：`改完自动生效，不用重启` ✓（下划线开头的键被忽略）。想换某条船的 LoRA / 关掉 LoRA（`lora_off: true`），改这里就行。

---

## ④ 桥：`:9881`（中文聊天也能出声的关键）

文件：`tts\irodori\bridge.py`（374 行 ✓），端口 `9881` ✓，后端写死
`BACKEND = "http://127.0.0.1:9889/tts"` ✓。

### 对外端点（OpenAI 兼容）

| 端点 | 作用 |
|---|---|
| `GET /v1/models` | 列出可用音色（六船：albion / taihou / shinano / brest / jinshi / denia ✓）|
| `POST /v1/audio/speech` | 传 `{model, input, voice, response_format}` → 回 WAV ✓ |

### 它替我们做的三件事

1. **中文自动译日语** ✓
   ```python
   _CN_ONLY = re.compile(r"[的了吗吧呢这]")   # 中文特征字
   if _CN_ONLY.search(text):  speech = to_japanese(text)   # 假名占比也参与判定
   ```
   ⇒ 你在壳里跟她说中文、在聊天里发中文，最后**出声的都是日语** ✓
   （实测：「指挥官，欢迎回来。」→ 200 / 322 KB WAV / 7.2s ✓）
2. **按音色找参考音频 / 权重**：把 `voice` 名翻成后端的船名；Denia 例外，
   参考音频写死在 `DENIA_REF = D:/AI/Wife/tts/denia/audio/...wav` ✓，并且要另起 `:9880` 后端 ✓
3. **记录入口原文**：每个请求第一行就把**翻译前的原文**写进 `logs/tts/_breq.log` ✓
   —— 注意：**这个日志里出现中文是正常的**，不代表翻译没跑（曾经误判过一次）。

> 桥只绑 `127.0.0.1`（安全考虑），不对外暴露 ✓。

---

## ⑤ 使用线 A：壳里说话（桌宠的"嘴"）

```
她生成文字
  → 壳的语音管线（packages/stage-ui/src/components/scenes/Stage.vue）
  → 切句器：现在**整句合成**（minimumWords/maximumWords = 无穷大、boost=0、ttsMaxConcurrent=1）
  → 提供方：settings/speech/active-provider = openai-compatible-audio-speech ✓
             active-model = albion，voice = albion，language = zh-CN ✓
  → POST http://127.0.0.1:9881/v1/audio/speech
  → 桥 → :9889 → WAV → 壳播放
```

**为什么要"整句合成"**：早期是默认切句器（每 4~12 个词一块），会**短句抢着说**（实测一百毫秒内塞进 4 条）。改成整句后，一次请求 = 一整句 ✓。

**验收方法**（我常用的）：从壳的真管线灌一句 → 看 `logs/tts/_breq.log` 是否收到 ✓
（实测：灌 2 句 → 日志收到 2 条 `voice=albion` ✓）

---

## ⑥ 使用线 B：聊天语音（我发给你的语音条）

- 同一条桥、同一个音色 ✓
- 流程：**想说的内容 → 翻译成日语 → 桥合成 → 语音消息**
  （日语原文不必发给你）
- 分工约定：**问候 / 回应 / 晚安可以发语音；工作与任务汇报只发文字** ✓

---

## ⑦ 耳朵：`:9882`（听你说话）

- `pet\start_ear.bat` → 中文语音识别（paraformer）✓
- 它是**中文**模型 ⇒ 拿日语去测当然识别不出来（我实测确认过，属正常）✓

---

## ⑧ 排障速查（都是真踩过的）

| 现象 | 真正的原因 | 怎么查 |
|---|---|---|
| 双击"一键启动"没反应 | ① 启动器引用了一个**不存在**的 `start_all.bat`；② 静默层往**被占用的**共享日志重定向 ⇒ cmd 连重定向都开不了 ⇒ 一句都不执行 | 试 `[System.IO.File]::Open($f,'Open','Write','None')` 能否独占写 |
| 她说"看不到屏幕" | 多半是**视觉模型那一步**失败（网络被占满时更容易） | 三端各取一张图 + 看 `眼睛.log` |
| 她取图 403 | 采集端的 token ≠ 壳里的 token（**两处持有者**） | 用真值取一张图，200 就是对的 |
| 语音是静音 | 桥只念日语；纯中文若没走翻译就会出静音 | 看 `logs/tts/_breq.log` 的原文 + 桥日志 |
| 短句抢着说 | 切句器按词切 | 改成整句合成（见 ⑤）|
| 启动后没声音 | 后端要 15 秒加载，桥会等 10 秒 | `9889` / `9881` 各探一次 |
| 换了 LoRA 不生效 | 忘了 `ships.json` 是热更新的，但**权重目录名**要写对 | 看后端启动日志里的载入行 |

---

## ⑨ 一键操作用什么

| 想干什么 | 双击 |
|---|---|
| 全起（后端→桥→耳朵→脑→眼睛→壳，最后自检 7 项）| `D:\AI\Wife\一键启动-构建版.bat`（无窗口；带参数运行能看到进度）|
| 全关 | `D:\AI\Wife\一键关闭.bat` |
| 只开声音那两件 | `tts\irodori\启动Irodori后端.bat` → 等 15 秒 → `启动TTS桥.bat` |
| 单独开 Denia 音色 | `tts\GPT-sovits\启动Denia后端.bat`（`:9880`）|
