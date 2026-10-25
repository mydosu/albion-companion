"""首单元翻译并发/预取与日志脱敏：离线、无模型和网络。"""
import asyncio
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tts/irodori"), str(ROOT / "tts/stream")]
import bridge
import bridge_v2 as B
import stream_server as S
import session_text as ST


def request(uid="u"):
    return B.PrepareRequest("s", 1, uid, "albion", (0, 6), "第一。第二。", plan_version=4)


class FirstPacket(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_sentences_keep_order_and_global_limit(self):
        lock = threading.Lock()
        active = peak = 0
        overlapped = threading.Event()
        def translate(text):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    overlapped.set()
            try:
                self.assertTrue(overlapped.wait(2), "源句必须真实重叠执行")
                time.sleep(.04 if text.startswith("第一") else .005)
                return "訳" + text, {"valence": .1 if text == "第一。" else .2}
            finally:
                with lock:
                    active -= 1
        svc = B.PrepareService(None, translate_ex=translate)
        results = await asyncio.gather(svc.prepare(request()), svc.prepare(request("v")))
        for result in results:
            self.assertEqual(result.status, "ready")
            self.assertEqual([s["text"] for s in result.ja_segments], ["訳第一", "訳第二"])
        self.assertEqual(peak, 2)
        self.assertEqual(svc.stats["peak_in_flight"], 2)
        self.assertFalse(svc._running)
        self.assertFalse(svc._held)

    async def test_failure_waits_for_other_sentence_and_releases_slots(self):
        started, release = threading.Event(), threading.Event()
        def translate(text):
            if text.startswith("第一"):
                raise RuntimeError("private-error-body")
            started.set()
            release.wait(2)
            return "訳第二。"
        svc = B.PrepareService(translate)
        task = asyncio.create_task(svc.prepare(request()))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            self.assertFalse(task.done())
            self.assertGreaterEqual(svc._in_flight, 1)
        finally:
            release.set()
        result = await task
        self.assertEqual((result.status, result.error), ("failed", "RuntimeError"))
        self.assertEqual(svc._in_flight, 0)
        self.assertFalse(svc._held)

    async def test_current_prepares_during_credit_wait_and_next_during_render(self):
        cfg = S.Config()
        co = S.Coordinator(cfg)
        sess = S.Session(cfg, "s", 1, "albion", "wav")
        first = ST.Unit("first", 1, 0, 3, "第一。", "speech", "hard")
        second = ST.Unit("second", 2, 3, 6, "第二。", "speech", "hard")
        sess.text.text = "第一。第二。"
        prepared, next_started = asyncio.Event(), asyncio.Event()
        calls = []
        async def prepare(s, unit):
            calls.append(unit.unit_id)
            (prepared if unit is first else next_started).set()
            return {"status": "ready", "ja_segments": [{"text": "こんにちは。"}]}
        async def credit(s):
            await asyncio.wait_for(prepared.wait(), 1)
            if len(calls) == 1:
                self.assertEqual(calls, ["first"])
            return 0
        async def render(s, unit, segments, deadline):
            if unit is first:
                await asyncio.wait_for(next_started.wait(), 1)
        co.prepare, co._wait_credit, co.render_unit = prepare, credit, render
        co._finish = AsyncMock()
        await co._enqueue(sess, first)
        await co._enqueue(sess, second)
        sess.state = "INPUT_CLOSED"
        dispatcher = asyncio.create_task(co._dispatcher(sess))
        try:
            for _ in range(100):
                if co._finish.await_count:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(calls, ["first", "second"])
            self.assertEqual(sess.counts["failed"], 0)
            co._finish.assert_awaited_once()
            self.assertFalse(co._T0)
            self.assertFalse(sess._PREP_S)
        finally:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
            await co.aclose()

    async def test_cancel_during_first_prefetch_releases_task_and_timing(self):
        cfg = S.Config()
        co, sess = S.Coordinator(cfg), S.Session(cfg, "s", 1, "albion", "wav")
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def prepare(*a):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def credit(*a):
            await asyncio.Event().wait()
        co.prepare, co._wait_credit = prepare, credit
        await co._enqueue(sess, ST.Unit("first", 1, 0, 3, "第一。", "speech", "hard"))
        dispatcher = asyncio.create_task(co._dispatcher(sess))
        await asyncio.wait_for(started.wait(), 1)
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(co._T0)
        self.assertIsNone(sess.cur_pf)
        await co.aclose()


class Logs(unittest.IsolatedAsyncioTestCase):
    async def test_request_log_contains_metadata_only(self):
        class Request:
            headers = {}
            async def json(self):
                return {"input": "private-dialogue-私密", "voice": "unknown-private-voice"}
        with patch.object(bridge, "_file_log") as logged:
            await bridge.audio_speech(Request())
        message = logged.call_args.args[1]
        self.assertNotIn("private", message)
        self.assertIn("sha256=", message)
        self.assertIn("len=", message)

    async def test_stream_timestamp_and_text_redaction(self):
        co = S.Coordinator()
        sess = S.Session(co.cfg, "s", 1, "albion", "wav")
        with io.StringIO() as output, contextlib.redirect_stderr(output):
            await co._on_text(sess, "private-dialogue-私密。尾")
            S.log("test")
            data = output.getvalue()
        self.assertNotIn("private-dialogue", data)
        self.assertRegex(data, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIn("sha256=", data)
        await co.aclose()

    async def test_rotating_files_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bridge, "__file__", str(Path(temp) / "tts" / "irodori" / "bridge.py")), \
                    patch.object(bridge, "_FILE_LOGGERS", {}):
                for name in ("_breq.log", "_translate_err.log"):
                    bridge._file_log(name, "start")
                    logger = bridge._FILE_LOGGERS[name]
                    handler = logger.handlers[-1]
                    try:
                        self.assertEqual(handler.maxBytes, 1024 * 1024)
                        self.assertEqual(handler.backupCount, 3)
                        handler.maxBytes = 200
                        for _ in range(20):
                            bridge._file_log(name, bridge.text_meta("private-dialogue"))
                        files = list((Path(temp) / "logs" / "tts").glob(name + "*"))
                        self.assertEqual(len(files), 4)
                        self.assertTrue(all(p.stat().st_size < 200 for p in files))
                        self.assertTrue(all("private-dialogue" not in p.read_text() for p in files))
                    finally:
                        logger.removeHandler(handler)
                        handler.close()


if __name__ == "__main__":
    unittest.main()
