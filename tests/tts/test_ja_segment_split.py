# -*- coding: utf-8 -*-
"""长日文子段再切（2026-11-07）。

复现结论：≥90 朗读字的日文块，模型会在句尾提前收尾并丢掉最后一整句——跨 5 个 seed、
跨 15.2/16.4/17.4 秒三个时长档位都一样（尾静音还有 0.5~1.0s，说明不是被预算挤掉），
对应 `inference_runtime` 的 `fixed_target_latent_steps` 那条 long-tail 警告；
58 字（12 秒）的块复测全部完整。所以在 `bridge_v2._ready()` 生成 ja_segments 之后、
交给 9885 之前，把超过 `JA_SEG_MAX=60` 朗读字的子段切开。

口径：朗读字数 = 非空白且 isalnum 的字符（同 `render_queue._spoken_chars`）。
切点：① 不超过上限的**最后一个句末**（。！？!?）；② 没有就取不超过上限、离中点最近的逗号
（、，,）；③ 都没有就不切；④ 每段至少 8 字，碎片并回相邻段；⑤ 超长递归再切。
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

import bridge_v2 as B  # noqa: E402

B11 = ("隙あらばバックグラウンドで勝手にディスクを読み込んでCPUを消費したり、"
       "ゲームを遊んでいる時やプログラムを動かしている時にカクつかせられたりすると、"
       "本当にイライラしてしまいますね。")
B12 = ("このDefender Removerなら、確かに根こそぎ削除できますね。"
       "指揮官、後で実行する時にちょっとオプションを確認して、綺麗さっぱりオフにしてくださいね。"
       "そうすれば、パソコンも軽快に動くようになりますよ。")


def lens(pieces):
    return [B._ja_spoken_len(p) for p in pieces]


class SplitTextTest(unittest.TestCase):
    def test_b11_splits_and_keeps_last_clause(self):
        pieces = B._ja_split_text(B11)
        self.assertEqual(B._ja_spoken_len(B11), 87)
        self.assertEqual(len(pieces), 2, "87 字 / 上限 60 ⇒ 两段")
        self.assertEqual(lens(pieces), [35, 52])
        # 第二个逗号在 72 字处（> 上限 60），所以只能切在第一个逗号「…消費したり、」
        self.assertTrue(pieces[0].endswith("消費したり、"))
        self.assertIn("イライラしてしまいますね。", pieces[1])
        self.assertTrue(all(n <= B.JA_SEG_MAX for n in lens(pieces)))

    def test_b12_splits_at_sentence_ends(self):
        pieces = B._ja_split_text(B12)
        self.assertEqual(len(pieces), 3, "句末切两刀 ⇒ 33/41/23")
        self.assertEqual(lens(pieces), [33, 41, 23])
        self.assertTrue(pieces[-1].endswith("軽快に動くようになりますよ。"))
        self.assertTrue(all(n <= B.JA_SEG_MAX for n in lens(pieces)))

    def test_short_text_untouched(self):
        text = "行ってらっしゃいませ、指揮官。"
        self.assertEqual(B._ja_split_text(text), [text])

    def test_long_text_without_punctuation_untouched(self):
        text = "あ" * 90                      # 没有句末也没有逗号 ⇒ 不切
        self.assertEqual(B._ja_split_text(text), [text])

    def test_recursive_split_respects_limit(self):
        text = B12 * 3                        # 291 字，句末足够多
        pieces = B._ja_split_text(text)
        self.assertGreater(len(pieces), 3)
        self.assertTrue(all(n <= B.JA_SEG_MAX for n in lens(pieces)), lens(pieces))

    def test_tiny_piece_merges_back(self):
        text = "あ" * 55 + "。" + "い。"     # 第二段只有 1 个字 ⇒ 并回
        self.assertEqual(len(B._ja_split_text(text)), 1)


class SplitSegmentsTest(unittest.TestCase):
    def test_segments_keep_fields_and_renumber(self):
        segs = [{"sub_index": 0, "text": B11, "emotion": {"v": 1}, "voice_caption": "c"},
                {"sub_index": 1, "text": "行ってらっしゃいませ、指揮官。"}]
        out = B._split_ja_segments(segs)
        self.assertEqual([s["sub_index"] for s in out], [0, 1, 2])
        self.assertEqual([B._ja_spoken_len(s["text"]) for s in out], [35, 52, 13])
        # 切出来的子段沿用原段的 emotion / voice_caption
        self.assertEqual(out[0]["emotion"], {"v": 1})
        self.assertEqual(out[1]["voice_caption"], "c")
        self.assertNotIn("emotion", out[2])

    def test_ready_returns_multiple_sub_indices(self):
        """接线：prepare 的 ja_segments 带出多个子段，sub_index 连续。"""
        svc = B.PrepareService(None)
        req = B.PrepareRequest("s", 1, "u1", "albion", (0, 5), "中文原文。")
        res = svc._ready(req, [B11], 0.0, True, None)
        self.assertEqual(res.status, "ready")
        self.assertEqual(len(res.ja_segments), 2)
        self.assertEqual([s["sub_index"] for s in res.ja_segments], [0, 1])
        self.assertTrue(all(B._ja_spoken_len(s["text"]) <= B.JA_SEG_MAX
                            for s in res.ja_segments))


if __name__ == "__main__":
    unittest.main()

