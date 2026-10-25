# -*- coding: utf-8 -*-
"""credit 看门狗回归：**纯函数口径 + 闸门接线**。

codex 复核第 7 条记着：只测 `credit_patience` 的话，接线改回「固定 5 秒」这些测试照样全绿。
所以这里用假钟（`asyncio.sleep` 只推假时间）真的走一遍 `Coordinator._wait_credit_body`：

- 段末才 ack 的长段**不该被砍**（2026-09-30「漏句」现场：一段 16.96s 在播着就被砍）；
- 死客户端仍然会被砍，而且等待**有界**（写出终点 + credit_wait），不会挂死；
- 非 WAV 不设闸门（我们对外宣告 `flow_control=false`，再设闸门就是声明一套执行另一套）；
- 上行 ack 是信任边界：inf / 越界 / bool / 负数一律不认（codex 实测 inf+999 曾被原样吃下）。
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tts", "stream"))
import stream_server as SS  # noqa: E402

REAL_SLEEP = asyncio.sleep


class FakeClock:
    """假钟：sleep 只把假时间往前推，测试不用真等十几秒。"""

    def __init__(self, start=1000.0):
        self.now = start
        self.events = []

    def monotonic(self):
        return self.now

    def at(self, dt, fn):
        self.events.append((self.now + dt, fn))

    async def sleep(self, s):
        self.now += max(0.0, s)
        for at, fn in list(self.events):
            if self.now >= at:
                self.events.remove((at, fn))
                fn()
        await REAL_SLEEP(0)


def sess_like(**kw):
    base = dict(id="t", fmt="wav", cancelled=False, credit_timeouts=0, acks=0, credits=0,
                credit_s=15.0, last_ack=None, sent_end_wall=None,
                cfg=SimpleNamespace(max_ahead_s=15.0, max_ahead_units=8, credit_wait=5.0),
                sent_s=20.24, sent_units=2, acked_s=3.28, acked_units=1)
    base.update(kw)
    return SimpleNamespace(**base)


def with_fake_clock(fn):
    """只换模块全局的 time/asyncio/log，不动真 asyncio。"""
    def run(self):
        clock = FakeClock()
        orig = (SS.time, SS.asyncio, SS.log)
        SS.time = SimpleNamespace(monotonic=clock.monotonic)
        SS.asyncio = SimpleNamespace(sleep=clock.sleep)
        SS.log = lambda *a, **k: None
        try:
            fn(self, clock)
        finally:
            SS.time, SS.asyncio, SS.log = orig
    return run


class CreditPatienceTest(unittest.TestCase):
    def test_长段在播期间不算卡死(self):
        # 现场那一回：写出时间轴终点在 12.06s 之后，客户端的段末 ack 12.26s 才到。
        self.assertGreater(SS.credit_patience(5.0, 1012.06, 20.24, 3.28, 1000.0), 12.26)

    def test_有效_ack_重新锚定上界(self):
        # ack 到 20.24s 后只剩 1.0s 未播 ⇒ 上界 = 上次 ack + 1.0s（不再从整轮最初外推）
        self.assertEqual(SS.credit_patience(5.0, None, 21.24, 20.24, 1000.0), 6.0)

    def test_没有已发音频时就是原始宽限(self):
        self.assertEqual(SS.credit_patience(5.0, None, 0.0, 0.0, 1000.0), 5.0)
        self.assertEqual(SS.credit_patience(5.0, 900.0, 1.0, 1.0, 1000.0), 5.0)   # 终点已过


class CreditGateWiringTest(unittest.TestCase):
    @with_fake_clock
    def test_段末才ack的长段不该被砍(self, clock):
        sess = sess_like(sent_end_wall=clock.now + 12.06)

        def client_finishes():
            sess.acked_s, sess.acked_units = 20.24, 2

        clock.at(12.26, client_finishes)                 # 壳只在段末 ack
        asyncio.run(SS.Coordinator._wait_credit_body(None, sess, 4.96, 1))
        self.assertEqual(sess.credit_timeouts, 0)
        # 真的等过了 12 秒才放行 ⇒ 接线若改回固定 5 秒，这里会抛 StalledCredit
        self.assertGreater(clock.now - 1000.0, 12.0)

    @with_fake_clock
    def test_死客户端仍会被砍且等待有界(self, clock):
        sess = sess_like(sent_end_wall=clock.now + 12.06)     # 一个 ack 都不来
        with self.assertRaises(SS.StalledCredit):
            asyncio.run(SS.Coordinator._wait_credit_body(None, sess, 4.96, 1))
        waited = clock.now - 1000.0
        bound = max(12.06, 20.24 - 3.28)                      # 上界 = max(写出终点, 上次 ack + 未播完音频)
        self.assertGreaterEqual(waited, bound + 4.9)          # 覆盖到上界 + credit_wait
        self.assertLessEqual(waited, bound + 5.0 + 1.0)       # 有界，不会挂死
        self.assertEqual(sess.credit_timeouts, 1)

    @with_fake_clock
    def test_非WAV不设闸门(self, clock):
        sess = sess_like(fmt="mp3", sent_end_wall=clock.now + 99.0)
        self.assertEqual(asyncio.run(SS.Coordinator._wait_credit_body(None, sess, 5.0, 1)), 0.0)
        self.assertEqual(clock.now, 1000.0)                   # 一秒都没等


class AckValidationTest(unittest.TestCase):
    def _ack(self, sess, **frame):
        SS.Coordinator._on_feedback(None, sess, "playback.ack", frame)

    def test_非法与越界的_ack_不认(self):
        for bad in ({"seconds": float("inf"), "units": 999},
                    {"seconds": float("nan")},
                    {"seconds": -5, "units": -1},
                    {"seconds": True, "units": True},
                    {"seconds": "x", "units": "2"},
                    {"seconds": 21.9, "units": 3}):           # 超过已发（20.24s / 2 段）不算进度
            sess = sess_like(acked_s=0.0, acked_units=0)
            self._ack(sess, **bad)
            self.assertEqual((sess.acked_s, sess.acked_units), (0.0, 0), bad)

    def test_合法_ack_单调前进(self):
        sess = sess_like(acked_s=0.0, acked_units=0)
        self._ack(sess, seconds=3.28, units=1)
        self._ack(sess, seconds=1.0, units=0)                 # 迟到的不许把进度拨回去
        self.assertEqual((sess.acked_s, sess.acked_units), (3.28, 1))
        self._ack(sess, seconds=20.24, units=2.0)             # JS 可能发 2.0：整数浮点要认
        self.assertEqual((sess.acked_s, sess.acked_units), (20.24, 2))

    def test_极大整数不许把_handler_打崩(self):
        """codex 复核收口项：JSON 的大整数能原样进来（`json.loads` 是任意精度），
        `float(10**400)` 会 OverflowError —— 那会打在 WS 处理里，整条会话跟着死。"""
        sess = sess_like(acked_s=0.0, acked_units=0)
        for bad in ({"seconds": 10 ** 400}, {"units": 10 ** 400}, {"seconds": float("inf"), "units": 10 ** 400}):
            self._ack(sess, **bad)
        self.assertEqual((sess.acked_s, sess.acked_units), (0.0, 0))
        SS.Coordinator._on_feedback(None, sess, "playback.credit", {"seconds": 10 ** 400})
        self.assertEqual(sess.credit_s, 15.0)
        self.assertEqual(sess.credits, 0)          # 非法 credit 不动账

    def test_credit_钳上限且拒非法(self):
        sess = sess_like(credit_s=15.0)
        SS.Coordinator._on_feedback(None, sess, "playback.credit", {"seconds": 99})
        self.assertEqual(sess.credit_s, 15.0)                 # 15s 是硬上限
        SS.Coordinator._on_feedback(None, sess, "playback.credit", {"seconds": 7})
        self.assertEqual(sess.credit_s, 7.0)
        SS.Coordinator._on_feedback(None, sess, "playback.credit", {"seconds": float("inf")})
        self.assertEqual((sess.credit_s, sess.credits), (7.0, 2))   # 非法值不动账


class WavAccountingTest(unittest.TestCase):
    """时长口径是这个链路的信任边界（2026-09-30 我自己的探针就多算了一倍，误判成服务端低估）。

    服务端 `_wav_info` 与探针 `blob_secs` 必须都等于真值：数据字节 ÷ (声道 × 位深/8) ÷ 采样率。
    """

    @staticmethod
    def _wav(secs=3.16, sr=48000, ch=1, bits=16):
        import io
        import struct
        import wave as wavemod
        frames = int(round(secs * sr))
        buf = io.BytesIO()
        with wavemod.open(buf, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(bits // 8)
            w.setframerate(sr)
            w.writeframes(struct.pack("<h", 1200) * (frames * ch))
        return buf.getvalue(), secs

    def test_服务端口径(self):
        blob, secs = self._wav()
        n, sr = SS.Coordinator._wav_info(blob)
        self.assertEqual(sr, 48000)
        self.assertAlmostEqual(n / sr, secs, places=2)

    def test_探针口径跟服务端一致(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "credit_repro.py")
        spec = importlib.util.spec_from_file_location("credit_repro", path)
        cr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cr)
        blob, secs = self._wav()
        got, frames = cr.blob_secs(blob)
        self.assertEqual(frames, 1)
        self.assertAlmostEqual(got, secs, places=2)

    def test_多份子段要逐份求和(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "credit_repro.py")
        spec = importlib.util.spec_from_file_location("credit_repro", path)
        cr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cr)
        a, _ = self._wav(1.0)
        b, _ = self._wav(2.0)
        blob = a + b
        self.assertEqual(cr.blob_secs(blob)[1], 2)
        self.assertAlmostEqual(cr.blob_secs(blob)[0], 3.0, places=2)


if __name__ == "__main__":
    unittest.main()
