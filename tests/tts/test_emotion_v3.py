# -*- coding: utf-8 -*-
"""v3 情绪条件化：契约与回归测试（纯 CPU、不联网、不加载模型）。

跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p test_emotion_*.py -v

覆盖三件事：
  ① v2 旧路径**逐字节没变**（黄金哈希；这是"可回滚"的前提）；
  ② v3 的滞回/档位/白名单语义；
  ③ **区分力**：v3 必须真的比 v2 分得细（否则测试自己在骗自己 —— 见技能里的教训）。
"""
import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tts"))     # D:/AI/Wife/tts
sys.path.insert(0, os.path.join(ROOT, "irodori"))
sys.path.insert(0, os.path.join(ROOT, "stream"))

import emotion_codebook as CB                                          # noqa: E402
import scene_plan as SP                                                # noqa: E402
import render_queue as RQ                                              # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v2_golden.json")
PERSONA = os.path.join(ROOT, "persona")


def persona(name):
    with open(os.path.join(PERSONA, name), encoding="utf-8-sig") as f:
        return f.read().strip()


def units_of(text, limit=None):
    segs, _ = SP._segments(text)
    if limit:
        segs = segs[:limit]
    return [{"text": s, "unit_id": "u%d" % i, "parent_id": i, "complete": True}
            for i, s in enumerate(segs)]


def run_v3(text, limit=None):
    """逐句喂给 v3（模拟流式：一次一句 + 把 state 带回去）。"""
    prev, recs = None, []
    for u in units_of(text, limit):
        r, prev = SP.plan_units_v3([u], prev=prev)
        recs.append(r[0])
    return recs


class TestV2Regression(unittest.TestCase):
    """v2 是回滚目标：一个字节都不许变。"""

    def test_document_path_unchanged(self):
        with open(GOLDEN, encoding="utf-8") as f:
            golden = json.load(f)
        import hashlib
        for name, want in golden.items():
            if name == "units_v2":
                continue
            got = hashlib.md5(json.dumps(SP.plan_document(persona(name)), ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()
            self.assertEqual(got, want, "%s 的 v2 整篇规划变了（回滚目标被动过）" % name)

    def test_units_path_unchanged(self):
        import hashlib
        with open(GOLDEN, encoding="utf-8") as f:
            want = json.load(f)["units_v2"]
        got = hashlib.md5(json.dumps(SP.plan_units(units_of(persona("阿尔比恩.txt"), 12)),
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(got, want)


class TestCodebook(unittest.TestCase):
    def test_only_trained_captions(self):
        pairs = CB.allowed_pairs()
        for fam, cap in pairs:
            self.assertIn(cap, CB.TRAINED, "码本里混进了训练集外的描述")
            self.assertIn(fam, SP.FAMILIES)
        self.assertEqual(len(CB.TRAINED), 7)

    def test_cfg_is_family_value(self):
        for fam in CB.CFG:
            self.assertEqual(CB.compile_condition(fam, 0.0)["cfg"], CB.CFG[fam])
            self.assertEqual(CB.compile_condition(fam, 1.0)["cfg"], CB.CFG[fam])

    def test_tier_threshold(self):
        self.assertEqual(CB.compile_condition("intimate", 0.49)["tier"], "weak")
        self.assertEqual(CB.compile_condition("intimate", 0.51)["tier"], "strong")
        self.assertEqual(CB.compile_condition("intimate", 0.51)["caption"], CB.A_BREATHY)


class TestHysteresis(unittest.TestCase):
    def test_strong_signal_switches_at_once(self):
        recs, _ = SP.plan_units_v3([{"text": "はぁ……んっ……", "unit_id": "a"}])
        self.assertEqual(recs[0]["family"], "intimate")

    def test_no_signal_inherits_and_decays(self):
        recs, state = SP.plan_units_v3([{"text": "はぁ……んっ……", "unit_id": "a"}])
        self.assertEqual(state["tier"], "strong")
        lv0 = state["level"]
        # 后面接三句没信号的日常句：族应继承，强度逐句下降，最终落回弱档
        nxt = [{"text": "はい、わかりました。", "unit_id": "b"},
               {"text": "今日はいい天気ですね。", "unit_id": "c"},
               {"text": "そうですか。", "unit_id": "d"}]
        out, st = SP.plan_units_v3(nxt, prev=state)
        self.assertTrue(all(r["family"] == "intimate" for r in out), "没信号时应继承上一档")
        self.assertTrue(all(out[i]["level"] < out[i - 1]["level"] for i in range(1, len(out))),
                        "强度应逐句衰减")
        self.assertLess(out[-1]["level"], lv0)
        self.assertEqual(out[-1]["tier"], "weak", "衰减几句后应回到弱档（语气回落）")

    def test_step_is_bounded(self):
        # 单句最多走 0.34：从 0 开始，一句"最激动"的文本不该直接冲到 1.0
        recs, _ = SP.plan_units_v3([{"text": "はぁ！？んっ、あっ、だめっ！？", "unit_id": "a"}],
                                   prev={"family": "intimate", "level": 0.0})
        self.assertLessEqual(recs[0]["level"], 0.34 + 1e-9)

    def test_single_anchor_family_never_changes_caption(self):
        caps = {CB.compile_condition("sad", lv)["caption"] for lv in (0.0, 0.5, 1.0)}
        self.assertEqual(len(caps), 1)

    def test_override(self):
        recs, _ = SP.plan_units_v3([{"text": "そうですか。", "unit_id": "a"}], style_override="moan")
        self.assertEqual(recs[0]["family"], "intimate")
        self.assertEqual(recs[0]["caption"], CB.A_BREATHY)

    def test_hint_moves_level(self):
        lo, _ = SP.plan_units_v3([{"text": "はい。", "unit_id": "a"}])
        hi, _ = SP.plan_units_v3([{"text": "はい。", "unit_id": "a",
                                   "emotion": {"intensity": 0.95}}])
        self.assertGreater(hi[0]["level"], lo[0]["level"])

    def test_scene_id_follows_family(self):
        recs, _ = SP.plan_units_v3([{"text": "はぁ……んっ……", "unit_id": "a"},
                                    {"text": "はぁ……んっ……", "unit_id": "b"},
                                    {"text": "ええっ！？まさか！", "unit_id": "c"}])
        self.assertEqual(recs[0]["scene_id"], recs[1]["scene_id"], "同族同场景")
        self.assertNotEqual(recs[1]["scene_id"], recs[2]["scene_id"], "换族应开新场景")


class TestRenderWhitelist(unittest.TestCase):
    def _unit(self, cap, cfg, fam="intimate"):
        return {"unit_id": "u", "text": "はぁ…", "caption": cap, "cfg": cfg,
                "family": fam, "scene_id": 1}

    def test_v3_accepts_codebook(self):
        for lv in (0.0, 1.0):
            c = CB.compile_condition("intimate", lv)
            self.assertIsNone(RQ._check_unit(self._unit(c["caption"], c["cfg"]), 0, 3))

    def test_v3_rejects_untrained_caption(self):
        bad = self._unit("甘く誘うように、色っぽく囁いて", 4.5)
        self.assertIsNotNone(RQ._check_unit(bad, 0, 3), "训练集外的 caption 必须被拒")

    def test_v3_rejects_odd_cfg(self):
        c = CB.compile_condition("intimate", 1.0)
        self.assertIsNotNone(RQ._check_unit(self._unit(c["caption"], 4.7), 0, 3))

    def test_v2_still_strict(self):
        c = CB.compile_condition("intimate", 0.0)          # v3 的弱档 anchor
        self.assertIsNotNone(RQ._check_unit(self._unit(c["caption"], c["cfg"]), 0, 2),
                             "v2 合同必须仍然只认 FAMILIES")
        cap, cfg = SP.FAMILIES["intimate"]
        self.assertIsNone(RQ._check_unit(self._unit(cap, cfg), 0, 2))


class TestDiscriminatingPower(unittest.TestCase):
    """**必须有区分力**：v3 若退回"整段一句 caption"，这些断言要变红。"""

    def test_v3_is_finer_than_v2(self):
        text = persona("阿尔比恩.txt")
        us = units_of(text)
        v2 = SP.plan_units(us)
        v3 = run_v3(text)
        self.assertEqual(len({r["caption"] for r in v2}), 1, "v2 基线本来就该是整段一句")
        self.assertGreaterEqual(len({r["caption"] for r in v3}), 3,
                                "v3 应至少分出 3 档语气（弱/强 + 跨族）")

    def test_all_four_ships_get_variation(self):
        for name in ("阿尔比恩.txt", "大凤.txt", "布雷斯特.txt", "金狮.txt"):
            v3 = run_v3(persona(name))
            self.assertGreaterEqual(len({r["caption"] for r in v3}), 3, name)
            self.assertGreaterEqual(len({r["family"] for r in v3}), 2, name)


class TestHintWiring(unittest.TestCase):
    """识别结果必须真的改变发声：提示 → 族 → caption。"""

    def test_family_from_hint(self):
        self.assertEqual(CB.family_from_hint({"phonation": "whispered", "arousal": 0.3}), "intimate")
        self.assertEqual(CB.family_from_hint({"phonation": "breathy", "intensity": 0.4}), "intimate")
        self.assertEqual(CB.family_from_hint({"arousal": 0.9, "valence": 0.8}), "bright")
        self.assertEqual(CB.family_from_hint({"arousal": 0.3, "valence": -0.5}), "sad")
        self.assertIsNone(CB.family_from_hint(None))
        self.assertIsNone(CB.family_from_hint({}))

    def test_hint_switches_family_when_rules_have_nothing(self):
        # 「はい。」关键词没信号；提示说是耳语 ⇒ 必须走 whisper 锚，而不是平静档
        recs, _ = SP.plan_units_v3([{"text": "はい。", "unit_id": "a",
                                     "emotion": {"phonation": "whispered", "arousal": 0.2,
                                                 "intensity": 0.25}}])
        self.assertEqual(recs[0]["family"], "intimate")
        self.assertEqual(recs[0]["caption"], CB.A_WHISPER)

    def test_hint_raises_family_for_excited(self):
        recs, _ = SP.plan_units_v3([{"text": "そうですか。", "unit_id": "a",
                                     "emotion": {"arousal": 0.85, "intensity": 0.7,
                                                 "valence": 0.9}}])
        self.assertEqual(recs[0]["family"], "bright")
        self.assertEqual(recs[0]["caption"], CB.A_STRONG)

    def test_rules_win_on_explicit_onomatopoeia(self):
        # 文本本身就是喘息，提示却说"正常发声低强度" —— 关键词表更可信，别被压平
        recs, _ = SP.plan_units_v3([{"text": "はぁ……んっ……", "unit_id": "a",
                                     "emotion": {"phonation": "voiced", "arousal": 0.4,
                                                 "intensity": 0.4}}])
        self.assertEqual(recs[0]["family"], "intimate")


class TestPlanEndpointV3(unittest.TestCase):
    """/v2/plan 的 v3 分派（直接调 ASGI，不起服务）。"""

    def test_endpoint_returns_state(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        r = c.post("/v2/plan", json={"units": [{"unit_id": "u", "text": "はぁ……んっ……"}],
                                     "plan_version": 3})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["plan_version"], 3)
        self.assertIn("state", body)
        self.assertEqual(body["units"][0]["caption"], CB.A_BREATHY)
        # v2 请求仍然拿旧计划（没有 state）
        r2 = c.post("/v2/plan", json={"units": [{"unit_id": "u", "text": "はぁ……んっ……"}]})
        self.assertEqual(r2.json()["plan_version"], 2)
        self.assertNotIn("state", r2.json())


class TestReviewFixes(unittest.TestCase):
    """复核（GPT-6-Sol / medium）指出问题的定向回归 —— 每条都对应一个真实可复现的坏行为。"""

    def test_v3_cfg_must_match_family(self):
        """复核问题 1：跨族 cfg 曾是合法输入（family=intimate + whisper caption + cfg=3.0）。"""
        good = {"unit_id": "u", "text": "はい。", "family": "intimate",
                "caption": CB.A_WHISPER, "cfg": 4.5, "scene_id": 1}
        self.assertIsNone(RQ._check_unit(good, 0, 3))
        for bad_cfg in (3.0, 3.2, 3.5, 4.0, 4.7):
            u = dict(good, cfg=bad_cfg)
            self.assertIsNotNone(RQ._check_unit(u, 0, 3),
                                 "family=intimate 配 cfg=%s 必须被拒" % bad_cfg)
        # 码本外的 caption 依旧被拒
        self.assertIsNotNone(RQ._check_unit(dict(good, caption="とろけた声で"), 0, 3))

    def test_hysteresis_releases_after_hold_max(self):
        """复核问题 3：一句亲密不该把后面几十句都粘住。"""
        def tail(n):
            units = [{"unit_id": "u0", "text": "はぁ……んっ……"}]           # 明确信号：intimate
            units += [{"unit_id": "u%d" % (i + 1), "text": "はい、承知しました。"} for i in range(n)]
            recs, state = SP.plan_units_v3(units)
            return recs[-1]["family"], state
        self.assertEqual(tail(3)[0], "intimate")          # 3 句之内：继承还在
        fam, state = tail(9)
        self.assertEqual(fam, "neutral")                  # 超过 _HOLD_MAX：释放
        self.assertIn("held", state)

    def test_prev_is_sanitized(self):
        """复核问题 6：prev 来自请求体（信任边界），坏类型不能一路抛下去。"""
        recs, state = SP.plan_units_v3([{"unit_id": "u", "text": "はい。"}],
                                       prev={"family": "../etc", "level": "x",
                                             "scene_id": "9", "held": -5})
        self.assertEqual(state["family"], "neutral")      # 未知族被丢，不是被信任
        self.assertEqual(state["scene_id"], 1)             # 坏类型被丢 → 回到默认场景号
        recs2, state2 = SP.plan_units_v3([{"unit_id": "u", "text": "はい。"}],
                                         prev={"family": "intimate", "level": 5.0,
                                               "scene_id": 2, "held": 3})
        self.assertEqual(state2["family"], "intimate")    # 合法族保留
        self.assertLessEqual(state2["level"], 1.0)        # 越界强度被夹住
        self.assertEqual(state2["held"], 4)               # 继承计数继续

    def test_endpoint_rejects_unknown_plan_version(self):
        """复核问题 6：plan_version 只认明确支持的版本 —— 现在是 2/3/4/5（5 见 astra 第 11 轮 ②，
        6 见 astra 第 12 轮）。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        for bad in (1, 7, 99, "v3"):
            r = c.post("/v2/plan", json={"units": [{"unit_id": "u", "text": "はい。"}],
                                         "plan_version": bad})
            self.assertNotEqual(r.status_code, 200, "plan_version=%r 不该被接受" % (bad,))
        for good in (2, 3, 4, 5, 6):
            r = c.post("/v2/plan", json={"units": [{"unit_id": "u", "text": "はい。"}],
                                         "plan_version": good})
            self.assertEqual(r.status_code, 200, "plan_version=%r 应当被接受" % (good,))
            self.assertEqual(r.json()["plan_version"], good)

    def test_emotion_json_parsing_robustness(self):
        """复核问题 5：多块 JSON / 尾巴 / 正文里的花括号 / 截断，都要扛住。"""
        import bridge as B
        self.assertEqual(B._parse_emotion_json('{"ja":"あ。","arousal":0.8}')[0], "あ。")
        self.assertEqual(B._parse_emotion_json('{"ja":"い。"} 顺带说一句')[0], "い。")
        ja, emo = B._parse_emotion_json('{!} 前言 {"ja":"う。","valence":-0.5} 尾巴 {"x":1}')
        self.assertEqual(ja, "う。")
        self.assertEqual(emo, {"valence": -0.5})
        # 结构截断了：正文里的花括号不该把 ja 吃掉，也不该白跑一次翻译
        ja, emo = B._parse_emotion_json('{"ja":"かっこ { } 入り","arousal":0.5')
        self.assertEqual(ja, "かっこ { } 入り")
        self.assertIsNone(emo)
        self.assertEqual(B._parse_emotion_json("完全没有 JSON"), ("", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
