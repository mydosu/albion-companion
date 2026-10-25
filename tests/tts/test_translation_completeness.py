# -*- coding: utf-8 -*-
"""翻译不许吞：整句译文漏了分句、JSON 抢救截断、逐句译文被静默丢掉（全部离线替身，不联网）。

背景（2026-10-24 吞句排查）：v2 准备接口按「一个单元一句」送进翻译，旧的句数闸 `_sent_count(ja) >= _sent_count(zh)`
对单句恒成立，句内漏掉的分句查不出来；JSON 不合法时的 `_JA_RE` 抢救会把译文截在第一个内嵌引号处；
`to_japanese` 逐句译时空译文被 `if o` 静默滤掉。
"""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tts" / "irodori"))

import bridge  # noqa: E402

ZH = "抓到的原文是：标题：同人誌ダウンロード，指挥官您看一下"
ZH_CLAUSES = ["抓到的原文是，标题", "同人誌ダウンロード", "指挥官您看一下"]


class ParseRescueTest(unittest.TestCase):
    def test_inner_quote_does_not_truncate_rescued_translation(self):
        bad = ('{"ja":"鳥を追い払おうとしたけど、"release bird"って入力しても返事がないんです。",'
               '"arousal":0.4,"phonation":"voiced"}')
        # 抢救不出完整译文就老实返回空，让调用方走老路径，别交半句
        self.assertEqual(bridge._parse_emotion_json(bad), ("", None))

    def test_truncated_structure_still_rescued(self):
        ja, emo = bridge._parse_emotion_json('{"ja":"かっこ { } 入り","arousal":0.5')
        self.assertEqual(ja, "かっこ { } 入り")
        self.assertIsNone(emo)


class CompleteTest(unittest.TestCase):
    def test_normal_translation_is_complete(self):
        self.assertTrue(bridge._complete("在的，指挥官。", "はい、指揮官。"))
        self.assertTrue(bridge._complete("指挥官，您在呀——久等了。", "指揮官、いらっしゃいましたね——お待たせしました。"))

    def test_shrunken_translation_is_not(self):
        self.assertFalse(bridge._complete(ZH, "同人誌ダウンロード、ですね。"))

    def test_fewer_sentences_is_not(self):
        self.assertFalse(bridge._complete("好的。我知道了。", "はい。"))

    def test_clauses_merge_tiny_fragments(self):
        self.assertEqual(bridge._clauses(ZH), ZH_CLAUSES)
        self.assertEqual(bridge._clauses("嗯。"), ["嗯。"])


class ShrunkenTranslationRetriedByClauseTest(unittest.TestCase):
    def _run(self, ask_side_effect):
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), patch.object(bridge, "EMOTION_HINT", True), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", side_effect=ask_side_effect) as ask:
            return bridge.to_japanese_ex(ZH), ask

    def test_dropped_clause_is_retranslated_clause_by_clause(self):
        per_clause = {"抓到的原文是，标题": "原文は、タイトル", "同人誌ダウンロード": "同人誌ダウンロード",
                      "指挥官您看一下": "指揮官、ご覧ください"}

        def ask(key, text, model, endpoint=None, prompt=None):
            if prompt:                                   # 情绪主路：漏了前两个分句
                return '{"ja":"指揮官、ご覧ください。","arousal":0.4}'
            if text == ZH:                               # 老路径整句：同样缩水
                return "指揮官、ご覧ください。"
            return per_clause[text]

        (ja, emo), ask_mock = self._run(ask)
        self.assertIsNone(emo)
        for part in per_clause.values():
            self.assertIn(part, ja)
        self.assertEqual(ask_mock.call_count, 2 + len(ZH_CLAUSES))

    def test_complete_translation_costs_one_call(self):
        full = '{"ja":"原文は、タイトル、同人誌ダウンロード、指揮官、ご覧ください。","arousal":0.4}'
        (ja, emo), ask_mock = self._run([full])
        self.assertIn("ご覧ください", ja)
        self.assertEqual(emo["arousal"], 0.4)
        self.assertEqual(ask_mock.call_count, 1)


class EmptySegmentTest(unittest.TestCase):
    def test_empty_sentence_translation_fails_whole_instead_of_dropping(self):
        outs = {"第一句": "一つ目。", "第二句": "", "第三句": "三つ目。"}
        with patch.object(bridge, "TRANSLATE_BACKEND", "codex"), patch.object(bridge, "CODEX_API_FALLBACK", False), \
                patch.object(bridge, "_cc_keys", return_value=["offline-placeholder"]), \
                patch.object(bridge, "_ask", side_effect=lambda k, seg, *a, **kw: outs[seg.strip()]):
            self.assertEqual(bridge.to_japanese("第一句。第二句。第三句。"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
