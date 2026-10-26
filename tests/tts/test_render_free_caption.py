# -*- coding: utf-8 -*-
"""自由描述 opt-in（caption_mode="free"，2026-10-26）。

阶段 2 的第一步：**只让评测台能送训练集外的 caption**，生产路径一条都不动。
这里锁四件事：
  * free 放行任意自由描述（≤120 字、无控制字符）+ 1.5–7.0 任意 cfg；
  * 超长/空/控制字符/越界 cfg/非法族名/非法 caption_mode 都必须显式拒；
  * **不带这个字段时旧行为逐条不变**（码本外 caption 仍 400、cfg 仍须等于族值）；
  * free 单元真的把 caption/cfg 透传进合成腿，且日志里能看出 caption_mode=free。

不联网、不用 GPU：合成腿是假的，编码器返回固定字节。
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

import httpx
from fastapi import FastAPI

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tts", "irodori"))

import emotion_codebook as CB    # noqa: E402
import render_queue as RQ        # noqa: E402

FREE = "恥ずかしそうに、声を潜めて、頬を赤らめながら話す。"


def unit(**extra):
    u = {"unit_id": "u0", "text": "はい、そうですね。", "family": "neutral",
         "caption": CB.A_QUIET, "cfg": 3.0, "codebook_version": CB.CODEBOOK_VERSION_V2}
    u.update(extra)
    return u


def free_unit(**extra):
    u = unit()
    u.update(caption=FREE, cfg=4.2, caption_mode="free")
    u.update(extra)
    return u


class FreeCaptionAccepted(unittest.TestCase):
    def test_free_caption_and_cfg_pass_on_v4(self):
        self.assertIsNone(RQ._check_unit(free_unit(), 0, plan_version=4))

    def test_free_works_on_every_plan_version(self):
        for pv in (2, 3, 4, 5, 6):
            u = free_unit()
            if pv < 4:
                u.pop("codebook_version")          # 旧版本不带码本号
            self.assertIsNone(RQ._check_unit(u, 0, plan_version=pv), "plan v%d 应放行" % pv)

    def test_cfg_range_boundaries(self):
        for cfg in (1.5, 2.0, 3.0, 4.2, 6.0, 7.0):
            self.assertIsNone(RQ._check_unit(free_unit(cfg=cfg), 0, plan_version=4), cfg)

    def test_caption_length_boundary(self):
        self.assertIsNone(RQ._check_unit(free_unit(caption="あ" * 120), 0, plan_version=4))
        self.assertIn("过长", RQ._check_unit(free_unit(caption="あ" * 121), 0, plan_version=4) or "")

    def test_free_caption_may_equal_trained_anchor(self):
        """"自由"也允许正好撞上训练锚 —— 那样就是既有条件的复验，不是新条件。"""
        self.assertIsNone(RQ._check_unit(free_unit(caption=CB.A_NEUTRAL, cfg=2.4),
                                         0, plan_version=4))

    def test_family_still_must_be_legal(self):
        self.assertIn("未知 family", RQ._check_unit(free_unit(family="angry"),
                                                   0, plan_version=4) or "")

    def test_seed_still_validated_in_free_mode(self):
        self.assertIn("seed 非法", RQ._check_unit(free_unit(seed=True), 0, plan_version=4) or "")


class FreeCaptionRejected(unittest.TestCase):
    def _err(self, **extra):
        return RQ._check_unit(free_unit(**extra), 0, plan_version=4) or ""

    def test_empty_caption(self):
        for cap in ("", "   ", "\u3000"):
            self.assertIn("非空字符串", self._err(caption=cap), repr(cap))

    def test_control_characters(self):
        # \n \t \x00 是 Cc；U+200B 零宽、U+202E 双向覆盖是 Cf
        for ch in ("\n", "\t", "\x00", "\u200b", "\u202e"):
            self.assertIn("控制字符", self._err(caption="恥ずかしそうに" + ch + "話す"), repr(ch))

    def test_cfg_out_of_range(self):
        for cfg in (1.49, 9.0, 20.0):
            self.assertIn("cfg 越界", self._err(cfg=cfg), cfg)

    def test_bad_caption_mode(self):
        for mode in ("Free", "free ", "free2", False, 1, "", "FREE"):
            self.assertIn("caption_mode 非法", self._err(caption_mode=mode), repr(mode))

    def test_codebook_version_still_must_match_plan(self):
        err = RQ._check_unit(free_unit(codebook_version=CB.CODEBOOK_VERSION), 0, plan_version=4)
        self.assertIn("不匹配", err or "")

    def test_bad_scene_id(self):
        self.assertIn("scene_id 非法", self._err(scene_id="s1"))

    def test_nan_cfg(self):
        self.assertIn("cfg 非法", self._err(cfg=float("nan")))


class ProductionPathUnchanged(unittest.TestCase):
    """不带 caption_mode：一律走原码本校验（回归的显式守卫）。"""

    def test_out_of_codebook_caption_still_rejected(self):
        err = RQ._check_unit(unit(caption=FREE, cfg=3.0), 0, plan_version=4)
        self.assertIn("码本外", err or "")

    def test_cfg_must_still_equal_family_value(self):
        err = RQ._check_unit(unit(cfg=4.2), 0, plan_version=4)
        self.assertIn("cfg 与该族不符", err or "")

    def test_legal_anchor_still_accepted(self):
        self.assertIsNone(RQ._check_unit(unit(), 0, plan_version=4))

    def test_caption_mode_none_is_not_free(self):
        """显式 null 等于没带 —— 不能顺手把白名单关掉。"""
        err = RQ._check_unit(unit(caption=FREE, caption_mode=None), 0, plan_version=4)
        self.assertIn("码本外", err or "")


class FreeModeFlowsThrough(unittest.IsolatedAsyncioTestCase):
    """free 单元的 caption/cfg 原样进合成腿，日志能看出来。"""

    async def asyncSetUp(self):
        self.seen = {}

        def synth(u, out_wav):
            self.seen.update(u)
            return 16000, [0.1] * 160
        self.q = RQ.RenderQueue(synth=synth, encode=lambda *a: b"wav")

    async def asyncTearDown(self):
        await self.q.aclose()

    async def _drain(self, u):
        sid, gen = self.q.open("albion")
        return [e async for e in self.q.events(sid, gen, [u])]

    async def test_synth_receives_free_caption_unchanged(self):
        chunks = await self._drain(free_unit())
        self.assertTrue(any("audio_chunk" in e for e in chunks))
        self.assertTrue(any("done" in e for e in chunks))
        self.assertEqual(self.seen["caption"], FREE)
        self.assertEqual(self.seen["cfg"], 4.2)
        self.assertEqual(self.seen["caption_mode"], "free")
        self.assertEqual(self.q.stats["ok"], 1)

    async def test_log_marks_free_units_only(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await self._drain(free_unit())
            await self._drain(unit())            # 生产单元：同样的日志行，但没有 free 标记
        log = buf.getvalue()
        self.assertIn("caption_mode=free", log)
        self.assertEqual(log.count("caption_mode=free"), 1)
        self.assertEqual(log.count("[render_queue]"), 6)   # 两段 × (queue/final/encode)


class RunInferPassthrough(unittest.TestCase):
    def test_run_infer_synth_passes_free_caption_and_cfg(self):
        import numpy as np
        import soundfile as sf
        seen = {}

        def fake(text, caption, cfg, out_wav, ship, seed=None):
            seen.update(caption=caption, cfg=cfg, ship=ship)
            sf.write(out_wav, np.zeros(160, dtype="float32"), 16000)

        with tempfile.TemporaryDirectory() as d:
            RQ.run_infer_synth(free_unit(), os.path.join(d, "o.wav"), fake, "albion")
        self.assertEqual(seen["caption"], FREE)
        self.assertEqual(seen["cfg"], 4.2)
        self.assertEqual(seen["ship"], "albion")


class RenderEndpoint(unittest.IsolatedAsyncioTestCase):
    """/v2/render 端到端：free 单元 200，同一句不带字段仍 400。"""

    async def asyncSetUp(self):
        self.q = RQ.RenderQueue(synth=lambda u, p: (16000, [0.1] * 160),
                                encode=lambda *a: b"wav")
        self.app = RQ.install(FastAPI(), self.q, all_ships=["albion"])

    async def asyncTearDown(self):
        await self.q.aclose()

    async def _post(self, client, units, pv=4):
        opened = (await client.post("/v2/render/open", json={"voice": "albion"})).json()
        body = dict(session_id=opened["session_id"], generation=opened["generation"],
                    voice="albion", plan_version=pv, units=units)
        r = await client.post("/v2/render", json=body)
        await client.post("/v2/render/close", json=body)
        return r

    async def test_free_unit_renders_and_production_unit_is_still_rejected(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app),
                                     base_url="http://test") as c:
            ok = await self._post(c, [free_unit()])
            self.assertEqual(ok.status_code, 200, ok.text[:300])
            self.assertIn("audio_chunk", ok.text)
            self.assertIn("done", ok.text)

            bad = await self._post(c, [unit(caption=FREE, cfg=3.0)])
            self.assertEqual(bad.status_code, 400)
            self.assertIn("码本外", bad.json()["error"])

            long_caption = await self._post(c, [free_unit(caption="あ" * 121)])
            self.assertEqual(long_caption.status_code, 400)
            self.assertIn("过长", long_caption.json()["error"])


if __name__ == "__main__":
    unittest.main()
