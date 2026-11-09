# -*- coding: utf-8 -*-
"""预测时长 + 固定句尾余量（2026-11-08）。

背景：11-08 的 40 句对照实验（`probes/tts_budget_calib.py`）表明——预算就是语速旋钮，模型会把
预测时长吃掉约 95~99%，40 句里 3 句尾部静音只剩 0.14/0.19/0.23s；而 chars/5.0 那条字数下限
只真正生效 1/40。所以按「预测时长 + 固定余量」给预算，拍数假设已被实验否掉。

这里锁 `resolve_latent_steps()`：余量折帧、与字数下限取大、max_frames 封顶、0 等于关、
显式给秒数的请求不进这条路径（那一支在 runtime 里另走，见源码守卫用例）。
"""
import math
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import irodori_tts.inference_runtime as RT  # noqa: E402

SR = 24000
HOP = 1200                      # 1 帧 = 50ms；0.6s 的余量 = 12 帧整


def budget(pred_frames, margin=None, duration_scale=1.0, min_seconds=0.5, max_seconds=30.0,
           sr=SR, hop=HOP):
    return RT.resolve_latent_steps(pred_frames=pred_frames, duration_scale=duration_scale,
                                   min_seconds=min_seconds, max_seconds=max_seconds,
                                   sample_rate=sr, hop_length=hop, margin_s=margin)


def legacy_steps(pred_frames, duration_scale, min_seconds, max_seconds, sr=SR, hop=HOP):
    """改之前的算法（原样重写一遍，用来逐帧对比）。"""
    scaled = pred_frames * duration_scale
    min_frames = max(1, math.ceil(min_seconds * sr / hop))
    max_frames = max(1, math.floor(max_seconds * sr / hop))
    steps = int(round(scaled))
    return max(min_frames, min(max_frames, steps))


class MarginTest(unittest.TestCase):
    def test_margin_adds_expected_frames(self):
        """MARGIN=0.6 ⇒ 步数正好多 ceil(0.6*sr/hop) = 12 帧（这里是整帧，所以是精确值）。"""
        base = budget(100, margin=0.0)["latent_steps"]
        got = budget(100, margin=0.6)["latent_steps"]
        self.assertEqual(base, 100)
        self.assertEqual(got - base, math.ceil(0.6 * SR / HOP))
        self.assertEqual(got, 112)

    def test_margin_zero_matches_legacy_exactly(self):
        """MARGIN=0 与旧行为逐帧一致（扫一批组合，含 duration_scale ≠ 1）。"""
        for pred in (0.4, 3.0, 17.5, 100.0, 100.49, 100.5, 599.0, 5000.0):
            for scale in (1.0, 0.8, 1.25):
                for mn in (0.5, 3.0, 12.0):
                    for mx in (10.0, 30.0, 5.0):
                        with self.subTest(pred=pred, scale=scale, mn=mn, mx=mx):
                            self.assertEqual(
                                budget(pred, margin=0.0, duration_scale=scale,
                                       min_seconds=mn, max_seconds=mx)["latent_steps"],
                                legacy_steps(pred, scale, mn, mx))

    def test_margin_is_capped_by_max_seconds(self):
        got = budget(1000.0, margin=0.6, max_seconds=10.0)     # 预测 50s，封顶 10s = 200 帧
        self.assertEqual(got["max_frames"], 200)
        self.assertEqual(got["latent_steps"], 200)
        # 余量把「贴着上限」的块抬出界也要被压回去
        self.assertEqual(budget(199.0, margin=5.0, max_seconds=10.0)["latent_steps"], 200)

    def test_margin_never_shrinks_a_block(self):
        """余量只加不减：任何预测值下新步数都 ≥ 旧步数，且等于 max(旧, ceil((pred_sec+margin)*sr/hop))。"""
        for pred in (0.4, 10.0, 250.0, 599.0):
            with self.subTest(pred=pred):
                old = legacy_steps(pred, 1.0, 0.5, 30.0)
                got = budget(pred, margin=0.6)["latent_steps"]
                want = min(600, max(old, math.ceil(pred + 0.6 * SR / HOP - 1e-9)))   # 600 帧 = 30s 上限
                self.assertEqual(got, want)
                self.assertGreaterEqual(got, old)
        # 已经贴着 30s 上限的块：加余量也不会超界
        self.assertEqual(budget(600.0, margin=0.6)["latent_steps"],
                         legacy_steps(600.0, 1.0, 0.5, 30.0))

    def test_word_count_floor_still_wins_when_larger(self):
        """字数下限（min_seconds）比预测+余量还大时照旧生效。"""
        got = budget(10.0, margin=0.6, min_seconds=5.0)        # 5s = 100 帧 > 10+12
        self.assertEqual(got["latent_steps"], 100)

    def test_margin_applies_after_duration_scale(self):
        """余量加在「乘过 duration_scale 之后」的预算上，不是原始预测上。"""
        got = budget(100.0, margin=0.6, duration_scale=2.0)
        self.assertEqual(got["latent_steps"], 212)             # 200 + 12，不是 212*2

    def test_env_default_and_off(self):
        old = os.environ.get(RT.PRED_MARGIN_ENV)
        try:
            os.environ.pop(RT.PRED_MARGIN_ENV, None)
            # 2026-11-08 晚：默认值改回 0（0.6s 的 40 句对照证明余量被吃成语速、尾巴没变松）
            self.assertEqual(RT.PRED_MARGIN_DEFAULT_S, 0.0)
            self.assertEqual(RT.pred_margin_seconds(), 0.0)
            self.assertEqual(budget(100)["latent_steps"], legacy_steps(100, 1.0, 0.5, 30.0),
                             "默认（不设环境变量）必须与改之前逐帧一致")
            os.environ[RT.PRED_MARGIN_ENV] = "0.6"
            self.assertAlmostEqual(RT.pred_margin_seconds(), 0.6)
            self.assertEqual(budget(100)["latent_steps"], 112, "开关还在，设了照样生效")
            os.environ[RT.PRED_MARGIN_ENV] = "0"
            self.assertEqual(RT.pred_margin_seconds(), 0.0)
            self.assertEqual(budget(100)["latent_steps"], 100, "0 等于关")
            os.environ[RT.PRED_MARGIN_ENV] = "1.5"
            self.assertAlmostEqual(RT.pred_margin_seconds(), 1.5)
            os.environ[RT.PRED_MARGIN_ENV] = "abc"
            self.assertEqual(RT.pred_margin_seconds(), 0.0, "解不开按关处理，不许炸")
            os.environ[RT.PRED_MARGIN_ENV] = "-3"
            self.assertEqual(RT.pred_margin_seconds(), 0.0)
        finally:
            if old is None:
                os.environ.pop(RT.PRED_MARGIN_ENV, None)
            else:
                os.environ[RT.PRED_MARGIN_ENV] = old


class ManualDurationTest(unittest.TestCase):
    """显式给了秒数的请求走 runtime 里另一条分支（manual_seconds），不该经过余量。

    那一支在 `run()` 深处、没有模型跑不起来，所以这里退一步做**源码守卫**：余量只能出现在
    预测器分支里，`manual_seconds` 分支里不许出现。
    """

    def test_manual_duration_branch_is_untouched(self):
        src = (ROOT / "tts" / "irodori" / "irodori_tts" / "inference_runtime.py").read_text("utf8")
        manual_at = src.index("if manual_seconds is not None:")
        predictor_at = src.index("elif self.model_cfg.use_duration_predictor:")
        call_at = src.index("budget = resolve_latent_steps(")
        self.assertLess(manual_at, predictor_at)
        self.assertGreater(call_at, predictor_at, "余量必须只在预测器分支里")
        manual_block = src[manual_at:predictor_at]
        self.assertNotIn("resolve_latent_steps", manual_block)
        self.assertNotIn("PRED_MARGIN", manual_block)
        # 显式时长那一支的算法保持原样：ceil(target_samples / hop_length)
        self.assertIn("latent_steps = math.ceil(target_samples / hop_length)", manual_block)


if __name__ == "__main__":
    unittest.main()
