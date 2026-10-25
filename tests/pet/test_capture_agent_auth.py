"""Offline tests for the eye capture endpoint and its fail-closed capture contract."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pet" / "tools" / "eyes"))

import capture_agent as eyes  # noqa: E402


TEST_TOKEN = "unit-test"
JPEG = b"\xff\xd8unit-test-image\xff\xd9"


class CaptureAgentAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = eyes.ThreadingHTTPServer(
            ("127.0.0.1", 0), eyes.make_handler(TEST_TOKEN, 1920)
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.logs = io.StringIO()
        self.stdout = redirect_stdout(self.logs)
        self.stdout.__enter__()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.stdout.__exit__(None, None, None)

    def request(self, target: str, headers: dict[str, str] | None = None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.request("GET", target, headers=headers or {})
        response = conn.getresponse()
        result = response.status, response.getheaders(), response.read()
        conn.close()
        return result

    def test_shot_and_cam_require_bearer_header_and_return_images(self) -> None:
        with patch.object(eyes, "grab_jpeg", return_value=(JPEG, "1x1")) as screen, patch.object(
            eyes, "grab_cam_jpeg", return_value=JPEG
        ) as camera:
            for path in ("/shot", "/cam"):
                with self.subTest(path=path):
                    status, headers, body = self.request(
                        path, {"Authorization": f"Bearer {TEST_TOKEN}"}
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(body, JPEG)
                    self.assertIn(("Content-Type", "image/jpeg"), headers)
            screen.assert_called_once_with(1920)
            camera.assert_called_once_with(1920, rotate=eyes.DEFAULT_CAM_ROTATE)

    def test_query_token_is_not_authentication_and_logs_hide_all_queries(self) -> None:
        with patch.object(eyes, "grab_jpeg") as screen:
            status, _, body = self.request(f"/shot?t={TEST_TOKEN}")
            self.assertEqual(status, 401)
            self.assertNotIn(TEST_TOKEN.encode(), body)
            screen.assert_not_called()

        with patch.object(eyes, "grab_jpeg", return_value=(JPEG, "1x1")):
            self.request(f"/health?diagnostic={TEST_TOKEN}")
        self.assertNotIn("?", self.logs.getvalue())
        self.assertNotIn(TEST_TOKEN, self.logs.getvalue())
        self.assertNotIn("Authorization", self.logs.getvalue())

    def test_bad_bearer_header_is_rejected(self) -> None:
        with patch.object(eyes, "grab_jpeg") as screen:
            for value in ("Basic unit-test", "Bearer wrong", "Bearer "):
                with self.subTest(value=value):
                    status, _, _ = self.request("/shot", {"Authorization": value})
                    self.assertEqual(status, 401)
            screen.assert_not_called()

    def test_non_ascii_bearer_header_returns_401(self) -> None:
        with patch.object(eyes, "grab_jpeg") as screen:
            status, _, _ = self.request("/shot", {"Authorization": "Bearer é"})
        self.assertEqual(status, 401)
        screen.assert_not_called()

    def test_unauthorized_advertises_bearer_scheme(self) -> None:
        with patch.object(eyes, "grab_jpeg") as screen, patch.object(
            eyes, "grab_cam_jpeg"
        ) as camera:
            cases = [
                ("/shot", {}),
                ("/shot", {"Authorization": "Basic unit-test"}),
                ("/shot", {"Authorization": "Bearer wrong"}),
                ("/shot", {"Authorization": "Bearer "}),
                ("/shot", {"Authorization": "Bearer é"}),
                (f"/shot?t={TEST_TOKEN}", {}),
                ("/cam", {}),
                (f"/cam?t={TEST_TOKEN}", {}),
            ]
            for path, headers in cases:
                with self.subTest(path=path, headers=headers):
                    status, response_headers, body = self.request(path, headers)
                    self.assertEqual(status, 401)
                    self.assertIn(("WWW-Authenticate", "Bearer"), response_headers)
                    self.assertIn(b"unauthorized", body)
            screen.assert_not_called()
            camera.assert_not_called()

    def test_capture_errors_are_generic_and_do_not_return_image(self) -> None:
        with patch.object(eyes, "grab_jpeg", side_effect=RuntimeError("private-detail")):
            status, _, body = self.request(
                "/shot", {"Authorization": f"Bearer {TEST_TOKEN}"}
            )
        self.assertEqual(status, 500)
        self.assertIn(b"screen capture unavailable", body)
        self.assertNotIn(b"private-detail", body)
        self.assertNotIn("private-detail", self.logs.getvalue())

    def test_cors_preflight_allows_authorization_header(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.request("OPTIONS", "/shot")
        response = conn.getresponse()
        headers = dict(response.getheaders())
        response.read()
        conn.close()
        self.assertEqual(response.status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Headers"), "Authorization, Content-Type")


class GrabJpegFailClosedTests(unittest.TestCase):
    def test_process_failure_discards_partial_output_and_hides_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / f"albion_shot_{os.getpid()}.jpg"

            def failed_capture(argv, **kwargs):
                out.write_bytes(JPEG)
                return eyes.subprocess.CompletedProcess(argv, 21, "", "private-detail")

            with (
                patch.object(eyes, "IS_WIN", True),
                patch.object(eyes, "_ps_path", None),
                patch.object(eyes.tempfile, "gettempdir", return_value=temp),
                patch.object(eyes.subprocess, "run", side_effect=failed_capture),
                self.assertRaisesRegex(RuntimeError, "screen capture unavailable"),
            ):
                eyes.grab_jpeg()
            self.assertFalse(out.exists())

    def test_success_requires_redaction_marker_and_complete_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / f"albion_shot_{os.getpid()}.jpg"

            def successful_capture(argv, **kwargs):
                out.write_bytes(JPEG)
                return eyes.subprocess.CompletedProcess(
                    argv, 0, "REDACTION_OK|1x1\n", ""
                )

            with (
                patch.object(eyes, "IS_WIN", True),
                patch.object(eyes, "_ps_path", None),
                patch.object(eyes.tempfile, "gettempdir", return_value=temp),
                patch.object(eyes.subprocess, "run", side_effect=successful_capture),
            ):
                data, size = eyes.grab_jpeg()
            self.assertEqual((data, size), (JPEG, "1x1"))
            self.assertFalse(out.exists())

    def test_success_without_marker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / f"albion_shot_{os.getpid()}.jpg"

            def unmarked_capture(argv, **kwargs):
                out.write_bytes(JPEG)
                return eyes.subprocess.CompletedProcess(argv, 0, "1x1\n", "")

            with (
                patch.object(eyes, "IS_WIN", True),
                patch.object(eyes, "_ps_path", None),
                patch.object(eyes.tempfile, "gettempdir", return_value=temp),
                patch.object(eyes.subprocess, "run", side_effect=unmarked_capture),
                self.assertRaisesRegex(RuntimeError, "screen capture unavailable"),
            ):
                eyes.grab_jpeg()
            self.assertFalse(out.exists())

    def test_subprocess_timeout_is_rejected_and_temp_file_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / f"albion_shot_{os.getpid()}.jpg"

            def timed_out(argv, **kwargs):
                out.write_bytes(JPEG)
                raise eyes.subprocess.TimeoutExpired(argv, 60, stderr="private-detail")

            with (
                patch.object(eyes, "IS_WIN", True),
                patch.object(eyes, "_ps_path", None),
                patch.object(eyes.tempfile, "gettempdir", return_value=temp),
                patch.object(eyes.subprocess, "run", side_effect=timed_out),
                self.assertRaisesRegex(RuntimeError, "screen capture unavailable"),
            ):
                eyes.grab_jpeg()
            self.assertFalse(out.exists())


class TokenSourceTests(unittest.TestCase):
    def test_token_file_uses_first_non_empty_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "token.txt"
            path.write_text("﻿\n  -from-file \nignored\n", encoding="utf-8")
            self.assertEqual(eyes.read_token_file(str(path)), "-from-file")
            self.assertEqual(eyes.read_token_file(str(Path(temp) / "missing.txt")), "")
        self.assertEqual(eyes.read_token_file(None), "")

    def test_missing_token_file_refuses_to_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = eyes.subprocess.run(
                [sys.executable, str(ROOT / "pet" / "tools" / "eyes" / "capture_agent.py"),
                 "--token-file", str(Path(temp) / "missing.txt"), "--bind", "127.0.0.1", "--port", "0"],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
                env={**os.environ, eyes.TOKEN_ENV: "", "PYTHONIOENCODING": "utf-8"},
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("token", result.stderr)


if __name__ == "__main__":
    unittest.main()
