# -*- coding: utf-8 -*-
"""拒绝/制止/安抚类句子的语义闸门（Astra 第 11 轮 ②）：最终条件 + v4 冻结 + 反事实。

要修的东西（实测，不是转述）：v4 里「高 arousal + voiced」被 `family_from_hint` 的
`hot >= 0.65` 分支判成 bright，于是拒绝句拿到 `bright / 感情的で、声を張り上げて、早口に /
cfg=3.2` —— "明るく、弾んだ声で" 说出的拒绝、"声を張り上げて" 说出的安抚，都是错的。

本文件锁四件事：
  ① **v4 逐字段冻结**：同一批材料在 v4（本轮的既有版本）下必须与
     `v4_refusal_golden.json` 逐字段相同。该黄金文件由**改动前的 roller 快照**
     `backup/code/20261002/tts/_rollback-20260928-emotion-v3/scene_plan.py` 生成（生成命令写在 `_tmp/impl_round11_refusal_evidence.md`），
     所以它证明的是"旧 v4 没被原地改写"，而不是"我拿改后的代码对了一遍自己"。
  ② **v5 的最终条件**：family/caption/cfg/tier/level 全部断言（不看中间阶段），含成对材料
     （玩笑式拒绝 vs 真拒绝：同一句日语、只有上下文不同）、正向兴奋防误伤、日语引述/否定/亲密上下文。
  ③ **两态对照**：v5 关掉闸门 = v4（只差 plan_version 字段），所以"改了哪一层"是分开可归因的。
  ④ **反事实**：把闸门拿掉，本文件的正断言必须变红（否则测试是空转的）。

纯 CPU、离线、不加载模型、不联网、不碰 GPU。
跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p "test_emotion_*.py" -v
"""
import asyncio
import copy
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))
sys.path.insert(0, os.path.join(TTS, "stream"))

import bridge                                                          # noqa: E402
import bridge_v2                                                       # noqa: E402
import emotion_codebook as CB                                          # noqa: E402
import refusal_guard as RG                                             # noqa: E402
import render_queue as RQ                                              # noqa: E402
import scene_plan as SP                                                # noqa: E402

GOLDEN = os.path.join(HERE, "v4_refusal_golden.json")
A_QUIET, A_NEUTRAL = CB.A_QUIET, CB.A_NEUTRAL
A_LOW, A_BRIGHT, A_STRONG = CB.A_LOW, CB.A_BRIGHT, CB.A_STRONG
A_WHISPER, A_BREATHY = CB.A_WHISPER, CB.A_BREATHY

# ---------------- 材料（zh = 生产里送桥的中文；ja = 送规划器的日语；hint = 上游 LLM 的情绪提示）
# 中文句取自 `tests/tts/eval/ab_stream_v4.py` 的「中文-普通拒绝」那一组（就是发现这个误判的材料）。
# ja 是逐句等价的离线替身：规划器只看日语正文 + hint，本轮不依赖真翻译模型（见证据文件的局限节）。
MATS = {
    # ① 平静拒绝「不行，别这样。」
    "R1_calm_refuse": {
        "zh": "不行，别这样。", "ja": "だめ、そんなことしないで。",
        "hint": {"arousal": 0.75, "intensity": 0.6, "valence": -0.3, "phonation": "voiced"},
        "cls": "stop", "veto": True,
        "v5": ("neutral", A_QUIET, 3.0, "weak", 0.54)},
    # ② 坚定制止「停下。现在就停。」
    "R2_firm_stop": {
        "zh": "停下。现在就停。", "ja": "やめて。今すぐやめて。",
        "hint": {"arousal": 0.85, "intensity": 0.85, "valence": -0.6, "phonation": "voiced"},
        "cls": "stop", "veto": True,
        "v5": ("sad", A_LOW, 3.5, "weak", 0.69)},
    # ②b 制止（ab 材料原句「我说了不要，快住手。」）
    "R2b_stop_repeat": {
        "zh": "我说了不要，快住手。", "ja": "やめてって言ったでしょ、早くやめて。",
        "hint": {"arousal": 0.85, "intensity": 0.8, "valence": -0.5, "phonation": "voiced"},
        "cls": "stop", "veto": True,
        "v5": ("sad", A_LOW, 3.5, "weak", 0.66)},
    # ③ 安抚降温「请你先冷静一下。」—— 规格点名的：不许"明亮弹跳"，也不许"提高嗓门"
    "R3_soothe": {
        "zh": "请你先冷静一下。", "ja": "落ち着いてください。",
        "hint": {"arousal": 0.8, "intensity": 0.7, "valence": -0.2, "phonation": "voiced"},
        "cls": "soothe", "veto": True,
        "v5": ("neutral", A_QUIET, 3.0, "weak", 0.60)},
    # ④ 恐惧制止「别过来，我害怕。」
    "R4_fear_stop": {
        "zh": "别过来，我害怕。", "ja": "来ないで、怖い。",
        "hint": {"arousal": 0.9, "intensity": 0.8, "valence": -0.7, "phonation": "voiced"},
        "cls": "fear", "veto": True,
        "v5": ("sad", A_LOW, 3.5, "weak", 0.66)},
    # ⑤ 悲伤拒绝「我不想再谈这件事了。」
    "R5_sad_refuse": {
        "zh": "我不想再谈这件事了。", "ja": "もうこの話はしたくない。",
        "hint": {"arousal": 0.7, "intensity": 0.6, "valence": -0.6, "phonation": "voiced"},
        "cls": "sad_refuse", "veto": True,
        "v5": ("sad", A_LOW, 3.5, "weak", 0.54)},
    # ⑥⑦ **成对样本**：同一句日语、同一个词，只换上下文（效价）——
    #   玩笑式拒绝该亮就亮，真拒绝不许"明亮弹跳"。
    "R6_joke_refuse": {
        "zh": "讨厌啦，人家才不要呢。", "ja": "いやだってば、もう。",
        "hint": {"arousal": 0.8, "intensity": 0.7, "valence": 0.6, "phonation": "voiced"},
        "cls": "refuse", "veto": False, "why": "playful_positive_valence",
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.60)},
    "R7_real_refuse": {
        "zh": "我是认真的，不行。", "ja": "いやだってば、もう。",
        "hint": {"arousal": 0.8, "intensity": 0.7, "valence": -0.6, "phonation": "voiced"},
        "cls": "refuse", "veto": True,
        "v5": ("sad", A_LOW, 3.5, "weak", 0.60)},
    # ⑦b 真拒绝但提示没给效价：照样受保护（"高唤醒自动等于 bright"的原始路径）
    "R7b_real_no_valence": {
        "zh": "讨厌，我说真的。", "ja": "いやだなあ。もう、本気で言ってるの。",
        "hint": {"arousal": 0.8, "intensity": 0.7},
        "cls": "refuse", "veto": True,
        "v5": ("neutral", A_QUIET, 3.0, "weak", 0.60)},
    # ⑧ 玩笑标记（无负效价证据）⇒ 不干预
    "R8_joke_marker": {
        "zh": "讨厌啦，嘻嘻。", "ja": "いやだなあ。ふふ。",
        "hint": {"arousal": 0.8, "intensity": 0.7},
        "cls": "refuse", "veto": False, "why": "playful_marker",
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.60)},
    # ⑨ 日语「だめ」普通用法
    "J1_dame_plain": {
        "zh": "不行，住手。", "ja": "だめ、やめて。",
        "hint": {"arousal": 0.8, "intensity": 0.75, "valence": -0.5, "phonation": "voiced"},
        "cls": "stop", "veto": True,
        "v5": ("sad", A_LOW, 3.5, "weak", 0.63)},
    # ⑩ 引述：转述别人的话，不是当前说话者的制止 ⇒ 不干预
    "J2_quoted": {
        "zh": "他只说了「不要」。", "ja": "彼は「やめて」と言っただけ。",
        "hint": {"arousal": 0.8, "intensity": 0.7, "valence": -0.4, "phonation": "voiced"},
        "cls": None, "veto": None,
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.60)},
    # ⑪ 否定：だめじゃない / 落ち着いてなんかない ⇒ 不是拒绝、也不是安抚 ⇒ 不干预
    "J3_dame_negated": {
        "zh": "又不是不行，没事的。", "ja": "だめじゃないよ、大丈夫。",
        "hint": {"arousal": 0.8, "intensity": 0.7, "valence": -0.3},
        "cls": None, "veto": None,
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.60)},
    "J4_soothe_negated": {
        "zh": "我一点都不冷静。", "ja": "落ち着いてなんかない。",
        "hint": {"arousal": 0.8, "intensity": 0.7, "valence": -0.4},
        "cls": None, "veto": None,
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.60)},
    # ⑫ 亲密上下文反例：耳语/气声场景里的「だめ」必须**留在 intimate**，不许被压平
    "J5_intimate_whisper": {
        "zh": "（声音放轻）不要……那里……", "ja": "……小声で、だめ……そこ……",
        "hint": {"phonation": "whispered", "arousal": 0.7, "intensity": 0.6, "valence": 0.2},
        "cls": "refuse", "veto": False, "why": "family_not_forbidden(intimate)",
        "v5": ("intimate", A_WHISPER, 4.5, "weak", 0.53)},
    "J5b_intimate_breathy": {
        "zh": "（气息不稳）不要……那里……", "ja": "だめ……、そこ……",
        "cues": [{"kind": "breath", "span": [0, 2], "negated": False}],
        "hint": {"phonation": "breathy", "arousal": 0.8, "intensity": 0.7, "valence": 0.2},
        "cls": "refuse", "veto": False, "why": "family_not_forbidden(intimate)",
        "v5": ("intimate", A_BREATHY, 4.5, "strong", 0.79)},
    # ⑬ 正向兴奋：修拒绝句不能把真正的 bright 一起压掉
    "P1_happy_rule": {
        "zh": "太好了，终于成功了！", "ja": "やった、ついに成功した！",
        "hint": {"arousal": 0.9, "intensity": 0.85, "valence": 0.95, "phonation": "voiced"},
        "cls": None, "veto": None,
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.714)},
    "P2_happy_hint": {
        "zh": "终于做完了呢。", "ja": "ついに最後までできたの。",
        "hint": {"arousal": 0.85, "intensity": 0.8, "valence": 0.9, "phonation": "voiced"},
        "cls": None, "veto": None,
        "v5": ("bright", A_STRONG, 3.2, "strong", 0.66)},
}

# 闸门**本来就不该动**的材料（v5 与 v4 的最终条件必须逐字段相同）
UNTOUCHED = ("R6_joke_refuse", "R8_joke_marker", "J2_quoted", "J3_dame_negated",
             "J4_soothe_negated", "J5_intimate_whisper", "J5b_intimate_breathy",
             "P1_happy_rule", "P2_happy_hint")


def plan(text, hint=None, pv=SP.PLAN_VERSION_V5, guard=True, cues=None, prev=None, uid="u0"):
    """喂一句给真规划器（与生产同序：一次一句 + state 串回去）。"""
    unit = {"unit_id": uid, "text": text, "advance": True}
    if hint is not None:
        unit["emotion"] = dict(hint)
    if cues:
        unit["src_cues"] = [dict(c) for c in cues]
    recs, state = SP.plan_units_v3([unit], prev=prev, plan_version=pv,
                                   allow_neutral_whisper=pv >= SP.PLAN_VERSION_V4,
                                   weak_delivery=pv >= SP.PLAN_VERSION_V4,
                                   refusal_guard=(guard and pv >= SP.PLAN_VERSION_V5))
    return recs[0], state


def run_mat(name, pv, guard=True):
    m = MATS[name]
    return plan(m["ja"], m.get("hint"), pv=pv, guard=guard, cues=m.get("cues"))


def plain(rec):
    """层③要断言的东西：**最终**记录里的条件字段（不是中间阶段）。"""
    return (rec["family"], rec["caption"], rec["cfg"], rec["tier"], rec["level"])


def guard_of(rec):
    return (rec.get("diagnostics") or {}).get("guard") or {}


# ---------------- ① v4 逐字段冻结（回滚基线） --------------------------------
class TestV4Frozen(unittest.TestCase):
    def test_v4_matches_pre_change_golden(self):
        """v4 = 本轮改动前的冻结输出：黄金文件由改动前的 scene_plan 生成，不是自比对。"""
        with open(GOLDEN, encoding="utf-8") as f:
            golden = json.load(f)
        self.assertEqual(sorted(golden), sorted(MATS))
        for name in sorted(MATS):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V4)
            got = json.loads(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            self.assertEqual(got, golden[name], "%s 的 v4 输出变了（回滚基线被动过）" % name)

    def test_v4_still_puts_the_refusals_on_bright(self):
        """把误判本身钉住：v4 下这五类拒绝/制止/安抚句仍是 bright/strong/cfg=3.2。"""
        for name in ("R1_calm_refuse", "R2_firm_stop", "R3_soothe", "R4_fear_stop",
                     "R5_sad_refuse"):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V4)
            self.assertEqual((rec["family"], rec["cfg"], rec["tier"]), ("bright", 3.2, "strong"),
                             "%s：v4 的误判材料不成立，测试会空转" % name)
            self.assertIn("張り上げ", rec["caption"])


# ---------------- ② v5 的最终条件（含成对材料 / 防误伤 / 日语反例） -----------
class TestV5FinalConditions(unittest.TestCase):
    def test_every_material_final_conditions(self):
        for name in sorted(MATS):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            self.assertEqual(plain(rec), MATS[name]["v5"], name)

    def test_guard_classification_and_veto_per_material(self):
        for name in sorted(MATS):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            g = guard_of(rec)
            self.assertEqual(g.get("class"), MATS[name]["cls"], "%s：语义类别判错" % name)
            self.assertEqual(g.get("veto"), MATS[name]["veto"], "%s：该不该拦判错" % name)
            if MATS[name].get("why"):
                self.assertEqual(g.get("reason"), MATS[name]["why"], name)
            if MATS[name]["veto"]:
                self.assertTrue(g.get("forbid"), name)          # 禁止条件要写在记录里
                self.assertEqual(rec["diagnostics"]["gap"], RG.GAP, name)

    def test_soothe_gets_neither_bright_nor_strong(self):
        """规格点名：「请你先冷静一下。」不能仅因上游高 arousal 就"明亮弹跳"/"提高嗓门"。"""
        rec, _ = run_mat("R3_soothe", pv=SP.PLAN_VERSION_V5)
        self.assertNotEqual(rec["family"], "bright")
        self.assertNotEqual(rec["cfg"], 3.2)
        self.assertEqual(rec["tier"], "weak")                   # 强档 = 声を張り上げて/早口
        self.assertEqual(rec["caption"], A_QUIET)               # 落ち着いた、静かな声で
        self.assertNotEqual(rec["caption"], A_STRONG)

    def test_joke_vs_real_pair_same_sentence_context_decides(self):
        """成对样本：**同一句日语**，只有上下文（效价）不同 ⇒ 最终条件必须分岔。"""
        joke = MATS["R6_joke_refuse"]
        real = MATS["R7_real_refuse"]
        self.assertEqual(joke["ja"], real["ja"], "成对材料必须是同一句日语")
        self.assertGreater(joke["hint"]["valence"], 0)
        self.assertLess(real["hint"]["valence"], 0)
        jr, _ = run_mat("R6_joke_refuse", pv=SP.PLAN_VERSION_V5)
        rr, _ = run_mat("R7_real_refuse", pv=SP.PLAN_VERSION_V5)
        self.assertEqual(plain(jr), joke["v5"])
        self.assertEqual(plain(rr), real["v5"])
        self.assertNotEqual(plain(jr), plain(rr), "玩笑式拒绝与真拒绝的最终条件必须不同")
        self.assertEqual((jr["family"], jr["cfg"]), ("bright", 3.2))   # 玩笑：该亮就亮
        self.assertNotIn(rr["family"], ("bright",))                    # 真拒绝：不许明亮弹跳

    def test_positive_excitement_is_not_suppressed(self):
        """正向兴奋防误伤：真 bright 一处都不许被压掉。"""
        for name in ("P1_happy_rule", "P2_happy_hint"):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            self.assertEqual(plain(rec), MATS[name]["v5"], name)
            self.assertEqual((rec["family"], rec["cfg"], rec["tier"]), ("bright", 3.2, "strong"))
            self.assertEqual(guard_of(rec), {}, "%s：正向句不该进闸门" % name)

    def test_intimate_context_is_not_flattened(self):
        """亲密上下文反例：耳语/气声场景里的「だめ」留在 intimate（不被压成 neutral/sad）。"""
        for name in ("J5_intimate_whisper", "J5b_intimate_breathy"):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            self.assertEqual(rec["family"], "intimate", name)
            self.assertEqual(rec["cfg"], 4.5, name)
            self.assertEqual(guard_of(rec).get("veto"), False, name)
            self.assertEqual(rec["diagnostics"]["delivery"],
                             "whispered" if name.endswith("whisper") else "breathy", name)

    def test_quoted_and_negated_are_not_refusals(self):
        """引述/否定里的词不算：过闸门 = 空转，v5 必须等于 v4。"""
        for name in ("J2_quoted", "J3_dame_negated", "J4_soothe_negated"):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            self.assertEqual(guard_of(rec), {}, "%s：引述/否定不该进闸门" % name)
            v4, _ = run_mat(name, pv=SP.PLAN_VERSION_V4)
            self.assertEqual(plain(rec), plain(v4), name)

    def test_no_new_families_or_anchors(self):
        """本轮不新增字族、不新增训练锚：换出去的族与锚都必须还在旧码本里。"""
        self.assertEqual(set(CB.TRAINED),
                         {A_NEUTRAL, A_QUIET, A_LOW, A_BRIGHT, A_STRONG, A_BREATHY, A_WHISPER})
        self.assertEqual(len(CB.TRAINED), 7)
        self.assertNotIn("angry", CB.LADDER)                     # 没有"生气"族
        for name in sorted(MATS):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            self.assertIn((rec["family"], rec["caption"]), CB.allowed_pairs(CB.CODEBOOK_VERSION_V2),
                          name)
            self.assertIsNone(RQ._check_unit(
                {"unit_id": "u", "text": "はい。", "family": rec["family"], "caption": rec["caption"],
                 "cfg": rec["cfg"], "codebook_version": CB.CODEBOOK_VERSION_V2}, 0, 5), name)


# ---------------- ③ 两态对照：闸门关掉 = v4 ----------------------------------
class TestTwoState(unittest.TestCase):
    def test_v5_without_guard_equals_v4(self):
        """v5 只关闸门 ⇒ 与本轮之前的 v4 逐字段相同（只差 plan_version 字段）。"""
        for name in sorted(MATS):
            on, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            off, _ = run_mat(name, pv=SP.PLAN_VERSION_V5, guard=False)
            v4, _ = run_mat(name, pv=SP.PLAN_VERSION_V4)
            self.assertEqual({k: v for k, v in off.items() if k != "plan_version"},
                             {k: v for k, v in v4.items() if k != "plan_version"},
                             "%s：v5 关闸门后与 v4 不等（版本号本身改了行为）" % name)
            self.assertEqual(off["plan_version"], 5)
            self.assertNotIn("guard", off["diagnostics"])
            self.assertEqual(off["diagnostics"]["policy"], "cascade_v2")

    def test_guard_is_the_only_delta(self):
        """开/关闸门的差异只落在被拦的材料上，其余一律相同。"""
        for name in sorted(MATS):
            on, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            off, _ = run_mat(name, pv=SP.PLAN_VERSION_V5, guard=False)
            if name in UNTOUCHED:
                self.assertEqual(plain(on), plain(off), "%s：本来不该动的被动了" % name)
            elif MATS[name]["veto"]:
                self.assertNotEqual(plain(on), plain(off), "%s：该拦的没拦" % name)


# ---------------- ④ 反事实：把闸门拿掉，正断言必须变红 ------------------------
class TestCounterfactual(unittest.TestCase):
    def _positive(self, name="R1_calm_refuse"):
        rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
        self.assertEqual(plain(rec), MATS[name]["v5"])

    def test_positive_assertions_hold_normally(self):
        for name in ("R1_calm_refuse", "R3_soothe", "R7_real_refuse"):
            self._positive(name)

    def test_removing_the_guard_turns_red(self):
        with mock.patch.object(RG, "apply", lambda *a, **k: None):
            for name in ("R1_calm_refuse", "R3_soothe", "R7_real_refuse"):
                with self.assertRaises(AssertionError, msg=name):
                    self._positive(name)

    def test_shrinking_the_veto_set_turns_red(self):
        """只禁 bright 不够：强档也必须一起禁（安抚不能变"声を張り上げて"）。"""
        for name in ("R1_calm_refuse", "R3_soothe"):
            rec, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
            self.assertEqual(rec["tier"], "weak", name)
            with mock.patch.object(RG, "FORCE_WEAK", ()):
                rec2, _ = run_mat(name, pv=SP.PLAN_VERSION_V5)
                self.assertEqual(rec2["tier"], "strong", name)
                self.assertNotEqual(plain(rec2), MATS[name]["v5"], name)


# ---------------- ⑤ 接线：真桥 → 真规划器（v5） ------------------------------
JA_BY_ZH = {m["zh"]: m["ja"] for m in MATS.values()}


def translate_ex(sentence):
    """离线替身：中文句 → (日语, 上游情绪提示)。未登记的中文立刻失败，不兜底成假绿。"""
    key = (sentence or "").strip().strip("。！？!?、，,；; \t\n")
    for zh, m in ((m["zh"], m) for m in MATS.values()):
        if zh.strip().strip("。！？!?、，,；; \t\n") == key:
            return m["ja"], dict(m.get("hint") or {})
    raise KeyError("离线替身未登记：%r" % key)


class TestBridgeWiring(unittest.TestCase):
    """身份/hint 从真桥一路到规划器：证明闸门吃的是**生产形状**的输入。"""

    def _segments(self, zh):
        svc = bridge_v2.PrepareService(lambda s: "", translate_ex=translate_ex,
                                       cues=bridge.zh_source_cues)
        req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u0", voice="albion",
                                       source_range=(0, len(zh)), speech_text=zh, raw_text=zh)
        res = asyncio.run(svc.prepare(req))
        self.assertEqual(res.status, "ready", res)
        return list(res.ja_segments)

    def _run(self, zh, pv):
        segs, recs, prev = self._segments(zh), [], None
        for seg in segs:
            r, prev = SP.plan_units_v3([{"unit_id": "u0:%d" % seg["sub_index"], "text": seg["text"],
                                         "advance": True, "emotion": copy.deepcopy(seg["emotion"])}],
                                       prev=prev, plan_version=pv,
                                       allow_neutral_whisper=pv >= 4, weak_delivery=pv >= 4,
                                       refusal_guard=pv >= SP.PLAN_VERSION_V5)
            recs.append(r[0])
        return segs, recs

    def test_refusal_rides_the_real_bridge_into_v5(self):
        segs, recs = self._run("不行，别这样。", SP.PLAN_VERSION_V5)
        self.assertEqual(segs[0]["text"], MATS["R1_calm_refuse"]["ja"])
        self.assertEqual(segs[0]["emotion"]["arousal"], 0.75)          # hint 真到了规划器
        self.assertEqual(recs[0]["diagnostics"]["policy"], "cascade_v3")
        self.assertEqual(plain(recs[0]), MATS["R1_calm_refuse"]["v5"])

    def test_same_bridge_input_differs_between_v4_and_v5(self):
        _, v4 = self._run("不行，别这样。", SP.PLAN_VERSION_V4)
        _, v5 = self._run("不行，别这样。", SP.PLAN_VERSION_V5)
        self.assertEqual(v4[0]["diagnostics"]["policy"], "cascade_v2")
        self.assertEqual((v4[0]["family"], v4[0]["cfg"]), ("bright", 3.2))
        self.assertNotEqual(plain(v4[0]), plain(v5[0]))

    def test_positive_excitement_still_bright_through_the_bridge(self):
        _, recs = self._run("太好了，终于成功了！", SP.PLAN_VERSION_V5)
        self.assertEqual((recs[0]["family"], recs[0]["cfg"]), ("bright", 3.2))
        self.assertNotIn("guard", recs[0]["diagnostics"])


# ---------------- ⑥ 版本/开关：端点与渲染队列 --------------------------------
class TestVersionsAndSwitches(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ALBION_REFUSAL_GUARD", None)
        os.environ.pop("ALBION_CUE_SCOPE", None)
        os.environ.pop("ALBION_PLAN_VERSION", None)

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        return TestClient(app)

    def _unit(self, name="R1_calm_refuse"):
        m = MATS[name]
        u = {"unit_id": "u0", "text": m["ja"], "advance": True, "emotion": dict(m["hint"])}
        if m.get("cues"):
            u["src_cues"] = [dict(c) for c in m["cues"]]
        return u

    def test_plan_endpoint_v5(self):
        c = self._client()
        r = c.post("/v2/plan", json={"plan_version": 5, "units": [self._unit()]})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["plan_version"], 5)
        self.assertEqual(b["codebook_version"], CB.CODEBOOK_VERSION_V2)
        self.assertIs(b["refusal_guard"], True)
        rec = b["units"][0]
        self.assertEqual((rec["family"], rec["caption"], rec["cfg"], rec["tier"], rec["level"]),
                         MATS["R1_calm_refuse"]["v5"])
        self.assertEqual(rec["codebook_version"], CB.CODEBOOK_VERSION_V2)

    def test_plan_endpoint_v4_is_untouched(self):
        c = self._client()
        b = c.post("/v2/plan", json={"plan_version": 4, "units": [self._unit()]}).json()
        self.assertNotIn("refusal_guard", b)                    # 旧响应不追加新字段
        self.assertEqual((b["units"][0]["family"], b["units"][0]["cfg"]), ("bright", 3.2))
        with open(GOLDEN, encoding="utf-8") as f:
            want = json.load(f)["R1_calm_refuse"]
        got = json.loads(json.dumps(b["units"][0], ensure_ascii=False, sort_keys=True))
        # 端点会在记录上追加 voice / codebook_version（旧响应本来就有）—— 只比规划字段
        self.assertEqual({k: got[k] for k in want}, want)

    def test_plan_endpoint_switch_off_within_v5(self):
        """开关单独关：仍拿 v5，但条件与 v4 相同（归因用）。"""
        c = self._client()
        off = c.post("/v2/plan", json={"plan_version": 5, "units": [self._unit()],
                                       "refusal_guard": False}).json()
        self.assertNotIn("refusal_guard", off)
        self.assertEqual((off["units"][0]["family"], off["units"][0]["cfg"]), ("bright", 3.2))

    def test_env_switch_off(self):
        with mock.patch.dict(os.environ, {"ALBION_REFUSAL_GUARD": "0"}):
            self.assertFalse(SP._refusal_guard_on({}))
            self.assertTrue(SP._refusal_guard_on({"refusal_guard": True}))   # 请求里显式优先
        self.assertTrue(SP._refusal_guard_on({}))

    def test_unknown_versions_rejected(self):
        c = self._client()
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 7,
                                                  "units": [self._unit()]}).status_code, 400)
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 5, "units": [self._unit()],
                                                  "emotion_policy": "fusion_v1"}).status_code, 400)
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 5, "units": [self._unit()],
                                                  "codebook_version": CB.CODEBOOK_VERSION}
                                ).status_code, 400)

    def test_render_queue_accepts_v5(self):
        u = {"unit_id": "u", "text": "はい。", "family": "neutral", "caption": A_QUIET, "cfg": 3.0,
             "codebook_version": CB.CODEBOOK_VERSION_V2}
        self.assertIsNone(RQ._check_unit(dict(u), 0, 5))
        self.assertIn("codebook_version", RQ._check_unit(dict(u, codebook_version=None), 0, 5) or "")
        self.assertIn("不匹配", RQ._check_unit(dict(u, codebook_version=CB.CODEBOOK_VERSION), 0, 5) or "")
        # v5 的新条件仍是码本内合法条件，队列照收
        rec, _ = run_mat("R3_soothe", pv=SP.PLAN_VERSION_V5)
        self.assertIsNone(RQ._check_unit({"unit_id": "u", "text": "はい。", "family": rec["family"],
                                          "caption": rec["caption"], "cfg": rec["cfg"],
                                          "codebook_version": CB.CODEBOOK_VERSION_V2}, 0, 5))

    def test_explicit_style_request_is_not_overridden_by_the_guard(self):
        # 显式点风格时闸门不插手（Sol 第 13 轮阻塞点；修正前这条必红）
        for t in ("だめ、やめて。", "落ち着いてください。"):
            rec = SP.plan_units_v3(
                [{"unit_id": "u", "text": t, "advance": True,
                  "emotion": {"arousal": 0.9, "valence": -0.3, "phonation": "voiced"}}],
                plan_version=5, refusal_guard=True, style_override="happy",
                allow_neutral_whisper=True, weak_delivery=True)[0][0]
            self.assertNotEqual(rec["family"], "neutral", "显式风格被闸门推翻了：%s" % t)

    def test_unresolved_style_override_does_not_disable_the_guard(self):
        # 不在风格表里的非空值：规划器本来就忽略它（override=None），闸门**不能**当成"显式指定"
        # 而放过该拦的 bright（Sol 第 14 轮旁路；修正前这条必红）
        rec = SP.plan_units_v3(
            [{"unit_id": "u", "text": "だめ、やめて。", "advance": True,
              "emotion": {"arousal": 0.9, "valence": -0.3, "phonation": "voiced"}}],
            plan_version=5, refusal_guard=True, style_override="不存在的风格",
            allow_neutral_whisper=True, weak_delivery=True)[0][0]
        self.assertEqual(rec["family"], "neutral", "无效 style_override 让闸门放过了该拦的 bright")

    def test_stream_config_versions(self):
        import stream_server
        # 未配置 = 现行 v4：新版本一律 opt-in（与 ALBION_CUE_SCOPE 同口径），v5/v6 必须显式指定
        self.assertEqual(stream_server.Config().plan_version, 4)
        self.assertEqual(stream_server.Config(plan_version=4).plan_version, 4)
        self.assertEqual(stream_server.Config(plan_version=5).plan_version, 5)
        self.assertEqual(stream_server.Config(plan_version=6).plan_version, 6)
        for bad in (2, 7, "v5"):
            with self.assertRaises(ValueError):
                stream_server.Config(plan_version=bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
