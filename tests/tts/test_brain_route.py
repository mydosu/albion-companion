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
import unittest
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

    def test_magpie_request_pins_low_effort_then_retries_plain(self):
        seen = []

        def fake(req, timeout=0):
            seen.append(json.loads(req.data.decode("utf-8")))
            if len(seen) == 1:
                raise OSError("boom")
            return _Resp(json.dumps({"choices": [{"message": {"content": "こんにちは。"}}]}).encode())

        with patch.dict(os.environ, {"TTS_TRANSLATE_MAGPIE_EFFORT": "low"}), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=fake), \
                patch.object(bridge, "_magpie_down_until", 0.0):
            bridge._magpie_down_until = 0.0          # 上一个用例可能刚把它冷却
            self.assertEqual(bridge._ask_magpie("你好。", "antigravity/gemini-3.8-flash"), "こんにちは。")
        self.assertEqual(seen[0].get("reasoning_effort"), "low")
        self.assertNotIn("reasoning_effort", seen[1])
        self.assertEqual(seen[0]["model"], "antigravity/gemini-3.8-flash")

    def test_magpie_cooldown_skips_next_calls(self):
        with patch.object(bridge.urllib.request, "urlopen", side_effect=OSError("down")) as up:
            bridge._magpie_down_until = 0.0
            self.assertEqual(bridge._ask_magpie("你好。", "m"), "")
            self.assertEqual(bridge._ask_magpie("再见。", "m"), "")
        self.assertEqual(up.call_count, 2)           # 第一次两条 variant 都打空了才算失败
        bridge._magpie_down_until = 0.0


if __name__ == "__main__":
    unittest.main()
