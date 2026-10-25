# -*- coding: utf-8 -*-
"""情绪证据融合（fusion_v1）—— 把"先命中的规则说了算"换成"五路证据加权比较"。

设计依据：`docs/history/情感条件化方案-astra-2.md` 第一问（1.1~1.5）。要点与取舍：

1. **内部状态 vs 送给模型的条件**：这里可以算连续量、可以留歧义，但送给合成器的
   始终只能是码本里**训过的完整日语短句**（`emotion_codebook`）。
2. **五路证据**：LLM 提示 / 日语词义 / 中文源提示 / 韵律 / 前文。
   每路给出**每族支持值** `u[k][f] ∈ [-1, 1]`（0 = 无信息，负 = 反证），
   不先压成一个赢家；最后加权求和再 softmax。
3. **同源去重**：LLM、译文、标点本就同源，用 `origin` 标记同一出处只算一次，避免
   一个感叹号在三路被算成三份独立支持。缺失的路**不重新均分权重**（否则一句带个
   emoji 的短句会显得"证据满满"）。
4. **不切换的条件**：最高分 < 0.60、与第二名差 < 0.20、或证据质量 Q < 0.35 时**不切**
   （继承上一档）。这些门槛在校准前**没有概率含义**，只是待标定的初值。
5. **歧义保留**：flirty / intimate 在唤醒度/效价/发声上本就相同，靠发音方式分不出来 ——
   没有互动语用证据时两者得分就该接近，正确动作是**保留歧义**，不是发明识别能力。
6. **覆盖缺口**：目标（例如"高唤醒 + 负效价"的生气）在现有 7 句锚里没有对应表达时，
   记 `coverage_gap` 并退回最接近的合法锚，**不假装识别到了**。

纯 CPU、纯标准库、无状态（所有历史由调用方通过参数带进来），可单独测试。
"""
import re

import emotion_codebook as CB

FAMILIES = ("neutral", "bright", "sad", "flirty", "intimate")

# 冷启动权重：**待校准的设计初值**，不代表已证明哪路最准（方案 1.3）
W0 = {"llm": 0.40, "ja": 0.15, "zh": 0.25, "prosody": 0.10, "history": 0.10}
# 各路的可靠度 r（同一句话里多路共用 origin 时只算一次）
R = {"llm": 0.7, "ja": 0.7, "zh": 0.5, "prosody": 0.4, "history": 0.4}
# 明确直述发声指令（「（小声）」这种）可靠度更高
R_DIRECT = 1.0
# 切换门槛（未校准）
P_MIN, MARGIN_MIN, Q_MIN = 0.60, 0.20, 0.35
_MIN_SUPPORT = 0.20      # 单路最强支持低于此值 → 整路丢弃（薄证据不算一路，见 fuse）
# 温度校准值取自方案给的候选集 {0.25, 0.5, 1, 2}；本机支持值尺度下 0.25 才能让
# "两路同向的明确证据"越过 0.60，而"单路薄证据"仍停在门槛以下。**未用标注集校准**。
TEMP = 0.25


# ---------------- 各路证据 → 每族支持值 ----------------
def _clamp(x, lo=-1.0, hi=1.0):
    return lo if x < lo else (hi if x > hi else x)


def _num(v, lo, hi):
    """只认有限数值；bool / NaN / Inf / 越界一律当缺失。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    if v != v or v in (float("inf"), float("-inf")) or not (lo <= v <= hi):
        return None
    return v


def llm_support(hint):
    """LLM 提示 → 支持值。缺字段就是缺失，**不强填 0**。

    A=唤醒度 I=语气强度 V=效价 P=发声方式。交互项（高唤醒×正效价 → bright 等）
    正是"关键词表读不出来"的那部分信息。
    """
    if not isinstance(hint, dict):
        return None, 0.0
    a = _num(hint.get("arousal"), 0, 1)
    i = _num(hint.get("intensity"), 0, 1)
    v = _num(hint.get("valence"), -1, 1)
    if a is None and i is None and v is None and not hint.get("phonation"):
        return None, 0.0
    hot = max([x for x in (a, i) if x is not None] or [0.0])
    cool = 1.0 - hot
    pos = v if v is not None else None
    u = {f: 0.0 for f in FAMILIES}
    if v is not None:
        u["bright"] += hot * max(v, 0.0)
        u["sad"] += cool * max(-v, 0.0)
        u["neutral"] += (1.0 - abs(v)) * cool * 0.5
        if hot >= 0.65 and v < -0.25:
            u["neutral"] -= 0.2                       # 生气：不是平静
            u["sad"] += 0.2 * cool                    # 但也不是"低唤醒的悲伤"能表达的 → 见 coverage_gap
    else:
        # 只有唤醒度、没有效价：**分不出**是兴奋、生气还是崩溃（要成对才读得出交互项），
        # 所以不给族级支持 —— 一个含糊的提示不该压过原文里明确的指令（方案 1.3）。
        # 只有唤醒度很高时才给一点 bright 先验，并且由 coverage_gap 如实记下缺了效价。
        u["neutral"] += 0.1
        if hot >= 0.75:
            u["bright"] += 0.25 * (hot - 0.75) / 0.25
    ph = hint.get("phonation")
    # 发声方式**只**支持 intimate（近距离交流意图），不单独产生 flirty 证据（方案 1.3）
    if ph in ("whispered", "breathy"):
        u["intimate"] += 0.6 * cool + 0.2
    elif ph == "soft":
        u["intimate"] += 0.35 * cool
        u["neutral"] += 0.1
    q = _num(hint.get("intensity"), 0, 1)
    return u, (R["llm"] if hint.get("reliability") is None else _num(hint.get("reliability"), 0, 1) or R["llm"])


# 日语词义：弱声/气息/断续这一类**关键词表原本没有**的信息（NSFW 场景的主力）
LEX = [
    # (正则, 族加成, 发声倾向, 权重)
    (re.compile(r"囁|ささや|小声|ひそひそ|声を潜|息を(殺|潜)"), "intimate", "whispered", 0.7),
    (re.compile(r"息を荒|息が荒|息を混|あえ|喘|はぁ|はあ|んっ|ぁ…"), "intimate", "breathy", 0.7),
    (re.compile(r"途切|息継ぎ|とぎれ|息が続|乱れ"), None, "broken", 0.5),
    (re.compile(r"だめ|いや|やめて|そこ|ん…|ふふ|もう…"), "flirty", None, 0.45),
    (re.compile(r"嬉し|うれし|よかった|楽しみ|わあ|わー|やった"), "bright", None, 0.6),
    (re.compile(r"悲し|つら|寂し|ごめん|すみません|申し訳"), "sad", None, 0.6),
    (re.compile(r"驚|びっくり|えっ|まさか"), "bright", None, 0.5),
    (re.compile(r"恥ずかし|はずかし|照れ"), "intimate", None, 0.55),
    (re.compile(r"ありがと|うん|はい|そうですね|ですね"), "neutral", None, 0.25),
]
_NEG = re.compile(r"(じゃない|ではない|ない|ません|やめて|ず)[。、！？…]*$")
_QUOTE = re.compile(r"[「『\"“]([^」』\"”]{0,60})[」』\"”]")


def ja_support(text):
    """日语文本 → (支持值, 发声倾向, 可靠度)。带否定与引用的**范围**处理。

    「泣かないで」不是说话者正在哭；「小声じゃない」不支持小声；引号里的话是转述。
    """
    if not text:
        return None, None, 0.0
    t = str(text)
    quoted = set()
    for m in _QUOTE.finditer(t):
        quoted.add(m.start(1))
    u = {f: 0.0 for f in FAMILIES}
    ph = None
    hits, r_min = 0, 1.0
    for rx, fam, phon, w in LEX:
        for m in rx.finditer(t):
            inside_quote = any(s <= m.start() < s + 60 for s in quoted)
            rel = w * (0.3 if inside_quote else 1.0)      # 转述证据：弱化，但不丢弃
            tail = t[m.end():m.end() + 6]
            # 否定要往后看够（`小声じゃない` 的「ない」在提示词之后 3 个字）
            if _NEG.search(t[max(0, m.start() - 8):m.end() + 8]):
                rel = -abs(rel) * 0.5                     # 反证：宁可少算，不翻转整轴
            if fam:
                u[fam] += rel * 0.8
                if fam == "intimate":
                    u["neutral"] -= rel * 0.3
            if phon and rel > 0:
                ph = ph or phon
            hits += 1
            r_min = min(r_min, R["ja"] * (0.4 if inside_quote else 1.0))
    if not hits:
        return None, None, 0.0
    return ({f: _clamp(v) for f, v in u.items()}, ph, r_min)


def weights_for(origins):
    """权重随**证据基座**变化，不是随"缺失"随便分摊。

    日语直通（她本来就说的日语）时，LLM 与中文源两路**结构上不存在**，只剩日语词义这一路
    在扛全部信息；此时若仍用 W0，Q 的上限只有 0.15*0.7=0.105，永远过不了 0.35 ——
    表现就是"整段判不出来、全掉 neutral"。所以把权重让给实际存在的证据路。
    注意：偶发缺失（模型没给 hint）**不**走这条，那种情况就是证据薄，不该硬撑。
    """
    if not ({"llm", "zh"} & set(origins)):
        return {"ja": 0.55, "prosody": 0.25, "history": 0.20}
    return dict(W0)


# 中文源提示：桥里从**原文**抽出来的结构化线索（emoji / 括号短指令 / 标点），
# 日语译文里看不到这些。kind 取值见 bridge.zh_source_cues()
ZH_KIND = {
    # soft（「小声」「压低声音」）只管**音量意图**，不产生族支持 —— 方案 1.4：
    # 发声指令只负责选锚，不负责判族（安静说明本身可以是 neutral）。whisper/breath 另说：
    # 在本产品里它们只出现在近距离场景，属于有意保留的族证据（用户优先级最高的那一类）。
    "soft": (None, "soft", 0.0),
    "whisper": ("intimate", "whispered", 0.85),
    "breath": ("intimate", "breathy", 0.75),
    "excite": ("bright", None, 0.6),
    "sad": ("sad", None, 0.6),
    "shy": ("intimate", None, 0.5),
    "flat": ("neutral", None, 0.3),
}


def zh_support(cues):
    """中文源提示 → (支持值, 发声倾向, 可靠度)。`cues` 是 [{kind, negated?}, …]。"""
    if not cues:
        return None, None, 0.0
    u = {f: 0.0 for f in FAMILIES}
    ph, rel = None, 0.0
    seen = False
    for c in cues if isinstance(cues, list) else []:
        kind = (c or {}).get("kind") if isinstance(c, dict) else None
        spec = ZH_KIND.get(kind)
        if not spec:
            continue
        seen = True
        fam, phon, w0 = spec
        if isinstance(c, dict) and c.get("negated"):
            if fam:
                u[fam] -= w0 * 0.5                        # 「（别小声）」：反证
            continue
        if fam:
            u[fam] += w0
        if phon:
            ph = ph or phon
        rel = max(rel, R_DIRECT if kind in ("whisper", "breath", "soft") else R["zh"])
    if not seen:
        return None, None, 0.0
    if rel <= 0:                                          # 只有反证：也算一路（薄，但要说得出）
        rel = R["zh"]
    return ({f: _clamp(v) for f, v in u.items()}, ph, rel)


_PAUSE = re.compile(r"[…‥ー〜～]{2,}|[、，]{2,}")
_REPEAT = re.compile(r"(.)\1{1,}")


def prosody_support(text):
    """韵律先验：标点连串、重复字符、句长。只提供弱证据，且只碰唤醒/停连轴。

    `!!!` 主要支持高唤醒，**不支持正效价**；长句不等于低情绪；省略号不等于悲伤。
    """
    if not text:
        return None, None, 0.0
    t = str(text)
    bang = t.count("!") + t.count("！")
    ques = t.count("?") + t.count("？")
    pauses = len(_PAUSE.findall(t))
    reps = len(_REPEAT.findall(t))
    n = len(re.sub(r"\s", "", t))
    u = {f: 0.0 for f in FAMILIES}
    ph = None
    hot = min(1.0, 0.35 * bang + 0.15 * ques + 0.1 * reps)
    if bang or reps:
        u["bright"] += hot * 0.5                          # 高唤醒（效价未知）
    if pauses:
        u["intimate"] += min(0.4, 0.12 * pauses)          # 断续/停顿 → 弱声倾向
        u["neutral"] -= min(0.2, 0.06 * pauses)
        ph = "broken"
    if n and n <= 8:
        u["neutral"] -= 0.1                               # 短句略偏有情绪，仅弱证据
    if not (bang or ques or pauses or reps or (n and n <= 8)):
        u["neutral"] += 0.15                              # 平铺陈述：平静的弱正证据
    if all(abs(v) < 1e-9 for v in u.values()):
        return None, None, 0.0
    return ({f: _clamp(v) for f, v in u.items()}, ph, R["prosody"])


def history_support(prev_p):
    """前文先验：H[f] = 2*p[f] - 1。它**不计作新信号**（Q 里不算），只推动分数。"""
    if not prev_p:
        return None
    return {f: _clamp(2.0 * float(prev_p.get(f, 0.0)) - 1.0) for f in FAMILIES}


# ---------------- 融合 ----------------
def fuse(evidence, history, weights=None, temp=TEMP):
    """evidence: [(origin, u, rel), …]（同一 origin 只算第一份）。返回 (p, Q, conflict)。

    `Q`（证据质量）只看**当句新证据**的可靠度加权和，明确排除前文 —— 否则
    "一直在继承"会被误当成"一直在确认"。
    """
    w = dict(W0 if weights is None else weights)
    S = {f: 0.0 for f in FAMILIES}
    # 证据太薄的一路**不算一路**（`_MIN_SUPPORT`）：它不该占证据质量，也不该进族分布 ——
    # 否则"只有 0.1 的中性支持"会凭 0.40 的权重把明确的耳语指令摊平（实测 p 从 0.84 掉到 0.52，
    # 够不到 0.60 的切换门槛，于是"原文明说耳语"却判不出来）。
    evidence = [(o, u, r) for o, u, r in evidence
                if u and max(abs(v) for v in u.values()) >= _MIN_SUPPORT]
    Q, seen = 0.0, set()
    conflict = False
    for origin, u, rel in evidence:
        if not u or origin in seen:                       # 同源去重（一个感叹号别算三份）
            continue
        seen.add(origin)
        Q += rel * w.get(origin, 0.0)
        for f in FAMILIES:
            S[f] += w.get(origin, 0.0) * rel * u.get(f, 0.0)
    if not seen:
        return None, 0.0, conflict
    # 方向取**加权均值**（S/Q）：分数落在 [-1,1]，softmax 的温度才有稳定含义。
    # 缺证据不稀释方向（方案 1.3），"只有一条弱证据却显得证据满满"由 Q 去管。
    for f in FAMILIES:
        S[f] /= max(Q, 1e-9)
    h = history_support(history)
    if h:
        for f in FAMILIES:
            S[f] += w["history"] * h[f]
    # 冲突检测有两层，都在 `conflict` 里暴露（方案 1.3：分歧要看得见，不要静默平均掉）：
    # ① 同一轴上正负对撞（将来某路给出反证时生效）
    ups = [u for _, u, _ in evidence if u]
    for f in FAMILIES:
        vals = [u.get(f, 0.0) for u in ups]
        if max(vals, default=0) > 0.15 and min(vals, default=0) < -0.15:
            conflict = True
    # ② 各路的"最强族"明显不是同一个（LLM 说开心、原文词却在哭）
    tops = []
    for origin, u, rel in evidence:
        if not u or rel < 0.4:
            continue
        f, s = max(u.items(), key=lambda kv: kv[1])
        if s >= 0.3:
            tops.append(f)
    if len(tops) >= 2 and len(set(tops)) >= 2:
        conflict = True
    m = max(S.values())
    ex = {f: pow(2.718281828459045, (S[f] - m) / temp) for f in FAMILIES}
    tot = sum(ex.values()) or 1.0
    return {f: ex[f] / tot for f in FAMILIES}, min(1.0, Q), conflict


def effective_origins(evidence):
    """`fuse` 真正采用的那几路（薄证据已按 `_MIN_SUPPORT` 丢弃）。

    权重与门槛都必须按**同一份**口径算：否则一路 0.1 的薄证据会把权重分母撑成四路齐备，
    门槛随之抬高，明确的那一路反被卡死（实测：Q=0.25 撞上 0.2625 的门槛）。
    """
    out = []
    for o, u, r in evidence:
        if u and max(abs(v) for v in u.values()) >= _MIN_SUPPORT and o not in out:
            out.append(o)
    return out


def quality_bar(origins, q_min=Q_MIN, floor=0.40):
    """证据质量的**门槛随可用证据基座缩放**（与 `weights_for` 同一道理）。

    4 路齐全时门槛就是 `Q_MIN`；日语直通/没有 LLM 提示时，能说话的通道本来就少，
    若还按 4 路口径卡 0.35，"一路明确指令"也会被判成"证据不足"而永不切换。
    `floor` 保住下限：只剩一路**弱**证据（一个 emoji）时仍然过不了线 —— 方案 1.3 的要求。
    """
    w = weights_for(origins)
    avail = sum(w[o] for o in set(origins) & set(w))
    return q_min * max(avail, floor)


def decide(p, Q, prev_family=None, prev_tier=None, p_min=P_MIN, margin_min=MARGIN_MIN, q_min=None):
    """选择性切换：门槛不满足就**不切**（继承），并给出可诊断的理由。"""
    if not p:
        return {"switch": False, "family": prev_family, "reason": "no_evidence",
                "confidence": 0.0, "margin": 0.0, "Q": 0.0, "gap": None}
    top_f, top_p = max(p.items(), key=lambda kv: kv[1])
    second = max((v for f, v in p.items() if f != top_f), default=0.0)
    margin = top_p - second
    ok = (top_p >= p_min and margin >= margin_min and Q >= (Q_MIN if q_min is None else q_min))
    return {"switch": bool(ok), "family": top_f if ok else prev_family,
            "reason": ("confident" if ok else
                       ("low_conf" if top_p < p_min else
                        ("ambiguous" if margin < margin_min else "thin_evidence"))),
            "confidence": round(top_p, 3), "margin": round(margin, 3), "Q": round(Q, 3),
            "gap": None}


def coverage_gap(hint):
    """现有 7 句锚表达不了的目标：如实记缺口，不硬塞一个近似的族。

    典型：高唤醒 + 负效价（生气）、带 negative 效价的耳语等。
    """
    if not isinstance(hint, dict):
        return None
    a = _num(hint.get("arousal"), 0, 1)
    i = _num(hint.get("intensity"), 0, 1)
    v = _num(hint.get("valence"), -1, 1)
    hot = max([x for x in (a, i) if x is not None] or [0.0])
    if v is not None and hot >= 0.65 and v <= -0.25:
        return "high_arousal_negative_valence"          # 生气/激动地生气：码本里没有
    if v is not None and hot < 0.65 and v <= -0.6 and hint.get("phonation") in ("whispered", "breathy"):
        return "negative_valence_breathy"               # 呜咽/忍着哭的气声
    return None


def support_cost(family, tier, caption, target_phonation):
    """候选锚与目标发声方式是否相容（方案 1.3：不相容就加代价/禁用）。

    `息を荒くして、甘く喘ぐように、近い距離で` 是**粗气息**；目标是耳语/弱声时不该用它 ——
    这一条正是"弱声/气声整组被喂成强气息"的根因。
    """
    if not target_phonation or not caption:
        return 0.0
    coarse = "息を荒くして" in caption
    if target_phonation in ("whispered", "soft") and coarse:
        return 1.0                                       # 直接禁用
    if target_phonation == "breathy" and not coarse and "囁" in caption:
        return 0.4
    return 0.0


def delivery_of(text, src_cues=None):
    """本句**明说的发声方式**（「（小声）」「囁く」「息を殺して」这类直述指令）。

    与"族"分开门控是方案 1.4 的要求：发声指令可靠度最高（明确直述 → 1.0），
    但它**只**用来约束"选哪条锚"，不负责判定情绪族 —— 安静说明可以是 neutral。
    引号里的转述（「她说'小声点'」）不算本人指令，按低可靠度返回。
    """
    _, ph_ja, r_ja = ja_support(text)
    _, ph_zh, _ = zh_support(src_cues)
    if ph_zh:
        return ph_zh, R_DIRECT
    if ph_ja and r_ja >= 0.5:
        return ph_ja, round(r_ja, 3)
    return None, 0.0


def phonation_conflict(llm_phonation, direct_phonation, direct_rel=1.0):
    """LLM 自述的发声方式与**原文直述**打架（方案 1.3/1.4：要暴露冲突，不要静默掩盖）。

    典型：「（小声）……」（原文明说耳语）而 LLM 回报 `voiced`。原文直述可靠度最高，
    所以冲突**不改变**采用谁（用原文），只是让人看得见这条分歧。
    """
    if not direct_phonation or direct_rel < 0.5:
        return False
    a, b = llm_phonation, direct_phonation
    if a not in ("voiced", "soft", "breathy", "whispered"):
        return False
    return (a in ("voiced", "soft") and b in ("whispered", "breathy")) or \
           (b in ("voiced", "soft") and a in ("whispered", "breathy"))


def anchor_is_phonation_specific(caption):
    """这条锚是不是**发声专属**锚（目前只有耳语/气声两条是）。"""
    return caption in (CB.A_WHISPER, CB.A_BREATHY)


def compile_target(family, level, prev_tier=None, phonation=None, allow_neutral_whisper=False,
                   force_weak=False):
    """族 + 强度 + 目标发声 → 合法三元组；**联络不上就换档或如实记缺口**。

    这一步才是"弱声/气声别被喂成粗气息"的闸门：`息を荒くして…` 是粗气息，
    目标是耳语/弱声时直接禁用，退回同族另一条锚；另一条也不合适就记 `coverage_gap`。

    `allow_neutral_whisper=True`（v4 / codebook v2）：neutral 族明说耳语时，借**既有**耳语锚
    （保持 neutral 的语义，不绕道 intimate）。cfg 用该族的值，并**如实记候选标记** ——
    白名单通过不等于听感通过（astra-3 §5.1）。

    `force_weak=True`（v6 ①）：**档位保守** —— 证据只有 hint 自述时不许用强档锚（默认 False，
    旧版本行为因此不变）。
    """
    weak, strong = CB.ladder(family)
    tier = "weak" if force_weak else CB.tier_for(family, level, prev_tier)
    cap = strong if tier == "strong" else weak
    gap = None
    # ⓪ v4 专用：neutral 说耳语，而本族锚里没有发声专属锚 → 借既有耳语锚（§5.1 受控候选）
    # 只认 **neutral**：耳语锚在码本里只对 neutral（v2 扩展）与已经有发声专属锚的族合法，
    # 别的族强借会产出白名单外的组合（render 队列按版本校验时会拒）。
    if (allow_neutral_whisper and phonation == "whispered" and family == "neutral"
            and not anchor_is_phonation_specific(cap)):
        return {"family": family, "caption": CB.A_WHISPER, "cfg": CB.cfg_of(family),
                "tier": tier, "gap": CB.NEUTRAL_WHISPER_GAP}
    # ① 选中的锚与目标发声**打架** → 换成同族另一条
    cost = support_cost(family, tier, cap, phonation)
    if cost > 0.9:
        alt = weak if tier == "strong" else strong
        if alt != cap and support_cost(family, tier, alt, phonation) <= 0.9:
            tier, cap = ("weak" if tier == "strong" else "strong"), alt
        else:
            gap = "phonation_unmatchable"
    elif cost > 0.0:
        # 0.4 这一档是"能用但不完全匹配"（breathy 目标拿到耳语锚这类）：
        # 不做更换，但**如实记缺口** —— 不能留一个看着生效、其实没人读的代价（Sol r2 A 节）
        gap = "phonation_approximate_%s" % phonation
    # ② 目标是耳语/气声，可这一族的锚里**根本没有**发声专属锚（例如 neutral）
    #    → 只能给最接近的普通锚，如实记缺口（这正是要新增 `囁き声で、静かに` 的理由）
    if not gap and phonation in ("whispered", "breathy") and not anchor_is_phonation_specific(cap):
        gap = "no_anchor_for_%s" % phonation
    return {"family": family, "caption": cap, "cfg": CB.cfg_of(family),
            "tier": tier, "gap": gap}


if __name__ == "__main__":                 # 自检：融合器的边界行为
    # ① 五路都缺 → 不给结论（不能"没有证据"却自信）
    p, Q, _ = fuse([], None)
    assert p is None and Q == 0.0
    d = decide(p, Q, prev_family="intimate")
    assert d["switch"] is False and d["reason"] == "no_evidence"

    # ② 耳语的原文明示 + 低唤醒 → intimate 胜出且可切
    u_zh, ph_zh, r_zh = zh_support([{"kind": "whisper"}])
    u_llm, r_llm = llm_support({"arousal": 0.2, "valence": 0.4, "phonation": "whispered"})
    p, Q, _ = fuse([("zh", u_zh, r_zh), ("llm", u_llm, r_llm)], None)
    d = decide(p, Q)
    assert d["switch"] and d["family"] == "intimate", (p, d)
    assert ph_zh == "whispered"

    # ③ 耳语目标 + 粗气息锚 = 不相容（必须被禁掉）
    assert support_cost("intimate", "strong", "息を荒くして、甘ぐ喘ぐように、近い距離で", "whispered") > 0.9
    assert support_cost("intimate", "weak", "柔らかく、優しく囁くように", "whispered") == 0.0

    # ④ 只有前文、没有新证据 → 不切（Q 不算前文）
    p, Q, _ = fuse([], {"intimate": 0.9})
    assert Q == 0.0 and decide(p, Q, prev_family="neutral")["switch"] is False

    # ⑤ 同源去重：一个感叹号不能在三路各算一次
    ev = [("prosody", {"bright": 0.5}, 0.4)] * 3
    p1, Q1, _ = fuse(ev, None)
    p2, Q2, _ = fuse([("prosody", {"bright": 0.5}, 0.4)], None)
    assert abs(Q1 - Q2) < 1e-12, (Q1, Q2)

    # ⑥ 否定：小声じゃない 不支持小声
    u_yes, _, _ = ja_support("小声で話す")
    u_no, _, _ = ja_support("小声じゃない")
    assert u_yes and u_yes["intimate"] > 0 > u_no["intimate"]

    # ⑦ 引号里的转述：证据被弱化但不丢弃
    u_plain, _, r_plain = ja_support("囁いてください")
    u_quote, _, r_quote = ja_support("彼は「囁いて」と言った")
    assert r_quote < r_plain and u_quote["intimate"] < u_plain["intimate"]

    # ⑧ 覆盖缺口：生气在码本里没有
    assert coverage_gap({"arousal": 0.9, "valence": -0.6}) == "high_arousal_negative_valence"
    assert coverage_gap({"arousal": 0.3, "valence": 0.5}) is None

    print("emotion_fusion 自检通过：缺证据不给结论 / 五路融合 / 发声相容 / 去重 / 否定 / 转述 / 缺口")
