"""长句准备预算、客户端离开后的名额与 API 默认路由；全部离线。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tts/irodori"), str(ROOT / "tts/stream")]
import bridge
import bridge_v2 as B
import session_text as ST
import stream_server as S
import httpx


def request(uid="first"):
    return B.PrepareRequest("longline", 1, uid, "albion", (0, 3), "你好。")


class PrepareBudget(unittest.IsolatedAsyncioTestCase):
    async def check_timeout(self, cfg, expected):
        def reply(req):
            self.assertEqual(req.extensions["timeout"]["read"], expected)
            return httpx.Response(200, json={"status": "ready"})
        co = S.Coordinator(cfg)
        co._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            sess = S.Session(cfg, "longline", 1, "albion", "wav")
            result = await co.prepare(sess, ST.Unit("first", 1, 0, 3, "你好。", "speech", "hard"))
            self.assertEqual(result["status"], "ready")
        finally:
            await co.aclose()

    async def test_default_prepare_budget_reaches_http_client(self):
        await self.check_timeout(S.Config(), 60.0)

    async def test_explicit_prepare_budget_is_preserved(self):
        await self.check_timeout(S.Config(prepare_timeout=17.0), 17.0)


class PrepareSlots(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.calls = []
        self.svc = B.PrepareService(None, session_window=1)

        async def translate(req, t0):
            self.calls.append(req.unit_id)
            if req.unit_id == "first":
                self.started.set()
                await self.release.wait()
            return B.PrepareResult(req.unit_id, "ready")
        self.svc._translate_unit = translate

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*list(self.svc._running.values()), return_exceptions=True)
        await asyncio.sleep(0)

    async def test_client_timeout_releases_slot_for_next_request(self):
        waiter = asyncio.create_task(asyncio.wait_for(self.svc.prepare(request()), .05))
        await asyncio.wait_for(self.started.wait(), 1)
        with self.assertRaises(asyncio.TimeoutError):
            await waiter
        self.assertTrue(self.svc._running)  # 底层翻译保留，线程限流与同 ID 去重仍有效。
        self.assertFalse(self.svc._held)
        self.assertEqual((await self.svc.prepare(request("next"))).status, "ready")
        retry = asyncio.create_task(self.svc.prepare(request()))
        self.release.set()
        self.assertEqual((await retry).status, "ready")
        self.assertEqual(self.calls, ["first", "next"])
        self.assertFalse(self.svc._held)

    async def test_cancel_one_duplicate_keeps_slot_for_remaining_waiter(self):
        first = asyncio.create_task(self.svc.prepare(request()))
        await asyncio.wait_for(self.started.wait(), 1)
        duplicate = asyncio.create_task(self.svc.prepare(request()))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual((await self.svc.prepare(request("next"))).error, "session_window")
        self.release.set()
        self.assertEqual((await duplicate).status, "ready")
        self.assertFalse(self.svc._held)
        self.assertEqual((await self.svc.prepare(request("next"))).status, "ready")
        self.assertEqual(self.calls, ["first", "next"])

    async def test_http_disconnect_releases_slot_without_cancelling_translation(self):
        app = B.FastAPI()
        service = B.install(app, translate=None, session_window=1)
        service._translate_unit = self.svc._translate_unit
        self.svc = service
        incoming = asyncio.Queue()
        body = dict(session_id="longline", generation=1, unit_id="first", speech_text="你好。")
        await incoming.put({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})
        sent = []
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/v2/prepare", "raw_path": b"/v2/prepare",
                 "query_string": b"", "headers": [(b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1234), "server": ("offline", 80)}
        call = asyncio.create_task(app(scope, incoming.get, send))
        try:
            await asyncio.wait_for(self.started.wait(), 1)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(call), .05)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(call, 1)
            self.assertEqual(sent[0]["status"], 499)
            self.assertFalse(service._held)
            self.assertFalse(service._waiters)
            self.assertTrue(service._running)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
                body["unit_id"] = "next"
                response = await client.post("/v2/prepare", json=body)
            self.assertEqual(response.json()["status"], "ready")
            self.assertEqual(self.calls, ["first", "next"])
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)


class ApiTranslation(unittest.TestCase):
    def setUp(self):
        # 2026-10-30：翻译首腿改成「跟着脑的模型走」（问 127.0.0.1:8650/health）。这些用例要离线，
        # 把那条腿关掉，断言的就是老链（Command Code → codex / DeepSeek 官方）。
        self._offline = patch.dict(os.environ, {"TTS_BRAIN_ROUTE": "0"})
        self._offline.start()
        self.addCleanup(self._offline.stop)
        bridge.reset_brain_route()

    def test_codex_is_default_and_api_can_be_selected(self):
        # 只执行配置赋值，避免重载全局 app 或读取任何真实凭据。
        import ast
        tree = ast.parse((ROOT / "tts/irodori/bridge.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "TRANSLATE_BACKEND" for t in node.targets))
        for env, expected in (({}, "codex"), ({"TTS_TRANSLATE_BACKEND": "api"}, "api")):
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(eval(compile(ast.Expression(assignment.value), "bridge.py", "eval"), {"os": os}), expected)

    def test_default_backend_without_api_key_uses_command_code(self):
        self.assertEqual(bridge.TRANSLATE_BACKEND, "codex")
        with patch.object(bridge, "_translate_key", return_value=""), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]) as keys, \
                patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "_ask", side_effect=["こんにちは。", '{"ja":"こんにちは。","arousal":0.2}']) as ask, \
                patch.object(bridge, "_ask_codex") as codex:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
            self.assertEqual(bridge.to_japanese_ex("你好。")[0], "こんにちは。")
        self.assertEqual(keys.call_count, 2)
        self.assertEqual(ask.call_args_list[0].args, ("offline-placeholder", "你好。", bridge.FALLBACK_MODEL, bridge.FALLBACK_BASE))
        self.assertEqual(ask.call_args_list[1].args, ("offline-placeholder", "你好。", bridge.FALLBACK_MODEL, bridge.FALLBACK_BASE))
        codex.assert_not_called()

    def test_plain_api_translation_never_calls_codex(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "api"), \
                patch.object(bridge, "_translate_key", return_value="offline-placeholder"), \
                patch.object(bridge, "_ask", return_value="こんにちは。") as ask, \
                patch.object(bridge, "_cc_keys") as keys, patch.object(bridge, "_ask_codex") as codex:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        ask.assert_called_once_with("offline-placeholder", "你好。", bridge.TRANSLATE_MODEL)
        keys.assert_not_called()
        codex.assert_not_called()

    def test_emotion_api_translation_uses_api_model_and_endpoint(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "api"), patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "_translate_key", return_value="offline-placeholder"), \
                patch.object(bridge, "_cc_keys") as keys, patch.object(bridge, "_ask_codex") as codex, \
                patch.object(bridge, "_ask", return_value='{"ja":"こんにちは。","arousal":0.2}') as ask:
            ja, _ = bridge.to_japanese_ex("你好。")
        self.assertEqual(ja, "こんにちは。")
        ask.assert_called_once_with("offline-placeholder", "你好。", bridge.TRANSLATE_MODEL,
                                    bridge.TRANSLATE_BASE, prompt=bridge.TRANSLATE_PROMPT + bridge.EMOTION_PROMPT)
        keys.assert_not_called()
        codex.assert_not_called()

    def test_explicit_codex_backend_keeps_existing_fallback(self):
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "_translate_key", return_value=""), \
                patch.object(bridge, "_cc_keys", return_value=[]), \
                patch.object(bridge, "CODEX_API_FALLBACK", True), \
                patch.object(bridge, "_ask_codex", return_value="こんにちは。") as codex:
            self.assertEqual(bridge.to_japanese("你好。"), "こんにちは。")
        codex.assert_called_once_with("你好。")

    def test_api_key_prefers_environment_then_configured_env_file(self):
        with patch.dict(os.environ, {"TTS_TRANSLATE_KEY": "offline-placeholder"}), patch("builtins.open") as opened:
            self.assertEqual(bridge._translate_key(), "offline-placeholder")
            opened.assert_not_called()
        with patch.dict(os.environ, {"TTS_TRANSLATE_KEY": "", "HERMES_ENV_FILE": "offline.env"}), \
                patch("builtins.open", mock_open(read_data='DEEPSEEK_API_KEY="offline-file-placeholder"\n')) as opened:
            self.assertEqual(bridge._translate_key(), "offline-file-placeholder")
            opened.assert_called_once_with("offline.env", encoding="utf-8", errors="ignore")

    def test_bridge_launcher_defaults_preserve_explicit_overrides(self):
        raw = (ROOT / "tts/irodori/启动TTS桥.bat").read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        text = raw.decode("gbk")
        self.assertNotIn("set TTS_TRANSLATE_BACKEND=", text)
        self.assertNotIn('set "TTS_TRANSLATE_BACKEND=', text)
        for name, default in (("BASE", "https://api.deepseek.com/v1"), ("MODEL", "deepseek-flash")):
            self.assertIn('if not defined TTS_TRANSLATE_%s set "TTS_TRANSLATE_%s=%s"' % (name, name, default), text)


if __name__ == "__main__":
    unittest.main()
