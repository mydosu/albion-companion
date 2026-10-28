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
  * 9 个场景对上场景评测集 `tests/tts/data/emotion_scene_20261026.json` 的 8 类情绪，
    外加 2026-10-26 用户要求拆出来的**真哭** `sob`（快哭/委屈仍走 `cry`，描述不变）。

快哭（`cry`）与真哭（`sob`）怎么分
---------------------------------
用户口径：「快哭用快哭（和委屈差不多），真哭用真哭」。
  * `cry`：normal/strong 两档用**旧措辞** `今にも泣き出しそうに、声を震わせて話す。`
    （2026-10-27 用户盲听 #20：旧措辞更像哭）；weak 仍用三段模板
    `少し声を震わせて、途切れがちに、言う。`（哭腔延续句就是 weak）。判据不变：除了标量
    （valence<=-0.5 且 arousal<=0.45），中文原文里出现
    委屈 / 想哭 / 快哭 / 要哭 / 哽咽 / 哭腔 / 眼眶红 / 含泪 / 泪汪汪 这类也走 `cry`
    （`cry_cue`）；**不认单独的「哭 / 眼泪 / 眼眶」**（「笑出眼泪」「看电影哭了」不抢 cry），
    「呜呜」单独出现只是快哭，不算真哭。
  * `sob`：**只认明示线索**，宁漏勿错 —— 中文原文括号里的**无修饰**哭泣动作（（哭）（大哭）
    （哭出声）（抽泣）（啜泣）（哭着说））、明确哭声的拟声 `呜哇 / 呜呜哇 / 哇哇大哭`、
    日语译文 `しくしく / うわーん / えーん / 泣きじゃくり / 嗚咽`；否定（「我才没哭」
    「泣いてなんか、いません」）一律排除。标量路径永远不产出 `sob`。
  * 括号里带「快 / 要 / 想 / 差点」或「强忍 / 忍着 / 忍住」的是**快哭**，不是真哭
    （（快哭了）（要哭了）（想哭）（差点哭出来）（强忍着不哭）→ `cry`）。
  * 两者都**优先于 `phonation`**：生产实测「呜呜……我、我好像要哭了」被情感抽取标成
    `phonation=breathy`，旧顺序会拿到「鼻にかけて、柔らかく、甘える。」（coq）；
    哭泣的明示证据应当压过发声方式（2026-10-26 修）。
  * 😭 只在真哭时回句末（`pick_emoji(..., sob=True)`）；快哭/委屈不加任何 emoji。

校验口径（`validate_caption`，桥与规划器共用同一把尺子）
------------------------------------------------------
非空、10–40 字、每个「、」段只能是 `MANNER` 里的动作词（可带 `PREFIXES` 前缀）、
末段只能是 `FINAL_VERBS` 里的动词。出现词表外的实词 / 超长 / 空 → 整条丢掉，
调用方回到原来的码本锚路径（**绝不把半截描述送进模型**）。`FIXED_CAPTIONS` 里定死的
描述（真哭一条、快哭 normal/strong 的旧措辞）在基本检查后**整句等于原文**即放行。

开关（默认全关，关时生产行为逐位不变）
------------------------------------
  * `ALBION_VOICE_CAPTION=1`：桥写描述、规划器把它当 caption（`caption_mode="free"`）发出去；
  * `ALBION_VOICE_EMOJI=1`：台词本身在笑/叹气/打哈欠/哭时，句末加**一个**白名单非语言 emoji，
    每条回复最多 1 个（`EmojiBudget`），白名单外的 emoji 一律剥掉；
  * `ALBION_VOICE_CARRY`（**默认开**）：中文原文前一句还在哭时，本句的哭腔可以延续下去
    （见下节）；`=0` 关掉后与没有这个开关时逐字节一致。

哭腔延续（`ALBION_VOICE_CARRY`，默认开）
----------------------------------------
一条回复被切成多句时，哭的明示线索往往只在**前一句**（「呜呜……我好像要哭了。对不起，指挥官……」
第二句自己没有线索，会落到 shy/neutral，听起来像突然不哭了）。所以桥把本单元之前的中文原文
（`req.source_prefix`）也交给 `caption_for`，在**同一段落**里回看最后 2 句：
  * 当前句自己判出 sob/cry，或 exc/whi/ang，或提示里有非否定的 whisper/excite/shy 线索
    → **不延续**（她自己换语气了 / 本句自己有更强的证据）；
  * 最后 2 句里更靠近当前句的那一句带反向线索（（笑）「哈哈」「小声」「生气」…）→ **断开**；
  * 最后 2 句里有真哭（`sob_of`）→ 本句 `cry` / level normal；只有快哭
    （`paren_cry=="cry"` 或 `cry_cue`）→ 本句 `cry` / level weak。
  * 延续**永远不产出 sob、不加 😭**：😭 只给带明示真哭线索的那一句。

ACT 情绪先验（`ALBION_VOICE_ACT`，默认开）
------------------------------------------
脑的回复开头带 `<|ACT:{"emotion":"sad"}|>`，壳从 2026-10-27 起把它原样送进 9885 的文字流
（服务端只当 `control` 块：不进 speech_text、不出字幕），于是桥能在 `source_prefix` / 中文原文里
看到它。本句**自己**判不出情绪时，用最后一条 ACT 的 emotion 当场景：

  * 优先级：本句自己的证据 > 哭腔延续 > ACT 先验 > neutral；
  * **弱判断可以让 ACT 顶掉**（2026-10-27 任务 #24）：本句只说得出 gen/shy/coq，而且这点
    判断只来自 phonation（soft→shy、breathy→coq）或标量（va≥0.2→gen）时，用 ACT 的场景。
    src 明示线索（whisper/breath/shy/excite/sad/soft）和日语表面线索不算弱；whispered→whi、
    exc/ang/sad/cry/sob 也不算弱（「太好了！」+ACT sad 仍是 exc）。哭腔延续仍用本句自己的
    判断来判，所以延续 > ACT 不变；
  * 映射：happy→`gen`（intensity≥0.7 → `exc`）、surprised→`exc`、sad→`sad`、angry→`ang`、
    awkward/shy→`shy`；neutral/think/question/curious 和没见过的名字**不给先验**；
  * 强度仍走 `level_of(cues)`；ACT 先验**不产出 sob/cry、不加 emoji**；
  * `ALBION_VOICE_ACT=0` 关掉后与加它之前逐字节一致。
"""  # noqa: E501 - 说明性长行

import json
import os
import re

# ---- 开关 ---------------------------------------------------------------------
VOICE_CAPTION_ENV = "ALBION_VOICE_CAPTION"
VOICE_EMOJI_ENV = "ALBION_VOICE_EMOJI"
VOICE_CARRY_ENV = "ALBION_VOICE_CARRY"
VOICE_ACT_ENV = "ALBION_VOICE_ACT"

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


def voice_carry_on():
    """哭腔延续开关（**默认开**；`ALBION_VOICE_CARRY=0` 关掉后行为与加它之前逐字节一致）。"""
    return _switch(VOICE_CARRY_ENV, default=True)


def voice_act_on():
    """ACT 情绪先验开关（**默认开**；`ALBION_VOICE_ACT=0` 关掉后与加它之前逐字节一致）。"""
    return _switch(VOICE_ACT_ENV, default=True)


# ---- 词表 ---------------------------------------------------------------------
# 每个场景两个动作词：一个定「音色/音量」，一个定「节奏/语尾」。
# 词全部来自阶段 2b/3c 实测**有反应**的声音动作词，或场景评测集里有效的描述
# （tests/tts/data/emotion_scene_20261026.json 的 free 条件）。
MANNER = {
    "neutral": ("落ち着いた声で", "淡々と"),
    "sad": ("低い声で", "語尾を下げて"),
    "cry": ("声を震わせて", "途切れがちに"),
    "sob": ("声を震わせて", "しゃくり上げて"),   # 真哭：措辞由 tests/output/tts-sob-20261026 实测选
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
    "sob": "言う。",
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
    "neutral": "neutral", "sad": "sad", "cry": "sad", "sob": "sad", "whi": "flirty",
    "shy": "neutral", "coq": "intimate", "exc": "bright", "ang": "bright",
    "gen": "flirty",
}


# 少数场景的措辞**定死一条**，不再套「少し/とても」前缀。
# 真哭（sob）是唯一一个不分档的：3 句真哭台词 × 2 seed 实测（tests/output/tts-sob-20261026/REPORT.md），
# 只有带「とても」的这一条让三句的 f0_std 全部高过基线（+1.3 / +2.1 / +7.9，均值 +3.8）
# 且 ΔF0 +8 Hz；去掉前缀（声を震わせて、しゃくり上げて、言う。）只能 +1.2 / ±0，和快哭分不开。
# 真哭不需要「弱一点的哭」，所以定死。
# 快哭（cry）按**档位**定死（2026-10-27 用户盲听 #20：旧措辞更像哭）：
#   * normal / strong 用旧实验的原句 `今にも泣き出しそうに、声を震わせて話す。`（两段、「、」只有一个）；
#   * weak 保留现措辞（哭腔延续句用的就是 weak，弱一点正合适），仍走三段模板。
# 这条两段描述不在 MANNER/FINAL_VERBS 词表里，靠 `FIXED_CAPTION_TEXTS` 在校验里整句放行。
FIXED_CAPTIONS = {
    "sob": "とても声を震わせて、しゃくり上げて、言う。",
    ("cry", "normal"): "今にも泣き出しそうに、声を震わせて話す。",
    ("cry", "strong"): "今にも泣き出しそうに、声を震わせて話す。",
}

# 定死描述的整句白名单（validate_caption 用）：只认表里的**原字符串**，不做前缀/分词匹配。
FIXED_CAPTION_TEXTS = frozenset(FIXED_CAPTIONS.values())


def compose_caption(scene, level="normal"):
    """拼一条描述。`scene` 必须是 `SCENES` 之一，`level` 是 `LEVELS` 之一。

    纯函数：同一 (scene, level) 永远同一字符串 —— 相邻句只要情绪没变，描述就不会跳。
    `FIXED_CAPTIONS` 可以按场景定死（sob 不分档），也可以按 `(scene, level)` 定死
    （cry 的 normal/strong 用旧措辞，weak 仍走三段模板）。
    """
    if scene not in MANNER:
        raise ValueError("未知场景：%r（合法：%s）" % (scene, ",".join(SCENES)))
    if level not in PREFIXES:
        raise ValueError("未知强度：%r（合法：%s）" % (level, ",".join(LEVELS)))
    caption = FIXED_CAPTIONS.get((scene, level))
    if caption is None:
        caption = FIXED_CAPTIONS.get(scene)
    if caption is None:
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
    if text in FIXED_CAPTION_TEXTS:           # 定死的两段/定档描述：整句等于表里的原文就放行
        return None
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

# ---- 快哭（cry）与真哭（sob）的判据 ------------------------------------------
# 中文原文里的**快哭/委屈**：只认"快要哭"的说法，**不认单独的「哭 / 眼泪 / 眼眶」** ——
# 「笑出眼泪」「哭笑不得」「你哭什么呀，笑一个」「看电影哭了」都不该抢到 cry（2026-10-26 审）。
# 否定（「我才没哭」）不算；「哭笑不得」这类成语显式排除。
_ZH_CRY_CUE = re.compile(r"委屈|想哭|快哭|要哭|哽咽|哭腔|眼眶红|眼圈红|含泪|呜呜|泪汪汪|气哭|气到哭")
_ZH_NOT_CRY = re.compile(r"哭笑不得|笑出眼泪|笑到流泪|笑出泪|破涕为笑")

# 「气哭」一类是**快哭**（被气到眼眶热），不是真哭：「（要被你气哭了）」
# 原来会被括号里的裸「哭」判成 sob（2026-10-27 修）。括号里先按这条落 cry。
_ZH_ANGRY_CRY = re.compile(r"气哭|气到哭")

# 真哭只认**明示**动作：中文括号里的哭泣动作（（哭）（大哭）（哭出声）（抽泣）（啜泣）（哭着说））。
# 括号外的一句「哭」不算 —— 宁漏勿错（「我才没哭」在括号外，本来也进不来）。
_ZH_SOB_PAREN = re.compile(r"大哭|哭出声|哭起来|哭出来|哭了出来|抽泣|啜泣|哭泣|哭着|哭了|哭")

# 括号里**紧挨着**哭泣词前 3 字内带这些修饰的只是快哭，不算真哭（（快哭了）（要哭了）（想哭）
# （差点哭出来）（眼眶红红的，快要哭出来））；「强忍 / 忍着 / 忍住」是憋着没哭出来，同样只算快哭。
# 只看紧邻的 3 字，是为了不把「（哭着说要抱抱）」里的「要」误当修饰。
_ZH_SOB_SOFT = re.compile(r"快|要|想|差点|差一点")
_ZH_SOB_HOLD = re.compile(r"强忍|忍着|忍住")

# 拟声只认**明确的哭声**（呜哇 / 呜呜哇 / 哇哇大哭）；单独的「哇——好漂亮」「哇~~~太棒了」
# 是惊叹，不是哭（2026-10-26 审，去掉了光秃秃的「哇——」）。
_ZH_SOB_ONOMATOPOEIA = re.compile(r"呜哇|呜呜哇|哇哇大哭")

_PAREN_BODY = re.compile(r"[（(]([^）)\n]{1,24})[）)]")

# 中文否定：紧挨着哭泣词前面 1–5 字（「我才没哭」「别哭了」「不会哭」）。
_ZH_NEG_HEAD = re.compile(r"(?:没有|还没|才没|不要|不用|不许|不会|别|没|不|未|勿|莫)\s*$")

# 日语译文的真哭词；紧跟着的否定（「泣きじゃくってない」「しくしくしてない」）才排除 ——
# 只认贴着动词的 `(し)(て)(は)(なんか)(ない/ません)`，所以 `泣きじゃくって、息ができない。`
# 的「できない」、`嗚咽が止まらない。` 的「止まらない」都不算否认在哭。
_JA_SOB = re.compile(r"しくしく|うわーん|うわあん|えーん|ええん|泣きじゃく|嗚咽|むせび泣")
_JA_NEG_TAIL = re.compile(r"^(?:し)?(?:て|って|で)?(?:は|も)?(?:なんか|なんて)?(?:ない|ません|いない|いません)")


def _zh_hit(text, pattern):
    """第一个**非否定**的匹配（否定看词前 1–5 字）；没有就返回 None。"""
    t = text or ""
    for m in pattern.finditer(t):
        if _ZH_NEG_HEAD.search(t[max(0, m.start() - 5):m.start()]):
            continue
        return m
    return None


def paren_cry(text):
    """中文括号里的哭泣动作 → `"sob"` / `"cry"` / None。

    * （哭）（大哭）（哭出声）（抽泣）（啜泣）（哭着说）→ `"sob"`；
    * 带「快 / 要 / 想 / 差点」修饰（（快哭了）（要哭了）（想哭）（差点哭出来）
      （眼眶红红的，快要哭出来））或「强忍 / 忍着 / 忍住」（（强忍着不哭））→ `"cry"`；
    * 否定（（别哭）（不要哭））→ None。
    """
    for m in _PAREN_BODY.finditer(text or ""):
        body = m.group(1)
        if _ZH_NOT_CRY.search(body):               # 「（哭笑不得）」这种成语不是哭
            continue
        angry = _ZH_ANGRY_CRY.search(body)
        if angry is not None:                      # 「气哭」一类：快哭（否定则「没在哭」）
            if _ZH_NEG_HEAD.search(body[max(0, angry.start() - 5):angry.start()]):
                continue
            return "cry"                           # （要被你气哭了）= 快哭，不是真哭
        if _ZH_SOB_HOLD.search(body):              # 憋着没哭出来 = 快哭
            return "cry"
        hit = _zh_hit(body, _ZH_SOB_PAREN)
        if hit is None:                            # 没有（非否定的）哭泣动作
            continue
        if _ZH_SOB_SOFT.search(body[max(0, hit.start() - 3):hit.start()]):
            return "cry"                           # 快哭了 / 要哭 / 想哭 / 差点哭出来 = 快哭
        return "sob"
    return None


def _ja_sob_hit(text):
    t = text or ""
    for m in _JA_SOB.finditer(t):
        if _JA_NEG_TAIL.match(t[m.end():m.end() + 10]):
            continue
        return m
    return None


def sob_of(zh_text="", ja_text=""):
    """**真哭**判定（桥的 caption 与句末 emoji 共用这一个函数）。只认明示线索，宁漏勿错。

    触发：中文括号里的**无修饰**哭泣动作 / 拟声「呜哇、呜呜哇、哇哇大哭」/ 日语
    `しくしく・うわーん・えーん・泣きじゃくり・嗚咽`（非否定）。**标量永远不产出真哭**；
    「呜呜」单独出现、「要哭了 / 快哭 / 想哭」、括号里带「快/要/想/差点/忍住」的都只算快哭
    （见 `cry_cue` / `paren_cry`）。
    """
    if paren_cry(zh_text) == "sob" or _ZH_SOB_ONOMATOPOEIA.search(zh_text or ""):
        return True
    return bool(_ja_sob_hit(ja_text))


def cry_cue(zh_text=""):
    """**快哭/委屈**的中文判据（委屈、想哭、快哭、哽咽、眼眶红、「呜呜」…）；否定/成语不算。"""
    t = _ZH_NOT_CRY.sub("", zh_text or "")         # 「哭笑不得」这类不是哭
    return bool(_zh_hit(t, _ZH_CRY_CUE))


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def _scene_basis_of(cues, ja_text="", zh_text="", fallback="neutral"):
    """情绪提示 → `(场景, 依据)`。依据：`cry`/`cue`/`phonation`/`scalar`/`ja`/`fallback`。

    `scene_of` 只是它的薄壳（对外 API 不变）。**依据是给 ACT 先验用的**：只有
    phonation（soft→shy、breathy→coq）和标量（va≥0.2→gen）判出来的场景算「弱判断」，
    日语表面线索与 src 明示线索都不算 —— 判据在 `_WEAK_BASIS` / `_WEAK_SCENES`。
    """
    c = cues if isinstance(cues, dict) else {}
    if sob_of(zh_text, ja_text):
        return "sob", "cry"
    if paren_cry(zh_text) == "cry" or cry_cue(zh_text):
        return "cry", "cry"                   # （快哭了）（强忍着不哭）/ 委屈类说法
    kinds = set()
    for item in c.get("src") or []:
        if isinstance(item, dict) and item.get("kind") and not item.get("negated"):
            kinds.add(str(item["kind"]))
    for kind in ("whisper", "breath", "shy", "excite", "sad", "soft"):
        if kind in kinds:
            return _CUE_SCENE[kind], "cue"
    ph = c.get("phonation")
    if isinstance(ph, str) and ph in _PHONATION_SCENE:
        return _PHONATION_SCENE[ph], "phonation"
    ar, va, it = _num(c.get("arousal")), _num(c.get("valence")), _num(c.get("intensity"))
    if va is not None:
        if va <= -0.5 and (ar is None or ar <= 0.45):
            return "cry", "scalar"            # 低落 + 不高唤醒：哽咽（快哭）
        if va <= -0.35 and ar is not None and ar >= 0.6:
            return "ang", "scalar"            # 负面 + 高唤醒：生气（码本没有这个锚，描述里有）
        if va <= -0.3:
            return "sad", "scalar"
        if va >= 0.35 and (ar is None or ar >= 0.55):
            return "exc", "scalar"
        if va >= 0.2:
            return "gen", "scalar"
    if it is not None and it >= 0.75:
        return "exc", "scalar"                # 只有强度没有正负：按「感情を込めて」处理
    if no_scalar_cues(c):
        for scene, rx in _JA_SCENE:
            if ja_text and rx.search(ja_text):
                return scene, "ja"
    return fallback, "fallback"


def scene_of(cues, ja_text="", zh_text="", fallback="neutral"):
    """情绪提示 → 场景。优先级：真哭 > 快哭 > 明示线索 > 发声方式 > 标量 > 日语表面线索 > 默认。

    哭泣的明示证据（`sob_of` / `cry_cue`）**先于 phonation**：生产实测「呜呜……我、我好像
    要哭了。对不起，指挥官……」被情感抽取标成 `phonation=breathy`，旧顺序拿到了 coq 的
    「鼻にかけて、柔らかく、甘える。」；哭泣是比发声方式更强的证据（2026-10-26 修）。

    只做「读得出什么就用什么」，猜不出就是 `fallback`（默认 neutral：和现在 83% neutral
    的生产分布一致，不会凭空给一句台词加戏）。
    """
    return _scene_basis_of(cues, ja_text, zh_text=zh_text, fallback=fallback)[0]


# ACT 先验能覆盖的「弱判断」（任务 #24）：场景是 gen/shy/coq，而且依据只有 phonation
# 或标量 —— src 明示线索（whisper/breath/shy/excite/sad/soft）和日语表面线索都不算弱；
# whispered→whi、exc/ang/sad/cry/sob 也不在弱集里（「太好了！」+ ACT sad 仍是 exc）。
_WEAK_SCENES = frozenset(("gen", "shy", "coq"))
_WEAK_BASIS = frozenset(("phonation", "scalar"))


def is_weak_scene(scene, basis):
    """这个判断是不是「弱判断」（可以被 ACT 覆盖）。公开给测试用。"""
    return scene in _WEAK_SCENES and basis in _WEAK_BASIS


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


# ---- 哭腔延续（`ALBION_VOICE_CARRY`，默认开） ---------------------------------
# 回看前文时只用中文原文能判的东西（不为此再调翻译模型）。
# 切句：句末标点（。！？!?）与换行各算一句；「……」也当句界（「呜呜……我好像要哭了」里的
# 前半截不该和后半截粘成一句）。切完只留非空片段。
_CARRY_BREAK = re.compile(r"[。！？!?\n]|…+")

# 反向线索：她在**换语气**（笑 / 压低声音 / 生气）—— 哭腔不再往下延续。
# 只看最后两句里**离当前句最近**的那一句，避免把更早的笑话当成现在的语气。
# 否定（我没生气）和被否掉的笑（笑不出来）不算换语气。
_ZH_CARRY_BREAK = re.compile(
    r"笑|哈哈|嘿|嘻|呵呵|开心|高兴|兴奋|小声|低声|轻声|悄悄|耳语|压低|"
    r"生气|气死|哼|怒|凶|讨厌|烦死")
_ZH_CARRY_NOT_BREAK = re.compile(r"笑不出[来]?|哭笑不得|不想笑|没心情笑")

# 当前句**自己**的明示非哭线索（非否定）：这些说明她换了语气，不延续。
_CARRY_BLOCK_KINDS = frozenset(("whisper", "excite", "shy"))

# 当前句判出这些场景就用自己的：sob/cry 是本句有哭的证据；exc/whi/ang 是换语气。
_CARRY_BLOCK_SCENES = frozenset(("sob", "cry", "exc", "whi", "ang"))


def _carry_sentences(text):
    """切句（句末标点 / 换行 / 「……」后断），去掉空白片段。"""
    return [s.strip() for s in _CARRY_BREAK.split(text or "") if s.strip()]


def _paragraph_tail(text):
    """同一段落：遇到空行（`\\n\\n`，允许行内空格）就截断，只留最后一段。"""
    if not isinstance(text, str) or not text:
        return ""
    return re.split(r"\n[ \t\r]*\n", text)[-1]


def _tone_switched(sent):
    """这句是不是已经换了语气（笑 / 小声 / 生气）。否定与「笑不出来」不算。"""
    t = _ZH_CARRY_NOT_BREAK.sub("", sent or "")
    return bool(_zh_hit(t, _ZH_CARRY_BREAK))


def carry_cry(scene, cues, prefix):
    """前文最后 2 句还在哭、本句自己没哭/没换语气 → `("cry", level)`；否则 None。

    规则（任务 #18）：
      * 本句自己判出 sob/cry/exc/whi/ang，或提示里有非否定的 whisper/excite/shy 线索
        → 不延续（用自己的）；来自 phonation / 标量 / soft 的 shy、sad、gen、coq、neutral
        都可以被覆盖；
      * 最后 2 句里**更靠近当前句**的那句带反向线索（（笑）「哈哈」「小声」「生气」…）→ 断开；
      * 这 2 句里有真哭 → `cry`/normal；只有快哭 → `cry`/weak；
      * 永远不产出 sob（😭 只跟本句自己的真哭线索走）。
    """
    if scene in _CARRY_BLOCK_SCENES:
        return None
    c = cues if isinstance(cues, dict) else {}
    for item in c.get("src") or []:
        if (isinstance(item, dict) and item.get("kind") in _CARRY_BLOCK_KINDS
                and not item.get("negated")):
            return None
    sents = _carry_sentences(_paragraph_tail(prefix))[-2:]
    if not sents:
        return None
    if _tone_switched(sents[-1]):                  # 最近那句已经换语气了：断开
        return None
    for sent in reversed(sents):                   # 近的优先：强度用更近的那句
        if sob_of(sent, ""):
            return ("cry", "normal")
        if paren_cry(sent) == "cry" or cry_cue(sent):
            return ("cry", "weak")
    return None


# ---- ACT 情绪先验（`ALBION_VOICE_ACT`，2026-10-27 任务 #23，默认开） -------------
# 脑的回复开头带 `<|ACT:{"emotion":"sad"}|>`（壳送进 9885 的文字流 → 服务端当 control 块 →
# `source_prefix` / 中文原文里看得到）。本句自己判不出情绪时用它补位：脸在哭、声音却平淡
# 就是两边各判各的。正则与壳的 `ALBION_ACT_RE` 同形（JSON 里嵌套花括号也能整段吃到）。
_ACT_RE = re.compile(r"<\|ACT\s*(?::\s*)?(\{[\s\S]*?\}\s*)\|>", re.IGNORECASE)

# ACT 的 emotion → 场景。**只收映射得动的**：neutral/think/question/curious 不在这里，
# 没见过的名字也不在这里（宁可不给先验，也不凭名字瞎猜）。
_ACT_SCENE = {
    "happy": "gen",        # intensity ≥ 0.7 时升到 exc（见 act_scene_of）
    "surprised": "exc",
    "sad": "sad",
    "angry": "ang",
    "awkward": "shy",      # 害羞 ≈ awkward（Live2D 的姿态表也是这么合的）
    "shy": "shy",          # 不在 AIRI 那 9 个里，但脑偶尔会这么写
}
_ACT_STRONG = 0.7


def last_act_emotion(text):
    """源文本里**最后一条** ACT 的 `(emotion, intensity)`；没有 / JSON 坏 → None。

    emotion 两种写法都认：字符串 `"sad"`、或 `{"name":"sad","intensity":0.8}`（壳侧解析
    也是这么处理的，见 albion-pet.ts）。**最后一条**才算：一条回复里她可能先笑后哭，
    后面那句的 ACT 才是现在的语气。
    """
    if not isinstance(text, str) or not text:
        return None
    last = None
    for m in _ACT_RE.finditer(text):
        last = m
    if last is None:
        return None
    try:
        payload = json.loads(last.group(1))
    except (ValueError, TypeError):
        return None                       # JSON 坏 = 当没有（不退回更早那条，口径是"最后一条"）
    if not isinstance(payload, dict):
        return None
    emo, intensity = payload.get("emotion"), None
    if isinstance(emo, dict):
        intensity = _num(emo.get("intensity"))
        emo = emo.get("name")
    if not isinstance(emo, str):
        return None
    name = emo.strip().lower()
    return (name, intensity) if name else None


def act_scene_of(text):

    """`prefix + 中文原文` 里最后一条 ACT → 场景先验；给不出（缺失/不认识的 neutral 族）→ None。

    不产出 sob/cry（哭只认本句的明示线索），也不带 emoji —— 先验只管"说话方式"。
    """
    parsed = last_act_emotion(text)
    if not parsed:
        return None
    name, intensity = parsed
    scene = _ACT_SCENE.get(name)
    if scene is None:
        return None
    if name == "happy" and intensity is not None and intensity >= _ACT_STRONG:
        return "exc"                      # 很高兴：明るく弾んだ声（gen 是「優しく、ゆっくり」）
    return scene


def _strip_act(text):
    """去掉 `<|ACT:{…}|>` 块（只给哭腔延续回看原文用；其余地方要保留 ACT 才能判先验）。"""
    if not isinstance(text, str) or not text:
        return text or ""
    return _ACT_RE.sub("", text)


def caption_for(cues, ja_text="", zh_text="", fallback="neutral", prefix=""):
    """桥的入口：情绪提示 + 日语译文 + 中文原文 → 一条通过校验的描述（没把握就返回 ""）。

    `zh_text` 是**中文原文**（桥里传 `req.raw_text`）：真哭/快哭的判据在中文一侧
    （括号动作、委屈类说法），日语译文里看不到。
    `prefix` 是本单元**之前**的中文原文（桥里传 `req.source_prefix`）：做哭腔延续，也用来
    回看 `<|ACT:{…}|>` 情绪先验。
    开关 `ALBION_VOICE_CARRY=0` 或 `prefix` 为空时行为与加这个参数之前逐字节一致；
    `ALBION_VOICE_ACT=0` 时同样逐字节一致（ACT 块不参与判场景）。

    优先级：**本句自己的证据** > 哭腔延续 > ACT 先验 > fallback。
    但「弱判断」是例外（任务 #24）：本句自己只说得出 gen/shy/coq，而且这点判断只来自
    phonation（soft→shy / breathy→coq）或标量（va≥0.2 的 gen）时，ACT 先验可以直接顶掉它
    —— 生产实测中性寒暄句常被翻译腿标成这两种，ACT 因此几乎从不生效。
    src 明示线索（whisper/breath/shy/excite/sad/soft）与日语表面线索**不算弱**，照样优先。
    哭腔延续仍用**本句自己的**判断（`own`）来判，所以延续 > ACT 不变。
    """
    own, own_basis = _scene_basis_of(cues, ja_text, zh_text=zh_text, fallback=None)  # None = 判不出
    act = act_scene_of("%s%s" % (prefix or "", zh_text or "")) if voice_act_on() else None
    carry_scene = own if own is not None else fallback   # 延续判据只看本句（与加 ACT 之前同一口径）
    scene = own or act or fallback
    if act and is_weak_scene(own, own_basis):
        scene = act                           # 弱判断让位给 ACT（level 仍看 cues）
    level = level_of(cues)
    if prefix and voice_carry_on():
        # 回看哭腔时先把 ACT 块剥掉（与开关无关：它只是元数据）：留着会把「最后一句」
        # 变成 `<|ACT…|>`，让 `_tone_switched` 看不到真正的上一句已经换了语气。
        carried = carry_cry(carry_scene, cues, _strip_act(prefix))
        if carried:
            scene, level = carried
    caption = compose_caption(scene, level)
    if validate_caption(caption):
        return ""                             # 理论上不会发生；真发生了也不送半句
    return caption


def breath_cue_prefers_codebook(cues, ja_text="", zh_text=""):
    """明示「喘」线索（（喘着气）/（喘息））→ 这一句交给**码本的喘息锚**，不用自由描述。

    自由描述里 `breath` 场景是 `coq`（「鼻にかけて、柔らかく、甘える」），是撒娇不是喘；
    码本里有训练过的 `息を荒くして、甘く喘ぐように、近い距離で`（cfg 4.5）。
    只认**明示 cue**：`phonation=breathy` 这类自述仍走 coq（弱判断，留给 ACT/其它规则），
    真哭/快哭优先于 cue，`_scene_basis_of` 会先返回 cry，这里自然不放行。
    """
    scene, basis = _scene_basis_of(cues, ja_text, zh_text=zh_text)
    return scene == "coq" and basis == "cue"


# ---- 句末非语言 emoji ---------------------------------------------------------
# 只收 `checkpoints/EMOJI_ANNOTATIONS.md` 里**非语言**的那几个（笑声/叹气/哈欠），
# 风格类（😊😠🥺…）会改整句语气，不在本阶段范围内。
# 😭 只在**真哭**（`sob_of` 为真）时回来：句末 😭 会多出 0.3–1.6 s 抽泣声，
# 快哭/委屈不加（用户口径：「快哭用快哭，真哭用真哭」）。
EMOJI_WHITELIST = ("🤭", "😮‍💨", "🥱", "😭")

_EMOJI_RULES = (
    ("🤭", re.compile(r"笑|哈哈|嘿|嘻|呵呵|くす|ふふ|ふふっ")),
    ("😮‍💨", re.compile(r"唉|叹|叹气|呼|ため息|ふう|はあ|ほっと")),
    ("🥱", re.compile(r"哈欠|困|睡|疲|あくび|眠|ふわ")),
)

SOB_EMOJI = "😭"

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


def pick_emoji(*texts, sob=None):
    """台词本身在笑/叹气/打哈欠/真哭 → 对应 emoji；否则 ""。按 `_EMOJI_RULES` 的优先级。

    `sob` 是真哭判定（桥里传 `sob_of(raw_text, ja_text)`，和 caption 共用同一个函数）：
      * `sob=True` → 先给 😭（真哭压过其它语气）；
      * `sob=False`/未给（自己按第一条文本当中文、其余当日语兜底判）→ 😭 永远不出来，
        快哭/委屈一律不加 emoji。
    """
    blob = " ".join(t for t in texts if isinstance(t, str) and t)
    if sob is None:
        first_zh = texts[0] if texts and isinstance(texts[0], str) else ""
        rest_ja = " ".join(t for t in texts[1:] if isinstance(t, str))
        sob = sob_of(first_zh, rest_ja)
    if sob:
        return SOB_EMOJI
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
    # 快哭：normal/strong 定死旧措辞（两段结构），weak 仍走三段模板；别的两段句不放行
    _legacy = "今にも泣き出しそうに、声を震わせて話す。"
    assert compose_caption("cry", "normal") == compose_caption("cry", "strong") == _legacy
    assert validate_caption(_legacy) is None
    assert compose_caption("cry", "weak") == "少し声を震わせて、途切れがちに、言う。"
    assert validate_caption("今にも泣き出しそうに、声を潜めて話す。") is not None
    assert validate_caption(_legacy + "。") is not None
    # 相邻句同场景同强度 → 同一个描述（不跳）
    assert compose_caption("sad", "normal") == compose_caption("sad", "normal")
    assert compose_caption("sad", "weak") != compose_caption("sad", "strong")
    # emoji：白名单外的剥掉
    assert strip_emoji("もう大丈夫😊です🙂") == ("もう大丈夫です", 2)
    assert pick_emoji("ふふ、そうなんだ") == "🤭"
    assert pick_emoji("はあ、疲れた") == "😮‍💨"
    assert pick_emoji("眠い……") == "🥱"
    assert pick_emoji("うう、泣きそう") == ""      # 快哭：不加 emoji（😭 只跟真哭）
    assert pick_emoji("（大哭）不要走") == "😭"      # 真哭：句末才回来
    assert pick_emoji("呜呜……我好像要哭了") == ""   # 呜呜/要哭 = 快哭，不配 😭
    assert pick_emoji("はい、わかりました。") == ""
    b = EmojiBudget(max_keys=2)
    assert b.take(("s", 1)) is True and b.take(("s", 1)) is False
    assert b.take(("s", 2)) is True and b.take(("s", 3)) is True
    assert len(b) == 2                              # 有界：最老的被挤掉
    # 哭腔延续（默认开）：快哭 → cry/weak，真哭 → cry/normal；换语气/空行/隔三句都断开。
    _keep = os.environ.pop(VOICE_CARRY_ENV, None)   # 自检不能受当前环境变量影响
    try:
        assert caption_for({}, "ごめんなさい。", "我先走了。",
                           prefix="（快哭了）我真的有点难受。") == compose_caption("cry", "weak")
        assert caption_for({}, "ごめんなさい。", "我先走了。",
                           prefix="（大哭）对不起……") == compose_caption("cry", "normal")
        assert caption_for({}, "ごめんなさい。", "我先走了。",
                           prefix="（大哭）对不起。哈哈，逗你的。") == compose_caption("neutral")
        assert caption_for({}, "ごめんなさい。", "我先走了。",
                           prefix="（大哭）对不起。\n\n换个话题。") == compose_caption("neutral")
        assert caption_for({}, "ごめんなさい。", "我先走了。",
                           prefix="（大哭）对不起。一。二。现在。") == compose_caption("neutral")
        os.environ[VOICE_CARRY_ENV] = "0"
        assert caption_for({}, "ごめんなさい。", "我先走了。",
                           prefix="（大哭）对不起……") == compose_caption("neutral")
    finally:
        os.environ.pop(VOICE_CARRY_ENV, None)
        if _keep is not None:
            os.environ[VOICE_CARRY_ENV] = _keep
    # ACT 先验（默认开）：本句判不出 → 用 ACT；本句自己有证据 → 自己的。
    _keep = os.environ.pop(VOICE_ACT_ENV, None)
    try:
        _sad = '<|ACT:{"emotion":"sad"}|>'
        assert act_scene_of(_sad) == "sad"
        assert act_scene_of('<|ACT:{"emotion":{"name":"happy","intensity":0.9}}|>') == "exc"
        assert act_scene_of('<|ACT:{"emotion":"neutral"}|>') is None    # 中性族：不给先验
        assert act_scene_of('<|ACT:{"emotion":}|>') is None             # JSON 坏：当没有
        assert act_scene_of(_sad + '<|ACT:{"emotion":"angry"}|>') == "ang"   # 最后一条算数
        assert caption_for({}, "そう、ですか。", "嗯。", prefix=_sad) == compose_caption("sad")
        assert caption_for({}, "そう、ですか。", prefix=_sad,
                           zh_text='<|ACT:{"emotion":"sad"}|>嗯。') == compose_caption("sad")
        assert caption_for({"valence": 0.6, "arousal": 0.7, "intensity": 0.5},
                           "やった！", "太好了！", prefix=_sad) == compose_caption("exc")
        # 哭腔延续压过 ACT 先验
        assert caption_for({}, "ごめんなさい。", "我先走了。", prefix="（快哭了）对不起。" + _sad) \
            == compose_caption("cry", "weak")
        # 弱判断（phonation / 标量给的 gen·shy·coq）让 ACT 顶掉（任务 #24）
        assert caption_for({"phonation": "soft"}, "そう、ですか。", "嗯。", prefix=_sad) \
            == compose_caption("sad")
        assert caption_for({"valence": 0.25}, "そう、ですか。", "嗯。", prefix=_sad) \
            == compose_caption("sad")
        assert caption_for({"phonation": "breathy"}, "そう、ですか。", "嗯。",
                           prefix='<|ACT:{"emotion":"happy"}|>') == compose_caption("gen")
        # 明示线索 / whispered / 强场景不让位
        assert caption_for({"src": [{"kind": "shy"}]}, "そう、ですか。", "嗯。", prefix=_sad) \
            == compose_caption("shy")
        assert caption_for({"phonation": "whispered"}, "そう、ですか。", "嗯。", prefix=_sad) \
            == compose_caption("whi")
        os.environ[VOICE_ACT_ENV] = "0"
        assert caption_for({}, "そう、ですか。", "嗯。", prefix=_sad) == compose_caption("neutral")
    finally:
        os.environ.pop(VOICE_ACT_ENV, None)
        if _keep is not None:
            os.environ[VOICE_ACT_ENV] = _keep
    print("voice_caption 自检通过：%d 场景 × %d 档 = %d 条描述"
          % (len(SCENES), len(LEVELS), len(SCENES) * len(LEVELS)))


if __name__ == "__main__":
    _selftest()
