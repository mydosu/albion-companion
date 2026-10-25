"""弱线索候选阶段（astra-3 §5.2 + 复核收窄 §2）：分组、按来源去重、证据不足就 abstain。

为什么单独一层：`emotion_fusion.delivery_of()` 把中文指令与日语词典混成一个可靠度数字，
于是「LLM 引用了だめ + 词典命中同一个だめ」被算成两票，歧义词（だめ/いや/やめて/そこ/もう）
还能直接推出亲密/气声。这一层**只决定 delivery**：不碰 family，不给歧义词加成；
弱证据冲突或只有歧义句时 abstain（= 本阶段不作新推断，既有状态照常衰减），并把判据原样写进日志。

证据分组：
  D 明确发声指令（原文里有作用域、未被否定）—— 可直接决定 delivery，不决定 family
  S 日语表层舞台提示（`（小声で）`/`囁くように`/`息を荒くして`，v6 `ALBION_JA_STAGE_CUE`）——
    与 D 同级：**句面明确**，可单独决定 delivery（`（小声で）` 这类翻译残留就靠它兜住）
  L LLM 自述的 phonation —— 默认必须**另有可定位依据**才作数，否则记 unsupported_delivery；
    v6 `ALBION_HINT_PHONATION` 起单独也算一等证据，但**档位保守**（调用方压低档，本层只记
    `evidence_tier=hint_self_report` 且 trusted=False —— 不许给自己滚动续期）
  J 日语正文里的**非歧义**发声线索 —— 同一句多个同类命中仍只算这一组一次
  H 可信前句状态（由 D / S / 被接纳的多证据决定）—— 只延续**一句**，且不刷新可信来源
  A 歧义词与形式线索 —— **不算支持票**，只记录（`evidence_tier=prosody`）

证据等级（v6 ①，写进 `log["evidence_tier"]`）：句面明确（D/S/J） > hint 自述（L） > 韵律推断（A）。

用法：`gate(text=日语正文, src_cues=…, hint=…, prev=…)` → dict(phonation, reliability, trusted, log)
"""

import re

import emotion_fusion as F

# 审核过的**非歧义**日语发声线索。歧义词不进这里（只进 A 组留痕）；
# `息を荒くして`（粗气息）也不是耳语，绝不能混进来。
JA_PHONATION = (
    (re.compile(r"囁|ささや|ひそひそ|小声|声を潜|声をひそ|息をひそめ|息を殺"), "whispered"),
)
# 日语**表层舞台提示**（v6 ③ `ALBION_JA_STAGE_CUE`，默认关 ⇒ v4/v5 输出逐字段不变）。
# 与 JA_PHONATION 的区别：这里认的是**舞台提示形**——括号里的指示（`（小声で）`，中文提示翻成
# 日语后残留的那类）与命令/愿望形（`囁くように`、`息を荒くして`）。命中即 `S` 组（句面明确），
# 可单独决定 delivery。注意 `息を荒くして` 是**粗气息 → breathy**，绝不能并进 whispered。
JA_STAGE = (
    (re.compile(r"（[^）]{0,24}(?:囁|ささや|小声|声を潜|声をひそ|ひそひそ|息を(?:ひそ|殺|潜))[^）]{0,24}）"
                r"|[(][^)]{0,24}(?:囁|ささや|小声|声を潜|息を(?:ひそ|殺|潜))[^)]{0,24}[)]"), "whispered"),
    (re.compile(r"（[^）]{0,24}(?:息を荒|息が荒|息を混|あえぎ|喘ぎ|はぁはぁ)[^）]{0,24}）"
                r"|[(][^)]{0,24}(?:息を荒|息が荒|あえぎ|喘ぎ)[^)]{0,24}[)]"), "breathy"),
    (re.compile(r"囁くように|囁いて|囁け|ささやくように|小声で(?:話|言|喋)して"
                r"|声を潜めて|声をひそめて|息を(?:ひそ|殺)めて"), "whispered"),
    (re.compile(r"息を荒くして|息を荒らげて|あえぐように|喘ぐように"), "breathy"),
)

# 证据等级（v6 ①）：句面明确 > hint 自述 > 韵律推断。只在 ①/③ 开关打开时写进日志。
TIER_SURFACE = "explicit_surface"
TIER_HINT = "hint_self_report"
TIER_PROSODY = "prosody"

# A 组：可表达普通拒绝/制止/位置的词，以及省略号、单个「ん」、重复这类形式线索
A_AMBIGUOUS = re.compile(r"だめ|ダメ|いや|イヤ|やめて|ヤメテ|そこ|もう|ん[、，,]")
A_FORM = re.compile(r"[…‥]+|(.)\1{2,}")

# J 组的排除项：否定/制止（复核收窄 §2 明写「排除引用、否定、普通词语中的误匹配」）。
# 宁可疑而漏判，也不要靠"命中了一个词"就升级到耳语 —— 漏判是这套首版刻意允许的。
NOT_PHONATION = re.compile(r"じゃな|ではな|わけではな|なんかじゃ|というわけ|ないし|しなかった|"
                           r"ずに|なくて|くな|なよ|ではない")
# S 组（日语舞台提示）专用否定窗口：在共享判据之上补舞台提示常见的否定形。
# 独立一份是为了让 J 组（v4/v5 的既有判据）**逐字不变**。
STAGE_NEG = re.compile(NOT_PHONATION.pattern + r"|なんかない|なんかな|してない|てない|わけじゃ")
QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("(", ")"))


def _quoted(text, pos):
    """匹配点是否落在引号/括号里（引用、讨论该词 —— 不算发声指令）。"""
    for op, cl in QUOTE_PAIRS:
        if text.count(op, 0, pos) > text.count(cl, 0, pos):
            return True
    return False


def _ja_phonation(text):
    """日语正文里的**非歧义**发声线索；排掉否定、制止与引用。同一句多个同类命中仍只算一组。"""
    for rx, target in JA_PHONATION:
        for m in rx.finditer(text):
            if NOT_PHONATION.search(text[max(0, m.start() - 4):m.end() + 8]):
                continue
            if _quoted(text, m.start()) or _quoted(text, m.end() + 1):
                continue
            return target
    return None

def _ja_stage(text):
    """日语表层**舞台提示**（上述 JA_STAGE 四条）。返回 `(目标发声, 命中的原文片段)`。

    判据与 `_ja_phonation` 同口径复用（否定/制止/引述一律排除）——「息を荒くして」是舞台指示，
    「息を荒くしてないよ」「小声で話してなんかない」不是。否定窗口另立一份 `STAGE_NEG`
    （在共享的 `NOT_PHONATION` 之上补舞台提示常见的否定形）：**J 组的老判据因此逐字不变**。
    """
    for rx, target in JA_STAGE:
        for m in rx.finditer(text):
            if STAGE_NEG.search(text[max(0, m.start() - 4):m.end() + 8]):
                continue
            if _quoted(text, m.start()) or _quoted(text, m.end() + 1):
                continue
            return target, m.group(0)
    return None, None


def _ja_negated(text):
    """句面**否定/引述**掉的发声词（`小声じゃないよ。`/`「小声で」と言った。`）—— 返回 True。

    ① 用它挡住 hint 自述：正文都说了"没在小声"，这时 hint 再说 whispered 也不许生效
    （Sol 第 7/8 轮把「小声じゃないよ。」被念成耳语列为错例）。
    """
    for rx, _ in JA_PHONATION:
        for m in rx.finditer(text or ""):
            if NOT_PHONATION.search(text[max(0, m.start() - 4):m.end() + 8]):
                continue
            if _quoted(text, m.start()) or _quoted(text, m.end() + 1):
                continue
            return False                     # 有一条没被否掉的 ⇒ 句面本来就是正向证据
    return bool(JA_PHONATION[0][0].search(text or ""))

REASONS = ("explicit_directive", "independent_l_j", "continuation_h", "ambiguous_only",
           "correlated_evidence", "conflicting_evidence", "stale_context",
           "unsupported_delivery", "hint_self_report", "ja_stage_directive")


def _pack(log, target, rel, trusted):
    log["candidate_delivery"] = target
    log["accepted"] = target is not None
    return {"phonation": target, "reliability": rel, "trusted": trusted, "log": log,
            "tier": log.get("evidence_tier")}


def gate(text="", src_cues=None, hint=None, prev=None, hint_self_report=False, ja_stage_cue=False):
    """本句的 delivery 候选；`trusted` = 这个决定能不能给下一句当可信来源。

    `hint_self_report`（v6 ①）：hint 自述单独也算一等证据 —— 但它**不作可信来源**（trusted=False），
    档位由调用方压到弱档。`ja_stage_cue`（v6 ③）：日语表层舞台提示进 `S` 组（句面明确）。
    两个开关默认关 ⇒ v4/v5 的既有判据与日志逐字段不变。
    """
    text = text or ""
    ev, dropped = [], 0
    seen = set()
    for c in (src_cues or []):                       # D
        if not isinstance(c, dict) or c.get("negated"):
            continue                                 # 被否定的指令是反例，不是证据
        spec = F.ZH_KIND.get(str(c.get("kind") or "").strip().lower())
        if not spec or not spec[1]:
            continue
        oid = "cue:%s" % (c.get("span") or "+",)
        if oid in seen:
            dropped += 1                             # 同一依据的重复：不是第二票
            continue
        seen.add(oid)
        ev.append({"group": "D", "target": spec[1], "origin": oid, "rel": float(spec[2] or 0.0)})
    if ja_stage_cue:                                                            # S
        s_target, s_hit = _ja_stage(text)
        if s_target:
            ev.append({"group": "S", "target": s_target,
                       "origin": "ja_stage:%s" % s_hit, "rel": 0.8})
    j_hit = _ja_phonation(text)                                              # J
    if j_hit:
        ev.append({"group": "J", "target": j_hit, "origin": "ja:%s" % j_hit, "rel": 0.6})
    ph = hint.get("phonation") if isinstance(hint, dict) else None            # L
    if ph in ("soft", "breathy", "whispered"):
        # 依据不能只有歧义词/标点：没有别的可定位依据就不给目标（记 unsupported_delivery）；
        # v6 ① 起 hint 自述单独成立（`hint_self_report`），可靠度更低、且不作可信来源 ——
        # 但**句面否定/引述**掉的发声词仍然拦得住它（正文说"没在小声"时，hint 不许把它念成耳语）。
        l_ok = bool(ev) or (hint_self_report and not _ja_negated(text))
        ev.append({"group": "L", "target": (ph if l_ok else None), "origin": "hint",
                   "rel": 0.4 if hint_self_report else 0.5})
    if isinstance(prev, dict) and prev.get("delivery") and prev.get("delivery_trusted"):   # H
        ev.append({"group": "H", "target": prev["delivery"], "origin": "prev", "rel": 0.0})
    amb = []
    if A_AMBIGUOUS.search(text):
        amb.append("ambiguous_word")
    if A_FORM.search(text):
        amb.append("form_cue")
    all_groups = {e["group"] for e in ev}
    groups = {e["group"] for e in ev if e["target"]}          # 有目标的组才算支持票
    log = {"accepted": False, "candidate_delivery": None,
           "evidence_groups": sorted(all_groups | ({"A"} if amb else set())),
           "origin_ids": [e["origin"] for e in ev], "reason": None,
           "dropped_correlated": dropped, "ambiguous": amb}
    if hint_self_report or ja_stage_cue:
        # 证据等级：句面明确（D/S/J）> hint 自述（L）> 韵律推断（A）。只在这两个开关打开时落盘，
        # 旧版本（v4/v5）的日志逐字段不变。
        # 只看**有目标的**证据（`groups`）：开关关掉时 L 只是"登记过但没成立"，
        # 不能算 hint 自述等级 —— 否则 ① 关掉后这个等级还会漏进档位滞回。
        log["evidence_tier"] = (TIER_SURFACE if groups & {"D", "S", "J"} else
                                (TIER_HINT if "L" in groups else TIER_PROSODY))
    tset = {e["target"] for e in ev if e["target"]}
    if len(tset) > 1:                                        # 弱证据互相冲突：不看票数
        d = next((e for e in ev if e["group"] in ("D", "S")), None)
        log["reason"] = "conflicting_evidence"
        log["conflict"] = sorted(tset)
        if d is None:
            return _pack(log, None, 0.0, False)
        return _pack(log, d["target"], d["rel"] * 0.5, True)  # 明确指令优先，但冲突留痕
    if not tset:
        if "H" in all_groups and not (groups & {"L", "J"}):
            log["reason"] = "stale_context"        # 前句的弱状态不能给自己续期
        elif any(e["group"] == "L" and not e["target"] for e in ev):
            log["reason"] = "unsupported_delivery"
        elif dropped:
            log["reason"] = "correlated_evidence"
        else:
            log["reason"] = "ambiguous_only"       # 只有歧义词/标点
        return _pack(log, None, 0.0, False)
    target = tset.pop()
    if "D" in groups:
        log["reason"] = "explicit_directive"
        return _pack(log, target, next(e["rel"] for e in ev if e["group"] == "D"), True)
    if "S" in groups:                              # ③ 日语舞台提示：句面明确，可单独成立
        log["reason"] = "ja_stage_directive"
        return _pack(log, target, next(e["rel"] for e in ev if e["group"] == "S"), True)
    if {"L", "J"} <= groups:                       # 相互独立、目标一致
        log["reason"] = "independent_l_j"
        return _pack(log, target, 0.7, True)
    if "H" in groups and ({"L", "J"} & groups):    # 可信前句 + 当前有效弱证据：只延续一句
        log["reason"] = "continuation_h"
        return _pack(log, target, 0.5, False)      # trusted=False ⇒ 不能滚动续期
    if hint_self_report and groups == {"L"}:       # ① hint 自述单独成立：保守（不作可信来源）
        log["reason"] = "hint_self_report"
        return _pack(log, target, 0.4, False)
    log["reason"] = ("correlated_evidence" if dropped else
                     "stale_context" if "H" in groups else "unsupported_delivery")
    return _pack(log, None, 0.0, False)            # 单组弱证据不采纳（宁可漏判）


def _selfcheck():
    def z(kind, span=(0, 2), neg=False):
        return {"kind": kind, "span": list(span), "negated": neg}

    def g(text="", cues=None, hint=None, prev=None):
        return gate(text=text, src_cues=cues, hint=hint, prev=prev)

    # 只有 A：abstain（普通拒绝不能被念成亲密）
    for t in ("だめ", "そこ、だめ", "……ん、そこ、だめ……"):
        r = g(t)
        assert r["phonation"] is None and r["log"]["reason"] == "ambiguous_only", (t, r)
    # 明确 D：直接决定 delivery，且可作可信来源
    r = g("はい。", cues=[z("whisper")])
    assert (r["phonation"], r["log"]["reason"], r["trusted"]) == ("whispered", "explicit_directive", True)
    # 单组弱证据（只有 J）不采纳
    assert g("……小声で言うね。")["log"]["reason"] == "unsupported_delivery"
    # 单组 L（没有可定位依据）不采纳
    assert g("はい。", hint={"phonation": "whispered"})["log"]["reason"] == "unsupported_delivery"
    # L + J 相互独立、目标一致 → 接纳
    r = g("……小声で言うね。", hint={"phonation": "whispered"})
    assert (r["phonation"], r["log"]["reason"]) == ("whispered", "independent_l_j"), r
    # 冲突 → abstain（不看票数）
    assert g("小声で言うね。", hint={"phonation": "breathy"})["log"]["reason"] == "conflicting_evidence"
    # H + J 目标一致 → 只延续一句，且不刷新可信来源（不能滚动续期）
    r = g("小声で言うね。", prev={"delivery": "whispered", "delivery_trusted": True})
    assert (r["phonation"], r["trusted"], r["log"]["reason"]) == ("whispered", False, "continuation_h")
    # H 单独存在（前句弱状态）→ stale_context；H 与当前证据冲突 → abstain
    assert g("はい。", prev={"delivery": "whispered",
                             "delivery_trusted": True})["log"]["reason"] == "stale_context"
    assert g("小声で言うね。", prev={"delivery": "breathy",
                                     "delivery_trusted": True})["log"]["reason"] == "conflicting_evidence"
    # 未标记可信的前句状态不作数
    assert g("小声で言うね。", prev={"delivery": "whispered"})["log"]["reason"] == "unsupported_delivery"
    # 被否定的指令是反例，不是证据
    assert g("はい。", cues=[z("whisper", neg=True)])["log"]["reason"] == "ambiguous_only"
    # J 组排除否定/制止/引用：不能靠"命中了一个词"就把弱句升级成耳语
    for bad in ("小声じゃないよ。", "囁くな。", "「小声で言うね」って言っただけ。",
                "ささやきではない。"):
        r = g(bad, hint={"phonation": "whispered"})
        assert r["phonation"] is None, (bad, r)
        assert r["log"]["reason"] == "unsupported_delivery", (bad, r)
    assert g("小声で言うね。", hint={"phonation": "whispered"})["phonation"] == "whispered"
    # 同一依据的重复不是第二票
    r = g("はい。", cues=[z("whisper", span=(0, 2)), z("whisper", span=(0, 2))],
          hint={"phonation": "breathy"})
    assert r["log"]["dropped_correlated"] == 1 and r["log"]["reason"] == "conflicting_evidence"
    # reason 只能落在固定枚举里
    for t in ("", "だめ", "小声で言うね。", "はい。"):
        for h in (None, {"phonation": "whispered"}, {"phonation": "soft"}):
            assert g(t, hint=h)["log"]["reason"] in REASONS
    assert not (set(REASONS) ^ set(REASONS))
    # ---- v6 ① hint 自述（`hint_self_report=True`；默认关时不改变上面任何一条）----
    r = gate("だめです、やめてください……", hint={"phonation": "whispered"},
             hint_self_report=True)
    assert (r["phonation"], r["log"]["reason"], r["trusted"], r["tier"]) == \
        ("whispered", "hint_self_report", False, "hint_self_report"), r
    assert r["log"]["evidence_tier"] == "hint_self_report"
    # 不作可信来源 ⇒ 不能给自己滚动续期（下一句只有 H 时照样 stale）
    r2 = gate("はい。", prev={"delivery": "whispered", "delivery_trusted": False},
              hint={"phonation": "whispered"}, hint_self_report=True)
    assert r2["log"]["reason"] == "hint_self_report", r2   # H 未标可信 ⇒ 不作证据
    # 句面明确仍在 hint 自述之上：D/J 与 hint 冲突时取明确指令（不看票数），tier 标 explicit_surface
    r3 = gate("小声で言うね。", src_cues=[z("whisper")], hint={"phonation": "breathy"},
              hint_self_report=True)
    assert (r3["phonation"], r3["log"]["reason"], r3["tier"]) == \
        ("whispered", "conflicting_evidence", "explicit_surface"), r3
    assert r3["log"]["conflict"] == ["breathy", "whispered"], r3
    # 只有歧义词/标点 ⇒ 仍韵律等级、abstain（不许因为开了 ① 就凭空给结论）
    r4 = gate("だめ", hint_self_report=True)
    assert (r4["phonation"], r4["log"]["reason"], r4["tier"]) == (None, "ambiguous_only", "prosody")
    # 句面**否定/引述**掉的发声词仍然拦得住 hint 自述（Sol 第 7/8 轮的错例，不许被 ① 复活）
    assert gate("はい。", hint={"phonation": "whispered"},
                hint_self_report=True)["phonation"] == "whispered"
    for bad in ("小声じゃないよ。", "囁くな。", "「小声で」と言われた。"):
        r = gate(bad, hint={"phonation": "whispered"}, hint_self_report=True)
        assert r["phonation"] is None and r["log"]["reason"] == "unsupported_delivery", (bad, r)
    # ① 关掉时 L 不成立 ⇒ 等级不算 hint 自述（否则 ① 关掉后它还会漏进档位滞回）
    r5 = gate("はい。", hint={"phonation": "whispered"}, ja_stage_cue=True)
    assert (r5["phonation"], r5["log"]["evidence_tier"]) == (None, "prosody"), r5
    # ---- v6 ③ 日语舞台提示（`ja_stage_cue=True`）----
    for t, want in (("（小声で）はい。", "whispered"), ("囁くように言った。", "whispered"),
                    ("息を荒くして近づく。", "breathy"), ("（息を荒くして）だめ。", "breathy")):
        r = gate(t, ja_stage_cue=True)
        assert (r["phonation"], r["log"]["reason"], r["tier"]) == \
            (want, "ja_stage_directive", "explicit_surface"), (t, r)
    # 开关关时这些句子一条都不采纳（v4/v5 的输出就是这样，别被 ③ 顺手改了）
    assert gate("（小声で）はい。")["log"]["reason"] == "unsupported_delivery"
    # 否定/引述里的舞台提示不算
    for bad in ("小声で話してなんかない。", "息を荒くしてないよ。", "「囁くように」と言った。"):
        assert gate(bad, ja_stage_cue=True)["phonation"] is None, bad
    # `小声で言うね。` 不是舞台提示形（没有括号/ように），仍走 J 组 ⇒ 单组不采纳
    assert gate("小声で言うね。", ja_stage_cue=True)["log"]["reason"] == "unsupported_delivery"
    print("delivery_stage 自检通过：只有 A 必 abstain / soft≠whispered / 冲突 abstain / 只决定 delivery / "
          "① 证据等级 + ③ 日语舞台提示（开关关时逐字段不变）")


if __name__ == "__main__":
    _selfcheck()
