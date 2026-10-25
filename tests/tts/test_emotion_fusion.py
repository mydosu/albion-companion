# -*- coding: utf-8 -*-
"""emotion_fusion / 发声闸门的定向测试（纯 CPU、不联网、不加载模型）。

对应 Astra 补充方案 §第 5 条的测试清单：NaN/Inf/缺失、证据冲突、soft≠intimate、
否定、引号作用域、9 句释放、软切子段不重复计数、失败重试不清状态、v2 字节不变。
跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p "test_emotion_*.py" -v
"""
import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tts"))     # D:/AI/Wife/tts
sys.path.insert(0, os.path.join(ROOT, "irodori"))

import emotion_codebook as CB                                          # noqa: E402
import emotion_fusion as F                                             # noqa: E402
import scene_plan as SP                                                # noqa: E402


def units(texts, **kw):
    out = []
    for i, t in enumerate(texts):
        u = {"unit_id": "u%d" % i, "text": t, "parent_id": i}
        u.update(kw.get("each", {}))
        out.append(u)
    return out


class TestEvidenceChannels(unittest.TestCase):
    def test_missing_and_bad_values_are_not_evidence(self):
        for bad in (None, {}, {"arousal": float("nan")}, {"arousal": float("inf")},
                    {"arasal": 0.9}, {"arousal": True}, {"intensity": 7}):
            u, r = F.llm_support(bad)
            self.assertIsNone(u, "坏提示不该产生证据：%r" % (bad,))
            self.assertEqual(r, 0.0)

    def test_no_evidence_never_switches(self):
        p, Q, _ = F.fuse([], None)
        self.assertIsNone(p)
        d = F.decide(p, Q, prev_family="intimate")
        self.assertFalse(d["switch"])
        self.assertEqual(d["reason"], "no_evidence")

    def test_history_alone_is_not_quality(self):
        p, Q, _ = F.fuse([], {"intimate": 0.95})
        self.assertEqual(Q, 0.0)
        self.assertFalse(F.decide(p, Q, prev_family="neutral")["switch"])

    def test_same_origin_counted_once(self):
        one = [("prosody", {"bright": 0.5}, 0.4)]
        three = one * 3
        self.assertAlmostEqual(F.fuse(one, None)[1], F.fuse(three, None)[1], places=12)

    def test_negation_and_quotes(self):
        u_yes, ph_yes, _ = F.ja_support("（小声で）話す")
        u_no, _, _ = F.ja_support("小声じゃない")
        self.assertEqual(ph_yes, "whispered")
        self.assertGreater(u_yes["intimate"], 0)
        self.assertLess(u_no["intimate"], 0)
        u_q, _, r_q = F.ja_support("彼は「囁いて」と言った")
        _, _, r_plain = F.ja_support("囁いてください")
        self.assertLess(r_q, r_plain)               # 转述：证据弱化

    def test_soft_directive_is_not_intimate_by_itself(self):
        """方案 1.4：明确发声指令不负责判定族 —— 单独一条「（小声）」不该把族切成 intimate。"""
        u, ph, r = F.zh_support([{"kind": "soft"}])
        self.assertEqual(ph, "soft")
        d = F.decide(*F.fuse([("zh", u, r)], None)[:2])
        self.assertFalse(d["switch"] and d["family"] == "intimate")

    def test_conflict_flagged(self):
        # 同轴正负对撞（LLM 说开心、原文提示却在哭）
        u1, r1 = F.llm_support({"arousal": 0.9, "valence": 0.9, "phonation": "voiced"})
        u2, _ph, r2 = F.ja_support("悲しくて泣きそう")
        _, _, conflict = F.fuse([("llm", u1, r1), ("ja", u2, r2)], None)
        self.assertTrue(conflict)
        # 发声方式对撞：原文明说耳语、LLM 报正常发声
        self.assertTrue(F.phonation_conflict("voiced", "whispered", 1.0))
        self.assertFalse(F.phonation_conflict("whispered", "whispered", 1.0))
        self.assertFalse(F.phonation_conflict("voiced", "whispered", 0.2))   # 转述：不算冲突


class TestDeliveryGate(unittest.TestCase):
    """发声闸门：耳语/弱声目标不能被喂粗气息锚。"""

    def test_whisper_target_rejects_coarse_breath_anchor(self):
        c = F.compile_target("intimate", 0.9, None, "whispered")
        self.assertEqual(c["caption"], CB.A_WHISPER)          # 换到同族另一条锚
        self.assertEqual(c["tier"], "weak")

    def test_breathy_target_keeps_breath_anchor(self):
        c = F.compile_target("intimate", 0.9, None, "breathy")
        self.assertEqual(c["caption"], CB.A_BREATHY)

    def test_gap_recorded_when_family_has_no_such_anchor(self):
        c = F.compile_target("neutral", 0.2, None, "whispered")
        self.assertEqual(c["gap"], "no_anchor_for_whispered")

    def test_cascade_records_delivery_and_gap(self):
        recs, _ = SP.plan_units_v3(units(["（小声で）今日は早めに休んでくださいね。"]))
        self.assertEqual(recs[0]["diagnostics"]["delivery"], "whispered")
        self.assertTrue(recs[0]["diagnostics"]["gap"])
        # 明说囁く → 必须是耳语锚，不是粗气息锚
        recs, _ = SP.plan_units_v3(units(["（囁くように）今夜は離さないでください。"]))
        self.assertEqual(recs[0]["caption"], CB.A_WHISPER)


class TestFusionPolicy(unittest.TestCase):
    def test_release_after_eight_sentences(self):
        texts = ["はぁ……んっ……"] + ["はい、承知しました。"] * 9
        recs, state = SP.plan_units_fusion(units(texts))
        self.assertEqual(recs[-1]["family"], "neutral")       # 无新证据够 8 句 → 释放
        self.assertEqual(state["family"], "neutral")
        recs, _ = SP.plan_units_fusion(units(texts[:4]))
        self.assertEqual(recs[-1]["family"], "intimate")      # 4 句之内还在继承

    def test_soft_cut_subsegments_do_not_double_count(self):
        """软切/强切出来的后续子段不该重复推进"无证据"计数（方案 1.4）。"""
        base = units(["はぁ……んっ……"])[0]
        hist = [dict(base, unit_id="a")]
        held_a = SP.plan_units_fusion([dict(base, unit_id="b", text="はい。")],
                                      prev={"family": "intimate", "level": 0.7, "held": 3})[1]["held"]
        held_b = SP.plan_units_fusion([dict(base, unit_id="b", text="はい。", advance=False)],
                                      prev={"family": "intimate", "level": 0.7, "held": 3})[1]["held"]
        self.assertEqual(held_a, 4)          # 新自然句：推进
        self.assertEqual(held_b, 3)          # 同句续片：不推进

    def test_shadow_mode_does_not_change_v2(self):
        """fusion 策略只作用于 v3；v2 输出与码本无关地保持不变。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        r = c.post("/v2/plan", json={"units": units(["はぁ……んっ……"]), "plan_version": 2,
                                     "emotion_policy": "fusion_v1"})
        self.assertEqual(r.json()["plan_version"], 2)
        self.assertNotIn("diagnostics", r.json()["units"][0])
        r3 = c.post("/v2/plan", json={"units": units(["はぁ……んっ……"]), "plan_version": 3,
                                      "emotion_policy": "fusion_v1"})
        self.assertEqual(r3.json()["policy"], "fusion_v1")
        self.assertEqual(r3.json()["units"][0]["diagnostics"]["policy"], "fusion_v1")

    def test_unknown_policy_rejected(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        r = c.post("/v2/plan", json={"units": units(["はい。"]), "plan_version": 3,
                                     "emotion_policy": "magic"})
        self.assertEqual(r.status_code, 400)

    def test_double_threshold_keeps_tier_in_the_middle_band(self):
        """双阈值：升档要 0.58，降档要 <0.42，中间带保持原档（修 0.5 附近的抖动）。"""
        self.assertEqual(CB.tier_for("intimate", 0.55, prev_tier="weak"), "weak")
        self.assertEqual(CB.tier_for("intimate", 0.60, prev_tier="weak"), "strong")
        self.assertEqual(CB.tier_for("intimate", 0.45, prev_tier="strong"), "strong")
        self.assertEqual(CB.tier_for("intimate", 0.40, prev_tier="strong"), "weak")
        self.assertEqual(CB.tier_for("intimate", 0.55), "strong")      # 无历史：旧口径
        self.assertEqual(CB.tier_for("sad", 0.9, prev_tier="weak"), "weak")   # 单锚族不换档

    def test_state_roundtrip_is_sanitized(self):
        recs, state = SP.plan_units_fusion(units(["はい。"]),
                                           prev={"family": "intimate", "p": {"intimate": "x"},
                                                 "tier": "STRONG", "held": -1})
        self.assertEqual(state["family"], "intimate")         # 合法族保留并继承
        self.assertIn(state["tier"], ("weak", "strong"))      # 坏 tier 字符串被丢、重算
        self.assertEqual(state["held"], 1)                    # 坏 held 被丢；继承第 1 句 → 1
        _, state2 = SP.plan_units_fusion(units(["はぁ……んっ……"]),
                                         prev={"family": "intimate", "p": {"intimate": 0.9,
                                                                           "neutral": 0.1}})
        self.assertEqual(state2["family"], "intimate")
        self.assertIsNotNone(state2["p"])


class TestZhSourceCues(unittest.TestCase):
    """中文源线索：只在**原文**里看得见的信息（括号短指令 / emoji），译文里已经没了。"""

    def setUp(self):
        import bridge
        self.b = bridge

    def test_bracket_directive_and_negation(self):
        c = self.b.zh_source_cues("（小声）别让指挥官听到……")
        self.assertEqual(c[0]["kind"], "soft")
        self.assertFalse(c[0]["negated"])
        c = self.b.zh_source_cues("别小声说话，正常点。")
        self.assertEqual(c[0]["kind"], "soft")
        self.assertTrue(c[0]["negated"])                      # 反证，不是正证据

    def test_emoji_and_plain_sentence(self):
        kinds = {c["kind"] for c in self.b.zh_source_cues("太好了！！(≧▽≦)")}
        self.assertIn("excite", kinds)
        self.assertEqual(self.b.zh_source_cues("今天天气不错。"), [])

    def test_cues_ride_into_the_planner(self):
        """桥把线索塞在 emotion["src"] 里 → 规划器要能取到（不再另开一条通道）。"""
        u = {"unit_id": "a", "text": "……", "emotion": {"arousal": 0.3,
                                                        "src": [{"kind": "whisper"}]}}
        recs, _ = SP.plan_units_fusion([u])
        self.assertEqual(recs[0]["diagnostics"]["phonation"], "whispered")
        self.assertEqual(recs[0]["caption"], CB.A_WHISPER)

    def test_negated_cue_is_evidence_against(self):
        u, ph, rel = F.zh_support([{"kind": "whisper", "negated": True}])
        self.assertLess(u["intimate"], 0)
        self.assertIsNone(ph)                                 # 反证不提供发声目标
        self.assertEqual(F.zh_support([{"kind": "soft"}])[0]["intimate"], 0.0)  # 音量指令不判族


class TestSolR2Fixes(unittest.TestCase):
    """Sol 第 2 轮复审的三条阻塞 + 点名"假绿"的回归。每条都要"改回去就变红"。"""

    def test_bad_prev_types_do_not_crash_or_downgrade(self):
        """坏 prev 只能被丢掉 —— 不能抛成"规划失败"把会话静默降级 v2（Sol r2 阻塞③）。

        用**裸 body**：`json=` 编不出 `Infinity`，而 Python 的 json 默认收得下这个字面量，
        所以它确实是一条能到达信任边界的输入（不是想象出来的）。
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        raw = ('{"plan_version":3,"prev":{"family":["intimate"],"level":Infinity,'
               '"scene_id":1000000000,"held":1e999,"tier":["strong"],"p":{"intimate":"x"}},'
               '"units":[{"unit_id":"u","text":"はい。"}]}')
        r = c.post("/v2/plan", content=raw.encode(), headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan_version"], 3)          # 仍是 v3，没被降级
        self.assertIsNone(r.json().get("fallback"))
        self.assertEqual(r.json()["units"][0]["family"], "neutral")   # 坏状态一律没继承

    def test_bad_emotion_type_is_ignored(self):
        recs, _ = SP.plan_units_fusion([{"unit_id": "a", "text": "はい。", "emotion": "小声"}])
        self.assertEqual(recs[0]["family"], "neutral")

    def test_cue_survives_through_prepare(self):
        """Sol r2 阻塞①：`speech_text` 已把旁白剥掉，线索必须从 `raw_text` 抽。"""
        import bridge
        import bridge_v2
        svc = bridge_v2.PrepareService(translate=lambda s: s, cues=bridge.zh_source_cues)
        mk = lambda **kw: bridge_v2.PrepareRequest(          # noqa: E731
            session_id="s", generation=1, unit_id="u", voice="albion",
            source_range=(0, 20), **kw)
        with_raw = svc._ready(mk(speech_text="请慢一点。", raw_text="（小声）请慢一点。"),
                              ["ゆっくりでいいですよ。"], 0.0, translated=True)
        self.assertEqual(with_raw.ja_segments[0]["emotion"]["src"][0]["kind"], "soft")
        without_raw = svc._ready(mk(speech_text="请慢一点。"), ["ゆっくりでいいですよ。"],
                                 0.0, translated=True)
        self.assertNotIn("emotion", without_raw.ja_segments[0])   # 只看 speech_text 就是看不到

    def test_coordinator_sends_the_raw_slice(self):
        """接线守卫：协调器必须把原始切片一并送桥（去掉这行 → 闸门又收不到线索）。"""
        import inspect
        import stream_server
        self.assertIn("raw_text", inspect.getsource(stream_server.Coordinator.prepare))

    def test_soft_cue_cannot_switch_family_on_the_production_path(self):
        """Sol r2 点名的假绿：用生产 `quality_bar` 而不是默认 Q_MIN 走完整路径。"""
        recs, _ = SP.plan_units_fusion([{"unit_id": "a", "text": "はい。",
                                         "emotion": {"src": [{"kind": "soft"}]}}])
        self.assertNotEqual(recs[0]["family"], "intimate")
        self.assertEqual(recs[0]["diagnostics"]["phonation"], "soft")   # 发声目标仍然生效

    def test_negated_cue_reaches_fusion(self):
        """Sol r2：`_MIN_SUPPORT` 按 max() 判会**把纯反证路整条丢掉**（全是负值时 max=0），应按 max(abs())。"""
        u, _ph, r = F.zh_support([{"kind": "whisper", "negated": True}])
        self.assertLess(u["intimate"], -0.2)                       # 确实是一条反证
        self.assertIn("zh", F.effective_origins([("zh", u, r)]))   # 按 max(abs) 才留得下来

    def test_cascade_held_only_advances_on_complete_sentences(self):
        """软切/强切续片不该重复推进"无信号"计数（Sol r2 建议改）。"""
        prev = {"family": "intimate", "level": 0.7, "held": 3}
        full, _ = SP.plan_units_v3([{"unit_id": "a", "text": "はい。"}], prev=dict(prev))
        part, _ = SP.plan_units_v3([{"unit_id": "b", "text": "はい。", "advance": False}],
                                   prev=dict(prev))
        self.assertEqual(full[0]["reason"].split(")")[1][0], "4")
        self.assertEqual(part[0]["reason"].split(")")[1][0], "3")

    def test_approximate_phonation_is_reported(self):
        """breathy 目标拿到耳语锚：不做更换，但必须记缺口（不能留着没人读的代价）。"""
        c = F.compile_target("intimate", 0.2, None, "breathy")
        self.assertEqual(c["caption"], CB.A_WHISPER)
        self.assertEqual(c["gap"], "phonation_approximate_breathy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
