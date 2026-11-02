# 壳首次启动要设的东西

对象：一台陌生机器上第一次启动 Electron 壳（`shell/apps/stage-tamagotchi`，产物 `airi.exe`）之后，
哪些配置**已经由代码兜底**、哪些**必须人工补**。每条都给 `文件:行号` 出处；找不到确实证据的标「未确认」。

判断依据只有一条：`.env.local` 的 `VITE_*` 是 **Vite 编译期内联**（`shell/apps/stage-tamagotchi/.env.example:1`
写明「改完重新打包外壳才生效」，`00-复现向导.md:179` 同样提示），而壳里点出来的设置存在渲染端
**localStorage**（`useLocalStorage*`）或 Electron **userData** 的文件里。两者互不覆盖。

## 最短路径（照做即可）

1. 先在 `shell/apps/stage-tamagotchi/.env.local` 填 `VITE_ALBION_LLM_KEY`（= 脑的 `API_SERVER_KEY`），
   其它地址保持默认，然后 `pnpm -F @proj-airi/stage-tamagotchi build:unpack`。
   —— 不填这支 key，聊天 provider 根本不会预置（`albion-defaults.ts:48`），开机就是没脑子。
2. 确认两个角色包在位：`assets/live2d/models/albion.zip`、`assets/spine/models/albion-spine-38.zip`
   （`display-models.ts:48-49` 按文件名引用，`.gitignore:13` 忽略 `*.zip`，必须自己放，见 `docs/REBUILD.md:62-63`）。
3. 启动一次，让它自己写 onboarding 标记与默认 provider（`applyAlbionDefaults()`，`renderer/main.ts:54`）。
4. 只有换模型、换上游时才动界面；形象、窗口位置、缩放都不用设（下面表里标「否」的项）。
5. 要在壳里听声音，先把桥/流式服务拉起来；否则她出字不出声（不是壳配错了）。

## 配置项总表

| 配置项 | 存在哪 | `.env.local` 能否覆盖 | 首次要不要手设 | 出处 |
|---|---|---|---|---|
| 脑地址（OpenAI 兼容） | 构建期常量，默认 `http://127.0.0.1:8650/v1` | 能（`VITE_ALBION_LLM_BASE`） | 否，本机默认就对 | `packages/stage-ui/src/albion/albion-defaults.ts:14` |
| **脑的 key** | localStorage `settings/providers/configured` 里 provider 的 `config.apiKey` | 能（`VITE_ALBION_LLM_KEY`），但**必须重新打包**；不填该 provider 不预置 | **是**（要么打包前填，要么界面里补） | `albion-defaults.ts:15`、`:48`、`stores/providers/config.ts:47` |
| 聊天 provider 选择 | localStorage `settings/consciousness/active-provider`，默认 `openai-compatible` | 不能 | 否（默认就指向脑） | `stores/modules/consciousness.ts:22`、`albion-defaults.ts:30` |
| 聊天模型名 | localStorage `settings/consciousness/active-model`，默认 `albion` | 不能 | **只在脑的 profile 不叫 `albion` 时**要改 | `stores/modules/consciousness.ts:23`、`albion-defaults.ts:31` |
| TTS 桥地址 / 音色名 | localStorage `settings/speech/*`，默认 `http://127.0.0.1:9881/v1`、`albion` | 能（`VITE_ALBION_TTS_BASE`、`VITE_ALBION_TTS_MODEL`） | 否；换船名才要（桥的 `VOICES` 里必须有这个音色） | `stores/modules/speech.ts:60-62`、`albion-defaults.ts:16-17`、`tts/irodori/bridge.py:40-41` |
| 流式 TTS 地址与 voice | localStorage `settings/speech/*` + provider `albion-stream-tts` | 能（`VITE_ALBION_STREAM_TTS_BASE`） | 否 | `albion-defaults.ts:22-24`、`:68-77` |
| 语音识别（耳朵） | localStorage `settings/hearing/*`，默认 `:9882/v1`、`paraformer-zh` | 能（`VITE_ALBION_STT_BASE`） | 否 | `stores/modules/hearing.ts:335-337`、`albion-defaults.ts:26-27` |
| 视觉模型地址 | provider `vision-openai-compatible`，默认**空** | 能（`VITE_ALBION_VISION_BASE`）；为空则不预置该 provider | 想用壳里的看图才要；眼睛主要走脑的 `ALBION_EYES_VLM` | `albion-defaults.ts:37-39`、`:88-97`、`stores/modules/vision/store.ts:19-21` |
| 默认形象（皮肤） | localStorage `settings/stage/model`，默认 `preset-spine38-albion` | 不能 | 否 | `stores/settings/stage-model.ts:19`、`albion-defaults.ts:43` |
| 角色模型包本体 | `packages/stage-ui/src/assets/{live2d,spine}/models/*.zip` | 不能（是文件，不是变量） | **是**（仓库不含，缺了舞台空） | `stores/display-models.ts:48-49`、`docs/REBUILD.md:62-63` |
| 模型缩放 / 位置 | localStorage `settings/live2d/scale|position`、`settings/spine38/scale|x|y` | 不能 | 否；拖一下、滚轮缩放会自动存，托盘「推荐尺寸」还能一键套一套推荐值 | `apps/stage-tamagotchi/src/renderer/bridges/stage-layout.ts:41-45`、`src/main/tray/recommended-layout.ts:18-33`、`:45-59` |
| 主窗位置 / 尺寸 | `<userData>/app-config.json` 的 `windows[]` | 不能 | 否，移动/缩放后自动写 | `src/main/windows/main/app-config.ts:8-24`、`windows/main/index.ts:102-127` |
| 托盘三开关（沉底 / 锁拖动 / 锁滚轮缩放） | `<userData>/app-config.json` 的 `windowFlags` | 不能 | 否，默认都不勾 | `windows/main/app-config.ts:25-29`、`windows/main/window-flags.ts:21-23` |
| 角色卡（名字、system prompt、模块绑定） | localStorage `airi-cards`、`airi-card-active-id` | 不能 | 否；首次启动自动建内置卡 `default`（ReLU） | `stores/modules/airi-card.ts:44-45`、`:368-377` |
| 卡片对模块/形象的绑定 | 卡片 `extensions.airi.modules`，激活时覆盖壳内设置 | 不能 | 否；**改完形象/模块要写回卡**，否则下次切卡被卡里的旧值盖回去 | `stores/modules/airi-card.ts:400-456`（尤其 `:442-444`） |
| onboarding 标记 | localStorage `onboarding/completed`、`onboarding/skipped` | 不能 | 否，代码启动时自动置 `'true'` | `albion-defaults.ts:110-114`、`renderer/main.ts:54` |
| 语音 sidecar 目录（让壳自己拉 TTS/耳朵） | `<userData>/albion-sidecars.json` | 不能（是另一个文件；也可用进程环境变量 `ALBION_IRODORI_DIR` 等） | 否；用 `start_all.bat` 起服务就不需要 | `src/main/services/airi/albion-sidecars.ts:29-45`、`:71-82` |
| userData 目录本身 | 进程环境变量 `APP_USER_DATA_PATH` | 不能（不是 `VITE_`，也不是构建期） | 否 | `src/main/index.ts:72-74` |
| 触摸音用的桥地址 / 音色 | localStorage `settings/albion/tts-bridge`、`settings/albion/tts-voice`，默认 `http://127.0.0.1:9881`、`albion` | 不能 | 否 | `packages/stage-ui/src/albion/albion-pet.ts:126-127` |
| 输出音频设备 / 自动播放策略 | — | 不能 | 未确认（没在代码里找到写死设备或开关的地方） | — |

## 三个容易踩的点

1. **角色卡里没有人设。** 出站请求只带最后一条 user 消息（`stores/chat.ts:208-210`），卡片的 system prompt
   （`stores/modules/airi-card.ts:28-35`）不会发给脑。她的口吻来自脑的 `SOUL.md`，改卡不会改口吻。
2. **先填 env 再首次启动。** provider 表的默认值只在该 localStorage 键还不存在时生效
   （`stores/providers/config.ts:47`，`useLocalStorage` 首次会写入默认值）。已经跑过一次再补
   `VITE_ALBION_LLM_KEY` 并重打包，旧键还在，得在界面里补 key 或清掉那份 localStorage。
3. **改了 `.env.local` 一律要重新打包。** `import.meta.env` 是编译期替换，直接换 exe 才有效；
   dev 模式（`pnpm dev`）改完重开 dev 也会生效。

## 与 00 复现向导的关系

`00-复现向导.md:189` 已经写了「聊天 provider 与 key、布局、角色卡」要在壳里补。本表是那句话的展开：
其中**布局**和**角色卡**其实都有代码兜底、不用手设，真正必须人工处理的只有**脑的 key**、
**两个角色包**，以及（在换模型时）**模型名**。
