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
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tts", "irodori"))
sys.path.insert(0, os.path.join(ROOT, "tts", "stream"))

import bridge_v2 as B2        # noqa: E402
import emotion_codebook as CB  # noqa: E402
import render_queue as RQ     # noqa: E402
import scene_plan as SP       # noqa: E402
import stream_server as SS    # noqa: E402
import voice_caption as VC   # noqa: E402

ON = {VC.VOICE_CAPTION_ENV: "1", VC.VOICE_EMOJI_ENV: "1"}
OFF = {VC.VOICE_CAPTION_ENV: "0", VC.VOICE_EMOJI_ENV: "0"}


def req(unit_id="u0", speech="今天有点累了", raw=None, session="s1", gen=1):
    return B2.parse_request({
        "session_id": session, "generation": gen, "unit_id": unit_id,
        "voice": "albion", "source_range": [0, len(speech)],
        "speech_text": speech, "raw_text": raw if raw is not None else speech,
    })


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


try:
    import httpx
except ImportError:              # 生产 venv 有 httpx；缺了就把转发测试标 skip，别让整套挂掉
    httpx = None
    TestGatewayForwarding = unittest.skip("没有 httpx")(TestGatewayForwarding)


if __name__ == "__main__":
    unittest.main(verbosity=2)
