# -*- coding: utf-8 -*-
"""受控声音描述（2026-10-26，阶段 3d）：只用声音动作词按句拼 caption，取代 7 条固定锚。

为什么要这套词表
----------------
生产情绪一直是「翻译桥顺手判的几个标量 → `emotion_codebook` 的 7 条训练锚」，整条回复一个族，
neutral 占 83%，而 7 条锚彼此差异很小（`tests/output/tts-emotion-survey-20261026/SURVEY.md`）。
自由描述实验（`tests/output/tts-emotion-free-20261026/REPORT.md`、`tests/output/tts-emotion-scene-20261026/REPORT.md`）
证明现有 LoRA **只对声音动作词有反应**：早口 / 囁く / 息まじり / 声を震わせて / 低い声 /
優しく / ゆっくり / 語尾を下げて / 明るく弾んだ 这类能推时长和音高；抽象情绪词
（恥ずかしそう / 甘えた / 得意げ）在噪声里；**cfg 不是强度旋钮**（2/4/6 时长一模一样）。

所以这里的描述：
  * 一个场景只决定「用哪两个动作词 + 哪个句末动词」，**不写情绪形容词**；
  * 强弱只用「少し / とても」前缀，和评测台的结论一致（强度靠措辞，不靠 cfg）；
  * 同一场景 + 同一强度 → **同一个字符串**（相邻句不会因为标点/字数小差别换来换去，
    只有族或强度真的变了才换描述 —— 这就是阶段 3d 要的「别跳太猛」，靠纯函数而不是跨句状态）；
  * 8 个场景对上场景评测集 `tests/tts/data/emotion_scene_20261026.json` 的 8 类情绪。

校验口径（`validate_caption`，桥与规划器共用同一把尺子）
------------------------------------------------------
非空、10–40 字、每个「、」段只能是 `MANNER` 里的动作词（可带 `PREFIXES` 前缀）、
末段只能是 `FINAL_VERBS` 里的动词。出现词表外的实词 / 超长 / 空 → 整条丢掉，
调用方回到原来的码本锚路径（**绝不把半截描述送进模型**）。

开关（默认全关，关时生产行为逐位不变）
------------------------------------
  * `ALBION_VOICE_CAPTION=1`：桥写描述、规划器把它当 caption（`caption_mode="free"`）发出去；
  * `ALBION_VOICE_EMOJI=1`：台词本身在笑/叹气/打哈欠/哭时，句末加**一个**白名单非语言 emoji，
    每条回复最多 1 个（`EmojiBudget`），白名单外的 emoji 一律剥掉。
"""  # noqa: E501 - 说明性长行

import os
import re

# ---- 开关 ---------------------------------------------------------------------
VOICE_CAPTION_ENV = "ALBION_VOICE_CAPTION"
VOICE_EMOJI_ENV = "ALBION_VOICE_EMOJI"

# 自由描述单元的 cfg。评测台（阶段 2b）测过：cfg 2/3/4/6 对同一描述时长完全相同，
# 不是强度旋钮，所以这里固定 3.0（= 原 neutral_quiet 的 cfg），只让 caption 一个变量动。
FREE_CFG = 3.0

CAPTION_MIN, CAPTION_MAX = 10, 40
# 后端 `render_queue.FREE_CAPTION_MAX` 是 120 字；这里更严，给未来加词留余量。

_UNSET = object()


def _switch(env_name, default=False):
    """环境变量开关：1/true/yes/on 开，0/false/no/off 关，其余用默认值。"""
    v = os.environ.get(env_name)
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def voice_caption_on():
    """描述开关（每次现读环境：部署时不用改代码，也方便测试里临时打开）。"""
    return _switch(VOICE_CAPTION_ENV)


def voice_emoji_on():
    """句末非语言 emoji 开关。"""
    return _switch(VOICE_EMOJI_ENV)


# ---- 词表 ---------------------------------------------------------------------
# 每个场景两个动作词：一个定「音色/音量」，一个定「节奏/语尾」。
# 词全部来自阶段 2b/3c 实测**有反应**的声音动作词，或场景评测集里有效的描述
# （tests/tts/data/emotion_scene_20261026.json 的 free 条件）。
MANNER = {
    "neutral": ("落ち着いた声で", "淡々と"),
    "sad": ("低い声で", "語尾を下げて"),
    "cry": ("声を震わせて", "途切れがちに"),
    "whi": ("声を潜めて", "囁くように"),
    "shy": ("小さな声で", "語尾を消え入るように"),
    "coq": ("鼻にかけて", "柔らかく"),
    "exc": ("明るく弾んだ声で", "早口に"),
    "ang": ("低い声で", "強く"),
    "gen": ("優しく", "ゆっくりと"),
}

# 句末动词：让描述读起来像一句人话，同时给模型一个「说话方式」的落点。
FINAL_VERBS = {
    "neutral": "話す。",
    "sad": "呟く。",
    "cry": "言う。",
    "whi": "囁く。",
    "shy": "話す。",
    "coq": "甘える。",       # 只在这里出现（「甘える」实测没反应，但句末动词影响很小）
    "exc": "話す。",
    "ang": "言う。",
    "gen": "語りかける。",
}

# 强度前缀：评测台的结论是强度只能靠措辞（cfg 不是旋钮），所以只给这三档。
PREFIXES = {
    "weak": "少し",
    "normal": "",
    "strong": "とても",
}

SCENES = tuple(MANNER)
LEVELS = tuple(PREFIXES)

# 词表全集（校验用）：动作词 + 动词，任何不在集合里的「实词」都会让整条描述作废。
MANNER_BASES = frozenset(w for pair in MANNER.values() for w in pair)
FINAL_SET = frozenset(FINAL_VERBS.values())
PREFIX_SET = frozenset(p for p in PREFIXES.values() if p)

# 场景 → 桥的码本族（规划器还要按族做校验/渲染；描述本身不换族，只换 caption）。
SCENE_FAMILY = {
    "neutral": "neutral", "sad": "sad", "cry": "sad", "whi": "flirty",
    "shy": "neutral", "coq": "intimate", "exc": "bright", "ang": "bright",
    "gen": "flirty",
}


def compose_caption(scene, level="normal"):
    """拼一条描述。`scene` 必须是 `SCENES` 之一，`level` 是 `LEVELS` 之一。

    纯函数：同一 (scene, level) 永远同一字符串 —— 相邻句只要情绪没变，描述就不会跳。
    """
    if scene not in MANNER:
        raise ValueError("未知场景：%r（合法：%s）" % (scene, ",".join(SCENES)))
    if level not in PREFIXES:
        raise ValueError("未知强度：%r（合法：%s）" % (level, ",".join(LEVELS)))
    first, second = MANNER[scene]
    caption = "%s%s、%s、%s" % (PREFIXES[level], first, second, FINAL_VERBS[scene])
    why = validate_caption(caption)
    if why:                                   # 词表自己拼坏了：宁可没有描述，也不送半句
        raise AssertionError("词表拼出的描述没通过自校验（%s）：%s" % (why, caption))
    return caption


def _split_chunk(chunk):
    """一个「、」段 → (前缀, 实词)；带前缀时前缀必须在词表里。"""
    for p in PREFIX_SET:
        if chunk.startswith(p) and chunk[len(p):] in MANNER_BASES:
            return p, chunk[len(p):]
    if chunk in MANNER_BASES:
        return "", chunk
    return None, None


def validate_caption(caption):
    """描述能不能用：能 → None；不能 → 原因（调用方据此丢掉描述、回锚路径）。"""
    if not isinstance(caption, str):
        return "不是字符串"
    text = caption.strip()
    if not text:
        return "空描述"
    if len(text) > CAPTION_MAX:
        return "过长（%d 字 > %d）" % (len(text), CAPTION_MAX)
    if len(text) < CAPTION_MIN:
        return "过短（%d 字 < %d）" % (len(text), CAPTION_MIN)
    if any(_control_cat(ch) in ("Cc", "Cf", "Cs") for ch in text):
        return "含控制字符"
    if "\n" in text or "\r" in text or "\t" in text:
        return "含换行/制表符"
    parts = text.split("、")
    if len(parts) != 3:
        return "结构不对（要 A、B、动词 三段，实际 %d 段）" % len(parts)
    for part in parts[:-1]:
        prefix, base = _split_chunk(part)
        if base is None:
            return "词表外实词：%r" % part
    if parts[-1] not in FINAL_SET:
        return "句末动词不在词表：%r" % parts[-1]
    return None


def _control_cat(ch):
    """轻量代理：不 import unicodedata 也能判控制字符（零宽/代理/换行都算）。"""
    o = ord(ch)
    if o < 0x20 or o == 0x7F:
        return "Cc"
    if 0x200B <= o <= 0x200F or 0x202A <= o <= 0x202E or 0x2060 <= o <= 0x206F:
        return "Cf"
    if 0xD800 <= o <= 0xDFFF:
        return "Cs"
    return ""


# ---- 桥侧：从情绪提示挑场景/强度 ----------------------------------------------

# `bridge.zh_source_cues` 的 kind → 场景。只认**非否定**的明示线索。
_CUE_SCENE = {
    "whisper": "whi",
    "breath": "coq",
    "shy": "shy",
    "excite": "exc",
    "sad": "sad",
    "soft": "shy",       # 「（小声）」= 音量小，落在 shy 的「小さな声で」
}

# hint 自述的发声方式 → 场景（只影响描述，不换族；族仍由规划器决定）。
_PHONATION_SCENE = {"whispered": "whi", "breathy": "coq", "soft": "shy"}

# 日语译文里能直接读到的表面线索（没有 hint 时的兜底；不做语义解读）。
_JA_SCENE = (
    ("cry", re.compile(r"泣|嗚咽|涙|震え|ぐす|しくしく")),
    ("whi", re.compile(r"囁|ひそひそ|耳元|ささや")),
    ("ang", re.compile(r"怒|ふざけ|許さ|失礼")),
    ("exc", re.compile(r"笑|くすくす|ふふ|やった|嬉し")),
)


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def scene_of(cues, ja_text="", fallback="neutral"):
    """情绪提示 → 场景。优先级：明示线索 > 发声方式 > 标量 > 日语表面线索 > 默认。

    只做「读得出什么就用什么」，猜不出就是 `fallback`（默认 neutral：和现在 83% neutral
    的生产分布一致，不会凭空给一句台词加戏）。
    """
    c = cues if isinstance(cues, dict) else {}
    kinds = set()
    for item in c.get("src") or []:
        if isinstance(item, dict) and item.get("kind") and not item.get("negated"):
            kinds.add(str(item["kind"]))
    for kind in ("whisper", "breath", "shy", "excite", "sad", "soft"):
        if kind in kinds:
            return _CUE_SCENE[kind]
    ph = c.get("phonation")
    if isinstance(ph, str) and ph in _PHONATION_SCENE:
        return _PHONATION_SCENE[ph]
    ar, va, it = _num(c.get("arousal")), _num(c.get("valence")), _num(c.get("intensity"))
    if va is not None:
        if va <= -0.5 and (ar is None or ar <= 0.45):
            return "cry"                      # 低落 + 不高唤醒：哽咽（快哭）
        if va <= -0.35 and ar is not None and ar >= 0.6:
            return "ang"                      # 负面 + 高唤醒：生气（码本没有这个锚，描述里有）
        if va <= -0.3:
            return "sad"
        if va >= 0.35 and (ar is None or ar >= 0.55):
            return "exc"
        if va >= 0.2:
            return "gen"
    if it is not None and it >= 0.75:
        return "exc"                          # 只有强度没有正负：按「感情を込めて」处理
    if no_scalar_cues(c):
        for scene, rx in _JA_SCENE:
            if ja_text and rx.search(ja_text):
                return scene
    return fallback


def no_scalar_cues(cues):
    """提示里一个标量都没有（只有 src 也没关系）—— 才允许退回日语表面线索。"""
    c = cues if isinstance(cues, dict) else {}
    return all(_num(c.get(k)) is None for k in ("arousal", "intensity", "valence"))


def level_of(cues):
    """强度分档：weak / normal / strong。取 intensity，其次 arousal（评测台口径）。"""
    c = cues if isinstance(cues, dict) else {}
    v = _num(c.get("intensity"))
    if v is None:
        v = _num(c.get("arousal"))
    if v is None:
        return "normal"
    if v < 0.35:
        return "weak"
    if v >= 0.72:
        return "strong"
    return "normal"


def caption_for(cues, ja_text="", fallback="neutral"):
    """桥的入口：情绪提示 + 日语译文 → 一条通过校验的描述（没把握就返回 ""）。"""
    scene = scene_of(cues, ja_text, fallback=fallback)
    caption = compose_caption(scene, level_of(cues))
    if validate_caption(caption):
        return ""                             # 理论上不会发生；真发生了也不送半句
    return caption


# ---- 句末非语言 emoji ---------------------------------------------------------
# 只收 `checkpoints/EMOJI_ANNOTATIONS.md` 里**非语言**的那几个（笑声/叹气/哈欠），
# 风格类（😊😠🥺…）会改整句语气，不在本阶段范围内。
# 不收 😭：用户要「快哭」不要「大哭」，而 😭 放句末会多出一段抽泣声，属于大哭。
EMOJI_WHITELIST = ("🤭", "😮‍💨", "🥱")

_EMOJI_RULES = (
    ("🤭", re.compile(r"笑|哈哈|嘿|嘻|呵呵|くす|ふふ|ふふっ")),
    ("😮‍💨", re.compile(r"唉|叹|叹气|呼|ため息|ふう|はあ|ほっと")),
    ("🥱", re.compile(r"哈欠|困|睡|疲|あくび|眠|ふわ")),
)

# emoji 的码位范围（判"是不是 emoji"用；白名单外的整颗剥掉，包括变体选择符/ZWJ）。
_EMOJI_CHAR = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]"
)


def is_emoji(ch):
    return bool(_EMOJI_CHAR.match(ch))


def strip_emoji(text):
    """剥掉**所有** emoji（含白名单里的）。返回 (干净文本, 剥掉几个)。

    为什么白名单里的也剥：额度是「每条回复最多 1 个」，模型自己写的 emoji 位置/个数不受控，
    留着就可能一条回复蹦出三四个。要加的那一个由 `pick_emoji` 决定。
    """
    if not isinstance(text, str) or not text:
        return text or "", 0
    out, n = [], 0
    for ch in text:
        if is_emoji(ch):
            n += 1
            continue
        out.append(ch)
    return "".join(out), n


def pick_emoji(*texts):
    """台词本身在笑/叹气/打哈欠/哭 → 对应 emoji；否则 ""。按 `_EMOJI_RULES` 的优先级。"""
    blob = " ".join(t for t in texts if isinstance(t, str) and t)
    if not blob:
        return ""
    for emoji, rx in _EMOJI_RULES:
        if rx.search(blob):
            return emoji
    return ""


class EmojiBudget:
    """每条回复（session + generation）最多 1 个 emoji。纯内存、有界，不落盘。

    不 undo：一个回复里第一句先抢到的那颗就是唯一的；桥重新翻译同一单元时不会重复加
    （同一个 key 再来就是 False）。
    """

    def __init__(self, max_keys=64):
        self.max_keys = max(1, int(max_keys))
        self._used = {}

    def used(self, key):
        return key in self._used

    def take(self, key):
        if key in self._used:
            return False
        self._used[key] = True
        while len(self._used) > self.max_keys:      # dict 保插入序：先丢最老的
            self._used.pop(next(iter(self._used)))
        return True

    def __len__(self):
        return len(self._used)


# ---- 自检 ---------------------------------------------------------------------
def _selftest():
    for scene in SCENES:
        for level in LEVELS:
            cap = compose_caption(scene, level)
            assert CAPTION_MIN <= len(cap) <= CAPTION_MAX, (scene, level, cap, len(cap))
            assert validate_caption(cap) is None, (scene, level, cap)
    assert validate_caption("") == "空描述"
    assert validate_caption("少し低い声で、語尾を下げて、呟く。") is None
    assert validate_caption("恥ずかしそうに、声を潜めて、話す。") is not None      # 抽象情绪词
    assert validate_caption("低い声で、語尾を下げて。") is not None                # 缺动词
    assert validate_caption("低い声で、語尾を下げて、呟く。") is None
    # 相邻句同场景同强度 → 同一个描述（不跳）
    assert compose_caption("sad", "normal") == compose_caption("sad", "normal")
    assert compose_caption("sad", "weak") != compose_caption("sad", "strong")
    # emoji：白名单外的剥掉
    assert strip_emoji("もう大丈夫😊です🙂") == ("もう大丈夫です", 2)
    assert pick_emoji("ふふ、そうなんだ") == "🤭"
    assert pick_emoji("はあ、疲れた") == "😮‍💨"
    assert pick_emoji("眠い……") == "🥱"
    assert pick_emoji("うう、泣きそう") == ""      # 😭 已下线：只要快哭，不要句末抽泣
    assert pick_emoji("はい、わかりました。") == ""
    b = EmojiBudget(max_keys=2)
    assert b.take(("s", 1)) is True and b.take(("s", 1)) is False
    assert b.take(("s", 2)) is True and b.take(("s", 3)) is True
    assert len(b) == 2                              # 有界：最老的被挤掉
    print("voice_caption 自检通过：%d 场景 × %d 档 = %d 条描述"
          % (len(SCENES), len(LEVELS), len(SCENES) * len(LEVELS)))


if __name__ == "__main__":
    _selftest()
