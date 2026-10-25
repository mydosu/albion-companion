# -*- coding: utf-8 -*-
"""流式 TTS 的节奏（2026-10-25）：少切半句、半句保持「没说完」的语调、统一气口、符号段并回上一段。

全部离线：切句器是纯逻辑，整形用合成的 WAV，翻译用替身。
"""
import asyncio
import io
import pathlib
import sys
import time
import unittest
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "stream"))
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import bridge_v2  # noqa: E402
import stream_server as S  # noqa: E402
import gap_shaper as GS  # noqa: E402
from session_text import SessionText  # noqa: E402

SR = 24000


def cut(text, **kw):
    s = SessionText("rhythm", **kw)
    units = s.feed(text, 0.0) + s.finish(1.0)
    return [(u.speech_text, u.reason) for u in units]


class CutPolicyTest(unittest.TestCase):
    TEXT = "指挥官，今天也辛苦了，我给你泡了杯热茶放在桌上，等你忙完再慢慢喝，不要着急，也不要硬撑。晚安。"

    def test_first_unit_keeps_soft_cut_for_fast_first_audio(self):
        units = cut(self.TEXT)
        self.assertEqual(units[0][1], "soft")
        self.assertTrue(units[0][0].endswith("，"))

    def test_later_units_cut_only_at_sentence_end(self):
        units = cut(self.TEXT)
        later = units[1:]
        self.assertTrue(later)
        # 第一个单元之后：没有任何一个是逗号半句
        self.assertNotIn("soft", [r for _, r in later])
        self.assertEqual("".join(t for t, _ in units), self.TEXT)

    def test_old_behaviour_available_by_knob(self):
        units = cut(self.TEXT, soft_units=99)
        self.assertGreater(len([1 for _, r in units if r == "soft"]), 1)

    def test_long_sentence_falls_back_to_nearest_comma(self):
        body = "，".join("这是第%d段比较长的话" % i for i in range(1, 12)) + "。"
        units = cut("好的，我知道了。" + body, later_force=40)
        long_units = [(t, r) for t, r in units[1:]]
        self.assertTrue(all(len(t) <= 41 for t, _ in long_units), [len(t) for t, _ in long_units])
        self.assertTrue(all(t.endswith(("，", "。")) for t, _ in long_units))   # 在逗号切，不切在词中间
        self.assertIn("soft", [r for _, r in long_units])

    def test_no_comma_means_hard_force(self):
        units = cut("好的。" + "没有标点的超长串" * 12, later_force=30)
        self.assertIn("force", [r for _, r in units])

    def test_chunking_invariance_with_new_policy(self):
        whole = [(u.start, u.end, u.reason) for u in
                 (lambda s: s.feed(self.TEXT, 0.0) + s.finish(1.0))(SessionText("c"))]
        s = SessionText("c")
        got = []
        for ch in self.TEXT:
            got += s.feed(ch, 0.0)
        got += s.finish(1.0)
        self.assertEqual([(u.start, u.end, u.reason) for u in got], whole)

    def test_later_idle_window_is_longer(self):
        s = SessionText("idle")
        s.feed("好的，我们开始吧。", 0.0)
        s.tick(0.5)      # 句末硬切等收尾窗口
        s.tick(0.7)
        s.feed("然后第二句话讲到一半", 1.0)      # 后续单元
        self.assertEqual(s.tick(1.0 + s.idle_window + 0.05), [], "首单元的 0.8s 窗口不该冲掉后续半句")
        out = s.tick(1.0 + s.later_idle_window + 0.05)
        self.assertEqual([u.reason for u in out], ["idle"])


def make_wav(lead_s, voice_s, tail_s, sr=SR, ch=1, amp=0.5):
    t = np.arange(int(voice_s * sr)) / sr
    voice = (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    pcm = np.concatenate([np.zeros(int(lead_s * sr), "<i2"), voice, np.zeros(int(tail_s * sr), "<i2")])
    if ch > 1:
        pcm = np.repeat(pcm[:, None], ch, axis=1)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def info(wav):
    with wave.open(io.BytesIO(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels())
        sr = w.getframerate()
    loud = np.flatnonzero(np.abs(pcm).max(axis=1) > 300)
    return dict(dur=len(pcm) / sr, lead=loud[0] / sr, tail=(len(pcm) - 1 - loud[-1]) / sr)


class GapShaperTest(unittest.TestCase):
    def test_lead_trimmed_and_tail_fixed_per_kind(self):
        for kind in ("sentence", "comma", "ellipsis", "cut"):
            with self.subTest(kind=kind):
                got = info(GS.shape(make_wav(0.85, 1.0, 1.08), kind))
                self.assertAlmostEqual(got["lead"], GS.LEAD_S, delta=0.02)
                # 有声结束后保留 TAIL_KEEP_S 的自然衰减，再接定长静音
                self.assertAlmostEqual(got["tail"], GS.TAIL_S[kind], delta=0.05)

    def test_lead_is_padded_when_voice_starts_at_sample_zero(self):
        got = info(GS.shape(make_wav(0.0, 1.0, 0.9), "sentence"))
        self.assertAlmostEqual(got["lead"], GS.LEAD_S, delta=0.02)

    def test_short_tail_is_padded_up(self):
        got = info(GS.shape(make_wav(0.1, 1.0, 0.1), "sentence"))
        self.assertAlmostEqual(got["tail"], GS.TAIL_S["sentence"], delta=0.05)

    def test_voice_is_not_touched(self):
        src = make_wav(0.4, 1.0, 0.9)
        out = GS.shape(src, "sentence")
        with wave.open(io.BytesIO(src)) as a, wave.open(io.BytesIO(out)) as b:
            va = np.frombuffer(a.readframes(a.getnframes()), "<i2")
            vb = np.frombuffer(b.readframes(b.getnframes()), "<i2")
        loud_a, loud_b = va[np.abs(va) > 300], vb[np.abs(vb) > 300]
        self.assertTrue(np.array_equal(loud_a, loud_b))

    def test_stereo_ok(self):
        got = info(GS.shape(make_wav(0.5, 1.0, 1.0, ch=2), "comma"))
        self.assertAlmostEqual(got["tail"], GS.TAIL_S["comma"], delta=0.05)

    def test_unshapeable_input_is_returned_unchanged(self):
        silence = make_wav(0.5, 0.0, 0.5)
        for blob in (b"", b"not a wav at all", silence):
            self.assertEqual(GS.shape(blob, "sentence"), blob)
        self.assertEqual(GS.shape(make_wav(0.5, 1.0, 1.0), "nonsense"), make_wav(0.5, 1.0, 1.0))

    def test_disabled_switch(self):
        src = make_wav(0.5, 1.0, 1.0)
        old, GS.ENABLED = GS.ENABLED, False
        try:
            self.assertEqual(GS.shape(src, "sentence"), src)
        finally:
            GS.ENABLED = old

    def test_tail_kind(self):
        k = GS.tail_kind
        self.assertEqual(k("今天辛苦了。"), "sentence")
        self.assertEqual(k("真的吗？"), "sentence")
        self.assertEqual(k("太好了！"), "sentence")
        self.assertEqual(k("他说「好的。」"), "sentence")        # 闭引号后看里面的句末
        self.assertEqual(k("我想想，"), "comma")
        self.assertEqual(k("嗯……"), "ellipsis")
        self.assertEqual(k("嗯…", "hard"), "ellipsis")
        self.assertEqual(k("说到一半", "idle"), "cut")
        self.assertEqual(k("说到一半", "force"), "cut")
        self.assertEqual(k("晚安", "finish"), "sentence")       # 整段收尾没标点，按句末
        self.assertEqual(k("我想想，", "soft"), "comma")        # 标点优先于原因


class ConfigTest(unittest.TestCase):
    def test_cut_defaults_and_env_override_reach_session(self):
        from unittest.mock import patch
        cfg = S.Config()
        self.assertEqual(cfg.cut, {"soft_units": 1, "later_force": 64, "later_soft_min": 12, "later_idle_window": 2.0, "later_ramp": 3.5, "ramp_floor": 24})
        self.assertEqual(cfg.as_dict()["cut"], cfg.cut)
        self.assertEqual(cfg.as_dict()["gap"]["tail_s"], GS.TAIL_S)
        with patch.dict("os.environ", {"ALBION_CUT_LATER_FORCE": "80", "ALBION_CUT_SOFT_UNITS": "2"}):
            cfg = S.Config()
        sess = S.Session(cfg, "s", 1, "albion", "wav")
        self.assertEqual((sess.text.later_force, sess.text.soft_units), (80, 2))

    def test_gap_defaults(self):
        self.assertEqual(GS.TAIL_S, {"sentence": 0.7, "comma": 0.3, "ellipsis": 0.8, "cut": 0.15})
        self.assertEqual(GS.LEAD_S, 0.06)


class ContinuationTest(unittest.TestCase):
    def test_continues(self):
        self.assertTrue(bridge_v2.continues("今天海风很舒服，", "soft"))
        self.assertTrue(bridge_v2.continues("今天海风很舒服，"))                 # 老调用方不带原因：只认逗号
        self.assertTrue(bridge_v2.continues("说到一半", "idle"))
        self.assertTrue(bridge_v2.continues("说到一半", "force"))
        self.assertFalse(bridge_v2.continues("说到一半"))                       # 无原因 + 无标点：不猜
        self.assertFalse(bridge_v2.continues("晚安", "finish"))
        self.assertFalse(bridge_v2.continues("今天辛苦了。", "hard"))
        self.assertFalse(bridge_v2.continues("真的吗？", "soft"))

    def test_continue_tail(self):
        f = bridge_v2.continue_tail
        self.assertEqual(f("今日は海風が気持ちいいです。"), "今日は海風が気持ちいいです、")
        self.assertEqual(f("そうなんですか？"), "そうなんですか、")
        self.assertEqual(f("そうなんですか？！"), "そうなんですか、")
        self.assertEqual(f("海風が気持ちいいですね、"), "海風が気持ちいいですね、")
        self.assertEqual(f("海風が気持ちいいですね"), "海風が気持ちいいですね、")
        self.assertEqual(f("海風が…"), "海風が…")
        self.assertEqual(f("おはよう♡"), "おはよう♡")
        self.assertEqual(f(""), "")

    def _unit(self, text, reason, tr):
        req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u", voice="albion",
                                       source_range=(0, len(text)), speech_text=text, unit_reason=reason)
        return asyncio.run(bridge_v2.PrepareService(tr)._translate_unit(req, time.monotonic()))

    def test_soft_unit_translation_ends_with_comma(self):
        r = self._unit("今天海风很舒服，", "soft", lambda s: "今日は海風が気持ちいいです。")
        self.assertEqual(r.status, "ready")
        self.assertEqual(r.ja_segments[-1]["text"], "今日は海風が気持ちいいです、")

    def test_hard_unit_untouched(self):
        r = self._unit("今天海风很舒服。", "hard", lambda s: "今日は海風が気持ちいいです。")
        self.assertEqual(r.ja_segments[-1]["text"], "今日は海風が気持ちいいです。")

    def test_switch_off(self):
        old, bridge_v2.CONTINUE_TAIL = bridge_v2.CONTINUE_TAIL, False
        try:
            r = self._unit("今天海风很舒服，", "soft", lambda s: "今日は海風が気持ちいいです。")
        finally:
            bridge_v2.CONTINUE_TAIL = old
        self.assertEqual(r.ja_segments[-1]["text"], "今日は海風が気持ちいいです。")

    def test_only_last_segment_changes(self):
        tbl = {"好的": "はい。", "我先说一半": "先に半分だけ言いますね。"}
        r = self._unit("好的。我先说一半", "idle", lambda s: tbl[s])
        self.assertEqual([s["text"] for s in r.ja_segments], ["はい。", "先に半分だけ言いますね、"])


class SymbolSegmentTest(unittest.TestCase):
    def test_split_merges_unspeakable_into_previous(self):
        f = bridge_v2.split_for_translation
        self.assertEqual(f("实验做完了吗？……"), ["实验做完了吗？……"])
        self.assertEqual(f("你好。……再见。"), ["你好", "……再见"])        # 「……再见」是一个可念的句子
        self.assertEqual(f("你好。……！再见"), ["你好。……", "再见"])
        self.assertEqual(f("……你好。再见。"), ["……你好", "再见"])
        self.assertEqual(f("你好。再见"), ["你好", "再见"])
        self.assertEqual(f("……"), [])

    def test_unit_with_trailing_ellipsis_is_translated_not_failed(self):
        seen = []

        def tr(seg):
            seen.append(seg)
            return "もう終わりましたか？……"

        req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u", voice="albion",
                                       source_range=(0, 9), speech_text="实验做完了吗？……", unit_reason="hard")
        r = asyncio.run(bridge_v2.PrepareService(tr)._translate_unit(req, time.monotonic()))
        self.assertEqual(r.status, "ready", r.error)
        self.assertEqual(seen, ["实验做完了吗？……"])      # 只送一次，且带着问号和省略号


if __name__ == "__main__":
    unittest.main(verbosity=2)
