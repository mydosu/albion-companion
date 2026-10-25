# -*- coding: utf-8 -*-
"""纯网址/路径/代码片段不念（2026-10-25，全部离线替身，不联网）。

背景：她的中文回复按单元切开、逐段送翻译再合成。某个单元只是一段网址或路径时
（`/product_id/RJ….html`、`https://…`），翻译模型不翻译它，而是自己编一句
「はい、指揮官。アルビオンに何かご用ですか？」念出去。修法：送翻译前把 URL/路径剥掉；
剥完一个汉字、一个假名都不剩就当不可读，返回空串（`to_japanese_ex` 返回 `("", None)`），
调用方回 0.05s 静音 —— 字幕照常显示，那是壳的事。
"""
import asyncio
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tts" / "irodori"))

import bridge      # noqa: E402
import bridge_v2   # noqa: E402

URL = "https://x.com/product_id/RJ0123456789.html"
PATH = "/product_id/RJ0123456789.html"
WINPATH = r"D:\AI\Wife\tts\irodori\bridge.py"
ZH = "在的，指挥官。"


def _run_ex(text, reply="", emotion=False):
    """跑 `to_japanese_ex`：翻译腿全打替身，回 (结果, _ask 替身)。"""
    with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
            patch.object(bridge, "EMOTION_HINT", emotion), \
            patch.object(bridge, "CODEX_API_FALLBACK", False), \
            patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
            patch.object(bridge, "_ask", return_value=reply) as ask:
        return bridge.to_japanese_ex(text), ask


def _run(text, reply=""):
    """跑 `to_japanese`：同样全打替身，回 (译文, _ask 替身)。"""
    with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
            patch.object(bridge, "CODEX_API_FALLBACK", False), \
            patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
            patch.object(bridge, "_ask", return_value=reply) as ask:
        return bridge.to_japanese(text), ask


class StripLinksTest(unittest.TestCase):
    """剥除规则本身（纯函数）。"""

    def test_url_and_paths_go_away(self):
        self.assertEqual(bridge_v2.strip_links(URL), "")
        self.assertEqual(bridge_v2.strip_links("www.example.com/a/b"), "")
        self.assertEqual(bridge_v2.strip_links(PATH), "")
        self.assertEqual(bridge_v2.strip_links(WINPATH), "")
        self.assertEqual(bridge_v2.strip_links("见 /product_id/RJ1.html"), "见")

    def test_chinese_sentence_keeps_its_words(self):
        self.assertEqual(bridge_v2.strip_links("我把链接放在 %s 了" % URL), "我把链接放在 了")
        self.assertEqual(bridge_v2.strip_links("看这个%s哦" % PATH), "看这个 哦")

    def test_slashes_inside_plain_words_are_kept(self):
        # 中文里的「和/或」、数字间的斜杠（日期/比例）都不是路径，别误删
        for text in ("这是 和/或 的选择", "今天是2026/10/24", "比例 1/2 左右"):
            self.assertEqual(bridge_v2.strip_links(text), text)

    def test_readable_needs_han_or_kana(self):
        self.assertFalse(bridge_v2.has_readable("const x = 1;"))
        self.assertFalse(bridge_v2.has_readable(""))
        self.assertTrue(bridge_v2.has_readable("在的"))


class UnreadableUnitTest(unittest.TestCase):
    """纯链接/路径/代码单元：返回空串，且**一次翻译都不调**。"""

    def test_pure_url_is_not_translated(self):
        for text in (URL, "www.example.com/a/b", URL + "。"):
            (ja, emo), ask = _run_ex(text)
            self.assertEqual((ja, emo), ("", None), text)
            self.assertEqual(ask.call_count, 0, text)

    def test_pure_path_is_not_translated(self):
        for text in (PATH, "~/docs/a.md", WINPATH, "a/b/c.py"):
            (ja, emo), ask = _run_ex(text)
            self.assertEqual((ja, emo), ("", None), text)
            self.assertEqual(ask.call_count, 0, text)

    def test_pure_code_or_latin_is_not_translated(self):
        for text in ("const x = 1;", "def foo(bar): return 1", "OK"):
            (ja, emo), ask = _run_ex(text)
            self.assertEqual((ja, emo), ("", None), text)
            self.assertEqual(ask.call_count, 0, text)

    def test_to_japanese_returns_empty_too(self):
        # v1 老路径（`/v1/audio/speech`）走的是 `to_japanese`，同一个口径
        for text in (URL, PATH, "const x = 1;"):
            ja, ask = _run(text)
            self.assertEqual(ja, "", text)
            self.assertEqual(ask.call_count, 0, text)


class UrlInsideChineseTest(unittest.TestCase):
    """中文句子夹网址：照常翻译剩下的部分，但送翻译的文本里不能带网址。"""

    def test_translator_never_sees_the_url(self):
        text = "我把链接放在 %s 了" % URL
        (ja, _), ask = _run_ex(text, reply="リンクを置いておきました。")
        self.assertEqual(ja, "リンクを置いておきました。")
        self.assertTrue(ask.call_count >= 1)
        sent = ask.call_args[0][1]
        self.assertNotIn("https://", sent)
        self.assertIn("我把链接放在", sent)

    def test_to_japanese_also_strips(self):
        text = "看这个%s哦" % PATH
        ja, ask = _run(text, reply="これを見てください。")
        self.assertEqual(ja, "これを見てください。")
        self.assertNotIn("product_id", ask.call_args[0][1])

    def test_emotion_path_still_works(self):
        text = "链接在这 %s ，看嘛" % URL
        (ja, emo), ask = _run_ex(text, reply='{"ja":"リンクはこちらです、見てください。","arousal":0.4}',
                                 emotion=True)
        self.assertEqual(ja, "リンクはこちらです、見てください。")
        self.assertEqual(emo["arousal"], 0.4)
        self.assertNotIn("https://", ask.call_args[0][1])


class NormalChineseUnaffectedTest(unittest.TestCase):
    def test_plain_chinese_is_translated_as_before(self):
        (ja, emo), ask = _run_ex(ZH, reply='{"ja":"はい、指揮官。","arousal":0.3}', emotion=True)
        self.assertEqual(ja, "はい、指揮官。")
        self.assertEqual(emo["arousal"], 0.3)
        self.assertEqual(ask.call_args[0][1], ZH)      # 没有要剥的东西，原文照送

    def test_japanese_passthrough_is_unchanged(self):
        (ja, emo), ask = _run_ex("おはよう。")
        self.assertEqual((ja, emo), ("おはよう。", None))
        self.assertEqual(ask.call_count, 0)


def _req(text):
    return bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u1", voice="albion",
                                    source_range=(0, 5), speech_text=text, plan_version=6)


class V2PrepareTest(unittest.TestCase):
    """v2 主路径（壳真正调的 /v2/prepare）：纯链接单元直接 no_audio，不占翻译名额。"""

    def test_pure_url_unit_is_no_audio_without_translation(self):
        calls = []
        svc = bridge_v2.PrepareService(lambda s: calls.append(s) or "はい。")
        r = asyncio.run(svc.prepare(_req(URL)))
        self.assertEqual((r.status, r.reason), ("no_audio", "symbols"))
        self.assertEqual(calls, [])

    def test_chinese_with_url_is_still_ready(self):
        seen = []

        def ask(*a, **kw):
            seen.append(a[1])
            return "リンクを置いておきました。"

        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), \
                patch.object(bridge, "EMOTION_HINT", False), \
                patch.object(bridge, "CODEX_API_FALLBACK", False), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", side_effect=ask):
            svc = bridge_v2.PrepareService(bridge.to_japanese)
            r = asyncio.run(svc.prepare(_req("我把链接放在 %s 了。" % URL)))
        self.assertEqual(r.status, "ready")
        self.assertTrue(seen)
        for sent in seen:
            self.assertNotIn("https://", sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
