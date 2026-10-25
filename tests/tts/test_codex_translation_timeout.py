"""常驻翻译进程的真实静默管道超时和离线协议回归。"""
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tts" / "irodori"))
import bridge


class CodexTranslationTimeoutTests(unittest.TestCase):
    def test_missing_codex_returns_immediately_without_subprocess_or_cooldown(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bridge, "CODEX_BIN", str(Path(temp) / "missing.cmd")), \
                patch.object(bridge, "_CODEX_MISSING_LOGGED", False), \
                patch.object(bridge, "_codex_down_until", 0.0), \
                patch.object(bridge, "_ask_codex_once") as once, \
                patch.object(bridge.subprocess, "run") as run, \
                patch.object(bridge.subprocess, "Popen") as popen, \
                patch.object(bridge, "_tlog") as tlog, patch("builtins.print") as logged:
            start = time.monotonic()
            self.assertEqual(bridge._ask_codex("你好。"), "")
            self.assertEqual(bridge._ask_codex("再见。"), "")
            self.assertEqual(bridge._CodexWarm().translate("你好。"), "")
            self.assertEqual(bridge._ask_codex_oneshot("你好。"), "")
            self.assertLess(time.monotonic() - start, 1.0)
            self.assertEqual(bridge._codex_down_until, 0.0)
            once.assert_not_called()
            run.assert_not_called()
            popen.assert_not_called()
            tlog.assert_not_called()
            logged.assert_called_once()

    def test_silent_subprocess_does_not_hold_request_forever(self):
        warm = bridge._CodexWarm()
        warm.proc = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(10)"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        proc = warm.proc
        warm._start_reader()
        reader = warm._reader
        try:
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                warm._call("initialize", {}, 0.05)
            self.assertLess(time.monotonic() - start, 1.0)
        finally:
            warm._kill()
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertFalse(reader.is_alive())
        warm._kill()  # 重复清理安全。

    def warm_with_messages(self, *messages):
        warm = bridge._CodexWarm()
        warm.proc = SimpleNamespace(stdin=io.StringIO(), stdout=True)
        warm.thread = "ready"
        warm._lines = queue.Queue()
        for message in messages:
            warm._lines.put(json.dumps(message))
        return warm

    def translate(self, warm, fallback="fallback"):
        with patch.object(warm, "_call", return_value={"result": {"thread": {"id": "t"}}}), \
             patch.object(warm, "_kill") as kill, \
             patch.object(bridge, "_ask_codex_oneshot", return_value=fallback) as oneshot, \
             patch.object(bridge, "CODEX_TIMEOUT", 0.02):
            result = warm.translate("你好。")
        return result, kill, oneshot

    def test_completed_translation_preserves_final_text(self):
        warm = self.warm_with_messages(
            {"method": "item/agentMessage/delta", "params": {"threadId": "t", "delta": "こん"}},
            {"method": "item/completed", "params": {"threadId": "t", "item": {
                "type": "agentMessage", "text": "こんにちは。"}}},
            {"method": "turn/completed", "params": {"threadId": "t"}})
        result, kill, fallback = self.translate(warm)
        self.assertEqual(result, "こんにちは。")
        kill.assert_not_called()
        fallback.assert_not_called()

    def test_timeout_discards_partial_translation_and_uses_existing_fallback(self):
        warm = self.warm_with_messages(
            {"method": "item/agentMessage/delta", "params": {"threadId": "t", "delta": "未完成"}})
        result, kill, fallback = self.translate(warm)
        self.assertEqual(result, "fallback")
        kill.assert_called_once()
        fallback.assert_called_once_with("你好。")

    def test_failed_turn_never_returns_partial_translation(self):
        for failure in ({"method": "turn/failed", "params": {"threadId": "t"}},
                        {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "failed"}}},
                        {"id": 1, "error": {"message": "failed"}}):
            with self.subTest(failure=failure):
                warm = self.warm_with_messages(
                    {"method": "item/agentMessage/delta", "params": {"threadId": "t", "delta": "未完成"}},
                    failure)
                result, kill, fallback = self.translate(warm)
                self.assertEqual(result, "fallback")
                kill.assert_called_once()
                fallback.assert_called_once()

    def test_pipe_eof_is_reported_immediately(self):
        warm = self.warm_with_messages()
        warm._lines.put(None)
        with self.assertRaisesRegex(RuntimeError, "输出断了"):
            warm._readline(time.monotonic() + 10)


if __name__ == "__main__":
    unittest.main()
