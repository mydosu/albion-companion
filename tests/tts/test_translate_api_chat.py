# -*- coding: utf-8 -*-
"""TTS_TRANSLATE_BACKEND=api 的通用 OpenAI 兼容线路（TTS_TRANSLATE_API_STYLE=chat）：不联网，mock 掉 urlopen。

证明：chat 形态打 <base>/chat/completions、带 Bearer key 与 model、messages 里有 system+user、
能解析 choices[0].message.content；默认形态仍打 <base>/responses（作者行为不变）；
Command Code 退路（别的 endpoint）即使开了 chat 也仍走 /responses；TTS_DEFAULT_VOICE 默认 albion。
"""
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import bridge  # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake(body, seen):
    def urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data.decode("utf-8"))))
        return Resp(json.dumps(body).encode("utf-8"))
    return urlopen


class ApiChatTest(unittest.TestCase):
    def test_chat_style_request(self):
        seen = []
        out = {"choices": [{"message": {"content": "こんにちは。"}}]}
        with patch.object(bridge, "TRANSLATE_API_STYLE", "chat"), \
                patch.object(bridge, "TRANSLATE_BASE", "http://x.test/v1"), \
                patch("urllib.request.urlopen", fake(out, seen)):
            got = bridge._ask("k123", "你好", "m-1")
        self.assertEqual(got, "こんにちは。")
        url, auth, body = seen[0]
        self.assertEqual(url, "http://x.test/v1/chat/completions")
        self.assertEqual(auth, "Bearer k123")
        self.assertEqual(body["model"], "m-1")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "你好")

    def test_default_style_still_responses(self):
        seen = []
        out = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "やあ"}]}]}
        with patch.object(bridge, "TRANSLATE_API_STYLE", "responses"), \
                patch("urllib.request.urlopen", fake(out, seen)):
            got = bridge._ask("k", "你好", "m")
        self.assertEqual(got, "やあ")
        self.assertTrue(seen[0][0].endswith("/responses"))

    def test_other_endpoint_not_hijacked(self):
        seen = []
        out = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "やあ"}]}]}
        with patch.object(bridge, "TRANSLATE_API_STYLE", "chat"), \
                patch("urllib.request.urlopen", fake(out, seen)):
            bridge._ask("k", "你好", "m", endpoint="https://cc.test/provider/v1")
        self.assertEqual(seen[0][0], "https://cc.test/provider/v1/responses")

    def test_default_voice(self):
        self.assertEqual(bridge.DEFAULT_VOICE, "albion")
        self.assertIn(bridge.DEFAULT_VOICE, bridge.VOICES)


if __name__ == "__main__":
    unittest.main()
