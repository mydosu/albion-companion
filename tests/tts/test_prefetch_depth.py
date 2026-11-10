# -*- coding: utf-8 -*-
"""预取深度（2026-11-10）：9885 从写死 1 改成 `ALBION_PREFETCH_DEPTH`（默认 2），桥侧窗口跟着走。

背景：一个单元的 prepare 会到 10~26s，比一句台词的播放长度还长；深度 1 时第二句就露空档。
全部离线：`prepare` 被换成一个只挂起的假函数，不发网络。
"""
import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tts/irodori"), str(ROOT / "tts/stream")]

import bridge_v2 as B          # noqa: E402
import session_text as ST      # noqa: E402
import stream_server as S      # noqa: E402


def mk_unit(i):
    return ST.Unit("u%d" % i, 1, i * 4, i * 4 + 4, "你好。", "speech", "hard")


class PrefetchDepthTest(unittest.IsolatedAsyncioTestCase):
    async def _prefetch(self, depth, units=4):
        cfg = S.Config(prefetch_depth=depth)
        co = S.Coordinator(cfg)
        gate = asyncio.Event()
        started = []

        async def fake_prepare(sess, unit):
            started.append(unit.unit_id)
            await gate.wait()
            return {"status": "ready", "ja_segments": [{"sub_index": 0, "text": "こんにちは。"}]}

        co.prepare = fake_prepare
        try:
            sess = S.Session(cfg, "s", 1, "albion", "wav")
            for i in range(units):
                sess.units.put_nowait(mk_unit(i))
            co._prefetch_next(sess)
            await asyncio.sleep(0)          # 让建出来的 task 跑到第一句 await
            return [u.unit_id for u, _t in sess.prefetch], started
        finally:
            gate.set()
            await co.aclose()

    async def test_默认深度2(self):
        got, started = await self._prefetch(2)
        self.assertEqual(got, ["u0", "u1"], "深度 2 = 提前翻译两个单元，且保持原文顺序")
        self.assertEqual(started, ["u0", "u1"])

    async def test_深度1等于改前行为(self):
        got, started = await self._prefetch(1)
        self.assertEqual(got, ["u0"])
        self.assertEqual(started, ["u0"])

    async def test_深度0关掉预取(self):
        got, started = await self._prefetch(0)
        self.assertEqual(got, [])
        self.assertEqual(started, [], "深度 0 不许提前翻译")

    async def test_队列不足深度时有多少取多少(self):
        got, _ = await self._prefetch(3, units=2)
        self.assertEqual(got, ["u0", "u1"])

    async def test_从配置读环境变量(self):
        with patch.dict(os.environ, {"ALBION_PREFETCH_DEPTH": "3"}):
            self.assertEqual(S.Config().prefetch_depth, 3)
        with patch.dict(os.environ, {"ALBION_PREFETCH_DEPTH": "0"}):
            self.assertEqual(S.Config().prefetch_depth, 0)
        with patch.dict(os.environ, {"ALBION_PREFETCH_DEPTH": ""}):
            self.assertEqual(S.Config().prefetch_depth, 2, "没配就是默认 2")


class BridgeWindowTest(unittest.TestCase):
    """桥侧窗口必须 ≥ 预取深度+1，否则多出来的预取会被判 `session_window`、整句不念。"""

    def _defaults(self, env):
        code = ("import sys; sys.path.insert(0, %r); import bridge_v2 as B;"
                "print(B.DEFAULT_SESSION_WINDOW, B.DEFAULT_MAX_IN_FLIGHT)"
                % str(ROOT / "tts" / "irodori"))
        e = dict(os.environ)
        e.update(env)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=e, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        window, inflight = out.stdout.split()
        return int(window), int(inflight)

    def test_默认_窗口3_在途2(self):
        self.assertEqual(self._defaults({"ALBION_PREFETCH_DEPTH": ""}), (3, 2))

    def test_深度3时窗口跟着到4(self):
        self.assertEqual(self._defaults({"ALBION_PREFETCH_DEPTH": "3"}), (4, 3))

    def test_显式窗口覆盖(self):
        self.assertEqual(self._defaults({"ALBION_PREFETCH_DEPTH": "2",
                                         "ALBION_SESSION_WINDOW": "6"}), (6, 2))

    def test_窗口恒不小于深度加一(self):
        for depth in (0, 1, 2, 5):
            window, _ = self._defaults({"ALBION_PREFETCH_DEPTH": str(depth)})
            self.assertGreaterEqual(window, depth + 1, "深度 %d 时窗口 %d 太小" % (depth, window))

    def test_服务默认值跟模块常量一致(self):
        svc = B.PrepareService(None)
        self.assertEqual(svc.session_window, B.DEFAULT_SESSION_WINDOW)
        self.assertEqual(svc.max_in_flight, B.DEFAULT_MAX_IN_FLIGHT)


if __name__ == "__main__":
    unittest.main()
