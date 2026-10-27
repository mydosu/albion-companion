# -*- coding: utf-8 -*-
"""受控声音描述 + 句末非语言 emoji（阶段 3d，2026-10-26）。

锁四件事（对应任务 A/B/C）：
  * **词表校验**：只有「声音动作词 + 少し/とても + 句末动词」拼出来的 10–40 字描述能用；
    抽象情绪词、超长、空、结构不对一律判废（`voice_caption.validate_caption`）；
  * **回退路径**：描述判废时桥不产字段、规划器不认字段 —— 这句仍走原来的码本锚；
  * **开关关闭时输出不变**：`ALBION_VOICE_CAPTION` / `ALBION_VOICE_EMOJI` 都没开时，
    桥的准备结果逐字段与以前相同（含模型自己写的 emoji 原样保留）；
  * **emoji 限额与剥离**：每条回复最多 1 个、白名单外的全剥掉、句末才加。

另加两条接线检查：规划器把描述当 `caption_mode="free"` 发出去后，渲染队列的白名单校验
认得它（`render_queue._check_unit` 返回 None）；网关 `/v2/plan` 请求体只在 v3 且桥真的写了
描述时才带 `voice_caption` 字段。

快哭 / 真哭（2026-10-26 用户口径）：`cry` 是快哭/委屈（描述不变，中文 委屈/想哭/快哭/哽咽/
眼眶红 也算）；`sob` 只认明示线索（中文括号动作、拟声 呜哇/哇——、日语 しくしく/うわーん/
えーん/泣きじゃくり/嗚咽），否定排除，标量永不产出 sob；😭 只跟真哭走。两者都优先于
`phonation`（生产实测「呜呜…要哭了」被标 breathy 时曾拿到 coq 描述）。

不联网、不用 GPU：翻译腿是替身，`/v2/plan` 用 httpx.MockTransport。
"""
import asyncio
import json
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tts", "irodori"))
sys.path.insert(0, os.path.join(ROOT, "tts", "stream"))

import bridge_v2 as B2        # noqa: E402
import cue_scope as CS       # noqa: E402
import emotion_codebook as CB  # noqa: E402
import render_queue as RQ     # noqa: E402
import scene_plan as SP       # noqa: E402
import session_text as ST     # noqa: E402
import stream_server as SS    # noqa: E402
import voice_caption as VC   # noqa: E402

ON = {VC.VOICE_CAPTION_ENV: "1", VC.VOICE_EMOJI_ENV: "1"}
OFF = {VC.VOICE_CAPTION_ENV: "0", VC.VOICE_EMOJI_ENV: "0"}


def req(unit_id="u0", speech="今天有点累了", raw=None, session="s1", gen=1, prefix=""):
    body = {
        "session_id": session, "generation": gen, "unit_id": unit_id,
        "voice": "albion", "source_range": [0, len(speech)],
        "speech_text": speech, "raw_text": raw if raw is not None else speech,
    }
    if prefix:
        body["source_prefix"] = prefix
    return B2.parse_request(body)


def svc(ja="少しだけ、休ませてください。", emo=None):
    calls = []

    def translate_ex(text):
        calls.append(text)
        return ja, (emo if emo is not None else {"arousal": 0.45, "intensity": 0.4,
                                                 "valence": -0.4, "phonation": "voiced"})

    return B2.PrepareService(translate=translate_ex, translate_ex=translate_ex), calls


def prepare(service, request):
    return asyncio.run(service.prepare(request))


class TestVocab(unittest.TestCase):
    def test_every_scene_level_is_valid_and_bounded(self):
        n = 0
        for scene in VC.SCENES:
            for level in VC.LEVELS:
                cap = VC.compose_caption(scene, level)
                self.assertIsNone(VC.validate_caption(cap), cap)
                self.assertGreaterEqual(len(cap), VC.CAPTION_MIN, cap)
                self.assertLessEqual(len(cap), VC.CAPTION_MAX, cap)
                # 后端自由描述那道闸也要过（≤120 字、无控制字符）
                self.assertIsNone(RQ.free_caption_error(cap), cap)
                n += 1
        self.assertEqual(n, 30)              # 10 场景（含真哭 sob）× 3 档

    def test_no_abstract_emotion_words(self):
        """描述里不许出现抽象情绪词（评测台结论：那些模型没反应）。"""
        banned = ("恥ずかし", "甘えた", "得意げ", "悲し", "嬉し", "怒り", "不安")
        for scene in VC.SCENES:
            cap = VC.compose_caption(scene, "normal")
            for word in banned:
                self.assertNotIn(word, cap)

    def test_same_scene_same_level_is_stable(self):
        """相邻句同场景同强度 → 同一个描述（不跳）；族/强度真变了才换。"""
        self.assertEqual(VC.compose_caption("sad", "normal"), VC.compose_caption("sad", "normal"))
        self.assertNotEqual(VC.compose_caption("sad", "weak"),
                            VC.compose_caption("sad", "strong"))
        self.assertNotEqual(VC.compose_caption("sad", "normal"),
                            VC.compose_caption("exc", "normal"))

    def test_rejects_out_of_vocab(self):
        bad = [
            ("恥ずかしそうに、声を潜めて、話す。", "词表外实词"),      # 抽象情绪词
            ("低い声で、鼻歌まじりで、話す。", "词表外实词"),          # 词表外动作
            ("低い声で、語尾を下げて。", "结构不对"),                  # 只有两段
            ("低い声で、語尾を下げて、呟く。", None),                  # 正确样例（对照）
        ]
        for text, why in bad:
            err = VC.validate_caption(text)
            if why is None:
                self.assertIsNone(err, text)
            else:
                self.assertIn(why, err or "", text)

    def test_rejects_empty_long_and_control(self):
        self.assertEqual(VC.validate_caption(""), "空描述")
        self.assertEqual(VC.validate_caption("   "), "空描述")
        self.assertEqual(VC.validate_caption(None), "不是字符串")
        self.assertIn("过长", VC.validate_caption("低い声で、語尾を下げて、" + "呟く" * 20))
        self.assertIn("过短", VC.validate_caption("低い声で、"))
        self.assertIn("控制字符", VC.validate_caption("低い声で、\u200b語尾を下げて、呟く。"))


class TestSceneOf(unittest.TestCase):
    def test_cues_beat_scalars(self):
        cues = {"arousal": 0.1, "valence": 0.9, "src": [{"kind": "whisper"}]}
        self.assertEqual(VC.scene_of(cues), "whi")

    def test_negated_cue_is_ignored(self):
        cues = {"arousal": 0.5, "valence": 0.0, "src": [{"kind": "whisper", "negated": True}]}
        self.assertEqual(VC.scene_of(cues), "neutral")

    def test_phonation_then_scalars(self):
        self.assertEqual(VC.scene_of({"phonation": "breathy", "valence": 0.0}), "coq")
        self.assertEqual(VC.scene_of({"valence": -0.7, "arousal": 0.3}), "cry")
        self.assertEqual(VC.scene_of({"valence": -0.5, "arousal": 0.8}), "ang")
        self.assertEqual(VC.scene_of({"valence": 0.6, "arousal": 0.8}), "exc")
        self.assertEqual(VC.scene_of({}), "neutral")

    def test_level_buckets(self):
        self.assertEqual(VC.level_of({"intensity": 0.1}), "weak")
        self.assertEqual(VC.level_of({"intensity": 0.5}), "normal")
        self.assertEqual(VC.level_of({"intensity": 0.9}), "strong")
        self.assertEqual(VC.level_of({}), "normal")


class TestSobIsRealCry(unittest.TestCase):
    """真哭（sob）只认明示线索；快哭/委屈仍走 cry（2026-10-26 用户口径）。"""

    def test_explicit_sob_cues(self):
        zh_cases = ["（哭）对不起……", "（大哭）不要走", "（哭出声）我忍不住了",
                    "（抽泣）……嗯。", "（啜泣着）没事的", "（哭着说）别丢下我",
                    "呜哇——", "呜呜哇，好难过", "哇哇大哭，停不下来"]
        for zh in zh_cases:
            self.assertTrue(VC.sob_of(zh, ""), zh)
            self.assertEqual(VC.scene_of({}, "", zh), "sob", zh)
        ja_cases = ["しくしく、ごめんなさい。", "うわーん、もう嫌。", "えーん、ひどいよ。",
                    "泣きじゃくって、息ができない。", "嗚咽が止まらない。"]
        for ja in ja_cases:
            self.assertTrue(VC.sob_of("", ja), ja)
            self.assertEqual(VC.scene_of({}, ja), "sob", ja)

    def test_negation_is_not_sob(self):
        """否定一律不算真哭（「我才没哭」「泣いてなんか、いません」最多算 cry）。"""
        self.assertFalse(VC.sob_of("我才没哭呢", ""))
        self.assertFalse(VC.sob_of("（别哭）我没事", ""))
        self.assertFalse(VC.sob_of("", "泣いてなんか、いません。"))
        self.assertFalse(VC.sob_of("", "しくしくしてないよ。"))
        self.assertNotEqual(VC.scene_of({}, "", "我才没哭呢"), "sob")
        self.assertNotEqual(VC.scene_of({}, "泣いてなんか、いません。"), "sob")

    def test_wuwu_and_about_to_cry_are_only_cry(self):
        """「呜呜」单独出现、「要哭了/快哭/想哭」→ cry，不是 sob（用户补充口径）。"""
        zh_cases = ["呜呜……", "呜呜……我、我好像要哭了。对不起，指挥官……",
                    "我快哭了", "想哭", "好委屈……", "眼眶红了"]
        for zh in zh_cases:
            self.assertFalse(VC.sob_of(zh, ""), zh)
            self.assertTrue(VC.cry_cue(zh), zh)
            self.assertEqual(VC.scene_of({}, "", zh), "cry", zh)

    def test_cry_beats_phonation(self):
        """生产实测：breathy + valence 负 的那句必须落 cry，不许落 coq。"""
        zh = "呜呜……我、我好像要哭了。对不起，指挥官……"
        ja = "ごめんなさい……っ、わたし、もう……"
        cues = {"phonation": "breathy", "arousal": 0.3, "valence": -0.6, "intensity": 0.5}
        self.assertEqual(VC.scene_of(cues, ja, zh), "cry")
        self.assertEqual(VC.caption_for(cues, ja, zh), VC.compose_caption("cry", "normal"))
        # 真哭同样压过 breathy
        self.assertEqual(VC.scene_of(cues, "うわーん、もう。", "（大哭）"), "sob")
        # 没哭的字面还是照旧：breathy 依然落 coq
        self.assertEqual(VC.scene_of(cues, "そうなんだ。", "这样啊。"), "coq")

    def test_scalars_never_produce_sob(self):
        for cues in ({"valence": -0.9, "arousal": 0.1}, {"valence": -0.5, "arousal": 0.9},
                     {"valence": -0.8, "arousal": 0.0, "intensity": 0.9}):
            self.assertNotEqual(VC.scene_of(cues), "sob", cues)

    def test_sob_caption_is_in_vocab_and_distinct(self):
        cap = VC.compose_caption("sob", "normal")
        self.assertIsNone(VC.validate_caption(cap))
        self.assertNotEqual(cap, VC.compose_caption("cry", "normal"))
        self.assertIn("sob", VC.SCENES)
        self.assertEqual(VC.SCENE_FAMILY["sob"], "sad")
        # 真哭措辞定死一条（实测只有这条三句 f0_std 都高过基线）：不分强度
        self.assertEqual(cap, VC.compose_caption("sob", "weak"))
        self.assertEqual(cap, VC.compose_caption("sob", "strong"))
        self.assertEqual(cap, "とても声を震わせて、しゃくり上げて、言う。")
        self.assertIn("しゃくり上げて", cap)

    def test_caption_for_takes_chinese_text(self):
        """caption_for 的新参数（中文原文）决定真哭/快哭。"""
        cues = {"phonation": "breathy", "valence": -0.4}
        self.assertEqual(VC.caption_for(cues, "そうなんだ。", "（大哭）"),
                         VC.compose_caption("sob", "normal"))
        self.assertEqual(VC.caption_for(cues, "そうなんだ。", "呜呜……"),
                         VC.compose_caption("cry", "normal"))
        self.assertEqual(VC.caption_for(cues, "そうなんだ。", ""),
                         VC.compose_caption("coq", "normal"))
        # 位置参数顺序：cues, ja_text, zh_text
        self.assertEqual(VC.caption_for(cues, "うわーん", ""),
                         VC.compose_caption("sob", "normal"))

    def test_sob_emoji_only_for_real_cry(self):
        self.assertEqual(VC.pick_emoji("（大哭）不要走", "", "ごめんなさい"), "😭")
        self.assertEqual(VC.pick_emoji("", "", "うわーん、もう嫌。"), "😭")
        self.assertEqual(VC.pick_emoji("呜呜……我好像要哭了", "", "泣きそう"), "")   # 快哭不加
        self.assertEqual(VC.pick_emoji("好委屈……", "", "つらい"), "")
        self.assertEqual(VC.pick_emoji("我才没哭", "", "泣いてない"), "")
        # 显式 sob 参数（桥里和 caption 共用同一个判定）
        self.assertEqual(VC.pick_emoji("はい。", "", "はい。", sob=True), "😭")
        self.assertEqual(VC.pick_emoji("ふふ、そうなんだ", "", "ふふ", sob=False), "🤭")
        self.assertEqual(VC.pick_emoji("ふふ", "", "ふふ", sob=True), "😭")       # 真哭压过笑
        self.assertIn("😭", VC.EMOJI_WHITELIST)


class TestCryWording(unittest.TestCase):
    """快哭 normal/strong 用旧措辞（#20 用户盲听），weak 保留现措辞。"""

    LEGACY = "今にも泣き出しそうに、声を震わせて話す。"

    def test_normal_and_strong_use_legacy(self):
        for level in ("normal", "strong"):
            cap = VC.compose_caption("cry", level)
            self.assertEqual(cap, self.LEGACY, level)
            self.assertIsNone(VC.validate_caption(cap), level)
            self.assertIsNone(RQ.free_caption_error(cap), level)   # 后端 free 模式那道闸
        self.assertEqual(VC.FIXED_CAPTIONS[("cry", "normal")], self.LEGACY)
        self.assertEqual(VC.FIXED_CAPTIONS[("cry", "strong")], self.LEGACY)

    def test_weak_unchanged(self):
        self.assertEqual(VC.compose_caption("cry", "weak"),
                         "少し声を震わせて、途切れがちに、言う。")
        self.assertIsNone(VC.validate_caption(VC.compose_caption("cry", "weak")))

    def test_other_two_part_caption_still_rejected(self):
        """只有表里那条原句被放行；随手拼的两段描述仍判废。"""
        bad = "今にも泣き出しそうに、声を潜めて話す。"
        self.assertIsNotNone(VC.validate_caption(bad))
        self.assertIn("结构不对", VC.validate_caption(bad))
        self.assertIsNotNone(VC.validate_caption(self.LEGACY + "。"))
        self.assertIsNotNone(VC.validate_caption("とても" + self.LEGACY))

    def test_caption_for_returns_legacy_for_cry_cue(self):
        cues = {"phonation": "breathy", "valence": -0.6, "arousal": 0.3, "intensity": 0.5}
        self.assertEqual(VC.caption_for(cues, "ごめんなさい。", "呜呜……"), self.LEGACY)


class TestCueBoundaries(unittest.TestCase):
    """2026-10-26 审出来的三处误判：惊叹「哇——」、括号里的「快哭」修饰、单独的「哭/眼泪/眼眶」。"""

    def test_wow_is_not_crying(self):
        """「哇——好漂亮」这类惊叹不是哭（原来只看「哇——」会判真哭、句末加 😭）。"""
        for zh in ["哇——好漂亮！", "哇~~~太棒了", "哇——！", "哇，你看那个"]:
            self.assertFalse(VC.sob_of(zh, ""), zh)
            self.assertNotEqual(VC.scene_of({}, "", zh), "sob", zh)
            self.assertEqual(VC.pick_emoji(zh, ""), "", zh)
        # 明确的哭声仍然是真哭、仍然带 😭
        for zh in ["呜哇——", "呜呜哇", "哇哇大哭，停不下来"]:
            self.assertTrue(VC.sob_of(zh, ""), zh)
            self.assertEqual(VC.scene_of({}, "", zh), "sob", zh)
            self.assertEqual(VC.pick_emoji(zh, ""), "😭", zh)

    def test_paren_soft_cry_is_cry_not_sob(self):
        """括号里带「快/要/想/差点」「强忍/忍着/忍住」→ 快哭（cry），不是真哭。"""
        soft = ["（快哭了）", "（要哭了）", "（想哭）", "（差点哭出来）",
                "（眼眶红红的，快要哭出来）", "（强忍着不哭）", "（忍着眼泪）"]
        for zh in soft:
            self.assertFalse(VC.sob_of(zh, ""), zh)
            self.assertEqual(VC.paren_cry(zh), "cry", zh)
            self.assertEqual(VC.scene_of({}, "", zh), "cry", zh)
            self.assertNotEqual(VC.pick_emoji(zh, ""), "😭", zh)
        # 无修饰的括号动作仍是真哭、也仍然带 😭（「要」离哭泣词远时不算修饰）
        hard = ["（哭）", "（大哭）", "（哭出声）", "（抽泣）", "（啜泣）", "（哭着说）",
                "（哭着说要抱抱）"]
        for zh in hard:
            self.assertTrue(VC.sob_of(zh, ""), zh)
            self.assertEqual(VC.paren_cry(zh), "sob", zh)
            self.assertEqual(VC.scene_of({}, "", zh), "sob", zh)
            self.assertEqual(VC.pick_emoji(zh, ""), "😭", zh)
        # 否定还是「没在哭」，成语也不算
        for zh in ["（别哭）我没事", "（不要哭）", "（哭笑不得）"]:
            self.assertIsNone(VC.paren_cry(zh), zh)
            self.assertFalse(VC.sob_of(zh, ""), zh)
            self.assertNotEqual(VC.scene_of({}, "", zh), "cry", zh)
            self.assertNotEqual(VC.scene_of({}, "", zh), "sob", zh)

    def test_standalone_cry_words_do_not_grab_cry(self):
        """单独的「哭 / 眼泪 / 眼眶」不再是 cry 判据（笑、看电影之类不抢 cry）。"""
        for zh in ["笑出眼泪", "哭笑不得", "你哭什么呀，笑一个", "看电影哭了", "眼眶有点干"]:
            self.assertFalse(VC.cry_cue(zh), zh)
            self.assertNotEqual(VC.scene_of({}, "", zh), "cry", zh)
            self.assertNotEqual(VC.scene_of({}, "", zh), "sob", zh)
        # 「快要哭」的说法照旧
        for zh in ["委屈", "想哭", "快哭", "要哭", "哽咽", "哭腔", "眼眶红", "眼圈红",
                   "含泪", "呜呜", "泪汪汪"]:
            self.assertTrue(VC.cry_cue(zh), zh)
            self.assertEqual(VC.scene_of({}, "", zh), "cry", zh)
        # 成语被剥掉后，句子里真的委屈仍然算 cry
        self.assertTrue(VC.cry_cue("哭笑不得，但好委屈"))

    def test_angry_cry_is_cry_not_sob(self):
        """「气哭」一类（气哭 / 气得想哭 / 被气哭 / 要被你气哭了）是快哭，不是真哭。"""
        for zh in ["（要被你气哭了）", "（气哭了）", "（气哭）", "（被你气哭了）",
                   "（气得想哭）", "（气到哭）"]:
            self.assertEqual(VC.paren_cry(zh), "cry", zh)
            self.assertFalse(VC.sob_of(zh, ""), zh)
            self.assertEqual(VC.scene_of({}, "", zh), "cry", zh)
            self.assertNotEqual(VC.pick_emoji(zh, ""), "😭", zh)
        # 括号外的「气哭了」也走快哭（不是 sob）
        self.assertTrue(VC.cry_cue("我气哭了"))
        self.assertEqual(VC.scene_of({}, "", "我气哭了"), "cry")
        self.assertNotEqual(VC.scene_of({}, "", "我气哭了"), "sob")
        # 否定还是不算
        for zh in ["（我才没气哭）", "（没气哭）"]:
            self.assertIsNone(VC.paren_cry(zh), zh)
        # 别的真哭判据没被放宽
        self.assertEqual(VC.paren_cry("（大哭）"), "sob")
        self.assertIsNone(VC.paren_cry("（哭笑不得）"))


def _env_without(*names):
    """复制当前环境、去掉指定变量（用来断言「默认开」）。"""
    env = dict(os.environ)
    for name in names:
        env.pop(name, None)
    return mock.patch.dict(os.environ, env, clear=True)


class TestCryCarry(unittest.TestCase):
    """任务 #18：哭腔延续到下一句（`ALBION_VOICE_CARRY`，默认开，`=0` 关）。"""

    def cap(self, prefix="", cues=None, zh="我先走了。", ja="ごめんなさい。"):
        return VC.caption_for(cues if cues is not None else {}, ja, zh, prefix=prefix)

    def test_mild_cry_carries_weak(self):
        """前一句是快哭/委屈 → 本句 cry/weak（不再突然变回 neutral）。"""
        with _env_without(VC.VOICE_CARRY_ENV):          # 默认开
            for p in ("（快哭了）我真的有点难受。", "有点委屈……", "呜呜……我好像要哭了。"):
                self.assertEqual(self.cap(prefix=p), VC.compose_caption("cry", "weak"), p)

    def test_real_cry_carries_normal_but_never_sob(self):
        """前一句是真哭 → 本句 cry/normal；延续永远不产出 sob（😭 只看本句）。"""
        with _env_without(VC.VOICE_CARRY_ENV):
            for p in ("（大哭）对不起……", "（哭）对不起。", "呜哇——好难过。"):
                cap = self.cap(prefix=p)
                self.assertEqual(cap, VC.compose_caption("cry", "normal"), p)
                self.assertNotEqual(cap, VC.compose_caption("sob", "normal"), p)
                self.assertEqual(VC.pick_emoji("我先走了。", "", "ごめんなさい。"), "", p)

    def test_blank_line_and_three_sentences_back_break(self):
        with _env_without(VC.VOICE_CARRY_ENV):
            self.assertEqual(self.cap(prefix="（大哭）对不起……\n\n换个话题。"),
                             VC.compose_caption("neutral"))
            self.assertEqual(
                self.cap(prefix="（大哭）对不起……我还好。你先坐。现在说吧。"),
                VC.compose_caption("neutral"))

    def test_changed_tone_in_next_sentence_breaks(self):
        """离当前句最近的那句（笑）/哈哈/小声/生气 → 她换语气了，不延续。"""
        with _env_without(VC.VOICE_CARRY_ENV):
            for p in ("（大哭）对不起……（笑）好啦好啦。",
                      "（大哭）对不起……哈哈，逗你的。",
                      "（大哭）对不起……你小声一点。",
                      "（大哭）对不起……我生气了。"):
                self.assertEqual(self.cap(prefix=p), VC.compose_caption("neutral"), p)
            # 否定（我没生气）和「笑不出来」不算换语气，哭腔继续
            for p in ("（大哭）对不起……我没生气。",
                      "（大哭）对不起……我笑不出来。"):
                self.assertEqual(self.cap(prefix=p), VC.compose_caption("cry", "normal"), p)

    def test_closer_sentence_decides_strength(self):
        with _env_without(VC.VOICE_CARRY_ENV):
            # 近的那句是快哭 → weak（不被更早的真哭抬成 normal）
            self.assertEqual(self.cap(prefix="（大哭）对不起……（快哭了）嗯。"),
                             VC.compose_caption("cry", "weak"))
            # 近的那句没线索，回看第二句
            self.assertEqual(self.cap(prefix="（大哭）对不起……我先走了。"),
                             VC.compose_caption("cry", "normal"))
            # 「……」也是句界，最后两句正好落在证据上
            self.assertEqual(self.cap(prefix="今天好累……（大哭）对不起……"),
                             VC.compose_caption("cry", "normal"))

    def test_current_sentence_keeps_own_cry(self):
        with _env_without(VC.VOICE_CARRY_ENV):
            # 本句自己有快哭：用自己的强度（不被前一句的真哭抬成 normal）
            self.assertEqual(self.cap(prefix="（大哭）对不起……", zh="（快哭了）我没事。",
                                      cues={"intensity": 0.1}),
                             VC.compose_caption("cry", "weak"))
            # 本句自己有真哭线索：仍是 sob
            self.assertEqual(self.cap(prefix="（快哭了）别走……", zh="（大哭）不要走。"),
                             VC.compose_caption("sob", "normal"))

    def test_current_non_cry_explicit_cue_blocks_carry(self):
        p = "（大哭）对不起……"
        self.assertIsNone(VC.carry_cry("exc", {}, p))
        self.assertIsNone(VC.carry_cry("whi", {}, p))
        self.assertIsNone(VC.carry_cry("ang", {}, p))
        for kind in ("whisper", "excite", "shy"):
            self.assertIsNone(VC.carry_cry("neutral", {"src": [{"kind": kind}]}, p), kind)
        # 否定的 shy 不算「换语气」
        self.assertEqual(VC.carry_cry("neutral", {"src": [{"kind": "shy", "negated": True}]}, p),
                         ("cry", "normal"))
        # 来自 soft / phonation / 标量的 shy、coq、sad、gen 都能被延续覆盖
        for scene, cues in (("shy", {"src": [{"kind": "soft"}]}),
                            ("coq", {"phonation": "breathy"}),
                            ("sad", {"valence": -0.3}),
                            ("gen", {"valence": 0.25})):
            self.assertEqual(VC.carry_cry(scene, cues, p), ("cry", "normal"), scene)
        # 本句自己判出 cry/sob 就不用延续
        self.assertIsNone(VC.carry_cry("cry", {}, p))
        self.assertIsNone(VC.carry_cry("sob", {}, p))
        # 没有前文 / 前文没哭 → None
        self.assertIsNone(VC.carry_cry("neutral", {}, ""))
        self.assertIsNone(VC.carry_cry("neutral", {}, "今天天气不错。"))

    def test_switch_off_is_old_behavior(self):
        """`ALBION_VOICE_CARRY=0`：与不带 prefix 时逐字节一致。"""
        p = "（大哭）对不起……"
        with mock.patch.dict(os.environ, {VC.VOICE_CARRY_ENV: "0"}, clear=False):
            self.assertEqual(self.cap(prefix=p), VC.compose_caption("neutral"))
            self.assertEqual(self.cap(prefix=p), self.cap(prefix=""))


class TestBridgeCarry(unittest.TestCase):
    def test_source_prefix_carries_cry_without_sob_emoji(self):
        env = dict(ON, **{VC.VOICE_CARRY_ENV: "1"})
        with mock.patch.dict(os.environ, env, clear=False):
            service, _ = svc(ja="ごめんなさい……",
                             emo={"arousal": 0.2, "intensity": 0.3, "valence": 0.0})
            res = prepare(service, req(speech="我先走了。", raw="我先走了。",
                                       prefix="（大哭）对不起……"))
        seg = res.ja_segments[0]
        self.assertEqual(seg["voice_caption"], VC.compose_caption("cry", "normal"))
        self.assertNotIn("😭", seg["text"])          # 延续不加 😭（本句自己没有真哭线索）

    def test_carry_off_in_bridge(self):
        env = dict(ON, **{VC.VOICE_CARRY_ENV: "0"})
        with mock.patch.dict(os.environ, env, clear=False):
            service, _ = svc(ja="ごめんなさい……",
                             emo={"arousal": 0.2, "intensity": 0.3, "valence": 0.0})
            res = prepare(service, req(speech="我先走了。", raw="我先走了。",
                                       prefix="（大哭）对不起……"))
        self.assertEqual(res.ja_segments[0]["voice_caption"],
                         VC.compose_caption("neutral", "weak"))


class TestBridgeOutput(unittest.TestCase):
    def test_switch_off_is_byte_identical(self):
        """开关关着：没有 voice_caption，emoji 原样保留（逐字符等于以前）。"""
        with mock.patch.dict(os.environ, OFF, clear=False):
            service, _ = svc(ja="ふふ、そうなんだ😊")
            res = prepare(service, req())
        seg = res.ja_segments[0]
        self.assertEqual(seg["text"], "ふふ、そうなんだ😊")     # 模型自己写的 emoji 不动
        self.assertNotIn("voice_caption", seg)

    def test_switch_on_adds_valid_caption(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc(emo={"arousal": 0.2, "intensity": 0.3, "valence": -0.5})
            res = prepare(service, req())
        seg = res.ja_segments[0]
        self.assertIn("voice_caption", seg)
        self.assertIsNone(VC.validate_caption(seg["voice_caption"]))
        self.assertEqual(seg["voice_caption"], VC.caption_for(
            {"arousal": 0.2, "intensity": 0.3, "valence": -0.5}))

    def test_caption_confirmed_invalid_is_dropped(self):
        """回退路径：描述判废（这里让生成端返回空）→ 桥不加字段，规划器走锚。"""
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc()
            with mock.patch.object(VC, "caption_for", return_value=""):
                res = prepare(service, req())
        self.assertNotIn("voice_caption", res.ja_segments[0])

    def test_emoji_strip_and_single_quota(self):
        """白名单外的剥掉；同一回复只加 1 个（第二句不再加）。"""
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc(ja="ふふ、そうなんだ😊", emo={"valence": 0.6, "arousal": 0.5})
            first = prepare(service, req("u0", speech="哈哈，真的吗", raw="哈哈，真的吗"))
            second = prepare(service, req("u1", speech="哈哈，又来了", raw="哈哈，又来了"))
        t0 = first.ja_segments[0]["text"]
        t1 = second.ja_segments[0]["text"]
        self.assertNotIn("😊", t0)                  # 非法/不受控的 emoji 剥掉了
        self.assertTrue(t0.endswith("🤭"), t0)      # 只在句末加一个
        self.assertEqual(t0.count("🤭"), 1)
        self.assertFalse(t1.endswith("🤭"), t1)     # 同一回复第二个单元不再加
        self.assertNotIn("😊", t1)

    def test_new_generation_resets_quota(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc(ja="ふふ、そうなんだ", emo={"valence": 0.6, "arousal": 0.5})
            prepare(service, req("u0", session="s1", gen=1))
            nxt = prepare(service, req("u0", session="s1", gen=2))
        self.assertTrue(nxt.ja_segments[0]["text"].endswith("🤭"))

    def test_emoji_only_when_line_actually_laughs(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc(ja="はい、わかりました。", emo={"valence": 0.0, "arousal": 0.3})
            res = prepare(service, req(speech="好的，我知道了。", raw="好的，我知道了。"))
        self.assertFalse(res.ja_segments[0]["text"].endswith(("🤭", "😮‍💨", "🥱", "😭")))  # 😭 也不在句末

    def test_real_cry_gets_sob_caption_and_emoji(self):
        """（大哭）→ 真哭：sob 描述 + 句末 😭（真哭是唯一会带 😭 的路径）。"""
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc(ja="ごめんなさい……っ、わたし、もう……",
                             emo={"phonation": "breathy", "arousal": 0.3,
                                  "valence": -0.6, "intensity": 0.5})
            res = prepare(service, req(speech="（大哭）对不起，我、我已经……",
                                       raw="（大哭）对不起，我、我已经……"))
        seg = res.ja_segments[0]
        self.assertEqual(seg["voice_caption"], VC.compose_caption("sob", "normal"))
        self.assertTrue(seg["text"].endswith("😭"), seg["text"])

    def test_about_to_cry_is_cry_and_has_no_emoji(self):
        """生产实测那句：breathy + 呜呜 + 要哭 → cry 描述（不是 coq），且不加 😭。"""
        zh = "呜呜……我、我好像要哭了。对不起，指挥官……"
        with mock.patch.dict(os.environ, ON, clear=False):
            service, _ = svc(ja="ごめんなさい……っ、わたし、もう……",
                             emo={"phonation": "breathy", "arousal": 0.3,
                                  "valence": -0.6, "intensity": 0.5})
            res = prepare(service, req(speech=zh, raw=zh))
        seg = res.ja_segments[0]
        self.assertEqual(seg["voice_caption"], VC.compose_caption("cry", "normal"))
        self.assertNotEqual(seg["voice_caption"], VC.compose_caption("coq", "normal"))
        self.assertFalse(seg["text"].endswith("😭"), seg["text"])

    def test_pick_emoji_table(self):
        self.assertEqual(VC.pick_emoji("ふふ、そうなんだ"), "🤭")
        self.assertEqual(VC.pick_emoji("はあ、疲れた"), "😮‍💨")
        self.assertEqual(VC.pick_emoji("眠い……"), "🥱")
        self.assertEqual(VC.pick_emoji("うう、泣きそう"), "")     # 😭 下线：只要快哭，不要句末抽泣
        self.assertEqual(VC.pick_emoji("はい、わかりました。"), "")


class TestPlannerAdoption(unittest.TestCase):
    CAP = VC.compose_caption("cry", "strong")

    def unit(self, **extra):
        u = {"unit_id": "u0", "text": "泣いてなんか、いません。", "emotion": {}, **extra}
        return [u]

    def test_off_is_anchor_path(self):
        with mock.patch.dict(os.environ, OFF, clear=False):
            recs, _ = SP.plan_units_v3(self.unit(voice_caption=self.CAP), voice_caption=False)
        self.assertIn(recs[0]["caption"], (CB.A_NEUTRAL, CB.A_QUIET, CB.A_LOW, CB.A_BRIGHT,
                                           CB.A_STRONG, CB.A_BREATHY, CB.A_WHISPER))
        self.assertNotIn("caption_mode", recs[0])
        self.assertGreater(recs[0]["cfg"], 0)          # 锚路径的 cfg 是族值（sad=3.5），不动它

    def test_on_uses_free_caption_and_render_accepts_it(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            recs, _ = SP.plan_units_v3(self.unit(voice_caption=self.CAP), voice_caption=True)
        rec = recs[0]
        self.assertEqual(rec["caption"], self.CAP)
        self.assertEqual(rec["cfg"], VC.FREE_CFG)
        self.assertEqual(rec["caption_mode"], "free")
        self.assertIn("caption=voice", rec["reason"])
        # render_queue 的自由描述分支必须收得下这条计划单元（网关按 u_body 形状补 text/版本）
        body = dict(rec, text="泣いてなんか、いません。",
                    codebook_version=CB.CODEBOOK_VERSION_V2)
        self.assertIsNone(RQ._check_unit(body, 0, plan_version=4), body)

    def test_on_without_caption_still_anchor(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            recs, _ = SP.plan_units_v3(self.unit(), voice_caption=True)
        self.assertNotIn("caption_mode", recs[0])

    def test_invalid_caption_is_ignored(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            recs, _ = SP.plan_units_v3(self.unit(voice_caption="恥ずかしそうに、話す。"),
                                       voice_caption=True)
        self.assertNotIn("caption_mode", recs[0])
        self.assertNotEqual(recs[0]["caption"], "恥ずかしそうに、話す。")

    def test_fusion_path_too(self):
        with mock.patch.dict(os.environ, ON, clear=False):
            recs, _ = SP.plan_units_fusion(self.unit(voice_caption=self.CAP), voice_caption=True)
        self.assertEqual(recs[0]["caption"], self.CAP)
        self.assertEqual(recs[0]["caption_mode"], "free")


class TestGatewayForwarding(unittest.TestCase):
    """网关只做搬运：v3 且桥给了描述时，`/v2/plan` 请求体里才有 `voice_caption`。"""

    def plan_body(self, voice_caption, emotion_mode="v3"):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"plan_version": 4, "units": [{
                "unit_id": "u0", "family": "neutral", "caption": CB.A_QUIET, "cfg": 3.0}],
                "state": {}})

        coord = SS.Coordinator(flux=None) if False else SS.Coordinator(
            cfg=SS.Config(emotion_mode=emotion_mode, plan_version=4),
            transport=httpx.MockTransport(handler))
        asyncio.run(coord.plan("はい。", "u0", voice_caption=voice_caption))
        return seen

    def test_v3_forwards_caption(self):
        body = self.plan_body(VC.compose_caption("sad", "normal"))
        self.assertEqual(body["units"][0]["voice_caption"], VC.compose_caption("sad", "normal"))

    def test_none_and_legacy_do_not_forward(self):
        self.assertNotIn("voice_caption", self.plan_body(None)["units"][0])
        self.assertNotIn("voice_caption",
                         self.plan_body(VC.compose_caption("sad"), "legacy")["units"][0])


ACT_SAD = '<|ACT:{"emotion":"sad"}|>'
ACT_HAPPY = '<|ACT:{"emotion":"happy","intensity":0.3}|>'
ACT_HAPPY_STRONG = '<|ACT:{"emotion":{"name":"happy","intensity":0.9}}|>'
ACT_ANGRY = '<|ACT:{"emotion":"angry"}|>'
ACT_BROKEN = '<|ACT:{"emotion":}|>'


class TestActPrior(unittest.TestCase):
    """任务 #23：ACT 情绪先验进 TTS（`ALBION_VOICE_ACT`，默认开，`=0` 关）。

    口径：本句自己判出的场景 > 哭腔延续 > ACT 先验 > neutral。ACT 只补位，不抢戏：
    不产出 sob/cry、不加 emoji，强度仍由 `level_of(cues)` 决定。
    """

    def cap(self, prefix="", zh="我先走了。", ja="ごめんなさい。", cues=None):
        return VC.caption_for(cues if cues is not None else {}, ja, zh, prefix=prefix)

    def test_mapping_table(self):
        """happy→gen（强→exc）、surprised→exc、sad→sad、angry→ang、awkward/shy→shy。"""
        cases = {
            ACT_SAD: "sad",
            ACT_HAPPY: "gen",
            ACT_HAPPY_STRONG: "exc",
            '<|ACT:{"emotion":"happy"}|>': "gen",              # 没给强度：gen
            '<|ACT:{"emotion":"surprised"}|>': "exc",
            ACT_ANGRY: "ang",
            '<|ACT:{"emotion":"awkward"}|>': "shy",
            '<|ACT:{"emotion":"shy"}|>': "shy",
            '<|ACT:{"emotion":"SHY"}|>': "shy",                # 大小写都认
            '<|ACT {"emotion":"sad"}|>': "sad",                # 冒号可省（与壳 ALBION_ACT_RE 同形）
        }
        for text, scene in cases.items():
            self.assertEqual(VC.act_scene_of(text), scene, text)
        # neutral 族 / 没见过的名字：不给先验（宁可不加戏）
        for name in ("neutral", "think", "question", "curious", "smug", "love"):
            self.assertIsNone(VC.act_scene_of('<|ACT:{"emotion":"%s"}|>' % name), name)
        for text in ("", "没有标记的句子。", ACT_BROKEN,
                     '<|ACT:{"emotion":"sad"',                 # 没闭合：整条不吃
                     '<|ACT:{"emotion":123}|>'):
            self.assertIsNone(VC.act_scene_of(text), text)

    def test_act_only_fills_what_the_sentence_cannot_judge(self):
        with _env_without(VC.VOICE_ACT_ENV):                   # 默认开
            # 本句中性、没有线索 → 用 ACT
            self.assertEqual(self.cap(prefix=ACT_SAD), VC.compose_caption("sad"))
            # 第一个单元没有 prefix：ACT 落在中文原文里（raw_text 从 0 开始）
            self.assertEqual(self.cap(zh=ACT_SAD + "嗯。"), VC.compose_caption("sad"))
            # 本句自己判出 exc（标量）→ 用自己的，不被 ACT sad 覆盖
            self.assertEqual(self.cap(prefix=ACT_SAD,
                                      cues={"valence": 0.6, "arousal": 0.8, "intensity": 0.5}),
                             VC.compose_caption("exc"))
            # 本句有明示线索（兴奋）→ 自己的
            self.assertEqual(self.cap(prefix=ACT_SAD, cues={"src": [{"kind": "excite"}]}),
                             VC.compose_caption("exc"))
            # 本句自己在哭 → sob，ACT 改不了
            self.assertEqual(self.cap(prefix=ACT_SAD, zh="（大哭）不要走。"),
                             VC.compose_caption("sob"))

    def test_level_still_comes_from_cues(self):
        """强度仍走 `level_of(cues)`（arousal 不会自己判出场景，正好单独验档位）。"""
        with _env_without(VC.VOICE_ACT_ENV):
            self.assertEqual(self.cap(prefix=ACT_SAD, cues={"arousal": 0.9}),
                             VC.compose_caption("sad", "strong"))
            self.assertEqual(self.cap(prefix=ACT_SAD, cues={"arousal": 0.1}),
                             VC.compose_caption("sad", "weak"))

    def test_carry_beats_act(self):
        """前一句在哭，这条 ACT 说 happy → 仍延续哭腔（哭腔延续 > ACT 先验）。"""
        with _env_without(VC.VOICE_ACT_ENV):
            self.assertEqual(self.cap(prefix="（快哭了）我真的有点难受。" + ACT_HAPPY),
                             VC.compose_caption("cry", "weak"))
            self.assertEqual(self.cap(prefix="（大哭）对不起……" + ACT_HAPPY_STRONG),
                             VC.compose_caption("cry", "normal"))
            # 她换了语气（笑）→ 不延续；此时才轮到 ACT（sad），不是哭
            self.assertEqual(self.cap(prefix="（大哭）对不起……哈哈，逗你的。" + ACT_SAD),
                             VC.compose_caption("sad"))
            # ACT 挂在最后也不会把「换语气」那句挡住（回看前先把 ACT 剥掉）
            self.assertEqual(self.cap(prefix="（大哭）对不起……你小声一点。" + ACT_SAD),
                             VC.compose_caption("sad"))

    def test_last_act_wins(self):
        with _env_without(VC.VOICE_ACT_ENV):
            self.assertEqual(self.cap(prefix=ACT_ANGRY + ACT_SAD), VC.compose_caption("sad"))
            self.assertEqual(self.cap(prefix=ACT_SAD + ACT_ANGRY), VC.compose_caption("ang"))
            # prefix 里的是旧 ACT，中文原文里的是这条新的 → 新的算数
            self.assertEqual(self.cap(prefix=ACT_SAD, zh=ACT_HAPPY + "嗯。"),
                             VC.compose_caption("gen"))

    def test_broken_json_is_no_prior(self):
        with _env_without(VC.VOICE_ACT_ENV):
            self.assertEqual(self.cap(prefix=ACT_BROKEN), VC.compose_caption("neutral"))
            # 最后一条坏了 → 当没有（不退回更早那条）
            self.assertEqual(self.cap(prefix=ACT_SAD + ACT_BROKEN), VC.compose_caption("neutral"))

    def test_switch_off_is_old_behavior(self):
        with mock.patch.dict(os.environ, {VC.VOICE_ACT_ENV: "0"}, clear=False):
            self.assertEqual(self.cap(prefix=ACT_SAD), VC.compose_caption("neutral"))
            self.assertEqual(self.cap(prefix=ACT_SAD, zh=ACT_SAD + "嗯。"),
                             VC.compose_caption("neutral"))
            self.assertEqual(self.cap(prefix=ACT_SAD + ACT_ANGRY), VC.compose_caption("neutral"))

    def test_act_never_produces_sob_or_cry_and_no_emoji(self):
        for name in ("happy", "surprised", "sad", "angry", "awkward", "shy"):
            scene = VC.act_scene_of('<|ACT:{"emotion":"%s"}|>' % name)
            self.assertNotIn(scene, (None, "sob", "cry"), name)
        self.assertEqual(VC.pick_emoji(ACT_SAD + "我先走了。", "", "ごめんなさい。"), "")


class TestBridgeActPrior(unittest.TestCase):
    """桥接线：`source_prefix` / 中文原文里的 ACT 真的会变成 `voice_caption`。"""

    def prepare_with(self, raw, prefix, emo=None):
        env = dict(ON, **{VC.VOICE_ACT_ENV: "1"})
        with mock.patch.dict(os.environ, env, clear=False):
            service, _ = svc(ja="そう、ですか。",
                             emo=emo if emo is not None else {"arousal": 0.4, "intensity": 0.4,
                                                              "valence": 0.0})
            return prepare(service, req(speech=raw, raw=raw, prefix=prefix))

    def test_prefix_act_drives_caption(self):
        seg = self.prepare_with("嗯。", ACT_SAD).ja_segments[0]
        self.assertEqual(seg["voice_caption"], VC.compose_caption("sad"))
        self.assertNotIn("😭", seg["text"])          # ACT 先验不加 emoji

    def test_act_in_raw_text_drives_caption(self):
        """第一个单元：ACT 在 unit.start=0 的原文里，不在 prefix 里。"""
        seg = self.prepare_with(ACT_SAD + "嗯。", "").ja_segments[0]
        self.assertEqual(seg["voice_caption"], VC.compose_caption("sad"))

    def test_switch_off_in_bridge(self):
        env = dict(ON, **{VC.VOICE_ACT_ENV: "0"})
        with mock.patch.dict(os.environ, env, clear=False):
            service, _ = svc(ja="そう、ですか。",
                             emo={"arousal": 0.4, "intensity": 0.4, "valence": 0.0})
            res = prepare(service, req(speech="嗯。", raw="嗯。", prefix=ACT_SAD))
        self.assertEqual(res.ja_segments[0]["voice_caption"], VC.compose_caption("neutral"))


class TestActControlBlockInStream(unittest.TestCase):
    """9885：ACT 块进了会话原文，也不进 speech_text / 字幕，不改切句（任务 #23）。"""

    TEXTS = {
        "句首 ACT": (ACT_SAD + "指挥官，今天也辛苦了。要不要先休息一下？", "指挥官"),
        "句中 ACT": ("哎？" + ACT_HAPPY + "真的吗！太好了，我还以为你不会来了呢，所以一直一个人在这里发呆。",
                 "真的吗"),
        "嵌套 JSON": ("（小声）其实我一直在等你回来……" + ACT_HAPPY_STRONG + "那个，不要误会！", "不要误会"),
    }

    @staticmethod
    def feed(text):
        st = ST.SessionText("act")
        return st, st.feed(text, 0.0) + st.finish(1.0)

    def test_control_block_is_not_spoken(self):
        for name, (text, must) in self.TEXTS.items():
            st, units = self.feed(text)
            spoken = "".join(u.speech_text for u in units)
            self.assertNotIn("<|", spoken, name)
            self.assertNotIn("ACT", spoken, name)
            self.assertIn(must, spoken, name)
            self.assertEqual(units[-1].end, len(text), name)     # 原文覆盖到最后一个字
            self.assertFalse(st.unclosed_aside, name)

    def test_display_text_has_no_marker(self):
        """`display.span` / `sentence.start` 的文本 = 原文去掉 control 块（心声/标点一字不少）。"""
        for name, (text, _must) in self.TEXTS.items():
            st, units = self.feed(text)
            sess = types.SimpleNamespace(text=st)
            shown = "".join(SS.Coordinator.source_of(sess, u) for u in units)
            self.assertNotIn("<|", shown, name)
            self.assertEqual(shown, VC._strip_act(text), name)
            self.assertEqual(shown[-1], text[-1], name)          # 句末标点/感叹号原样留着

    def test_chunking_unchanged_by_marker(self):
        """把 ACT 块删掉：切出的可说单元序列（speech_text/kind/reason）必须逐项一样。"""
        for name, (text, _must) in self.TEXTS.items():
            _, with_act = self.feed(text)
            _, without = self.feed(VC._strip_act(text))
            self.assertEqual([(u.speech_text, u.kind, u.reason) for u in with_act],
                             [(u.speech_text, u.kind, u.reason) for u in without], name)


class TestActCuePrefix(unittest.TestCase):
    """ACT JSON 里的括号/引号/花括号不许带偏跨句指令的重放（`active_directive(source_prefix)`）。"""

    def test_directive_survives_act_json(self):
        self.assertEqual(CS.active_directive(ACT_SAD + "接下来都小声说。"), "soft")
        self.assertEqual(CS.active_directive("接下来都小声说。" + ACT_HAPPY_STRONG), "soft")
        self.assertEqual(CS.active_directive(ACT_HAPPY_STRONG + "接下来都耳语说。"), "whisper")
        self.assertIsNone(CS.active_directive("接下来都小声说。" + ACT_SAD + "可以正常说话了。"))
        self.assertIsNone(CS.active_directive(ACT_SAD + "今天好累。"))


try:
    import httpx
except ImportError:              # 生产 venv 有 httpx；缺了就把转发测试标 skip，别让整套挂掉
    httpx = None
    TestGatewayForwarding = unittest.skip("没有 httpx")(TestGatewayForwarding)


if __name__ == "__main__":
    unittest.main(verbosity=2)
