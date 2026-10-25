"""TTS 离线并发/资源回归；替换合成与翻译腿，不联网、不加载模型。"""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))
sys.path.insert(0, str(ROOT / "tts" / "stream"))
import bridge_v2 as B
import render_queue as R
import serve_v2
import stream_server as S
from fastapi import FastAPI
import httpx


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queues = []

    async def asyncTearDown(self):
        for q in self.queues:
            await q.aclose()

    def queue(self, **kwargs):
        q = R.RenderQueue(synth=lambda unit, out: (10, [0.0] * 10),
                          encode=lambda wave, sr, fmt: b"offline audio", **kwargs)
        self.queues.append(q)
        return q

    async def test_closing_one_stream_keeps_other_stream_cancellable(self):
        q = self.queue()
        started, release = threading.Event(), threading.Event()

        def synth(unit, out):
            if unit["unit_id"] == "second":
                started.set()
                if not release.wait(2):
                    raise RuntimeError("test synth not released")
            return 10, [0.0] * 10

        q._synth = synth
        sid, gen = q.open()
        first = q.events(sid, gen, [{"unit_id": "first"}])
        second = q.events(sid, gen, [{"unit_id": "second"}])
        pending = None
        try:
            self.assertIn("audio_chunk", await first.__anext__())
            pending = asyncio.create_task(second.__anext__())
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await first.aclose()
            q.cancel(sid, gen)
            # GPU 还在跑，取消事件必须立刻到达第二条流。
            self.assertIn("render.cancelled", await asyncio.wait_for(pending, 0.5))
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await first.aclose()
            await second.aclose()
            for _ in range(100):
                if q.running is None:
                    break
                await asyncio.sleep(0.01)
        self.assertFalse(q._cancel_events)
        self.assertFalse(q._watch_refs)

    async def test_cancelled_and_withdrawn_jobs_leave_no_timing_records(self):
        q = self.queue()
        with patch.object(q, "_ensure_worker"):
            for i in range(100):
                sid, gen = q.open()
                fut = q.submit(sid, gen, {"unit_id": "u"})
                q.cancel(sid, gen)
                self.assertEqual((await fut)["status"], "cancelled")
                sid, gen = q.open()
                fut = q.submit(sid, gen, {"unit_id": "u"})
                self.assertTrue(q.withdraw(fut))
                self.assertEqual((await fut)["status"], "cancelled")
                q.close(sid, gen)
        self.assertEqual(q.queued, 0)
        self.assertFalse(getattr(R, "_QAT", {}))  # 历史全局计时表曾每次撤回泄漏一项。

    async def test_synthesis_failure_cleans_temporary_files_and_queue_continues(self):
        with tempfile.TemporaryDirectory() as root:
            q = self.queue(tmp_root=root)

            def synth(unit, out):
                Path(out).write_bytes(b"partial")
                if unit["unit_id"] == "bad":
                    raise RuntimeError("synth failed")
                return 10, [0.0] * 10

            q._synth = synth
            sid, gen = q.open()
            self.assertEqual((await q.submit(sid, gen, {"unit_id": "bad"}))["status"], "failed")
            self.assertEqual((await q.submit(sid, gen, {"unit_id": "good"}))["status"], "ok")
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_legacy_routes_keep_contract_and_release_success_ledgers(self):
        q = self.queue()
        app = serve_v2.install(FastAPI(), q)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            response = await c.post("/tts", json={"text": "こんにちは。", "voice": "albion"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(response.headers["x-irodori-ship"], "albion")
            self.assertEqual(response.content, b"offline audio")
            response = await c.post("/tts_stream", json={"text": "こんにちは。", "format": "wav"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("event: audio_chunk", response.text)
            self.assertIn("event: done", response.text)
        self.assertFalse(q._done_units)
        self.assertFalse(q._inflight)

    def request(self, generation=1, text="你好。"):
        return B.PrepareRequest(session_id="s", generation=generation, unit_id="u", voice="albion",
                                source_range=(0, len(text)), speech_text=text, plan_version=4)

    async def test_prepare_deduplicates_only_within_generation_and_releases_sessions(self):
        calls = []

        def translate(text):
            calls.append(text)
            return "訳：" + text

        svc = B.PrepareService(translate)
        a, b = await asyncio.gather(svc.prepare(self.request()),
                                    svc.prepare(self.request(2, "再见。")))
        self.assertEqual(len(calls), 2)
        self.assertEqual((a.status, b.status), ("ready", "ready"))
        self.assertNotEqual(a.ja_segments[0]["text"], b.ja_segments[0]["text"])
        self.assertFalse(svc._held)
        self.assertFalse(svc._running)
        calls.clear()
        await asyncio.gather(svc.prepare(self.request()), svc.prepare(self.request()))
        self.assertEqual(len(calls), 1)
        self.assertFalse(svc._held)

    async def test_cancelled_prepare_waiter_preserves_underlying_translation(self):
        svc = B.PrepareService(lambda text: "unused")
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def translate(req, t0):
            calls.append(req.unit_id)
            started.set()
            await release.wait()
            return B.PrepareResult(unit_id=req.unit_id, status="ready")

        svc._translate_unit = translate
        waiter = asyncio.create_task(svc.prepare(self.request()))
        await asyncio.wait_for(started.wait(), 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        retry = asyncio.create_task(svc.prepare(self.request()))
        release.set()
        self.assertEqual((await retry).status, "ready")
        self.assertEqual(calls, ["u"])
        self.assertFalse(svc._held)

    async def test_application_shutdown_closes_http_pool(self):
        app = S.create_app()
        co = app.state.coordinator
        async with app.router.lifespan_context(app):
            client = co._http()
            self.assertFalse(client.is_closed)
        self.assertTrue(client.is_closed)
        self.assertIsNone(co._client)
        await co.aclose()

    async def test_websocket_handshake_log_redacts_query_token(self):
        import logging
        import uvicorn
        uvicorn.Config(S.create_app(), log_level="info", access_log=False)
        S.redact_uvicorn_logs()
        with self.assertLogs("uvicorn.error", "INFO") as captured:
            logging.getLogger("uvicorn.error").info(
                '%s - "WebSocket %s" [accepted]', "127.0.0.1:5000",
                "/api/v1/audio/speech/ws?token=secret-value&tts_trigger=manual")
        line = captured.output[0]
        self.assertNotIn("secret-value", line)
        self.assertIn("?token=<TOKEN>&tts_trigger=manual", line)

    async def test_render_app_lifespan_closes_queue(self):
        q = self.queue()
        original_close = q.aclose
        close = AsyncMock()
        q.aclose = close
        app = R.install(FastAPI(), q)
        app = serve_v2.install(app, q)
        async with app.router.lifespan_context(app):
            pass
        close.assert_awaited_once_with()
        q.aclose = original_close


if __name__ == "__main__":
    unittest.main()
