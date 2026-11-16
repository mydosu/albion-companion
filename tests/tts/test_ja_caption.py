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
    # 汉字多的日语（假名 <0.5，以前判成中文）与「带简体专用字」的中文，两处判据必须一致
    "指揮官、今日はいい天気ですね。", "本日は晴天なり", "明日会議室で打ち合わせ",
    "指挥官早上好，今天天气不错", "指挥官说おはよう", "这是すごい", "東京都庁",
]

# 汉字多的日语：假名占比 <0.5，旧判据会当中文再翻一遍（→ 这句没字幕）
JA_HEAVY = ["指揮官、今日はいい天気ですね。", "本日は晴天なり", "明日会議室で打ち合わせ"]
# 中文侧：有简体专用字（挥/说）或中文虚词（这），或干脆没有假名（東京都庁）
ZH_MIXED = ["指挥官早上好，今天天气不错", "指挥官说おはよう", "这是すごい", "東京都庁"]


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

    def test_kanji_heavy_japanese_both_sides(self):
        """汉字多的日语两处都得判日语（假名占比 <0.5，靠 cp932 那条加宽收进来）。"""
        for text in JA_HEAVY:
            with self.subTest(text=text):
                self.assertTrue(SS.looks_japanese(text), "本地判据漏了「%s」" % text)
                self.assertTrue(B2.is_japanese(text), "桥判据漏了「%s」" % text)

    def test_simplified_chinese_both_sides(self):
        """简体专用字 / 中文虚词 / 没假名的纯汉字，两处都不许判成日语。"""
        for text in ZH_MIXED:
            with self.subTest(text=text):
                self.assertFalse(SS.looks_japanese(text), "本地判据把「%s」当日语" % text)
                self.assertFalse(B2.is_japanese(text), "桥判据把「%s」当日语" % text)


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


def _wav_bytes(samples=800, sr=16000):
    import io
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * samples)
    return out.getvalue()


def _sse_render(wav):
    import base64
    chunk = json.dumps({"audio_base64": base64.b64encode(wav).decode()})
    return "event: audio_chunk\ndata: %s\n\nevent: done\ndata: {}\n\n" % chunk


class _Bridge:
    """假桥：/v1/caption 按给定延时回 zh（或 500），其余（渲染）回 SSE。"""

    def __init__(self, zh="哈啊……啊……", delay=0.0, status=200):
        self.zh, self.delay, self.status = zh, delay, status
        self.hits = 0

    async def __call__(self, req):
        if "/v1/caption" in str(req.url):
            self.hits += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.status != 200:
                return httpx.Response(self.status, text="boom")
            return httpx.Response(200, json={"zh": self.zh})
        return httpx.Response(200, text=_sse_render(_wav_bytes()),
                              headers={"content-type": "text/event-stream"})


class CaptionEarlyTest(unittest.IsolatedAsyncioTestCase):
    """字幕提前起跑、不再挡音频（2026-11-15）。

    旧行为：9885 出第一帧后才等 caption_wait(3s)，没回就把日语原文当字幕发出去，
    之后中文回来没人用，于是亲密段字幕几乎全是日语。现在：
      * 预取拿回 translated=False 就起 /v1/caption，同一单元只打一次；
      * display.span 立刻发日语原文，不等字幕；
      * 中文回来另发 display.caption（壳按 unit_id 换这一句）；
      * 失败/空/取消/换 generation 一律不发。
    """

    JA = "はぁ……あっ……んっ……"

    async def asyncSetUp(self):
        self.co = SS.Coordinator(SS.Config())
        self.sess = SS.Session(self.co.cfg, "s", 1, "albion", "wav")
        self.unit = self._feed(self.JA)
        self.sess.text.text = self.JA          # prepare 之外也要能取到原文（_begin_caption 用）

    def _feed(self, text):
        st, units = unit_of(text)
        self.sess.text = st
        self.sess.text.text = text
        return units[0]

    async def asyncTearDown(self):
        tasks = list(self.sess.captions.values()) + list(self.sess.caption_senders.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.co.aclose()

    def _bridge(self, **kw):
        b = _Bridge(**kw)
        self.co._client = httpx.AsyncClient(transport=httpx.MockTransport(b))
        return b

    def _take_all(self):
        """取空出站队列（一次读完，别分几趟 —— 队列读走就没了）。"""
        out = []
        while not self.sess.out.empty():
            kind, payload = self.sess.out.get_nowait()
            if kind == "text":
                out.append(json.loads(payload))
        return out

    def _events(self, event=None):
        out = []
        while not self.sess.out.empty():
            kind, payload = self.sess.out.get_nowait()
            if kind != "text":
                continue
            evt = json.loads(payload)
            if event is None or evt["event"] == event:
                out.append(evt["payload"])
        return out

    async def test_prefetch_starts_caption_when_bridge_says_japanese(self):
        """预取回 translated=False，立刻起字幕，不等渲染。"""
        b = self._bridge()
        self.co.prepare = mock.AsyncMock(return_value={
            "status": "ready", "translated": False,
            "ja_segments": [{"sub_index": 0, "text": "はぁ……あっ……んっ……"}]})
        _nxt, task = self.co._prepare_ahead(self.sess, self.unit)
        await task
        self.assertIn(self.unit.unit_id, self.sess.captions)
        await asyncio.gather(*self.sess.caption_senders.values())
        self.assertEqual(b.hits, 1, "预取阶段就该打一次 /v1/caption")

    async def test_chinese_unit_does_not_start_caption(self):
        b = self._bridge()
        self.unit = self._feed("指挥官，今天也辛苦了。")
        self.co.prepare = mock.AsyncMock(return_value={
            "status": "ready", "translated": True,
            "ja_segments": [{"sub_index": 0, "text": "指揮官、今日もお疲れさまでした。"}]})
        _nxt, task = self.co._prepare_ahead(self.sess, self.unit)
        await task
        self.assertNotIn(self.unit.unit_id, self.sess.captions)
        self.assertEqual(b.hits, 0)

    async def test_same_unit_only_one_caption_request(self):
        b = self._bridge(delay=0.05)
        self.co._begin_caption(self.sess, self.unit)
        self.co._begin_caption(self.sess, self.unit)
        self.co._start_caption(self.sess, self.unit, "はぁ……", True)
        await asyncio.gather(*self.sess.caption_senders.values())
        self.assertEqual(b.hits, 1, "同一单元只许打一次 /v1/caption")

    async def _render(self, **bridge_kw):
        self._bridge(**bridge_kw)
        self.co.plan = mock.AsyncMock(return_value=({"family": "neutral"}, None, False))
        self.co.open_render = mock.AsyncMock(return_value=("rs", 1))
        self.co._wait_credit = mock.AsyncMock(return_value=0)

    async def test_render_sends_japanese_span_without_waiting_caption(self):
        """首帧那条 display.span 立刻发日语原文（字幕 0.5s 才回，不许等它）。"""
        await self._render(delay=0.5)
        with mock.patch.object(SS.WR, "save", return_value=None):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await self.co.render_unit(self.sess, self.unit,
                                      [(0, "はぁ……あっ……んっ……", None, None, False)], t0 + 30)
            took = loop.time() - t0
        spans = self._events("display.span")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["text"], "はぁ……あっ……んっ……", "span 必须是日语原文，不能等中文")
        self.assertLess(took, 0.4, "字幕还没回来就必须已经把音频发完了")

    async def test_caption_event_arrives_after_render(self):
        await self._render(delay=0.2)
        with mock.patch.object(SS.WR, "save", return_value=None):
            await self.co.render_unit(self.sess, self.unit,
                                      [(0, "はぁ……あっ……んっ……", None, None, False)],
                                      asyncio.get_running_loop().time() + 30)
            self.assertEqual(self._events("display.caption"), [], "中文还没回来，先不发")
            await asyncio.gather(*self.sess.caption_senders.values())
        caps = self._events("display.caption")
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["unit_id"], self.unit.unit_id)
        self.assertEqual(caps[0]["zh"], "哈啊……啊……")
        self.assertEqual(caps[0]["generation"], 1)

    async def test_cancelled_session_sends_no_caption(self):
        self._bridge(delay=0.1)
        self.co._begin_caption(self.sess, self.unit)
        self.sess.cancelled = True
        await asyncio.gather(*self.sess.caption_senders.values())
        self.assertEqual(self._events("display.caption"), [])

    async def test_generation_change_sends_no_caption(self):
        self._bridge(delay=0.1)
        self.co._begin_caption(self.sess, self.unit)
        self.sess.generation = 2                       # 换 generation：旧那条中文不许再上屏
        await asyncio.gather(*self.sess.caption_senders.values())
        self.assertEqual(self._events("display.caption"), [])

    async def test_failed_caption_sends_nothing(self):
        self._bridge(status=500)
        self.co._begin_caption(self.sess, self.unit)
        await asyncio.gather(*self.sess.caption_senders.values())
        self.assertEqual(self._events("display.caption"), [])

    async def test_empty_caption_sends_nothing(self):
        self._bridge(zh="")
        self.co._begin_caption(self.sess, self.unit)
        await asyncio.gather(*self.sess.caption_senders.values())
        self.assertEqual(self._events("display.caption"), [], "空译文不发，壳保持日语")

    async def test_caption_after_finish_still_sent(self):
        """音频全部发完（session.finished）之后中文才回来，也要送到壳（2026-11-15 修复）。"""
        await self._render(delay=0.3)
        with mock.patch.object(SS.WR, "save", return_value=None):
            await self.co.render_unit(self.sess, self.unit,
                                      [(0, "はぁ……あっ……んっ……", None, None, False)],
                                      asyncio.get_running_loop().time() + 30)
            self.assertEqual(len(self.sess.caption_senders), 1)
            fin = asyncio.create_task(self.co._finish(self.sess))
            await asyncio.sleep(0.05)             # 收尾已开始，字幕还没回来
            self.assertFalse(fin.done(), "字幕还在路上时不许把会话收掉")
            await fin
        events = self._take_all()                  # 一次取完（队列只能读一次）
        finished = [e for e in events if e.get("event") == "session.finished"]
        self.assertEqual(finished[0]["captions_pending"], 1)
        caps = [e["payload"] for e in events if e.get("event") == "display.caption"]
        self.assertEqual(len(caps), 1, "收尾之后回来的字幕也要发出去")
        self.assertEqual(caps[0]["zh"], "哈啊……啊……")

    async def test_captions_pending_is_zero_without_caption(self):
        """中文单元没有字幕任务 ⇒ captions_pending=0，收尾立刻返回（不多留会话）。"""
        self.unit = self._feed("指挥官，今天也辛苦了。")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await self.co._finish(self.sess)
        self.assertLess(loop.time() - t0, 0.2)
        finished = [e for e in self._take_all() if e.get("event") == "session.finished"]
        self.assertEqual(finished[0]["captions_pending"], 0)

    async def test_cancel_while_waiting_drops_caption(self):
        """收尾还在等字幕时被打断（cancel）⇒ 字幕任务撤掉、一条都不发。"""
        self._bridge(delay=5.0)
        self.co._begin_caption(self.sess, self.unit)
        await self.co._cancel(self.sess)
        await asyncio.sleep(0.05)
        self.assertEqual(self.sess.caption_senders, {})
        self.assertEqual(self._events("display.caption"), [])

    async def test_failed_render_drops_caption_tasks(self):
        """渲染失败，这个单元的字幕没人要了，任务撤掉（不许攒着打上游）。"""
        await self._render(delay=5.0)
        self.co.open_render = mock.AsyncMock(side_effect=RuntimeError("render 挂了"))
        await self.co.render_unit(self.sess, self.unit,
                                  [(0, "はぁ……", None, None, False)],
                                  asyncio.get_running_loop().time() + 30)
        self.assertEqual(self.sess.captions, {})
        self.assertEqual(self.sess.caption_senders, {})


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
