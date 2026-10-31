# -*- coding: utf-8 -*-
"""plan_version=6（Astra 第 12 轮）：v5 + 四缺口 —— 每项一个独立开关，v6 下默认开。

  ① `ALBION_HINT_PHONATION`  只有 hint 自述 whispered/breathy 时的**发声证据等级**
  ② `ALBION_CROSS_SENTENCE`  「接下来都小声说」这类**跨句指令**的作用域（本 unit 内 + 跨 unit）
  ③ `ALBION_JA_STAGE_CUE`    纯日语舞台提示（`（小声で）`/`息を荒くして`）也算发声证据
  ④ `ALBION_LEVEL_REBASE`    闸门换族后**移除被否决 hint、以文本先验重置强度**

材料全部 CPU 可复现：中文材料走**真桥**（bridge_v2 + 真 bridge.zh_source_cues），翻译腿用
离线替身（固定句表，不起 HTTP）；规划走**真规划器**（scene_plan.plan_units_v3，逐子段串 prev）。
不加载模型、不合成音频、不碰服务。

四层验收：
  ① **v5 逐字段冻结**：`tests/tts/v5_frozen_golden.json` 由**改动前**的 v5 快照
     （`backup/code/20261002/tts/_rollback-20260929-emotion-v5/`）生成（`_tmp/gen_v5_frozen_golden.py`）；
     并且"v6 把四个开关全关"必须与 v5 逐字段相等 —— 证明 v5 及更早不被改写。
  ② **四项各自的最终五字段**（family/caption/cfg/tier/level）+ 诊断字段，逐材料断言。
  ③ **每项开关两态对照**（其余三项保持 v6 默认）—— 证明改动可单独归因、可单独回退。
  ④ **反事实**：把旧行为放回去，本文件的断言必须变红。

跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p "test_emotion_*.py"
"""
import asyncio
import contextlib
import copy
import json
import os
import sys
import unittest
from unittest import mock

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))
sys.path.insert(0, os.path.join(TTS, "stream"))

import bridge                                                          # noqa: E402
import bridge_v2                                                       # noqa: E402
import cue_scope as CS                                                 # noqa: E402
import emotion_codebook as CB                                          # noqa: E402
import render_queue as RQ                                              # noqa: E402
import scene_plan as SP                                                # noqa: E402
import stream_server as SS                                             # noqa: E402

GOLDEN = os.path.join(HERE, "v5_frozen_golden.json")
A_WHISPER = "柔らかく、優しく囁くように"
A_QUIET = "落ち着いた、静かな声で"
A_BREATHY = "息を荒くして、甘く喘ぐように、近い距離で"
A_STRONG = "感情的で、声を張り上げて、早口に"
A_LOW = "落ち着いた声で、沈んだ調子に"

# ---------------- 材料 --------------------------------------------------------
# 单句材料：日语正文 + hint（ja 侧已经翻译完，直接喂规划器）
ONE = {
    # ① hint 自述发声
    "H1_refuse_hint_whisper": (
        "だめです、やめてください……",
        {"arousal": 0.45, "intensity": 0.45, "valence": 0.5, "phonation": "whispered"}),
    "H2_hint_whisper_calm_sentence": (
        "はい、わかりました。",
        {"arousal": 0.2, "intensity": 0.25, "phonation": "whispered"}),
    "H3_hint_breathy_high_arousal": (
        "はい、それでは始めますね。",
        {"arousal": 0.8, "intensity": 0.8, "phonation": "breathy"}),
    "H4_refusal_whisper_negative": (
        "やめてください。",
        {"arousal": 0.9, "intensity": 0.85, "valence": -0.6, "phonation": "whispered"}),
    "H5_negated_surface": ("小声じゃないよ。", {"phonation": "whispered"}),
    "H6_voiced_no_delivery": (
        "今日はいい天気ですね。",
        {"arousal": 0.4, "valence": 0.3, "phonation": "voiced"}),
    # ③ 日语表层舞台提示
    "S1_paren_whisper": ("（小声で）はい、わかりました。", None),
    "S2_breath_stage": ("息を荒くして、肩で息をついた。", None),
    "S3_whisper_over_coarse": ("はぁ……んっ……囁くように、耳元でささやいた。", None),
    "S4_conflict_hint_breathy": ("（小声で）はい。", {"phonation": "breathy"}),
    # ④ 换族后 level 重算（拒绝类，闸门换族）
    "V1_stop_neutral": (
        "だめ、そんなことしないで。",
        {"arousal": 0.75, "intensity": 0.6, "valence": -0.3, "phonation": "voiced"}),
    "V2_stop_sad": (
        "やめて。今すぐやめて。",
        {"arousal": 0.85, "intensity": 0.85, "valence": -0.6, "phonation": "voiced"}),
    "V3_soothe": (
        "落ち着いてください。",
        {"arousal": 0.8, "intensity": 0.7, "valence": -0.2, "phonation": "voiced"}),
    "V4_positive_no_change": (
        "やった、ついに成功した！",
        {"arousal": 0.9, "intensity": 0.85, "valence": 0.95, "phonation": "voiced"}),
}

# 跨句材料：raw = 送桥的原始切片（生产里壳就是这么发的），speech = 可说话文本
XMATS = {
    "X1_persist_whisper": "接下来都耳语说。今天天气不错。明天早上我叫你。",
    "X2_revoke": "接下来都耳语说。今天天气不错。可以正常说话了。明天早上我叫你。",
    "X3_leftover_ja_stage": "今天早点休息。",
}

# ③ 的两条反例材料（Sol 第 13 轮第三条）：引述**不算**指令、引述的撤销**不算**撤销
XMATS_QUOTED = {
    "Q1_quoted_directive": "他说「接下来都耳语说」。今天天气不错。",
    "Q2_quoted_revoke": "接下来都耳语说。今天天气不错。「可以正常说话了」。明天早上我叫你。",
    # 引述**后面**还有真指令/真撤销：只看首个匹配就返回会把它一起漏掉（Sol 第 14 轮第三条）
    "Q3_quote_then_real_directive": "他说「接下来都耳语说」，但我们接下来都小声说。今天天气不错。",
    "Q3b_quote_then_real_whisper": "他说「接下来都小声说」，但我们接下来都耳语说。今天天气不错。",
    "Q4_quote_then_real_revoke": "接下来都耳语说。有人说「可以正常说话了」，但现在可以正常说话了。明天早上我叫你。",
}

# 离线翻译替身：中文句（去句末标点）→ 日语句。③ 的两个材料故意让日语里残留舞台提示。
JA_PLAIN = {
    "接下来都耳语说": "これからは小さな声で話しますね。",
    "今天天气不错": "今日はいい天気ですね。",
    "可以正常说话了": "もう普通に話していいですよ。",
    "明天早上我叫你": "明日の朝、呼びますね。",
    "今天早点休息": "（小声で）今日は早めに寝てくださいね。",   # 译完残留的日语提示
    # 引述材料（整句成一个子段；句末标点在桥里被切走）
    "他说「接下来都耳语说」": "彼は「これからは小さな声で話す」と指摘しただけです。",
    "「可以正常说话了」": "「もう普通に話していい」と彼は言った。",
    # 引述后面还有真指令/真撤销的材料：日语侧保持中性（反面证据只在中文源句上）
    "他说「接下来都耳语说」，但我们接下来都小声说": "彼は何か言っただけです。",
    "他说「接下来都小声说」，但我们接下来都耳语说": "彼は何か言っただけです。",
    "有人说「可以正常说话了」，但现在可以正常说话了": "誰かがそう言っただけです。",
}


def _punct_free(s):
    return (s or "").strip().strip("。！？!?、，,；; \t\n")


def _translator(table=None):
    t = dict(JA_PLAIN)
    t.update(table or {})

    def tr(s):
        k = _punct_free(s)
        if k not in t:
            raise KeyError("离线替身未登记：%r" % k)
        return t[k]
    return tr


def one_run(ja, hint, pv, *, hp=False, xs=False, jc=False, lr=False, cues=None, prev=None):
    """单句：直接喂规划器（ja 侧已完成）。"""
    unit = {"unit_id": "u0", "text": ja, "advance": True}
    if hint:
        unit["emotion"] = copy.deepcopy(hint)
    if cues:
        unit["src_cues"] = [copy.deepcopy(c) for c in cues]
    v4 = pv >= 4
    recs, _ = SP.plan_units_v3([unit], "", prev=prev, plan_version=pv,
                               allow_neutral_whisper=v4, weak_delivery=v4, cue_scope=False,
                               refusal_guard=(pv >= 5), hint_phonation=hp, cross_sentence=xs,
                               ja_stage_cue=jc, level_rebase=lr)
    if v4:                                       # 端点会给 v4+ 记录盖码本版本（render 按它校验）
        recs[0]["codebook_version"] = CB.CODEBOOK_VERSION_V2
    return recs[0]


def x_run(raw, pv, *, cross=None, hp=None, jc=None, lr=None, cue_scope=True):
    """跨句：真桥（cue 按源句归属 + 跨句 pass）+ 真规划器（逐子段串 prev）。

    `plan_version` 随请求发给桥（生产壳就是这么发的）—— 桥按**版本**定开关，不读桥侧环境变量。
    `source_prefix` 留空：这些材料把整段当成**一次**桥请求（同一 unit 内的跨句）。
    """
    v6 = pv >= 6
    flags = {"hp": v6 if hp is None else hp, "xs": v6 if cross is None else cross,
             "jc": v6 if jc is None else jc, "lr": v6 if lr is None else lr}
    svc = bridge_v2.PrepareService(translate=_translator(), cues=bridge.zh_source_cues,
                                   cue_scope=cue_scope, cross_sentence=flags["xs"])
    req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u0", voice="albion",
                                   source_range=(0, len(raw)), speech_text=raw, raw_text=raw,
                                   plan_version=pv)
    res = asyncio.run(svc.prepare(req))
    segs = list(res.ja_segments)
    recs, prev = [], None
    for s in segs:
        r, prev = SP.plan_units_v3([{"unit_id": "u0:%d" % s["sub_index"], "text": s["text"],
                                     "advance": True,
                                     "emotion": copy.deepcopy(s.get("emotion"))}],
                                   prev=prev, plan_version=pv, allow_neutral_whisper=True,
                                   weak_delivery=True, cue_scope=cue_scope,
                                   refusal_guard=True, hint_phonation=flags["hp"],
                                   cross_sentence=flags["xs"], ja_stage_cue=flags["jc"],
                                   level_rebase=flags["lr"])
        if pv >= 4:
            r[0]["codebook_version"] = CB.CODEBOOK_VERSION_V2
        recs.append(r[0])
    return recs, segs


def five(r):
    """最终五字段 —— 断言只落在**最终**记录上，不看中间阶段。"""
    return (r["family"], r["caption"], r["cfg"], r["tier"], r["level"])


def dg(r, *path):
    cur = r["diagnostics"]
    for p in path:
        cur = (cur or {}).get(p)
    return cur


def golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


# ---- Sol 第 13 轮的四条接线/隔离边界的验收小工具 --------------------------------
@contextlib.contextmanager
def _env(d):
    """临时设/清开关类环境变量（每个用例结束后恢复原样）。"""
    keys = ("ALBION_CUE_SCOPE", "ALBION_CROSS_SENTENCE", "ALBION_PLAN_VERSION",
            "ALBION_HINT_PHONATION", "ALBION_JA_STAGE_CUE", "ALBION_LEVEL_REBASE")
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in (d or {}).items():
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _flags(pv, cross=None, hp=None, jc=None, lr=None):
    v6 = pv >= 6
    return {"hp": v6 if hp is None else hp, "xs": v6 if cross is None else cross,
            "jc": v6 if jc is None else jc, "lr": v6 if lr is None else lr}


def _plan_segs(segs, pv, flags, cue_scope=True):
    """逐子段串 `prev` 规划（与壳侧协调器同序）。"""
    recs, prev = [], None
    for s in segs:
        r, prev = SP.plan_units_v3([{"unit_id": "u0:%d" % s["sub_index"], "text": s["text"],
                                     "advance": True,
                                     "emotion": copy.deepcopy(s.get("emotion"))}],
                                   prev=prev, plan_version=pv, allow_neutral_whisper=pv >= 4,
                                   weak_delivery=pv >= 4, cue_scope=cue_scope,
                                   refusal_guard=pv >= 5, hint_phonation=flags["hp"],
                                   cross_sentence=flags["xs"], ja_stage_cue=flags["jc"],
                                   level_rebase=flags["lr"])
        if pv >= 4:
            r[0]["codebook_version"] = CB.CODEBOOK_VERSION_V2
        recs.append(r[0])
    return recs


def _bridge(raw, pv, *, cue_scope=None, cross=None, prefix="", table=None):
    """直调**真桥**；不显式给开关 ⇒ 桥只能靠请求版本/环境决定（生产里壳的形态）。

    离线翻译替身对未登记输入立刻抛错（不兜底），所以材料必须逐句登记。
    """
    svc = bridge_v2.PrepareService(translate=_translator(table), cues=bridge.zh_source_cues,
                                   cue_scope=cue_scope, cross_sentence=cross)
    req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u0", voice="albion",
                                   source_range=(0, len(raw)), speech_text=raw, raw_text=raw,
                                   plan_version=pv, source_prefix=prefix)
    return asyncio.run(svc.prepare(req))


# ---------------- ① v5 冻结 + 版本隔离 ---------------------------------------
class TestV5FrozenAndVersionIsolation(unittest.TestCase):
    def setUp(self):
        self.g = golden()

    def test_v5_one_materials_match_frozen_golden(self):
        for name, (ja, hint) in ONE.items():
            with self.subTest(name):
                self.assertEqual(one_run(ja, hint, 5), self.g["ONE"][name])

    def test_v5_cross_sentence_materials_match_frozen_golden(self):
        """v5 + cue_scope 开（② 的对照形态）。"""
        for name, raw in XMATS.items():
            with self.subTest(name):
                recs, _ = x_run(raw, 5, cue_scope=True)
                self.assertEqual(recs, self.g["X"][name])

    def test_v5_default_shape_also_unchanged(self):
        """v5 的默认形态（cue_scope 关）同样逐字段不动。"""
        for name, raw in XMATS.items():
            with self.subTest(name):
                recs, _ = x_run(raw, 5, cue_scope=False)
                self.assertEqual(recs, self.g["X_off_scope"][name])

    def test_v6_all_switches_off_equals_v5_field_by_field(self):
        """四个开关全关的 v6 == v5（除了 plan_version 这一个字段）。"""
        for name, (ja, hint) in ONE.items():
            with self.subTest(name):
                a, b = one_run(ja, hint, 5), one_run(ja, hint, 6)
                a.pop("plan_version"), b.pop("plan_version")
                self.assertEqual(a, b)
        for name, raw in XMATS.items():
            with self.subTest(name):
                ra, _ = x_run(raw, 5)
                rb, _ = x_run(raw, 6, cross=False, hp=False, jc=False, lr=False)
                for a, b in zip(ra, rb):
                    a.pop("plan_version"), b.pop("plan_version")
                    self.assertEqual(a, b)

    def test_every_switch_off_leaves_own_diagnostics_absent(self):
        """开关关掉时该开关自己的诊断键不出现（老响应不追加新字段）。"""
        r = one_run(*ONE["H2_hint_whisper_calm_sentence"], 6, hp=False, jc=False)
        self.assertIsNone(dg(r, "delivery"))
        self.assertEqual(dg(r, "delivery_stage", "reason"), "unsupported_delivery")
        self.assertEqual(dg(r, "policy"), "cascade_v3")            # 四项全关 ⇒ 与 v5 同档
        recs, _ = x_run(XMATS["X1_persist_whisper"], 6, cross=False, hp=False, jc=False,
                        lr=False)
        for r in recs:
            self.assertNotIn("mode", dg(r, "cue_scope"))
            self.assertNotIn("cross", dg(r, "cue_scope"))
            self.assertEqual(dg(r, "cue_scope", "cross_sentence"), CS.CROSS_SENTENCE)
            self.assertEqual(dg(r, "policy"), "cascade_v3")
        self.assertNotIn("level_rebase", recs[0]["diagnostics"])

    def test_all_v6_materials_stay_inside_codebook(self):
        """v6 不许产出码本白名单外的组合（渲染队列照收）——逐材料验。"""
        items = [("ONE:" + n, one_run(ja, hint, 6, hp=True, jc=True, lr=True))
                 for n, (ja, hint) in ONE.items()]
        for n, raw in XMATS.items():
            items += [("X:%s#%d" % (n, i), r) for i, r in enumerate(x_run(raw, 6)[0])]
        for tag, r in items:
            with self.subTest(tag):
                self.assertIn((r["family"], r["caption"]),
                              CB.allowed_pairs(CB.CODEBOOK_VERSION_V2))
                self.assertIsNone(RQ._check_unit({"unit_id": "u", "text": "はい。",
                                                  "family": r["family"], "caption": r["caption"],
                                                  "cfg": r["cfg"],
                                                  "codebook_version": CB.CODEBOOK_VERSION_V2},
                                                 0, 6), tag)


# ---------------- ① hint 自述的发声证据等级 ----------------------------------
class TestHintPhonationEvidenceTier(unittest.TestCase):
    def test_hint_whisper_alone_reaches_whisper_anchor_at_weak_tier(self):
        """现象本体：只有 hint 说 whispered、句子里什么都没说 —— v5 落 neutral+A_QUIET，v6 借耳语锚。"""
        ja, hint = ONE["H1_refuse_hint_whisper"]
        self.assertEqual(five(one_run(ja, hint, 5)), ("neutral", A_QUIET, 3.0, "weak", 0.49))
        r = one_run(ja, hint, 6, hp=True)
        self.assertEqual(five(r), ("neutral", A_WHISPER, 3.0, "weak", 0.44))
        # 诊断：hint 自述被采纳，但**不作可信来源**（不能给自己滚动续期）
        self.assertEqual(dg(r, "delivery"), "whispered")
        self.assertEqual(dg(r, "delivery_stage", "reason"), "hint_self_report")
        self.assertEqual(dg(r, "delivery_stage", "accepted"), True)
        self.assertEqual(dg(r, "delivery_stage", "evidence_groups"), ["A", "L"])
        # hint 自述的可靠度 0.4 < 句面明确的 0.8/0.85 —— 弱证据就是弱证据
        self.assertEqual(dg(r, "delivery_rel"), 0.4)
        self.assertEqual(dg(r, "delivery_tier"), "hint_self_report")

    def test_hint_whisper_alone_stays_neutral_and_borrows_the_whisper_anchor(self):
        """① 最小取舍（Sol 第 13 轮第五条）：hint 自述**只决定 delivery、不单独选族**。

        「はい、わかりました。」没有任何句面信号，hint 说 whispered —— v5 落 neutral+A_QUIET；
        v6 借码本里**已有**的耳语锚，但族仍是 neutral（不能因为"低声说"就变成 intimate）。
        """
        ja, hint = ONE["H2_hint_whisper_calm_sentence"]
        self.assertEqual(five(one_run(ja, hint, 5)), ("neutral", A_QUIET, 3.0, "weak", 0.33))
        r = one_run(ja, hint, 6, hp=True)
        self.assertEqual(five(r), ("neutral", A_WHISPER, 3.0, "weak", 0.28))
        self.assertNotEqual(r["family"], "intimate", "低可信的发声方式不许单独选族")
        self.assertEqual(r["cfg"], 3.0, "cfg 也必须还是 neutral 的")
        self.assertEqual(dg(r, "delivery_stage", "reason"), "hint_self_report")
        self.assertEqual(dg(r, "delivery_tier"), "hint_self_report")
        # 借的锚是码本里的合法组合（渲染队列照收），不是自造条件
        self.assertIn((r["family"], r["caption"]), CB.allowed_pairs(CB.CODEBOOK_VERSION_V2))

    def test_hint_breathy_is_downgraded_to_weak(self):
        """hint 自述 breathy 时档位保守、且发声方式不参与选族：v5 走 bright+strong，
        v6 只给弱档（族由 hint 的 **arousal** 决定，不由 phonation 决定）。"""
        ja, hint = ONE["H3_hint_breathy_high_arousal"]
        self.assertEqual(five(one_run(ja, hint, 5)), ("bright", A_STRONG, 3.2, "strong", 0.66))
        r = one_run(ja, hint, 6, hp=True)
        self.assertEqual(five(r), ("bright", "明るく、弾んだ声で", 3.2, "weak", 0.81))
        self.assertEqual(r["tier"], "weak")
        # neutral 族没有气声锚：如实记账，不冒充（"气声已听得到"是**未验收**的能力缺口）
        self.assertEqual(dg(r, "gap"), "no_anchor_for_breathy")
        self.assertEqual(dg(r, "delivery"), "breathy")
        self.assertNotEqual(r["family"], "intimate", "breathy 不许单独把它选成 intimate")

    def test_hint_never_beats_explicit_surface_evidence(self):
        """句面明确（S 组）时 hint 冲突不生效：S 的 whispered 压过 hint 的 breathy。"""
        ja, hint = ONE["S4_conflict_hint_breathy"]
        r = one_run(ja, hint, 6, hp=True, jc=True)
        self.assertEqual(five(r), ("intimate", A_WHISPER, 4.5, "weak", 0.6))
        self.assertEqual(dg(r, "delivery"), "whispered")
        self.assertEqual(dg(r, "delivery_tier"), "explicit_surface")
        self.assertEqual(dg(r, "delivery_stage", "reason"), "conflicting_evidence")
        self.assertEqual(dg(r, "delivery_stage", "conflict"), ["breathy", "whispered"])

    def test_hint_self_report_does_not_break_the_refusal_gate(self):
        """① 与语义闸门不打架：拒绝类句子不会因此变亲密，"拒绝不许 bright"依然成立。"""
        ja, hint = ONE["H4_refusal_whisper_negative"]
        r = one_run(ja, hint, 6, hp=True, lr=True)
        self.assertEqual(five(r), ("sad", A_LOW, 3.5, "weak", 0.4))
        self.assertEqual(dg(r, "guard", "class"), "stop")
        self.assertTrue(dg(r, "guard", "veto"))
        self.assertEqual(dg(r, "guard", "replacement"), "sad")
        self.assertEqual(dg(r, "guard", "reason"), "forbid_bright(->sad)")
        self.assertNotEqual(r["family"], "intimate")          # 不许变亲密
        self.assertNotEqual(r["caption"], A_STRONG)           # 不许 bright
        # 发声近似 / 无锚如实记账，不冒充耳语
        self.assertEqual(dg(r, "gap"), "no_anchor_for_whispered")

    def test_negated_or_quoted_surface_still_blocks_hint(self):
        """正文说了"没在小声"时 hint 不许把它念成耳语。"""
        r = one_run("小声じゃないよ。", {"phonation": "whispered"}, 6, hp=True)
        self.assertEqual(five(r), ("neutral", A_QUIET, 3.0, "weak", 0.45))
        self.assertIsNone(dg(r, "delivery"))
        self.assertEqual(dg(r, "delivery_stage", "reason"), "unsupported_delivery")

    def test_non_soft_hint_phonation_is_still_not_evidence(self):
        """voiced 之类不算发声证据：① 只管 soft/breathy/whispered。"""
        ja, hint = ONE["H6_voiced_no_delivery"]
        r = one_run(ja, hint, 6, hp=True, jc=True)
        self.assertEqual(five(r), five(one_run(ja, hint, 5)))
        self.assertIsNone(dg(r, "delivery"))
        self.assertEqual(dg(r, "delivery_tier"), "prosody")     # 只有韵律推断

    def test_switch_two_states(self):
        """③ 两态对照：只有 ① 变，其余三项保持 v6 默认。"""
        for name, want_off, want_on in (
                ("H1_refuse_hint_whisper", ("neutral", A_QUIET, 3.0, "weak", 0.49),
                 ("neutral", A_WHISPER, 3.0, "weak", 0.44)),
                ("H2_hint_whisper_calm_sentence", ("neutral", A_QUIET, 3.0, "weak", 0.33),
                 ("neutral", A_WHISPER, 3.0, "weak", 0.28)),
                ("H3_hint_breathy_high_arousal", ("bright", A_STRONG, 3.2, "strong", 0.66),
                 ("bright", "明るく、弾んだ声で", 3.2, "weak", 0.81))):
            ja, hint = ONE[name]
            with self.subTest(name):
                off = one_run(ja, hint, 6, hp=False, xs=True, jc=True, lr=True)
                on = one_run(ja, hint, 6, hp=True, xs=True, jc=True, lr=True)
                self.assertEqual(five(off), want_off)
                self.assertEqual(five(on), want_on)
                self.assertNotEqual(five(off), five(on))


# ---------------- ② 跨句指令 --------------------------------------------------
class TestCrossSentenceDirective(unittest.TestCase):
    def test_persist_directive_covers_following_source_sentences(self):
        recs, segs = x_run(XMATS["X1_persist_whisper"], 6)
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.11)])
        # 首句是自带 cue；后两句是**跨句副本**（带来源与区间）
        self.assertEqual(dg(recs[0], "cue_scope", "mode"), "sentence")
        self.assertIsNone(dg(recs[0], "cue_scope", "cross"))
        for i in (1, 2):
            self.assertEqual(dg(recs[i], "cue_scope", "mode"), "cross_sentence")
            self.assertEqual(dg(recs[i], "cue_scope", "cross"), {"from": 0, "to": 2})
            self.assertEqual(dg(recs[i], "delivery"), "whispered")
            self.assertEqual(dg(recs[i], "delivery_stage", "reason"), "explicit_directive")
        # 副本盖的是**接收句自己的**源句身份
        self.assertEqual([s["emotion"]["src_scope"]["sentence"] for s in segs], [0, 1, 2])
        self.assertEqual([s["emotion"]["src"][0]["sid"] for s in segs],
                         ["0-23#0", "0-23#1", "0-23#2"])
        self.assertTrue(segs[1]["emotion"]["src"][0]["cross_sentence"])
        self.assertEqual((segs[1]["emotion"]["src"][0]["cross_from"],
                          segs[1]["emotion"]["src"][0]["cross_to"]), (0, 2))
        self.assertNotIn("cross_sentence", segs[0]["emotion"]["src"][0])
        self.assertEqual([s["emotion"]["src"][0]["kind"] for s in segs],
                         ["whisper", "whisper", "whisper"])

    def test_cross_unit_continuation_is_done_by_the_bridge_not_the_planner(self):
        """跨 unit 的续接在**桥侧**（重放壳发的 `source_prefix`）；规划器自己不做外推。"""
        recs, _ = x_run(XMATS["X1_persist_whisper"], 6)
        for r in recs:
            self.assertEqual(dg(r, "cue_scope", "cross_unit"), CS.CROSS_UNIT)
        self.assertEqual(CS.CROSS_UNIT, CS.CROSS_SUPPORTED, "跨 unit 的续接已支持（桥侧重放）")
        # 规划器单独看这句话（没有桥给的跨句副本）⇒ 不外推、也不给续期
        nxt = one_run("今日はいい天気ですね。", None, 6)
        self.assertIsNone(dg(nxt, "delivery"))

    def test_revoke_sentence_restores_normal(self):
        recs, _ = x_run(XMATS["X2_revoke"], 6)
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28),
                          ("neutral", A_QUIET, 3.0, "weak", 0.11),
                          ("neutral", A_QUIET, 3.0, "weak", 0.0)])
        self.assertEqual(dg(recs[1], "cue_scope", "cross"), {"from": 0, "to": 1})
        self.assertIsNone(dg(recs[2], "cue_scope", "cross"))     # 「可以正常说话了。」后失效
        self.assertIsNone(dg(recs[2], "delivery"))
        self.assertIsNone(dg(recs[3], "delivery"))

    def test_switch_two_states(self):
        """② 两态对照：关掉就只有首句自己有 cue。"""
        recs_off, _ = x_run(XMATS["X1_persist_whisper"], 6, cross=False)
        self.assertEqual([five(r) for r in recs_off],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_QUIET, 3.0, "weak", 0.28),
                          ("neutral", A_QUIET, 3.0, "weak", 0.11)])
        self.assertIsNone(dg(recs_off[1], "delivery"))
        self.assertEqual(dg(recs_off[1], "delivery_stage", "reason"), "stale_context")

    def test_counterfactual_legacy_copy_to_every_segment_turns_revoke_red(self):
        """反事实：把旧的"整份 cue 复制给每个子段"放回去，撤销句的断言必须变红。"""
        orig = CS.attribute

        def legacy(text, cues, n_segments, unit_key="", **kw):
            rows, diag = orig(text, cues, n_segments, unit_key=unit_key, cross_sentence=False)
            rows = [[dict(c, sid=diag["ids"][i]) for c in (cues or [])]
                    for i in range(len(rows))]
            return rows, diag

        with mock.patch.object(CS, "attribute", legacy):
            recs, _ = x_run(XMATS["X2_revoke"], 6)
        # 撤销句被旧行为硬塞了耳语 cue ⇒ 本文件的正确断言（A_QUIET）不再成立
        self.assertEqual(five(recs[2]), ("neutral", A_WHISPER, 3.0, "weak", 0.11))


# ---------------- ③ 纯日语舞台提示 -------------------------------------------
class TestJapaneseStageCue(unittest.TestCase):
    def test_paren_stage_directive_is_evidence_in_v6(self):
        ja, _ = ONE["S1_paren_whisper"]
        self.assertEqual(five(one_run(ja, None, 5)), ("neutral", A_QUIET, 3.0, "weak", 0.45))
        r = one_run(ja, None, 6, jc=True)
        self.assertEqual(five(r), ("neutral", A_WHISPER, 3.0, "weak", 0.45))
        self.assertEqual(dg(r, "delivery"), "whispered")
        self.assertEqual(dg(r, "delivery_stage", "reason"), "ja_stage_directive")
        self.assertIn("S", dg(r, "delivery_stage", "evidence_groups"))
        self.assertIn("ja_stage:（小声で）", dg(r, "delivery_stage", "origin_ids"))
        self.assertEqual(dg(r, "delivery_tier"), "explicit_surface")

    def test_leftover_stage_directive_from_translation_is_covered(self):
        """"中→日翻译后残留的日语提示"这一情况：中文侧没有 cue，日语表层带（小声で）。"""
        recs, _ = x_run(XMATS["X3_leftover_ja_stage"], 6)
        self.assertEqual(five(recs[0]), ("neutral", A_WHISPER, 3.0, "weak", 0.45))
        self.assertEqual(dg(recs[0], "delivery_stage", "reason"), "ja_stage_directive")
        recs_off, _ = x_run(XMATS["X3_leftover_ja_stage"], 6, jc=False)
        self.assertEqual(five(recs_off[0]), ("neutral", A_QUIET, 3.0, "weak", 0.45))
        self.assertIsNone(dg(recs_off[0], "delivery"))

    def test_stage_cue_steers_away_from_the_coarse_anchor(self):
        """囁くように 与粗气息同句：取耳语目标，别喂成粗气息锚。"""
        ja, _ = ONE["S3_whisper_over_coarse"]
        self.assertEqual(five(one_run(ja, None, 5)), ("intimate", A_BREATHY, 4.5, "strong", 0.73))
        r = one_run(ja, None, 6, jc=True)
        self.assertEqual(five(r), ("intimate", A_WHISPER, 4.5, "weak", 0.73))
        self.assertEqual(dg(r, "delivery"), "whispered")

    def test_breathy_stage_cue_is_honestly_recorded_as_no_anchor(self):
        """`息を荒くして` 是粗气息（breathy），不是耳语；neutral 族没有气声锚 ⇒ 如实记账。"""
        ja, _ = ONE["S2_breath_stage"]
        r = one_run(ja, None, 6, jc=True)
        self.assertEqual(five(r), ("neutral", A_QUIET, 3.0, "weak", 0.45))
        self.assertEqual(dg(r, "delivery"), "breathy")
        self.assertEqual(dg(r, "gap"), "no_anchor_for_breathy")

    def test_switch_two_states(self):
        for name, want_off, want_on in (
                ("S1_paren_whisper", ("neutral", A_QUIET, 3.0, "weak", 0.45),
                 ("neutral", A_WHISPER, 3.0, "weak", 0.45)),
                ("S3_whisper_over_coarse", ("intimate", A_BREATHY, 4.5, "strong", 0.73),
                 ("intimate", A_WHISPER, 4.5, "weak", 0.73))):
            ja, _ = ONE[name]
            with self.subTest(name):
                self.assertEqual(five(one_run(ja, None, 6, jc=False)), want_off)
                self.assertEqual(five(one_run(ja, None, 6, jc=True)), want_on)

    def test_chinese_cue_path_is_not_rewritten(self):
        """既有中文通路（bridge.zh_source_cues + v4 J 组）输出不许被 ③ 改写。"""
        raw = "（小声说）今天早点休息"
        speech = "今天早点休息"
        svc = bridge_v2.PrepareService(translate=_translator(), cues=bridge.zh_source_cues,
                                       cue_scope=True, cross_sentence=True)
        req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u0", voice="albion",
                                       source_range=(0, len(raw)), speech_text=speech,
                                       raw_text=raw)
        res = asyncio.run(svc.prepare(req))
        seg = res.ja_segments[0]
        self.assertEqual(seg["emotion"]["src"][0]["kind"], "soft")     # 「（小声说）」→ soft
        self.assertEqual(seg["emotion"]["src"][0]["span"], [0, 5])
        self.assertEqual(seg["emotion"]["src_scope"]["basis"], "span")
        self.assertEqual(seg["emotion"]["src_scope"]["sentence"], 0)
        self.assertNotIn("cross_sentence", seg["emotion"]["src"][0])   # 不是跨句副本


# ---------------- ④ 换族后 level 重算 ----------------------------------------
class TestLevelRebaseAfterFamilySwap(unittest.TestCase):
    def test_stop_gate_resets_level_from_text_prior(self):
        """④ 换族后 level = **移除被否决 hint、以文本先验重置强度**（不是"按新族重算"：
        公式读的是文本先验 + 发声微调，不读替换后的族；族相关的强度校准**未验收**）。"""
        ja, hint = ONE["V1_stop_neutral"]
        self.assertEqual(five(one_run(ja, hint, 5)), ("neutral", A_QUIET, 3.0, "weak", 0.54))
        r = one_run(ja, hint, 6, lr=True)
        self.assertEqual(five(r), ("neutral", A_QUIET, 3.0, "weak", 0.45))
        self.assertEqual(dg(r, "level_rebase", "before"), 0.54)
        self.assertEqual(dg(r, "level_rebase", "after"), 0.45)
        self.assertEqual(dg(r, "level_rebase", "basis"), "guard_veto_text_prior_reset")
        self.assertTrue(dg(r, "guard", "veto"))
        self.assertEqual(dg(r, "guard", "replacement"), "neutral")

    def test_refusal_whisper_material_gets_both_rebase_and_no_surface_delivery(self):
        """① 与 ④ 的交互：hint 自述给了耳语，但被 ④ 重算后的 level 落在弱档。"""
        ja, hint = ONE["H4_refusal_whisper_negative"]
        r = one_run(ja, hint, 6, hp=True, lr=True)
        self.assertEqual(five(r), ("sad", A_LOW, 3.5, "weak", 0.4))
        self.assertEqual(dg(r, "level_rebase"), {"before": 0.64, "after": 0.4,
                                                 "basis": "guard_veto_text_prior_reset"})

    def test_sad_and_soothe_veto_branches(self):
        for name, want_before, want_after in (("V2_stop_sad", 0.69, 0.45),
                                              ("V3_soothe", 0.6, 0.45)):
            ja, hint = ONE[name]
            with self.subTest(name):
                base = one_run(ja, hint, 5)
                r = one_run(ja, hint, 6, lr=True)
                self.assertEqual(r["level"], want_after)
                self.assertNotEqual(r["level"], base["level"])
                self.assertEqual(dg(r, "level_rebase", "before"), want_before)
                self.assertEqual(dg(r, "level_rebase", "after"), want_after)

    def test_family_and_anchor_are_untouched_by_rebase(self):
        """④ 只动 level：family/caption/cfg/tier 不许因它改变。"""
        ja, hint = ONE["V2_stop_sad"]
        a, b = one_run(ja, hint, 6, lr=False), one_run(ja, hint, 6, lr=True)
        self.assertEqual(five(a)[:4], five(b)[:4])
        self.assertEqual(five(a), ("sad", A_LOW, 3.5, "weak", 0.69))
        self.assertEqual(five(b), ("sad", A_LOW, 3.5, "weak", 0.45))

    def test_no_veto_means_no_rebase(self):
        ja, hint = ONE["V4_positive_no_change"]
        r = one_run(ja, hint, 6, lr=True)
        self.assertEqual(five(r), ("bright", A_STRONG, 3.2, "strong", 0.714))
        self.assertIsNone(dg(r, "level_rebase"))
        self.assertIsNone(dg(r, "guard"))

    def test_switch_two_states(self):
        for name, want_off, want_on in (("V1_stop_neutral", 0.54, 0.45),
                                        ("V2_stop_sad", 0.69, 0.45),
                                        ("V3_soothe", 0.6, 0.45)):
            ja, hint = ONE[name]
            with self.subTest(name):
                self.assertEqual(one_run(ja, hint, 6, lr=False)["level"], want_off)
                self.assertEqual(one_run(ja, hint, 6, lr=True)["level"], want_on)
                self.assertIsNone(dg(one_run(ja, hint, 6, lr=False), "level_rebase"))


# ---------------- ③ 反例：引述不是指令、引述的撤销不算撤销（Sol 第 13 轮第三条）----
class TestQuotedCrossSentenceNotADirective(unittest.TestCase):
    """断言落在**最终五字段**上（不只看 `rows`/`diag`）：引述的指令与撤销都必须被挡住。"""

    def setUp(self):
        e = _env({})
        e.__enter__()
        self.addCleanup(lambda: e.__exit__(None, None, None))

    def test_quoted_directive_does_not_reach_the_next_sentence(self):
        raw = XMATS_QUOTED["Q1_quoted_directive"]
        recs, segs = x_run(raw, 6)
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_QUIET, 3.0, "weak", 0.45),
                          ("neutral", A_QUIET, 3.0, "weak", 0.28)])
        for r in recs:
            self.assertNotEqual(r["caption"], A_WHISPER)
            self.assertIsNone(dg(r, "delivery"))
        for s in segs:
            self.assertNotIn("src", s["emotion"], "引述里的提示词不产生指令")

    def test_quoted_revoke_does_not_revoke(self):
        recs, _ = x_run(XMATS_QUOTED["Q2_quoted_revoke"], 6)
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.11),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.0)])
        # 对照：同一句**没被引述**就是真撤销（X2 材料）—— 后两句的五字段立刻回落
        ctrl, _ = x_run(XMATS["X2_revoke"], 6)
        self.assertEqual([five(r) for r in ctrl][2:],
                         [("neutral", A_QUIET, 3.0, "weak", 0.11),
                          ("neutral", A_QUIET, 3.0, "weak", 0.0)])

    def test_negated_phonation_word_is_not_a_directive(self):
        """否定窗口：`接下来别耳语说` 否定的是**发声词** ⇒ 不算持续指令，也不外推。"""
        self.assertIsNone(CS._persist_kind("接下来别耳语说"))
        self.assertEqual(CS.active_directive("接下来别耳语说。"), None)
        rows, diag = CS.attribute("接下来别耳语说。今天天气不错。", [], 2, unit_key="k",
                                  cross_sentence=True)
        self.assertEqual(rows, [[], []])
        self.assertEqual(diag["persist"], [])
        self.assertFalse(diag.get("open_at_end"))

    def test_quoted_directive_then_a_real_one_in_the_same_sentence(self):
        """反例 ③-a（Sol 第 14 轮第三条 CPU 反例）：首个匹配在引号里 ⇒ 不能就此收工。

        `他说「接下来都耳语说」，但我们接下来都小声说。` —— 引述的"耳语"不算指令，但**后半句
        的"小声"是真指令**。修前返回 None（真指令被引述挡掉），修后必须认出 soft 并外推到后句。
        """
        raw = XMATS_QUOTED["Q3_quote_then_real_directive"]
        self.assertEqual(CS._persist_kind(raw), "soft", "引述后面的真指令被漏掉了")
        self.assertEqual(CS.active_directive(raw), "soft")
        recs, _ = x_run(raw, 6)
        # 后句最终五字段 + 诊断：拿到跨句副本（soft ⇒ neutral 的静かな声锚）
        self.assertEqual(five(recs[1]), ("neutral", A_QUIET, 3.0, "weak", 0.28))
        self.assertEqual(dg(recs[1], "cue_scope", "mode"), "cross_sentence")
        self.assertEqual(dg(recs[1], "cue_scope", "cross"), {"from": 0, "to": 1})
        self.assertEqual(dg(recs[1], "delivery"), "soft")

    def test_quoted_soft_then_a_real_whisper_reaches_the_next_sentence(self):
        """反例 ③-a 的可观测形态：引述里是"小声"，后半句真正的指令是"耳语" ⇒ 后句借耳语锚。"""
        raw = XMATS_QUOTED["Q3b_quote_then_real_whisper"]
        self.assertEqual(CS._persist_kind(raw), "whisper")
        recs, _ = x_run(raw, 6)
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28)])
        self.assertEqual(dg(recs[1], "cue_scope", "mode"), "cross_sentence")
        self.assertEqual(dg(recs[1], "delivery"), "whispered")

    def test_quoted_revoke_then_a_real_one_in_the_same_sentence(self):
        """反例 ③-b：首个撤销匹配在引号里 ⇒ 必须往后找真撤销。

        `接下来都耳语说。有人说「可以正常说话了」，但现在可以正常说话了。明天早上我叫你。`
        —— 修前仍返回 whisper（真撤销被引述挡掉），修后第三句必须回落。
        """
        raw = XMATS_QUOTED["Q4_quote_then_real_revoke"]
        self.assertIsNone(CS.active_directive(raw), "引述后面的真撤销被漏掉了")
        recs, _ = x_run(raw, 6)
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_QUIET, 3.0, "weak", 0.28),
                          ("neutral", A_QUIET, 3.0, "weak", 0.11)])
        self.assertIsNone(dg(recs[2], "delivery"), "撤销句之后的句子不该还挂着耳语")
        self.assertIsNone(dg(recs[2], "cue_scope", "cross"))


# ---------------- ② 版本隔离：v5 在桥侧任何环境变量下都不被改写 -----------------
class TestBridgeVersionIsolation(unittest.TestCase):
    """跨句开关**随请求版本**走（Sol 第 13 轮第二条）。

    复现（修前必红）：桥进程设了 `ALBION_CROSS_SENTENCE=1` 时，旧实现把**请求 v5** 的材料后两句
    从 neutral/静かな声 改成 neutral/囁く声。这里对每一种桥侧环境形态，都把 v5 的最终记录与
    **改动前生成的冻结黄金**（`v5_frozen_golden.json` 的 X / X_off_scope 两态）逐字段对照。
    """

    def setUp(self):
        self.g = golden()
        e = _env({})
        e.__enter__()
        self.addCleanup(lambda: e.__exit__(None, None, None))

    def test_bridge_and_planner_resolve_the_same_cross_value_for_one_request(self):
        """阻塞 1（Sol 第 14 轮第一条）：同一次请求，桥与规划器算出的跨句开关必须**相同**。

        桥侧 `_cross_switch(None, pv)`（壳不再下发这个值）与规划器端点的 `v6 and _cross_sentence_on(body)`
        逐组合对照：`pv<6` 恒关、`pv>=6` 由 `ALBION_CROSS_SENTENCE` 决定（`0/false` 是关）。
        """
        for pv in (4, 5, 6):
            for env in (None, "0", "1", "false", "off"):
                with self.subTest(pv=pv, env=env):
                    with _env({} if env is None else {"ALBION_CROSS_SENTENCE": env}):
                        b = bridge_v2.PrepareService(translate=_translator())._cross_switch(None, pv)
                        p = (pv >= 6) and SP._cross_sentence_on({})     # 规划器端点口径
                    self.assertIs(b, p, "桥与规划器对同一次请求算出了不同的开关值")
                    if pv < 6:
                        self.assertIs(b, False)

    def test_v5_explicit_cross_true_is_gated_by_version(self):
        """阻塞 2（Sol 第 14 轮第二条）：`_cross_switch` **先判版本** —— v5 的显式 True 不再是旁路。

        旧实现先接受请求布尔/构造参数，`_cross_switch(True, 5) == True`；v5 三句材料 + 桥构造参数
        `cross_sentence=True` 时耳语锚从 `[是, 否, 否]` 变成 `[是, 是, 是]`。这里两者一起回归。
        """
        svc = bridge_v2.PrepareService(translate=_translator(), cues=bridge.zh_source_cues,
                                       cue_scope=True, cross_sentence=True)
        with _env({}):
            self.assertIs(svc._cross_switch(True, 5), False, "v5 下显式 True 不许生效")
            self.assertIs(svc._cross_switch(None, 5), False)
        with _env({"ALBION_CROSS_SENTENCE": "1"}):
            self.assertIs(svc._cross_switch(True, 5), False, "环境变量也开不了 v5 的跨句")
        res = _bridge(XMATS["X1_persist_whisper"], 5, cue_scope=True, cross=True)
        recs = _plan_segs(list(res.ja_segments), 5, _flags(5, cross=False), cue_scope=True)
        self.assertEqual([r["caption"] for r in recs], [A_WHISPER, A_QUIET, A_QUIET],
                         "v5 的耳语锚不许被插回后两句")
        self.assertEqual(recs, self.g["X"]["X1_persist_whisper"], "v5 冻结输出不许被改写")

    def test_v5_beats_the_bridge_cross_env_in_both_cue_scope_states(self):
        for scope_env, key, planner_scope in ((None, "X_off_scope", False),
                                              ({"ALBION_CUE_SCOPE": "1"}, "X", True)):
            for cross_env in (None, "0", "1"):
                d = dict(scope_env or {})
                if cross_env is not None:
                    d["ALBION_CROSS_SENTENCE"] = cross_env
                for name, raw in XMATS.items():
                    with self.subTest(scope=key, cross_env=cross_env, name=name):
                        with _env(d):
                            res = _bridge(raw, 5)          # 请求 v5、不带任何显式开关
                        self.assertEqual(res.status, "ready", (res.status, res.error))
                        recs = _plan_segs(list(res.ja_segments), 5,
                                          _flags(5, cross=False), cue_scope=planner_scope)
                        self.assertEqual(recs, self.g[key][name])

    def test_v6_request_does_turn_cross_on_in_the_bridge(self):
        """反向：同一个桥、同样环境，v6 请求**能**开 —— 版本是唯一的开关来源。"""
        with _env({}):
            res1 = _bridge(XMATS["X1_persist_whisper"], 6)
            recs1 = _plan_segs(list(res1.ja_segments), 6, _flags(6))
        with _env({"ALBION_CROSS_SENTENCE": "0"}):
            res0 = _bridge(XMATS["X1_persist_whisper"], 6)
            recs0 = _plan_segs(list(res0.ja_segments), 6, _flags(6, cross=False))
        self.assertEqual([five(r) for r in recs1],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.11)])
        self.assertEqual([five(r) for r in recs0][:1], [("neutral", A_WHISPER, 3.0, "weak", 0.45)])
        self.assertEqual([five(r) for r in recs0][1:],
                         [("neutral", A_QUIET, 3.0, "weak", 0.28),
                          ("neutral", A_QUIET, 3.0, "weak", 0.11)])


# ---------------- ② 流式那一半：SessionText → prepare → plan ------------------
class TestCrossUnitStreamingWiring(unittest.TestCase):
    """生产里 `session_text` 按硬标点把文本切成**每句一个 unit**，桥每次只看到一句 ——
    跨句指令不可能在一次 prepare 里闭环（Sol 第 13 轮第一条）。这里用与壳**同序**的驱动：
    真 `SessionText` 切单元 → 真 `Coordinator.prepare`（in-process 桥的 HTTP 路由）→
    真 `Coordinator.plan`（in-process 规划器的 HTTP 路由），逐子段串 `prev`。
    跨 unit 的延续靠 `prepare` 随请求发出去的 `source_prefix`（桥侧重放）。
    """

    def setUp(self):
        e = _env({})
        e.__enter__()
        self.addCleanup(lambda: e.__exit__(None, None, None))

    def _async_drive(self, raw, pv=6):
        from fastapi import FastAPI
        app = FastAPI()
        bridge_v2.install(app, translate=_translator(), cues=bridge.zh_source_cues)
        SP.install(app)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=20.0)
        cfg = SS.Config(plan_version=pv, bridge_url="http://b", irodori_url="http://i")
        co = SS.Coordinator(cfg)
        co._client = client
        sess = SS.Session(cfg, "s1", 1, "albion", "wav")
        units = sess.text.feed(raw, 0.0) + sess.text.finish(1.0)
        recs = []
        for u in units:
            assert u.kind == "speech", u
            res = asyncio.run(self._one(co, sess, u, recs))
            assert res["status"] == "ready", res
        asyncio.run(client.aclose())
        return recs, units

    async def _one(self, co, sess, unit, recs):
        res = await co.prepare(sess, unit)
        for s in res.get("ja_segments") or []:
            plan, state, _fb = await co.plan(s["text"], "%s:%d" % (unit.unit_id, s["sub_index"]),
                                             context_before=sess.emotion_ctx,
                                             prev=sess.emotion_prev, emotion=s.get("emotion"),
                                             advance=(unit.reason not in ("soft", "force")))
            recs.append(plan)
            if state:
                sess.emotion_prev = state
                sess.emotion_ctx = (sess.emotion_ctx + " " + s["text"])[-SS.EMOTION_CTX_CHARS:]
        return res

    def test_persist_directive_survives_the_streaming_cut(self):
        recs, units = self._async_drive(XMATS["X1_persist_whisper"])
        self.assertEqual([u.speech_text for u in units],
                         ["接下来都耳语说。", "今天天气不错。", "明天早上我叫你。"],
                         "材料不成立：这三句必须**各成一个 unit**（跨 unit 才有意义）")
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.11)])
        self.assertEqual(dg(recs[0], "cue_scope", "mode"), "sentence")
        for r in recs[1:]:
            self.assertEqual(dg(r, "cue_scope", "mode"), "cross_sentence")
            self.assertEqual(dg(r, "cue_scope", "cross"), {"from": -1, "to": 0})
            self.assertEqual(dg(r, "delivery"), "whispered")

    def test_revoke_sentence_restores_normal_through_the_same_path(self):
        recs, _ = self._async_drive(XMATS["X2_revoke"])
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_WHISPER, 3.0, "weak", 0.28),
                          ("neutral", A_QUIET, 3.0, "weak", 0.11),
                          ("neutral", A_QUIET, 3.0, "weak", 0.0)])
        self.assertIsNone(dg(recs[2], "delivery"))
        self.assertIsNone(dg(recs[3], "delivery"))
        self.assertIsNone(dg(recs[3], "cue_scope", "cross"), "撤销后不再有跨句副本")

    def test_cross_env_zero_turns_it_off_through_the_full_wiring(self):
        """阻塞 1 的关闭态（Sol 第 14 轮第一条）：`ALBION_CROSS_SENTENCE=0` 走**完整接线**必须真关掉。

        断言落在**最终五字段**：后两句不再借耳语锚；规划器的 `cue_scope.cross` 也如实为空
        （旧实现的错位正是"显示已关、声音条件仍在变"）。
        """
        with _env({"ALBION_CROSS_SENTENCE": "0"}):
            recs, units = self._async_drive(XMATS["X1_persist_whisper"])
        self.assertEqual([u.speech_text for u in units],
                         ["接下来都耳语说。", "今天天气不错。", "明天早上我叫你。"])
        self.assertEqual([five(r) for r in recs],
                         [("neutral", A_WHISPER, 3.0, "weak", 0.45),
                          ("neutral", A_QUIET, 3.0, "weak", 0.28),
                          ("neutral", A_QUIET, 3.0, "weak", 0.11)])
        for r in recs[1:]:
            self.assertIsNone(dg(r, "cue_scope", "cross"), "显示已关，声音条件不许仍在变")
            self.assertIsNone(dg(r, "delivery"))
            self.assertEqual(dg(r, "cue_scope", "cross_sentence"), CS.CROSS_SENTENCE)

    def test_prepare_request_carries_version_switch_and_source_prefix(self):
        """版本与 `source_prefix` **随请求**发给桥；跨句开关**不由壳写死**（否则关不掉）。"""
        seen = []

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "ready", "ja_segments": []}

        class _Stub:
            async def post(self, url, json=None, timeout=None):
                seen.append((url, json))
                return _Resp()

        def drive(pv, text):
            cfg = SS.Config(plan_version=pv)
            co = SS.Coordinator(cfg)
            co._http = lambda: _Stub()
            sess = SS.Session(cfg, "s1", 1, "albion", "wav")
            us = sess.text.feed(text, 0.0) + sess.text.finish(1.0)
            asyncio.run(co.prepare(sess, us[1]))
            return seen[-1][1]

        body6 = drive(6, "接下来都耳语说。今天天气不错。")
        self.assertEqual(body6["plan_version"], 6)
        self.assertEqual(body6["source_prefix"], "接下来都耳语说。")
        # 壳不下发跨句开关：写死 True 会把桥侧 ALBION_CROSS_SENTENCE=0 短路掉（Sol 第 14 轮第一条）
        self.assertNotIn("cross_sentence", body6)
        with _env({"ALBION_CROSS_SENTENCE": "1"}):
            body5 = drive(5, "接下来都耳语说。今天天气不错。")
        self.assertEqual(body5["plan_version"], 5)
        self.assertNotIn("cross_sentence", body5)


# ---------------- 开关与版本端点 ---------------------------------------------
class TestSwitchesAndEndpoint(unittest.TestCase):
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        return TestClient(app)

    def _unit(self):
        return {"unit_id": "u0", "text": "はい、わかりました。",
                "emotion": {"arousal": 0.2, "intensity": 0.25, "phonation": "whispered"}}

    def test_v6_features_default_on_and_reported(self):
        c = self._client()
        env = {"ALBION_PLAN_VERSION": "6", "ALBION_CUE_SCOPE": "1", "ALBION_REFUSAL_GUARD": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            r = c.post("/v2/plan", json={"plan_version": 6, "units": [self._unit()]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["plan_version"], 6)
        self.assertEqual(body["v6_features"], {"hint_phonation": True, "cross_sentence": True,
                                               "ja_stage_cue": True, "level_rebase": True})
        self.assertEqual((body["units"][0]["family"], body["units"][0]["caption"]),
                         ("neutral", A_WHISPER))

    def test_each_switch_can_be_turned_off_by_env_and_by_body(self):
        c = self._client()
        pairs = (("ALBION_HINT_PHONATION", "hint_phonation"), ("ALBION_CROSS_SENTENCE",
                                                              "cross_sentence"),
                 ("ALBION_JA_STAGE_CUE", "ja_stage_cue"), ("ALBION_LEVEL_REBASE",
                                                           "level_rebase"))
        for env_name, field in pairs:
            with self.subTest(env_name):
                env = {"ALBION_PLAN_VERSION": "6", env_name: "0"}
                with mock.patch.dict(os.environ, env, clear=False):
                    r = c.post("/v2/plan", json={"plan_version": 6, "units": [self._unit()]})
                self.assertFalse(r.json()["v6_features"][field])
                # 请求体显式布尔 > 环境
                r2 = c.post("/v2/plan", json={"plan_version": 6, "units": [self._unit()],
                                              field: True})
                self.assertTrue(r2.json()["v6_features"][field])

    def test_switches_are_ignored_below_v6(self):
        c = self._client()
        for pv in (4, 5):
            with self.subTest(pv):
                r = c.post("/v2/plan", json={"plan_version": pv, "units": [self._unit()]})
                self.assertEqual(r.status_code, 200)
                self.assertNotIn("v6_features", r.json())
                self.assertEqual(r.json()["units"][0]["family"], "neutral")

    def test_v6_cue_scope_env_zero_turns_the_feature_off(self):
        """④ 关闭态：v6 里 `cue_scope` 默认开，但显式 `0` 必须读成关（它一度读不了 0）。"""
        c = self._client()
        with _env({}):
            self.assertTrue(CS.enabled(None, default=True))
            self.assertFalse(CS.enabled(None, default=False))
        for zero in ("0", "false", "off", "no"):
            with _env({"ALBION_CUE_SCOPE": zero}):
                self.assertFalse(CS.enabled(None, default=True), zero)
        with _env({"ALBION_CUE_SCOPE": "1"}):
            self.assertTrue(CS.enabled(None, default=False))
            self.assertFalse(CS.enabled(False, default=True), "显式 False 压过环境")
        with mock.patch.dict(os.environ, {"ALBION_CUE_SCOPE": "0"}, clear=False):
            os.environ.pop("ALBION_PLAN_VERSION", None)
            r = c.post("/v2/plan", json={"plan_version": 6, "units": [self._unit()]})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("cue_scope", r.json(), "v6 默认开也必须能读 0")

    def test_versions_below_v6_are_untouched_by_new_diagnostics(self):
        c = self._client()
        r = c.post("/v2/plan", json={"plan_version": 5, "units": [self._unit()]})
        self.assertEqual(r.json()["units"][0]["diagnostics"]["delivery_stage"]["reason"],
                         "unsupported_delivery")
        self.assertNotIn("delivery_tier", r.json()["units"][0]["diagnostics"])

    def test_endpoint_v5_body_matches_frozen_golden_field_by_field(self):
        """回退路径（S 侧把 plan_version 降回 5）在**端点**上也逐字段等于冻结黄金。"""
        g = golden()
        c = self._client()
        for name, (ja, hint) in ONE.items():
            with self.subTest(name):
                unit = {"unit_id": "u0", "text": ja, "advance": True}
                if hint:
                    unit["emotion"] = copy.deepcopy(hint)
                r = c.post("/v2/plan", json={"plan_version": 5, "units": [unit]})
                self.assertEqual(r.status_code, 200)
                rec = dict(r.json()["units"][0])
                rec.pop("voice", None)          # 端点按请求体盖音色，这里没给
                self.assertEqual(rec, g["ONE"][name])
                self.assertNotIn("v6_features", r.json())

    def test_stream_config_accepts_six_but_not_seven(self):
        import stream_server
        self.assertEqual(stream_server.Config(plan_version=6).plan_version, 6)
        self.assertEqual(stream_server.Config().plan_version, 4)
        for bad in (2, 7, "v6"):
            with self.assertRaises(ValueError):
                stream_server.Config(plan_version=bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
