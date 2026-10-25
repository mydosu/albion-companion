# -*- coding: utf-8 -*-
"""模型真实 token 预算（astra-3 §1.1/§2.3.4）。

指挥官 2026-09-29 收窄口径：**只保流式那条腿**（整段不做）。这里只锁两件事：
  * 255 这个常量锚在真 checkpoint 上（不是背下来的数）；
  * 超预算文本必须在句界/顿号处切开、硬切标出来 —— **不许 tokenizer 静默截断**。
"""
import json
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))

import emotion_codebook as CB    # noqa: E402
import render_queue as RQ        # noqa: E402
import scene_plan as SP          # noqa: E402

CKPT = os.path.join(TTS, "irodori", "checkpoints", "model.safetensors")


def _ckpt_cfg():
    """只读 checkpoint 的 8 字节长度字段 + JSON 头（3GB 权重一个都不载入）。"""
    with open(CKPT, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
    return json.loads(header["__metadata__"]["config_json"])


class TestModelBudget(unittest.TestCase):
    def test_token_limit_matches_checkpoint(self):
        """255 = checkpoint 的 max_text_len(256) 减掉 BOS —— 锚在真文件上，不是背下来的数。"""
        if not os.path.exists(CKPT):
            self.skipTest("本机没有 checkpoint，跳过")
        cfg = _ckpt_cfg()
        self.assertEqual(cfg["max_text_len"], 256)
        self.assertTrue(cfg.get("text_add_bos"))
        self.assertEqual(SP.TOKEN_LIMIT, cfg["max_text_len"] - 1)


class TestBudgetSplit(unittest.TestCase):
    def test_pieces_fit_and_text_is_lossless(self):
        text = "あ" * 7 + "。" + "い" * 9 + "、" + "う" * 5 + "。"
        pieces = SP.split_within_budget(text, len, limit=10)
        self.assertTrue(pieces)
        self.assertTrue(all(len(p) <= 10 for p, _ in pieces))
        self.assertEqual("".join(p for p, _ in pieces), text)      # 逐字等于原文
        self.assertFalse(any(hard for _, hard in pieces))          # 句界够用 ⇒ 不该硬切

    def test_hard_cut_is_flagged(self):
        text = "あ" * 25                                                # 没有任何句界
        pieces = SP.split_within_budget(text, len, limit=10)
        self.assertEqual("".join(p for p, _ in pieces), text)
        self.assertTrue(all(hard for _, hard in pieces))
        self.assertTrue(all(len(p) <= 10 for p, _ in pieces))

    def test_real_token_counter_keeps_document_under_budget(self):
        """用真 tokenizer 数：超预算长材料切开后每片都在 255 内（暴露"按字符估"的错误）。"""
        if not os.path.exists(CKPT):
            self.skipTest("本机没有 checkpoint，跳过")
        os.environ.update(CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        from irodori_tts.tokenizer import PretrainedTextTokenizer
        from irodori_tts.text_normalization import normalize_text
        cfg = _ckpt_cfg()
        root = os.path.dirname(CKPT)
        tokdir = next(p for p in (os.path.join(root, "tokenizer"),
                                  os.path.join(os.path.dirname(root), "tokenizer"))
                      if os.path.exists(os.path.join(p, "tokenizer_config.json")))
        tok = PretrainedTextTokenizer.from_pretrained(
            tokdir, add_bos=bool(cfg.get("text_add_bos")), local_files_only=True)

        def count(t):
            ids, mask = tok.batch_encode([normalize_text(t).strip()],
                                         max_length=10 ** 6)     # 不截断，数真实长度
            return int(mask[0].sum())

        text = ("アルビオンは静かにうなずいた。その声音はとても柔らかく、"
                "ほとんど息に溶けてしまうようで、彼女は少しだけ照れくさそうに微笑んだ。") * 10
        pieces = SP.split_within_budget(text, count)
        self.assertTrue(all(count(p) <= SP.TOKEN_LIMIT for p, _ in pieces), "有片段超预算")
        self.assertEqual("".join(p for p, _ in pieces), text)
        self.assertGreater(count(text), SP.TOKEN_LIMIT)             # 材料本身确实超预算


class TestRenderQueueTokenGuard(unittest.TestCase):
    """渲染队列的 token 守卫（astra-3 §2.3.4）：超预算显式失败，计数失败不拦合成。"""

    UNIT = {"unit_id": "u1", "text": "はい。", "caption": CB.A_NEUTRAL,
            "family": "neutral", "cfg": 3.0}

    def tearDown(self):
        RQ.TOKEN_COUNT = None

    def test_over_budget_unit_is_rejected_explicitly(self):
        self.assertIsNone(RQ._check_unit(dict(self.UNIT), 0, plan_version=3))
        RQ.TOKEN_COUNT = lambda t: SP.TOKEN_LIMIT + 1          # 拔掉守卫就变红
        err = RQ._check_unit(dict(self.UNIT), 0, plan_version=3)
        self.assertIn("超 token 预算", err or "")

    def test_counter_failure_is_rejected(self):
        """计数器报错曾被放过 —— 那正是静默右截断的入口（本机实测 200 字符可达 801 token）。
        现在必须显式拒绝（Sol 第 6 轮第二反例）。"""
        def boom(t):
            raise RuntimeError("tokenizer 炸了")
        RQ.TOKEN_COUNT = boom
        err = RQ._check_unit(dict(self.UNIT), 0, plan_version=3)
        self.assertIn("token 计数失败", err or "")


class TestSeedPassthrough(unittest.TestCase):
    """配对随机种子（astra-3 §4.1）：可选、要校验、要真传到合成腿 —— 只是透传，不改生产默认。"""

    def _unit(self, **extra):
        u = {"unit_id": "u", "text": "はい。", "caption": CB.A_NEUTRAL,
             "family": "neutral", "cfg": 3.0}
        u.update(extra)
        return u

    def test_bad_seed_rejected(self):
        for bad in (True, 1.5, "7", -1, 2 ** 31):
            self.assertIn("seed 非法", RQ._check_unit(self._unit(seed=bad), 0, plan_version=3) or "")

    def test_good_seed_accepted(self):
        self.assertIsNone(RQ._check_unit(self._unit(seed=12345), 0, plan_version=3))

    def test_run_infer_synth_passes_seed_through(self):
        import tempfile
        import numpy as np
        import soundfile as sf
        seen = {}

        def fake(text, caption, cfg, out_wav, ship, seed=None):
            seen["seed"] = seed
            sf.write(out_wav, np.zeros(160, dtype="float32"), 16000)

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.wav")
            sr, _ = RQ.run_infer_synth(self._unit(seed=99), p, fake, "albion")
            self.assertEqual(seen["seed"], 99)
            self.assertEqual(sr, 16000)
            RQ.run_infer_synth(self._unit(), p, fake, "albion")     # 不带 seed ⇒ None（生产行为）
            self.assertIsNone(seen["seed"])


class TestPlanV4NeutralWhisper(unittest.TestCase):
    """astra-3 §5.1 + §3：neutral 说耳语时借既有耳语锚，**且只在 v4/codebook v2 生效**。

    改回去（把新组合塞进 v3 的 allowed_pairs、或让 render 不按版本选白名单）必须变红。
    """

    UNIT = {"unit_id": "u", "text": "はい、わかりました。", "advance": True,
            "src_cues": [{"kind": "whisper", "negated": False, "span": [0, 4]}]}

    def _rec(self, plan_version):
        recs, _ = SP.plan_units_v3([dict(self.UNIT)], plan_version=plan_version,
                                   allow_neutral_whisper=plan_version >= SP.PLAN_VERSION_V4)
        self.assertEqual(recs[0]["family"], "neutral")          # 前提：这句本身是 neutral
        return recs[0]

    def test_v3_keeps_old_behaviour(self):
        r = self._rec(3)
        self.assertEqual(r["plan_version"], 3)
        self.assertEqual(r["diagnostics"]["policy"], "cascade")
        self.assertNotEqual(r["caption"], CB.A_WHISPER)
        self.assertEqual(r["diagnostics"]["gap"], "no_anchor_for_whispered")

    def test_v4_borrows_existing_whisper_anchor(self):
        r = self._rec(4)
        self.assertEqual(r["plan_version"], 4)
        self.assertEqual(r["diagnostics"]["policy"], "cascade_v2")
        self.assertEqual(r["caption"], CB.A_WHISPER)            # 借既有耳语锚，族仍是 neutral
        self.assertEqual(r["cfg"], CB.CFG["neutral"])           # cfg 用本族值
        self.assertEqual(r["diagnostics"]["gap"], CB.NEUTRAL_WHISPER_GAP)   # 候选 ≠ 已验证

    def test_render_whitelist_is_version_exact(self):
        u = {"unit_id": "u", "text": "はい。", "caption": CB.A_WHISPER,
             "family": "neutral", "cfg": CB.CFG["neutral"]}
        self.assertIn("码本外", RQ._check_unit(dict(u), 0, plan_version=3) or "")
        v4 = dict(u, codebook_version=CB.CODEBOOK_VERSION_V2)
        self.assertIsNone(RQ._check_unit(v4, 0, plan_version=4))
        bad = dict(u, codebook_version=CB.CODEBOOK_VERSION)      # 版本号与计划不符
        self.assertIn("不匹配", RQ._check_unit(bad, 0, plan_version=4) or "")

    def test_v4_render_requires_codebook_version(self):
        """复核反例：v4 缺 `codebook_version` 时无从核对，不能默默放行。"""
        u = {"unit_id": "u", "text": "はい。", "caption": CB.A_NEUTRAL,
             "family": "neutral", "cfg": CB.CFG["neutral"]}
        self.assertIn("codebook_version", RQ._check_unit(dict(u), 0, plan_version=4) or "")
        self.assertIsNone(RQ._check_unit(dict(u), 0, plan_version=3))   # 旧版本不追加新字段

    def test_continuation_does_not_step_level_again(self):
        """astra-3 §A 复核反例：`advance=False` 的续片继承强度，不第二次推进。

        `step_level` 有 0.34 的步长上限，所以**同一个 hint** 也能被推两次（0.54 → 0.88）——
        这就是"同一自然句推进三次"的来源。
        """
        hi = {"intensity": 0.9, "arousal": 0.6, "valence": 0.6}
        units = [{"unit_id": "a", "text": "今日は少し寒いですね。", "advance": True},
                 {"unit_id": "b", "text": "わあ、本当ですか！？", "advance": True, "emotion": dict(hi)},
                 {"unit_id": "c", "text": "続きの言葉です。", "advance": False, "emotion": dict(hi)}]
        recs, _ = SP.plan_units_v3(units, plan_version=SP.PLAN_VERSION_V3)
        self.assertNotEqual(recs[1]["level"], recs[0]["level"])   # 整句确实推进过一次
        self.assertEqual(recs[2]["level"], recs[1]["level"])      # 续片继承，不第二次推进

    def test_whispered_hint_beats_generic_arousal(self):
        """耳语+高唤醒 ⇒ intimate（不是 bright）—— 亲密场景的典型特征。"""
        recs, _ = SP.plan_units_v3([{"unit_id": "u", "text": "はい。", "advance": True,
                                     "emotion": {"phonation": "whispered", "arousal": 0.9,
                                                 "valence": -0.2}}],
                                   plan_version=SP.PLAN_VERSION_V4, allow_neutral_whisper=True)
        self.assertEqual(recs[0]["family"], "intimate")
        # 档位仍由既有梯子按 level 决定（这里落 weak ⇒ 耳语锚），本改动不碰它

    def test_mapping_rule_is_v4_only(self):
        """映射改动只对 v4 生效：v3 的规划结果逐字节不变（Sol 第 6 轮第一反例）。"""
        recs, _ = SP.plan_units_v3([{"unit_id": "u", "text": "はい。", "advance": True,
                                     "emotion": {"phonation": "whispered", "arousal": 0.9,
                                                 "valence": -0.2}}],
                                   plan_version=SP.PLAN_VERSION_V3)
        self.assertEqual(recs[0]["family"], "bright")      # 旧行为

    def test_abstained_phonation_changes_nothing(self):
        """闸门弃权时，加不加 phonation 必须产出**完全一样**的记录（Sol 第 8 轮第一条：
        `はい。` 加 breathy 被弃权后仍把 level 0.45→0.60、锚从 quiet 换成 neutral）。"""
        base, _ = SP.plan_units_v3([{"unit_id": "u", "text": "はい。", "advance": True}],
                                   plan_version=SP.PLAN_VERSION_V4,
                                   allow_neutral_whisper=True, weak_delivery=True)
        for ph in ("whispered", "breathy"):
            recs, _ = SP.plan_units_v3([{"unit_id": "u", "text": "はい。", "advance": True,
                                         "emotion": {"phonation": ph}}],
                                       plan_version=SP.PLAN_VERSION_V4,
                                       allow_neutral_whisper=True, weak_delivery=True)
            r = recs[0]
            self.assertIsNone((r.get("diagnostics") or {}).get("delivery"),
                              "%s：闸门没弃权，这条会空转" % ph)
            for k in ("family", "caption", "cfg", "tier", "level"):
                self.assertEqual(r[k], base[0][k], "%s 被弃权后仍改了 %s" % (ph, k))

    def test_abstained_phonation_cannot_pick_family(self):
        """弃权的 phonation 不得让最终 caption 变亲密/耳语。判定只看 diagnostics 里的 delivery
        （记录里没有顶层 delivery，Sol 第 8 轮第三条）。"""
        abstained = 0
        for text in ("小声じゃないよ。", "囁くな。", "「小声で」と言われた。"):
            recs, _ = SP.plan_units_v3([{"unit_id": "u", "text": text, "advance": True,
                                         "emotion": {"phonation": "whispered"}}],
                                       plan_version=SP.PLAN_VERSION_V4,
                                       allow_neutral_whisper=True, weak_delivery=True)
            r = recs[0]
            d = (r.get("diagnostics") or {}).get("delivery")
            self.assertIsNone(d, "%s：闸门没弃权" % text)
            abstained += 1
            self.assertNotEqual(r["caption"], CB.A_WHISPER, "%s：弃权了却仍选到耳语锚" % text)
            self.assertNotEqual(r["family"], "intimate", text)
        self.assertTrue(abstained, "反例材料没触发弃权，这条断言会变成空转")

    def test_plain_arousal_still_bright(self):
        recs, _ = SP.plan_units_v3([{"unit_id": "u", "text": "はい。", "advance": True,
                                     "emotion": {"arousal": 0.9, "valence": 0.5}}],
                                   plan_version=SP.PLAN_VERSION_V4, allow_neutral_whisper=True)
        self.assertEqual(recs[0]["family"], "bright")          # 没有发声证据时行为不变

    def test_plan_endpoint_version_dispatch(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        units = [{"unit_id": "u", "text": "はい。"}]
        r4 = c.post("/v2/plan", json={"plan_version": 4, "units": units})
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.json()["plan_version"], 4)
        self.assertEqual(r4.json()["codebook_version"], CB.CODEBOOK_VERSION_V2)
        r3 = c.post("/v2/plan", json={"plan_version": 3, "units": units})
        self.assertNotIn("codebook_version", r3.json())          # 老响应不追加新字段
        # v5（astra 第 11 轮 ②）= v4 + 拒绝类语义闸门：白名单要认它，6 仍然不认
        r5 = c.post("/v2/plan", json={"plan_version": 5, "units": units})
        self.assertEqual(r5.status_code, 200)
        self.assertEqual(r5.json()["plan_version"], 5)
        # v6（astra 第 12 轮）= v5 + 四缺口：白名单要认它，7 仍然不认
        r6 = c.post("/v2/plan", json={"plan_version": 6, "units": units})
        self.assertEqual(r6.status_code, 200)
        self.assertEqual(r6.json()["plan_version"], 6)
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 7,
                                                  "units": units}).status_code, 400)
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 4, "units": units,
                                                  "emotion_policy": "fusion_v1"}).status_code, 400)


    def test_plan_endpoint_rejects_mismatched_codebook(self):
        """请求显式报的码本号必须与 plan_version 一致（Sol 第 6 轮第五反例）。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        units = [{"unit_id": "u", "text": "はい。"}]
        ok = c.post("/v2/plan", json={"plan_version": 4, "units": units,
                                      "codebook_version": CB.CODEBOOK_VERSION_V2})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 4, "units": units,
                                                  "codebook_version": CB.CODEBOOK_VERSION}).status_code, 400)
        self.assertEqual(c.post("/v2/plan", json={"plan_version": 3, "units": units,
                                                  "codebook_version": CB.CODEBOOK_VERSION_V2}).status_code, 400)

    def test_v4_plan_failure_is_explicit(self):
        """v4 规划异常必须明确失败（不退回 v2）；v3 保留既有安全网（Sol 第 6 轮第三反例）。"""
        from unittest import mock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        units = [{"unit_id": "u", "text": "はい。"}]
        with mock.patch.object(SP, "plan_units_v3", side_effect=RuntimeError("boom")):
            r4 = c.post("/v2/plan", json={"plan_version": 4, "units": units})
            self.assertEqual(r4.status_code, 400)
            self.assertIn("v4 规划失败", r4.json().get("error", ""))
            r3 = c.post("/v2/plan", json={"plan_version": 3, "units": units})
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(r3.json().get("fallback"), "v2")


class TestPlanV4WeakDelivery(unittest.TestCase):
    """astra-3 §5.2 + 复核收窄 §2：弱线索候选阶段只在 v4 打开；歧义句 abstain、单组弱证据不采纳、
    冲突 abstain、延续不能滚动续期，且本阶段**只碰 delivery**。"""

    def _rec(self, text, weak=True, hint=None, prev=None, pv=SP.PLAN_VERSION_V4, cues=None):
        unit = {"unit_id": "u", "text": text, "advance": True}
        if hint:
            unit["emotion"] = hint
        if cues:
            unit["src_cues"] = cues
        recs, state = SP.plan_units_v3([unit], plan_version=pv, prev=prev,
                                       allow_neutral_whisper=(pv >= 4), weak_delivery=weak)
        return recs[0], state

    def test_v3_has_no_stage(self):
        r, s = self._rec("……ん、そこ、だめ……", weak=False, pv=3)
        self.assertNotIn("delivery_stage", r["diagnostics"])
        self.assertNotIn("delivery", s)

    def test_ambiguous_only_abstains(self):
        r, _ = self._rec("……ん、そこ、だめ……")
        d = r["diagnostics"]["delivery_stage"]
        self.assertFalse(d["accepted"])
        self.assertEqual(d["reason"], "ambiguous_only")
        self.assertIsNone(r["diagnostics"]["delivery"])
        self.assertEqual(r["family"], "neutral")        # 歧义词不得加成到亲密

    def test_single_weak_group_not_accepted(self):
        r, _ = self._rec("……小声で言うね。")
        self.assertEqual(r["diagnostics"]["delivery_stage"]["reason"], "unsupported_delivery")
        self.assertIsNone(r["diagnostics"]["delivery"])

    def test_l_plus_j_accepts_and_borrows_anchor(self):
        r, _ = self._rec("……小声で言うね。", hint={"phonation": "whispered"})
        self.assertEqual(r["diagnostics"]["delivery"], "whispered")
        self.assertEqual(r["diagnostics"]["delivery_stage"]["reason"], "independent_l_j")
        self.assertEqual(r["caption"], CB.A_WHISPER)    # 借既有耳语锚（§5.1 组合）

    def test_conflict_abstains(self):
        r, _ = self._rec("……小声で言うね。", hint={"phonation": "breathy"})
        self.assertEqual(r["diagnostics"]["delivery_stage"]["reason"], "conflicting_evidence")
        self.assertIsNone(r["diagnostics"]["delivery"])

    def test_stage_does_not_touch_family(self):
        a, _ = self._rec("……小声で言うね。", hint={"phonation": "whispered"})
        b, _ = self._rec("……小声で言うね。", hint={"phonation": "whispered"}, weak=False)
        self.assertEqual(a["family"], b["family"])      # 本阶段只处理 delivery

    def test_no_rolling_renewal(self):
        whisper = [{"kind": "whisper", "span": [0, 2], "negated": False}]
        _, s1 = self._rec("小声で言うね。", cues=whisper)          # D 决定 ⇒ 可信
        self.assertTrue(s1["delivery_trusted"])
        r2, s2 = self._rec("小声で言うね。", prev=s1)              # H + J ⇒ 延续一句
        self.assertEqual(r2["diagnostics"]["delivery_stage"]["reason"], "continuation_h")
        self.assertFalse(s2["delivery_trusted"])                  # 延续不刷新可信来源
        r3, _ = self._rec("小声で言うね。", prev=s2)               # 再续就续不上了
        self.assertEqual(r3["diagnostics"]["delivery_stage"]["reason"], "unsupported_delivery")


if __name__ == "__main__":
    unittest.main()