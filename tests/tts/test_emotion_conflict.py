# -*- coding: utf-8 -*-
"""证据优先级冲突（Sol 第 18 轮）：已采纳的**明确**发声线索压过相冲突的 hint phonation。

现象（生产 WS 实录）：源句 `他说「接下来都耳语说」，但我们接下来都小声说。`
—— cue 归属已正确排除引号里的「耳语」，只留下引号外的「小声说」（kind=soft，见下面 FROZEN_A
里逐字冻结的桥输出）；但同一句的翻译 hint 里带着 `phonation=whispered`。修前规划器**采纳
soft 作为 delivery 之后**，仍用这个相冲突的 hint phonation 去选族 ⇒ `intimate` /
`柔らかく、優しく囁くように` / cfg=4.5 / 耳语锚（落点 `scene_plan.py:697` 那一段）。

修法（v6-only，无新开关）：在**选族与算强度之前**，若已采纳的明确发声线索与 hint 的 phonation
相冲突，就丢掉该 hint phonation（族与 level 都不再用它）——与既有的「hint 自述只决定 delivery、
不单独选族」同口径。冲突的尺子直接复用 `emotion_fusion.phonation_conflict`（voiced/soft ↔
whispered/breathy 才算打架；whispered↔breathy 是同轴强弱，不算冲突）。
这段只在 ①/③ 开关打开（即 v6）时才有 `d_tier` ⇒ v2/v3/v4/v5 的确定性输出逐字节不变。

材料：**固定同一份桥输出**（不跑真桥）。下面 FROZEN_* 里的 `text` / `src_cues` / `src_scope`
就是真桥（`bridge_v2.PrepareService` + 离线翻译替身，纯 CPU）对那几句中文原文的输出，逐字抄录；
`phonation` 是生产那次翻译 hint 里的值（桥的 hint 由上游 LLM 给，离线替身不产 hint，所以这里
按实录固定）。断言只落在**最终五字段** family/caption/cfg/tier/level（外加必要的诊断键）。

跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p "test_emotion_*.py"
"""
import copy
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))

import emotion_codebook as CB      # noqa: E402
import emotion_fusion as F         # noqa: E402
import scene_plan as SP            # noqa: E402

A_WHISPER = "柔らかく、優しく囁くように"
A_QUIET = "落ち着いた、静かな声で"

# ---- 冻结的桥输出（真桥逐字抄录，见模块说明）----------------------------------
# ③ 源句「他说「接下来都耳语说」，但我们接下来都小声说。」→ 日语子段 + 只留引号外的 soft cue
FROZEN_A = {
    "unit_id": "u0", "text": "彼は何か言っただけです。", "advance": True,
    "emotion": {"phonation": "whispered", "arousal": 0.35, "intensity": 0.3,
                "src_scope": {"id": "0-30#0", "basis": "span"}},
    "src_cues": [{"kind": "soft", "negated": False, "span": [19, 21], "sid": "0-30#0",
                  "sentence": 0}],
}
# 真耳语指令两条形态：桥 cue=whisper；以及日语表层舞台提示（③）残留
FROZEN_B_CUE = {
    "unit_id": "u0", "text": "これからは小さな声で話しますね。", "advance": True,
    "emotion": {"phonation": "whispered", "arousal": 0.35, "intensity": 0.3,
                "src_scope": {"id": "0-40#0", "basis": "span"}},
    "src_cues": [{"kind": "whisper", "negated": False, "span": [4, 6], "sid": "0-40#0",
                  "sentence": 0}],
}
FROZEN_B_STAGE = {
    "unit_id": "u0", "text": "（小声で）はい、わかりました。", "advance": True,
    "emotion": {"phonation": "whispered", "arousal": 0.2, "intensity": 0.25},
}
# ④ 源句「有人说「可以正常说话了」，但现在可以正常说话了。」→ 真撤销 ⇒ 桥**不给** cue
FROZEN_C_REVOKE = {
    "unit_id": "u1", "text": "誰かがそう言っただけです。", "advance": True,
    "emotion": {"arousal": 0.3, "intensity": 0.3,
                "src_scope": {"id": "0-40#1", "basis": "span"}},
}


def plan(unit, pv, prev=None):
    """喂规划器（与生产端点同口径：v4+ 开弱线索闸门与 cue 归属，v5+ 开语义闸门，v6 四项开）。"""
    v4, v6 = pv >= 4, pv >= 6
    recs, state = SP.plan_units_v3(
        [copy.deepcopy(unit)], "", prev=prev, plan_version=pv,
        allow_neutral_whisper=v4, weak_delivery=v4, cue_scope=v6,
        refusal_guard=(pv >= 5), hint_phonation=v6, cross_sentence=v6,
        ja_stage_cue=v6, level_rebase=v6)
    recs[0]["codebook_version"] = CB.CODEBOOK_VERSION_V2
    return recs[0], state


def five(r):
    """最终五字段 —— 断言只落在**最终**记录上，不看中间阶段。"""
    return (r["family"], r["caption"], r["cfg"], r["tier"], r["level"])


def dg(r, *path):
    cur = r["diagnostics"]
    for p in path:
        cur = (cur or {}).get(p)
    return cur


# ---------------- (a) 冲突例 ③ -------------------------------------------------
class TestConflictingHintDoesNotPickTheFamily(unittest.TestCase):
    def test_soft_cue_wins_over_conflicting_hint_whisper(self):
        """未引述的 soft + 同一 unit 的 hint whispered ⇒ 不许变 intimate、不许给耳语锚。"""
        r, _ = plan(FROZEN_A, 6)
        self.assertEqual(five(r), ("neutral", A_QUIET, 3.0, "weak", 0.36))
        self.assertEqual(dg(r, "delivery"), "soft")
        self.assertEqual(dg(r, "delivery_tier"), "explicit_surface")
        self.assertNotEqual(r["family"], "intimate", "相冲突的 hint phonation 不许再选族")
        self.assertNotEqual(r["caption"], A_WHISPER, "soft 不许借到耳语锚")
        self.assertIn("style=calm", r["reason"], "选族应回到文本先验，而不是 hint")

    def test_the_same_frozen_input_is_unchanged_on_v5_and_on_v6_all_off(self):
        """版本隔离：同一份输入在 v5（以及四开关全关的 v6）仍是旧结果 —— 冻结输出不动它。"""
        want = ("intimate", A_WHISPER, 4.5, "weak", 0.31)
        r5, _ = plan(FROZEN_A, 5)
        self.assertEqual(five(r5), want)
        self.assertEqual(dg(r5, "delivery"), "soft")
        self.assertIsNone(dg(r5, "delivery_tier"), "v5 不该出现 ① 的诊断键")

    def test_counterfactual_reverting_the_fix_turns_this_case_red(self):
        """反事实：把修复还原（冲突判据恒 False）⇒ 本文件 (a) 的断言必须变红。"""
        with mock.patch.object(F, "phonation_conflict", lambda *a, **k: False):
            r, _ = plan(FROZEN_A, 6)
        self.assertEqual(five(r), ("intimate", A_WHISPER, 4.5, "weak", 0.31))
        self.assertNotEqual(five(r), ("neutral", A_QUIET, 3.0, "weak", 0.36))


# ---------------- (b) 真正的耳语指令不许被误伤 ---------------------------------
class TestRealWhisperDirectiveIsNotHarmed(unittest.TestCase):
    def test_stage_whisper_without_hint_still_whispers(self):
        r, _ = plan({**FROZEN_B_STAGE, "emotion": None}, 6)
        self.assertEqual(five(r), ("neutral", A_WHISPER, 3.0, "weak", 0.45))
        self.assertEqual(dg(r, "delivery"), "whispered")
        self.assertEqual(dg(r, "delivery_tier"), "explicit_surface")

    def test_chinese_whisper_cue_plus_hint_still_reaches_the_whisper_family(self):
        """真耳语指令（cue=whisper）时族照旧由 hint 的 whispered 抬到 intimate —— 修复没动这条。"""
        r, _ = plan(FROZEN_B_CUE, 6)
        self.assertEqual(five(r), ("intimate", A_WHISPER, 4.5, "weak", 0.31))
        self.assertEqual(dg(r, "delivery"), "whispered")
        self.assertEqual(dg(r, "delivery_tier"), "explicit_surface")
        r5, _ = plan(FROZEN_B_CUE, 5)
        self.assertEqual(five(r5), five(r), "真耳语指令 v5/v6 同值（修复没碰它）")

    def test_stage_whisper_survives_a_conflicting_hint(self):
        """句面明确耳语 + hint 说 soft（相冲突）⇒ 仍是耳语锚；只把 hint phonation 从 level 里拿掉。"""
        r, _ = plan({**FROZEN_B_STAGE, "emotion": {"phonation": "soft", "arousal": 0.2,
                                                   "intensity": 0.25}}, 6)
        self.assertEqual(five(r), ("neutral", A_WHISPER, 3.0, "weak", 0.33))
        self.assertEqual(dg(r, "delivery"), "whispered")
        self.assertEqual(dg(r, "gap"), CB.NEUTRAL_WHISPER_GAP, "借的是码本既有的耳语锚")


# ---------------- (c) ④ 的形状：同一 unit 里引述撤销 + 真撤销 -------------------
class TestQuotedRevokeWithRealRevokeInOneUnit(unittest.TestCase):
    def test_unit_containing_both_revokes_is_normal_and_has_no_cross(self):
        """该 unit 恢复正常：没有 delivery、没有 cross、也没有任何 cue（真撤销=桥不给 cue）。"""
        r, _ = plan(FROZEN_C_REVOKE, 6)
        self.assertEqual(five(r), ("neutral", A_QUIET, 3.0, "weak", 0.36))
        self.assertIsNone(dg(r, "delivery"))
        self.assertEqual(dg(r, "cue_scope", "cues"), 0)
        self.assertEqual(dg(r, "cue_scope", "mode"), "sentence")
        self.assertIsNone(dg(r, "cue_scope", "cross"))

    def test_revoke_does_not_let_the_whisper_directive_leak_into_this_unit(self):
        """串上句的 whisper 状态：本 unit 仍不产生 delivery/跨句副本（滞回只影响族与 level）。"""
        r0, state = plan(FROZEN_B_CUE, 6)
        self.assertEqual(five(r0), ("intimate", A_WHISPER, 4.5, "weak", 0.31))
        r, _ = plan(FROZEN_C_REVOKE, 6, prev=state)
        self.assertIsNone(dg(r, "delivery"))
        self.assertIsNone(dg(r, "cue_scope", "cross"))
        self.assertTrue(r["reason"].startswith("inherit(intimate)"), "族按滞回继承（设计行为）")
        self.assertEqual(five(r), ("intimate", A_WHISPER, 4.5, "weak", 0.14))


if __name__ == "__main__":
    unittest.main(verbosity=2)
