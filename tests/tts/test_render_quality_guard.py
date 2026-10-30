# -*- coding: utf-8 -*-
"""渲完自检与重渲（2026-10-30）：截在响处 / albion 男声漂移 → 换随机 seed 重渲一次。

背景（宣传片录制）：同一条回复里偶发几句 F0 中位 121–148Hz（正常 296–380Hz），另外有些句子的
原始渲染停在响处（末 2ms 还有 -25～-28dBFS）。`render_queue._render_one` 渲完量两样，命中就
换一个随机 seed 重渲**一次**，按「不男声 > 不截尾 > 原结果」取更好的那次；显式带 seed 的听测
请求不重渲。

不联网、不用 GPU：合成腿是假的，按调用次序返回事先做好的波形。判据本身（末 30ms 电平、
FFT 自相关 F0 中位）就是生产实现，和 gap_shaper / 评测台同口径。
"""
import pathlib
import sys
import unittest
from types import SimpleNamespace

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import render_queue as RQ  # noqa: E402

SR = 16000


def tone(f0_hz, dur_s=0.5, silence_s=0.06):
    """正弦 + 尾静音（silence_s=0 ⇒ 停在响处，模拟上游截在响）。"""
    a = 0.5 * np.sin(2 * np.pi * f0_hz * np.arange(int(dur_s * SR)) / SR)
    if silence_s > 0:
        a = np.concatenate([a, np.zeros(int(silence_s * SR))])
    return a


FEMALE = tone(320.0)                 # 不男声、不截尾
MALE = tone(180.0)                   # albion 下判男声、不截尾
CLIPPED = tone(320.0, silence_s=0)   # 不男声、截在响处
MALE_CLIPPED = tone(190.0, silence_s=0)


def unit(**extra):
    u = {"unit_id": "u0", "text": "はい、そうですね。", "caption": "自然な声で、丁寧に",
         "cfg": 3.0, "family": "neutral", "voice": "albion"}
    u.update(extra)
    return u


class GuardCase(unittest.TestCase):
    def make(self, waves):
        calls = []

        def synth(u, out_wav):
            calls.append(dict(u))
            return SR, waves[min(len(calls) - 1, len(waves) - 1)]
        return RQ.RenderQueue(synth=synth, encode=lambda *a: b"wav"), calls

    def render(self, waves, cancel=False, **extra):
        q, calls = self.make(waves)
        sid, gen = q.open("albion")             # 真会话：取消判断走生产的 is_cancelled
        if cancel:
            q.cancel(sid, gen)
        res = q._render_one(SimpleNamespace(unit=unit(**extra), session_id=sid, generation=gen))
        return res, calls


class RerenderTest(GuardCase):
    def test_clipped_first_attempt_is_rerendered(self):
        res, calls = self.render([CLIPPED, FEMALE])
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["picked"], 2)
        self.assertEqual(res["rerender"], "clip")
        self.assertTrue(res["quality"][0]["clip"])
        self.assertFalse(res["quality"][1]["clip"])
        self.assertTrue(np.array_equal(res["wave"], FEMALE))

    def test_male_first_attempt_is_rerendered(self):
        res, calls = self.render([MALE, FEMALE])
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["picked"], 2)
        self.assertEqual(res["rerender"], "male")
        self.assertTrue(res["quality"][0]["male"])
        self.assertTrue(np.array_equal(res["wave"], FEMALE))

    def test_at_most_one_rerender(self):
        res, calls = self.render([MALE, tone(185.0), FEMALE])
        self.assertEqual(len(calls), 2, "最多只重渲一次")
        self.assertEqual(res["picked"], 1, "两次都命中时保留原结果")
        self.assertEqual(res["rerender"], "male")
        self.assertTrue(np.array_equal(res["wave"], MALE))

    def test_male_outranks_clip(self):
        """「不男声 > 不截尾」：重渲出来不男声但截尾，仍好过男声不截尾。"""
        res, calls = self.render([CLIPPED, MALE])
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["picked"], 1)
        self.assertTrue(np.array_equal(res["wave"], CLIPPED))

    def test_clean_first_attempt_is_not_rerendered(self):
        res, calls = self.render([FEMALE])
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["picked"], 1)
        self.assertEqual(res["rerender"], "")

    def test_explicit_seed_passes_through_and_never_rerenders(self):
        res, calls = self.render([MALE], seed=12345)
        self.assertEqual(len(calls), 1, "听测指定了 seed 就不许偷偷重渲")
        self.assertEqual(calls[0]["seed"], 12345)
        self.assertEqual(res["seed"], 12345)
        self.assertTrue(res["explicit_seed"])
        self.assertEqual(res["picked"], 1)

    def test_generated_seed_is_real_and_in_range(self):
        res, calls = self.render([FEMALE])
        seed = calls[0]["seed"]
        self.assertIsInstance(seed, int)
        self.assertGreaterEqual(seed, 0)
        self.assertLess(seed, 2 ** 31)
        self.assertEqual(res["seed"], seed, "日志里的 seed 必须是真传下去的那个")

    def test_rerender_uses_a_different_seed(self):
        res, calls = self.render([MALE, FEMALE])
        self.assertNotEqual(calls[0]["seed"], calls[1]["seed"])

    def test_cancelled_session_skips_rerender(self):
        """会话/世代已取消：这一段结果本来就会被丢弃，不许再白烧一次 GPU 重渲。"""
        res, calls = self.render([MALE, FEMALE], cancel=True)
        self.assertEqual(len(calls), 1, "取消后只调用一次 _synth")
        self.assertEqual(res["picked"], 1)
        self.assertEqual(res["rerender"], "male")                    # 命中了什么仍记下来
        self.assertEqual(res["rerender_skipped"], "skipped_cancelled")

    def test_failed_rerender_keeps_first_result(self):
        """重渲炸了不能连累已经拿到的第一段：照常出声，日志记下原因。"""
        calls = []

        def synth(u, out_wav):
            calls.append(dict(u))
            if len(calls) == 2:
                raise RuntimeError("boom")
            return SR, MALE

        q = RQ.RenderQueue(synth=synth, encode=lambda *a: b"wav")
        sid, gen = q.open("albion")
        res = q._render_one(SimpleNamespace(unit=unit(), session_id=sid, generation=gen))
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["picked"], 1)
        self.assertIn("RuntimeError: boom", res["retry_error"] or "")
        self.assertTrue(np.array_equal(res["wave"], MALE))

    def test_other_ship_skips_male_guard_but_keeps_tail_guard(self):
        res, calls = self.render([MALE], voice="taihou")
        self.assertEqual(len(calls), 1)
        self.assertFalse(res["quality"][0]["male"], "男声判据只对 albion 生效")
        res, calls = self.render([CLIPPED, FEMALE], voice="taihou")
        self.assertEqual(len(calls), 2, "截尾判据与船无关")
        self.assertEqual(res["picked"], 2)


class SynthPassthroughTest(unittest.TestCase):
    """生产合成腿 `run_infer_synth` 必须把队列挑的 seed 真送进 `_run_infer`。"""

    def test_seed_reaches_run_infer(self):
        import os
        import tempfile
        import soundfile as sf
        seen = {}

        def fake(text, caption, cfg, out_wav, ship, seed=None):
            seen["seed"] = seed
            sf.write(out_wav, np.zeros(int(0.1 * SR), dtype="float32"), SR)

        with tempfile.TemporaryDirectory() as d:
            RQ.run_infer_synth(unit(seed=777), os.path.join(d, "o.wav"), fake, "albion")
        self.assertEqual(seen["seed"], 777)


class MetricsTest(unittest.TestCase):
    def test_tail_db_marks_loud_cut_only(self):
        self.assertGreater(RQ._tail_db(CLIPPED, SR), -RQ.TAIL_CLIP_DB)
        self.assertLess(RQ._tail_db(FEMALE, SR), -RQ.TAIL_CLIP_DB)
        self.assertIsNone(RQ._tail_db(np.zeros(100, dtype="float32"), SR))
        self.assertIsNone(RQ._tail_db(FEMALE[:10], SR), "短于探测窗不判")

    def test_f0_median_matches_tone(self):
        self.assertAlmostEqual(RQ._f0_median(MALE, SR), 180.0, delta=3.0)
        self.assertAlmostEqual(RQ._f0_median(FEMALE, SR), 320.0, delta=3.0)
        self.assertIsNone(RQ._f0_median(np.zeros(int(0.5 * SR), dtype="float32"), SR),
                          "无声段不计入，也不许编一个中位数出来")


if __name__ == "__main__":
    unittest.main()
