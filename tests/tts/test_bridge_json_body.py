# -*- coding: utf-8 -*-
"""非 UTF-8 请求体不再 500（2026-10-30）。

`bridge.py:1143`（`/v1/audio/speech`）原来 `await request.json()` 遇到 GBK 请求体直接
`UnicodeDecodeError` → 500，排查时被误认为「服务挂了」。现在按 Content-Type 的 charset
（缺省 utf-8）解码，失败回退 gb18030，再失败才 400；`/v2/prepare` 走同一个 helper。

不联网、不用 GPU：翻译腿与合成腿都是假的。
"""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import bridge      # noqa: E402
import bridge_v2   # noqa: E402


def gbk(payload):
    return json.dumps(payload, ensure_ascii=False).encode("gb18030")


class SpeechEndpointTest(unittest.IsolatedAsyncioTestCase):
    """/v1/audio/speech：GBK 请求体要能读出来，坏字节回 400 而不是 500。"""

    async def asyncSetUp(self):
        self.seen = []
        app = FastAPI()
        app.post("/v1/audio/speech")(bridge.audio_speech)
        patchers = [
            patch.object(bridge, "_file_log", lambda *a, **k: None),
            patch.object(bridge, "strip_asides_streaming", lambda t: t),
            patch.object(bridge, "synth", lambda text, voice, already_stripped=False:
                         (self.seen.append(text) or bridge.silent_wav(0.05), None)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t")
        self.addAsyncCleanup(self.client.aclose)

    async def test_gbk_body_is_decoded_instead_of_500(self):
        r = await self.client.post(
            "/v1/audio/speech", content=gbk({"input": "快七点了，要是手头的事都忙得差不多了。",
                                             "voice": "albion"}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.seen, ["快七点了，要是手头的事都忙得差不多了。"])

    async def test_declared_gbk_charset_is_used(self):
        r = await self.client.post(
            "/v1/audio/speech", content=gbk({"input": "こんにちは。", "voice": "albion"}),
            headers={"Content-Type": "application/json; charset=gbk"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.seen, ["こんにちは。"])

    async def test_undecodable_body_is_400_not_500(self):
        r = await self.client.post("/v1/audio/speech", content=b"\xff\xff\xff\xff",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("bad request body", r.text)

    async def test_empty_body_is_400(self):
        r = await self.client.post("/v1/audio/speech", content=b"",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)


class PrepareEndpointTest(unittest.IsolatedAsyncioTestCase):
    """/v2/prepare 走同一个 helper：生产翻译主路径的 GBK 请求体也不再 500。"""

    async def asyncSetUp(self):
        app = bridge_v2.create_app(translate=lambda s: "はい、わかりました。")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t")
        self.addAsyncCleanup(self.client.aclose)

    async def test_gbk_body_prepares(self):
        r = await self.client.post("/v2/prepare", content=gbk({
            "session_id": "s1", "generation": 1, "unit_id": "u0", "voice": "albion",
            "source_range": [0, 4], "speech_text": "你好。", "plan_version": 6}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ready", body)
        self.assertEqual([s["text"] for s in body["ja_segments"]], ["はい、わかりました。"])


if __name__ == "__main__":
    unittest.main()
