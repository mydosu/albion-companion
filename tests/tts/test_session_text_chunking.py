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
    "心声括号": "（小声）其实我一直在等你回来……那个，不要误会！只是有点担心而已。",
    "日语": "こんにちは、指揮官。今日もお疲れさまです。少し休みませんか？",
    "控制标记": "哎？<|ACT:{\"emotion\":\"happy\"}|>真的吗！太好了，我还以为你不会来了呢，所以一直一个人在这里发呆，连茶都凉了。",
    "无标点超长串": "没有标点的超长串" * 12,
    "换行与多种标点": "第一句。第二句！第三句？\n第四句，带逗号，再来一个；然后冒号：结束。",
}


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


if __name__ == "__main__":
    unittest.main()
