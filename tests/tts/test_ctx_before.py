# -*- coding: utf-8 -*-
"""翻译带前文（2026-11-10）：9885 填 context_before → 桥拼进翻译提示，且不重复拼。

跑：python tests/tts/test_ctx_before.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for sub in ("tts/irodori", "tts/stream"):
    sys.path.insert(0, os.path.join(ROOT, *sub.split("/")))

import bridge as B            # noqa: E402
import bridge_v2 as B2        # noqa: E402
import stream_server as SS    # noqa: E402


class TranslateContextTest(unittest.TestCase):
    """9885 侧：给翻译腿的「前文」怎么截。"""

    def test_first_unit_has_no_context(self):
        self.assertEqual(SS.translate_context("哈、哈、哈、嗯、哈啊……", 0), "")

    def test_short_prefix_is_sent_whole(self):
        full = "今天天气真好呀，我在窗边坐了一会儿。指挥官……我快不行了……"
        first = full.index("指挥官")
        self.assertEqual(SS.translate_context(full, first), "今天天气真好呀，我在窗边坐了一会儿。")

    def test_long_prefix_is_bounded_and_starts_after_punctuation(self):
        full = "。".join(["第%d句话在这里" % i for i in range(40)]) + "。最后这一句是要说的。"
        ctx = SS.translate_context(full, full.index("最后这一句"))
        self.assertLessEqual(len(ctx), SS.TRANSLATE_CTX_CHARS, ctx)
        self.assertFalse(ctx.startswith("。"), ctx)
        self.assertTrue(full[:full.index("最后这一句")].endswith(ctx), ctx)   # 确实是前缀的尾巴
        self.assertTrue(ctx[0] in "第最后", ctx)

    def test_byte_level_unchanged_for_empty_start(self):
        self.assertEqual(SS.translate_context("", 0), "")


class BridgeContextPromptTest(unittest.TestCase):
    """桥侧：前文进提示（只作参考、不翻译），没前文时逐字段不变。"""

    def test_without_context_nothing_changes(self):
        prompt, text = B.with_context("P", "在当前句", "")
        self.assertEqual((prompt, text), ("P", "在当前句"))
        prompt, text = B.with_context("P", "在当前句", "   ")
        self.assertEqual((prompt, text), ("P", "在当前句"))

    def test_context_goes_into_prompt_and_user_message(self):
        prompt, text = B.with_context("P", "指挥官……我快不行了……", "今天天气真好呀。")
        self.assertIn("只供参考、不要翻译、不要复述", prompt)
        self.assertIn("只译「当前句」", prompt)
        self.assertIn("【前文，不要翻译】\n今天天气真好呀。", text)
        self.assertIn("【当前句，只译这一句】\n指挥官……我快不行了……", text)

    def test_context_is_capped(self):
        _, text = B.with_context("P", "当前句", "前" * 1000)
        self.assertLessEqual(len(text), B.TRANSLATE_CONTEXT_MAX + 40, len(text))


class PriorTextDedupeTest(unittest.TestCase):
    """bridge_v2：context_before 是 source_prefix 的尾巴时，别把前文拼两遍。"""

    class Req:
        def __init__(self, ctx="", sp=""):
            self.context_before = ctx
            self.source_prefix = sp

    def test_tail_context_is_not_duplicated(self):
        sp = "今天天气真好呀，我在窗边坐了一会儿。"
        self.assertEqual(B2._prior_text(self.Req(sp[-6:], sp)), sp)

    def test_legacy_source_prefix_only_unchanged(self):
        self.assertEqual(B2._prior_text(self.Req("", "前面的原文")), "前面的原文")

    def test_context_only_still_used(self):
        self.assertEqual(B2._prior_text(self.Req("跨轮前文", "")), "跨轮前文")

    def test_disjoint_parts_are_joined(self):
        self.assertEqual(B2._prior_text(self.Req("上一轮的话。", "本轮前半句。")),
                         "上一轮的话。本轮前半句。")


if __name__ == "__main__":
    unittest.main()
