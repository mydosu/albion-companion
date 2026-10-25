#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""run_batch.py 的 --out 边界自测（离线、不跑任何套件，几秒结束）。

用法：
    python -I tests/regression/test_run_batch.py

覆盖：默认目录、相对/绝对路径、`..` 收敛、仓库外或 tests/output 之外被拒，
以及命令行在越界和建不出目录时返回 2 且不启动套件。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT_ROOT = ROOT / "tests" / "output"
SCRIPT = HERE / "run_batch.py"


def load_run_batch():
    spec = importlib.util.spec_from_file_location("run_batch_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rb = load_run_batch()


def decode(raw: bytes) -> str:
    """子进程带 -I 时 PYTHONIOENCODING 被忽略，控制台可能是 UTF-8 或 GBK。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class ResolveOutDirTest(unittest.TestCase):
    def test_default_is_timestamp_dir_under_regression(self):
        out = rb.resolve_out_dir(None)
        self.assertEqual(out.parent, OUT_ROOT / "regression")
        self.assertNotEqual(out.name, "regression")

    def test_relative_path_resolves_against_repo_root(self):
        out = rb.resolve_out_dir("tests/output/regression/selftest-relative")
        self.assertEqual(out, OUT_ROOT / "regression" / "selftest-relative")

    def test_absolute_path_inside_allowed_is_kept(self):
        target = OUT_ROOT / "regression" / "selftest-absolute"
        self.assertEqual(rb.resolve_out_dir(str(target)), target)

    def test_dotdot_inside_is_normalized_lexically(self):
        out = rb.resolve_out_dir("tests/output/regression/a/../b")
        self.assertEqual(out, OUT_ROOT / "regression" / "b")

    def test_outside_repo_rejected(self):
        outside = pathlib.Path(tempfile.mkdtemp(prefix="rb-outside-"))
        try:
            with self.assertRaises(rb.OutDirError) as ctx:
                rb.resolve_out_dir(str(outside))
        finally:
            shutil.rmtree(outside, ignore_errors=True)
        self.assertIn("tests/output", str(ctx.exception))

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(rb.OutDirError):
            rb.resolve_out_dir("tests/output/../../escape")

    def test_output_root_itself_rejected(self):
        with self.assertRaises(rb.OutDirError):
            rb.resolve_out_dir(str(OUT_ROOT))


class CliOutDirTest(unittest.TestCase):
    def setUp(self):
        (OUT_ROOT / "regression").mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="selftest-", dir=str(OUT_ROOT / "regression")))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args),
                              cwd=str(ROOT), capture_output=True, env=env)

    def test_outside_out_exits_2_and_runs_no_suite(self):
        outside = pathlib.Path(tempfile.mkdtemp(prefix="rb-outside-cli-"))
        try:
            proc = self.run_cli("--suite", "pet-bat", "--out", str(outside))
            leftovers = sorted(p.name for p in outside.iterdir())
        finally:
            shutil.rmtree(outside, ignore_errors=True)
        text = decode(proc.stdout) + decode(proc.stderr)
        self.assertEqual(proc.returncode, 2, text)
        self.assertIn("error:", text)
        self.assertIn("tests/output", text)
        self.assertNotIn("== pet-bat", text)
        self.assertEqual(leftovers, [])

    def test_uncreatable_out_exits_2(self):
        blocked = self.tmp / "blocked"
        blocked.write_text("not a directory\n", encoding="utf-8")
        proc = self.run_cli("--suite", "pet-bat", "--out", str(blocked / "batch"))
        text = decode(proc.stdout) + decode(proc.stderr)
        self.assertEqual(proc.returncode, 2, text)
        self.assertIn("无法创建输出目录", text)
        self.assertNotIn("== pet-bat", text)


class TimeoutTest(unittest.TestCase):
    def test_timeout_terminates_tree_and_next_suite_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = pathlib.Path(temp)
            child_pid_file = temp_path / "child.pid"
            child_code = "import os,pathlib,time; pathlib.Path({!r}).write_text(str(os.getpid())); time.sleep(60)".format(str(child_pid_file))
            parent_code = (
                "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
                "{!r}]); time.sleep(60)"
            ).format(child_code)
            slow = {
                "name": "slow", "what": "timeout fixture",
                "cmd": lambda py: [py, "-c", parent_code],
                "needs": [], "parse": lambda text: {},
            }
            following = {
                "name": "following", "what": "continuation fixture",
                "cmd": lambda py: [py, "-c", "print('continued')"],
                "needs": [], "parse": lambda text: {"failed": 0},
            }
            out = temp_path / "out"
            out.mkdir()
            patches = (
                mock.patch.object(rb, "ROOT", temp_path),
                mock.patch.object(rb, "OUT_ROOT", temp_path),
                mock.patch.object(rb, "SUITES", [slow, following]),
                mock.patch.object(sys, "argv", ["run_batch.py", "--all", "--python", sys.executable,
                                                  "--timeout", "1", "--out", str(out)]),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                # A patched root keeps test output outside the repository.
                result = rb.main()
            self.assertEqual(result, 1)
            summary = __import__("json").loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([item["status"] for item in summary["suites"]], ["timeout", "pass"])
            self.assertIn("TIMEOUT", (out / "slow.log").read_text(encoding="utf-8"))
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            if os.name == "nt":
                still_running = subprocess.run(
                    ["tasklist", "/FI", "PID eq {}".format(child_pid), "/FO", "CSV", "/NH"],
                    capture_output=True, text=True,
                ).stdout
                self.assertNotIn(str(child_pid), still_running)
            else:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

    def test_node25_probe_accepts_only_node_25(self):
        with mock.patch.object(rb.shutil, "which", return_value="node"), mock.patch.object(
                rb.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"v22.0.0", b"")):
            node, reason = rb.find_node25()
        self.assertIsNone(node)
        self.assertIn("Node 25", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
