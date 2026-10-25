# -*- coding: utf-8 -*-
"""/v2/health 的 emotion_degraded：情绪翻译主路没有可用 key（如两把都在额度冷却）时为 true；全部离线。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tts/irodori"), str(ROOT / "tts/stream")]
import bridge
import httpx


class EmotionDegradedHealth(unittest.IsolatedAsyncioTestCase):
    async def health(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app), base_url="http://t") as c:
            r = await c.get("/v2/health")
        self.assertEqual(r.status_code, 200)
        return r.json()

    async def test_degraded_follows_key_availability(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "TRANSLATE_ENABLED", True):
            with patch.object(bridge, "_cc_keys", return_value=[]):
                self.assertIs((await self.health())["emotion_degraded"], True)
            with patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]):
                self.assertIs((await self.health())["emotion_degraded"], False)

    async def test_two_keys_in_cooldown_means_degraded(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "TRANSLATE_ENABLED", True), \
                patch.object(bridge, "_fallback_key", return_value="k1"), \
                patch.object(bridge, "_fallback_key2", return_value="k2"), \
                patch.dict(bridge._KEY_DOWN, {"k1": 9e12, "k2": 9e12}):
            self.assertIs((await self.health())["emotion_degraded"], True)

    async def test_emotion_hint_switched_off_is_not_degraded(self):
        with patch.object(bridge, "EMOTION_HINT", False), patch.object(bridge, "_cc_keys", return_value=[]):
            self.assertIs((await self.health())["emotion_degraded"], False)

    async def test_api_backend_uses_translate_key(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "api"), patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "TRANSLATE_ENABLED", True):
            with patch.object(bridge, "_translate_key", return_value=""):
                self.assertIs((await self.health())["emotion_degraded"], True)
            with patch.object(bridge, "_translate_key", return_value="offline-placeholder"):
                self.assertIs((await self.health())["emotion_degraded"], False)


if __name__ == "__main__":
    unittest.main()
