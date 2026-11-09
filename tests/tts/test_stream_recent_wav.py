# -*- coding: utf-8 -*-
"""尾部现场环形缓存（2026-11-08）：logs/tts-recent/ 只留最近 40 个整形后 wav。

为什么：11-08 两次「听着断、日志正常」都卡在没留下原音——9889 渲完就把临时 wav 删了，重渲出来
的不是当时那份（caption 每次不同）。这里测的是纯标准库的 wav_ring：落盘、按 mtime 裁旧、失败
只回 None（绝不抛给调用方）。
"""
import os
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "stream"))

import wav_ring as WR  # noqa: E402


class SafeNameTest(unittest.TestCase):
    def test_colon_and_unsafe_chars_become_underscore(self):
        self.assertEqual(WR.safe_name("abc-def_1:2:0"), "abc-def_1_2_0")
        self.assertEqual(WR.safe_name("a/b\\c*d?.wav"), "a_b_c_d_.wav")
        self.assertEqual(WR.safe_name(""), "x", "空名字也要给个能落盘的")
        self.assertEqual(WR.safe_name("あいう"), "___")


class RingTest(unittest.TestCase):
    def test_keeps_only_the_newest_n(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(45):
                p = WR.save(d, "u%02d" % i, b"RIFF" + bytes([i]))
                self.assertIsNotNone(p, "第 %d 个没存下来" % i)
                os.utime(p, (1000 + i, 1000 + i))       # 明确 mtime 顺序，别靠时钟精度
            left = sorted(n for n in os.listdir(d) if n.endswith(".wav"))
            self.assertEqual(len(left), WR.KEEP, "只留 40 个")
            self.assertEqual(left[0], "u05.wav", "最旧的 5 个被删")
            self.assertEqual(left[-1], "u44.wav", "最新的一定还在")
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [],
                             "不能留下半截 .tmp")

    def test_custom_keep_and_protect(self):
        with tempfile.TemporaryDirectory() as d:
            first = WR.save(d, "keepme", b"RIFF0")
            os.utime(first, (1, 1))                     # 故意做得最旧
            for i in range(5):
                p = WR.save(d, "n%d" % i, b"RIFF1")
                os.utime(p, (100 + i, 100 + i))
            removed = WR.prune(d, keep=2, protect=first)
            self.assertEqual(len(removed), 3)
            self.assertTrue(os.path.exists(first), "protect 的那个永不删")

    def test_failure_returns_none_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "not_a_dir")
            with open(blocker, "wb") as f:
                f.write(b"x")
            self.assertIsNone(WR.save(blocker, "u0", b"RIFF"), "目录建不出来 ⇒ None，不抛")
            self.assertEqual(WR.prune(os.path.join(d, "missing"), 5), [])

    def test_prune_survives_files_vanishing(self):
        """列目录和 stat 之间文件被删：跳过就行，不许抛（生产里另一条会话可能同时在裁）。"""
        with tempfile.TemporaryDirectory() as d:
            p = WR.save(d, "u0", b"RIFF")
            os.remove(p)
            self.assertEqual(WR.prune(d, 1), [])

    def test_zero_keep_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            WR.save(d, "u0", b"RIFF")
            self.assertEqual(WR.prune(d, 0), [], "keep<=0 时什么都不删（保守）")
            self.assertEqual(len(os.listdir(d)), 1)


if __name__ == "__main__":
    unittest.main()
