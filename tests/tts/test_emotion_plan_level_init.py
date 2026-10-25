# -*- coding: utf-8 -*-
"""回归：**没有上一档可继承**时（本 turn 第一片、或 prev 缺 level），续片也必须给出有限 level。

2026-09-30 生产现场：一轮的第一片被软切（`session_text` 的 soft ⇒ 协调器发 advance=False），
它又是第一个单元（prev=None）⇒ `plan_units_v3` 里 level 一路保持 None ⇒
记录处 `round(float(level), 3)` 抛 TypeError ⇒ `/v2/plan` 400（v4+ 版本隔离不回落 v2）
⇒ **这一片完全没声音**，后半片正常。

修法：算出 rl 后、进分支前 `if level is None: level = rl`（`step_level(None, rl)` 本就返回 rl，
所以 advance=True 的既有路径逐字不变）。

反事实：把那两行去掉，本文件必须变红（已实测）。

跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p "test_emotion_*.py"
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))
sys.path.insert(0, os.path.join(TTS, "stream"))

import scene_plan as SP                                                     # noqa: E402

# 生产 v6 的开关组合（端点 v6 默认全开，见 scene_plan.install 的 v6 分支）
V6 = dict(plan_version=SP.PLAN_VERSION_V6, allow_neutral_whisper=True, weak_delivery=True,
          cue_scope=True, refusal_guard=True, hint_phonation=True, cross_sentence=True,
          ja_stage_cue=True, level_rebase=True)

FIRST = "というわけで、本当にお休みですね——今日から、"        # 现场首片（soft 切出来的续片）
NEXT = "一日中、あなたのものですよ。"                          # 同轮第二片


def plan(units, prev=None):
    return SP.plan_units_v3(units, prev=prev, **V6)


class PlanLevelInit(unittest.TestCase):
    def test_continuation_without_prev_is_finite_and_matches_whole_sentence(self):
        """advance=False + 无 prev：以前 400；现在给有限 level，且与整句（advance=True）起点一致。"""
        rec, _ = plan([{"unit_id": "u0", "text": FIRST, "advance": False}])
        lv = float(rec[0]["level"])
        self.assertTrue(math.isfinite(lv), "level 必须是有限数：%r" % (rec[0]["level"],))
        whole, _ = plan([{"unit_id": "u0", "text": FIRST}])
        self.assertEqual(rec[0]["level"], whole[0]["level"])   # 续片只是不推进，起点该一样

    def test_prev_present_but_level_missing_or_null(self):
        """prev 有对象但没 level / level 是 null —— 同类路径，也必须有限。"""
        for prev in ({}, {"level": None}, {"family": "neutral", "tier": None, "level": None}):
            rec, _ = plan([{"unit_id": "u0", "text": FIRST, "advance": False}], prev=dict(prev))
            self.assertTrue(math.isfinite(float(rec[0]["level"])),
                            "prev=%r ⇒ level=%r" % (prev, rec[0]["level"]))

    def test_first_unit_continuation_then_rest_of_turn(self):
        """一次请求多单元（首个 advance=False）以前会整次失败：现在两片都有 level，state 也有限。"""
        recs, state = plan([{"unit_id": "u0", "text": FIRST, "advance": False},
                            {"unit_id": "u1", "text": NEXT, "advance": True}])
        self.assertEqual(len(recs), 2)
        for r in recs:
            self.assertTrue(math.isfinite(float(r["level"])), r)
        self.assertTrue(math.isfinite(float(state["level"])))

    def test_continuation_does_not_advance_existing_level(self):
        """已有上一档时，续片**原样带走**、不推进（astra-3 §A）—— 本次改动不许动这条。"""
        _, st = plan([{"unit_id": "u0", "text": "（小声で）だめです。"}])
        rec, _ = plan([{"unit_id": "u1", "text": FIRST, "advance": False}], prev=dict(st))
        self.assertEqual(rec[0]["level"], st["level"])
        self.assertEqual(rec[0]["family"], st["family"])

    def test_json_false_only(self):
        """`advance` 的判据是 `is not False`：只有真布尔 false 才算续片（整数 0 不算）。

        证明「身份判断」而不是「恰好有个数」：给 prev 一个与 rl 不同的 level，
        断言 0 与缺省（true）同结果、且与 false 不同。
        """
        text = "今日はいい天気ですね。"
        prev = {"family": "neutral", "tier": "weak", "level": 0.9, "scene_id": 1, "held": 0}
        whole, _ = plan([{"unit_id": "u", "text": text}], prev=dict(prev))
        zero, _ = plan([{"unit_id": "u", "text": text, "advance": 0}], prev=dict(prev))
        part, _ = plan([{"unit_id": "u", "text": text, "advance": False}], prev=dict(prev))
        self.assertEqual(zero[0]["level"], whole[0]["level"])        # 0 = true：照常推进
        self.assertEqual(part[0]["level"], prev["level"])            # false = 续片：原样带走
        self.assertNotEqual(whole[0]["level"], part[0]["level"])     # 两者真有区别（区别力）


if __name__ == "__main__":
    unittest.main(verbosity=2)
