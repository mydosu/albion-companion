"""Backend issued render sessions: replay rejection, bounded retention and real thread races."""
import asyncio
import gc
import json
from pathlib import Path
import sys
import threading
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts/irodori"))
sys.path.insert(0, str(ROOT / "tts/stream"))
import render_queue as R
import serve_v2
import stream_server as S
import httpx
from fastapi import FastAPI


class Sessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.q = R.RenderQueue(synth=lambda u, p: (10, [0.1] * 10), encode=lambda *a: b"wav")

    async def asyncTearDown(self):
        await self.q.aclose()

    def empty(self):
        for key in ("active_sessions", "closing_sessions", "retired_entries", "watchers",
                    "inflight_units", "done_units", "dropped_units"):
            self.assertEqual(self.q.resources()[key], 0, key)
        self.assertFalse(self.q._cancel_events)
        self.assertFalse(self.q._watch_refs)
        self.assertFalse(self.q._jobs)

    async def test_two_100k_rounds_bounded_memory_and_replay(self):
        tracemalloc.start()
        sizes = []
        old = None
        try:
            for _ in range(2):
                for i in range(100000):
                    sid, gen = self.q.open()
                    old = old or (sid, gen)
                    (self.q.close if i % 2 else self.q.cancel)(sid, gen)
                gc.collect()
                sizes.append(tracemalloc.get_traced_memory()[0])
                self.empty()
            self.assertLess(sizes[1] - sizes[0], 1024 * 1024)
            self.assertEqual((await self.q.submit(*old, {"unit_id": "late"}))["status"], "failed")
        finally:
            tracemalloc.stop()

    async def test_dedup_and_multiple_batches(self):
        sid, gen = self.q.open()
        for uid in ("a", "b"):
            chunks = [e async for e in self.q.events(sid, gen, [{"unit_id": uid}])]
            self.assertTrue(any("audio_chunk" in e for e in chunks))
            self.assertEqual((await self.q.submit(sid, gen, {"unit_id": uid}))["status"], "duplicate")
        self.q.close(sid, gen)
        self.empty()

    async def test_expiry_rejects_replay_and_epoch(self):
        now = [0]
        self.q._clock = lambda: now[0]
        sid, gen = self.q.open()
        now[0] = 601
        self.q.sweep()
        self.empty()
        self.assertEqual(self.q.expired_sessions, 1)
        self.assertFalse(self.q.accepts(sid, gen))
        other = R.RenderQueue()
        self.assertFalse(other.accepts(sid, gen))
        fresh = other.open()
        self.assertTrue(other.accepts(*fresh))
        await other.aclose()

    async def test_closed_and_cancelled_immediate_and_unknown_noop(self):
        for action in (self.q.close, self.q.cancel):
            sid, gen = self.q.open()
            self.assertTrue(action(sid, gen))
            self.assertFalse(action(sid, gen))
            for generation in (gen, gen + 1, 999):
                self.assertEqual((await self.q.submit(sid, generation, {}))["status"], "failed")
        self.assertFalse(self.q.cancel("never-opened", 1))
        self.assertFalse(self.q.close("never-opened", 1))
        self.empty()

    async def blocked_job(self, cancel=False, expire=False):
        started, release = threading.Event(), threading.Event()
        def synth(u, p):
            started.set()
            release.wait(3)
            return 10, [0.1] * 10
        self.q._synth = synth
        sid, gen = self.q.open()
        stream = self.q.events(sid, gen, [{"unit_id": "a"}])
        result = asyncio.create_task(stream.__anext__())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            queued = self.q.submit(sid, gen, {"unit_id": "b"}) if cancel else None
            if expire:
                self.q._clock = lambda: 10**15
                self.q.sweep()
            else:
                (self.q.cancel if cancel else self.q.close)(sid, gen)
            self.assertFalse(self.q.accepts(sid, gen))
            self.assertEqual(len(self.q._sessions), 1)  # live GPU owns irreversible state
            if cancel:
                self.assertEqual((await queued)["status"], "cancelled")
                self.assertIn("render.cancelled", await asyncio.wait_for(result, 1))
            release.set()
            if not cancel:
                self.assertIn("audio_chunk", await result)
                self.assertIn("done", await stream.__anext__())
        finally:
            release.set()
            await asyncio.gather(result, return_exceptions=True)
            await stream.aclose()
            for _ in range(200):
                if self.q._running is None:
                    break
                await asyncio.sleep(.01)
        self.empty()

    async def test_close_drains_accepted_gpu_work(self):
        await self.blocked_job()

    async def test_cancel_drops_running_and_queued_work(self):
        await self.blocked_job(cancel=True)

    async def test_expiry_retains_gpu_cancellation(self):
        await self.blocked_job(cancel=True, expire=True)

    async def test_slow_watcher_capacity_and_expiry(self):
        sid, gen = self.q.open()
        ev = self.q.watch(sid, gen)
        self.q.watch(sid, gen)
        self.q.cancel(sid, gen)
        self.assertTrue(ev.is_set())
        self.q.unwatch(sid, gen)
        self.assertEqual(len(self.q._sessions), 1)
        self.q.unwatch(sid, gen)
        self.empty()
        ids = [self.q.open() for _ in range(128)]
        with self.assertRaises(OverflowError):
            self.q.open()
        for address in ids:
            self.q.cancel(*address)
        self.empty()

    async def test_http_contract(self):
        app = R.install(FastAPI(), self.q, all_ships=["albion", "taihou"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
            r = await c.post("/v2/render/open", json={"voice": "albion", "session_id": "old"})
            self.assertEqual(r.status_code, 200)
            opened = r.json()
            self.assertNotEqual(opened["session_id"], "old")
            self.assertEqual(opened["render_lifecycle_version"], 2)
            body = dict(session_id=opened["session_id"], generation=1, voice="albion", units=[{}])
            for route in ("/v2/render", "/v2/render/cancel", "/v2/render/close", "/v2/render/open"):
                self.assertEqual((await c.post(route, json=["invalid"])).status_code, 400)
            for units in (True, "invalid", [None]):
                self.assertEqual((await c.post("/v2/render", json=dict(body, units=units))).status_code, 400)
            for invalid in (True, "1", None):
                self.assertEqual((await c.post("/v2/render", json=dict(body, generation=invalid))).status_code, 400)
            self.assertEqual((await c.post("/v2/render", json=dict(body, voice="taihou"))).status_code, 410)
            await c.post("/v2/render/close", json=body)
            self.assertEqual((await c.post("/v2/render", json=body)).status_code, 410)
            self.assertFalse((await c.post("/v2/render/close", json=body)).json()["closed"])
            self.empty()
            for _ in range(128):
                await c.post("/v2/render/open", json={"voice": "albion"})
            self.assertEqual((await c.post("/v2/render/open", json={"voice": "albion"})).status_code, 429)

    async def test_expiry_also_cancels_closed_slow_watcher(self):
        now = [0]
        self.q._clock = lambda: now[0]
        sid, gen = self.q.open()
        ev = self.q.watch(sid, gen)
        self.q.close(sid, gen)
        now[0] = 601
        self.q.sweep()
        self.assertTrue(ev.is_set())
        self.q.unwatch(sid, gen)
        self.empty()

    async def test_stalled_http_send_closes_generator_and_releases_watcher(self):
        self.q.send_timeout = .02
        app = R.install(FastAPI(), self.q, all_ships=["albion"])
        sid, gen = self.q.open("albion")
        segs, plan, scenes, styles = serve_v2.legacy_plan("こんにちは。")
        unit = serve_v2._seg_unit(0, segs[0], plan, styles, scenes, "albion")
        body = dict(session_id=sid, generation=gen, voice="albion", plan_version=2, units=[unit])
        async def request_json():
            return body
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/v2/render")
        response = await endpoint(SimpleNamespace(json=request_json))
        async def stalled_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await asyncio.Event().wait()
        with self.assertRaises(asyncio.TimeoutError):
            await response.stream_response(stalled_send)
        self.assertEqual(self.q.resources()["watchers"], 0)
        self.q.close(sid, gen)
        self.empty()

    async def test_second_cancellation_cannot_interrupt_watcher_release(self):
        sid, gen = self.q.open()
        stream = self.q.events(sid, gen, [])
        self.assertIn("done", await stream.__anext__())
        # Model a cancelling transport interrupting the cleanup await itself.
        with patch.object(R.asyncio, "gather", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await stream.aclose()
        self.q.close(sid, gen)
        self.empty()

    async def test_legacy_five_voices_success_and_failure_release(self):
        ships = ["albion", "taihou", "shinano", "brest", "jinshi"]
        app = serve_v2.install(FastAPI(), self.q, all_ships=ships)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
            for voice in ships:
                r = await c.post("/tts", json={"text": "こんにちは。", "voice": voice})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["x-irodori-ship"], voice)
                self.empty()
            with patch.object(self.q, "_synth", side_effect=RuntimeError("offline failure")):
                r = await c.post("/tts", json={"text": "こんにちは。"})
                self.assertEqual(r.status_code, 500)
                self.empty()

    async def test_stream_open_once_and_no_reopen_after_epoch_failure(self):
        co = S.Coordinator(S.Config())
        calls = []
        async def handle(req):
            calls.append(req.url.path)
            return httpx.Response(200, json=dict(session_id="issued", generation=1,
                                               epoch="epoch", render_lifecycle_version=2))
        co._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        sess = S.Session(co.cfg, "public", 7, "albion", "wav")
        try:
            results = await asyncio.gather(co.open_render(sess), co.open_render(sess))
            self.assertEqual(results, [("issued", 1)] * 2)
            self.assertEqual(calls, ["/v2/render/open"])
            sess.cancelled = True
            await co.cancel_render(sess)
            with self.assertRaises(RuntimeError):
                await co.open_render(sess)
            self.assertEqual(calls.count("/v2/render/open"), 1)
        finally:
            await co.aclose()


if __name__ == "__main__":
    unittest.main()
