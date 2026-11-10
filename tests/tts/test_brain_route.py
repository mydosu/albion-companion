# -*- coding: utf-8 -*-
"""翻译首腿跟着脑的模型走：按脑 /health 的 provider 选线路（2026-10-30）。全部离线。

约束：
  · provider=magpie → 打本机 magpie 的 /chat/completions，模型名就用脑那个，思考压到 low；
  · provider=commandcode → 走现有 Command Code 腿 + 脑那个模型；
  · 认不出的 provider / 问不到脑 → 不猜，落回老链（Command Code key → codex）。
"""
import io
import json
import os
import pathlib
import sys
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import bridge


class _Resp(io.BytesIO):
    """够 urlopen 的上下文管理器用：read() 出 body，退出即关。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def health(provider, model):
    return _Resp(json.dumps({"status": "ok", "provider": provider, "model": model}).encode())


class BrainRouteTest(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"TTS_BRAIN_ROUTE": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        bridge.reset_brain_route()

    def tearDown(self):
        bridge.reset_brain_route()

    def test_magpie_route_goes_to_magpie_first(self):
        with patch.object(bridge.urllib.request, "urlopen", return_value=health("magpie", "antigravity/gemini-3.8-flash")), \
                patch.object(bridge, "_ask_magpie", return_value="こんにちは。") as magpie, \
                patch.object(bridge, "_cc_keys", return_value=["k"]) as keys, \
                patch.object(bridge, "_ask") as ask, \
                patch.object(bridge, "_ask_codex") as codex:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        magpie.assert_called_once()
        self.assertEqual(magpie.call_args.args[1], "antigravity/gemini-3.8-flash")
        keys.assert_not_called()
        ask.assert_not_called()
        codex.assert_not_called()

    def test_commandcode_route_uses_command_code_leg(self):
        with patch.object(bridge.urllib.request, "urlopen", return_value=health("commandcode", "deepseek/deepseek-v4-flash")), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value="こんにちは。") as ask, \
                patch.object(bridge, "_ask_codex") as codex:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        self.assertEqual(ask.call_args.args[:4],
                         ("offline-placeholder", "你好。", "deepseek/deepseek-v4-flash", bridge.FALLBACK_BASE))
        codex.assert_not_called()

    def test_unknown_provider_falls_back_to_old_chain(self):
        with patch.object(bridge.urllib.request, "urlopen", return_value=health("grokpool", "grok-4.7")), \
                patch.object(bridge, "_ask_magpie") as magpie, \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value="こんにちは。") as ask:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        magpie.assert_not_called()
        self.assertEqual(ask.call_args.args[2], bridge.FALLBACK_MODEL)

    def test_brain_down_falls_back_without_crash(self):
        with patch.object(bridge.urllib.request, "urlopen", side_effect=OSError("refused")), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value="こんにちは。") as ask:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        self.assertEqual(ask.call_args.args[2], bridge.FALLBACK_MODEL)

    def test_route_is_cached_for_ttl(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(1)
            return health("magpie", "antigravity/gemini-3.8-flash")

        with patch.object(bridge.urllib.request, "urlopen", side_effect=fake):
            self.assertEqual(bridge.brain_route(now=1000.0)["model"], "antigravity/gemini-3.8-flash")
            self.assertEqual(bridge.brain_route(now=1000.0 + bridge.BRAIN_TTL - 1)["model"],
                             "antigravity/gemini-3.8-flash")
        self.assertEqual(len(calls), 1)

    def test_magpie_compress_rejected_does_not_retry_plain(self):
        """压思考被拒（HTTP 400）⇒ **不**原样重跑同一条腿，直接让调用方换腿（2026-11-10）。"""
        seen = []

        def fake(req, timeout=0):
            seen.append(json.loads(req.data.decode("utf-8")))
            raise urllib.error.HTTPError("http://magpie/chat/completions", 400, "Bad Request", {}, None)

        with patch.dict(os.environ, {"TTS_TRANSLATE_MAGPIE_EFFORT": "low"}), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=fake):
            bridge._magpie_down_until = 0.0
            bridge._magpie_compress_down_until = 0.0
            try:
                started = time.time()
                self.assertEqual(bridge._ask_magpie("你好。", "antigravity/gemini-3.8-flash"), "")
                self.assertEqual(len(seen), 1, "只打压思考那次，不再原样重跑（那一跑是不压思考的 11~19s）")
                self.assertEqual(seen[0].get("reasoning_effort"), "low")
                self.assertEqual(seen[0]["model"], "antigravity/gemini-3.8-flash")
                self.assertGreater(bridge._magpie_compress_down_until, started)   # 压思考已冷却
                self.assertLessEqual(bridge._magpie_compress_down_until - started,
                                     bridge.MAGPIE_COMPRESS_DOWN_SEC + 1)
                # 压思考冷却 ≠ 整条腿冷却：下一句还能走不压思考那次
                self.assertEqual(bridge._magpie_down_until, 0.0)
            finally:
                bridge._magpie_down_until = 0.0
                bridge._magpie_compress_down_until = 0.0

    def test_magpie_compress_empty_reply_also_cools_down(self):
        """空译文跟 HTTPError 同一处理：判这条腿没成、冷却压思考，不原样重跑。"""
        seen = []

        def fake(req, timeout=0):
            seen.append(json.loads(req.data.decode("utf-8")))
            return _Resp(json.dumps({"choices": [{"message": {"content": ""}}]}).encode())

        with patch.dict(os.environ, {"TTS_TRANSLATE_MAGPIE_EFFORT": "low"}), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=fake):
            bridge._magpie_down_until = 0.0
            bridge._magpie_compress_down_until = 0.0
            try:
                self.assertEqual(bridge._ask_magpie("你好。", "m"), "")
                self.assertEqual(len(seen), 1)
                self.assertGreater(bridge._magpie_compress_down_until, 0.0)
            finally:
                bridge._magpie_down_until = 0.0
                bridge._magpie_compress_down_until = 0.0

    def test_magpie_cooldown_skips_compress_then_plain_then_down(self):
        """压思考失败 ⇒ 压思考冷却、整腿不冷却；再不压思考那次也失败 ⇒ 整腿冷却 60s。"""
        with patch.dict(os.environ, {"TTS_TRANSLATE_MAGPIE_EFFORT": "low"}), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=OSError("down")) as up:
            bridge._magpie_down_until = 0.0
            bridge._magpie_compress_down_until = 0.0
            try:
                self.assertEqual(bridge._ask_magpie("你好。", "m"), "")      # 压思考那次
                self.assertEqual(up.call_count, 1)
                self.assertGreater(bridge._magpie_compress_down_until, 0.0)
                self.assertEqual(bridge._ask_magpie("再见。", "m"), "")      # 冷却期 → 只走原样
                self.assertEqual(up.call_count, 2)
                self.assertGreater(bridge._magpie_down_until, 0.0)          # 整条腿也冷却了
                self.assertEqual(bridge._ask_magpie("第三句。", "m"), "")   # 整腿冷却 → 一次都不打
                self.assertEqual(up.call_count, 2)
            finally:
                bridge._magpie_down_until = 0.0
                bridge._magpie_compress_down_until = 0.0

    def test_magpie_timeout_skips_second_variant_and_cools_down(self):
        """超时同样只打一次，且把压思考冷却掉（两次 40s 会吃光 9885 的 60s 准备窗口）。"""
        with patch.dict(os.environ, {"TTS_TRANSLATE_MAGPIE_EFFORT": "low"}), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=TimeoutError("timed out")) as up:
            bridge._magpie_down_until = 0.0
            bridge._magpie_compress_down_until = 0.0
            try:
                started = time.time()
                self.assertEqual(bridge._ask_magpie("你好。", "m"), "")
                self.assertEqual(up.call_count, 1)          # 只打一次，不试「原样」变体
                self.assertGreater(bridge._magpie_compress_down_until, started)
            finally:
                bridge._magpie_down_until = 0.0
                bridge._magpie_compress_down_until = 0.0

    def test_magpie_urlerror_timeout_also_skips_second_variant(self):
        """urllib 把超时包进 URLError.reason 时同样算超时。"""
        err = urllib.error.URLError(TimeoutError("timed out"))
        with patch.dict(os.environ, {"TTS_TRANSLATE_MAGPIE_EFFORT": "low"}), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=err) as up:
            bridge._magpie_down_until = 0.0
            bridge._magpie_compress_down_until = 0.0
            try:
                self.assertEqual(bridge._ask_magpie("你好。", "m"), "")
                self.assertEqual(up.call_count, 1)
            finally:
                bridge._magpie_down_until = 0.0
                bridge._magpie_compress_down_until = 0.0


REFUSAL_EN = ("I cannot fulfill this request. I am unable to generate or translate "
              "sexually explicit content.")


class RefusalTest(unittest.TestCase):
    """拒绝文本不进 TTS（2026-11）：任一条腿交回英文拒绝/非日语都算这条腿失败，换下一腿。

    生产事故：亲密的句子首腿 Gemini 安全拒绝，返回英文模板；老路径 `to_japanese` 原样接收，
    TTS 就把 `I cannot fulfill this request...` 念了出来（logs/tts_bridge.log 有那条 unit 记录）。
    """

    def setUp(self):
        self._env = patch.dict(os.environ, {"TTS_BRAIN_ROUTE": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        bridge.reset_brain_route()
        self._magpie = bridge._magpie_down_until
        bridge._magpie_down_until = 0.0
        self.addCleanup(setattr, bridge, "_magpie_down_until", self._magpie)
        self._magpie_compress = bridge._magpie_compress_down_until
        bridge._magpie_compress_down_until = 0.0
        self.addCleanup(setattr, bridge, "_magpie_compress_down_until", self._magpie_compress)

    def tearDown(self):
        bridge.reset_brain_route()

    def test_looks_rejected_template_and_non_japanese(self):
        self.assertTrue(bridge._looks_rejected("你好。", REFUSAL_EN))
        self.assertTrue(bridge._looks_rejected("你好。", "I'm sorry, but I can't help with that."))
        self.assertTrue(bridge._looks_rejected("你好。", "I am unable to translate this."))
        self.assertTrue(bridge._looks_rejected("你好。", "Sure, here you go."))   # 有汉字、译文没汉字假名
        self.assertFalse(bridge._looks_rejected("你好。", "こんにちは。"))
        self.assertFalse(bridge._looks_rejected("你好。", "こんにちは。\n元気ですか？"))
        self.assertFalse(bridge._looks_rejected("Hello.", "はい。"))             # 原文没汉字：不按这条判
        self.assertFalse(bridge._looks_rejected("你好。", ""))                   # 空串由调用方换腿
        # 撞上 Sorry 模板但确实译成了日语：不许误杀（整句会静音）
        self.assertFalse(bridge._looks_rejected("Sorry啦，我来晚了", "Sorryね、遅くなりました"))

    def test_sorry_greeting_translation_is_kept(self):
        """「Sorry啦」→「Sorryね」是正常译文：首腿结果直接用，不换腿、不静音。"""
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge.urllib.request, "urlopen",
                             return_value=health("magpie", "antigravity/gemini-3.8-flash")), \
                patch.object(bridge, "_ask_magpie", return_value="Sorryね、遅くなりました"), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask") as ask, \
                patch.object(bridge, "_ask_codex") as codex:
            self.assertEqual(bridge.to_japanese("Sorry啦，我来晚了"), "Sorryね、遅くなりました")
        ask.assert_not_called()
        codex.assert_not_called()

    def test_first_leg_refusal_falls_through_to_next_leg(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge.urllib.request, "urlopen",
                             return_value=health("magpie", "antigravity/gemini-3.8-flash")), \
                patch.object(bridge, "_ask_magpie", return_value=REFUSAL_EN), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value="こんにちは。") as ask, \
                patch.object(bridge, "_ask_codex") as codex, \
                patch("builtins.print") as printed:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        ask.assert_called_once()
        codex.assert_not_called()
        logs = [" ".join(str(a) for a in c.args) for c in printed.call_args_list]
        self.assertTrue(any("首腿被拒（refusal）" in x and "换下一腿" in x for x in logs), logs)

    def test_all_legs_refused_returns_empty_string(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge.urllib.request, "urlopen",
                             return_value=health("magpie", "antigravity/gemini-3.8-flash")), \
                patch.object(bridge, "_ask_magpie", return_value=REFUSAL_EN), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value="I'm sorry, but I can't help with that."), \
                patch.object(bridge, "_ask_codex", return_value=REFUSAL_EN), \
                patch("builtins.print") as printed:
            self.assertEqual(bridge.to_japanese("你好。"), "")
        logs = [" ".join(str(a) for a in c.args) for c in printed.call_args_list]
        self.assertTrue(any("这句不念" in x for x in logs), logs)

    def test_emotion_path_fallback_never_voices_refusal(self):
        """生产那条路的原样复现：to_japanese_ex 首腿被拒 → 退回 to_japanese 也全拒 ⇒ 空串（不念）。"""
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "ask_brain", return_value=REFUSAL_EN), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value=REFUSAL_EN), \
                patch.object(bridge, "_ask_codex", return_value=""), \
                patch("builtins.print"):
            self.assertEqual(bridge.to_japanese_ex("这是个测试句子，别念英文。"), ("", None))

    def test_emotion_first_leg_refusal_falls_to_deepseek_json(self):
        """情绪主路（JSON 那次）首腿被拒 → 直接换 DeepSeek 的 JSON 腿：只问一次 Gemini，译文+情绪都要有。"""
        good = json.dumps({"ja": "今日はいいお天気ですね。", "arousal": 0.3,
                           "intensity": 0.4, "valence": 0.5}, ensure_ascii=False)
        prompt = bridge.TRANSLATE_PROMPT + bridge.EMOTION_PROMPT
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "ask_brain", return_value=REFUSAL_EN) as brain, \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", return_value=good) as ask, \
                patch.object(bridge, "_ask_codex") as codex, \
                patch("builtins.print"):
            ja, emo = bridge.to_japanese_ex("这是个测试句子，别念英文。")
        self.assertEqual(ja, "今日はいいお天気ですね。")
        self.assertIsNotNone(emo)
        self.assertEqual(emo.get("arousal"), 0.3)
        brain.assert_called_once()                       # 拒绝后不再问第二次 Gemini
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[:4],
                         ("offline-placeholder", "这是个测试句子，别念英文。",
                          bridge.FALLBACK_MODEL, bridge.FALLBACK_BASE))
        self.assertEqual(ask.call_args.kwargs.get("prompt"), prompt)
        codex.assert_not_called()

    def test_emotion_first_leg_ok_skips_second_leg(self):
        """首腿正常回 JSON（ja 有日文）→ 不算拒绝，不调 DeepSeek 那条 JSON 腿。"""
        good = json.dumps({"ja": "今日はいいお天気ですね。", "arousal": 0.3}, ensure_ascii=False)
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "ask_brain", return_value=good) as brain, \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask") as ask, \
                patch("builtins.print"):
            ja, emo = bridge.to_japanese_ex("这是个测试句子，别念英文。")
        self.assertEqual(ja, "今日はいいお天気ですね。")
        self.assertIsNotNone(emo)
        self.assertFalse(bridge._looks_rejected("这是个测试句子，别念英文。", good))   # 正常 JSON 不误判
        brain.assert_called_once()
        ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
