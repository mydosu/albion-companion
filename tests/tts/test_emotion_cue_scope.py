# -*- coding: utf-8 -*-
"""cue 按源句归属（Astra 第 10 轮 ①）：四层验收 + 10 组 CPU 材料。

纯 CPU、离线、不加载模型、不联网：中文材料走**真桥**（`bridge_v2.PrepareService` + 真
`bridge.zh_source_cues`），翻译腿用**离线替身**（固定中文句→日语句的表，不起 HTTP、不调模型）；
规划走**真规划器**（`scene_plan.plan_units_v3`，逐子段串 `prev`，与壳侧协调器同序）。

四层：
  ① 源范围合法（有序、不重叠、间隔只有句末标点）且 cue 落到正确的源句身份；
     cue 的跨度是**中文原文的 code point 下标**（非 BMP emoji 也不漂）；
  ② 接线真携带身份：真桥盖章 → 真规划器读取（cue 自带 `sid` 且等于本段 `src_scope.id`）；
  ③ 断言**最终记录**的 family/caption/cfg/tier/level，并做成对比较（有无已到期 cue）；
  ④ 开关关：与冻结的 v4 输出逐字段相等；另做**反事实**：把旧坏行为放回去，本文件的断言必须变红。

跑法（在 D:/AI/Wife 下）：
    tts/irodori/.venv/Scripts/python.exe -B -m unittest discover -s tests/tts -p "test_emotion_*.py" -v
"""
import asyncio
import copy
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))
sys.path.insert(0, os.path.join(TTS, "stream"))

import bridge                                                          # noqa: E402
import bridge_v2                                                       # noqa: E402
import cue_scope as CS                                                 # noqa: E402
import scene_plan as SP                                                # noqa: E402

GOLDEN = os.path.join(HERE, "v4_cue_golden.json")
A_WHISPER = "柔らかく、優しく囁くように"
A_QUIET = "落ち着いた、静かな声で"

# ---------------- 材料（CPU 全可做）------------------------------------------
# raw    = 送桥的**原始切片**（「（小声）」这类线索只在这里看得见）
# speech = 会话解析剥掉旁白后的可说话文本（生产里壳就是这么发的）
# JA_PLAIN = 离线翻译替身：中文句（去掉句末标点）→ 日语句
JA_PLAIN = {"今天早点休息": "今日は早めに寝てくださいね。",
            "明天早上我叫你": "明日の朝、呼びますね。",
            "甲句": "これは甲の文ですね。",
            "乙句": "これは乙の文ですね。",
            "丙句": "これは丙の文ですね。",
            "他说只是舞台提示": "彼はそれはト書きだと指摘しただけです。",
            "不要小声说，正常说就好": "普通に話せばいいのですよ。",
            "小声说，小声说": "そう言ってもいいのですよ。",
            "接下来都小声说": "これからは静かに話しますね。",
            "今天天气不错😊": "今日はいい天気ですね。"}

MATS = {
    # ① cue 属第一源句；第二句没有重新得到该 D
    "M1": {"raw": "（小声说）今天早点休息。明天早上我叫你。",
           "speech": "今天早点休息。明天早上我叫你。"},
    # ② cue 属第二源句：不得误挂第一句
    "M2": {"raw": "今天早点休息。明天早上（小声说）我叫你。",
           "speech": "今天早点休息。明天早上我叫你。"},
    # ②b 与 M2 同句，换成会让 caption 真变档的耳语 cue（soft 只动音量档，不动锚）
    "M2b": {"raw": "今天早点休息。明天早上（耳语）我叫你。",
            "speech": "今天早点休息。明天早上我叫你。"},
    # ③ 两条 cue 各自归属；第三句不复用任何局部 D
    "M3": {"raw": "（小声说）甲句。（气息不稳）乙句。丙句。",
           "speech": "甲句。乙句。丙句。"},
    # ④ 引述：「小声说」只是被讨论的词，不是当前说话者的发声指令
    "M4": {"raw": '他说"（小声说）"只是舞台提示。',
           "speech": "他说只是舞台提示。"},
    # ⑤ 否定：不能变成正向弱声证据
    "M5": {"raw": "不要小声说，正常说就好。", "speech": "不要小声说，正常说就好。"},
    # ⑥ 重复词 / emoji / 换行：不依赖首次字符串匹配，下标按 code point
    "M6": {"raw": "（小声说）小声说，小声说。\n今天天气不错😊。明天早上（耳语）我叫你。",
           "speech": "小声说，小声说。今天天气不错😊。明天早上我叫你。"},
    # ⑨ cue 跨度非法：记 scope_unknown，不注入
    "M9": {"raw": "（小声说）今天早点休息。", "speech": "今天早点休息。"},
    # ⑩ 跨句指令：本轮**明确记为未支持**，不写解释器、不外推
    "XC": {"raw": "接下来都小声说。明天早上我叫你。",
           "speech": "接下来都小声说。明天早上我叫你。"},
}

# 冻结对照用的额外形状（也走真桥 + 真规划器）
EXTRA_CASES = {
    "N1": {"raw": "今天早点休息。明天早上我叫你。",           # 完全没有 cue
           "speech": "今天早点休息。明天早上我叫你。"},
    "BI": {"raw": "（小声说）今天早点休息。明天早上我叫你。",    # 日语直通切片：子段数 < 源句数
           "speech": "今日は早めに寝てください。明日の朝、呼びますね。"},
}
GOLDEN_CASES = dict(MATS)
GOLDEN_CASES.update(EXTRA_CASES)

# 材料⑦的两个子段：同一源句译文的两片（例如按 token 预算切开），线索与身份完全相同
M7_TEXTS = ("今日は早めに寝てくださいね。", "ゆっくり休んでください！！")


def _punct_free(s):
    return (s or "").strip().strip("。！？!?、，,；; \t\n")


def translator(table=None):
    """离线翻译替身：中文句 → 日语句。查不到就给一句中性日语（绝不返回空 —— 空会让桥记 failed）。"""
    t = dict(JA_PLAIN)
    t.update(table or {})

    def tr(sentence):
        k = _punct_free(sentence)
        if k not in t:
            # 替身对未登记输入必须**立刻失败**：兜底句会让"分块送法译成了另一句日语"的用例
            # 照样通过（Sol 第 10 轮第二条）。
            raise KeyError("离线替身未登记：%r" % k)
        return t[k]
    return tr


def _kw(cue_scope):
    """冻结基线用 None（那个版本还没有这个参数）—— 关掉开关时必须与冻结输出逐字段对照。"""
    return {} if cue_scope is None else {"cue_scope": cue_scope}


def prepare(mat, cue_scope, unit_id="u0", src=(0, None), speech=None, raw=None, table=None):
    """真桥 + 离线翻译替身 → `PrepareResult`（子段里带着 identity）。"""
    speech = mat["speech"] if speech is None else speech
    raw = mat["raw"] if raw is None else raw
    svc = bridge_v2.PrepareService(translate=translator(table), cues=bridge.zh_source_cues,
                                   **_kw(cue_scope))
    req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id=unit_id, voice="albion",
                                   source_range=(src[0], len(raw) if src[1] is None else src[1]),
                                   speech_text=speech, raw_text=raw)
    return asyncio.run(svc.prepare(req))


def plan(segments, cue_scope, prev=None, advance=True, plan_version=4, weak=True, ctx="", uid="u0"):
    """真规划器：逐子段规划（跟壳侧协调器一样，一次一个子段、把 state 串回去）。"""
    recs = []
    for s in segments:
        r, prev = SP.plan_units_v3([{"unit_id": "%s:%d" % (s.get("unit_id", uid), s["sub_index"]),
                                     "text": s["text"], "advance": advance,
                                     "emotion": s.get("emotion")}],
                                   extra_ctx=ctx, prev=prev, plan_version=plan_version,
                                   allow_neutral_whisper=plan_version >= 4, weak_delivery=weak,
                                   **_kw(cue_scope))
        recs.append(r[0])
    return recs, prev


def run(mat, cue_scope=None, **kw):
    """材料 → (最终记录, 子段, 末态)。"""
    res = prepare(mat, cue_scope, **kw)
    assert res.status == "ready", res
    recs, state = plan(list(res.ja_segments), cue_scope)
    return recs, list(res.ja_segments), state


def plain(recs):
    """层③要断言的东西：最终记录里的条件字段。"""
    return [(r["family"], r["caption"], r["cfg"], r["tier"], r["level"]) for r in recs]


def delivery_of(rec):
    return (rec.get("diagnostics") or {}).get("delivery")


# ---------------- 层①：源范围合法 + cue 落到正确的源句 ------------------------
class TestSourceRanges(unittest.TestCase):
    def test_spans_are_ordered_and_only_punctuation_between(self):
        for name, mat in MATS.items():
            sents = CS.sentences(mat["raw"])
            self.assertTrue(sents, name)
            self.assertLessEqual(sents[-1][1], len(mat["raw"]), name)      # 不越界
            for a, b in zip(sents, sents[1:]):
                self.assertLess(a[1], b[0], "%s：源句跨度重叠/乱序" % name)
                gap = mat["raw"][a[1]:b[0]]
                self.assertLessEqual(len(gap), 3, "%s：句间夹了 %r" % (name, gap))
                self.assertFalse(any(ch.isalnum() for ch in gap),
                                 "%s：句间夹了正文 %r" % (name, gap))

    def test_every_attributable_cue_lands_in_exactly_one_sentence(self):
        for name, mat in MATS.items():
            cues = bridge.zh_source_cues(mat["raw"])
            rows, diag = CS.attribute(mat["raw"], cues, len(CS.sentences(mat["raw"])),
                                      unit_key="k")
            self.assertEqual(sum(len(r) for r in rows),
                             len(cues) - diag["quoted"] - len(diag["unknown"]),
                             "%s：归属数对不上（%r）" % (name, diag))
            for j, row in enumerate(rows):
                for c in row:
                    self.assertEqual(c["sentence"], j, "%s：cue 挂在自己不在的句上" % name)
                    self.assertEqual(c["sid"], "k#%d" % j, name)
                    self.assertLessEqual(c["span"][1], len(mat["raw"]), name)

    def test_cue_spans_are_codepoints_not_utf16(self):
        """非 BMP emoji 出现在 cue 之前：归属不能按 UTF-16 长度漂。"""
        raw = MATS["M6"]["raw"]
        self.assertGreater(len(raw.encode("utf-16-le")) // 2, len(raw),
                           "材料里必须有非 BMP 字符，否则这条会空转")
        ws = [c for c in bridge.zh_source_cues(raw) if c["kind"] == "whisper"]
        self.assertEqual(len(ws), 1)
        self.assertEqual(raw[ws[0]["span"][0]:ws[0]["span"][1]], "（耳语）",
                         "span 必须落在中文原文的 code point 下标上")
        rows, diag = CS.attribute(raw, bridge.zh_source_cues(raw), 3, unit_key="k")
        self.assertEqual([c["sentence"] for c in rows[2]], [2], "耳语属于第三句")
        self.assertEqual([c["sentence"] for c in rows[1]], [], "第二句（emoji 那句）不该有 cue")
        self.assertEqual(diag["ids"], ["k#0", "k#1", "k#2"])

    def test_repeated_words_keep_their_own_positions(self):
        """同一句里同一个词出现多次：每条 cue 各自的下标，不是首次匹配。"""
        raw = MATS["M6"]["raw"]
        cues = [c for c in bridge.zh_source_cues(raw) if c["kind"] == "soft"]
        spans = sorted(tuple(c["span"]) for c in cues)
        self.assertEqual(len({s[0] for s in spans}), len(spans), "三条 soft 的起点必须各不相同")
        self.assertEqual([raw[s[0]:s[1]] for s in spans], ["（小声说）", "小声", "小声"], spans)


# ---------------- 层②：接线真携带身份（真桥 + 离线翻译替身 → 真规划器）--------
class TestWiring(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ALBION_CUE_SCOPE", None)

    def test_identity_rides_from_bridge_to_planner(self):
        raw = MATS["M2"]["raw"]
        segs = list(prepare(MATS["M2"], True).ja_segments)
        self.assertEqual(len(segs), 2)
        emo0, emo1 = segs[0]["emotion"], segs[1]["emotion"]
        self.assertNotIn("src", emo0, "第二句的 cue 不许挂在首段")
        self.assertEqual(emo0["src_scope"], {"id": "0-%d#0" % len(raw), "basis": "span",
                                             "sentence": 0, "unknown": 0, "quoted": 0,
                                             "cross_sentence": "unsupported"})
        cue = emo1["src"][0]
        self.assertEqual((cue["kind"], cue["negated"]), ("soft", False))
        self.assertEqual(cue["sid"], emo1["src_scope"]["id"], "cue 必须自带本段的源句身份")
        self.assertNotEqual(emo0["src_scope"]["id"], emo1["src_scope"]["id"], "句末身份必须不同")
        # 真规划器读同一份身份：这条 D 只在第二段出现
        recs, state = plan(segs, True)
        self.assertIsNone(delivery_of(recs[0]))
        self.assertEqual(delivery_of(recs[1]), "soft")
        self.assertEqual(recs[1]["diagnostics"]["cue_scope"]["id"], cue["sid"])
        self.assertEqual(recs[1]["diagnostics"]["cue_scope"]["basis"], "span")
        self.assertEqual(state["delivery_scope"], cue["sid"])

    def test_env_switch_turns_it_on(self):
        with mock.patch.dict(os.environ, {"ALBION_CUE_SCOPE": "1"}):
            self.assertTrue(CS.enabled(None))
            segs = list(prepare(MATS["M2"], None).ja_segments)     # None ⇒ 看进程环境
            self.assertNotIn("src", segs[0]["emotion"])
            self.assertIn("src", segs[1]["emotion"])
        os.environ.pop("ALBION_CUE_SCOPE", None)
        self.assertFalse(CS.enabled(None))

    def test_planner_without_identity_does_not_inject(self):
        """规划器自己判不出归属（没有身份）⇒ 不注入 + scope_unknown（不许默认挂本段）。"""
        recs, _ = SP.plan_units_v3([{"unit_id": "u", "text": "今日は早めに寝てくださいね。",
                                     "src_cues": [{"kind": "whisper", "span": [0, 2]}]}],
                                   plan_version=4, allow_neutral_whisper=True, weak_delivery=True,
                                   cue_scope=True)
        r = recs[0]
        self.assertIsNone(delivery_of(r))
        self.assertEqual(r["diagnostics"]["cue_scope"]["id"], None)
        self.assertEqual(r["diagnostics"]["cue_scope"]["out_of_scope"], 1)
        self.assertEqual(r["diagnostics"]["delivery_stage"]["reason"], "ambiguous_only")
        self.assertNotEqual(r["caption"], A_WHISPER)

    def test_plan_endpoint_flag_and_response_shape(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        SP.install(app)
        c = TestClient(app)
        unit = {"unit_id": "u0", "text": "今日は早めに寝てくださいね。",
                "emotion": {"src_scope": {"id": "k#0", "basis": "span"},
                            "src": [{"kind": "whisper", "span": [0, 2], "sid": "k#0",
                                     "sentence": 0}]}}
        on = c.post("/v2/plan", json={"plan_version": 4, "units": [unit], "cue_scope": True})
        self.assertEqual(on.status_code, 200)
        self.assertIs(on.json().get("cue_scope"), True)
        self.assertEqual(on.json()["units"][0]["diagnostics"]["delivery"], "whispered")
        off = c.post("/v2/plan", json={"plan_version": 4, "units": [unit]})
        self.assertNotIn("cue_scope", off.json(), "开关关时响应形状不许变")
        self.assertNotIn("cue_scope", off.json()["units"][0]["diagnostics"])
        self.assertEqual(off.json()["units"][0]["diagnostics"]["delivery"], "whispered")


# ---------------- 层③+材料：最终记录 ---------------------------------------
class TestFinalRecords(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ALBION_CUE_SCOPE", None)

    def test_M1_cue_stays_in_first_sentence(self):
        recs, segs, state = run(MATS["M1"], True)
        self.assertEqual(delivery_of(recs[0]), "soft")
        self.assertIsNone(delivery_of(recs[1]), "第二句没有重新得到这条 D")
        self.assertIn("delivery=soft", recs[0]["reason"])
        self.assertNotIn("delivery=", recs[1]["reason"])
        self.assertEqual(recs[1]["diagnostics"]["delivery_stage"]["reason"], "stale_context")
        self.assertEqual([r["family"] for r in recs], ["neutral", "neutral"])
        self.assertEqual([r["tier"] for r in recs], ["weak", "weak"])
        self.assertEqual(state["delivery_scope"], None, "句末即失效：出句后没有生效中的指令")
        self.assertEqual(segs[1]["emotion"]["src_scope"]["id"], "0-%d#1" % len(MATS["M1"]["raw"]))

    def test_M1_pair_expired_cue_does_not_move_the_next_sentence(self):
        """成对比较：同样上下文，有无那条**已到期**的 cue —— 下句的条件不许被它改动。"""
        prev = {"family": "neutral", "level": 0.45, "tier": "weak"}
        seg = list(prepare(MATS["M1"], True).ja_segments)[1]          # 第二句（自己没有 cue）
        base = [{"unit_id": "u0:1", "text": seg["text"], "advance": True,
                 "emotion": copy.deepcopy(seg["emotion"])}]
        got, _ = SP.plan_units_v3(base, plan_version=4, allow_neutral_whisper=True,
                                  weak_delivery=True, cue_scope=True, prev=copy.deepcopy(prev))
        # 同样上下文 + 一条**属于别的源句**的 cue（= 已到期的那条）
        stale = copy.deepcopy(base)
        stale[0]["emotion"]["src"] = [{"kind": "soft", "span": [0, 5], "sid": "别的源句#9",
                                       "sentence": 9}]
        gotstale, _ = SP.plan_units_v3(stale, plan_version=4, allow_neutral_whisper=True,
                                       weak_delivery=True, cue_scope=True,
                                       prev=copy.deepcopy(prev))
        self.assertEqual(plain(got), plain(gotstale), "已到期的 cue 改了下句的条件")
        self.assertFalse(delivery_of(gotstale[0]))
        self.assertEqual(gotstale[0]["diagnostics"]["cue_scope"]["out_of_scope"], 1)
        # 到期**不等于**强制回 neutral：有独立依据的族照旧保留
        keep, _ = SP.plan_units_v3([{"unit_id": "u", "text": "はぁ……んっ……",
                                     "emotion": {"src_scope": {"id": "k#1", "basis": "span"}}}],
                                   plan_version=4, allow_neutral_whisper=True,
                                   weak_delivery=True, cue_scope=True)
        self.assertEqual(keep[0]["family"], "intimate", "到期不该把它压回 neutral")

    def test_M2_cue_goes_to_the_second_sentence(self):
        recs, _, _ = run(MATS["M2"], True)
        self.assertIsNone(delivery_of(recs[0]), "cue 属于第二句，不许挂到第一句")
        self.assertEqual(delivery_of(recs[1]), "soft")
        self.assertEqual(recs[1]["diagnostics"]["delivery_stage"]["reason"], "explicit_directive")
        self.assertIn("delivery=soft", recs[1]["reason"])
        self.assertGreater(recs[0]["level"], recs[1]["level"], "第一句没有音量指令，档位更高")

    def test_M2b_whisper_cue_moves_the_caption_of_its_own_sentence(self):
        recs, _, _ = run(MATS["M2b"], True)
        self.assertIsNone(delivery_of(recs[0]))
        self.assertEqual(delivery_of(recs[1]), "whispered")
        self.assertEqual(plain(recs)[0][1], A_QUIET)
        self.assertEqual(plain(recs)[1][1], A_WHISPER)
        self.assertEqual(recs[1]["cfg"], 3.0)                      # 仍是 neutral 族
        self.assertEqual(recs[1]["diagnostics"]["gap"], "neutral_whisper_candidate_untested")

    def test_M3_two_cues_two_sentences(self):
        recs, _, _ = run(MATS["M3"], True)
        self.assertEqual([delivery_of(r) for r in recs], ["soft", "whispered", None])
        self.assertEqual(recs[2]["diagnostics"]["cue_scope"]["cues"], 0, "第三句不复用局部 D")
        self.assertEqual([r["family"] for r in recs], ["neutral"] * 3)
        self.assertEqual(plain(recs)[0][1], A_QUIET)
        self.assertEqual(plain(recs)[1][1], A_WHISPER)
        self.assertEqual(plain(recs)[2][1], A_QUIET, "第三句不该复用第二句的局部 D")
        # 材料原文里的「（气息不稳）」在抽取层是 whisper（bare「气息」），不是 breath —— 如实记下
        self.assertEqual([c["kind"] for c in bridge.zh_source_cues(MATS["M3"]["raw"])],
                         ["soft", "whisper"])

    def test_M4_quoted_prompt_word_gives_no_D(self):
        recs, segs, _ = run(MATS["M4"], True)
        self.assertEqual(len(segs), 1)
        self.assertIsNone(delivery_of(recs[0]), "引述里的提示词不产生 D")
        self.assertNotEqual(recs[0]["caption"], A_WHISPER)
        self.assertEqual(recs[0]["family"], "neutral")
        emo = segs[0]["emotion"]
        self.assertNotIn("src", emo, "被引述的 cue 不该被当成指令带下去")
        self.assertEqual(emo["src_scope"]["quoted"], 1, "引述要记在诊断里")
        self.assertEqual(emo["src_scope"]["basis"], "span")
        full = dict(MATS["M4"], raw="他说“（小声说）”只是舞台提示。")     # 全角引号也一样
        self.assertIsNone(delivery_of(run(full, True)[0][0]))
        corner = dict(MATS["M4"], raw="他说(小声)只是舞台提示。")          # 半角括号里的 bare 词
        self.assertIsNone(delivery_of(run(corner, True)[0][0]))

    def test_M5_negated_cue_is_not_soft_evidence(self):
        recs, segs, _ = run(MATS["M5"], True)
        self.assertIsNone(delivery_of(recs[0]), "否定不能变成正向弱声证据")
        self.assertEqual(recs[0]["diagnostics"]["delivery_stage"]["reason"], "ambiguous_only")
        self.assertEqual(recs[0]["family"], "neutral")
        self.assertNotEqual(recs[0]["caption"], A_WHISPER)
        self.assertTrue(segs[0]["emotion"]["src"][0]["negated"], "反证仍要带下去（供闸门排除）")
        self.assertEqual(recs[0]["diagnostics"]["cue_scope"]["cues"], 1)

    def test_M6_repeats_emoji_newline(self):
        recs, segs, _ = run(MATS["M6"], True)
        self.assertEqual([delivery_of(r) for r in recs], ["soft", None, "whispered"])
        self.assertEqual(recs[1]["diagnostics"]["cue_scope"]["cues"], 0)
        ids = [s["emotion"]["src_scope"]["id"] for s in segs]
        self.assertEqual(len(set(ids)), 3, "三句三个身份")
        self.assertEqual(len(segs[0]["emotion"]["src"]), 3, "同句里三条 soft 都是这一句的")
        self.assertEqual([c["sentence"] for c in segs[0]["emotion"]["src"]], [0, 0, 0])

    def test_M9_illegal_span_is_scope_unknown(self):
        def bad(text):
            return [{"kind": "soft", "negated": False, "span": [999, 1001]}]
        svc = bridge_v2.PrepareService(translate=translator(), cues=bad, cue_scope=True)
        req = bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id="u0", voice="albion",
                                       source_range=(0, 11), speech_text=MATS["M9"]["speech"],
                                       raw_text=MATS["M9"]["raw"])
        segs = list(asyncio.run(svc.prepare(req)).ja_segments)
        sc = segs[0]["emotion"]["src_scope"]
        # 这一段自己还是能定位（1 句 1 子段）；是**那条 cue** 的跨度非法 ⇒ 记进 unknown、不注入
        self.assertEqual((sc["basis"], sc["id"], sc["unknown"]), ("span", "0-11#0", 1))
        self.assertNotIn("src", segs[0]["emotion"], "判不出归属就不注入（不许默认挂首句）")
        recs, _ = plan(segs, True)
        self.assertIsNone(delivery_of(recs[0]))
        # 归属不明的是**那条 cue**（桥侧 unknown=1，没被带下去）；子段自己的身份仍然有效
        self.assertEqual(recs[0]["diagnostics"]["cue_scope"]["id"], "0-11#0")
        self.assertEqual(recs[0]["diagnostics"]["cue_scope"]["cues"], 0)

    def test_cross_sentence_directive_recorded_unsupported(self):
        """跨句指令（"接下来都小声说"）本轮不扩展：记 unsupported，下一句不继承。"""
        segs = list(prepare(MATS["XC"], True).ja_segments)
        self.assertEqual(segs[0]["emotion"]["src_scope"]["cross_sentence"], "unsupported")
        r0, st0 = plan(segs[:1], True)
        self.assertEqual(delivery_of(r0[0]), "soft", "它自己所在的句子照旧生效")
        self.assertEqual(st0["delivery_scope"], "0-%d#0" % len(MATS["XC"]["raw"]))
        r1, st1 = plan(segs[1:], True, prev=st0)
        self.assertIsNone(delivery_of(r1[0]), "但不许自动扩到下一句")
        self.assertIsNone(st1["delivery_scope"], "句末即失效：跨句指令不给续期")
        self.assertEqual(r1[0]["diagnostics"]["cue_scope"]["cross_sentence"], CS.CROSS_SENTENCE)
        self.assertIsNone(run(MATS["XC"], None)[2].get("delivery_scope"),
                          "开关关：末态里连这个字段都没有")

    def test_M9_bilingual_slice_count_mismatch_is_scope_unknown(self):
        """日语直通切片（1 个子段）对中文原文（2 个源句）：跨坐标系 ⇒ 整份不注入。"""
        segs = list(prepare(MATS["M2"], True, speech=EXTRA_CASES["BI"]["speech"]).ja_segments)
        self.assertEqual(len(segs), 1, "材料不成立：这条要的是子段数少于源句数")
        sc = segs[0]["emotion"]["src_scope"]
        self.assertEqual((sc["basis"], sc["id"]), ("scope_unknown", None))
        self.assertNotIn("src", segs[0]["emotion"], "跨坐标系时不许拿中文下标当日语下标")
        recs, _ = plan(segs, True)
        self.assertIsNone(delivery_of(recs[0]))


class TestMultiSegmentScope(unittest.TestCase):
    """材料⑦：一个源句 → 两个日文子段（壳侧逐子段规划的真实形状）。

    今天桥对**一个源句只出一个子段**（子段数 = 源句数），所以第二个子段接的是这一句译文的
    另一片：身份与线索都用真桥给的那一份（不改身份），只有文本是另一片。
    """

    ONE = {"raw": "（小声说）今天早点休息。", "speech": "今天早点休息。"}

    def setUp(self):
        os.environ.pop("ALBION_CUE_SCOPE", None)

    def _two_subsegments(self, cue_scope):
        seg = list(prepare(self.ONE, cue_scope).ja_segments)[0]
        return [{"sub_index": i, "text": t, "emotion": copy.deepcopy(seg["emotion"])}
                for i, t in enumerate(M7_TEXTS)]

    def test_shared_scope_does_not_step_twice_nor_refresh_trust(self):
        segs = self._two_subsegments(True)
        recs, state = plan(segs, True)
        self.assertEqual([delivery_of(r) for r in recs], ["soft", "soft"], "两条子段都受该句指令")
        self.assertEqual(recs[0]["level"], recs[1]["level"], "同一源句不重复加档")
        self.assertIn("repeat_in_scope", recs[1]["reason"])
        self.assertIs(recs[1]["diagnostics"]["cue_scope"]["repeat"], True)
        self.assertEqual(recs[1]["diagnostics"]["delivery_stage"]["reason"],
                         "explicit_directive_repeat")
        self.assertEqual(state["delivery_scope"], recs[0]["diagnostics"]["cue_scope"]["id"])
        self.assertTrue(state["delivery_trusted"], "可信链没断，但也不是重新授予")
        # 对照：同一条 cue 落在**另一个源句**上 —— 那才会第二次推进强度
        other = copy.deepcopy(segs)
        for i, s in enumerate(other):
            s["emotion"]["src_scope"]["id"] = "k#%d" % i
            for c in s["emotion"]["src"]:
                c["sid"] = "k#%d" % i
        fresh, _ = plan(other, True)
        self.assertGreater(fresh[1]["level"], fresh[0]["level"], "换源句时照旧推进强度")
        self.assertIs(fresh[1]["diagnostics"]["cue_scope"]["repeat"], False)

    def test_off_state_does_step_twice(self):
        """开关关时同一条指令会在两个子段上各推一次强度 —— 这正是 ① 要修的地方。"""
        recs, _ = plan(self._two_subsegments(None), None)
        self.assertEqual([delivery_of(r) for r in recs], ["soft", "soft"])
        self.assertGreater(recs[1]["level"], recs[0]["level"])

    def test_scope_expiry_does_not_hand_a_free_hop_to_the_next_sentence(self):
        recs, state = plan(self._two_subsegments(True), True)
        nxt, _ = plan([{"sub_index": 2, "text": "明日の朝、呼びますね。", "emotion": None}],
                      True, prev=state)
        self.assertIsNone(delivery_of(nxt[0]), "下句只能靠自己或既有的一跳，不能白拿可信 D")
        self.assertEqual(nxt[0]["diagnostics"]["delivery_stage"]["reason"], "stale_context")
        self.assertIsNone(nxt[0]["diagnostics"]["cue_scope"]["id"])


class TestChunkingAndOrder(unittest.TestCase):
    """材料⑧/⑩：完成顺序、分块方式都不改变归属与最终条件序列。"""

    def setUp(self):
        os.environ.pop("ALBION_CUE_SCOPE", None)

    def test_M10_chunking_does_not_change_the_condition_sequence(self):
        whole, _, _ = run(MATS["M3"], True)
        whole_segs = [s["text"] for s in prepare(MATS["M3"], True).ja_segments]
        self.assertEqual(len(whole_segs), 3)
        chunks, prev, off = [], None, 0
        # 按生产形状喂：raw_text 带提示、speech_text 已剥离提示（替身对未登记输入会立刻失败，
        # 所以这里必须按真形状走 —— Sol 第 10 轮第二条）。
        for i, (raw, speech) in enumerate([("（小声说）甲句。", "甲句。"),
                                           ("（气息不稳）乙句。", "乙句。"),
                                           ("丙句。", "丙句。")]):
            res = prepare(MATS["M3"], True, unit_id="u%d" % i, src=(off, off + len(raw)),
                          raw=raw, speech=speech)
            off += len(raw)
            # 先证明两种送法拿到的是**同一份译文**，再比最终条件
            self.assertEqual([s["text"] for s in res.ja_segments], whole_segs[i:i + 1],
                             "分块送法 %d 的日文子段与整段送法不一致" % i)
            recs, prev = plan(list(res.ja_segments), True, prev=prev)
            chunks.append(recs[0])
        self.assertEqual(plain(chunks), plain(whole), "分块进入 S 不该改变最终条件序列")
        # 归属跟着源句走，不跟着块走
        self.assertEqual([c["kind"] for c in
                          list(prepare(MATS["M3"], True).ja_segments)[1]["emotion"]["src"]],
                         ["whisper"])

    def test_M8_completion_order_does_not_change_attribution_or_playback_order(self):
        """两个源句、第二次翻译先返回：归属与最终播出顺序按**源序**，不按完成时刻。"""

        def tr(sentence):
            if _punct_free(sentence) == "今天早点休息":               # 第一句慢一点
                time.sleep(0.2)
            return translator()(sentence)

        svc = bridge_v2.PrepareService(translate=tr, cues=bridge.zh_source_cues, cue_scope=True)

        async def go():
            def mk(uid, rng, raw, sp):
                return bridge_v2.PrepareRequest(session_id="s", generation=1, unit_id=uid,
                                                voice="albion", source_range=rng, raw_text=raw,
                                                speech_text=sp)
            a = mk("u0", (0, 14), "（小声说）今天早点休息。", "今天早点休息。")
            b = mk("u1", (14, 26), "明天早上我叫你。", "明天早上我叫你。")
            return await asyncio.gather(svc.prepare(b), svc.prepare(a))    # b 先发起、先完成
        rb, ra = asyncio.run(go())
        self.assertTrue(all(r.status == "ready" for r in (ra, rb)))
        self.assertEqual([r.unit_id for r in (rb, ra)], ["u1", "u0"],
                         "材料不成立：第二句应当先完成")
        by_order = bridge_v2.count_by_order({r.unit_id: r for r in (ra, rb)}, ["u0", "u1"])
        self.assertEqual([r.unit_id for r in by_order], ["u0", "u1"], "播出顺序按源序")
        self.assertEqual(by_order[0].ja_segments[0]["emotion"]["src"][0]["kind"], "soft")
        self.assertNotIn("src", by_order[1].ja_segments[0]["emotion"])
        self.assertNotEqual(by_order[0].ja_segments[0]["emotion"]["src_scope"]["id"],
                            by_order[1].ja_segments[0]["emotion"]["src_scope"]["id"])
        recs0, st = plan(list(by_order[0].ja_segments), True, uid="u0")
        recs1, _ = plan(list(by_order[1].ja_segments), True, prev=st, uid="u1")
        self.assertEqual(delivery_of(recs0[0]), "soft")
        self.assertIsNone(delivery_of(recs1[0]))
        self.assertEqual([r["unit_id"] for r in recs0 + recs1], ["u0:0", "u1:0"])


# ---------------- 层④：开关关的等价 + 反事实 --------------------------------
class TestSwitchOffEquivalence(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ALBION_CUE_SCOPE", None)

    def test_off_matches_frozen_v4(self):
        """冻结用例：关掉开关时必须与加这个开关之前**逐字段**相同（含 diagnostics）。"""
        with open(GOLDEN, encoding="utf-8") as f:
            golden = json.load(f)
        got = {}
        for name in sorted(GOLDEN_CASES):
            recs, _, _ = run(GOLDEN_CASES[name], None)
            got[name] = json.loads(json.dumps(recs, ensure_ascii=False, sort_keys=True))
        self.assertEqual(sorted(got), sorted(golden))
        for name in sorted(got):
            self.assertEqual(got[name], golden[name], "%s 的 v4（开关关）输出变了" % name)

    def test_off_keeps_the_old_wrong_attachment_visible(self):
        """现状记录：开关关时 cue 仍整份挂首段（第二句的 cue 挂错段）—— 开关开时被修正。"""
        off = list(prepare(MATS["M2"], None).ja_segments)
        on = list(prepare(MATS["M2"], True).ja_segments)
        self.assertEqual(off[0]["emotion"]["src_scope"], "unknown_first_segment")
        self.assertEqual(off[1]["emotion"]["src_scope"], "unknown_after_first_segment")
        self.assertEqual(off[0]["emotion"]["src"][0]["kind"], "soft")   # 挂错了：它属于第二句
        self.assertNotIn("src", off[1]["emotion"])
        self.assertNotIn("src", on[0]["emotion"])                       # 修好了
        self.assertEqual(on[1]["emotion"]["src"][0]["kind"], "soft")
        off_recs, _ = plan(off, None)
        on_recs, _ = plan(on, True)
        self.assertEqual([delivery_of(r) for r in off_recs], ["soft", None])
        self.assertEqual([delivery_of(r) for r in on_recs], [None, "soft"])
        # soft 只动音量档、不动锚，所以拿**耳语**那条（M2b）看最终条件真的分岔了
        offb, _ = plan(list(prepare(MATS["M2b"], None).ja_segments), None)
        onb, _ = plan(list(prepare(MATS["M2b"], True).ja_segments), True)
        self.assertEqual(plain(offb)[0][1], A_WHISPER, "错挂：耳语档落在第一句")
        self.assertEqual(plain(onb)[0][1], A_QUIET, "正确归属：第一句不该变")
        self.assertEqual(plain(onb)[1][1], A_WHISPER, "正确归属：耳语档落在它所在的第二句")
        self.assertNotEqual(plain(offb), plain(onb), "错挂与正确归属的最终记录必须不同")
        self.assertEqual([delivery_of(r) for r in offb], ["whispered", None])
        self.assertEqual([delivery_of(r) for r in onb], [None, "whispered"])

    def test_off_does_not_add_any_new_field(self):
        recs, _, state = run(MATS["M1"], None)
        self.assertNotIn("cue_scope", recs[0]["diagnostics"])
        self.assertNotIn("delivery_scope", state)
        self.assertEqual(sorted(recs[0]["diagnostics"]),
                         ["delivery", "delivery_rel", "delivery_stage", "gap", "policy"])


_ORIG_ATTRIBUTE = CS.attribute          # 反事实替身要调的是**真实现**，不是被换掉的那个名字


def _legacy_attach_to_first(text, cues, n_segments, unit_key="", **kw):
    """旧坏行为①：整份 cue 挂首段（就是现状）。"""
    rows, diag = _ORIG_ATTRIBUTE(text, cues, n_segments, unit_key=unit_key)
    sid = (diag["ids"] or [None])[0]
    first = [dict(c, sid=sid) for c in (cues or [])]
    return ([first] + [[] for _ in range(max(0, len(rows) - 1))]), diag


def _legacy_copy_to_every_segment(text, cues, n_segments, unit_key="", **kw):
    """旧坏行为②：跨源句复制 D（每个子段都拿到整份 cue，各自盖章）。"""
    rows, diag = _ORIG_ATTRIBUTE(text, cues, n_segments, unit_key=unit_key)
    return [[dict(c, sid=diag["ids"][i]) for c in (cues or [])] for i in range(len(rows))], diag


class TestCounterfactual(unittest.TestCase):
    """反事实：把旧坏行为放回去，本文件的断言必须**变红**（否则测试是空转的）。"""

    def setUp(self):
        os.environ.pop("ALBION_CUE_SCOPE", None)

    def _positive(self):
        """会被"错挂首句 / 跨句复制"弄红的正断言（与上面的正测试同口径）。"""
        recs, _, _ = run(MATS["M2b"], True)
        self.assertIsNone(delivery_of(recs[0]))
        self.assertEqual(delivery_of(recs[1]), "whispered")
        self.assertEqual(plain(recs)[0][1], A_QUIET)
        self.assertEqual(plain(recs)[1][1], A_WHISPER)

    def test_positive_assertions_hold_normally(self):
        self._positive()

    def test_attach_to_first_segment_turns_red(self):
        with mock.patch.object(CS, "attribute", _legacy_attach_to_first):
            with self.assertRaises(AssertionError):
                self._positive()

    def test_cross_sentence_copy_turns_red(self):
        with mock.patch.object(CS, "attribute", _legacy_copy_to_every_segment):
            with self.assertRaises(AssertionError):
                self._positive()


if __name__ == "__main__":
    unittest.main(verbosity=2)
