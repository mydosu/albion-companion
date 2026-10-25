# -*- coding: utf-8 -*-
"""桥的 v2 准备接口（P2）。规格：方案 §3.2。

与旧 `/v1/audio/speech` 的分工：
  - 旧接口一行不改（`prod-copy/bridge.py` 保持只读），v2 是**新增**路径；
  - v2 **不读写**桥的进程级全局 `_ASIDE_DEPTH` / `_ctx_state`：心声剥离已在会话解析层做完，
    这里只做"把已剥好的可说话文本译成日语、按 unit_id 逐项返回"；
  - 翻译模型/提示词/退路**不另选**：默认直接用旧桥的 `to_japanese`（惰性 import）。

修掉的两件事（旧 `to_japanese` 的坑）：
  ① 全局按源句限流，单元内并行翻译也不会把并发倍增；
  ② `"\\n".join(o for o in outs if o)` 会把空译文静默丢掉（听感上就是漏台词）—— v2 里空译文
     一律 `failed(stage=translation)`，绝不伪装成 no_audio。

源句乱序完成后按原顺序组装，任一句空译或失败都不能静默丢句。
"""
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

import cue_scope

KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z0-9]")
# 中文独有的虚词：出现就基本可断定是中文（旧桥同判据，见 bridge.py:is_japanese）
CN_ONLY = re.compile(r"[的了吗吧呢这]")
# 可发音内容：字/词/假名/汉字（纯符号段不合成）
SPEAKABLE = re.compile(r"[\w\u3040-\u30ff\u4e00-\u9fff]")
SENT = cue_scope.SENT            # 切句判据的唯一来源（归属侧必须与翻译腿同一套，否则坐标会漂）
ASIDE = re.compile(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^\]]*\]|<\|[^|]*\|>")


# ---------------- 纯函数（无状态，可离线测） ----------------
def is_japanese(text: str) -> bool:
    """假名占比够高才算「本来就在说日语」；夹中文虚词一律按中文处理。

    ponytail: 与旧桥 `bridge.py:is_japanese` 判据一致，等价性由 tests 逐条比对；
    落地时由 bridge.py 反向 import 本模块，避免两处判据漂移。
    """
    if CN_ONLY.search(text or ""):
        return False
    kana = len(KANA.findall(text or ""))
    body = kana + len(HAN.findall(text or "")) + len(LATIN.findall(text or ""))
    return body > 0 and kana / body >= 0.5


def pick_speech_slices(text: str):
    """语言选择（无状态纯函数）：有可用日语行就只念日语行，否则整篇当中文待译。

    返回 (mode, lines)。**先按行判断，再规整行内空白** —— 行结构一旦提前压平，双语回复就会
    被当成一段中文去翻译，然后把日语行再念一遍（P2 明确要避免的重复朗读）。
    """
    lines = [" ".join(l.split()) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    ja = [l for l in lines if is_japanese(l)]
    if ja:
        return "ja", ja
    return "zh", ([" ".join(lines)] if lines else [])


def split_sentences(text: str):
    return [x.strip() for x in SENT.findall(text or "") if x.strip()]


def has_speakable(text: str) -> bool:
    return bool(SPEAKABLE.search(text or ""))


def split_for_translation(text: str):
    """切成送翻译的源句；不可发音的段（「……」）并回上一段（开头就是则并进下一段）。

    `……` 单独送翻译会得到空译文，整个单元就被判 `empty_translation` 失败、只剩字幕没声音
    （「实验做完了吗？……」实测踩到）。并回去时连同中间的终止标点一起带上，译文语气不丢。
    """
    out, pending = [], None
    for m in SENT.finditer(text or ""):
        s, e = m.span()
        if not m.group().strip():
            continue
        if has_speakable(m.group()):
            out.append([s if pending is None else pending, e])
            pending = None
        elif out:
            out[-1][1] = e
        elif pending is None:
            pending = s
    return [text[s:e].strip() for s, e in out]


# 半句保持「没说完」的语调（2026-10-25）：软切/力断/空闲冲刷出来的单元是句子的前半截，合成时若译文以「。」
# 「？」收尾，模型就按句子收尾的语调念完，下一个半句又从头起调。让译文以「、」结尾，念出来是延续的语调。
CONTINUE_TAIL = os.environ.get("ALBION_CONTINUE_TAIL", "1").lower() not in ("0", "false", "no", "off")
_CLOSERS = "」』）)】]\"'”’》〉 "
_FINAL_PUNCT = re.compile(r"[。．.？?！!]+$")


def continues(src: str, reason: str = "") -> bool:
    """这个单元的中文原文是不是「句子没说完」。有切分原因就信它（并排除自带句末标点的），否则只认逗号类结尾。"""
    t = (src or "").rstrip(_CLOSERS)
    if not t or t[-1] in "。！？!?…":
        return False
    if reason in ("soft", "force", "idle"):
        return True
    return t[-1] in "，、；：,;:"


def continue_tail(ja: str) -> str:
    """译文收尾改成「、」：句末的 。？！ 换成 、；已是 、，/ 省略号 / 波浪线 / 符号的保持；光秃秃的补一个 、。"""
    t = (ja or "").rstrip()
    if not t:
        return ja
    if t[-1] in "、，,":
        return t
    if _FINAL_PUNCT.search(t):
        return _FINAL_PUNCT.sub("、", t)
    if not t[-1].isalnum():          # …、〜、♡ 之类的收尾符号保持原样
        return t
    return t + "、"


# 不念的片段（2026-10-25）：URL、文件路径、代码残片。翻译模型遇到它们不翻译，而是自己编一句
# 「はい、指揮官。アルビオンに何かご用ですか？」念出去（实测 /product_id/RJ….html 就是这句）。
# 边界同时止于中日文字与全角标点：别把紧跟其后的「，」「了」一起吃掉；
# 前缀只排除 ASCII 词字符，粘在中文后面的路径（「看/a/b」）也能剥掉，而「和/或」这类不受影响。
_SEG = "[^\\s\\u3000-\\u303f\\u3040-\\u30ff\\u4e00-\\u9fff\\uff00-\\uffef]"
LINK = re.compile(
    r"(?:https?://|www\.)" + _SEG + r"*"                     # URL
    r"|[A-Za-z]:\\" + _SEG + r"*"                            # Windows 路径 X:\…
    r"|(?<![A-Za-z0-9_])~/" + _SEG + r"*"                    # ~/…
    r"|(?<![A-Za-z0-9_])/" + _SEG + r"+"                      # 以 / 开头（至少一个字符）
    r"|(?<![A-Za-z0-9_])" + _SEG + r"+(?:/" + _SEG + r"*){2,}",  # 含两个以上 / 的无空格片段
    re.I)
_ALLNUM = re.compile(r"[\d/]+$")     # 纯数字+斜杠（日期/比例/版本）不是路径，见 strip_links


def strip_links(text: str) -> str:
    """去掉不念的网址/路径/代码残片，空白规整；剩没剩可念的字由 `has_readable` 判。"""
    def repl(m):
        s = m.group(0)
        if _ALLNUM.match(s):          # 「2026/10/24」「1/2/3」：数字间的斜杠不是路径，照念
            return s
        return " "
    return " ".join(LINK.sub(repl, text or "").split())


def has_readable(text: str) -> bool:
    """还有汉字/假名可念（她的回复是中文：只剩拉丁字母的单元只可能是网址/路径/代码残片）。"""
    t = text or ""
    return bool(HAN.search(t) or KANA.search(t))


def aside_only(text: str) -> bool:
    """整段只是括号里的心声（防御：v2 理论上收不到，但契约要能明确回 no_audio(aside)）。"""
    t = (text or "").strip()
    return bool(t) and not has_speakable(ASIDE.sub("", t))


def count_by_order(results, order):
    """把乱序完成的结果按 unit 顺序取出（缺的不拿别人顶替）。"""
    return [results[u] for u in order if u in results]


# ---------------- 契约 ----------------
@dataclass(frozen=True)
class PrepareRequest:
    session_id: str
    generation: int
    unit_id: str
    voice: str
    source_range: tuple
    speech_text: str
    context_before: str = ""
    raw_text: str = ""               # 原始切片（含旁白/括号）；线索只在这里看得见
    # 规划版本（生产壳一定带）：**按版本**定跨句指令这类开关，桥侧环境变量不改写版本化请求
    plan_version: int = None
    cue_scope: bool = None           # 显式布尔（不带就按版本默认 / 构造参数 / 环境）
    cross_sentence: bool = None
    # 本 unit **之前**的原文字符串（壳按 unit.start 切）：跨 unit 的指令延续靠重放它得到，
    # 而不是桥持有会话状态 —— 只取决于文本，与 unit 取回顺序（翻译预取）无关。
    source_prefix: str = ""
    # 网关的切分原因（hard/soft/force/idle/finish）：判断这是不是「句子没说完」的半句；老调用方不带。
    unit_reason: str = ""


@dataclass(frozen=True)
class PrepareResult:
    unit_id: str
    status: str                      # ready | no_audio | failed
    source_unit_id: str = None
    ja_segments: tuple = ()
    reason: str = None               # no_audio: aside | symbols | empty
    stage: str = None                # failed: prepare | translation
    error: str = None
    translated: bool = False
    duration_ms: float = None

    def as_dict(self):
        return {"unit_id": self.unit_id, "status": self.status,
                "source_unit_id": self.source_unit_id,
                "ja_segments": [dict(s) for s in self.ja_segments],
                "reason": self.reason, "stage": self.stage, "error": self.error,
                "translated": self.translated, "duration_ms": self.duration_ms}


def _plan_version(v):
    """只认真整数（`True` 是 bool，不是版本号）。"""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parse_request(body: dict) -> PrepareRequest:
    return PrepareRequest(
        session_id=str(body["session_id"]), generation=int(body.get("generation", 0)),
        unit_id=str(body["unit_id"]), voice=str(body.get("voice", "albion")),
        source_range=tuple(body.get("source_range") or ()),
        speech_text=str(body.get("speech_text") or ""),
        context_before=str(body.get("context_before") or ""),
        raw_text=str(body.get("raw_text") or ""),
        plan_version=_plan_version(body.get("plan_version")),
        cue_scope=body.get("cue_scope") if isinstance(body.get("cue_scope"), bool) else None,
        cross_sentence=(body.get("cross_sentence")
                        if isinstance(body.get("cross_sentence"), bool) else None),
        source_prefix=str(body.get("source_prefix") or ""),
        unit_reason=str(body.get("unit_reason") or ""))


# ---------------- 服务 ----------------
class PrepareService:
    """全局最多 `max_in_flight` 个源句翻译在途；每会话最多 `session_window` 个单元有人等待。"""

    def __init__(self, translate, translate_ex=None, cues=None, *, max_in_flight=2, session_window=2,
                 cue_scope=None, cross_sentence=None):
        self._translate = translate                       # 同步可调用 str->str（旧桥 to_japanese 即可）
        # 可选：str -> [中文源线索]，从**原始切片**抽（旁白/括号在 speech_text 里已被剥离）
        self._cues = cues
        # cue 按源句归属（`ALBION_CUE_SCOPE`，默认关；显式布尔可覆盖环境）。
        # 关 ⇒ 本类行为逐字段等于加它之前那版（cue 只挂首段、标 unknown_first_segment）。
        self._cue_scope = cue_scope
        # 跨句指令（`ALBION_CROSS_SENTENCE`，v4/v5 时代默认关；v6 侧默认开）：
        # 需要 cue 归属先打开（身份是它的前提）；关 ⇒ 与加它之前逐字段相同。
        self._cross = cross_sentence
        self._scope_seq = 0                               # 无 source_range 时的单元号（只用于身份唯一）
        # 可选：str -> (日语, 情绪|None)。给了就用它（同一次调用顺带判情绪），
        # 没给/抛错都退回上面的老路径 —— 译文永远优先。
        self._translate_ex = translate_ex
        self._sem = asyncio.Semaphore(max_in_flight)
        self.max_in_flight = max_in_flight
        self.session_window = session_window
        self._held = {}                                   # session_id -> set((generation, unit_id))
        self._running = {}                                # (session_id, generation, unit_id) -> Task
        self._waiters = {}                                # 同一 unit 当前仍在等待的请求数
        self._in_flight = 0
        self.stats = {"prepares": 0, "ready": 0, "no_audio": 0, "failed": 0, "peak_in_flight": 0}

    async def prepare(self, req: PrepareRequest) -> PrepareResult:
        t0 = time.perf_counter()
        self.stats["prepares"] += 1
        mode, lines = pick_speech_slices(req.speech_text)      # ← 传原文：行结构是判据的一部分
        text = " ".join(lines)
        if not text:
            return self._done(self._no_audio(req, "empty", t0))
        if not has_speakable(text):
            return self._done(self._no_audio(req, "symbols", t0))
        if aside_only(text):
            return self._done(self._no_audio(req, "aside", t0))
        if not has_readable(strip_links(text)):
            # 纯网址/纯路径/纯代码/英文残片（2026-10-25）：念出来就是噪声，翻译腿还会给它们编一句寒暄。
            # 提前判 no_audio：不占翻译名额，也不至于走空译文重试再报 failed（字幕照常由壳显示）。
            return self._done(self._no_audio(req, "symbols", t0))

        if mode == "ja":
            return self._done(self._ready(req, lines, t0, translated=False))

        key = (req.session_id, req.generation, req.unit_id)
        running = self._running.get(key)
        held = self._held.setdefault(req.session_id, set())
        unit = (req.generation, req.unit_id)
        if unit not in held and len(held) >= self.session_window:
            return self._done(self._failed(req, "prepare", "session_window", t0))
        held.add(unit)
        if running is None:
            running = asyncio.ensure_future(self._translate_unit(req, t0))
            self._running[key] = running
            # 在途登记保留到翻译结束：同 ID 重试仍共享结果，全局线程限流也不提前释放。
            running.add_done_callback(lambda t: self._running.pop(key, None))
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return self._done(await asyncio.shield(running))
        finally:
            self._waiters[key] -= 1
            if not self._waiters[key]:
                self._waiters.pop(key)
                held.discard(unit)
                if not held:
                    self._held.pop(req.session_id, None)

    async def _translate_unit(self, req: PrepareRequest, t0) -> PrepareResult:
        """源句并行、全局限流、按源序组装。异常不取消工作线程或提前释放全局翻译名额。"""
        lines = pick_speech_slices(req.speech_text)[1]
        text = " ".join(lines)
        segs = split_for_translation(text)
        # 单句**送含标点的原文**（与旧桥 bridge.py:191-203 一致：问号/感叹号影响译文语气）；
        # 多句才逐句送（旧桥同样逐句，只是它会把空译文静默丢掉）。
        inputs = [text] if len(segs) <= 1 else segs
        async def tr(seg):
            async with self._sem:
                self._in_flight += 1
                self.stats["peak_in_flight"] = max(self.stats["peak_in_flight"], self._in_flight)
                try:
                    async def once():
                        if self._translate_ex is not None:
                            ja, emo = (await asyncio.to_thread(self._translate_ex, seg)) or ("", None)
                        else:
                            ja, emo = await asyncio.to_thread(self._translate, seg), None
                        return (ja or "").strip(), emo
                    ja, emo = await once()
                    if not ja:
                        ja, emo = await once()
                    return ja, emo
                finally:
                    self._in_flight -= 1
        results = await asyncio.gather(*(tr(seg) for seg in inputs), return_exceptions=True)
        error = next((r for r in results if isinstance(r, BaseException)), None)
        if error is not None:
            return self._failed(req, "translation", type(error).__name__, t0)
        outs, emos = zip(*results) if results else ((), ())
        if any(not o for o in outs):
            return self._failed(req, "translation", "empty_translation", t0)
        if CONTINUE_TAIL and continues(text, req.unit_reason):
            outs = (*outs[:-1], continue_tail(outs[-1]))      # 只动单元最后一段：前面的段是完整句子
        return self._ready(req, outs, t0, translated=True, emotions=emos)

    # ---- 内部 ----
    def _done(self, res: PrepareResult):
        self.stats[res.status] = self.stats.get(res.status, 0) + 1
        return res

    def _switches(self, req):
        """本请求的两个开关：① cue 按源句归属、② 跨句指令。返回 `(scoped, cross)`。

        **版本隔离**（Sol 第 13 轮第二条）：请求带了 `plan_version`（生产壳一定会带）时，两个开关
        都按**版本**定，与规划器 `scene_plan` 的口径逐条对齐 ——
          * cue 归属：v6 默认开、v4/v5 默认关，`ALBION_CUE_SCOPE` 在两版都能显式改（与
            规划器 `_cue_scope_on(body, default=v6)` 同口径：桥与规划器必须同一套判据，
            否则身份盖不上、规划器只能记 scope_unknown）；
          * 跨句指令：v6 才可能开 —— v4/v5 一律关，**桥侧环境变量不能把它打开**（规划器那边是
            `v6 and _cross_sentence_on(...)`，同理）。于是"把壳切回 v5 就逐字段回退"在任何桥侧
            环境变量下都成立。
        没带版本的旧调用（离线替身 / 直调）才回落到构造参数与环境，行为与加这段之前相同。
        """
        pv = _plan_version(getattr(req, "plan_version", None))
        explicit = getattr(req, "cue_scope", None)
        if isinstance(explicit, bool):
            scoped = explicit
        elif isinstance(self._cue_scope, bool):
            scoped = self._cue_scope
        else:
            scoped = cue_scope.enabled(None, default=(pv >= 6) if pv is not None else False)
        return scoped, self._cross_switch(getattr(req, "cross_sentence", None), pv)

    def _cross_switch(self, explicit, pv):
        """跨句指令的**最终**开关值（与规划器 `v6 and _cross_sentence_on(body)` 同口径）。

        **版本最优先**（Sol 第 14 轮第二条）：`pv < 6` 一律 False —— 在这之前返回，请求显式布尔、
        构造参数、环境变量都改不了它（旧实现先看显式值，`_cross_switch(True, 5) == True` 是个旁路）。
        带了版本且 ≥6 才轮到：显式值 > 构造参数 > 环境（`ALBION_CROSS_SENTENCE=0/false/off/no` 是关）。
        """
        if pv is not None and pv < 6:
            return False                     # v4/v5：跨句一律关
        if isinstance(explicit, bool):
            return explicit
        if isinstance(self._cross, bool):
            return self._cross
        if pv is None:                       # 旧调用（无版本）：看环境，行为不变
            return cue_scope.cross_enabled(None)
        return cue_scope.cross_enabled(None, default=True)    # v6：`ALBION_CROSS_SENTENCE=0` 可关

    def _ready(self, req, lines, t0, translated, emotions=None):
        src_text = req.raw_text or req.speech_text
        cues = self._cues(src_text) if self._cues else None
        raw_scoped, cross = self._switches(req)
        scoped = bool(raw_scoped) and cues is not None
        cross = scoped and bool(cross)                        # 身份是跨句的前提
        # 跨 unit 的指令延续：重放本 unit 之前的源文本（纯函数，与取回顺序无关）
        carried = cue_scope.active_directive(getattr(req, "source_prefix", "") or "") if cross else None
        rows, sdiag = (None, None)
        if scoped:            # 归属按**中文原文的 code point 跨度**做（别拿中文下标当日语下标）
            rows, sdiag = cue_scope.attribute(src_text, cues, len(lines),
                                              unit_key=self._unit_key(req), cross_sentence=cross,
                                              cross_in=carried)
        segs = []
        for i, t in enumerate(lines):
            seg = {"sub_index": i, "text": t}
            emo = emotions[i] if emotions and i < len(emotions) else None
            if scoped:
                mine = rows[i] if i < len(rows) else []
                emo = dict(emo or {})
                emo["src_scope"] = {"id": sdiag["ids"][i], "basis": sdiag["basis"],
                                    "sentence": (i if sdiag["ids"][i] else None),
                                    "unknown": len(sdiag["unknown"]), "quoted": sdiag["quoted"],
                                    "cross_sentence": sdiag["cross_sentence"]}
                if mine:
                    emo["src"] = mine      # 只带**本子段源句**的线索；判不出归属就不带
            elif cues and i == 0:
                # 现状（开关关时的旧行为，别再当成已解决）：cue 属于原文哪一句需要
                # "中文子句 ↔ 日文子段"的对齐，这里只保证**不向后广播**：cue 只挂首段，
                # 作用域标 unknown。若 cue 其实属于第二句，它会挂错段 —— 打开
                # `ALBION_CUE_SCOPE` 走上面那条按源句归属的路径。
                emo = dict(emo or {})
                emo["src"] = cues          # 线索跟着情绪一起走：退路（老翻译）时也不丢
                emo["src_scope"] = "unknown_first_segment"
            elif cues and i > 0:
                emo = dict(emo or {})
                emo["src_scope"] = "unknown_after_first_segment"
            if emo:
                seg["emotion"] = emo            # 可缺字段：缺了规划器就只用关键词表，不影响译文
            segs.append(seg)
        return PrepareResult(unit_id=req.unit_id, status="ready", source_unit_id=req.unit_id,
                             ja_segments=tuple(segs), translated=translated, duration_ms=_ms(t0))

    def _unit_key(self, req):
        """本单元的源范围（壳侧 `source_range`）——源句身份 `<key>#<句号>` 的前缀。

        有了它，"同一源句的两个子段"共享身份、"不同单元的第 0 句"不会撞号（否则重号会被
        规划器误判成"同一条指令的续片"）。壳没给范围（离线/直调）时退回本进程内的单元序号。
        """
        sr = getattr(req, "source_range", None) or ()
        if len(sr) == 2:
            return "%s-%s" % (sr[0], sr[1])
        self._scope_seq += 1
        return "call%d" % self._scope_seq

    def _no_audio(self, req, reason, t0):
        return PrepareResult(unit_id=req.unit_id, status="no_audio", reason=reason, duration_ms=_ms(t0))

    def _failed(self, req, stage, error, t0):
        return PrepareResult(unit_id=req.unit_id, status="failed", stage=stage, error=error,
                             duration_ms=_ms(t0))


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000.0, 2)


def make_bridge_translator():
    """复用旧桥翻译腿（模型/提示词/退路都不另选）；首次调用才 import，避免循环依赖。"""
    state = {}

    def translate(text: str) -> str:
        if "fn" not in state:
            d = os.environ.get("ALBION_BRIDGE_DIR", r"D:/AI/Wife/tts/irodori")
            if d not in sys.path:
                sys.path.insert(0, d)
            import importlib
            state["fn"] = importlib.import_module("bridge").to_japanese
        return state["fn"](text)

    return translate


def install(app, translate=None, translate_ex=None, cues=None, emotion_degraded=None, **kw):
    """把 v2 路由挂到**已有**的桥 app 上（方案要求不加第四个常驻服务）。

    生产落地只需在 `bridge.py` 末尾加一行：`bridge_v2.install(app, translate=to_japanese)`。
    **要显式传 translate**：bridge.py 支持以脚本方式启动，此时它的模块名是 `__main__`，
    默认的惰性 `import bridge` 会把同一个文件再导入一份（两套模块状态）。端口与旧路由都不动。
    `translate_ex` 可选：给了就用它（翻译同一次调用顺带出情绪结构）。
    `emotion_degraded` 可选：无参可调用，返回情绪翻译主路是否不可用；/v2/health 原样带出（不给就恒为 False）。
    """
    svc = PrepareService(translate or make_bridge_translator(), translate_ex, cues=cues, **kw)
    app.state.prepare_v2 = svc

    @app.post("/v2/prepare")
    async def v2_prepare(request: Request):
        try:
            body = await request.json()
            req = parse_request(body)
        except Exception as e:
            return JSONResponse({"status": "failed", "stage": "prepare",
                                 "error": "bad_request: %s" % str(e)[:120]}, status_code=400)
        pending = asyncio.create_task(svc.prepare(req))

        async def disconnected():
            # JSON body 已读完，此后 receive 只需等待断开事件，不轮询、不额外占超时窗口。
            while (await request.receive())["type"] != "http.disconnect":
                pass

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((pending, watcher), return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                return JSONResponse(pending.result().as_dict())
            return Response(status_code=499)
        finally:
            # 普通 HTTP handler 不会随客户端断开自动取消，主动结束等待才能归还会话名额。
            pending.cancel()
            watcher.cancel()
            await asyncio.gather(pending, watcher, return_exceptions=True)

    @app.get("/v2/health")
    async def v2_health():
        return {"ok": True, "limits": {"max_in_flight": svc.max_in_flight,
                                       "session_window": svc.session_window},
                "stats": svc.stats,
                "emotion_degraded": bool(emotion_degraded and emotion_degraded())}

    return svc


def create_app(translate=None, translate_ex=None, **kw):
    """沙箱自用：独立 app（离线测试与工具用）。生产路径是 install()。"""
    app = FastAPI(title="albion-tts-bridge-v2-sandbox")
    install(app, translate, translate_ex, **kw)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ALBION_V2_PORT", "19883")),
                log_level="warning", access_log=False)
