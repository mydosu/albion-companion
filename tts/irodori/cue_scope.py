# -*- coding: utf-8 -*-
"""cue 的**源句归属**（Astra 第 10 轮 ①；开关 `ALBION_CUE_SCOPE`，默认关）。

本模块只做"坐标 + 归属"这一段（纯函数、纯 CPU）；判罚由桥与规划器各按自己的口径执行。

规则：
  * 舞台提示（cue）只作用于**它所在的那个源句**；跨度是**中文原文的 Unicode code point 下标**
    （不是日语下标 —— 译文里这些词早就没了，坐标不能混用）；
  * 一个源句译成多个日文子段时，各子段共享该句作用域（同一个 `sid`）：这是**同一条指令
    持续有效**，不重复加档、也不刷新可信链（判重在规划器，按 `sid` 比 `prev`）；
  * 句末即失效：下一句拿到的是**另一个** `sid`，上一句的局部 D 不会被当成新出现的 D；
  * 引号/括号里的提示词是**转述或讨论该词本身**（"他说『小声说』只是舞台提示"）→ 不产生 D；
  * **归属看不出来就不注入**（没跨度 / 跨度非法 / 分句数与子段数对不上 / 跨坐标系），
    记 `scope_unknown`：**不再默认挂首句**；
  * 跨句指令（"接下来都小声说"）由 `ALBION_CROSS_SENTENCE`（v6 内默认开）打开：指令句起
    **后续源句**都带走该 cue（副本盖接收句自己的 `sid`），撤销句起恢复正常。作用域原本只在本
    unit 内；**跨 unit 的延续靠调用方把前文源切片传进来重放**（`active_directive(prefix)`，
    见壳侧 `Coordinator.prepare` 的 `source_prefix`）—— 结果只取决于前文文本，与 unit 取回
    顺序无关，因此对翻译预取（提前 prepare 下一单元）免疫。
"""
import os
import re

import delivery_stage as DS      # 引号判据复用同一条（「」『』“”/半角括号的配对语义）

# 切句判据的**唯一来源**：桥的翻译腿与这里的归属必须同一套，否则两端坐标会漂
SENT = re.compile(r"[^。！？!?\n]+")

# 跨句指令：**本句作用域**的开关关时明确记未支持；v6 `ALBION_CROSS_SENTENCE` 起在本 unit 内支持
CROSS_SENTENCE = "unsupported"
# ---- 跨句指令词表（Astra 第 12 轮 ②；开关 `ALBION_CROSS_SENTENCE`，v4/v5 时代默认关）--------
# 只认"**前瞻性标记 + 发声词**"同句出现（"接下来都小声说"/"从现在起轻点说"），以及撤销句
# （"可以正常说话了"/"接下来都别这样"）。作用范围＝从该句起**后续源句**；撤销句起恢复正常；
# 本 unit（一次 /v2/prepare 的源切片）末即失效 —— 跨 unit 的续接由调用方**重放前文源切片**
# 实现（`active_directive`），不是桥持有会话状态：同一份前文重放出的结果永远相同。
CROSS_SUPPORTED = "supported"
CROSS_UNSUPPORTED = "unsupported"
# 跨 unit 的续接**已支持**（桥按 `source_prefix` 重放指令状态，本层只消费副本、不外推）。
# 之前这里是 "unsupported"（纯本 unit 内实现）；保留常量名，值改为实际状态。
CROSS_UNIT = CROSS_SUPPORTED
# 跨句指令的 kind 白名单（撤销/合成都要落在这三种里，才谈得上"持续发声方式"）
CROSS_KINDS = ("soft", "whisper", "breath")
_PERSIST_MARK = re.compile(r"接下来|从现在起|从现在开始|从现在|以后|之后|往后|一直|全程|从此")
_PERSIST_PHON = re.compile(r"小声|低声|轻点|轻声|轻轻|小点声|悄悄|耳语|气声|压低|别大声|轻一些")
REVOKE = re.compile(r"(?:可以|就|能|请)?正常(?:地)?(?:说话|说就行|说就好|说话就行)"
                    r"|不用(?:再)?小声|别(?:再)?小声|不用压(?:低)?|可以出声"
                    r"|放开(?:声音|嗓子)|(?:接下来|以后|从现在起|之后|现在)(?:都)?(?:别|不要)这样"
                    r"|别这样了|不要这样了|恢复正常")
_KIND_BY_PHON = (("耳语", "whisper"), ("气声", "breath"), ("喘", "breath"))

_TRUE = ("1", "true", "on", "yes")
_FALSE = ("0", "false", "off", "no")
# 否定窗口（与 bridge.zh_source_cues 的 `_ZH_NEG` 同口径：否定词 + 至多 4 个非标点字符）
# 只用于**发声词**：`接下来别小声说` 否定的是"小声"，不算持续指令。
_NEG_BEFORE = re.compile(r"(?:别|不要|不用|不许|不是)\s*[^，。！？、]{0,4}$")
# 对称引号（开闭同字符）：数它前面的个数奇偶；非对称的走 delivery_stage.QUOTE_PAIRS
_SYM_QUOTES = ('"', "'")


def enabled(value=None, default=False):
    """开关 `ALBION_CUE_SCOPE`（默认关）。显式布尔优先（请求体/构造参数），否则看进程环境。

    `default` 供 v6 用（v6 起默认开：② 跨句指令以源句身份为前提）。
    **`0/false/off/no` 是明确的"关"** —— 不能因为 `default=True` 就把它读成开（Sol 第 13 轮第四条）。
    """
    if isinstance(value, bool):
        return value
    s = (os.environ.get("ALBION_CUE_SCOPE") or "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def cross_enabled(value=None, default=False):
    """开关 `ALBION_CROSS_SENTENCE`（v4/v5 时代默认关；v6 侧默认开）。同形，同样识别 `0`。"""
    if isinstance(value, bool):
        return value
    s = (os.environ.get("ALBION_CROSS_SENTENCE") or "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def _negated_at(text, pos):
    """`pos` 之前的**发声词**是否被否定（`接下来别小声说` / `现在不要耳语`）—— 不算持续指令。"""
    return bool(_NEG_BEFORE.search((text or "")[:max(0, pos)][-12:]))


def _first_valid(pattern, text, start=0, end=None, negated=False):
    """`text[start:end)` 里的**第一个有效匹配**（跳过引述处，可选跳过被否定的位置）。

    只看第一个匹配是不够的：`他说「接下来都耳语说」，但我们接下来都小声说。` 的首个发声词
    落在引号里，返回 None 就把**后半句的真指令一起漏掉**（撤销句同理）—— 逐个往后找第一个
    不在引述/否定位置里的匹配（Sol 第 14 轮第三条）。找不到 ⇒ None。
    """
    for m in pattern.finditer(text, start, len(text) if end is None else end):
        if _quoted(text, m.start()):
            continue
        if negated and _negated_at(text, m.start()):
            continue
        return m
    return None


def _persist_kind(text, start=0, end=None):
    """跨句指令句 → 要持续的那个 cue kind（不是指令句 ⇒ None）。

    `text[start:end]` = 要判的那一句（`end=None` 时就是整个 `text`）；整篇语境只用来看引述。
    **引述/否定**掉的标记不算指令：`他说「接下来都耳语说」` 是转述、`接下来别小声说` 否定的是
    发声词 —— 两者都不外推（Sol 第 13 轮第三条）；引述**后面**还有真指令时按后者算（第 14 轮）。
    """
    sent = text if end is None else text[start:end]
    if not (_PERSIST_MARK.search(sent or "") and _PERSIST_PHON.search(sent or "")):
        return None
    m = _first_valid(_PERSIST_PHON, text, start, end, negated=True)
    if m is None:
        return None
    word = m.group(0)
    for k, kind in _KIND_BY_PHON:
        if k in word:
            return kind
    return "soft"


def active_directive(text):
    """重放 `text` 里的源句，返回**最后一句之后仍生效**的跨句指令 kind（没有 ⇒ None）。

    纯函数、只看文本：壳侧每个 unit 只带自己的源切片，跨 unit 的延续就是把**前文源切片**送进来
    重放。结果只取决于前文文本，与 unit 取回顺序无关 —— 翻译预取提前 prepare 下一单元时也算得对。
    """
    active = None
    for a, b in sentences(text or ""):
        # 撤销：**逐个**看，引号里的那个是转述，它后面可能还有真撤销（Sol 第 14 轮第三条）
        if _first_valid(REVOKE, text, a, b) is not None:
            active = None
            continue
        kind = _persist_kind(text, a, b)
        if kind:
            active = kind
    return active


def sentences(text):
    """源句跨度列表 [(start, end), …]（半开区间；**有序、不重叠，间隔为句界字符** ——
    不是"连续覆盖"，句界标点落在间隔里）。"""
    return [(m.start(), m.end()) for m in SENT.finditer(text or "")]


def _quoted(text, pos):
    """匹配点是否落在引号里 —— 转述/讨论该词，不算当前说话者的发声指令。"""
    if DS._quoted(text, pos):                      # 「」『』“” 与半角括号（配对计数）
        return True
    return any(text.count(q, 0, pos) % 2 for q in _SYM_QUOTES)   # 对称引号：奇数 ⇒ 在里面


def _sentence_of(span, sents, n):
    """cue 的 code point 跨度落在哪个源句；看不出归属就返回 None（宁可不注入）。"""
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    s, e = span
    if isinstance(s, bool) or isinstance(e, bool):
        return None
    if not isinstance(s, int) or not isinstance(e, int) or not (0 <= s < e <= n):
        return None
    for j, (a, b) in enumerate(sents):
        # 起**终点都要落在同一源句**内：`第一句。第二句。` 里 span[1,6] 从第一句跨进第二句，
        # 只查起点会把它错派给第一句（Sol 第 10 轮第一条）。跨句界一律算归属不明、不注入。
        if a <= s and e <= b:
            return j
    return None


def attribute(text, cues, n_segments, unit_key="", cross_sentence=False, cross_in=None):
    """把 cue 按源句归给子段。返回 `(rows, diag)`。

    `rows[i]` = 第 i 个子段该带走的 cue（每个都盖了 `sid` = 它所属源句的身份）；
    `diag["ids"][i]` = 该子段的源句身份（`<unit_key>#<句号>`），判不出就是 None。
    子段数与原句数对不上（跨坐标系 / 混语言切片）⇒ 整份 `scope_unknown`，一律不注入。

    `cross_sentence=True`（v6 ②，默认关 ⇒ 逐字段等于加它之前那版）：识别**跨句指令**并按
    源句把指令复制给**后续源句**（每个副本盖接收句自己的 `sid`，另带 `cross_from`/`cross_to`
    与 `cross_sentence=True`）；撤销句起恢复正常。

    `cross_in`（v6 ② 的**跨 unit** 一半）：本 unit **之前**已生效的指令 kind —— 由调用方用
    `active_directive(前文源切片)` 算出来传进来。跨 unit 的副本 `cross_from=-1`（指令句不在本
    slice 的坐标系里），`cross_to` = 本 unit 的末句。只在 `cross_sentence=True` 时生效。

    只在**源句文本**上匹配词表 —— cue 的跨度常常只盖住"小声"两个字，看不到"接下来都"。
    """
    text = text or ""
    sents = sentences(text)
    diag = {"basis": "span", "ids": [None] * max(0, int(n_segments or 0)), "unknown": [],
            "quoted": 0, "sentences": len(sents), "cross_sentence": CROSS_SENTENCE}
    rows = [[] for _ in diag["ids"]]
    if len(sents) != len(rows):
        diag["basis"] = "scope_unknown"
        diag["unknown"] = ["segment_count_mismatch"] * len(cues or [])
        return rows, diag
    diag["ids"] = ["%s#%d" % (unit_key or "u", j) for j in range(len(rows))]
    for c in (cues or []):
        if not isinstance(c, dict):
            diag["unknown"].append("bad_cue")
            continue
        j = _sentence_of(c.get("span"), sents, len(text))
        if j is None:
            diag["unknown"].append("no_span" if not c.get("span") else "bad_span")
            continue
        if _quoted(text, c["span"][0]):
            diag["quoted"] += 1                     # 引述/讨论提示词本身：不产生 D
            continue
        rows[j].append(dict(c, sid=diag["ids"][j], sentence=j))
    if cross_sentence:
        _attribute_cross(text, sents, rows, diag, carried=cross_in)
    return rows, diag


def _attribute_cross(text, sents, rows, diag, carried=None):
    """跨句指令的作用范围（原地改 `rows` 与 `diag`）。见 `attribute` 的说明。

    `carried`（前文已生效的 kind）：`cross_from` 记 **-1** —— 指令句在**上一个 unit** 的坐标系
    里，本 slice 里没有它的下标，不能假装是第 0 句。
    """
    diag["cross_sentence"] = CROSS_SUPPORTED
    diag["cross_unit"] = CROSS_UNIT
    ranges, active, origin = [], None, -1 if carried in CROSS_KINDS else None
    if origin is not None:
        active = {"kind": carried, "negated": False, "cross_synth": True}
    for j in range(len(rows)):
        # 撤销句：引号里的那个是转述；它后面还有真撤销时要逐个往后找（Sol 第 14 轮第三条）
        if _first_valid(REVOKE, text, sents[j][0], sents[j][1]) is not None:
            if active is not None:                  # 撤销句：它自己起就恢复正常
                ranges.append([origin, j - 1])
                active, origin = None, None
            continue
        if active is None or origin < 0:            # 生效中的**跨 unit** 指令可被本句自己的新指令顶掉
            kind = _persist_kind(text, sents[j][0], sents[j][1])
            if kind:
                src = next((c for c in rows[j] if c.get("kind") in CROSS_KINDS
                            and not c.get("negated")), None)
                # 词表认出来了、但抽取层没给这一句 cue（"从现在起轻轻地说"）⇒ 自己合成一条，
                # 只在**后续源句**上用；指令句本身仍按它自己的 cue 走。
                src = src or {"kind": kind, "negated": False, "cross_synth": True}
                active, origin = src, j
        if active is not None and origin < j:
            rows[j].append(dict(active, sid=diag["ids"][j], sentence=j,
                                cross_sentence=True, cross_from=origin))
    if active is not None:
        ranges.append([origin, len(rows) - 1])
        diag["open_at_end"] = True                  # 本 unit 末仍生效 ⇒ 由调用方带到下一 unit
    for a, b in ranges:                             # 补齐生效区间（诊断要能直接读出来）
        for k in range(max(0, a), b + 1):
            for c in rows[k]:
                if c.get("cross_sentence") and c.get("cross_from") == a:
                    c["cross_to"] = b
    diag["persist"] = ranges



def _selfcheck():
    t = "（小声说）今天早点休息。明天早上我叫你。"
    sents = sentences(t)
    assert sents == [(0, 11), (12, 19)], sents                # 有序、不重叠、不交叉
    assert all(sents[i][1] < sents[i + 1][0] for i in range(len(sents) - 1))
    # 间隔里只该有句末标点/空白（源范围合法：不能越界、不能重叠、不能漏掉整句）
    gaps = [t[a:b] for (_, a), (b, _) in zip(sents, sents[1:])]
    assert all(len(g) <= 3 and not any(ch.isalnum() for ch in g) for g in gaps), gaps
    cues = [{"kind": "soft", "negated": False, "span": [0, 5]}]
    rows, diag = attribute(t, cues, 2, unit_key="0-18")
    assert (rows[0][0]["sid"], diag["ids"]) == ("0-18#0", ["0-18#0", "0-18#1"])
    assert rows[1] == [] and diag["unknown"] == []           # 第二句没得到这条 D
    # 后句的 cue 归后句，不挂首句
    t2 = "今天早点休息。明天早上（小声说）我叫你。"
    rows, diag = attribute(t2, [{"kind": "soft", "span": [9, 14]}], 2, unit_key="k")
    assert rows[0] == [] and rows[1][0]["sid"] == "k#1"
    # 引述/讨论：不算指令
    t3 = '他说"（小声说）"只是舞台提示。'
    rows, diag = attribute(t3, [{"kind": "soft", "span": [3, 8]}], 1)
    assert rows == [[]] and diag["quoted"] == 1 and diag["unknown"] == []
    t4 = "「小声说」只是舞台提示。"
    assert attribute(t4, [{"kind": "soft", "span": [1, 6]}], 1)[1]["quoted"] == 1
    # 没有跨度（emoji / ！！）与越界跨度：不猜，记 scope_unknown
    rows, diag = attribute(t, [{"kind": "excite"}, {"kind": "soft", "span": [99, 101]}], 2)
    assert rows == [[], []] and diag["unknown"] == ["no_span", "bad_span"]
    # 跨句跨度：只查起点会把 [1,6] 错派给第一句（**旧实现下这条必红**），现在必须算归属不明。
    # 「第一句。」= 0..4、「第二句。」= 4..8，[1,6] 跨过句界（Sol 第 10 轮第一条）。
    t6 = "第一句。第二句。"
    rows, diag = attribute(t6, [{"kind": "soft", "span": [1, 6]}], 2, unit_key="k")
    assert rows == [[], []], rows
    assert diag["unknown"], diag
    # 子段数与源句数对不上（跨坐标系）：整份不注入，且**不挂首句**
    rows, diag = attribute(t, cues, 3, unit_key="k")
    assert diag["basis"] == "scope_unknown" and diag["ids"] == [None] * 3
    assert diag["unknown"] == ["segment_count_mismatch"] and rows == [[], [], []]
    # code point 下标：非 BMP emoji 出现在 cue 之前，归属不能漂
    t5 = "今天早点休息😊。明天早上（小声说）我叫你。"
    sp = t5.index("（小声说")
    rows, _ = attribute(t5, [{"kind": "soft", "span": [sp, sp + 5]}], 2, unit_key="k")
    assert rows[0] == [] and rows[1][0]["sentence"] == 1
    # 跨句指令：明确记未支持，不做任何外推（句 1 的 cue 不会出现在句 2）
    rows, diag = attribute(t, [{"kind": "whisper", "span": [0, 5]}], 2, unit_key="k")
    assert diag["cross_sentence"] == CROSS_SENTENCE == "unsupported"
    assert len(rows[0]) == 1 and rows[1] == []
    # ---- v6 ② 跨句指令（默认关 ⇒ 上面那条仍成立）----
    t7 = "接下来都小声说。明天早上我叫你。"
    c1 = {"kind": "soft", "negated": False, "span": [4, 6]}          # 小型「小声」在源句里
    rows, diag = attribute(t7, [dict(c1)], 2, unit_key="k", cross_sentence=True)
    assert diag["cross_sentence"] == CROSS_SUPPORTED and diag["cross_unit"] == CROSS_UNIT
    assert diag["persist"] == [[0, 1]] and diag["open_at_end"] is True
    assert len(rows[0]) == 1 and len(rows[1]) == 1                   # 指令扩到后续源句
    assert (rows[1][0]["sid"], rows[1][0]["sentence"]) == ("k#1", 1)  # 副本盖接收句自己的身份
    assert (rows[1][0]["cross_sentence"], rows[1][0]["cross_from"],
            rows[1][0]["cross_to"]) == (True, 0, 1)
    assert rows[1][0]["kind"] == "soft" and "cross_sentence" not in rows[0][0]
    # 撤销句起恢复正常；它后面不再有副本
    t8 = "从现在起小声说。今天天气不错。可以正常说话了。明天早上我叫你。"
    rows, diag = attribute(t8, [{"kind": "soft", "negated": False, "span": [4, 6]}], 4,
                           unit_key="k", cross_sentence=True)
    assert diag["persist"] == [[0, 1]] and not diag.get("open_at_end"), diag
    assert [len(r) for r in rows] == [1, 1, 0, 0], rows
    # "接下来都别这样" = 撤销，不是新指令
    t9 = "接下来都小声说。接下来都别这样。明天早上我叫你。"
    rows, diag = attribute(t9, [{"kind": "soft", "negated": False, "span": [4, 6]}], 3,
                           unit_key="k", cross_sentence=True)
    assert diag["persist"] == [[0, 0]] and [len(r) for r in rows] == [1, 0, 0], (diag, rows)
    # 词表认出指令、抽取层没给 cue（"轻轻"）⇒ 合成一条，只在后续源句上用
    t10 = "从现在起轻轻地说。明天早上我叫你。"
    rows, diag = attribute(t10, [], 2, unit_key="k", cross_sentence=True)
    assert diag["persist"] == [[0, 1]] and rows[0] == [], rows
    assert (rows[1][0]["kind"], rows[1][0].get("cross_synth")) == ("soft", True)
    # 否定/引述里的指令不算；开关关时一切照旧（不外推、不加字段）
    assert _persist_kind("接下来都小声说") == "soft"
    assert _persist_kind("小声说") is None
    assert _persist_kind("接下来别小声说") is None, "否定掉的发声词不算持续指令"
    assert _persist_kind("他说「接下来都耳语说」") is None, "引述里的指令不算指令"
    assert _persist_kind("从现在起轻轻地说") == "soft"
    rows, diag = attribute(t9, [{"kind": "soft", "negated": False, "span": [4, 6]}], 3,
                           unit_key="k")
    assert diag["cross_sentence"] == "unsupported" and "persist" not in diag
    # ---- 跨 unit 的延续（v6 ② 流式那一半）：前文切片重放 + `cross_in` ----
    pre = "接下来都耳语说。今天天气不错。"
    assert active_directive(pre) == "whisper"
    assert active_directive("今天天气不错。") is None
    assert active_directive("接下来都耳语说。可以正常说话了。") is None, "撤销后不再生效"
    assert active_directive("他说「接下来都耳语说」。") is None, "引述不算"
    assert active_directive("「可以正常说话了。」") is None, "引述的撤销本来也没生效对象"
    assert active_directive("接下来都耳语说。「可以正常说话了。」") == "whisper", "引述的撤销不算撤销"
    assert active_directive("接下来都耳语说。先把这件事做完。") == "whisper", "没撤销就一直在"
    assert active_directive("接下来都耳语说。") == "whisper"
    # ---- 引述后面还有真指令/真撤销：找**第一个有效**匹配，不在引述处收工（Sol 第 14 轮第三条）----
    q1 = "他说「接下来都耳语说」，但我们接下来都小声说。"
    assert _persist_kind(q1) == "soft", "引述后面的真指令不许被引述挡掉"
    assert active_directive(q1) == "soft"
    assert _persist_kind("他说「接下来都小声说」，但我们接下来都耳语说。") == "whisper"
    q2 = "接下来都耳语说。有人说「可以正常说话了」，但现在可以正常说话了。"
    assert active_directive(q2) is None, "引述后面的真撤销必须生效"
    rows, diag = attribute(q1, [], 1, unit_key="k", cross_sentence=True)
    assert diag["persist"] == [[0, 0]] and diag["open_at_end"], diag   # 真指令被认出（外推后句）
    # 本 unit 只有一句、且前文已生效 ⇒ 该句拿到副本，cross_from=-1（指令句不在本 slice 里）
    rows, diag = attribute("今天天气不错。", [], 1, unit_key="8-15",
                           cross_sentence=True, cross_in="whisper")
    assert diag["ids"] == ["8-15#0"] and diag["persist"] == [[-1, 0]] and diag["open_at_end"]
    assert (rows[0][0]["kind"], rows[0][0]["cross_sentence"], rows[0][0]["cross_from"],
            rows[0][0]["cross_to"]) == ("whisper", True, -1, 0)
    # 撤销句：本 unit 起恢复正常（不拿副本），且它自己不带 cue
    rows, diag = attribute("可以正常说话了。", [], 1, unit_key="k",
                           cross_sentence=True, cross_in="whisper")
    assert rows == [[]] and diag["persist"] == [[-1, -1]] and not diag.get("open_at_end"), (rows, diag)
    # 本句自己的新指令顶掉跨 unit 的旧指令
    rows, diag = attribute("从现在起耳语说。", [{"kind": "whisper", "negated": False,
                                                "span": [4, 6]}], 1, unit_key="k",
                           cross_sentence=True, cross_in="soft")
    assert rows[0][0]["kind"] == "whisper" and not rows[0][0].get("cross_sentence"), rows
    # 跨 unit 的开关没开时一律不注入（旧行为）
    rows, diag = attribute("今天天气不错。", [], 1, unit_key="k", cross_in="whisper")
    assert rows == [[]] and diag["cross_sentence"] == "unsupported"
    # ---- 开关读法：显式 > 环境（0/false 是明确的"关"）> 默认 ----
    _keep = os.environ.pop("ALBION_CUE_SCOPE", None)      # 自检不能受当前环境变量影响
    assert enabled(True) and not enabled(False) and not enabled(None)
    assert enabled(None, default=True), "v6 的默认值是开"
    os.environ["ALBION_CUE_SCOPE"] = "1"
    assert enabled(None) is True and enabled(None, default=False) is True   # 环境开关能自己打开
    os.environ["ALBION_CUE_SCOPE"] = "0"
    assert enabled(None, default=True) is False, "ALBION_CUE_SCOPE=0 必须读成关"
    assert enabled(True, default=False) is True, "显式布尔压过环境"
    os.environ.pop("ALBION_CUE_SCOPE", None)
    assert enabled(None) is False
    assert cross_enabled(True) and not cross_enabled(False)
    assert cross_enabled(None, default=True) is True and cross_enabled(None) is False
    os.environ["ALBION_CROSS_SENTENCE"] = "0"
    assert cross_enabled(None, default=True) is False, "ALBION_CROSS_SENTENCE=0 必须读成关"
    os.environ["ALBION_CROSS_SENTENCE"] = "1"
    assert cross_enabled(None) is True
    os.environ.pop("ALBION_CROSS_SENTENCE", None)
    if _keep is not None:
        os.environ["ALBION_CUE_SCOPE"] = _keep
    print("cue_scope 自检通过：源句跨度 / 按句归属 / 引述不产 D / 归属不明不注入 / code point / "
          "跨句 + 跨 unit 延续 / 开关读 0")



if __name__ == "__main__":
    _selfcheck()
