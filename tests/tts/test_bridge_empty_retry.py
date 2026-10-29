# -*- coding: utf-8 -*-
"""桥的空译文重试（2026-09-30：一天两次 `FAILED stage=translation error=empty_translation` = 那两句没音频）。

上游偶发回空字符串 ⇒ 直接判失败太亏（一句就是一整片文本）。重试一次；仍然空就照旧报错。
"""
import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tts", "irodori"))
import bridge_v2  # noqa: E402


def mk_req(text):
    return bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u1", voice="albion",
                                    source_range=(0, 5), speech_text=text, plan_version=6)


class EmptyTranslationRetryTest(unittest.TestCase):
    def test_空一次就重试并成功(self):
        calls = []

        def tr(seg):
            calls.append(seg)
            return "" if len(calls) == 1 else "おはよう。"

        r = asyncio.run(bridge_v2.PrepareService(tr)._translate_unit(mk_req("おはよう。"), time.monotonic()))
        self.assertEqual(r.status, "ready")
        self.assertEqual(len(calls), 2, "空译文必须重试一次")
        self.assertEqual(len(r.ja_segments), 1)

    def test_一直空就照旧报错(self):
        calls = []

        def tr(seg):
            calls.append(seg)
            return ""

        r = asyncio.run(bridge_v2.PrepareService(tr)._translate_unit(mk_req("おはよう。"), time.monotonic()))
        self.assertEqual(r.status, "failed")
        self.assertIn("empty_translation", str(r.error))
        self.assertEqual(len(calls), 2, "只重试一次，不许无限重试")


    def test_带情绪的翻译回调也走同一条重试(self):
        """codex 复核第 4 条：生产走的是 `translate_ex`（译文 + 情绪二元组），单独验一次。

        源句这里必须是**中文**：2026-10-28 起，译文与原文相同的单元按日语直通处理，
        翻译腿带回的情绪会被丢掉（`bridge_v2._ready` 的 `passthrough`），
        这条测的是"重试 + 第二次的情绪要留住"，用真翻译的输入才验得到。
        """
        calls = []

        def tr_ex(seg):
            calls.append(seg)
            return ("", None) if len(calls) == 1 else ("おはよう。", {"arousal": 0.3, "valence": 0.2})

        svc = bridge_v2.PrepareService(translate=lambda s: "（不该走到这条路）", translate_ex=tr_ex)
        r = asyncio.run(svc._translate_unit(mk_req("早上好。"), time.monotonic()))
        self.assertEqual(r.status, "ready")
        self.assertEqual(len(calls), 2)
        self.assertEqual(r.ja_segments[0]["text"], "おはよう。")       # 用第二次的译文
        self.assertIn("0.3", json.dumps(r.ja_segments[0], ensure_ascii=False))   # 情绪也跟着第二次


if __name__ == "__main__":
    unittest.main()
