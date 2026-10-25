# ASR（耳朵）路径清单

> 用途：以后换 ASR 模型 / 换路径 / 切引擎时，照这份表逐处改，不用再满仓库找。
> 生成：2026-09-22 · 扫描范围 `D:\AI\Wife`（已排除 `node_modules` / `site-packages` / 模型自带 README / i18n）
> **当前实况**：引擎 `paraformer`（中文专用，CPU）· 端口 `9882` · 模型在 `pet\models\modelscope\iic\`（1.11 GB，3 个目录）

---

## 一、总览：改一个地方要连带改哪几处

| 想改什么 | 必须一起改 |
|---|---|
| **换模型目录** | `pet\start_ear.bat`（`ALBION_FUNASR_ROOT`）· `whisper_serve.py`（默认值 `_FUNASR_ROOT`）· `albion-sidecars.ts`（`ALBION_FUNASR_ROOT`）· `docs\打包与语音服务.md` |
| **换端口** | `pet\start_ear.bat` · `albion-sidecars.ts` · `albion-defaults.ts`（`ALBION_STT_BASE`）· 三个启动 bat 的 `:check` · `一键关闭.bat` · `check_all.py` · 托盘标签 |
| **切引擎**（paraformer ↔ whisper） | `pet\start_ear.bat:4` · `albion-sidecars.ts:108` · `albion-defaults.ts:25`（模型名）· 两处 `hearing.vue` 默认值 |
| **换整台耳服务**（搬到别的机器） | `albion-defaults.ts:24`（`ALBION_STT_BASE`）· 文档 |

（壳自建的备用耳朵 `albion-ear.ts` 已于 2026-10-16 删除，上表不再列它。）

**环境变量一览**（耳朵读的，都在 `whisper_serve.py:14-19` 有说明）

| 变量 | 默认 | 谁设的 |
|---|---|---|
| `ALBION_STT_ENGINE` | `paraformer` | `start_ear.bat:4` · `albion-sidecars.ts:108` |
| `ALBION_FUNASR_ROOT` | `<耳朵目录>\models\modelscope\iic` | `start_ear.bat:5` · `albion-sidecars.ts:109` |
| `ALBION_STT_PORT` | `9882` | `start_ear.bat:6` · `albion-sidecars.ts:110` |
| `ALBION_STT_MODEL` | `small` | 只有切 whisper 引擎才用 |
| `ALBION_STT_DEVICE` | `auto` | 代码默认，没人设 |
| `ALBION_STT_PRELOAD` | `1` | 代码默认 |
| `ALBION_EAR_DIR` | `D:/AI/Wife/pet` | `albion-sidecars.ts:77`（打包时用） |
| `ALBION_GSV_PYTHON` | — | 耳朵/桥/后端共用的 python |
| `ALBION_SIDECARS` | — | 设 `0` 关掉壳自启三件套 |

---

## 二、耳朵服务本体（`D:\AI\Wife\pet\`）

| 文件 | 行 | 内容 | 作用 |
|---|---|---|---|
| `start_ear.bat` | 2 | `title 阿尔比恩的耳朵 (paraformer-zh :9882)` | 窗口标题 |
| | 4 | `set ALBION_STT_ENGINE=paraformer` | **引擎开关** |
| | 5 | `set ALBION_FUNASR_ROOT=%~dp0models\modelscope\iic` | **模型目录** |
| | 6 | `set ALBION_STT_PORT=9882` | 端口 |
| | 8 | `"%~dp0..\tts\GPT-sovits\runtime\python.exe" whisper_serve.py` | 解释器 + 入口 |
| `whisper_serve.py` | 2, 5, 6 | 文件头说明（跑法 / BaseURL） | 注释 |
| | 8-19 | 引擎与 5 个环境变量说明 | 注释 |
| | 32 | `ENGINE = os.environ.get('ALBION_STT_ENGINE', 'paraformer')` | 引擎默认值 |
| | 33 | `MODEL_NAME = os.environ.get('ALBION_STT_MODEL', 'small')` | whisper 引擎默认模型 |
| | 35 | `PORT = int(os.environ.get('ALBION_STT_PORT', '9882'))` | 端口默认值 |
| | 37 | `_FUNASR_ROOT = ...` | **模型根目录默认值** |
| | 38-40 | 三件套目录名（paraformer / fsmn-vad / ct-punc） | **三个模型路径** |
| | 56-67 | paraformer 分支：`AutoModel(model=..., vad_model=..., punc_model=...)` | 加载 |
| | 68-80 | whisper 分支：`WhisperModel(MODEL_NAME, device, compute_type)` | 加载 |
| | 86 | `/health` 返回 `engine` + `model` | 判活用 |
| | 93 | `/v1/models` 列 `['paraformer-zh','whisper-1']` | 壳校验用 |
| | 100 | `model_name: str = Form('whisper-1', alias='model')` | **请求里的模型名（耳朵不看它）** |
| | 107-128 | paraformer 处理分支 | 写临时 `.wav` 交给 funasr |
| `check_all.py` | 76, 84 | `http://127.0.0.1:9882/health` | 自检 |
| `tools\check_ear.py` | 2, 18, 26 | 耳朵验收脚本（`model=whisper-1`） | 测试 |
| `models\modelscope\iic\` | — | `speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` / `speech_fsmn_vad_zh-cn-16k-common-pytorch` / `punc_ct-transformer_zh-cn-common-vocab272727-pytorch` | **模型文件本体（1.11 GB）** |

---

## 三、壳侧（`D:\AI\Wife\shell\`）

| 文件 | 行 | 内容 | 作用 |
|---|---|---|---|
| `packages\stage-ui\src\albion\albion-defaults.ts` | 23-25 | `ALBION_STT_BASE`（默认 `http://127.0.0.1:9882/v1`）/ `ALBION_STT_MODEL = 'paraformer-zh'` | **壳看到的地址与模型名** |
| | 33 | `ALBION_STT_PROVIDER_ID = 'openai-compatible-audio-transcription'` | provider id |
| | 77-81 | `out[ALBION_STT_PROVIDER_ID] = { config: { baseUrl: ALBION_STT_BASE } }` | 写进 provider 默认值 |
| `packages\stage-ui\src\stores\modules\hearing.ts` | 19 | import `ALBION_STT_MODEL` | — |
| | 336-338 | `active-provider` / `active-model` / `active-custom-model` 的 localStorage 默认值 | **AIRI 听力控制的默认值** |
| `apps\stage-tamagotchi\src\main\services\airi\albion-sidecars.ts` | 4, 7, 31 | 三件套端口注释 / 覆盖用环境变量 | 注释 |
| | 77 | `earDir: process.env.ALBION_EAR_DIR \|\| cfg.earDir` | **耳朵目录（打包时覆盖）** |
| | 103-110 | `port: 9882` / `args: ['whisper_serve.py']` / `ALBION_STT_ENGINE` / `ALBION_FUNASR_ROOT` / `ALBION_STT_PORT` | **壳自启耳朵的全部参数** |
| | 134, 143 | `ALBION_SIDECARS === '0'` 关掉 / 起不来的报错文案 | 开关 |
| `apps\stage-tamagotchi\src\main\index.ts` | 156 | 模块 A「语音」注释 | 注释 |
| `apps\stage-tamagotchi\src\main\tray\index.ts` | 437 | 托盘菜单 `耳·STT :9882 就绪/未就绪` | **托盘标签（端口写死）** |
| `packages\stage-pages\src\pages\settings\providers\transcription\openai-compatible-audio-transcription.vue` | 140, 174-175, 230 | 默认模型 `whisper-1` 与回退逻辑 | 设置页 |
| `packages\stage-pages\src\pages\settings\modules\hearing.vue` | 321, 491, 519 | 默认模型 `whisper-1` | 设置页 |

> 无关项（**别动**）：`libs\providers\providers\modelscope\` = AIRI 自带的 ModelScope 聊天 provider；
> `libs\inference\constants.ts` 的 `onnx-community/whisper-large-v3-turbo` = 浏览器内本地 ASR，与本链路无关。

---

## 四、启动 / 关闭脚本（`D:\AI\Wife\`）

| 文件 | 行 | 内容 |
|---|---|---|
| `一键启动.bat` | 参数转发 | 转发 `start_all.bat dev`，保留可见控制台 |
| `一键启动-dev.bat` | 参数转发 | 转发 `start_all.bat dev`，后台运行 |
| `start_all.bat` | `rem 3) 耳朵 STT` 段 | 唯一 STT 启动实现，探测 9882 后调用 `pet/start_ear.bat` |
| `一键关闭.bat` | 13, 29 | 端口表 `(9889 9881 9885 9882 8650 8790 9335)` |
| `_收控制台.ps1` | 1 | 进程名匹配表（含 `start_ear` / `whisper_serve`） |

---

## 五、训练 / 打标工具（引用**另一个** ASR 模型，该目录已不存在）

四个脚本都写死 `D:/AI/models/faster-whisper-lv3t-ct2`（CT2 版 large-v3-turbo）：

| 文件 | 行 |
|---|---|
| `tts\GPT-sovits\audio\_records\_tools\asr_subset.py` | 18 |
| `tts\GPT-sovits\audio\_records\_tools\check_asr_spot.py` | 14 |
| `tts\GPT-sovits\audio\_records\_tools\cut_split.py` | 60 |
| `tts\GPT-sovits\audio\_records\_tools\prep_asr_ship.py` | 51 |

> 都是 2026-09-12 五船打标那次用的一次性工具，项目已收工；要用先把模型拉回来或改这四行路径。
> 依赖声明：`tts\GPT-sovits\extra-req.txt:1`（`faster-whisper`）、`requirements.txt:13,19`（`funasr==1.0.27` / `modelscope`）。

---

## 六、文档（改完记得同步）

改耳朵/端口前这些文件都要跟着改。2026-09-22 曾按行号列过，2026-10-24 文档归位后行号已失效；
按文件名搜 `9882`、`ALBION_STT`、`paraformer` 定位：

- `docs/阿尔比恩TTS全流程.md`（端口表、⑦ 耳朵一节）
- `docs/打包与语音服务.md`（外置目录与 runtime python；原先的 `pet/docs/`、`shell/docs/albion/` 两份已合并到这里）
- `pet/docs/PLAN-交互与动画.md`
- `使用说明.md`（服务表与排错）
- `shell/ALBION.md`、`shell/README.md`
- 已归档（历史参考，不必再同步）：`docs/history/pet-ROADMAP.md`、`docs/history/pet-PLAN-四需求.md`、
  `docs/history/pet-STRUCTURE.md`、`docs/history/pet-Hermes-配置文档.md`、`docs/history/shell-Hermes-配置文档.md`

---

## 七、已删除的模型（要用时再拉）

| 曾在此处 | 大小 | 怎么拉回来 |
|---|---|---|
| `D:\AI\Wife\pet\models\faster-whisper-large-v3` | 2.88 GB | `hf download Systran/faster-whisper-large-v3 --local-dir D:\AI\Wife\pet\models\faster-whisper-large-v3` |
| HF 缓存 `models--Systran--faster-whisper-large-v3` | 1.11 GB | 同上（或让 `WhisperModel('large-v3')` 自己下） |
| `pet\models\faster-whisper-large-v3-turbo` | 3.6 MB | 未下完的空壳 |
| HF `models--deepdml--faster-whisper-large-v3-turbo-ct2` | 253.6 MB | `hf download deepdml/faster-whisper-large-v3-turbo-ct2` |
| HF `models--Systran--faster-whisper-base` | 141.0 MB | `hf download Systran/faster-whisper-base` |
| HF `models--Systran--faster-whisper-small` | 463.7 MB | `hf download Systran/faster-whisper-small` |
| `D:\AI\models\faster-whisper-lv3t-ct2` | — | 删除时间早于本次，四个打标脚本引用它 |

**切到 whisper 引擎的最小改法**：`start_ear.bat:4` 改 `whisper` + 加一行 `set ALBION_STT_MODEL=large-v3`（它会自己下）；
壳侧模型名（`albion-defaults.ts:25`）改 `whisper-1` 即可，地址不用动。

---

## 八、判活用（不跑 ASR 造数据）

```
模型自带中文样例（现成、已知正确）：
pet\models\modelscope\iic\speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch\example\asr_example.wav
POST http://127.0.0.1:9882/v1/audio/transcriptions  (multipart, 字段名 file, model=whisper-1)
期望：'欢迎大家来体验达摩院推出的语音识别模型。'（约 0.5s）
```

耳朵日志 `D:\AI\Wife\logs\ear.log`（**GBK 编码**）：
- `跳过无法解码的音频…IndexError: list index out of range` ⇒ funasr 返回空（格式不对 / 没人声）
- 每行 `[ear] <字节数>B -> '识别结果' 用时 x.xs` ⇒ 正常输出
