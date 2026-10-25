# -*- coding: utf-8 -*-
"""拒绝/制止/安抚类句子的**语义闸门**（Astra 第 11 轮 ②；新计划版本 v5 / 开关 `ALBION_REFUSAL_GUARD`）。

**先有实测**（v4，纯 CPU 复现，见 `tests/test_emotion_refusal.py`）：上游 LLM 给「高 arousal +
voiced」时，`emotion_codebook.family_from_hint` 的 `hot >= 0.65` 分支把**任何**激动的句子判成
bright —— 拒绝句「不行，别这样。」（日语「だめ、そんなことしないで。」）于是拿到
`bright / 感情的で、声を張り上げて、早口に / cfg=3.2`。「明るく、弾んだ声で」说出的拒绝，
和「声を張り上げて」说出的安抚，都是错的。

本模块**只下禁令，不替句子指定族**，顺序就是规格要求的顺序：

  ① 认清**语义类别**（拒绝 / 制止 / 安抚降温 / 恐惧制止 / 低落拒绝）；
  ② 标注每类**明确禁止的发声条件**（见 `FORBID` / `FORCE_WEAK`）；
  ③ 只在"高唤醒自动等于 bright"这条路真的要生效时，把族换成**同一份证据**支持的另一个合法族
     （负效价 → `sad` 低声；其余 → `neutral` 平静），并把被禁的强档降回弱档。

因此**没有新增字族，也没有新增训练锚**：换出去的族仍是码本里训过的 7 句锚之一。

三条边界（都是"不干预"，而不是"一律压成 neutral"）：
  * **玩笑式拒绝**由上下文判定：效价为正、或文本自带笑/玩笑标记 ⇒ 不干预（玩笑式拒绝本来
    就该是明亮弹跳的）；
  * 族**本来就是证据选出来的**（例如 LLM 说 whispered ⇒ intimate、负效价低唤醒 ⇒ sad）⇒
    没有禁令对象，不干预——「だめ」落在亲密上下文里不会被压平；
  * 引述（「やめて」と言っただけ）与否定（だめじゃない／やめるつもりはない）里的词**不算**，
    判据与 `delivery_stage` 同口径（复用同一份 `_quoted`）。
"""
import re

import delivery_stage as DS      # 引号判据复用同一条（「」『』“”/半角括号）

GAP = "refusal_bright_veto"      # 记录里如实标的缺口：族是被禁令换掉的，不是"识别到了生气"

# ---- ① 语义类别（顺序即优先级：恐惧 > 低落拒绝 > 安抚 > 制止 > 拒绝）----------------
FEAR = re.compile(r"(?:怖|こわ|恐)[^。]{0,20}(?:来ないで|近づかないで|やめて|いや|だめ|助けて)"
                  r"|(?:来ないで|近づかないで|やめて|いや|だめ)[^。]{0,20}(?:怖|こわ|恐)")
SAD_REFUSE = re.compile(r"したくない|したくありません|話したくない|言いたくない|聞きたくない"
                        r"|これ以上[^。]{0,12}(?:ない|無理)|もう(?:いい|たくさん)")
SOOTHE = re.compile(r"落ち着いて|落ち着け|落ち着こう|冷静に|冷静にな|深呼吸"
                    r"|焦らないで|慌てないで|大丈夫(?:だから|ですから)")
STOP = re.compile(r"やめろ|やめなさい|やめとけ|やめて|黙れ|黙って|止まれ|止まって|来るな|するな|近づくな"
                  r"|(?:や|止|来|行|見|み|触|開|消|近づ|話しかけ|起き|し)ないで")
REFUSE = re.compile(r"だめ|ダメ|駄目|やだ|嫌だ|いらない|無理|できない|やめ(?:て|とく|る)"
                    r"|いや(?:だ|です|よ|っ|[、。…！!])")

# 类别 → (这说明的是什么话, 明确禁止的发声条件) —— 映射之前先把"不许"写清楚
CLASSES = {
    "fear":       ("恐惧制止：因为害怕而叫对方停手/别靠近", "禁 bright"),
    "sad_refuse": ("低落拒绝：不想再说/再做下去", "禁 bright"),
    "soothe":     ("安抚降温：要对方冷静下来", "禁 bright + 禁强档（提高嗓门/早口）"),
    "stop":       ("制止：命令/请求对方停手", "禁 bright"),
    "refuse":     ("拒绝：拒绝对方的提议或要求（可以平静，也可以不甘心）", "禁 bright + 禁强档"),
}
# 所有类都禁 bright（= 明るく、弾んだ声で；强档是 感情的で、声を張り上げて、早口に）
FORBID = ("bright",)
# 这五类**连强档一起禁**：上游的高 arousal 只说明"激动"，不能当"要更大声"的证据
# ——「高唤醒自动等于 bright」的同一根因，另一个出口就是这个档位。所以受禁令保护的句子一律
# 回到弱档锚（`落ち着いた、静かな声で` / `低い声で、沈んだ調子に`）。真要"大声制止"的场景
# 不在本轮材料里，届时该由喊叫类**自己的**证据另开一条，而不是靠上游 arousal 顶上来。
FORCE_WEAK = tuple(CLASSES)

# 紧跟标记词之后的否定：だめじゃない / やめるつもりはない / 落ち着いてなんかない
_NEG = re.compile(r"じゃな|ではな|わけではな|なんかな|つもりはな|気はな|ことはな")
# 玩笑标记：有它（且没有负效价证据）⇒ 是开玩笑，不是真拒绝
_PLAYFUL = re.compile(r"ふふ|あはは|えへへ|(?:冗談|じょうだん)(?:だよ|だね|だ|よ)|からかっ|いたずら")
# 效价高于此值 ⇒ 上下文是正向的，拒绝是玩笑（成对材料见 tests 里的 R6/R7）
PLAYFUL_VALENCE = 0.3
SAD_AT = -0.5                    # 效价低于此值才换 sad；-0.5~0 换 neutral（平静拒绝不是"悲伤"）
_NEG_VALENCE_FALLBACK = -0.6     # 没给效价时，恐惧/低落两类用自己的词当负效价证据


def classify(text):
    """返回 `(类别, 命中的词)`；不是拒绝/制止/安抚类就返回 `(None, None)`。

    引述与否定里的标记词不算（「やめて」と言っただけ 不是制止；だめじゃない 不是拒绝）。
    """
    t = text or ""

    def alive(rx):
        for m in rx.finditer(t):
            if DS._quoted(t, m.start()):
                continue                                  # 引述/讨论该词
            if _NEG.search(t[m.end():m.end() + 6]):
                continue                                  # 紧跟其后的否定
            return m.group(0)
        return None

    for name, rx in (("fear", FEAR), ("sad_refuse", SAD_REFUSE), ("soothe", SOOTHE),
                     ("stop", STOP), ("refuse", REFUSE)):
        hit = alive(rx)
        if hit:
            return name, hit
    return None, None


def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if lo <= v <= hi else None


def _num_of(h, key, lo, hi):
    return _num(h.get(key), lo, hi) if isinstance(h, dict) else None


def apply(text, family, hint=None, explicit_style=False):
    """闸门判定。返回 `None`（不管）或诊断字典：`veto=True` 表示 `replacement` 是新的族。

    只在**即将采用的族**是禁令对象时才有话可说 —— 别的族本来就是证据（发声方式/效价/文本）
    选出来的，禁令没有对象，这也是「拒绝句不是一律被压成 neutral」的落点。
    """
    cls, hit = classify(text)
    if not cls:
        return None
    out = {"class": cls, "hit": hit, "meaning": CLASSES[cls][0], "forbid": CLASSES[cls][1],
           "veto": False, "replacement": None, "force_weak": False, "reason": None,
           "valence": _num_of(hint, "valence", -1.0, 1.0)}
    # 本请求**显式**点了风格（`style_override`）时不插手：闸门纠正的是「高唤醒自动等于 bright」
    # 这一条，不能越权推翻调用方的明确要求（Sol 第 13 轮阻塞点）。
    if explicit_style:
        out["reason"] = "explicit_style_request"
        return out
    if family not in FORBID:
        out["reason"] = "family_not_forbidden(%s)" % family   # 证据已经把它选成别的族了
        return out
    val = out["valence"]
    if val is not None and val >= PLAYFUL_VALENCE:
        out["reason"] = "playful_positive_valence"            # 玩笑式拒绝：该亮就亮
        return out
    if _PLAYFUL.search(text or ""):
        out["reason"] = "playful_marker"
        return out
    eff = val if val is not None else (
        _NEG_VALENCE_FALLBACK if cls in ("fear", "sad_refuse") else None)
    out["veto"] = True
    out["replacement"] = "sad" if (eff is not None and eff <= SAD_AT) else "neutral"
    out["force_weak"] = cls in FORCE_WEAK
    out["reason"] = "forbid_bright(->%s)" % out["replacement"]
    return out


def _selfcheck():                # 自检：五类识别 / 三条不干预 / 换族与降档
    assert classify("だめ、そんなことしないで。") == ("stop", "しないで")
    assert classify("やめて。今すぐやめて。")[0] == "stop"
    assert classify("落ち着いてください。")[0] == "soothe"
    assert classify("来ないで、怖い。")[0] == "fear"
    assert classify("もうこの話はしたくない。")[0] == "sad_refuse"
    assert classify("だめ、やめて。")[0] == "stop"
    # 引述 / 否定 / 非拒绝类：不算
    assert classify("彼は「やめて」と言っただけ。")[0] is None
    assert classify("だめじゃないよ、大丈夫。")[0] is None
    assert classify("やめるつもりはない。")[0] is None
    assert classify("落ち着いてなんかない。")[0] is None
    assert classify("やった、ついに成功した！")[0] is None
    assert classify("今日はいい天気ですね。")[0] is None
    # 高唤醒 + voiced → bright：真拒绝被换族，安抚连强档一起降
    r = apply("だめ、そんなことしないで。", "bright", {"arousal": 0.8, "valence": -0.3})
    assert r["veto"] and r["replacement"] == "neutral" and r["force_weak"], r
    r = apply("やめて。今すぐやめて。", "bright", {"arousal": 0.85, "valence": -0.6})
    assert r["veto"] and r["replacement"] == "sad", r
    r = apply("落ち着いてください。", "bright", {"arousal": 0.8, "valence": -0.2})
    assert r["veto"] and r["replacement"] == "neutral" and r["force_weak"], r
    # 没给效价也照样拦（这正是"高唤醒自动等于 bright"的原始路径）
    assert apply("だめ。", "bright", {"arousal": 0.9})["veto"] is True
    assert apply("来ないで、怖い。", "bright", {"arousal": 0.9})["replacement"] == "sad"
    # 三条不干预：玩笑（正效价 / 玩笑标记）、族本就不是 bright、非该类句子
    assert apply("もう、いやだなあ。ふふ。", "bright", {"valence": 0.6})["veto"] is False
    assert apply("いやだなあ。ふふ。", "bright", {"arousal": 0.8})["reason"] == "playful_marker"
    assert apply("だめ……、そこ……", "intimate", {"phonation": "whispered"})["veto"] is False
    assert apply("やった、ついに成功した！", "bright", {"valence": 0.95}) is None
    print("refusal_guard 自检通过：五类识别 / 引述与否定不算 / 换族（sad|neutral）/ 安抚降档 / 玩笑不干预")
    for k, v in CLASSES.items():
        print("  %-10s %s → 禁止：%s" % (k, v[0], v[1]))


if __name__ == "__main__":
    _selfcheck()
