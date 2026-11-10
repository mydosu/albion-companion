# -*- coding: utf-8 -*-
"""翻译对冲（2026-11-10）：首腿慢就并行发退路腿，谁先给出合格译文用谁。

背景：2026-11-09 亲密场景单单元 prepare 到 26s（Gemini 压思考被拒 7~9s + 不压思考重跑 11~19s），
比一句台词的播放长度长得多，句子中间就出现 24s 静默。对冲把"最坏 = 首腿最慢"压成
"最坏 ≈ HEDGE_S + 退路腿耗时"。全部离线（腿是带 sleep 的假函数）。
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tts", "irodori"))

import bridge  # noqa: E402

REFUSAL_EN = "I cannot fulfill this request. I am unable to generate that."


def slow(sec, value, calls, tag):
    """一条"腿"：睡 sec 秒再给 value（记录被调用）。"""
    def _f():
        calls.append((tag, time.monotonic()))
        time.sleep(sec)
        return value
    return _f


def leg(src, name, fn):
    """按生产口径包一层 `_leg`（拒绝判定就发生在这一层，`_hedge` 拿到的都是已判过的译文）。"""
    return lambda: bridge._leg(src, name, fn)


class HedgeTest(unittest.TestCase):
    def setUp(self):
        self._hedge = bridge.HEDGE_S
        self.addCleanup(setattr, bridge, "HEDGE_S", self._hedge)

    def test_慢首腿被对冲腿追上(self):
        """首腿 8s、对冲腿 0.2s，HEDGE_S=0.3 ⇒ 0.3+0.2 就拿到结果，不等首腿。"""
        calls = []
        bridge.HEDGE_S = 0.3
        t0 = time.monotonic()
        txt, winner, fired = bridge._hedge(
            "你好。", slow(8.0, "こんにちは。", calls, "first"), slow(0.2, "やあ。", calls, "hedge"))
        took = time.monotonic() - t0
        self.assertEqual(winner, "hedge")
        self.assertTrue(fired)
        self.assertEqual(txt, "やあ。")
        self.assertLess(took, 3.0, "不许等到首腿那 8s 才回来")
        self.assertEqual([c[0] for c in calls], ["first", "hedge"])

    def test_首腿快就不发对冲(self):
        """首腿 HEDGE_S 内回来 ⇒ 对冲腿一次都不许发（平常不变慢、不多打一次上游）。"""
        calls = []
        bridge.HEDGE_S = 5.0
        txt, winner, fired = bridge._hedge(
            "你好。", slow(0.05, "こんにちは。", calls, "first"), slow(0.05, "やあ。", calls, "hedge"))
        self.assertEqual((txt, winner, fired), ("こんにちは。", "first", False))
        self.assertEqual([c[0] for c in calls], ["first"])

    def test_首腿被拒且对冲在路上时等对冲(self):
        """首腿回英文拒绝（判拒 = 空串），对冲已在跑 ⇒ 等它，不再新发一次。"""
        calls = []
        bridge.HEDGE_S = 0.2
        txt, winner, fired = bridge._hedge(
            "你好。",
            leg("你好。", "首腿", slow(0.3, REFUSAL_EN, calls, "first")),
            leg("你好。", "DeepSeek", slow(0.4, "こんにちは。", calls, "hedge")))
        self.assertEqual(winner, "hedge")
        self.assertTrue(fired)
        self.assertEqual(txt, "こんにちは。")
        self.assertEqual([c[0] for c in calls], ["first", "hedge"], "对冲只发一次")

    def test_首腿被拒但对冲还没发就不发对冲(self):
        """首腿在 HEDGE_S 内就被拒 ⇒ fired=False，调用方按老路换下一条腿（不重复打）。"""
        calls = []
        bridge.HEDGE_S = 5.0
        txt, winner, fired = bridge._hedge(
            "你好。",
            leg("你好。", "首腿", slow(0.05, REFUSAL_EN, calls, "first")),
            leg("你好。", "DeepSeek", slow(0.05, "やあ。", calls, "hedge")))
        self.assertEqual((txt, winner, fired), ("", "none", False))
        self.assertEqual([c[0] for c in calls], ["first"])

    def test_两边都空就不算赢(self):
        calls = []
        bridge.HEDGE_S = 0.1
        txt, winner, fired = bridge._hedge(
            "你好。", slow(0.3, "", calls, "first"), slow(0.4, "", calls, "hedge"))
        self.assertEqual((txt, winner, fired), ("", "none", True))

    def test_对冲腿先回但首腿后回也不用首腿(self):
        """已经赢了的对冲腿不再被后来居上的首腿覆盖（先给出合格译文的就是结果）。"""
        calls = []
        bridge.HEDGE_S = 0.1
        txt, winner, _ = bridge._hedge(
            "你好。", slow(1.5, "おそい。", calls, "first"), slow(0.1, "はやい。", calls, "hedge"))
        self.assertEqual((txt, winner), ("はやい。", "hedge"))

    def test_HEDGE_S_为0时关掉对冲(self):
        calls = []
        bridge.HEDGE_S = 0.0
        txt, winner, fired = bridge._hedge(
            "你好。", slow(0.05, "こんにちは。", calls, "first"), slow(0.0, "やあ。", calls, "hedge"))
        self.assertEqual((txt, winner, fired), ("こんにちは。", "first", False))
        self.assertEqual([c[0] for c in calls], ["first"])

    def test_没开退路腿就不对冲(self):
        calls = []
        bridge.HEDGE_S = 0.1
        txt, winner, fired = bridge._hedge(
            "你好。", slow(0.3, "こんにちは。", calls, "first"), slow(0.0, "", calls, "hedge"),
            enabled=False)
        self.assertEqual((txt, winner, fired), ("こんにちは。", "first", False))
        self.assertEqual([c[0] for c in calls], ["first"])

    def test_首腿抛异常等于这条腿没成(self):
        """首腿抛异常（网络层偶发）：当没成，对冲腿照样能接手，绝不把整句炸掉。"""
        calls = []
        bridge.HEDGE_S = 0.1

        def boom():
            time.sleep(0.3)
            raise RuntimeError("boom")

        txt, winner, fired = bridge._hedge("你好。", boom, slow(0.2, "やあ。", calls, "hedge"))
        self.assertEqual((txt, winner, fired), ("やあ。", "hedge", True))


class HedgeWiringTest(unittest.TestCase):
    """接在 `to_japanese_ex` / `to_japanese` 上时也要真的走对冲。"""

    def setUp(self):
        bridge.reset_brain_route()
        self._hedge = bridge.HEDGE_S
        self._key = bridge._translate_key
        self.addCleanup(setattr, bridge, "HEDGE_S", self._hedge)
        self.addCleanup(setattr, bridge, "_translate_key", self._key)
        bridge._translate_key = lambda: "test-key"          # 让"退路腿可用"成立，不读真实 key

    def test_to_japanese_慢首腿由对冲腿接手(self):
        """老链（TRANSLATE_BACKEND=codex 默认）也一样：首腿慢 ⇒ Command Code 那条链并行接手。"""
        bridge.HEDGE_S = 0.2
        seen = []

        def slow_first(text):
            seen.append("first")
            time.sleep(6.0)
            return "おそい。"

        def fast_deep(key, text, model, *a, **kw):
            seen.append("deep")
            return "はやい。"

        with patch.dict(os.environ, {"TTS_TRANSLATE_BACKEND": "codex"}), \
                patch.object(bridge, "ask_brain", side_effect=slow_first), \
                patch.object(bridge, "_cc_keys", return_value=["k"]), \
                patch.object(bridge, "_ask", side_effect=fast_deep):
            t0 = time.monotonic()
            got = bridge.to_japanese("你好。")
            took = time.monotonic() - t0
        self.assertEqual(got, "はやい。")
        self.assertLess(took, 3.0)
        self.assertEqual(seen, ["first", "deep"])

    def test_to_japanese_ex_首腿被拒时由对冲腿给译文和情绪(self):
        bridge.HEDGE_S = 0.2
        js = ('{"ja":"はやい。","arousal":0.4,"intensity":0.5,"valence":0.2,'
              '"phonation":"voiced"}')

        def slow_refusal(text, prompt=None):
            time.sleep(0.5)
            return REFUSAL_EN

        with patch.dict(os.environ, {"TTS_BRAIN_ROUTE": "1"}), \
                patch.object(bridge, "ask_brain", side_effect=slow_refusal), \
                patch.object(bridge, "_ask", return_value=js):
            ja, emo = bridge.to_japanese_ex("你好。")
        self.assertEqual(ja, "はやい。")
        self.assertIsNotNone(emo)
        self.assertAlmostEqual(emo["arousal"], 0.4)


if __name__ == "__main__":
    unittest.main()
