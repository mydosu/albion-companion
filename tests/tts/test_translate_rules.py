# -*- coding: utf-8 -*-
"""翻译层规则自检：python test_translate_rules.py（不联网，只查判定与挑行）

全部用例收在 unittest.TestCase 里，`-m unittest discover` 和直接运行都能收集到同一批。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tts" / "irodori"))

from bridge import extract_speech_text, is_japanese, strip_asides, strip_asides_streaming


class IsJapaneseTest(unittest.TestCase):
    def test_cn_with_kana_is_not_japanese(self):
        # 中文里夹一个假名/罗马字，以前会被当成日语绕过翻译
        cases = [
            "指挥官，辛苦了。先喝杯茶吧，アルビオン在这里陪着您。",
            "指挥官，请看着阿尔比恩的眼睛哦。",
            "Today I will stay with you, 指挥官",
            "",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(is_japanese(text))

    def test_real_japanese_is_detected(self):
        cases = [
            "指揮官、今日はお疲れ様でした。アルビオンがお茶をお入れしますね。",
            "あっ……んっ……はぁ……",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(is_japanese(text))

    def test_cn_with_kana_short_sentences(self):
        # 2026-09-17：短句里带 アルビオン（5 假名），占比越线 ⇒ 被当日语 ⇒ 绕过翻译
        for text in ("那看阿尔比恩自己这边吧。", "阿尔比恩自己这边。", "指挥官，我看见您了。"):
            with self.subTest(text=text):
                self.assertIs(is_japanese(text), False)
        # 真日语照旧
        for text in ("こちらこそ、よろしくお願いします。", "アルビオン、参りますね。", "どうぞ、お茶を飲んでください。"):
            with self.subTest(text=text):
                self.assertIs(is_japanese(text), True)


class KanjiHeavyJapaneseTest(unittest.TestCase):
    """汉字多的日语不再被当成中文（2026-11-16）。

    事故：`指揮官、今日はいい天気ですね。` 只有 6/13 是假名（<0.5）⇒ 判成中文 ⇒ 桥把它
    再翻一遍 ⇒ 这句没有字幕。加宽的判据（无中文虚词 + 假名 ≥2 + 占比 ≥0.25 + 汉字全在
    cp932）要能收下这种句子，同时不能把带简体专用字的中文收进来（日语 TTS 念中文更糟）。
    """

    JA = ["指揮官、今日はいい天気ですね。", "本日は晴天なり", "明日会議室で打ち合わせ"]
    ZH = ["指挥官早上好，今天天气不错", "指挥官说おはよう", "这是すごい", "東京都庁"]

    def test_kanji_heavy_japanese_is_japanese(self):
        for text in self.JA:
            with self.subTest(text=text):
                self.assertIs(is_japanese(text), True)

    def test_chinese_with_simplified_only_chars_stays_chinese(self):
        for text in self.ZH:
            with self.subTest(text=text):
                self.assertIs(is_japanese(text), False)


class SpeechTextTest(unittest.TestCase):
    def test_extract_speech_text(self):
        # 挑朗读行：中文行不能进 TTS
        self.assertEqual(extract_speech_text("指挥官，晚安。\nおやすみなさい、指揮官。"), "おやすみなさい、指揮官。")
        self.assertEqual(extract_speech_text("指挥官，请喝茶，アルビオン去准备。"), "")
        self.assertEqual(extract_speech_text("（心声：他今天很累）\nお疲れ様です。"), "お疲れ様です。")

    def test_strip_asides(self):
        self.assertEqual(strip_asides("（心声）おはよう"), "おはよう")


class StripAsidesStreamingTest(unittest.TestCase):
    def test_partial_asides_stay_silent(self):
        # 逐句流式：前端会按标点把一句心声切成几个请求——中间那截没括号，也必须一声不吭
        self.assertEqual(strip_asides_streaming("（冷不防被摸了头，"), "")
        self.assertEqual(strip_asides_streaming("肩膀轻轻缩了一下，又慢慢松下来…"), "")
        self.assertEqual(strip_asides_streaming("）那个……是先喝口茶吗？"), "那个……是先喝口茶吗？")
        self.assertEqual(strip_asides_streaming("指挥官，欢迎回来。"), "指挥官，欢迎回来。")


if __name__ == "__main__":
    unittest.main()