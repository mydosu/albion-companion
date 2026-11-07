# -*- coding: utf-8 -*-
"""时长下限的单元测试（2026-11-07）。

背景：块内时长预测短了，模型会把句尾整句吞掉（复现：90 字块 15.20s、尾静音 0.00s，
whisper 转写缺最后一句，5 个 seed 全中）。修法是给 `min_seconds` 一个按字数算的下限：
`max(0.5, 朗读字数 / RQ.TTS_MIN_CPS)`，封顶 30s（= SamplingRequest.max_seconds）。

钉住：短句不抬、长句抬到 chars/R、封顶 30 秒、读不出字时退回 0.5。不联网、不用 GPU。
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import render_queue as RQ  # noqa: E402


class MinDurationFloorTest(unittest.TestCase):
    def test_short_sentence_not_lifted(self):
        # 短句：下限 = 字数/R，且必须**不高于**实测时长（2.88s），否则就会改动帧预算
        text = "ディフェンダー、ですね？"
        chars = RQ._spoken_chars(text)
        self.assertEqual(chars, 10, "标点（、？）不计")          # 标点不计
        self.assertEqual(RQ._min_seconds_for(text), chars / RQ.TTS_MIN_CPS)
        self.assertLessEqual(RQ._min_seconds_for(text), 2.88)

    def test_long_sentence_lifted_to_chars_over_r(self):
        text = ("隙あらばバックグラウンドで勝手にディスクを読み込んでCPUを消費したり、"
                "ゲームを遊んでいる時やプログラムを動かしている時にカクつかせられたりすると、"
                "本当にイライラしてしまいますね。")
        chars = RQ._spoken_chars(text)
        self.assertEqual(chars, 87, "整串 90 字里 3 个标点（、。）不计")
        self.assertEqual(RQ._min_seconds_for(text), 87 / RQ.TTS_MIN_CPS)
        # 修前实测 15.20s（尾静音 0.00s、whisper 缺最后一句），下限必须高过它
        self.assertGreater(RQ._min_seconds_for(text), 15.20)

    def test_capped_at_max_seconds(self):
        text = "あ" * 300                       # 300/R = 60s ⇒ 封顶
        self.assertEqual(RQ._min_seconds_for(text), 30.0)
        self.assertLessEqual(RQ._min_seconds_for(text), 30.0)

    def test_no_speakable_chars_falls_back(self):
        for text in ("……", "", None):
            self.assertEqual(RQ._min_seconds_for(text), RQ.TTS_MIN_SECONDS)

    def test_constants_are_documented(self):
        # R 取自实测：完整块 3.85~5.54 字/秒，丢尾块 5.92~6.02 ⇒ 先 5.3，部署复测后降到 5.0
        self.assertTrue(4.5 <= RQ.TTS_MIN_CPS <= 5.6, "R 落在实测区间的中段")
        self.assertEqual(RQ.TTS_MIN_SECONDS, 0.5)
        self.assertEqual(RQ.TTS_MAX_SECONDS, 30.0)


class SynthWiringTest(unittest.TestCase):
    """合成腿的接线：下限与时长报告真的传下去了，返回值第三位带上 floor。"""

    def _wav(self, path):
        import numpy as np
        import soundfile as sf
        sf.write(path, np.zeros(2400, dtype="float32"), 24000, subtype="PCM_16")

    def test_passes_floor_and_reads_report(self):
        import json
        import tempfile
        seen = {}

        def fake_run_infer(text, caption, cfg, out_wav, ship, seed=None,
                           min_seconds=None, duration_report=None):
            seen["min_seconds"] = min_seconds
            seen["report"] = duration_report
            self._wav(out_wav)
            with open(duration_report, "w", encoding="utf8") as f:
                json.dump({"pred_sec": 15.2, "used_sec": 16.42, "min_seconds": min_seconds,
                           "floor": True}, f)

        with tempfile.TemporaryDirectory() as d:
            out = str(pathlib.Path(d) / "seg1.wav")
            sr, wave, floor = RQ.run_infer_synth(
                {"text": "あ" * 90, "voice": "albion"}, out, fake_run_infer, "albion")
            self.assertEqual(round(seen["min_seconds"], 3), round(90 / RQ.TTS_MIN_CPS, 3))
            self.assertTrue(floor)
            self.assertEqual(sr, 24000)
            self.assertEqual(len(wave), 2400)
            self.assertFalse(pathlib.Path(seen["report"]).exists(), "报告读完要删掉")

    def test_old_signature_still_works(self):
        import tempfile

        def old_run_infer(text, caption, cfg, out_wav, ship, seed=None):
            self._wav(out_wav)

        with tempfile.TemporaryDirectory() as d:
            sr, wave, floor = RQ.run_infer_synth(
                {"text": "ディフェンダー、ですね？"}, str(pathlib.Path(d) / "seg1.wav"),
                old_run_infer, "albion")
            self.assertIsNone(floor, "拿不到信息就不标 floor")
            self.assertEqual(sr, 24000)


if __name__ == "__main__":
    unittest.main()


