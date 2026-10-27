# TTS 验证

离线：`python -B -m unittest discover -s tests/tts -p 'test_*.py'`（要 numpy/httpx，
系统 python 缺依赖，用 `D:\AI\Wife\tts\irodori\.venv\Scripts\python.exe`；当前 406 项）。
`test_first_packet_and_logs.py` 覆盖源句并发上限、顺序、异常排空、首单元预取及取消、日志脱敏与轮转。
`test_longline_prepare.py` 覆盖 60s 准备预算、客户端超时/HTTP 断开后释放会话名额、重复等待者与重试去重，
以及默认 Command Code 路由、显式 API 情绪翻译路由、凭据来源优先级和启动器覆盖；不联网、不读取真实密钥。
`test_link_not_spoken.py` 覆盖纯网址/路径/代码单元不送翻译（剥完没汉字、假名就返回空串）、
中文夹网址时送翻译的文本里没有链接，以及 /v2/prepare 对纯链接单元直接回 no_audio；不联网。
`probes/emotion_bench.py --out <tests/output 下目录>` 是**情感评测台**：固定 6 句自编日语（长短、
陈述/疑问）× 固定 seed × 可描述条件（caption 锚 + 正文 emoji 位置），每条量 时长、F0 均值/方差、
能量动态、字速、发声块（位置和数量）、坏音（削波/静音占比/时长异常）；写出 wav + `metrics.json`
+ 自动生成的一页 `README.md`（表格 + 「先听这几条」）。
默认矩阵就是 emoji 位置实验（169 条：风格类 11 个 emoji、非语言类 6 个，各跑句首/句中(逗号后)/句末，
每条件 2 个 seed；另有 5 句基线含 5 个 seed 量噪声底、换句复现、单个 vs 叠两个、两句连写看串句）。
`--dump-matrix x.json` 导出条件表改完再用 `--matrix x.json` 读进来；`--only 子串` 只跑一部分，
`--reuse` 复用已有 wav 只重算指标，`--report-only` 只重出 README。
**前置**：生产后端 :9889 在跑（只调 `/v2/render` 白名单内的锚，不重启、不做第二个实例）；
用 irodori 的 venv 跑（要 httpx + numpy）。判「可测」要同时超过「同句换 seed 的 spread」和最小可辨
阈值（时长 0.15 s / F0 10 Hz / 动态 2 dB / 字速 0.3 字每秒），且各 seed 同号——比这小的差异当噪声。
说话人相似度这一列跳过：本机没有能从任意 wav 提说话人向量的编码器（`speaker_inversion` 是训练出来的
嵌入目标，不是编码器；resemblyzer/speechbrain/pyannote 都没装，不为它装新依赖）。
实测见 `tests/output/tts-emoji-pos-20261026/`（README 是自动表，REPORT.md 是结论与建议规则）。

`test_render_free_caption.py` 锁后端的**自由描述 opt-in**（阶段 2，`caption_mode: "free"`）：
`(family, caption, cfg)` 白名单之外，free 单元放行任意非空、≤120 字、无控制字符的描述和 1.5–7.0 的
任意 cfg（family 仍须合法；codebook_version 若给出仍须与计划版本一致）；**不带这个字段时行为逐条不变**
（码本外 caption 仍 400）；free 单元的 caption/cfg 原样进合成腿，队列日志带 `caption_mode=free`。
固定矩阵 `data/emotion_free_20261026.json` 是这一步要跑的实验：15 条自编自由描述（害羞/忍笑/撒娇/困倦/
快哭/生气/担心/惊讶/温柔安慰/得意/无奈/紧张结巴/低落/兴奋/耳语，各 10–30 字）× 2 句 × 3 seed，外加
7 条码本锚基线（各自相对 `neutral_quiet`）与同一描述换 cfg 2/4/6，共 150 条；
`--matrix emotion_free_20261026` 即可（名字会自动去 `tests/tts/data/` 找）。
**前置**：后端已带 free 支持——旧后端会把自由描述按码本外 400 拒掉，别把 400 当成实验结论。

真哭（`sob`，2026-10-26）措辞扫描：`--matrix emotion_sob_20261026`（固定矩阵
`data/emotion_sob_20261026.json`）——7 条候选真哭描述 × 3 句真的在哭的日语台词 × 2 seed，
每条各跑「无句末 😭 / 有句末 😭」两组，外加 `cry` 现有描述与 `neutral_quiet` 基线各一组，共 96 条；
按时长 / F0 均值 / f0_std / 能量动态 / 坏音挑一条写进 `voice_caption.FIXED_CAPTIONS["sob"]`。
结论见 `tests/output/tts-sob-20261026/REPORT.md`。
`test_voice_caption.py` 锁快哭与真哭的分界：`sob` 只认明示线索（中文**无修饰**的括号动作、
明确的哭声拟声 呜哇/呜呜哇/哇哇大哭、日语 しくしく…），括号里带「快/要/想/差点/忍住」的只算 cry，
「哇——」这类惊叹不算哭，否定排除，标量不产出 sob；`cry` 只认委屈/想哭/快哭/哽咽/眼眶红/含泪/
泪汪汪这类「快要哭」的说法（单独的「哭/眼泪/眼眶」不再算，「哭笑不得」显式排除）；
「呜呜」「要哭了」只算 cry，😭 只跟 sob 走，且哭泣判定优先于 `phonation`。

`probes/benchmark_prepare.py --baseline-file <旧版 bridge_v2.py> --out <tests/output 下 JSON>`
用固定延迟离线比较两版准备阶段，不调用翻译或 GPU，不能代替真实首音频 p50。
`probes/verify_original_tts.py --out <tests/output 下目录>` 需要 :9881/:9885/:9889、
真实 GPU 与 websockets；默认跑 3 轮日语，`--extended` 加纯心声/取消/取消后新轮，
`--chinese` 另发一条固定中文，调用现有真实翻译后端。使用模拟 ACK，不等同真实壳/扬声器播放。
`test_render_session_lifecycle.py` 不联网、不用 GPU，覆盖显式会话、重放拒绝、租期、取消/关闭及两轮各十万次释放。

`test_stream_rhythm.py` 覆盖流式节奏（离线）：切单元策略（首单元软切、后续只在句末、爬坡、退到最近逗号）、
半句译文以「、」收尾、气口整形的 wav 单测（首静音定长、按末尾标点定尾静音、立体声、不适用输入原样返回）、
`……` 符号段并回上一句、`Config` 的环境变量。
`test_gap_shaper_tail.py` 只管句尾不被掐（2026-10-26）：-20→-70dB 的噪声收尾要留住（高于 -60dB 的样本
不许丢）、裁点前 30ms 线性淡出到 0（不留硬台阶）、还在大声时**不**淡出、气口长度与首静音不变。
`probes/tts_tail_probe.py --out <tests/output 下目录> --shape-dir shape-before [--from-raw] [--texts-file x.json]`
直接问生产 :9889 渲染一批日语短句（只读），存**整形前原始 wav** 与 `GS.shape` 之后的 wav，逐句量：末 50ms 还有没有声
（判断模型有没有截）、裁点前 30ms 的电平、被切掉的高于 -60dBFS 的尾音时长；`--report` 从 `shape-before.json` /
`shape-after.json`（同一批 raw 过修前/修后两版 gap_shaper）生成前后对比 README。修后实测见
`tests/output/tts-tail-20261026/`（裁点前 30ms 从 -40dB 降到 ≤-56dB，丢尾音 6/32 句 → 0/32 句）。
`probes/stream_rhythm_probe.py --out <tests/output 下目录> --tag X --text "…" [--ws ws://127.0.0.1:19885/api/v1/audio/speech/ws]`
经真实 :9885（或备用口）发一段文本，逐单元落 wav，量首/尾静音、末 200ms 电平、首音延迟，并按壳的播放规则
（首段 +0.05s、段间 +0.10s、渲染不及就欠载）模拟出 `X-sim.wav` 与可听间隔；会花少量翻译额度，只读。
`probes/f0_tail_probe.py --group 名字=通配符 …` 粗略比较句尾音高走向；自相关 F0 很糙，**分不出逗号收尾和句号收尾**，
只能当同一说话人成组对比的参考，别当结论。
切分/气口的环境变量（网关 :9885 启动时读，`/health` 的 `config.cut`/`config.gap` 可核对）：
`ALBION_CUT_SOFT_UNITS`(1) `ALBION_CUT_LATER_FORCE`(64) `ALBION_CUT_LATER_SOFT_MIN`(12) `ALBION_CUT_LATER_IDLE`(2.0)
`ALBION_CUT_RAMP`(3.5，0=关) `ALBION_CUT_RAMP_FLOOR`(24)；`ALBION_GAP_SHAPE`(1) `ALBION_GAP_LEAD`(0.06)
`ALBION_GAP_SENTENCE`(0.7) `ALBION_GAP_COMMA`(0.3) `ALBION_GAP_ELLIPSIS`(0.8) `ALBION_GAP_CUT`(0.15)；
桥 :9881 读 `ALBION_CONTINUE_TAIL`(1)。

真实探针 `probes/verify_tts_live.py` 需要 httpx、websockets、可用的翻译桥和真实 GPU 后端。
使用固定非私密材料；会发真实翻译/合成请求，结果必须写到主树 `tests/output/tts-leftovers/`。
旧 v4 用 `--legacy`（旧 /tts）；候选用生命周期 v2，不自动兼容旧接口。
示例：`python -B tests/tts/probes/verify_tts_live.py --out D:/AI/Wife/tests/output/tts-leftovers/live`。
每个 WAV 独立校验、保存，WS 按 1 倍音频秒数模拟 ack；这只能证明传输，不能证明壳或扬声器出声、人工音质与语义。
JSON 中的人工项保留 UNVERIFIED/REQUIRES_REVIEW，须另附真实壳、播放时钟、loopback 及人工听审记录。

隔离接线 `probes/run_candidate.py backend|bridge|stream --assets D:/AI/Wife/tts/irodori`：
使用 19889/19881/19885，后端显式完成真实预热才监听。仅在旧 GPU 服务已安全排空停止后运行；
不复制模型，不改 assets；生产启动器提供的翻译模型/安全凭据来源应保持一致。

`build_shell_harness.mjs <已安装 esbuild 包的绝对路径> <tests/output 内的 bundle.js>`
打包现有 `streaming-pipeline`、`streaming-playback` 和 `display-ledger`，仅适配本地测试认证入口。
`verify_shell_renderer.py --bundle <bundle.js> --out <主树 tests/output 内目录>`
通过本机 Electron :9335 调试接口，在已运行主渲染进程中执行固定测试材料；
不读取聊天/profile，不触碰 UI、不更改壳配置。真实 AudioContext 连接 destination，
字幕账本与实际时钟驱动 ACK；只录制本测试音频图的输出，保存 webm。
这不是聊天 UI 入口验收、WASAPI 设备 loopback 或人工听审。

部署工具 `tools/deploy_tts.ps1` 的 Plan/Backup/Deploy/Rollback 只管文件。
实际停启由维护窗口按端口和脚本身份核对后执行，启动命令须使用绝对 bat 路径；
Deploy/Rollback 会等监听端口释放，仍有监听则拒绝，不会自行停止进程。
	est_session_text_chunking.py 验证同一段文本整段/逐字/固定 seed 随机块送入 SessionText 切出的单元序列一致；	est_emotion_degraded_health.py 验证 /v2/health 的 motion_degraded 随翻译 key 可用性变化。


