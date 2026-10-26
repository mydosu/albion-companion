# -*- coding: utf-8 -*-
"""emotion_bench.py 的纯函数测试（不联网、不用 GPU）：emoji 插入位置、字数、
发声块切分、指标/坏音、噪声与差值判定、矩阵校验、README 生成、CLI 路径守卫。"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "tts" / "probes"))

import emotion_bench as EB  # noqa: E402

SR = 16000


def tone(seconds, hz=200.0, amp=0.3):
    t = np.arange(int(seconds * SR), dtype="float32") / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype("float32")


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype="float32")


def concat(*parts):
    return np.concatenate(parts).astype("float32")


def wav_bytes(samples, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class InsertEmojiTests(unittest.TestCase):
    TEXT = "今日は少しだけ遠回りして、いつもの喫茶店に寄ってから帰ろうと思うんだ。"

    def test_positions(self):
        self.assertEqual(EB.insert_emoji(self.TEXT, "😊", "none"), self.TEXT)
        self.assertEqual(EB.insert_emoji(self.TEXT, "😊", "head"), "😊" + self.TEXT)
        self.assertEqual(EB.insert_emoji(self.TEXT, "😊", "end"), self.TEXT + "😊")
        self.assertEqual(EB.insert_emoji(self.TEXT, "😊", "mid"),
                         "今日は少しだけ遠回りして、😊いつもの喫茶店に寄ってから帰ろうと思うんだ。")

    def test_mid_uses_first_comma_only(self):
        text = "紅茶と、それから焼き菓子も、少しだけ頂戴。"
        self.assertEqual(EB.insert_emoji(text, "😊", "mid"), "紅茶と、😊それから焼き菓子も、少しだけ頂戴。")

    def test_mid_without_comma_splits_by_spoken_chars(self):
        self.assertEqual(EB.insert_emoji("あいうえお", "😊", "mid"), "あいう😊えお")
        self.assertEqual(EB.insert_emoji("あい", "😊", "mid"), "あ😊い")
        # 句号不是「句中」逗号：中点只数能念的字（"あ。いうえ" → 可念字 4 个，切在第 2 个后）
        self.assertEqual(EB.insert_emoji("あ。いうえ", "😊", "mid"), "あ。い😊うえ")

    def test_end1_stops_at_first_sentence(self):
        pair = "雨の音が、なんだか落ち着くよね。ねえ、本当にそれでいいの？"
        self.assertEqual(EB.insert_emoji(pair, "😊", "end1"),
                         "雨の音が、なんだか落ち着くよね。😊ねえ、本当にそれでいいの？")
        # end 永远是整段末尾，end1 才是第一句末尾
        self.assertTrue(EB.insert_emoji(pair, "😊", "end").endswith("？😊"))
        self.assertEqual(EB.insert_emoji("あい", "😊", "end1"), "あい😊")   # 没有句末标点就接末尾
        self.assertEqual(EB.insert_emoji(pair, "😭", "mid"),
                         "雨の音が、😭なんだか落ち着くよね。ねえ、本当にそれでいいの？")

    def test_mid_never_splits_surrogate_pair(self):
        out = EB.insert_emoji("😊あいうえ", "🥺", "mid")
        self.assertEqual(out.count("😊"), 1)
        self.assertIn("🥺", out)

    def test_repeat_doubles(self):
        self.assertEqual(EB.insert_emoji("あい。", "😭", "end", 2), "あい。😭😭")
        self.assertEqual(EB.insert_emoji("あい。", "😭", "mid", 2), "あ😭😭い。")

    def test_rejects_bad_args(self):
        for bad in (("あ", "😊", "middle"), ("あ", "😊", "mid", 0), ("あ", "", "end")):
            with self.assertRaises(ValueError):
                EB.insert_emoji(*bad)


class SpokenCharsTests(unittest.TestCase):
    def test_counts_only_spoken(self):
        self.assertEqual(EB.spoken_chars("今日は少し疲れた。"), 8)
        self.assertEqual(EB.spoken_chars("今日は、😊！"), 3)
        self.assertEqual(EB.spoken_chars("😮‍💨"), 0)          # ZWJ 序列整段不算
        self.assertEqual(EB.spoken_chars("abc１２3"), 6)
        self.assertEqual(EB.spoken_chars("あ い\u3000う"), 3)
        self.assertEqual(EB.spoken_chars(""), 0)

    def test_rate_matches_chars(self):
        m = EB.analyze(tone(2.0), SR, "あいうえ")
        self.assertEqual(m["chars"], 4)
        self.assertAlmostEqual(m["rate_cps"], 4 / m["voiced_s"], places=1)


class SpeechBlockTests(unittest.TestCase):
    def test_two_blocks(self):
        blocks = EB.speech_blocks(concat(tone(0.30), silence(0.30), tone(0.20)), SR)
        self.assertEqual(len(blocks), 2)
        self.assertAlmostEqual(blocks[0][0], 0.0, places=2)
        self.assertAlmostEqual(blocks[0][1], 0.30, places=2)
        self.assertAlmostEqual(blocks[1][0], 0.60, places=2)
        self.assertAlmostEqual(blocks[1][1], 0.80, places=2)

    def test_short_gap_merges_and_150ms_splits(self):
        self.assertEqual(len(EB.speech_blocks(concat(tone(0.2), silence(0.10), tone(0.2)), SR)), 1)
        self.assertEqual(len(EB.speech_blocks(concat(tone(0.2), silence(0.15), tone(0.2)), SR)), 1)
        self.assertEqual(len(EB.speech_blocks(concat(tone(0.2), silence(0.16), tone(0.2)), SR)), 2)

    def test_block_ends_at_last_active_frame(self):
        blocks = EB.speech_blocks(concat(tone(0.25), silence(0.35)), SR)
        self.assertEqual(len(blocks), 1)
        self.assertAlmostEqual(blocks[0][1], 0.25, places=2)

    def test_quiet_and_empty_inputs(self):
        self.assertEqual(EB.speech_blocks(silence(1.0), SR), [])
        self.assertEqual(EB.speech_blocks(np.zeros(0, dtype="float32"), SR), [])
        # 低于 -55 dBFS 地板：算静音
        self.assertEqual(EB.speech_blocks(concat(silence(0.5), 1e-4 * tone(0.5, amp=1.0)), SR), [])

    def test_lead_and_tail_are_measured(self):
        m = EB.analyze(concat(silence(0.12), tone(1.0), silence(0.20)), SR, "あ")
        self.assertAlmostEqual(m["lead_s"], 0.12, places=2)
        self.assertAlmostEqual(m["tail_s"], 0.20, places=2)


class AnalyzeTests(unittest.TestCase):
    def test_clean_tone(self):
        m = EB.analyze(tone(1.5, 200.0), SR, "あいうえお")
        self.assertAlmostEqual(m["dur"], 1.5, places=2)
        self.assertEqual(m["n_blocks"], 1)
        self.assertAlmostEqual(m["f0_mean"], 200.0, delta=6.0)
        self.assertEqual(m["clip_ratio"], 0.0)
        self.assertLess(m["silent_ratio"], 0.1)
        self.assertEqual(EB.bad_audio(m), [])

    def test_clipping_and_short_and_silence(self):
        clipped = np.clip(tone(1.0, amp=3.0), -1, 1)
        self.assertTrue(any("削波" in w for w in EB.bad_audio(EB.analyze(clipped, SR, "あ"))))
        self.assertTrue(any("空/截断" in w for w in EB.bad_audio(EB.analyze(tone(0.2), SR, "あ"))))
        self.assertTrue(any("静音" in w for w in EB.bad_audio(EB.analyze(silence(2.0), SR, "あ"))))

    def test_extra_tail_block_shows_up(self):
        base = EB.analyze(concat(tone(1.0), silence(0.5)), SR, "あ")
        plus = EB.analyze(concat(tone(1.0), silence(0.5), tone(0.3)), SR, "あ")
        self.assertEqual(plus["n_blocks"], 2)
        extra = [b for b in plus["blocks"] if b[0] >= base["blocks"][-1][1] - 0.15]
        self.assertEqual(len(extra), 1)
        self.assertAlmostEqual(extra[0][1] - extra[0][0], 0.3, places=2)

    def test_wav_roundtrip(self):
        sr, a = EB.decode_wav(wav_bytes(concat(tone(0.5), silence(0.01))))
        self.assertEqual(sr, SR)
        self.assertAlmostEqual(len(a) / sr, 0.51, delta=0.01)

    def test_window_metrics(self):
        m = EB.window_metrics(tone(1.0, 220.0), SR, 0.0, 0.5)
        self.assertAlmostEqual(m["f0_mean"], 220.0, delta=8.0)
        self.assertIsNone(EB.window_metrics(tone(0.2), SR, 0.0, 0.02)["f0_mean"])


def base_row(sentence="s1", seed=1, dur_extra=0.0, f0_offset=0.0, group="base", emoji="",
             emoji_id="x", pos="end", anchor="quiet", caption=None, cfg=None,
             caption_mode=None, baseline_anchor=None):
    """造一条假 row（用真 analyze，音频是 2 s 主音 + 可选多出的尾音）。"""
    parts = [tone(2.0, 200.0 + f0_offset)]
    if dur_extra > 0:
        parts += [silence(0.5), tone(dur_extra, 300.0)]
    m = EB.analyze(concat(*parts), SR, "あいうえお")
    m.update(id="%s-%s-%d" % (sentence, group, seed), sentence=sentence, seed=seed,
             group=group, emoji=emoji, emoji_id=emoji_id if emoji else "",
             pos=pos if emoji else "none",
             repeat=1, anchor=anchor, text="あいうえお" + emoji,
             caption=caption, cfg=cfg, caption_mode=caption_mode,
             baseline_anchor=baseline_anchor)
    return m


class CompareTests(unittest.TestCase):
    def test_noise_floor_and_judge(self):
        rows = [base_row(seed=1, f0_offset=0), base_row(seed=2, f0_offset=6.0)]
        floor, detail = EB.noise_floor(rows)
        self.assertGreaterEqual(floor["f0"], 4.0)
        self.assertEqual(detail["s1"]["n_seeds"], 2)
        self.assertEqual(EB.judge([0.30, 0.40], {"dur": 0.0}, "dur"), (0.35, True))
        self.assertEqual(EB.judge([0.10, 0.12], {"dur": 0.0}, "dur"), (0.11, False))
        self.assertEqual(EB.judge([0.5, -0.4], {"dur": 0.0}, "dur")[1], False)   # 方向不一致
        self.assertEqual(EB.judge([None, None], {}, "dur"), (None, False))

    def test_judge_uses_noise_floor_over_min_delta(self):
        self.assertFalse(EB.judge([0.60, 0.70], {"dur": 0.8}, "dur")[1])
        self.assertTrue(EB.judge([0.90, 0.95], {"dur": 0.8}, "dur")[1])

    def test_attach_baselines_sets_deltas_and_tail_block(self):
        rows = [base_row(seed=1), base_row(seed=1, dur_extra=0.4, group="style", emoji="😊")]
        rows = EB.attach_baselines(rows)
        cond = [r for r in rows if r["group"] == "style"][0]
        self.assertEqual(cond["base_id"], "s1-base-1")
        self.assertAlmostEqual(cond["delta"]["dur"], 0.9, delta=0.05)   # 0.5 静音 + 0.4 尾音
        self.assertEqual(cond["n_blocks"], 2)
        self.assertAlmostEqual(cond["tail_block_s"], 0.4, delta=0.03)
        self.assertEqual(cond["bad"], [])
        self.assertIsNone([r for r in rows if r["group"] == "base"][0]["delta"])

    def test_attach_baselines_without_baseline(self):
        rows = EB.attach_baselines([base_row(seed=9, dur_extra=0.2, group="style", emoji="😊")])
        self.assertIsNone(rows[0]["delta"])
        self.assertIsNone(rows[0]["tail_block_s"])


class MatrixTests(unittest.TestCase):
    def test_default_matrix_is_valid_and_unique(self):
        m, texts = EB.normalize_matrix(EB.build_matrix())
        ids = [c["id"] for c in m["conditions"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("pair", texts)
        for c in m["conditions"]:
            self.assertTrue(EB.condition_text(c, texts))

    def test_default_matrix_covers_the_experiment(self):
        m, texts = EB.normalize_matrix(EB.build_matrix())
        style = [c for c in m["conditions"] if c["group"] == "style"]
        self.assertEqual(len(style), 11 * 3 * len(EB.DEFAULT_SEEDS))
        verb = [c for c in m["conditions"] if c["group"] == "nonverbal"]
        self.assertEqual(len(verb), 6 * 3 * len(EB.DEFAULT_SEEDS))
        pairs = [c for c in m["conditions"] if c["group"] == "pair"]
        self.assertTrue(all(c["sentence"] == "pair" for c in pairs))
        # 串句实验的 emoji 必须落在第一句里（不是整段末尾）
        end1 = [c for c in pairs if c["pos"] == "end1"][0]
        text = EB.condition_text(end1, texts)
        self.assertTrue(text.startswith(EB.SENTENCES_BY_ID[EB.PAIR[0]]["text"] + "😊"))
        self.assertTrue(text.endswith(EB.SENTENCES_BY_ID[EB.PAIR[1]]["text"]))
        # 每个 emoji 都有三个位置，且两 seed 各一条
        for eid in [k for k, _, _ in EB.STYLE_EMOJIS]:
            for pos in ("head", "mid", "end"):
                have = {c["seed"] for c in style if c["emoji_id"] == eid and c["pos"] == pos}
                self.assertEqual(have, set(EB.DEFAULT_SEEDS))

    def test_custom_matrix_json_roundtrip(self):
        raw = dict(sentences=[dict(id="a", text="あい。")],
                   anchors=dict(q=dict(family="neutral", caption="落ち着いた、静かな声で", cfg=3.0,
                                       tier="weak")),
                   conditions=[dict(id="c1", sentence="a", seed=1, anchor="q"),
                               dict(id="c2", sentence="a", seed=1, anchor="q", emoji="😊", pos="end")])
        m, texts = EB.normalize_matrix(raw)
        self.assertEqual(m["seeds"], EB.DEFAULT_SEEDS)
        self.assertEqual(EB.condition_text(m["conditions"][1], texts), "あい。😊")
        json.dumps(m)   # 可序列化

    # ---- 自由描述（caption_mode=free）：条件级覆盖 ----
    def _free_raw(self, **cond):
        c = dict(id="c1", sentence="a", seed=1, anchor="q", group="free",
                 caption="恥ずかしそうに、声を潜めて話す。", cfg=4.2, caption_mode="free")
        c.update(cond)
        return dict(sentences=[dict(id="a", text="あい。")],
                    anchors=dict(q=dict(family="neutral", caption="落ち着いた、静かな声で",
                                        cfg=3.0, tier="weak")),
                    conditions=[c])

    def test_free_condition_keeps_caption_cfg_and_mode(self):
        m, _ = EB.normalize_matrix(self._free_raw())
        spec = EB.unit_spec(m["conditions"][0], m["anchors"])
        self.assertEqual(spec["caption"], "恥ずかしそうに、声を潜めて話す。")
        self.assertEqual(spec["cfg"], 4.2)
        self.assertEqual(spec["caption_mode"], "free")
        self.assertEqual(spec["family"], "neutral")      # 族仍来自锚
        self.assertNotIn("tier", spec)                   # 自由描述没有码本档位

    def test_unit_spec_without_overrides_is_the_anchor(self):
        m, _ = EB.normalize_matrix(dict(
            sentences=[dict(id="a", text="あい。")],
            anchors=dict(q=dict(family="bright", caption="明るく、弾んだ声で", cfg=3.2, tier="weak")),
            conditions=[dict(id="c1", sentence="a", anchor="q")]))
        spec = EB.unit_spec(m["conditions"][0], m["anchors"])
        self.assertEqual(spec, dict(family="bright", caption="明るく、弾んだ声で", cfg=3.2, tier="weak"))
        self.assertIsNone(spec.get("caption_mode"))       # 不带 free ⇒ 生产路径

    def test_free_matrix_rejects_bad_conditions(self):
        bad = [
            dict(caption=""),                                   # 空描述
            dict(caption=None),                                 # free 却没给描述
            dict(cfg="4.2"),                                    # cfg 不是数值
            dict(cfg=True),
            dict(caption_mode="Free"),                          # 只认小写 free
            dict(baseline_anchor="nope"),                       # 基线锚不存在
        ]
        for patch in bad:
            with self.assertRaises(EB.BenchError, msg=repr(patch)):
                EB.normalize_matrix(self._free_raw(**patch))

    def test_unknown_noise_reference_rejected(self):
        raw = self._free_raw()
        raw["noise_reference"] = "nope"
        with self.assertRaises(EB.BenchError):
            EB.normalize_matrix(raw)

    def test_baseline_anchor_picks_the_named_anchor(self):
        """7 锚基线 / 自由描述都相对参考锚，不能被「同句同 seed 的另一条 base」抢走。"""
        rows = [base_row(seed=1, anchor="ref", dur_extra=0.0),
                base_row(seed=1, anchor="bright", dur_extra=1.0, baseline_anchor="ref"),
                base_row(seed=1, anchor="ref", group="free", dur_extra=0.4,
                         caption="自由描述", cfg=4.0, caption_mode="free")]
        rows = EB.attach_baselines(rows)
        bright = [r for r in rows if r["anchor"] == "bright"][0]
        free = [r for r in rows if r["group"] == "free"][0]
        self.assertEqual(bright["base_id"], "s1-base-1")        # 指到 ref，而不是自己
        self.assertIsNotNone(bright["delta"])
        self.assertEqual(free["base_id"], "s1-base-1")
        self.assertAlmostEqual(free["delta"]["dur"], 0.9, delta=0.05)

    def test_noise_floor_uses_only_the_reference_anchor(self):
        rows = [base_row(seed=1, anchor="ref", f0_offset=0.0),
                base_row(seed=2, anchor="ref", f0_offset=4.0),
                base_row(seed=1, anchor="bright", f0_offset=120.0),
                base_row(seed=2, anchor="bright", f0_offset=180.0)]
        floor, detail = EB.noise_floor(rows, "ref")
        self.assertEqual(list(detail), ["s1"])
        self.assertLess(floor["f0"], 10.0)      # 别的锚不参与，否则会被 120~180 Hz 撑爆
        # 参考锚对不上时退回第一条 base 行的锚，不静默出空表
        floor2, detail2 = EB.noise_floor(rows, "missing")
        self.assertTrue(detail2)

    # ---- 入库的 15 条自由描述矩阵 ----
    def test_shipped_free_matrix(self):
        path = EB.resolve_matrix_path("emotion_free_20261026")
        self.assertTrue(str(path).endswith("emotion_free_20261026.json"))
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf8"))
        m, texts = EB.normalize_matrix(raw)
        conds = m["conditions"]
        self.assertEqual(len(conds), 150)
        base = [c for c in conds if c["group"] == "base"]
        free = [c for c in conds if c["group"] == "free"]
        sweep = [c for c in conds if c["group"] == "free_cfg"]
        self.assertEqual((len(base), len(free), len(sweep)), (42, 90, 18))
        self.assertEqual(len(m["seeds"]), 3)
        self.assertEqual(len({c["sentence"] for c in conds}), 2)
        # 15 条描述 × 2 句 × 3 seed
        caps = {c["caption"] for c in free}
        self.assertEqual(len(caps), 15)
        for cap in caps:
            self.assertTrue(10 <= len(cap) <= 30, (len(cap), cap))
            for sid in ("s1", "s4"):
                seeds = {c["seed"] for c in free if c["caption"] == cap and c["sentence"] == sid}
                self.assertEqual(len(seeds), 3)
        # 15 个要求覆盖的情绪都在（按 slug 对一遍）
        slugs = {c["id"].split("-")[2] for c in free if c["sentence"] == "s1"}
        self.assertEqual(slugs, {
            "shy_quiet", "stifle_laugh", "coquettish", "sleepy", "about_cry", "angry",
            "worried", "surprised", "comfort", "proud", "resigned", "nervous_stutter",
            "down", "excited", "whisper"})
        # free 条件必须带 free 模式；锚条件一个都不带
        self.assertTrue(all(c.get("caption_mode") == "free" for c in free + sweep))
        self.assertTrue(all(c.get("caption_mode") is None for c in base))
        # cfg 扫描：同一描述 2/4/6
        self.assertEqual({c["cfg"] for c in sweep}, {2, 4, 6})
        self.assertEqual(len({c["caption"] for c in sweep}), 1)
        # 7 条锚里 6 条相对参考锚，参考锚自己不带（它就是基线）
        self.assertEqual(len({c["anchor"] for c in base}), 7)
        self.assertEqual(sum(1 for c in base if c.get("baseline_anchor")), 36)

    def test_resolve_matrix_path(self):
        p = EB.resolve_matrix_path("tests/tts/data/emotion_free_20261026.json")
        self.assertTrue(p.exists())
        with self.assertRaises(EB.BenchError):
            EB.resolve_matrix_path("no-such-matrix-xyz")

    def test_rejects_bad_matrix(self):
        good = dict(sentences=[dict(id="a", text="あ")],
                    conditions=[dict(id="c1", sentence="a", anchor="quiet")])
        bad_cases = [
            dict(good, sentences=[]),
            dict(good, conditions=[]),
            dict(good, conditions=[dict(id="c1", sentence="z", anchor="quiet")]),
            dict(good, conditions=[dict(id="c1", sentence="a", anchor="nope")]),
            dict(good, conditions=[dict(id="c1", sentence="a", anchor="quiet", emoji="😊")]),
            dict(good, conditions=[dict(id="c1", sentence="a", anchor="quiet", pos="end")]),
            dict(good, conditions=[dict(id="c1", sentence="a", anchor="quiet", pos="side", emoji="😊")]),
            dict(good, conditions=[dict(id="c1", sentence="a", anchor="quiet", repeat=0)]),
            dict(good, conditions=[dict(id="c1", sentence="a", anchor="quiet"),
                                   dict(id="c1", sentence="a", anchor="quiet")]),
            dict(good, sentences=[dict(id="a", text="あ"), dict(id="a", text="い")]),
        ]
        for bad in bad_cases:
            with self.assertRaises(EB.BenchError):
                EB.normalize_matrix(bad)


class ReadmeTests(unittest.TestCase):
    def _payload(self):
        rows = [base_row("s1", 1), base_row("s1", 2),
                base_row("s1", 1, dur_extra=0.4, group="style", emoji="😊", emoji_id="happy"),
                base_row("s1", 2, dur_extra=0.5, group="style", emoji="😊", emoji_id="happy")]
        rows = EB.attach_baselines(rows)
        floor, detail = EB.noise_floor(rows)
        matrix, _ = EB.normalize_matrix(dict(
            sentences=[dict(id="s1", text="あいうえお")],
            conditions=[dict(id=c["id"], sentence="s1", anchor="quiet", seed=c["seed"],
                             group=c["group"], emoji=c["emoji"], pos=c["pos"]) for c in rows]))
        return matrix, rows, floor, detail

    def test_readme_has_tables_and_picks(self):
        matrix, rows, floor, detail = self._payload()
        md = EB.render_readme("run-x", matrix, rows, [], floor, detail)
        self.assertIn("# 情感评测台：run-x", md)
        self.assertIn("口径与噪声底", md)
        self.assertIn("先听这几条", md)
        self.assertIn("| 😊 happy | 句末 |", md)          # 风格类表里有真行
        self.assertIn("`wav/s1-style-", md)              # 推荐里给到对应 wav
        self.assertIn("| emoji | 位置 |", md)
        self.assertIn("speaker_inversion", md)   # 说话人相似度如实写「跳过」

    def test_readme_reports_bad_audio(self):
        rows = EB.attach_baselines([base_row("s1", 1)])
        rows[0]["bad"] = ["时长<0.4s（空/截断）"]
        md = EB.render_readme("run-y", *self._payload()[:1], rows, [], {}, {})
        self.assertIn("`s1-base-1.wav`：时长<0.4s", md)

    def test_readme_lists_free_descriptions_and_cfg_sweep(self):
        cap = "恥ずかしそうに、声を潜めて話す。"
        rows = [base_row(seed=1, anchor="ref"),
                base_row(seed=1, anchor="ref", dur_extra=0.4, group="free",
                         caption=cap, cfg=4.2, caption_mode="free"),
                base_row(seed=1, anchor="ref", dur_extra=0.6, group="free_cfg",
                         caption=cap, cfg=6.0, caption_mode="free")]
        raw = dict(sentences=[dict(id="s1", text="あいうえお")],
                   anchors=dict(ref=dict(family="neutral", caption="落ち着いた、静かな声で",
                                         cfg=3.0, tier="weak")),
                   conditions=[dict(id="c0", sentence="s1", anchor="ref", seed=1),
                               dict(id="c1", sentence="s1", anchor="ref", seed=1, group="free",
                                    caption=cap, cfg=4.2, caption_mode="free"),
                               dict(id="c2", sentence="s1", anchor="ref", seed=1,
                                    group="free_cfg", caption=cap, cfg=6.0,
                                    caption_mode="free")])
        matrix, _ = EB.normalize_matrix(raw)
        rows = EB.attach_baselines(rows)
        floor, detail = EB.noise_floor(rows, "ref")
        md = EB.render_readme("run-z", matrix, rows, [], floor, detail)
        self.assertIn("自由描述（`caption_mode=free`）", md)
        self.assertIn(cap, md)
        self.assertIn("其中 **2 条条件**（1 条自由描述 + 1 条同描述换 cfg）", md)
        self.assertIn("同一描述换 cfg", md)
        self.assertIn("合计 1 个（描述 × 句）单元", md)
        self.assertIn("时长可测 **1**", md)


class CliTests(unittest.TestCase):
    def test_out_must_be_under_tests_output(self):
        with self.assertRaises(SystemExit):
            EB.main(["--out", str(ROOT / "tts" / "not-output")])

    def test_dump_matrix_writes_the_default_matrix(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "m.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(EB.main(["--dump-matrix", str(path)]), 0)
            m = json.loads(path.read_text(encoding="utf8"))
            self.assertEqual(len(m["conditions"]), 169)
            self.assertTrue(any(c.get("pos") == "end1" for c in m["conditions"]))


if __name__ == "__main__":
    unittest.main()
