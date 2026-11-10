# -*- coding: utf-8 -*-
"""直出日语的中文字幕（2026-11-10）：日语单元的字幕要显示日译中，不显示日语。

锁四件事：
  * **判据不漂移**：`stream_server.looks_japanese` 与 `bridge_v2.is_japanese` 逐条一致
    （语音单元用桥给的 `translated=False`，只有无音频单元走本地这份副本）；
  * **不为了字幕卡出声**：渲染出帧后最多等 `caption_wait` 秒，没回来就用日语原文顶上，
    并且日志里留一行 `[caption] ja fallback unit=...`；
  * **失败一律降级**：桥 5xx / 空译文 / 超时都不许把单元弄失败；
  * **中文单元一个请求都不发**。

不联网、不用 GPU：桥那一侧用 httpx.MockTransport，翻译腿是替身。
"""
import asyncio
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))
sys.path.insert(0, str(ROOT / "tts" / "stream"))

import httpx                       # noqa: E402

import bridge_v2 as B2             # noqa: E402
import session_text as ST          # noqa: E402
import stream_server as SS         # noqa: E402

# 判据等价性语料：中文、日语（含纯喘息）、中英混排、纯符号、空串
CORPUS = [
    "", "。", "……", "——唔！",
    "啊……嗯……！", "指挥官……慢、慢一点点……", "好的！明天见！",
    "はぁ……あっ……んっ……指揮官……", "——っ！んんんっ……！！",
    "んっ……はぁ、はっ、はっ……", "はい！あの！はやく！",
    "今日は、少し疲れました。", "指揮官？", "し、子宮口……",
    "舒服吧？ good girl", "Windows Defender 呀？", "（小声）其实我一直在等你回来……",
    "ふぅ……指揮官……全部……アルビオンに、くれたのですか……？",
]


class JaDetectTest(unittest.TestCase):
    def test_local_predicate_matches_bridge(self):
        for text in CORPUS:
            with self.subTest(text=text):
                self.assertEqual(SS.looks_japanese(text), B2.is_japanese(text),
                                 "「%s」两处判据不一致：本地 %s / 桥 %s"
                                 % (text, SS.looks_japanese(text), B2.is_japanese(text)))

    def test_chinese_is_not_japanese(self):
        for text in ("指挥官，今天也辛苦了。", "（说想我倒是听进去了。）", "——唔！"):
            self.assertFalse(SS.looks_japanese(text), text)

    def test_japanese_moans_are_japanese(self):
        for text in ("んっ……はぁ……", "——っ！んんんっ……！！"):
            self.assertTrue(SS.looks_japanese(text), text)


def mk_sess(cfg):
    return SS.Session(cfg, "s", 1, "albion", "wav")


def drain(sess):
    out = []
    while not sess.out.empty():
        out.append(sess.out.get_nowait())
    return out


def unit_of(text):
    """把一段文本切成单元，返回第一个（`_no_audio` 只需要 start/end/speech_text）。"""
    st = ST.SessionText("cap")
    units = []
    for ch in text:
        units += st.feed(ch, 0.0)
    units += st.finish(1.0)
    return st, units


class CaptionHelperTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.co = SS.Coordinator(SS.Config())

    async def asyncTearDown(self):
        await self.co.aclose()

    def _bridge(self, reply):
        self.co._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))

    async def test_japanese_unit_gets_chinese_caption(self):
        self._bridge(lambda req: httpx.Response(200, json={"zh": "哈啊……啊……嗯……指挥官……"}))
        st, units = unit_of("はぁ……あっ……んっ……指揮官……")
        u = units[0]
        cap = self.co._start_caption(mk_sess(self.co.cfg), u, "はぁ……あっ……んっ……", False)
        self.assertIsNotNone(cap, "日语单元必须起一次字幕翻译")
        zh = await self.co._caption_text(None, u, cap, "はぁ……あっ……んっ……")
        self.assertEqual(zh, "哈啊……啊……嗯……指挥官……")

    async def test_chinese_unit_sends_no_request(self):
        hits = []
        self._bridge(lambda req: hits.append(1) or httpx.Response(200, json={"zh": "x"}))
        u = unit_of("指挥官，今天也辛苦了。")[1][0]
        cap = self.co._start_caption(mk_sess(self.co.cfg), u, "指挥官，今天也辛苦了。", False)
        self.assertIsNone(cap)
        self.assertEqual(hits, [], "中文单元一个字幕请求都不该发")

    async def test_empty_reply_falls_back_to_japanese(self):
        self._bridge(lambda req: httpx.Response(200, json={"zh": ""}))
        u = unit_of("はぁ……")[1][0]
        cap = self.co._start_caption(mk_sess(self.co.cfg), u, "はぁ……", True)
        self.assertEqual(await self.co._caption_text(None, u, cap, "はぁ……"), "はぁ……")

    async def test_bridge_500_falls_back_to_japanese(self):
        self._bridge(lambda req: httpx.Response(500, text="boom"))
        u = unit_of("はぁ……")[1][0]
        cap = self.co._start_caption(mk_sess(self.co.cfg), u, "はぁ……", True)
        self.assertEqual(await self.co._caption_text(None, u, cap, "はぁ……"), "はぁ……")

    async def test_slow_caption_is_cut_at_caption_wait(self):
        async def slow(req):
            await asyncio.sleep(2.0)
            return httpx.Response(200, json={"zh": "哈啊……"})
        self._bridge(slow)
        self.co.cfg.caption_wait = 0.2
        u = unit_of("はぁ……")[1][0]
        cap = self.co._start_caption(mk_sess(self.co.cfg), u, "はぁ……", True)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        got = await self.co._caption_text(None, u, cap, "はぁ……")
        took = loop.time() - t0
        self.assertEqual(got, "はぁ……", "超时就用日语原文顶上")
        self.assertLess(took, 1.2, "最多等 caption_wait（0.2s），不许等到 2s 那次")

    async def test_wait_zero_disables_waiting(self):
        self._bridge(lambda req: httpx.Response(200, json={"zh": "哈啊……"}))
        self.co.cfg.caption_wait = 0.0
        u = unit_of("はぁ……")[1][0]
        cap = self.co._start_caption(mk_sess(self.co.cfg), u, "はぁ……", True)
        self.assertEqual(await self.co._caption_text(None, u, cap, "はぁ……"), "はぁ……")


class NoAudioCaptionTest(unittest.IsolatedAsyncioTestCase):
    """括号心声（无音频单元）也要翻成中文再显示。"""

    async def asyncSetUp(self):
        self.co = SS.Coordinator(SS.Config())
        self.co._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"zh": "（腰自己浮起来了。）"})))

    async def asyncTearDown(self):
        await self.co.aclose()

    async def test_aside_display_is_chinese(self):
        text = "（腰が勝手に浮いてしまいます。）"
        st, units = unit_of(text)
        sess = SS.Session(self.co.cfg, "s", 1, "albion", "wav")
        setattr(sess, "text", st)
        u = units[0]
        await self.co._no_audio(sess, u, reason="aside")
        events = [json.loads(p) for k, p in drain(sess) if k == "text"]
        span = [e for e in events if e.get("payload", {}).get("text") is not None]
        shown = span[0]["payload"]["text"]
        self.assertEqual(shown, "（腰自己浮起来了。）", "无音频的日语心声也要显示中文")


class CaptionSwitchTest(unittest.TestCase):
    def test_env_switches(self):
        with mock.patch.dict(os.environ, {"ALBION_CAPTION_WAIT_S": "1.5"}):
            self.assertAlmostEqual(SS.Config().caption_wait, 1.5)
        with mock.patch.dict(os.environ, {"ALBION_CAPTION_ZH": "0"}):
            self.assertFalse(SS.Config().caption_zh)
        with mock.patch.dict(os.environ, {"ALBION_CAPTION_ZH": ""}):
            self.assertTrue(SS.Config().caption_zh, "没配就是开")


if __name__ == "__main__":
    unittest.main()
