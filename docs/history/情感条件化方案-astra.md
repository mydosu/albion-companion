> 已失效（2026-10-24 归档）：2026-09-28 的设计稿，v4 已上线取代；现行看 `docs/情感条件化-v4-上线记录.md` 与 `tts/irodori/` 源码。

# 阿尔比恩 TTS 情绪条件化技术方案

2026-09-28；依据本机当前源码、manifest、训练配置静态核查。此次交付仅此文档，不修改生产、不重启、不调用合成、不训练。下文所有新文件、新接口和阈值均为待实施设计；实测统计与建议指标分开列示。

## 1. 问题诊断

### 1.1 结论及证据等级

推荐先做“会话条件贯通 + 情绪与发声方式分离 + 固定音色锚下的有限风格码本”，保留原 v2 路径作为基线；不要先增加正则、全面逐句换 caption，或把 CFG 当情绪强度旋钮。最高权重验收是弱声、气声、断续及三者转换。

已经证实的结构性主因是：上下文没有沿当前 v2 调用传下去；判定结果经过族映射丢失区别；接口只允许每族一个 caption/cfg；训练中最相关档只有个位数样本。它们限制“能够表达什么”。它们分别贡献多少听感损失、换哪一项真的更自然，本轮未听测，不能给因果百分比。

caption 训练/推理不一致是重要待检假设，尤其是语义不一致，但“字符串不一样”本身不足以定为首要声学根因：模型有自然语言 caption 编码器，并不是只接受七个类别 ID。技能里的历史听测能说明换条件曾引发跳变，不能证明今天任意近义描述必然跳变。

### 1.2 实际链路与任务书需要修正的地方

| 证据位置（相对 D:/AI/Wife） | 实际读到的行为 | 对方案的影响 |
|---|---|---|
| `tts/stream/stream_server.py:238` `Coordinator.prepare`、`:248` `plan`、`:400` `render_unit` | prepare 支持 context_before；plan 只发送一个子段，voice=None；render_unit 调 plan 时未传 context_before，也没传 parent_id/complete | 当前生产源码路径比“本句+滚动前文”更弱，实为逐子段无前文规划；必须修调用方 |
| `tts/irodori/bridge_v2.py` `PrepareService.prepare/_translate_unit/_ready` | v2 明确不读写旧桥 `_ctx_state`；请求有原文 speech_text，结果只留 ja_segments；context_before 未用于翻译 | 中文不是从系统消失，是没进入规划契约；20 秒过期只属于旧桥状态，不可照搬成 v2 事实 |
| `tts/stream/session_text.py` `Unit` | 有 source_range、speech_text、reason，没有自然句 parent_id/complete | 规划模块支持自然句投票，不代表协调器已接好自然句标识 |
| `scene_plan.py:93` `_match`、`:63` `FAMILIES`、`:70` `STYLE_FAMILY` | 瀑布优先级固定；shy/moan/tremble→intimate，**flirty→flirty**；soft/calm→neutral，surprised/happy→bright | `INTIMATE` 集合含 flirty，不等于 flirty 最终也用 intimate caption；任务书概括需作此区别 |
| `scene_plan.py` `_ono_hits` | 已检查拟声词右边界 | “あったかい误中あっ”是旧技能记录，当前纯函数检查已不命中，不能当现存 bug |
| `scene_plan.py` `_sticky/_merge_by_sentence/_plan_styles` | 有补洞和 40% 投票；online 的未来信息只限调用传入的 units | 单子段请求下，没有整段历史分母；scene_id 常从 0 重起，不能用作跨请求稳定场景 ID |
| `render_queue.py:428` `_check_unit`、`install` | caption 与 cfg 必须逐字/数值等于 FAMILIES；plan_version 必须为 2 | 单改规划器或提交自由 caption 会被 400 拒绝，必须成套版本化 |
| `render_queue.py:37` `run_infer_synth` → `irodori_serve.py:312` `_run_infer` | 真正传入 caption/cfg，但 wrapper 未暴露 seed、连续条件或轨迹 | 现在不能通过生产 API 做固定种子的严格配对实验 |
| `irodori_serve.py` `ship_cfg/ship_assets/_run_infer` | 优先 speaker embedding，随后 refs；大凤 joint_cfg=4.5，追加统一 cfg 覆盖族 cfg | 计划中的 cfg 不一定是最终 cfg；必须记录最终有效配置；joint 同时影响文本/说话人引导 |

当前硬预算为 prepare 10s、render 20s、unit 30s，播放预取窗口 15s/8 单元。`ActiveBudget` 对播放背压暂停计时；预取完成后等前一单元的空等也有扣除逻辑。不能把“总墙钟超过30秒”一概判失败，也不能把新增分类重试塞到预算外。

### 1.3 训练侧实数

直接逐行统计 `tts/irodori/data_lora/*_manifest_emo_spk.jsonl`：

| 船 | 总行 | 自然 | 低沉 | 静 | 明亮 | 强烈/快语 | 气息较强 | 轻柔耳语 | 有效二维 _z | speaker_id |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| albion | 83 | 40 | 16 | 10 | 5 | 5 | 4 | 3 | 83 | 83/83 |
| brest | 54 | 23 | 12 | 6 | 2 | 1 | 3 | 7 | 54 | 54/54 |
| shinano | 100 | 50 | 22 | 6 | 9 | 2 | 8 | 3 | 100 | 100/100 |
| jinshi | 70 | 36 | 13 | 7 | 5 | 5 | 4 | 0 | 70 | 70/70 |

这四份合计307行、气息较强19行、轻柔耳语13行。金狮生产资产实际指向 clean200，因此其 emo_spk 统计只是可用历史数据，不是生产训练集证明。大凤生产也指向 clean200。

阿尔比恩 `_z` 两轴范围 [-2.40,2.33]/[-2.51,2.40]；布雷斯特 [-2.39,2.90]/[-2.87,1.93]；信浓 [-3.18,3.06]/[-2.63,2.18]；金狮 [-3.41,1.73]/[-2.33,2.17]。检查全部9份 `*_manifest_emo*.jsonl` 共726行：缺二维 `_z` 0行、引用 latent 不存在0行。726包含 emo 与 emo_spk 的重复版本，**不是726条独立录音**。“部分行缺失”未在本次这些文件中复现；其它 cap/clean 清单仍应独立处理缺失值。

`outputs/{albion,brest,shinano}_lora_emo_spk/config.json` 显示 model_dim=1280、caption_dim=512、caption 与 duration predictor 开启、r=16、max_steps=500、caption/speaker dropout=0.1、modules_to_save=auto；两份生产 clean200 配置 max_steps=200。这里只核查配置和 adapter 文件存在，不把 max_steps 当实际训练完成证明，也未校验 checkpoint 内权重来源。

`dataset.py` 的 `LatentTextDataset.__getitem__` 返回 text/caption/latent/ref_latent 等，没有 `_z`；`TTSCollator.__call__` 编码 caption token，没有二维情绪张量。`model.py:1720` `TextToLatentRFDiT.encode_conditions` 接 text/ref/caption，不接 `_z`。所以把 `_z` 写进 JSON 或 CLI 参数不会自动起作用。

技能 `tts-style-captioning/references/caption-from-measurements.md` 支持“说话人内 arousal/valence z-score”这一来源解释；本轮没有找到/执行原始生成脚本并重算均值方差，故轴顺序、归一化版本、数值可靠性仍须回溯原始标注表，不能仅凭字段名强认。也不能将 z-score 当 [0,1] 概率或绝对声学幅度。

### 1.4 “一族一句、整段同档”具体丢失什么

当前 intimate 描述同时要求较强气息、特定音质和近距离，而弱声、耳语、带浊音的气声、颤抖、断续是不同维度。例如低响度可以仍有稳定声带振动；耳语往往缺少稳定基频；气声可以叠在有声语音上；断续主要涉及时间组织。二维情绪也区分不了这些。

| 目标维度 | 当前丢失 | 应如何表达与听测 |
|---|---|---|
| 层次与递进 | shy/tremble/moan 被压成同一条件，不能表达从克制到恢复或加深 | 场景基调稳定，逐自然句保留强度目标与趋势；听是否有可辨而连续的阶段 |
| 强度变化 | cfg=4.5 是引导力度，不是已校准情绪强度 | 强度独立标签；CFG 先固定，另做小范围校准 |
| 气息与浊音比例 | 没有 phonation 维度；“声音小”易被误做成“更强气息” | voiced/soft/breathy/whispered 与 breathiness 独立，听可懂度、气息位置、声带振动连续性 |
| 句间起伏 | 统一条件能减少条件跳变，但每段重新采样，仍没有前段声学状态 | 状态有界继承；同音色锚；段组盲听，不能只评单 WAV |
| 断续与语尾 | 一整段 caption 无法定位哪一个词后换气、何处放松语尾 | 保留原有标点、已有停连信息；后续再做子句/帧级条件，不能声称全局 caption 可精确定位 |

保留同一 caption 不等于输出必然完全平坦：文本、标点、采样和底模仍会产生变化。损失是**有意控制和可复现校准这些变化的能力**。反过来，不断换 caption 也不保证变化自然，可能复发用户在2026-09-13听到的音色跳变。

训练七句与运行五族的 mismatch 有两层：近义措辞偏移可以先低成本 A/B；语义压缩更严重，如柔和耳语被 neutral 吞掉、惊讶被 bright 吞掉、低强度 tremble 被强气息描述覆盖。这是主因候选之一，但气息相关样本太少、音色条件与 LoRA 耦合也可能更大。当前 speaker_id 已存在，不能再用“没有 speaker_id”解释现有 emo_spk 残留问题。

## 2. 候选方案

成本均为排期估计，非本轮性能实测；“增量显存0”指不新增 GPU 模型，原 Irodori 显存仍占用。所有 GPU 路线串行使用这张8GB卡，不另起第二个常驻 Irodori。

| 路线 | 原理及文件/函数改动 | 成本：显存、时间、额度、返工面 | 风险 | 预期收益/排序 |
|---|---|---|---|---|
| A. 原文和上下文贯通 | `Session/Coordinator.plan/render_unit`、`PrepareResult/_ready` 保留源文本、自然句ID、会话状态 | 增量GPU 0；1–2天；无额外LLM；约4文件 | 状态串会话、重复请求二次推进 | **首先做**；修可证实的信息损失 |
| B. 有界规则与置信度 | 新 `emotion_policy.py:infer_rule/update_state`，`scene_plan.plan_units` 只做策略分派 | GPU 0；1天；0额度；小 | 语言暗示仍有歧义 | 默认兜底；把 calm 未知与明确中性分开 |
| C. 翻译同次返回情绪结构 | 新 `bridge.py:to_japanese_with_emotion`，复用请求传输；`PrepareService._translate_unit` 接结构 | GPU 0；2–3天；通常仍每源句1次，增量输出约80–160 token，输入有有界前文；中 | JSON失败、翻译丢内容、输出变长 | **推荐主判定候选**，经限时与质量门禁后开启 |
| D. 独立云LLM判定 | 原文/译文联合分类，异步影子结果；同 `emotion_policy` schema | GPU 0；额外每句1请求，延迟/额度近加一条网络腿；中 | 超10s prepare或30s总预算 | 适合离线金标辅助，不推荐生产串行 |
| E. 本地CPU小判定器 | 离线蒸馏文本特征到轻量分类/回归器，新 `emotion_classifier.py:predict` | GPU 0；3–5天和500–1000条人工复核文本；CPU内存/耗时实测；无在线额度 | 域外失效、假置信度 | 日语直通和额度紧张时的后续候选 |
| F. 训练caption原文对齐 | 新 `emotion_codebook.json`，`compile_condition` 先选七个现有锚 | 增量GPU近0；1天；0额度；小，但须改render校验 | 仍粗粒度，不能期待治愈全部听感 | **第一组声学实验**，直接检验 mismatch |
| G. 因子化有限caption码本 | 固定身份锚，先审核6–9个组合，场景/自然句边界才换；三档强度不做全笛卡尔积 | 增量GPU近0；2–4天+多种子合成；0在线额度；中 | caption换档再次改变音色 | **第二组声学实验**；验证通过才作为表达主路 |
| H. CFG分船校准 | `_run_infer` 新 `resolve_effective_condition`；明确 independent/joint；固定caption下扫小网格 | 0额外模型；半天逻辑+多次渲染；0额度；小 | joint下同时动音色/文本；更高未必更自然 | 辅助手段，不作强度编码 |
| I. 参考音/嵌入与LoRA对照 | `ship_assets/_run_infer` 显式实验profile锁定ref或embed；与旧配方同seed对比 | ref长度增加显存/编码时间，需测；1–2天；0额度；中 | 参考演绎泄漏、切换音色；`inv=null`现在不等于禁用嵌入 | 排除身份/表现力耦合；不要预设旧弃选方案如今更好 |
| J. 子句条件与停连编排 | `SessionText._advance/_commit`、`Coordinator.render_unit`，模型自然静音保留 | 不多模型，但每拆一块增加一次合成；2–4天；0额度；中高 | 预算耗尽、截断呼吸、拼接感 | 仅在既有边界实验，不先把句子切成很多碎块 |
| K. 重标注+小LoRA | 新标注manifest、版本配置；`dataset/train.py:main` 可先不改架构，训练精确caption | 独占GPU；历史技能约5.4GB/600步6.5分钟仅参考；预计单轮10–30分钟+标注1–3天；中 | 小样本过拟合、气声被抹平、训练/推理说话人条件差异 | 数据够再做，先小步长和冻结duration对照 |
| L. 连续控制适配器 | `dataset/TTSCollator`→`train.main`→`model.encode_conditions`；`SamplingRequest/rf`贯通 | 小MLP增量很小，激活/CFG分支仍需实测；1–2周；需要更多真实录音；高 | 数值可插值但声音不单调，架构checkpoint兼容 | **中长期候选**；先全句向量，再帧级 |
| M. 编码后caption插值 | `model.encode_conditions`/runtime增加双锚编码与池化插值 | 0新大模型，2–4天；无在线额度；中高 | 不同长度token不能直接对齐线性混合，未训练的中间点可能失真 | 隔离实验；不直接插 token ID、不承诺自然 |
| N. 离线伪标签/合成增广 | 标注工具用声学模型+人工复核；合成样本严格标记来源 | CPU离线或GPU维护窗口；1–3天；可选离线LLM额度；中 | 自蒸馏放大底模偏差；重复文本造成泄漏 | 辅助文本分类；不能用伪造呼吸替代真实气声录音 |
| O. 其它本机引擎对照 | IndexTTS/GPT-SoVITS/SBV2只做离线对照适配器，不进入默认路由 | 不共驻GPU，重新加载和数据适配成本高；数天；无云额度；大 | 身份和延迟改变，不能公平归因单一情绪模块 | 仅比较能力上限，当前不推荐替换:9889 |

上游明确支持 text/reference/caption 三路条件，并区分 caption CFG 与 speaker CFG；本设计按本地已读代码约束，不跟随上游最新分支自动升级。[Irodori 官方说明](https://github.com/Aratako/Irodori-TTS/blob/main/README.md)、[参数说明](https://github.com/Aratako/Irodori-TTS/blob/main/docs/parameters.md)。

## 3. 推荐方案与实施顺序

### 3.1 目标数据结构与控制语义

保留离散“基调”，另加连续目标与发声方式，采用混合设计。示意schema（无台词）：

```json
{
  "condition_version": 1,
  "baseline": "neutral",
  "affect": {"valence": 0.0, "arousal": 0.0, "intensity": 0.3},
  "phonation": "breathy",
  "delivery": {"breathiness": 0.4, "brokenness": 0.2, "effort": 0.2},
  "ending": "release",
  "confidence": 0.7,
  "evidence": "source_and_translation",
  "parent_id": "session:generation:sentence",
  "complete": true
}
```

valence/arousal用[-1,1]的**意图空间**，其余[0,1]；它们不冒充声学测量。phonation∈{voiced,soft,breathy,whispered}，ending∈{hold,release,fall,rise,unspecified}。每轴可为空并有 mask；unknown 不强制等于0。`brokenness`是断续倾向，不能直接等同固定毫秒停顿。情绪判定只决定表达，不生成新台词。

第一阶段模型真正吃到的仍是编译后的日语 caption、有效cfg、固定说话人条件；JSON里的连续值经过有限码本量化，**不是连续声学控制已经实现**。逐帧控制要等3.6的新模型通路。

### 3.2 P0：冻结基线与可观测性（半天至1天）

实施时新增 `tests/tts/eval/emotion_eval.py`，函数 `audit_manifest/build_cases/render_pairs/score/report`；只在显式命令下写实验目录。记录源文件MD5、文本哈希、分段规则版本、配置哈希、实际adapter/embed哈希、seed、caption原文、有效CFG三元组、guidance模式、步数、参考音和采样率。音色资产必须解析到具体文件，不能仅记录目录别名。

在 `irodori_serve.py` 新增纯函数 `resolve_effective_condition(ship, plan, profile)`，`_run_infer(..., *, seed=None, condition=None)` 使用它；`render_queue.run_infer_synth` 透传并在结果元数据中回显。首先以旧参数调用保持行为；不改默认采样步数40。新增字段未启用时，不改变旧argv。

评测通道只能由本机受控profile启用，不能让任意客户端传权重路径。当前不提供的seed/ref选择通过白名单profile补上。队列仍是一条，评测一次一个低优先级单元；生产排队时暂停评测，不多加载一份模型。

### 3.3 P1：因果上下文与条件协议（1–2天）

1. `session_text.py:Unit` 增加可选 `parent_id/complete`；`SessionText._advance/_commit` 发单元的位置统一维护自然句编号，soft/force/idle保持parent，hard/finish闭合。需保留现有 source_range 完整覆盖与心声剥离语义，最后尾标点按既有解析规则归属。没有未来句时不得假装知道未来场景。
2. `stream_server.py:Session` 增加 `emotion_state`、版本快照和已提交源文/译文有界历史；key包含session_id/generation/voice。`Coordinator._open/_cancel/_terminate/_finish` 管理生命周期。前文最多2个自然句或400字符，先到上限先截断，不沿用旧桥按voice的全局缓存。
3. `bridge_v2.py:PrepareResult.as_dict/_ready` 给每个ja_segment加source句ID和可选emotion_hint；`_pipeline` 不能再只提取(text,sub_index)丢掉元数据。原中文speech_text取现有安全的可说话视图；被剥掉的心声不重新送入合成。
4. `Coordinator.plan/render_unit` 传源文、译文、parent/complete、状态快照、voice、剩余预算。预取可并行翻译，但情绪状态在dispatcher按播放顺序提交；不让晚返回的预取修改过去的计划。render前冻结单元计划；失败不承接成“已经听到的情绪”，保留上次成功状态。去重键包含generation及unit/sub_index。
5. 新 `emotion_policy.py`：`infer_rule` 返回值+置信度+证据；`update_state` 纯函数，有明确强信号立即切换，弱信号可继承一自然句，连续2个无证据完整句退出；每个自然句最多提交一次，空/心声/重复请求不推进。初始平滑系数0.35、每句强度变动上限0.20只是实验参数，强反转或用户明确表达可旁路，不能压平真实突然变化。
6. `scene_plan.py:install/plan_units` 增加请求策略分派，保留原v2实现逐字可回归。新记录带 `condition_id/codebook_version/effective_profile/reason`；`scene_id`由会话状态维持，不借每次从0开始的局部序号充当长期状态。

只保留同一句源文也能先提升可观测性，跨句内容不足时输出unknown/低置信度，不自动把弱声等同sad或intimate。严格整篇模式与因果online结果允许不同；只要求相同已知前缀下结果确定，不要求偷看未来后仍与过去一致。

### 3.4 P2：版本化渲染与有限条件码本（2–4天，先影子）

新增 `tts/irodori/emotion_codebook.json` 和 `emotion_policy.py:compile_condition`。第一组只把旧族caption替换为最接近的训练原文锚，保持其它参数；第二组才区分 soft/breathy/whispered/broken。每种候选必须是风格描述，不写台词，不能包含动态自由长文本。

起步码本只做中性、轻声有声、柔和气声、耳语、断续有声、断续气声等6–9个已审阅组合；未知组合回到相邻可支持锚。先以自然句/场景为粒度，在同一句软切片内冻结caption。相邻句想换档先经滞回，且需要听测证明音色连续；未通过则保持段内同caption，仅保留连续意图用于后续训练。**固定同一身份前缀或embedding只是降低变量，不保证不跳音色。**

`render_queue.py:_check_unit` 改成 `_check_unit(u,i,plan_version,profile_snapshot)`：v2沿用原严格表；v3从服务端白名单codebook按ID重新编译，核对版本、cfg范围、caption和有效模式，拒绝非有限数和未知ID。`install` 同时接收{2,3}，先部署接收能力再部署发送方。不要简单把 PLAN_VERSION 从2改3，让旧协调器全面失效。

`serve_v2.py:legacy_plan/_seg_unit/install` 默认仍构造v2计划，不因全局常量改变而升级旧REST。`Coordinator.render_unit` 的body不能再写死2，要用本次冻结版本；`run_infer_synth` 透传v3条件而不丢字段。

有效参数优先级写死为：已审核实验profile > 船配置 > 码本 > 旧默认。生产普通请求不能伪装成实验profile。大凤默认joint=4.5保持；不宣称调caption cfg能改变其引导值。独立校准时先选固定caption，扫caption cfg={3.0,3.5,4.0,4.5}，其它cfg固定；joint实验另组，统一cfg三路相同。仅在听测支持后采用子网格，不在线随机扫。

### 3.5 P3：一次翻译兼做情绪判定（2–3天）

新建 `bridge.py:to_japanese_with_emotion(text,context,budget)`，复用现有HTTP后端和退路，不改变旧 `to_japanese(str)->str` 返回契约。传输层如需抽出 `_request_translation`，只做本目标所需改动；新解析函数 `parse_translation_emotion` 输出 `{ja,emotion}`，不用旧文本清洗器将整个JSON拼进台词。

模型看源文与有界历史，输出原意译文及固定schema，不能自由写caption。字段缺失/越界只废弃emotion并走规则；ja有效则直接保留，不为补emotion重试。全部JSON不可解析或译文空才使用既有翻译退路，且只用剩余prepare预算；取消后丢弃晚到结果，不允许后台线程污染状态。

日语直通没有翻译LLM可“免费复用”，默认用规则/CPU分类器；另调LLM必须在实验中单列延迟和额度。不能让日语每句无条件多一次请求。

建议emotion输出上限160 token，带历史额外输入上限400字符，先实测tokenizer统计而非用汉字数等同token数。每1000源句额外输出约8–16万token是容量估算，不是费用报价；记录实际usage，无价格证据不填人民币。新增情绪处理P95目标≤300ms，超额时下一单元禁用该能力；翻译本身沿用10s prepare硬上限，解析/分类的失败不应把已译出的句子变成缺句。

### 3.6 P4/P5：补标注、小训练、连续通路

**先补标签，再决定需不需要改模型。** 阿尔比恩83条全部人工听标发声方式、气息量、断续位置、语尾、努力程度、标签置信度；两人独立复核其中至少30条及全部稀有档。已有 `_z` 只作为候选arousal/valence监督，经 `(speaker_id, 原始录音ID)` join校验，禁止按行号猜匹配。

用于当前caption LoRA的补量建议：每个目标发声大类至少20条独立原始片段、至少5个不同文本，低/中/高强度都有覆盖；5条/类作为先导保留听测样本。阿尔比恩当前4条强气息、3条耳语不满足这个试验门槛，且它们并未证实是严格人工标注类别。补量目标而非保证：新增约60–120条、约10–20分钟可用录音；真稀缺时先跨已有船共享发声规律并保留独立speaker锚，不复制同一条录音凑数。文本素材够多不等于监督音频够多。

新 `tools/annotate_emotion_manifest.py:join_measurements/validate_labels/write_manifest`、新版本manifest、独立配置和输出目录，不覆盖原始latent/旧adapter。归一化参数只用训练分组拟合；按原录音/文本来源分组切分，短切片不能跨train/test；最终盲听文本必须是训练未见的已有材料。

P4先训固定七锚或有限因子码本的LoRA。试验配置batch=1、bf16、gradient checkpointing、max latent steps=400，200/400/600步留点；具体峰值先跑20步检查，峰值超7.0GB停止并缩短样本/序列，不让训练与生产推理共驻。duration predictor是否冻结单独做实验（`lora_modules_to_save=none`候选），不同时换参考音/CFG。技能历史性能仅排期参考。训练需要维护窗口，当前方案阶段不安排实际停服。

P5的**低风险连续路线**：先以 `_z` 和新增发声标签在CPU侧选码本，采用分段线性标定+滞回；这个方案无模型新增能力，输出仍离散，但可以先证明标签是否有用。

P5的**真实连续路线**：新增零初始化门控的小投影器 `EmotionProjector`，将归一化情绪2维、发声控制、mask映射为固定4个caption维度512的条件token，追加到caption_state。新caption token复用现有attention，冻结底模，先训projector，必要时只放开受控LoRA。固定 `_z` 的数字字符串塞到caption不在本路线内。

具体改动链：`config.py:ModelConfig/TrainConfig` 增加可选enable_emotion与dim/version；`dataset.py:LatentTextDataset.__getitem__/TTSCollator.__call__` 返回emotion_values/mask；`train.py:main/run_validation/save_checkpoint` 传入并保存投影器和normalizer元数据；`model.py:TextToLatentRFDiT.encode_conditions/forward` 接收并追加token；`inference_runtime.py:SamplingRequest/InferenceRuntime.synthesize` 传入；`rf.py:sample_euler_rf_cfg` 的所有条件分支一致传值。时长预测器调用也需定义同一语义，初期冻结且不让新token进入duration分支，避免未经校准地改变长度。

情绪token初期跟caption共用dropout与CFG：caption无条件分支必须连情绪token一起mask，否则CFG无条件端仍泄漏情绪。若将来单独emotion CFG，需要新增分支及显存测试，不宣称免费。保存checkpoint schema_version、adapter基座指纹、normalizer；老checkpoint未启用时没有新参数行为，缺字段按mask关闭，不把“缺失”解释成中性。

连续路线先收集至少300–600条跨已有说话人的可靠标注片段及强度配对，再判断能否学到单调响应；这只是启动研究的预算建议，不是充分样本证明。每船83条不足以证明多维独立控制。`_z`两个轴即便可靠，也仍要新标注phonation/breathiness/brokenness；补齐这些不能靠自动z-score完成。

句内精确断续/气息位置需要时间或音素对齐的监督与条件轨迹。RF扩散步t不是音频时间，不能用“采样前半程低cfg、后半程高cfg”冒充语音前半句/后半句变化。真正轨迹需在 `DiffusionBlock.forward` 对latent时间步调制或给带位置的条件attention，并在dataset提供区间标注、处理duration预测映射。列为P5之后的独立研究，不与全句向量同批上线。

## 4. 验收标准

### 4.1 可复现行为/测试与命令

以下A/B/C为本轮已执行同口径的只读检查，可在PowerShell从工作区重跑；不联网、不加载GPU，不产生pycache。示例短文本只用于判定器边界检查，不是新增台词素材。

**A. 数据统计与latent引用**

```powershell
Set-Location D:/AI/Wife
@'
import json, pathlib, collections
n=missing=absent=0
for p in pathlib.Path('tts/irodori/data_lora').glob('*_manifest_emo*.jsonl'):
    rows=[json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
    n+=len(rows)
    m=sum(not isinstance(x.get('_z'),list) or len(x['_z'])!=2 for x in rows)
    a=sum(not (p.parent/x['latent_path']).exists() for x in rows)
    missing+=m; absent+=a
    print(p.name,len(rows),'missing_z',m,'missing_latent',a)
    if p.name.endswith('_emo_spk.jsonl'):
        print(dict(collections.Counter(x.get('caption') for x in rows)))
print('TOTAL',n,missing,absent)
assert (n,missing,absent)==(726,0,0)
'@ | python -B -
```

**B. 10项当前规则事实检查**

```powershell
@'
import sys,contextlib,io
sys.path.insert(0,'tts/irodori')
import scene_plan as s
c=[s.STYLE_FAMILY['flirty']=='flirty',s.STYLE_FAMILY['shy']=='intimate',
   s.STYLE_FAMILY['soft']=='neutral',s.STYLE_FAMILY['surprised']==s.STYLE_FAMILY['happy'],
   s.auto_style('あったかい')[0]=='calm',
   s._sticky(['moan','calm','calm','sad'])==['moan','tremble','calm','sad']]
with contextlib.redirect_stdout(io.StringIO()):
    c += [s.plan_units(['そう。'])[0]['family']=='neutral',
          s.plan_units(['そう。'],extra_ctx='恥ずかしい')[0]['family']=='intimate']
    c += [s.tone_vote(s._norm_units([{'text':'','parent_id':1,'complete':False}]),['moan'])['counted']==0]
    c += [len({s.plan_units(['そう。'],style_override=x)[0]['caption'] for x in ('moan','shy','tremble')})==1]
print('PASS',sum(c),'/',len(c)); assert all(c)
'@ | python -B -
```

**C. 现有A/B文本定位，不打印台词**

```powershell
@'
import sys,pathlib,hashlib,contextlib,io,collections
sys.path.insert(0,'tts/irodori'); import scene_plan as s
for p in pathlib.Path('tts/persona').glob('*.txt'):
    with contextlib.redirect_stdout(io.StringIO()):
        d=s.plan_document(p.read_text(encoding='utf-8-sig'))
    ids=[i for i,t in enumerate(d['segs']) if any(w in t for w in ('はぁ','ふぅ','囁','息','途切','震','んっ'))]
    print(p.name,hashlib.md5(p.read_bytes()).hexdigest(),len(d['segs']),
          dict(collections.Counter(x[0] for x in d['plan'])),ids[:16])
'@ | python -B -
```

实测分段：阿尔比恩30段全部intimate；布雷斯特58段中56段intimate；金狮78段中70段intimate；大凤62段中52段intimate。这是旧整篇planner输出，不是已听出的真实情绪金标，也不是现有online逐子段输出。

素材多为单行长文，不能按“第几行”抽样。固定源MD5，再用当前 `_segments` 的0基段号选取并人工审核：阿尔比恩[4,5,6]、[14,15,16]、[18,19,20]为首轮候选；布雷斯特[39,40,41]、大凤[56,57,58]、金狮[53,54,55]做跨船检查。关键词只负责找候选，必须再确认是弱声/气声/断续所需材料；不足时从同一现有文本补选，不新写台词。

**D. 后续实施必须新增的测试与评测入口**

下面文件和CLI目前不存在，是P0/P1要交付的实现合同；不能把这些命令记为本轮已通过。模块设计使用标准库unittest，CPU mock代替真实GPU/LLM：

```powershell
# 完成对应实现后才执行
python -B -m unittest discover -s tests/tts -p test_emotion_*.py -v
python -B tests/tts/eval/emotion_eval.py audit --root D:/AI/Wife
python -B tests/tts/eval/emotion_eval.py cases --source tts/persona --out _tmp/emotion-eval/cases.jsonl
# GPU合成需先完成v3评测profile并进入安排好的实验窗口；不另起模型
tts/irodori/.venv/Scripts/python.exe -B tests/tts/eval/emotion_eval.py render --cases _tmp/emotion-eval/cases.jsonl --profiles baseline,caption-aligned,no-lora --seeds 1,2,3,4,5,6,7,8 --endpoint http://127.0.0.1:9889 --serial --out _tmp/emotion-eval/run01
python -B tests/tts/eval/emotion_eval.py report --run _tmp/emotion-eval/run01 --bootstrap-unit case --bootstrap 2000
```

必须覆盖的行为：旧v2输入输出等价；v3未知码本/NaN/非法CFG拒绝；大凤joint覆盖可见；同句不同软切不重复投票；未完整句不计票；显式情绪不被粘滞覆写；两句无证据退出；新generation/换voice不继承；取消/重复/乱序不二次推进状态；非法emotion不吞有效译文；中文原文与ja对应ID不丢；预算不足禁用可选情绪腿；旧/新请求交错仍单GPU；回滚时不混版本；source_range和字幕资格守恒。

不要直接依赖旧 `tests/tts/test_ctx_scene.py` 充当v2验收：它含 `stream_server.take_pieces` 旧接口假设，当前协调器并非该形态。本轮未运行该旧测试。

### 4.2 数字阈值（建议上线门槛，不是达成结果）

| 项目 | 门槛与统计口径 |
|---|---|
| 接口/状态/回滚 | 上述必测场景100%通过；相同seed/同版本/同输入条件解析一致100%；源文本丢失/重复0；旧v2合同回归失败0 |
| 分类质量 | 先人工标注≥120个已有片段/片段组，弱声/气声/断续占≥60%；保留≥20个易混负例；phonation宏F1≥0.80，三种重点类型各召回≥0.80；报混淆矩阵，不拿族命中率代替 |
| 置信度 | 用独立标注集校准；自动强切换精确率≥0.90，否则降级码本/规则；LLM自报confidence本身不算可靠概率 |
| 延迟 | 在固定机器、同片长分布的≥200单元回放中，新增情绪处理P95≤300ms，首音P95相对基线增量≤500ms；prepare≤10s、render≤20s、活动工作预算≤30s；记录冷/热和排队，不用播放背压空等造失败 |
| 失败与额度 | 控制回放200单元中新增emotion导致缺句=0；同时报基线自身失败数。只做情绪补救的额外LLM重试=0；输出上限160 token，按实际usage汇总 |
| 显存 | 同时加载的新GPU判定模型=0；推理/训练各自测峰值目标≤7.0GB，OOM=0；不同时训练与服务推理，不以权重文件大小代替峰值 |
| 音频交付 | 全部WAV可解码；非有限采样=0；abs(sample)≥0.999比例=0；不能因弱声低RMS就删除片段；缺字/重复字由人工核验，ASR仅辅助 |
| 条件强度 | 低/中/高三档在≥80%的未见文本上被盲听按目标轴正确排序，且音色连续；不满足则保持离散锚，不发布“连续可控”结论 |
| A/B听感 | 至少12组弱声/气声/断续上下文、8组普通/反转对照，8个配对seed；至少3名盲评听者或用户3轮间隔盲测。重点组新方案偏好率≥65%（平局按0.5）；按片段组聚类bootstrap 95%区间下界>50%；普通组五分自然度均值退化不超过0.2 |
| 稳定性 | 重点组相邻句身份跳变≤5%且不高于基线；评委间分歧单独报，用户否定的版本不因客观分数获胜 |

20组×8seed×3臂=480条“片段组”产物；每组三子句则最多1440次单段渲染。先4组×8seed×3臂=96组做淘汰，胜者再扩到完整集。不能把seed或同一组的多个切片当独立听者样本。预算以单次warm渲染实测耗时×次数估计；本轮未测新变体合成耗时。

### 4.3 人工听感项与A/B归因

第一轮三臂：A当前生产、B仅训练caption原文对齐、C去LoRA但其它相同。A/B检验mismatch，A/C检验LoRA影响；C只是诊断臂，不声称是自然度上限或必然更干净。第二轮固定胜者权重和音色条件，只换有限因子码本；第三轮才比较分类器；第四轮才扫CFG。替换两个变量的综合方案仅作端到端比较，不归因为某个单旋钮。

弱声/气声/断续占最终听感总权重60%，普通表达20%，切换/退出和身份稳定20%。听完整前后句，逐项评分：

- 弱声是否是放轻发声，而不是单纯后期音量变小；词仍可辨，有没有不自然挤压。
- 气息是否自然混入浊音，换气位置是否合理，是否把全部句子变成噪声或同一种强气息。
- 断续是否有意图和节奏，恢复时是否连贯；是否像丢包、机械插静音或碎片拼接。
- 递进、回落和语尾是否与上下文一致；温和句之后是否突然拔高或换了一个人。
- 是否出现非目标颤抖、金属味、毛刺、额外发声、漏字/重复字；没有声音的停顿能否保留自然呼吸尾巴。

WAV原始版本用来查弱声强度；另提供**整组统一增益**的响度匹配盲听版本排除“更响更好”的偏差，不逐句归一化抹掉动态，不裁掉模型头尾气息。盲听隐藏组名、随机A/B次序，并保存随机化种子和评分表。

客观项记录时长、停顿区间、可可靠跟踪帧的有声率、RMS包络和差分谱；耳语F0无效帧标NA，不补0算平均。HF能量/平坦度可以量气息或噪声变化，**不能单独证明电音减少/更自然**；HNR、jitter、谱质心不设通用“越好”门槛。speaker embedding相似度只作分布偏移警报，不用统一阈值否定耳语。WAV哈希用于验证重复性，哈希不同只证明输出变了，不证明参数按预期生效。

### 4.4 尚未验证

本轮没有听音、合成、训练或HTTP链路压测；未核查运行进程是否已加载与磁盘相同的版本；未读权重张量或核验实际训练步数；未实测显存、LLM结构输出成功率及额度；未重算 `_z` 原始声学标签、做latent内容/波形对齐或验证时间标注；未验证其它引擎可用性；没有新方案MOS、偏好率、延迟收益结果。上述数字门槛和开发工时均为待验证设计。

本轮已完成：纯函数事实断言10/10；9份emo清单726行的字段/latent路径检查；4份emo_spk清单307行的caption/speaker统计；4份persona分段与旧整篇族分布检查。文件存在不等于latent内容正确，统计label不等于人工确认真实发声类型。

## 5. 风险与回滚

### 5.1 切换设计

实施时新增 `emotion_runtime.json`，由新 `emotion_policy.load_snapshot` 原子读取并校验，配置示意：`mode=legacy|shadow|canary|on`、`policy_version`、`codebook_version`、`allowed_voices`、`profile_id`。默认legacy；读坏沿用上一份合法快照，首次读坏走legacy。它是待实现功能，**当前不存在开关**。每会话/每自然句冻结快照，队列内记录不在半途重编译。

部署顺序：离线CPU合同测试→服务端兼容v2/v3与元数据→桥兼容新增可选字段→协调器仍发v2→shadow只比较计划不多渲染→阿尔比恩显式canary会话→盲听通过后扩大。不随机让用户同一轮对话混两种版本。代码加载需要维护窗口逐服务重启；本轮没有执行。8GB不允许旧新两个模型常驻做蓝绿切换。

shadow也需要预算，LLM影子不可无上限多调用；默认只跑CPU计划对照。恢复旧行为必须调用保留的legacy函数，不是在新函数里尽量模拟旧行为。

### 5.2 阶段独立回退

| 阶段 | 切入条件 | 回退操作 | 回退到哪里 |
|---|---|---|---|
| P0观测 | argv等价、v2合同不变 | 禁用额外观测；保留结构兼容 | 原_run_infer参数与原采样逻辑 |
| P1状态 | 会话隔离/守恒/因果测试通过 | 新会话mode=legacy，清新情绪状态，不动旧文本账本 | 原逐子段v2规划 |
| P2码本/接口 | v2/v3混跑、A/B通过 | 新请求发v2；在途v3按冻结快照完成，或显式cancel旧generation后重开，禁止重复播送 | 原FAMILIES caption/cfg |
| P3联合判定 | 译文质量与延迟不退化 | emotion提取关闭，调用旧to_japanese；保留合法译文 | 原翻译主路和既有退路 |
| P4新LoRA | 权重/hash与听感门槛通过 | profile改回旧adapter具体路径，不覆盖旧目录 | albion_lora_emo_spk等原船资产 |
| P5连续token | 全部CFG/mask/无条件分支验证通过 | 禁用新分支，换回原架构checkpoint/profile，维护窗口重载一次 | 原基座+原adapter/embed；不是把z设0假装回退 |

`ships.json` 当前支持热读，但它并不支持完整实验profile，也没有显式“强制ref取代embed”字段；实现时新增明确 `speaker_condition_mode` 和白名单asset ID，不能用 `inv:null` 当关闭embedding。回退须保存完整旧配置快照与资产哈希，不能依赖早期`.bak`恰好与本轮基线一致。

触发回退：出现新增缺句、OOM、版本400、跨会话串状态立即关闭canary；听感跳变超阈值或用户否定则退回同段caption锁定；稀有类别数据不足则暂停训练路线、保留基座与码本方案。取消/回退不改变字幕源范围，不把已播放音频重发。

### 5.3 本轮只读范围与资料口径

分析时对87个既有目标文件记录MD5，覆盖Irodori顶层Python/模型模块、stream源码、所有顶层manifest/config yaml、ships.json、四份persona、两份参考文档和任务书；末尾用内存保存的初始哈希表逐项重算，结果为checked=87、changed=0。运行日志、权重二进制、全部仓库未纳入这87文件范围，不能由此宣称整个工作区无任何外部变化。文档A/B/C三段命令逐段执行退出码均为0。只新增本文件，无commit、无push、无服务操作。

参考两份项目文档为历史背景，当前行为以实际源码为准。使用的本机技能为 `irodori-tts-local`、`tts-emotion-tagging`、`tts-style-captioning`、`tts-voice-eval`、`tts-ab-evaluation`（均位于 `%USERPROFILE%/AppData/Local/hermes/skills/media/`）；其中“必须生成音频/manifest”的执行步骤不适用于用户明确限定的本轮方案任务。它们的旧实验数值仅作假设和排期参考，不写成此次实测。
