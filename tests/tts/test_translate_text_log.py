# -*- coding: utf-8 -*-
"""TTS_LOG_TEXT 开关（2026-10-30）：译文全文只在显式打开时进日志。

背景：「早点歇息」那次错译，日志只有 `len=` + sha256 ⇒ 分不清是翻译偶尔译错，
还是 TTS 把上一句的尾巴串了过来。这里给桥加一行「中文原文 → 日语译文」，
默认关（公开仓库不落聊天原文），TTS_LOG_TEXT=1/true/yes 才写。
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tts", "irodori"))
import bridge_v2  # noqa: E402
import render_queue  # noqa: E402


def mk_req(text, unit_id="u7"):
    return bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id=unit_id, voice="albion",
                                    source_range=(0, len(text)), speech_text=text, plan_version=6)


def run_unit(text):
    """译一个单元，返回这一趟 stdout。首腿模型名打桩：测试不打真脑的 /health。"""
    svc = bridge_v2.PrepareService(translate=lambda s: "（不该走到这条路）",
                                   translate_ex=lambda s: ("もう二時過ぎですよ、早くお休みになりませんか…？",
                                                           {"arousal": 0.3}))
    buf = io.StringIO()
    old_model = bridge_v2._brain_model
    bridge_v2._brain_model = lambda: "test/model-1"
    try:
        with contextlib.redirect_stdout(buf):
            r = asyncio.run(svc._translate_unit(mk_req(text), time.monotonic()))
    finally:
        bridge_v2._brain_model = old_model
    assert r.status == "ready", r.status
    return buf.getvalue()


class TranslateTextLogTest(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("TTS_LOG_TEXT", None)

    def tearDown(self):
        os.environ.pop("TTS_LOG_TEXT", None)
        if self._old is not None:
            os.environ["TTS_LOG_TEXT"] = self._old

    def test_开关关掉时不落原文(self):
        out = run_unit("两点多啦，早点歇息好不好？")
        self.assertNotIn("两点多啦", out, "默认必须不落聊天原文")
        self.assertNotIn("[translate-text]", out)

    def test_开关打开时原文译文都在同一行(self):
        os.environ["TTS_LOG_TEXT"] = "1"
        out = run_unit("两点多啦，早点歇息好不好？")
        line = next((l for l in out.splitlines() if l.startswith("[translate-text]")), "")
        self.assertTrue(line, "开着开关必须有一行 [translate-text]")
        self.assertIn("两点多啦，早点歇息好不好？", line)
        self.assertIn("もう二時過ぎですよ", line)
        self.assertIn("unit=u7", line)
        self.assertIn("model=test/model-1", line)
        self.assertIn("fallback=no", line)      # 带情绪结构 ⇒ 没走退路

    def test_退路可见(self):
        """translate_ex 回 (ja, None) = 桥退回了 to_japanese，日志要能看出来。"""
        os.environ["TTS_LOG_TEXT"] = "true"
        svc = bridge_v2.PrepareService(translate=lambda s: "おやすみなさい。",
                                       translate_ex=lambda s: ("おやすみなさい。", None))
        buf = io.StringIO()
        old_model = bridge_v2._brain_model
        bridge_v2._brain_model = lambda: "test/model-1"
        try:
            with contextlib.redirect_stdout(buf):
                asyncio.run(svc._translate_unit(mk_req("晚安。"), time.monotonic()))
        finally:
            bridge_v2._brain_model = old_model
        self.assertIn("fallback=yes", buf.getvalue())


class SpeechLogTest(unittest.TestCase):
    def test_块序号到日语的记录写UTF8(self):
        old = render_queue._TEXT_LOG
        with tempfile.TemporaryDirectory() as d:
            render_queue._TEXT_LOG = os.path.join(d, "sub", "_speech.log")
            try:
                render_queue.speech_log("u7:1:2:0", "もう二時過ぎですよ。")
                with open(render_queue._TEXT_LOG, encoding="utf-8") as f:
                    line = f.read()
            finally:
                render_queue._TEXT_LOG = old
        self.assertIn("unit=u7:1:2:0", line)
        self.assertIn("もう二時過ぎですよ。", line)

    def test_开关判据(self):
        os.environ.pop("TTS_LOG_TEXT", None)
        self.assertFalse(render_queue.log_text_on())
        for v in ("1", "true", "YES"):
            os.environ["TTS_LOG_TEXT"] = v
            self.assertTrue(render_queue.log_text_on())
        os.environ["TTS_LOG_TEXT"] = "0"
        self.assertFalse(render_queue.log_text_on())
        os.environ.pop("TTS_LOG_TEXT", None)


class BrainModelTest(unittest.TestCase):
    """`_brain_model()` 只能读已加载的模块：生产是 `python bridge.py`（模块名 `__main__`），
    这里 import bridge 会把整个 bridge.py 再导一份、顶层代码重跑。"""

    def test_从__main__取且不触发新import(self):
        fake = types.ModuleType("__main__")
        fake.brain_route = lambda: {"model": "fake/main-9"}
        old_main = sys.modules.get("__main__")
        had_bridge = "bridge" in sys.modules
        old_bridge = sys.modules.pop("bridge", None)     # 别靠已有的 bridge 蒙对
        sys.modules["__main__"] = fake
        try:
            self.assertEqual(bridge_v2._brain_model(), "fake/main-9")
            self.assertNotIn("bridge", sys.modules, "_brain_model 不许 import bridge")
        finally:
            sys.modules["__main__"] = old_main
            if had_bridge and old_bridge is not None:
                sys.modules["bridge"] = old_bridge
            else:
                sys.modules.pop("bridge", None)

    def test_拿不到就回空串(self):
        old_main = sys.modules.get("__main__")
        old_bridge = sys.modules.pop("bridge", None)
        sys.modules["__main__"] = types.ModuleType("__main__")     # 没有 brain_route
        try:
            self.assertEqual(bridge_v2._brain_model(), "")
        finally:
            sys.modules["__main__"] = old_main
            if old_bridge is not None:
                sys.modules["bridge"] = old_bridge

    def test_brain_route抛异常也不影响(self):
        class Boom:
            @staticmethod
            def brain_route():
                raise RuntimeError("脑没起")

        old_main = sys.modules.get("__main__")
        sys.modules["__main__"] = Boom
        try:
            self.assertEqual(bridge_v2._brain_model(), "")
        finally:
            sys.modules["__main__"] = old_main


if __name__ == "__main__":
    unittest.main()
