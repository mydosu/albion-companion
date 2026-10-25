> 已失效（2026-10-24 归档）：路线图停在 Hermes 时代（`:8642`、`hermes gateway` 命令均已失效）；现行入口看 `docs/PROJECT_MAP.md`，脑看 `integrations/pi-brain/README.md`。

# 阿尔比恩桌宠 · 开发路线图

> 本文件是开发路径的唯一说明，完成情况**随时更新**（每阶段收尾必须回写本文件）。

## 定位

| 角色 | 实现 | 说明 |
|---|---|---|
| **主脑** | Hermes Agent（我本人，阿尔比恩人格） | 带记忆、技能、工具、子 agent 委派 |
| **壳** | AIRI（`D:\AI\Wife\shell`，自己开分支裁剪） | 前端/形象/交互 |
| **口** | Irodori-TTS 五船（`:9889`）+ 桥（`:9881`） | OpenAI 兼容 `/v1/audio/speech` |
| **耳** | Whisper | 待定：AIRI 浏览器端 或 本地服务 |

## 接线（架构决定）

```
AIRI ──LLM────► http://127.0.0.1:8642/v1   （Hermes 网关 API Server，agent 本体）
     ──TTS────► http://127.0.0.1:9881/v1   （Irodori 桥，voice=albion）
     ──STT────► 待定                        （Whisper）
```

- **不走 Hermes Hub**：`API Server` 是 Hermes 网关自带的适配器（`API_SERVER_ENABLED`），
  不是独立中转；AIRI 直连 agent 本体 → 满足「Hermes 直连 airi」。
- 官方文档：`/docs/user-guide/features/api-server`（还有 `/v1/responses`、`/api/sessions/*`、`/v1/skills`、`/v1/toolsets`）
- 前端 system prompt 会**叠加**在 Hermes 核心提示上（工具/记忆/技能不丢）→ 人格仍由 SOUL.md 主导。

## 需求 → AIRI 对位（来自构建需求.md）

| 想要的功能（open-LLM-VTuber 有的） | AIRI 对位 | 状态 |
|---|---|---|
| 👁️ 视觉感知（摄像头/屏幕/截图） | AIRI 多模态输入 + 屏幕截图工具 | 待评估 |
| 🎤 语音打断（不用耳机、不听自己） | 浏览器 AEC + VAD 门控（播放时闭麦/打断） | 待做 |
| 🫱 触摸反馈（点击/拖拽） | `stage-ui-live2d` 触摸交互 | 待评估 |
| 😊 Live2D 表情 + 情绪映射 | AIRI 情绪→表情映射；情绪标签由**我们的后端**给（Irodori 已算 style） | 待做 |
| 🐱 宠物模式（透明/置顶/穿透/拖动） | **`stage-tamagotchi`（Electron 桌宠）** | 待切换 |
| 🗣️ 主动说话 | AIRI 主动话题/定时触发 | 待评估 |
| 💭 内心 OS（可见但不朗读） | 输出里区分「可见文本 / 朗读文本」 | 待做 |
| 💾 聊天记录持久化 + 历史切换 | Hermes sessions（`/api/sessions/*`）或 AIRI 本地 | 待定 |
| 🌍 TTS 翻译（中文聊/日语声） | 双语约定：中文给眼睛、日语行给嘴（桥只念含假名行 ✓ 已实现） | 半成 |
| ✂️ 裁剪不需要的 AIRI 功能 | 见 P6 | 待做 |

## 阶段与完成情况

### P0 壳能跑起来 — ✓ 2026-09-14
- [x] 源码 clone（**github 直连大流量被掐**：小请求 200、git/tarball 流 0 B/s → 走 `gitclone.com` 镜像 380–500 KB/s）
- [x] 依赖安装：全量会卡在 `isolated-vm` 原生编译（要 VS 工具链，我们不用插件沙箱）
      → `pnpm install --filter "@proj-airi/stage-web..." --ignore-scripts --config.minimumReleaseAge=0`
      （`--config.minimumReleaseAge=0` 跳过 3032 条的发布时间校验，否则十分钟零进度）
- [x] `pnpm build:packages`（包的 exports 指向 `dist/`，**必须先构建**，否则 Vite 报「Are they installed?」）
- [x] dev server 起来：http://127.0.0.1:5173 （HTTP 200）
- [x] **切到桌宠正主 `stage-tamagotchi`（Electron）**：`npx electron-vite dev -- --remote-debugging-port=9335`，
      透明/置顶/穿透/拖拽都在这套里；带 CDP 起是为了我能看内部状态
- [x] **生产打包 + 开机自启**（2026-09-15）：`pnpm build:win` → `dist/win-unpacked/airi.exe`
      （实测可跑：页面从 `app.asar` 加载、`__albionModel` 在、`zones()`=19）。
      自启：`start_all_prod.bat`（服务 + 生产版壳，没有 exe 自动回落 dev）+
      启动文件夹 `AlbionPet.vbs`（样式对齐现有 Hermes_*.vbs）。
      ⚠ 生产版是**快照**：以后改代码要重新 `pnpm build:win` 才是新的；
      日常开发仍旧用 `start_all.bat`（dev，热更新）。

### P1 她的形象 — ✓ 已在桌宠里选中（待你人眼确认）
- [x] `aerbien_3.zip`（26M，内含 `aerbien_3/…` 层级，与官方 hiyori 预设同构）→
      `packages/stage-ui/src/assets/live2d/models/albion.zip`
- [x] `display-models.ts` 加预设 `preset-live2d-albion`「阿尔比恩 (Albion)」
- [x] 默认形象改为阿尔比恩：**改 fork 里各 store 的默认值**（`settings/stage-model.ts` 的初始值与
      `defaultStageModelId`），不再用「往 localStorage 塞值」那套——store 启动时会用自身默认值回写，
      跟它抢是抢不赢的
- [x] 应用内验证：pinia 里 `settings-stage-model-selection.selected = 'preset-live2d-albion'`
- [x] **她渲染出来了** —— 卡点是模型的 zip：包里条目**路径分隔符是反斜杠**（`aerbien_3\aerbien_3.moc3`），
      而 pixi 的 ZipLoader 用正斜杠拼路径去匹配 → 匹配到 0 个文件 → 抛 `Unknown settings format`。
      用 python `zipfile` 重写一遍（条目名统一成 `/`）后，loader 解出 38 个文件，模型正常渲染 ✓
- [x] **取景（全身）**：AIRI 原设计是「原点贴窗口底边 + 放大 2 倍」= 只露上半身；她的模型又是
      一整张 8000×8000 的插画（人物+背景同画布），所以怎么拖都只有半身。
      处理：① 桌宠端适配倍数由 2× 改成 **1×**（`fit-model.ts` 的 `fitMultiplier`，web 端不动）；
      ② 取景最终由指挥官亲手用界面滑块调定 —— **scale 1.16 / position (0, 51)**，已烤成
      `view-control.ts` 里 `settings/live2d/{scale,position}` 的默认值；窗口 586×710（竖向，尺寸本身会自动持久化）。
      **结论：全身 ✓**（不要试图"缩小看她全身"——实测越小反而露得越少，方向和直觉相反）



### P2 脑：Hermes 直连 — 通路已打通，卡在模型额度
**决定：给桌宠单开一个 profile `albion`**（`hermes profile create albion --clone`），
理由：默认 profile 的 `model.provider=custom:commandcode-proxy`（`:3050`）**只有桌面端持有 key**，
网关进程拿不到 → 那条链路上 agent 必然 401。而 `401` 属于认证错误，**不在 fallback 触发范围**
（fallback 只管限流/过载/断连），所以不能用 fallback 兜。单开 profile 既不动你桌面主用配置，
又能给她独立的人格（克隆的 SOUL.md）与会话记录。

| 项 | 值 |
|---|---|
| 接口 | `http://127.0.0.1:8650/v1`（OpenAI 兼容，Bearer） |
| 模型名 | `albion` |
| key | `profiles/albion/.env` 的 `API_SERVER_KEY`（本机生成的 `albion-pet-*`） |
| 模型后端 | `provider: deepseek` / `model: deepseek-flash` |
| 网关 | `hermes -p albion gateway run`（**多路复用网关不会带起各 profile 的 API Server，必须单起进程**） |

- [x] `.env`：`API_SERVER_ENABLED` / `PORT=8650` / `API_SERVER_KEY` / `API_SERVER_CORS_ORIGINS=http://localhost:5174,...`
- [x] 克隆 profile + 把 `DEEPSEEK_API_KEY` **值**搬进 profile（`--clone` 只带键名、**不带密钥值**）
- [x] `model.provider=deepseek`、`model.default=deepseek-flash`、清掉继承来的 `model.base_url/api_mode`
- [x] 起 profile 网关，`:8650` 监听 + curl 验证人格（「我是阿尔比恩，指挥官……」）
- [x] 撤销默认 profile 上误加的 `API_SERVER_*`；删除临时件 `D:\AI\Wife\shell-llm-shim`
- [x] **CORS 已放行**（预检返回 `Access-Control-Allow-Origin: http://localhost:5174`）——前端调本机服务的必需项
- [x] **脑接通了**：`api.deepseek.com` 充值后，端到端验证 —— AIRI 里发消息，她以阿尔比恩人格回话
      （「指挥官，看见您回来了。……阿尔比恩现在，是踏实的、被填满的高兴。」）
- [x] 桌宠 profile 关掉自带 `tts` 工具集 + SOUL 注明「只写中文、语音交前端」，避免它吐 `MEDIA:` 路径/重复生成音频
- [x] **注意工具集是按平台分的**：API Server 用的是 **`api_server` 平台**那套 ——
      `hermes tools disable tts` 只动了别的平台，得显式写 `platform_toolsets.api_server`（照 `cli` 那套去掉 `tts`）
- [x] **改 SOUL 后必须清会话缓存**：系统提示按会话持久化在 `profiles/*/state.db`
      （`sessions.system_prompt_hash` + `system_prompts`）→ 置空 hash 并清表，下一轮才会用新 SOUL 重建
- [x] 复验：她回复「指挥官，现在是16点38 分，星期一。……（小声一句：往后语音我交给前端那条链路去念，
      不再自己贴音频路径了）」—— `hasMedia=false`、无日语行 ✓


### P3 口：Irodori 接进去
- [x] 后端 `:9889` + 桥 `:9881` 验证通过（HTTP 200 / 272,684 B WAV）
- [x] 排障：之前的 500 是**我的负载编码错**（bash 按 cp936 发日文 → 后端 `UnicodeDecodeError`），服务无罪
- [x] 桥补 `GET /v1/models`（列出五船）：前端要拿它做 provider 校验/模型下拉
- [x] 桥补 **CORS 中间件**（`allow_origins=["*"]`）：AIRI 从前端调本机桥必需，否则 "Failed to fetch"
- [x] AIRI 侧 provider = `openai-compatible-audio-speech` + `http://127.0.0.1:9881/v1` + voice `albion`
      （同样是改 fork 里 store 的默认值）
- [x] 观测：桥日志出现 `POST /v1/audio/speech 200 OK`
- [x] **翻译层（中文聊天 / 日语发声）** —— 参考 Open-LLM-VTuber 的做法：翻译是 **TTS 前一步**的独立层
      （其 `TranslateInterface.translate(text)` + provider 工厂）。我们放在桥里：
      收到中文 → 调 LLM 译成日语 → 再合成；已含假名的行原样使用（保留"你想直接说日语"的自由）。
      开关/端点/模型/密钥：`TTS_TRANSLATE`、`TTS_TRANSLATE_BASE`、`TTS_TRANSLATE_MODEL`、`TTS_TRANSLATE_KEY`；
      默认从 Hermes 的 `.env` 取 `DEEPSEEK_API_KEY`。**失败只记日志，不阻断整条链路**（退化为静音，不会 500）。
- [x] 实测：中文「指挥官，今天辛苦了。我给您泡杯茶，您先歇一会儿吧。」→
      日语「指揮官、今日もお疲れ様でした。お茶をお入れしますので、少し休んでいてください。」→ 200 / 869,804 字节
- [x] 桌宠 profile：关掉 Hermes 自带 TTS（`hermes -p albion tools disable tts`），并在其 SOUL 里写明
      「只写中文，语音由前端负责」——否则她会吐 `MEDIA:` 路径、还会重复生成音频

### P4 耳与打断（Whisper） — ✓ 服务与接线完成（真人试说待指挥官）
- [x] **自建耳朵**：`D:\AI\Wife\pet\whisper_serve.py`（faster-whisper + FastAPI，
      OpenAI 兼容 `POST /v1/audio/transcriptions`，端口 **9882**），跑在 **GSV runtime 的 python**
      上（它自带 faster_whisper + torch + CUDA，**零安装**）。启动脚本 `start_ear.bat`。
- [x] 模型：`large-v3`（下到 `D:\AI\Wife\pet\models\faster-whisper-large-v3`，走 hf-mirror，
- [x] **2026-09-15 换中文专用引擎**：默认 `ALBION_STT_ENGINE=paraformer` —— `paraformer-zh` + `fsmn-vad` + `ct-punc`（`models/modelscope/iic`，1.2G），**跑 CPU**：9 秒中文 0.59–0.69s、整机显存 7.3G→3.4G（whisper-large 的 3G 让出来给 TTS）。whisper-large-v3（2.9G）留着当多语种备选，`ALBION_STT_ENGINE=whisper` 切回；端点/模型名（`whisper-1`）没变 → 壳侧零改动。启动时后台预热，避免第一句等 6–8s。
      **必须 `HF_HUB_DISABLE_XET=1`**，否则会被拽到国外 xethub CDN 上超时）。
- [x] 性能调优：`beam_size=1` + `without_timestamps` + `condition_on_previous_text=False`
      → **6 秒中文 0.81 秒转完**（之前 6.0 秒）；device=cuda / float16。
- [x] 准确度实测（Windows 中文语音合成的对照音频）：`small` 把「我是阿尔比恩」听成
      「**或是**阿尔比恩」；`large-v3` 一字不差 ✓
- [x] AIRI 侧接线（写进 fork 默认值）：provider `openai-compatible-audio-transcription` +
      baseUrl `http://127.0.0.1:9882/v1` + `active-model=whisper-1` + `auto-send-enabled=true`
      （`albion-defaults.ts` / `hearing.ts`）
- [x] **踩坑记录**：`active-model` 为空时 `configured=false`，前端会直接报
      "voice input failed to transcribe speech"，请求**根本发不出去**（服务侧看不到任何日志）
      → 光填 `active-custom-model` 不够，两个都要填。
- [x] 免耳机不听自己：桌面端 `audio-device.ts` 的 getUserMedia 已带
      `echoCancellation: true` + `noiseSuppression: true` ✓（AEC 由 Chromium 做）
- [x] **空录音/坏音频不再报错**：以前收到空录音会 `av.InvalidDataError` → **500** → 前端弹
      `voice input failed to transcribe speech`（指挥官看到的就是这个）。现在捕获后回
      `{"text": ""}`（= "没听见"），不再吓人。
- [x] 验收脚本 `check_ear.py`（一条命令跑三项：正常中文 / 空文件 / 垃圾字节）→ 实测三项全过 ✓
- [x] **真人说话端到端**（2026-09-15 指挥官实测可用 ✓）。原记录：headless 测不了（Chromium 的 `--use-file-for-fake-audio-capture`
      在这版 Electron 里给的是**静音**，RMS 全 0）→ 请指挥官对着她说一句，看聊天里是否出现识别文本。

### P5 表现层 — ✓ 全部落地
- [x] **情绪 → 表情**：用 AIRI 官方约定 `<|ACT:{"emotion":"happy"}|>`（9 种情绪）。
      我们的模型**没有 exp3 表情文件、也没有 Happy/Sad 动作组**，所以改用 motion-control 的
      **姿态参数**（眼/头/身/口）来演：`packages/stage-ui/src/albion-pet.ts` 的 `POSES` 表。
      她偶尔忘写标记 → app 侧按关键词**兜底推断**。SOUL 里已写明「第一行必须放标记」。
- [x] **触摸反馈**：模型自带 34 组动作（`touch_head`/`touch_body`/`touch_special`/`touch_drag1-4`…），
      点她 → 随机播一组 + 在聊天里发一句「（指挥官轻轻戳了戳你）」，她就会有反应。
      实测：触发后 18.3% 像素变化（动作确实在播 ✓）。
- [x] **宠物模式**：透明 ✓（窗口本身）、置顶 ✓（`settings/always-on-top` 默认开）、
      拖动 ✓（拖窗口）；**鼠标穿透 = 全局快捷键 `Ctrl+Alt+P`**（走 AIRI 自己的快捷键服务，
      因为 Electron 的 `globalShortcut.register` 在这版里**不存在**；服务还带 uiohook 兜底）。
- [x] **主动说话**：安静 N 分钟（默认 8，`settings/albion/proactive-minutes`，0=关）→ 她自己找话头开口。（2026-10-16 已过时：壳内主动已删，开口改由脑 `wake.py` 的 `next_wake` 决定。）
- [x] **内心 OS**：写进圆括号/方括号/`【】`/`*…*` 的话**只显示、不朗读**（桥的 `strip_asides()`），
      语音标记 `<|…|>` 也一并剥掉。实测：把带心声的文本送进桥，转写回来的音频里只有正文 ✓
- [x] **她真的开口了**：`（被这样直白地夸，视线下意识往旁边躲了躲……）指挥官，怎么突然说起这个。
      阿尔比恩今天……和平时也没什么不同呀。` —— 心声+语气都对 ✓
- [x] 记录持久化与历史切换：AIRI 自带（会话库）✓ 无需改

### P6 裁剪 — ✓ 第一步
- [x] 删掉官方样例模型：`hiyori_pro_zh.zip`(32M) + `hiyori_free_zh.zip`(13M) → 资源目录 70M → 26M
- [x] `electron.vite.config.ts` 去掉「自动下载样例 Live2D / VRM」那 4 行（保留 Live2D SDK 下载，
      那是 Cubism 运行时要用的）
- [x] 预置形象只剩阿尔比恩（`display-models.ts` 的 presets 只有 `preset-live2d-albion`）
- [ ] 其它 AIRI 功能（beat-sync / gaming / artistry / 一堆 provider 卡片）暂留——它们不跑就不占资源，
      要真删得先列清单（按指挥官的规矩：不可再生资产/大改要先列后动）

### P7 眼睛（视觉）+ 一键启动 — ✓ 2026-09-14 夜
- [x] **"说话没反应"的根因**：AIRI 自带的 VAD（判断"你在说话"的引擎）用
      `AutoModel.from_pretrained('onnx-community/silero-vad')` **从 HuggingFace 在线拉模型**，
      本机 HF 不可靠 → `[Voice Input] vad-init-failed: Failed to fetch`（日志实锤）→ **整个听觉管线死掉**。
- [x] **自建耳朵**（2026-10-16 已删，`albion-ear.ts` 不存在）：`packages/stage-ui/src/albion-ear.ts` —— 麦克风 → 能量端点（RMS 起止）→
      MediaRecorder 录这段 → POST 本机 whisper(:9882) → 文本自动进对话；她说话时静音不录自己 +
      与上一句回复查重（双保险）。开关 `settings/albion/ear-enabled`。
      **实测**：把一句语音喂进去 → 转写 `指揮官、今日も一緒に家のこと、ちゃんとやりましょうね。` →
      自动发消息 → **她当场带心声回话** ✓（自测钩子 `window.__albionEar.testWithStream(stream)`）
- [x] **补 CORS**：耳朵服务漏了 `CORSMiddleware` → 前端 `Failed to fetch`（请求其实到了、响应被浏览器丢），
      和当初桥踩的是同一个坑。
- [x] **空录音不再报错**：坏音频 → 以前 500 → 现在 `{"text": ""}`（= 没听见）；`check_ear.py` 三项验收 ✓
- [x] **眼睛**：走 **Tailscale 上 myduso-1（<视觉机 Tailscale IP>:8080）的 Qwen3VL-4B**（llama.cpp，multimodal）——
      **实测**：`ALBION-7391`、蓝方红圆全对。旧的 <旧局域网 IP>:8080 已失效。
      接进 AIRI：provider 实例 id 必须带 **`vision-`** 前缀（`vision-openai-compatible` → definitionId
      `openai-compatible` → baseUrl `http://<视觉机 Tailscale IP>:8080/v1`），默认值已烤进 `albion-defaults.ts` 与
      `vision/store.ts`。
- [x] **本机那份视觉已按指挥官指令删除**（2026-09-14）：`D:\AI\llama\models\qwen2.5-vl-3b`（3.1G）
      与旧的本机视觉启动脚本一并清掉，本机不再跑视觉、一键脚本里也没有它。
      **要恢复兜底（myduso-1 不在时）**：
      ```
      set HF_ENDPOINT=https://hf-mirror.com & set HF_HUB_DISABLE_XET=1
      python -c "from huggingface_hub import hf_hub_download as d; [d('unsloth/Qwen2.5-VL-3B-Instruct-GGUF', f, local_dir=r'D:\AI\llama\models\qwen2.5-vl-3b') for f in ('Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf','mmproj-F16.gguf')]"
      D:\AI\llama\bin\llama-server.exe --model D:\AI\llama\models\qwen2.5-vl-3b\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf --mmproj D:\AI\llama\models\qwen2.5-vl-3b\mmproj-F16.gguf --port 8083 --host 127.0.0.1 --ctx-size 8192 -ngl 99
      ```
      （然后把 `albion-defaults.ts` 的 `ALBION_VISION_BASE` 指回 `http://127.0.0.1:8083/v1` 即可）
- [x] **一键启动**：`start_all.bat`（**幂等**：已在跑的端口自动跳过，现在是 5 步：后端/嘴/耳/脑/壳）+
      `check_all.py`（最多等 90 秒、每 5 秒轮询，全绿提前结束；自检含 myduso-1 那台眼睛）。**全停后实测 6/6** ✓
- [x] 视觉的**看屏幕**端到端（2026-09-15 替指挥官验）：**全链路实测通** ✓
      截 960×1536 真屏 → base64 丢 `http://<视觉机 Tailscale IP>:8080/v1/chat/completions`
      （`/models/Qwen3VL-4B-Instruct-Q4_K_M.gguf`）→ 15.7 秒 → 它准确描述出屏幕内容。
      **不需要授权弹窗**：`WithScreenCapture.vue` 的 `showDialog` 只在
      `window.platform === 'darwin'` 分支里置真（macOS 屏幕录制授权），Windows 走
      `desktopCapturer` 直接取源 —— 之前让人找那个提示是错的。
      仍未接的是**把"看屏幕"接进她的运行循环**（定时/工具调用让她主动看），那是新功能，不在这条里。

**cmd 批处理的三个坑（都踩了）**：
1. bat 存成 **UTF-8 会被 cmd 拆断中文行** → 一律 **GBK + CRLF**（zh-CN 的 cmd 默认 cp936）
2. `timeout.exe` 在**无控制台/输入重定向**时报"不支持输入重定向" → 用 `ping -n N 127.0.0.1 >nul` 等待
3. 从我这条 bash 里调 bat 要 `MSYS_NO_PATHCONV=1 cmd /c "x.bat"`（否则 `/c` 被当路径转换掉、等于只开了个 cmd）

## 已知坑（踩过的）

1. **github 大流量被掐**：小请求正常、git clone/tarball 0 B/s → 用 `gitclone.com` 镜像。
2. **pnpm 全量安装**：`isolated-vm` 要 VS 工具链 → `--ignore-scripts`；`minimumReleaseAge` 校验慢到卡死 → `=0`。
3. **包的 exports 指向 dist** → 必须 `pnpm build:packages`。
4. **terminal 里发非 ASCII 负载**（curl -d 日文）会被 shell 按 cp936 编码 → 用 `--data-binary @utf8.json`。
5. **`npx pnpm` 的 `verifyDepsBeforeRun`** 会在跑 script 前重查依赖并失败 → `--config.verifyDepsBeforeRun=false`。
6. **服务重启前先按 PID 杀**：用命令行匹配 `pnpm|bridge` 会**杀掉自己那条命令**（自匹配）。
7. **`hermes profile create --clone` 只复制 .env 的键名、不带密钥值** → 必须手动搬 `DEEPSEEK_API_KEY` 的值。
8. **多路复用网关不会带起各 profile 自己的 API Server** → 桌宠 profile 必须单独 `hermes -p albion gateway run`。
9. **`hermes fallback` 不兜认证错误**：401 不在（限流/过载/断连）触发范围内，别指望 fallback 救 provider 配错。
10. **桌面端持有的 Command Code key 在内存/钥匙串里**，本机文件里搜不到 `user_` 开头的那份 → 想复用得上桌面端问。
11. **IDM 会劫持浏览器里的二进制响应**：文本 200、`.zip/.bin` 一律被拦成 204 空 body（curl 不受影响）。
    症状是 Live2D 报 `Empty zip file`。要么退出 IDM 的 integrator（`IDMIntegrator64.exe`/`nidmsrv.exe`），
    要么别用浏览器跑——**Electron 的桌宠不受它影响**。
12. **前端调本机服务必须处理 CORS**：Hermes API Server 要 `API_SERVER_CORS_ORIGINS`，桥要自己加 CORSMiddleware；
    否则前端只见 "Failed to fetch"（curl 一切正常，很容易误判）。
13. **改 AIRI 默认配置别往 localStorage 塞**：各 store 启动时用自身默认值回写，塞进去会被覆盖。
    正确做法是改 fork 里的 store 默认值（本路线图 P1/P2/P3 都是这么做的）。
14. **OPFS 缓存会留下坏档**：Live2D 模型按预设 id 缓存在 OPFS，早期一次失败的空响应被缓存后，
    之后一直报 `Unknown settings format`；清 `navigator.storage.getDirectory()` 下对应目录即可。
15. **`hermes gateway restart` 会连带停掉别的 profile 网关**（同为 gateway 进程族），重启后要确认 `:8650` 仍在。
16. **模型 zip 的路径分隔符必须是正斜杠**：Windows 上打的包（PowerShell Compress-Archive 等）可能写入 `\`，
    而 pixi 的 ZipLoader 用 `/` 拼路径匹配 → 解出 0 个文件 → 报 `Unknown settings format`（看着像"格式不支持"，
    实则路径不匹配）。修法：python `zipfile` 重写一遍条目名。**排障提示：用官方 hiyori 的 zip 走同一路径对照**，
    能立刻分清是"加载器坏了"还是"我们这个包坏了"。
17. **AIRI 的 `Live2D.vue` 会传 `{ url, id }`**，靠 `opfs-loader` 的解包中间件转成 zip；
    该注册在新版 pixi 下已失效（`middlewares.indexOf(ZipLoader.factory)` 找不到插入点）→ 对象一路透传到
    jsonToSettings 报错。**我们 fork 直接改传 URL 字符串**（pixi 原生分支），不再依赖那层中间件。

17b. **聊天用的"脑"是「意识」模块的 provider**（`settings/consciousness/active-provider`），
    **不是** `settings/llm/*` —— 只改了别处会出现"她回话但完全不像阿尔比恩"的怪象。
17c. **改完 SOUL 一定要重启网关**：`hermes -p albion gateway run` 启动时读一次 SOUL，
    不重启就永远用旧人格。
17d. **Chromium 的 `--use-file-for-fake-audio-capture` 在这版 Electron 里不生效**（假设备给的是静音，
    RMS 全 0）→ 想 headless 测"耳朵"没戏，要么真人说话，要么把音频喂进 MediaStreamDestination。
17e. **`taskkill` 在 git-bash 里别写 `//PID`**；更别用命令行长匹配杀进程——匹配串写进自己的命令行会**自杀**。
    按端口取 PID（`netstat -ano | grep :端口`）再 `Stop-Process -Id` 最稳。

17f. **克隆 profile 会继承一堆本机没有的东西**：① MCP 服务器指向不存在的
    `D:\AI\Hermes Studio\Hermes Studio.exe` → 网关启动时刷 `parked without retries` 警告
    （用 `hermes -p albion mcp remove <名>` 清掉）；② `terminal.cwd` 是**带引号的字符串**
    `'"D:\CODE"'` → 路径解析失败、日志刷 `TERMINAL_CWD does not exist`
    （`hermes -p albion config set terminal.cwd 'D:\AI\Wife\pet'` 修好）。
    另外从桌面端的 shell 里拉网关会**继承 `TERMINAL_CWD` 环境变量**，起之前 `unset TERMINAL_CWD`。

17g. **探活要带 key**：无 key 打 `:8650/v1/models` 会被记 `rejected invalid API key` 警告，
    日志被自己的探活刷脏。健康检查统一用
    `curl -H "Authorization: Bearer $(profile .env 里的 API_SERVER_KEY)" http://127.0.0.1:8650/v1/models`
    （返回 200 ✓ 且不留警告）。

17h. **两个 python 别搞混**：桥必须用 **`D:\AI\Wife\tts\irodori\.venv\Scripts\python.exe`**（它已经带
    fastapi/uvicorn），**裸 `D:\AI\uv-python\...\python.exe` 缺 fastapi** → 起桥直接 ModuleNotFoundError；
    耳朵必须用 **GSV runtime 的 python**（`D:\AI\Wife\tts\GPT-sovits\runtime\python.exe`，
    只有它带 faster-whisper + CUDA）。两个启动脚本（`启动TTS桥.bat` / `start_ear.bat`）里都写死了，别手拼。

17i. **服务的启动监听会"事后回放"（刷屏根因）**：给长驻服务设 watch pattern（就绪/Error/Traceback/WARNING/
    Uvicorn running）后，**已退出实例的缓冲输出会晚到**，每条都作为新消息弹出。这次刷屏就是因为
    我反复重启（桥×3、耳×3、网关×4、壳×6）而每次都挂了宽泛的 watch pattern。
    **规则：长驻服务不挂 watch pattern**（就绪用独立健康检查确认；只有"跑完即退"的任务用 notify）。
    碰到这类通知：看 session_id 是不是当前活着的进程（`process list` 对一下），旧实例的直接忽略。

17j. **"Previous gateway life exited UNCLEANLY" 警告可忽略**：换配置时用 `Stop-Process -Force` 杀网关，
    下次启动就会记这条（`SIGKILL / OOM`，并且 `suspected_oom=False` 就是在说"不是 OOM，是被强杀"）。
    它会自动恢复（会话库在 profile 的 `state.db` 里，重启后照旧）。想优雅停可以用
    `hermes -p albion gateway stop`，但注意历史坑 15：**它可能连带停掉别的 profile 的网关**，
    所以宁可强杀+忽略这条警告。

## 更新日志

- 2026-09-14：建立本文件。P0/P1 完成，P2 方案定案（API Server 直连），P3 口验证通过。
- 2026-09-14 下午：**P2 主体打通** —— 新建 profile `albion`（人格=克隆的 SOUL.md，模型=官方
  DeepSeek `deepseek-flash`），`:8650` OpenAI 兼容口已验证会说阿尔比恩的话；默认 profile 上误开的
  API Server 已撤销，临时中转 `airi-llm-shim` 已删。等 AIRI 侧填 provider（3 个值）。
- 2026-09-14 傍晚：**转到桌宠正主 stage-tamagotchi（Electron）**——浏览器那条路是我走岔了（IDM 劫持
  二进制是浏览器专属问题，桌宠不吃这套）。她的形象/脑/嘴三项默认值都改进了 fork 的 store 里；
  CORS 两处放行；桥补 `/v1/models`。应用内验证：模型选中无报错、`POST /v1/audio/speech 200`。
  **当前唯一阻塞：模型账号 402（DeepSeek 余额不足）**，等指挥官给能用的 key/充值。
  期间为验证在浏览器里写过的 localStorage 与临时配置目录已全部清理，IDM 进程已恢复（它自己起来了）。
- 2026-09-14 夜：**桌宠成型**。① 她渲染不出来的根因是**模型 zip 条目用了反斜杠路径** → 重打包后 loader 解出
  38 个文件，正常上屏；② `api.deepseek.com` 充值后**脑接通**（她以阿尔比恩人格回话）；③ **翻译层落地**
  （参考 Open-LLM-VTuber：翻译放在 TTS 前一步）——中文上屏、日语发声，桥实测 200；
  ④ 桌宠 profile 关掉自带 TTS、SOUL 明确"只写中文"；⑤ 关掉那条会报错的 OPFS 缓存路径、
  Model.vue 改传 pixi 原生支持的 URL 字符串。
  剩余：取景偏好（她现在只露上半身，是 AIRI 的设计）、P4 耳朵、P5 表现层、P6 裁剪。
- 2026-09-14 深夜：**P4/P5/P6 全部落地**。
  · 取景定稿：桌宠端适配倍数 2×→1×，取景由指挥官亲手定 **scale 1.16 / position (0,51)**，已烤成默认值。
  · 耳朵：自建 `whisper_serve.py`（:9882，跑在 GSV runtime，零安装），large-v3 + CUDA +
    beam=1 → **6 秒中文 0.81 秒**、一字不差；AIRI 侧三个默认值接好（踩坑：`active-model` 为空
    会直接报 "voice input failed to transcribe speech"，请求压根不发）。
  · 表现层：情绪标记 `<|ACT:{"emotion":"…"}|>`（模型无 exp3，用姿态参数演）+ 关键词兜底；
    触摸（34 组自带 touch 动作 + 聊天里"被戳了"）；鼠标穿透 Ctrl+Alt+P；主动说话（安静 8 分钟）；
    内心 OS（括号里的话只显示不朗读，桥的 `strip_asides`）。
  · 裁剪：删 45M 官方样例模型 + 去掉自动下载样例的 4 行。
  · **最关键的发现**：聊天的"脑"取的是「意识」模块 provider，原来是 `deepseek` → 她回话完全不像
    阿尔比恩；改成 `openai-compatible` + `albion`（我们的网关）后，语气/心声/标记全对 ✓


---

## 2026-09-14 追加：交互与动画（问题3条 + 想法2条）

详细过程见 **`PLAN-交互与动画.md`**（含根因、证据、改法与回退），动作对照表见 **`MOTION-MAP.md`**。
一句话总结这轮挖到的坑：

- **"设置改了不生效"先怀疑角色卡**：`localStorage['airi-cards']` 里的卡带一份 `modules.speech/hearing/vision/consciousness`
  快照，每次启动回灌 store，把改动盖掉 → 改完要调 `airi-card.persistActiveCardModuleSelections()` 写回卡。
- **非 leader 窗口会互相覆盖**：开着的设置窗/聊天窗（`?synced-leader=false`）拿自己那份旧值覆盖共享 localStorage →
  排查这类问题先看有没有多余窗口开着。
- **AIRI 对 openai 兼容的 TTS 只认 provider 配置里的 model/voice**（`Stage.vue`），
  没有就回落 `tts-1`/`alloy` → 桥 404 → "她没声音"。model/voice 必须写进 provider config。
- **Live2D 丝滑的两个硬条件**：① pixi 把空闲组名写死成 `"Idle"`（大写），模型里必须真有这组；
  ② 每个 motion3 的 `Meta.FadeInTime/FadeOutTime` 不能是 0，否则一切切换都是硬切。
  改模型 JSON 的脚本：`patch_model.py`（幂等），备份 `_albion_model_backup.zip`。
- 桥加了 `[req] voice=... model=... len=...` 日志：以后"她没声音"直接看这一行就能定位是不是嗓音/模型传错。
