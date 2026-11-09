# -*- coding: utf-8 -*-
"""渲完自检与重渲（2026-10-30）：截在响处 / albion 男声漂移 → 换随机 seed 重渲，最多两次。

背景（宣传片录制）：同一条回复里偶发几句 F0 中位 121–148Hz（正常 296–380Hz），另外有些句子的
原始渲染停在响处（末 2ms 还有 -25～-28dBFS）。`render_queue._render_one` 渲完量两样，命中就
换随机 seed 再渲，最多重渲**两次**（共 3 次尝试）；哪次干净就立刻停，三次都不达标时按
「不男声 > 不截尾 > F0 更高」取最好的那次；显式带 seed 的听测请求不重渲。

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


def tone(f0_hz, dur_s=0.5, silence_s=0.5):
    """正弦 + 尾静音（silence_s=0 ⇒ 停在响处，模拟上游截在响）。

    默认尾静音 0.5s（生产正常句尾 0.3~1.1s）：2026-11-08 起「尾静音 < 0.3s」会触发一次拉长
    重渲（tail_tight），原来的 0.06s 会让所有「干净」样本都变成紧尾巴，测试就没法测别的了。
    """
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

    def test_at_most_two_rerenders(self):
        res, calls = self.render([MALE, tone(185.0), FEMALE])
        self.assertEqual(len(calls), 3, "前两次都不达标，最多再渲第三次")
        self.assertEqual(res["picked"], 3, "第三次干净就取第三次")
        self.assertEqual(res["rerender"], "male")
        self.assertTrue(np.array_equal(res["wave"], FEMALE))

    def test_all_three_male_picks_highest_f0(self):
        """三次都男声：按 _rank 取 F0 中位最高的那次（180 < 200 < 230）。"""
        res, calls = self.render([MALE, tone(230.0), tone(200.0)])
        self.assertEqual(len(calls), 3)
        self.assertEqual(res["picked"], 2, "三次全男声时取 230Hz 那次")
        self.assertEqual(res["rerender"], "male")
        self.assertTrue(np.array_equal(res["wave"], tone(230.0)))

    def test_male_outranks_clip(self):
        """「不男声 > 不截尾」：重渲出来不男声但截尾，仍好过男声不截尾。"""
        res, calls = self.render([CLIPPED, MALE])
        self.assertEqual(len(calls), 3, "两次重渲后仍不达标，按 rank 取最好")
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

    def test_male_threshold_catches_half_sentence_drift(self):
        """10-31：门槛 260。中位 230 的半句漂移要拦下来，280 的正常句不许误伤。"""
        res, calls = self.render([tone(230.0), FEMALE])
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["rerender"], "male")
        self.assertTrue(res["quality"][0]["male"])
        self.assertFalse(res["quality"][1]["male"])
        res, calls = self.render([tone(280.0)])
        self.assertEqual(len(calls), 1, "280Hz 落在正常区，不重渲")
        self.assertEqual(res["rerender"], "")
        self.assertFalse(res["quality"][0]["male"])

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


class TailTightTest(GuardCase):
    """尾巴太紧就换 seed 重抽一次（2026-11-08）。

    现场：`s642d032b0fb8:1:2` 渲出 12.32s、尾静音只剩 0.10s、cps 4.3 —— 模型一路念到预算末尾，
    句尾被挤掉，而 cps 没到 5.0，字数下限管不着。判据是**原始音频的尾静音**。

    重渲方式（2026-11-08 晚改）：**同一个预算、换一个新 seed**（`TAIL_STRETCH=1.0`），是拒绝采样
    而不是「多给预算」——40 句对照证明多给预算只会让她念慢（`Δ有声/Δ总长` 中位 0.97），尾巴不一定
    变长；而紧不紧看起来跟着 seed 走。最多一次，取消的不重渲，挑法仍是尾巴更长者。
    """

    def test_tight_tail_rerenders_with_new_seed_same_budget(self):
        tight = tone(320.0, dur_s=4.0, silence_s=0.10)     # 4.00s 有声 + 0.10s 静音
        loose = tone(320.0, dur_s=5.0, silence_s=0.50)
        res, calls = self.render([tight, loose])
        self.assertEqual(len(calls), 2, "尾静音 0.10s < 0.30s ⇒ 必须重渲一次")
        self.assertEqual(res["rerender"], "tail_tight")
        self.assertIsNone(res["rerender_skipped"])
        # 预算不变：还是上一遍那 4.10s（TAIL_STRETCH=1.0），不是 ×1.25 的 5.125
        self.assertEqual(RQ.TAIL_STRETCH, 1.0, "默认必须是同预算的拒绝采样")
        self.assertAlmostEqual(calls[1]["min_seconds"], 4.10 * RQ.TAIL_STRETCH, places=3)
        self.assertNotEqual(calls[0]["seed"], calls[1]["seed"], "第二遍必须是新 seed")
        self.assertEqual(res["picked"], 2, "重渲那份尾巴更长 ⇒ 用它")
        self.assertTrue(np.array_equal(res["wave"], loose))

    def test_loose_tail_is_not_rerendered(self):
        res, calls = self.render([tone(320.0, dur_s=4.0, silence_s=0.50)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["rerender"], "")

    def test_unmeasurable_tail_is_not_rerendered(self):
        """全零波形量不出尾静音（-1）⇒ 不触发，也不许把它当成 0 秒紧尾巴。"""
        res, calls = self.render([np.zeros(int(1.0 * SR), dtype="float32")])
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["rerender"], "")

    def test_both_attempts_tight_picks_longer_tail(self):
        a = tone(320.0, dur_s=4.0, silence_s=0.10)
        b = tone(320.0, dur_s=5.0, silence_s=0.20)
        res, calls = self.render([a, b])
        self.assertEqual(len(calls), 2, "只重渲一次，不许因为仍紧再渲第三次")
        self.assertEqual(res["picked"], 2, "两份都紧时取尾巴更长的那份（0.20 > 0.10）")

    def test_cancelled_block_is_not_rerendered(self):
        res, calls = self.render([tone(320.0, dur_s=4.0, silence_s=0.10)], cancel=True)
        self.assertEqual(len(calls), 1, "取消过的块重渲也没人要")
        self.assertEqual(res["picked"], 1)
        self.assertEqual(res["rerender"], "tail_tight")          # 命中了什么仍记下来
        self.assertEqual(res["rerender_skipped"], "skipped_cancelled")

    def test_explicit_seed_is_not_rerendered_for_tight_tail(self):
        """听测配对给了 seed：那条尾巴是别人量过的条件，不许偷偷换一次。"""
        res, calls = self.render([tone(320.0, dur_s=4.0, silence_s=0.10)], seed=999)
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["rerender"], "")

    def test_male_or_clip_still_uses_the_old_rerender_budget(self):
        """男声/截尾那条路径不变：第一遍命中它就不走 tail_tight 分支。"""
        res, calls = self.render([MALE, FEMALE])
        self.assertEqual(len(calls), 2)
        self.assertEqual(res["rerender"], "male")


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

    def test_tail_profile_counts_voiced_end_and_silence(self):
        """2026-11-08 取证字段：1.5s 有声 + 0.5s 数字零 ⇒ (1.50, 0.50)。"""
        self.assertAlmostEqual(RQ._tail_profile(FEMALE, SR)[0], 0.50, delta=RQ.TAIL_FRAME_S)
        self.assertAlmostEqual(RQ._tail_profile(FEMALE, SR)[1], 0.50, delta=RQ.TAIL_FRAME_S)
        wave = np.concatenate([tone(320.0, dur_s=1.5, silence_s=0.5)])
        voiced_end, silence = RQ._tail_profile(wave, SR)
        self.assertAlmostEqual(voiced_end, 1.50, delta=RQ.TAIL_FRAME_S)
        self.assertAlmostEqual(silence, 0.50, delta=RQ.TAIL_FRAME_S)
        self.assertAlmostEqual(voiced_end + silence, len(wave) / SR, delta=0.02)

    def test_tail_profile_failure_is_minus_one(self):
        """量不出来（全零/空/采样率非法）一律 -1，绝不抛给合成腿。"""
        bad = RQ._tail_profile(np.zeros(100, dtype="float32"), SR)
        self.assertEqual(bad, (RQ.TAIL_PROFILE_FAILED, RQ.TAIL_PROFILE_FAILED))
        self.assertEqual(RQ._tail_profile(np.zeros(10), 0), bad)
        self.assertEqual(RQ._tail_profile(None, SR), bad)


class AudioNoteTest(unittest.TestCase):
    """final 行的音频时长/语速字段（2026-11-03）：secs 用已在手的 wave/sr 算，不重读文件。"""

    @staticmethod
    def _job(text):
        return SimpleNamespace(unit={"text": text})

    def test_secs_and_cps_from_wave(self):
        wave = np.concatenate([tone(320.0, dur_s=1.5, silence_s=0.5)])   # 2.00s
        note = RQ._audio_note(self._job("あいうえお"), {"sr": SR, "wave": wave})
        self.assertEqual(note, "secs=2.00 cps=2.5"
                               " tail_voiced_s=1.50 tail_sil_s=0.50 margin=0.00s")   # 5 字 / 2s

    def test_punct_and_space_not_counted(self):
        note = RQ._audio_note(self._job("あ、い。 う"), {"sr": SR, "wave": np.zeros(SR)})
        self.assertEqual(note, "secs=1.00 cps=3.0 tail_voiced_s=-1.00 tail_sil_s=-1.00"
                               " margin=0.00s",
                         "全零波形量不出有声帧 ⇒ 两个字段都记 -1")

    def test_margin_field_follows_env(self):
        """final 行要能看出这块用的句尾余量（探针靠它核对服务器配置），默认 0。"""
        import os
        wave = tone(320.0, dur_s=1.0, silence_s=0.5)
        old = os.environ.get("TTS_PRED_MARGIN_S")
        try:
            os.environ.pop("TTS_PRED_MARGIN_S", None)
            self.assertTrue(RQ._audio_note(self._job("あ"), {"sr": SR, "wave": wave})
                            .endswith("margin=0.00s"))
            os.environ["TTS_PRED_MARGIN_S"] = "0.6"
            self.assertTrue(RQ._audio_note(self._job("あ"), {"sr": SR, "wave": wave})
                            .endswith("margin=0.60s"))
        finally:
            if old is None:
                os.environ.pop("TTS_PRED_MARGIN_S", None)
            else:
                os.environ["TTS_PRED_MARGIN_S"] = old

    def test_missing_audio_is_silent(self):
        for res in ({"sr": SR}, {"sr": 0, "wave": np.zeros(10)}, {"status": "failed"}, {}):
            with self.subTest(res=res):
                self.assertEqual(RQ._audio_note(self._job("あ"), res), "", "拿不到就跳过，不报错")
        self.assertEqual(RQ._audio_note(self._job(""), {"sr": SR, "wave": np.zeros(SR)}),
                         "secs=1.00 tail_voiced_s=-1.00 tail_sil_s=-1.00 margin=0.00s",
                         "没文本就只报时长")


if __name__ == "__main__":
    unittest.main()
