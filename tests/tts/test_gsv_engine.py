"""gsv 引擎（中文 Denia）的离线接线：假桥只认 /v1/audio/speech，不碰 /v2/*。

覆盖两件事（都不联网、不用 GPU、不碰生产端口）：
  * tts/stream 网关在 ALBION_TTS_ENGINE=gsv 时：中文原文 → 桥 `/v1/audio/speech(voice=denia)`
    → 整条 WAV 按现有下发格式回壳；**不调** `/v2/prepare`、`/v2/plan`、`/v2/render`；
  * tts/gsv/denia_launch.py 的 `ALBION_GSV_DEVICE` 覆盖：只改 custom 段的 device/is_half，
    写临时 yaml，入库的 tts_infer_denia.yaml 一个字节都不动。
"""
import asyncio
import importlib
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tts/irodori"), str(ROOT / "tts/stream"), str(ROOT / "tts/gsv")]
import denia_launch  # noqa: E402
import stream_server as S  # noqa: E402

YAML = ROOT / "tts" / "gsv" / "tts_infer_denia.yaml"


def wav_bytes(seconds=0.1, rate=24000):
    """一小段合法的 16bit 单声道 WAV（和桥 /v1/audio/speech 回来的一样是整条 wav）。"""
    n = int(rate * seconds)
    return (struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + n * 2, b"WAVE",
                        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", n * 2)
            + b"\x00\x00" * n)


class FakeBridge:
    """假桥：只认 /v1/audio/speech；别的路径一律当失败（等于断言网关没去调 /v2/*）。"""

    def __init__(self, wav=None, status=200):
        self.paths = []
        self.bodies = []
        self.wav = wav if wav is not None else wav_bytes()
        self.status = status

    def __call__(self, request):
        self.paths.append(request.url.path)
        self.bodies.append(json.loads(request.content or b"{}"))
        if request.url.path != "/v1/audio/speech":
            return httpx.Response(500, text="unexpected upstream path %s" % request.url.path)
        if self.status != 200:
            return httpx.Response(self.status, text="gsv down")
        return httpx.Response(200, content=self.wav, headers={"content-type": "audio/wav"})


async def drain_until_finished(sess, timeout=5.0):
    """把单 writer 出站队列里的东西都收下来（测试里没有真的 socket writer）。"""
    events, blobs = [], []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            kind, payload = sess.out.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.01)
            continue
        if kind == "text":
            ev = json.loads(payload)
            events.append(ev)
            if ev.get("event") == "session.finished":
                return events, blobs
        elif kind == "bytes":
            blobs.append(payload)
    raise AssertionError("等不到 session.finished：%r" % ([e.get("event") for e in events],))


async def run_units(engine, text, bridge):
    cfg = S.Config(engine=engine)
    co = S.Coordinator(cfg, transport=httpx.MockTransport(bridge))
    sess = S.Session(cfg, "s", 1, "albion", "wav")
    # 走真的切句器：单元、区间、speech_text 都由 session_text 算（和网关里一致）
    units = list(sess.text.feed(text, time.monotonic()))
    units += list(sess.text.finish(time.monotonic()))
    for unit in units:
        await co._enqueue(sess, unit)
    sess.state = "INPUT_CLOSED"
    dispatcher = asyncio.create_task(co._dispatcher(sess))
    try:
        events, blobs = await drain_until_finished(sess)
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
        await co.aclose()
    return sess, units, events, blobs


class GsvGateway(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_goes_to_bridge_speech_and_wav_is_sent_as_before(self):
        bridge = FakeBridge()
        sess, units, events, blobs = await run_units("gsv", "你好。第二句。", bridge)
        self.assertEqual([u.speech_text for u in units], ["你好。", "第二句。"])

        # 只走了桥的 /v1/audio/speech：没有 /v2/prepare（翻译）、/v2/plan、/v2/render。
        self.assertEqual(bridge.paths, ["/v1/audio/speech", "/v1/audio/speech"])
        self.assertEqual(bridge.bodies, [{"input": "你好。", "voice": "denia"},
                                         {"input": "第二句。", "voice": "denia"}])

        # 壳侧协议没变：display.span → sentence.start → 一个 WAV 二进制帧 → sentence.end。
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["display.span", "sentence.start", "sentence.end",
                                 "display.span", "sentence.start", "sentence.end",
                                 "session.finished"])
        starts = [e["payload"] for e in events if e["event"] == "sentence.start"]
        self.assertEqual([p["text"] for p in starts], ["你好。", "第二句。"])   # 字幕是中文原文
        self.assertTrue(all(p["format"] == "wav" for p in starts))
        ends = [e["payload"] for e in events if e["event"] == "sentence.end"]
        self.assertEqual([p["sub_index"] for p in ends], [0, 0])
        self.assertEqual([p["frames"] for p in ends], [1, 1])
        self.assertEqual([p["samples"] for p in ends], [2400, 2400])
        self.assertEqual([p["sr"] for p in ends], [24000, 24000])

        # 下发字节就是桥回来的整条 WAV，一份音频帧对一个单元。
        self.assertEqual(len(blobs), 2)
        for blob in blobs:
            self.assertTrue(blob.startswith(b"RIFF"))
            self.assertEqual(S.Coordinator._wav_info(blob), (2400, 24000))
        self.assertEqual(sess.counts, {"units": 2, "audio": 2, "no_audio": 0, "failed": 0})
        self.assertEqual(sess.audio_bytes, 2 * len(bridge.wav))
        finished = events[-1]
        self.assertEqual((finished["status"], finished["units"], finished["audio"]),
                         ("complete", 2, 2))

    async def test_no_audio_unit_is_not_sent_to_bridge(self):
        bridge = FakeBridge()
        # 纯心声段：session_text 定成 kind=no_audio，gsv 也不该为它去合成。
        sess, units, events, blobs = await run_units("gsv", "（小声）", bridge)
        self.assertTrue(units and all(u.kind == "no_audio" for u in units))
        self.assertEqual(bridge.paths, [])
        self.assertEqual(blobs, [])
        self.assertIn("unit.no_audio", [e["event"] for e in events])
        self.assertEqual(sess.counts["no_audio"], len(units))

    async def test_bridge_failure_marks_unit_failed_stage_gsv(self):
        bridge = FakeBridge(status=502)
        sess, units, events, blobs = await run_units("gsv", "你好。", bridge)
        failed = [e["payload"] for e in events if e["event"] == "unit.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["stage"], "gsv")
        self.assertEqual(blobs, [])
        self.assertEqual(sess.counts["failed"], 1)
        self.assertEqual(events[-1]["status"], "partial")

    def test_engine_defaults_to_irodori_and_reads_env(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALBION_TTS_ENGINE", None)
            self.assertEqual(S.Config().engine, "irodori")
        with unittest.mock.patch.dict(os.environ, {"ALBION_TTS_ENGINE": "GSV"}):
            self.assertEqual(S.Config().engine, "gsv")
        with unittest.mock.patch.dict(os.environ, {"ALBION_TTS_ENGINE": "nope"}):
            self.assertEqual(S.Config().engine, "irodori")       # 认不出就回落，不静默切引擎


def section(text, name):
    """把一个顶层 yaml 段的行（去缩进）取出来，避免为测试引入 yaml 依赖。"""
    lines = text.splitlines()
    start = lines.index(name + ":")
    body = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        body.append(line.strip())
    return body


class DeniaDeviceOverride(unittest.TestCase):
    def test_only_custom_section_is_rewritten(self):
        src = YAML.read_text(encoding="utf-8")
        out = denia_launch.override_device(src, "cuda", True)
        self.assertIn("device: cuda", section(out, "custom"))
        self.assertIn("is_half: true", section(out, "custom"))
        self.assertEqual(out.count("device: cuda"), 1)             # 只改了 custom 那一段
        for other in ("v1", "v2", "v2ProPlus"):
            self.assertIn("device: cpu", section(out, other))
            self.assertIn("is_half: false", section(out, other))
        self.assertEqual(YAML.read_text(encoding="utf-8"), src)     # 入库的 yaml 不动

    def test_cpu_keeps_half_off(self):
        src = YAML.read_text(encoding="utf-8")
        out = denia_launch.override_device(src, "cpu", False)
        self.assertIn("device: cpu", section(out, "custom"))
        self.assertIn("is_half: false", section(out, "custom"))

    def test_missing_is_half_is_inserted_next_to_device(self):
        text = "custom:\n  device: cpu\n  version: v2ProPlus\nv1:\n  device: cpu\n"
        out = denia_launch.override_device(text, "cuda", True)
        self.assertIn("  device: cuda\n", out)
        self.assertIn("  is_half: true\n", out)
        self.assertEqual(out.count("device: cuda"), 1)
        self.assertIn("v1:\n  device: cpu\n", out)                 # 别的段原样

    def test_prepare_config_writes_temp_copy_and_leaves_source(self):
        with tempfile.TemporaryDirectory(prefix="denia cfg ") as tmp:
            src = Path(tmp) / "tts_infer_denia.yaml"
            src.write_text(YAML.read_text(encoding="utf-8"), encoding="utf-8")
            before = src.read_bytes()
            out = denia_launch.prepare_config(str(src), "cuda", True)
            self.assertNotEqual(out, str(src))
            self.assertIn("device: cuda", Path(out).read_text(encoding="utf-8"))
            self.assertEqual(src.read_bytes(), before)
            Path(out).unlink()

    def test_env_selects_device_and_is_half(self):
        try:
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ALBION_GSV_DEVICE", None)
                mod = importlib.reload(denia_launch)
                self.assertEqual((mod.DEVICE, mod.IS_HALF), ("cuda", True))    # 默认 GPU
            with unittest.mock.patch.dict(os.environ, {"ALBION_GSV_DEVICE": "cpu"}):
                mod = importlib.reload(denia_launch)
                self.assertEqual((mod.DEVICE, mod.IS_HALF), ("cpu", False))
        finally:
            importlib.reload(denia_launch)                                  # 还原模块状态


if __name__ == "__main__":
    unittest.main()
