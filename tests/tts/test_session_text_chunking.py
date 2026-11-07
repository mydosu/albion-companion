# -*- coding: utf-8 -*-
"""session_text 切句不变性：同一段文本不论按什么分块送入，切出的单元序列必须一致。

壳按流式 token 到达的快慢把文本拆成任意块，切句若依赖块边界，同一句话会念成不同的断句。
时间固定（feed 一律 now=0、finish 在 1.0），只验证文本分块这一个变量；随机块用固定 seed。
"""
import pathlib
import random
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tts" / "stream"))
from session_text import SessionText  # noqa: E402

TEXTS = {
    "中文多句": "指挥官，今天也辛苦了。要不要先休息一下？我给你泡杯茶吧。",
    "英文词带空格": "指挥官，托着下巴在琢磨怎么彻底关 Windows Defender 呀？",
    "心声括号": "（小声）其实我一直在等你回来……那个，不要误会！只是有点担心而已。",
    "日语": "こんにちは、指揮官。今日もお疲れさまです。少し休みませんか？",
    "控制标记": "哎？<|ACT:{\"emotion\":\"happy\"}|>真的吗！太好了，我还以为你不会来了呢，所以一直一个人在这里发呆，连茶都凉了。",
    "无标点超长串": "没有标点的超长串" * 12,
    "换行与多种标点": "第一句。第二句！第三句？\n第四句，带逗号，再来一个；然后冒号：结束。",
}

# 2026-11-03：9885 把「…彻底关 Windows」和「Defender 呀？」切成两块各自翻译、各自念。
# 英文词/数字两边的空格不是软切点（中文里的空格基本只出现在那里）。
EN_MIXED = "指挥官，托着下巴在琢磨怎么彻底关 Windows Defender 呀？"
PURE_CN = "指挥官，托着下巴在琢磨怎么彻底关掉那个东西呀，真的很难弄。"
CUT_PUNCT = "，,。！？!?；;：:…"



def run(chunks):
    s = SessionText("chunking")
    units = []
    for c in chunks:
        units += s.feed(c, 0.0)
    units += s.finish(1.0)
    return [(u.start, u.end, u.speech_text, u.kind, u.reason) for u in units]


def random_chunks(text, seed):
    rng, i, out = random.Random(seed), 0, []
    while i < len(text):
        n = rng.randint(1, 7)
        out.append(text[i:i + n])
        i += n
    return out


class SessionTextChunkingTest(unittest.TestCase):
    def test_chunking_does_not_change_units(self):
        for name, text in TEXTS.items():
            whole = run([text])
            self.assertTrue(whole, name)
            self.assertEqual(whole[-1][1], len(text), "%s: 单元要覆盖全文" % name)
            splits = {"逐字": list(text), **{"随机%d" % seed: random_chunks(text, seed) for seed in range(5)}}
            for how, chunks in splits.items():
                with self.subTest(text=name, chunking=how):
                    self.assertEqual("".join(chunks), text)
                    self.assertEqual(run(chunks), whole)


class SpaceCutTest(unittest.TestCase):
    """空格软切点只认「左右都不贴英文/数字」的位置（2026-11-03 劈开 Windows/Defender 的回归）。"""

    def test_english_word_not_split_at_space(self):
        win, dfd = EN_MIXED.index("Windows"), EN_MIXED.index("Defender")
        splits = {"整句": [EN_MIXED], "逐字": list(EN_MIXED),
                  "词中间": [EN_MIXED[:win + 3], EN_MIXED[win + 3:]]}
        for how, chunks in splits.items():
            with self.subTest(chunking=how):
                self.assertEqual("".join(chunks), EN_MIXED)
                units = run(chunks)
                self.assertEqual("".join(u[2] for u in units), EN_MIXED)
                cuts = {u[1] for u in units[:-1]}
                # 切在「关| Windows」或「Windows| Defender」之间 = 同一个英文词串被劈成两块
                self.assertFalse(cuts & {win, dfd},
                                 "%s: 切点 %s 劈开了英文词" % (how, sorted(cuts)))
                self.assertEqual(units, run([EN_MIXED]), "%s: 分块不能改变单元" % how)

    def test_pure_chinese_unchanged(self):
        whole = run([PURE_CN])
        self.assertEqual(run(list(PURE_CN)), whole)
        for u in whole[:-1]:                  # 纯中文仍只在标点后切
            self.assertIn(PURE_CN[u[1] - 1], CUT_PUNCT, "切点 %d 不在标点后：%r" % (u[1], u[2]))


if __name__ == "__main__":
    unittest.main()
