# -*- coding: utf-8 -*-
"""v3 情绪条件码本：**只用 LoRA 真正见过的日语风格描述**。

依据（2026-09-28 实测统计）：`data_lora/*_manifest_emo_spk.jsonl` 里每行的 `caption` 只取自
7 句固定描述（albion 83 行 / brest 54 / shinano 100 / jinshi 70）。而运行时原本喂的 5 句族
caption 里，`恥ずかしそうに…` / `甘く誘うように…` / `悲しそうに…` / `嬉しそうに…` /
`柔らかく、優しく、落ち着いた声で` **都不在训练集里** —— 属于分布外条件，模型只能"猜"。

本码本的做法：
  * 每个基调族 = **同族内的两条训练锚**（弱档 → 强档）；
  * cfg 按族**固定**（cfg 是引导强度、不是情绪强度 —— 不做强度旋钮，也不在线扫参）；
  * 强弱只决定用哪条锚，换档由 `scene_plan.plan_units_v3` 在自然句边界做滞回。

这等于把"一族一句、整段不动"换成"同族两档、随语气走"，且**不引入任何没训过的描述**。
"""
import re

# ---- 训练集里真实出现过的 7 句（原文照抄；A_LOW 自 10-31 起是唯一例外）----
A_NEUTRAL = "自然な声で、丁寧に"
A_QUIET = "落ち着いた、静かな声で"
A_LOW = "落ち着いた声で、沈んだ調子に"   # 10-31 起：与「低い声で」同族的 caption 会漂男声（旧锚在 1 句×50 seed 上实测 0/50，但去掉最稳；新 0/50）。已偏离训练原文，仅此一条
A_BRIGHT = "明るく、弾んだ声で"
A_STRONG = "感情的で、声を張り上げて、早口に"
A_BREATHY = "息を荒くして、甘く喘ぐように、近い距離で"
A_WHISPER = "柔らかく、優しく囁くように"

TRAINED = frozenset([A_NEUTRAL, A_QUIET, A_LOW, A_BRIGHT, A_STRONG, A_BREATHY, A_WHISPER])

CODEBOOK_VERSION = 1

# 基调族 → (弱档, 强档)。单锚的族两档相同（等于不换，保持旧行为）。
LADDER = {
    "neutral": (A_QUIET, A_NEUTRAL),
    "bright": (A_BRIGHT, A_STRONG),
    "sad": (A_LOW, A_LOW),
    "flirty": (A_WHISPER, A_BREATHY),
    "intimate": (A_WHISPER, A_BREATHY),
}

# 族 → cfg（与旧 FAMILIES 逐位相同：本次只动 caption，不动引导强度，变量单一）
CFG = {"neutral": 3.0, "bright": 3.2, "sad": 3.5, "flirty": 4.0, "intimate": 4.5}

CODEBOOK_VERSION_V2 = 2

# ---- v2 码本（astra-3 §5.1）：neutral 族的耳语能力 ----------------------------------
# 复用**既有**耳语锚（柔らかく、優しく囁くように），保持 neutral 语义 —— 不绕道 intimate。
# cfg 先沿用该族的规定值，作为**受控候选**：白名单通过 ≠ 效果通过（听感仍待验证）。
EXTRA_V2 = {("neutral", A_WHISPER): CFG["neutral"]}
NEUTRAL_WHISPER_GAP = "neutral_whisper_candidate_untested"

STRONG_AT = 0.5          # 冷启动时的单阈值（**只用在没有历史档位时**）
T_PROMOTE, T_DEMOTE = 0.58, 0.42   # 双阈值：升级要 ≥0.58，降级要 <0.42，中间带保持原档
# 允许随语气走的族（单锚族换档无意义，列在这里只为显式）
LADDERED = frozenset(k for k, v in LADDER.items() if v[0] != v[1])


def ladder(family: str):
    return LADDER.get(family) or LADDER["neutral"]


def cfg_of(family: str) -> float:
    return CFG.get(family, CFG["neutral"])


def tier_for(family, level, prev_tier=None):
    """弱/强档位判定 —— **双阈值**（施密特）：升档比降档难，避免在单阈值附近来回抖。

    实测过：单阈值 0.5 + 每句步长 0.34 时，`lv` 序列 0.45→0.57→0.55→0.38→0.45→0.73→0.56→0.51
    会让档位跟着噪声翻飞（0.51 就升档、0.45 又降回来），听感是"忽轻忽重"，不是滞回。
    """
    if family not in LADDERED:
        return "weak"                       # 单锚族不换档
    if prev_tier == "weak":
        return "strong" if level >= T_PROMOTE else "weak"
    if prev_tier == "strong":
        return "strong" if level >= T_DEMOTE else "weak"
    return "strong" if level >= 0.5 else "weak"


def compile_condition(family: str, level=None, prev_tier=None) -> dict:
    """族 + 强度 → 合法三元组 (family, caption, cfg) + tier。强度只决定用弱锚还是强锚。"""
    weak, strong = ladder(family)
    tier = tier_for(family, 0.0 if level is None else level, prev_tier)
    return {"family": family, "caption": (strong if tier == "strong" else weak),
            "cfg": cfg_of(family), "tier": tier}


def allowed_pairs(codebook_version=CODEBOOK_VERSION):
    """`(family, caption) -> cfg` 白名单 —— 给 render_queue 的校验用。

    三者必须**一起**查：族、训过的 caption、**该族的** cfg。分开查会漏掉跨族组合
    （`family=intimate` + 合法 whisper caption + `cfg=3.0`）—— 那是个没训过的条件，
    等于白名单形同虚设。

    `codebook_version=1`（默认）**逐键等于旧行为**；`=2` 追加 v2 的新组合（neutral+耳语）。
    按版本取白名单，旧 v2/v3 因此**仍然拒绝** neutral-whisper —— 新组合上线不会顺带放宽它们。
    """
    pairs = {(fam, cap): CFG[fam] for fam, (weak, strong) in LADDER.items()
             for cap in (weak, strong)}
    if codebook_version >= CODEBOOK_VERSION_V2:
        pairs.update(EXTRA_V2)
    return pairs


# ---- 强度档估计（纯 CPU、确定性）------------------------------------------------
_PAUSE = re.compile(r"[…‥]|——|、\s*、")
_ONO_HINT = re.compile(r"(んっ|あっ|はぁ|はあ|ふぅ|ふっ|うぅ|うっ|くぅ|んぅ|ひゃ|あぁ|んぁ|ふぁ|はふ|ちゅ)")
_BANG = re.compile(r"[!！?？]{1,}")


def family_from_hint(h, codebook_version=CODEBOOK_VERSION):
    """把上游 LLM 的情绪结构折成基调族（缺字段就 None）。

    这是"识别"真正落地的地方 —— 只有把它接到族上，`phonation=whispered`、`arousal=0.9`
    这类**关键词表读不出来的信息**才会改变发声。返回 None = 这条提示没有可用信息。
    """
    if not isinstance(h, dict):
        return None
    def num(k):
        v = h.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    ph = h.get("phonation")
    inten, ar, val = num("intensity"), num("arousal"), num("valence")
    hot = max([x for x in (inten, ar) if x is not None] or [0.0])
    if ph in ("whispered", "breathy") and (codebook_version >= CODEBOOK_VERSION_V2 or hot < 0.7):
        # v2 起：发声方式优先于通用"激动"规则 —— 耳语/气声不会因为唤醒高就变成"明亮弹跳"。
        # 旧式 `and hot < 0.7` 恰好把**耳语+高唤醒**（亲密场景的典型特征）漏给了 bright 分支。
        # v1 保留旧判据，保证旧规划的确定性结果逐字节不变（Sol 第 6 轮第一反例）。
        # 强度/档位仍由既有 intimate 梯子承担（weak=耳语锚，strong=喘息锚），本改动不碰它。
        return "intimate"                    # 弱声/气声：走 whisper↔breathy 那两条训练锚
    if hot >= 0.65:
        return "bright"                      # 提高嗓门/激动
    if val is not None and val <= -0.25:
        return "sad"
    if inten is None and ar is None and val is None and ph is None:
        return None
    return "neutral"


def hint_level_bonus(h):
    """发声方式对强度档的微调：气声往上够得着"息を荒くして"锚，耳语压在弱档。"""
    ph = (h or {}).get("phonation") if isinstance(h, dict) else None
    return {"breathy": 0.15, "whispered": -0.05, "soft": -0.05}.get(ph, 0.0)


def cue_level(text: str, hint: float = None) -> float:
    """从文本自身估一个 0..1 的强度档（确定性）。

    hint 是上游 LLM 给的强度（0..1，可缺）。文本线索只是兜底先验，不是"识别结果"——
    它的作用是在没有 LLM 判定（日语直通、翻译腿退路）时也**不退回整段同一档**。
    """
    t = text or ""
    lv = 0.45
    n_ono = len(_ONO_HINT.findall(t))
    if n_ono >= 2:
        lv += 0.18
    elif n_ono == 1:
        lv += 0.08
    lv += 0.06 * min(3, len(_BANG.findall(t)))
    if len(_PAUSE.findall(t)) >= 2:
        lv += 0.10
    if len(t) > 40:                       # 长句本身更平，别因为长就判成强
        lv -= 0.12
    if hint is not None:
        lv = 0.4 * lv + 0.6 * float(hint)
    return max(0.0, min(1.0, lv))


def step_level(prev_level, raw_level, max_step=0.34):
    """带步长限制的强度推进：允许真的变化，但不允许一跳到底（滞回的来源）。"""
    if prev_level is None:
        return raw_level
    d = raw_level - prev_level
    d = max(-max_step, min(max_step, d))
    return max(0.0, min(1.0, prev_level + d))


if __name__ == "__main__":                # 自检：码本自洽 + 强度函数的边界
    pairs = allowed_pairs()
    assert all(c in TRAINED for _, c in pairs), "码本里出现了训练集外的 caption"
    assert len(TRAINED) == 7
    assert all(want == CFG[fam] for (fam, _), want in pairs.items()), "cfg 必须绑定到族"
    assert len({(fam, c) for fam, c in pairs}) == len(pairs), "白名单有重复组合"
    assert set(CFG) == set(LADDER), "族集合必须一致"
    # v2 码本（astra-3 §5.1）：新组合只在新版本里出现，旧版本必须仍拒绝
    assert ("neutral", A_WHISPER) not in allowed_pairs(), "v1 不该有 neutral+耳语"
    assert allowed_pairs(CODEBOOK_VERSION_V2)[("neutral", A_WHISPER)] == CFG["neutral"]
    assert set(allowed_pairs()).issubset(allowed_pairs(CODEBOOK_VERSION_V2))
    assert len(allowed_pairs(CODEBOOK_VERSION_V2)) == len(allowed_pairs()) + 1
    assert compile_condition("intimate", 0.1)["caption"] == A_WHISPER
    # 耳语/气声优先于通用"激动"规则：高唤醒不该把耳语判成 bright（NSFW 场景的典型特征）
    assert family_from_hint({"phonation": "whispered", "arousal": 0.9},
                            codebook_version=CODEBOOK_VERSION_V2) == "intimate"
    assert family_from_hint({"phonation": "breathy", "intensity": 0.8},
                            codebook_version=CODEBOOK_VERSION_V2) == "intimate"
    assert family_from_hint({"arousal": 0.9}) == "bright"          # 没有发声证据时行为不变
    assert family_from_hint({"phonation": "whispered", "arousal": 0.9}) == "bright"   # v1 逐字节不变
    assert compile_condition("intimate", 0.9)["caption"] == A_BREATHY
    assert compile_condition("intimate", 0.9)["cfg"] == 4.5
    assert compile_condition("sad", 0.9)["tier"] == "weak"        # 单锚族不换档
    assert abs(step_level(None, 0.8) - 0.8) < 1e-9
    assert abs(step_level(0.0, 1.0, 0.34) - 0.34) < 1e-9
    assert abs(step_level(0.9, 0.0, 0.34) - 0.56) < 1e-9
    assert cue_level("はぁ……んっ……") > cue_level("はい、わかりました。")
    print("emotion_codebook 自检通过：档位切换 / cfg 固定 / 步长限制 / 文本先验")
    print("码本（族 → 弱档 | 强档, cfg）：")
    for fam in ("neutral", "bright", "sad", "flirty", "intimate"):
        c = compile_condition(fam, 0.0)
        s = compile_condition(fam, 1.0)
        print("  %-9s cfg=%.1f  %s  |  %s" % (fam, c["cfg"], c["caption"], s["caption"]))
