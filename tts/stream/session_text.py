# -*- coding: utf-8 -*-
"""会话解析与源文本账本（P1）。规格：方案 §3.1（D:/AI/codex/tools/_tmp/tts_stream_plan.md）。

纯逻辑：不联网、不加载模型、**不读时钟**（时间由调用方以 now 传入），可离线 property 测试。

两条不变量：
  ① 每个输入字符恰好属于一个显示 span（kind ∈ speech|aside|control），span 连续覆盖全文；
  ② speech_text 是**派生视图**（range 内 speech 类字符的连接），不是另一份文本 —— 所以不可能
    出现"忘了同步字幕"或"把心声念出来"这类漂移。

ponytail: 软切只按"字数窗口 + 标点"做，不做分词；短 token 保护只到"词串不长于 24 就整体前移"。
超长无边界串按 force 字数硬切（行为在 tests/test_session_text.py 里钉死）。
"""
from dataclasses import dataclass

ASIDE_OPEN = "（(【["
ASIDE_CLOSE = "）)】]"
ASIDE_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]"}
HARD = "。！？!?\n"
WEAK = "…"
SOFT = "，,；;：:"
# 硬边界之后允许跟尾的字符：终止标点、省略号、闭括号/闭引号 —— 它们归**同一个**单元
TERMINAL_TAIL = HARD + WEAK + "）)】]」』”\"'》〉"
CTRL_OPEN = "<|"
CTRL_CLOSE = "|>"


def is_word(ch: str) -> bool:
    return ch.isalnum() or "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"


def has_word(s: str) -> bool:
    return any(is_word(c) for c in s)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str          # speech | aside | control

    def as_tuple(self):
        return (self.start, self.end, self.kind)


@dataclass(frozen=True)
class Unit:
    unit_id: str
    seq: int
    start: int
    end: int
    speech_text: str     # 派生视图：可说的部分
    kind: str            # speech | no_audio
    reason: str          # hard|soft|force|idle|finish ; no_audio 时: aside|symbols|empty

    def as_dict(self):
        return {"unit_id": self.unit_id, "seq": self.seq, "source_range": [self.start, self.end],
                "speech_text": self.speech_text, "kind": self.kind, "reason": self.reason}


class SessionText:
    """一个会话（一个 turn）的解析状态。跨会话/新 turn 一律 new_turn() 重置，无进程级共享。"""

    def __init__(self, session_id: str, generation: int = 0, **kw):
        self.session_id = session_id
        self.generation = generation
        self.first_min = kw.get("first_min", 12)
        self.first_max = kw.get("first_max", 24)
        self.later_min = kw.get("later_min", 24)
        self.later_max = kw.get("later_max", 48)
        self.force = kw.get("force", 64)
        self.punct_window = kw.get("punct_window", 0.10)
        self.idle_window = kw.get("idle_window", 0.80)
        # 少切半句（2026-10-25）：只有开头 `soft_units` 个单元按上面的软切规则抢首音；之后的单元只在句末
        # 硬切（后面的单元在前一段播放期间准备，等一整句不影响听感），逗号半句单独合成会按"句子收尾"的
        # 语调念完、下一个半句又从头起调。累计到 `later_force` 个可说字符仍没句末，才在**最近的逗号**处切
        # （逗号落在 `later_soft_min` 个字之后才算，免得切出碎片），没逗号才硬切。
        self.soft_units = kw.get("soft_units", 1)
        self.later_force = kw.get("later_force", 64)
        self.later_soft_min = kw.get("later_soft_min", 12)
        self.later_idle_window = kw.get("later_idle_window", 2.0)   # LLM 句中停顿别把半句冲出去
        # 单元长度爬坡：下一个单元的渲染要在**前一个单元播放期间**完成。实测（2026-10-25）合成耗时约
        # 1.5s + 0.18×音频秒数，中文约 3 字/秒，所以第二个单元若是 50 字的整句（17s 音频，渲染 4.5s），
        # 而首单元只有 8 字（3s 音频），就会欠载 1.4s 的死空气。后续单元的 force 上限取
        # max(ramp_floor, ramp × 上一个有声单元的字数)，封顶 later_force；ramp=0 关闭爬坡。
        self.later_ramp = kw.get("later_ramp", 3.5)
        self.ramp_floor = kw.get("ramp_floor", 24)
        self.new_turn()

    # ---------- 生命周期 ----------
    def new_turn(self):
        """新 turn：状态全清（不依赖任何超时兜底）。"""
        self.text = ""
        self.kinds = []              # 逐字符 kind
        self.stack = []              # 心声 opener 下标栈（支持嵌套）
        self.ctrl = False            # 是否在 <|…|> 内
        self._ctrl_pending = False    # 尾部有一个孤立的 '<'（未完成多字符标记）
        self.committed_to = 0
        self._prev_len = 0
        self.seq = 0
        self.last_new_at = None
        self.hard_at = None          # 已命中的硬边界下标（等收尾窗口）
        self.hard_armed_at = None
        self.unclosed_aside = False
        self.cancelled = False
        self.units = []

    def cancel(self):
        """本 turn 作废：不再产出任何单元（协程侧无需再判断）。"""
        self.cancelled = True

    # ---------- 主入口 ----------
    def feed(self, piece: str, now: float):
        if self.cancelled or not piece:
            return []
        for ch in piece:
            self._push(ch)
        self.last_new_at = now
        return self._advance(now)

    def tick(self, now: float):
        """没有新文本时推进时间（收尾窗口 / 空闲冲刷）。"""
        if self.cancelled:
            return []
        return self._advance(now)

    def finish(self, now: float):
        if self.cancelled:
            return []
        out = self._advance(now, flush=True)
        if self.committed_to < len(self.text):
            out.append(self._commit(len(self.text), "finish"))
        if self.stack:
            self.unclosed_aside = True      # 保留原文、不补造闭括号、不朗读其内部
        self.last_new_at = None
        return out

    # ---------- 字符分类 ----------
    def _push(self, ch: str):
        i = len(self.text)
        self.text += ch
        if self.ctrl:
            self.kinds.append("control")
            if self.text.endswith(CTRL_CLOSE):
                self.ctrl = False
            return
        if self._ctrl_pending:
            if ch == "|":
                self.kinds[i - 1] = "control"      # 那个 '<' 升级成控制标记
                self.kinds.append("control")
                self.ctrl = True
                self._ctrl_pending = False
                return
            self._ctrl_pending = False             # 普通 '<'，照常参与文本
        if ch == "<":
            self.kinds.append("speech")
            self._ctrl_pending = True
            return
        if self.stack:
            self.kinds.append("aside")
            if ch in ASIDE_OPEN:
                self.stack.append(i)
            elif ch == ASIDE_PAIRS.get(self.text[self.stack[-1]]):
                self.stack.pop()          # 只有**匹配的**右括号才能关闭心声
            # 不匹配的右括号（如 （心声] ）留在心声内，不关闭它
            return
        if ch in ASIDE_OPEN:
            self.stack.append(i)
            self.kinds.append("aside")
            return
        if ch in ASIDE_CLOSE:
            # 无匹配 opener 的右括号：只做显示标点，**不朗读**（kind=punct 不进 speech_text）
            self.kinds.append("punct")
            return
        self.kinds.append("speech")

    # ---------- 边界判定 ----------
    def _hold_len(self):
        """尾部悬空的 '<' 不能进任何单元（它可能是 <| 的前缀）。"""
        return 1 if self._ctrl_pending else 0

    def _terminal_run_end(self, i: int):
        """硬边界之后紧跟的终止标点/闭引号归同一单元（孤立右括号 punct 也算，只补显示归属）。"""
        j = i + 1
        while j < len(self.text) and self.kinds[j] in ("speech", "punct") and self.text[j] in TERMINAL_TAIL:
            j += 1
        return j

    def _safe_cut(self, i: int) -> bool:
        """在 i 处切是否安全：不能把 aside/control/punct 的连续段从中间劈开。"""
        if i <= self.committed_to:
            return False
        if i >= len(self.text):
            return True
        a, b = self.kinds[i - 1], self.kinds[i]
        return not (a in ("aside", "control", "punct") and b == a)

    def _speakable_len(self, lo: int, limit: int):
        return sum(1 for i in range(lo, limit) if self.kinds[i] == "speech")

    def _next_candidate(self, limit: int):
        """最早的可提交切点 —— **因果**：只用已到达的文本，绝不看后面还会来什么。

        这一点是分块不变性的根：逐字喂和一次性喂必须得到同一结果，所以候选必须按位置从左到右
        取第一个合格的（软边界够长就立刻切，不等后面可能出现的硬边界）。
        返回 (切点下标, kind) 或 None。
        """
        lo = self.committed_to
        first = self.seq < self.soft_units
        min_len = self.first_min if first else self.later_min
        max_len = self.first_max if first else self.later_max
        force = self.force if first else self._later_force()
        speak = 0
        last_soft = None                    # 后续单元：最近一个可退守的逗号/空白切点
        for i in range(lo, limit):
            if self.kinds[i] == "speech":
                speak += 1
            cut = i + 1
            if not self._safe_cut(cut):
                continue
            ch, k = self.text[i], self.kinds[i]
            if k == "speech":
                if ch in HARD and has_word(self.speech_of_range(lo, cut)):
                    return (i, "hard")          # 硬边界：交给收尾窗口
                if first:
                    if ch in SOFT and speak >= min_len:
                        return (cut, "soft")    # 软边界：够长即切，不等后面的句子
                    if ch.isspace() and speak > max_len:
                        return (cut, "soft")
                elif (ch in SOFT or ch.isspace()) and speak >= self.later_soft_min:
                    last_soft = cut
            if speak >= force:
                if last_soft is not None:
                    return (last_soft, "soft")
                return (self._avoid_token_split(cut, limit), "force")
        return None

    def _later_force(self):
        """后续单元的 force 上限（含爬坡，见 __init__）。"""
        if self.later_ramp <= 0 or self._prev_len <= 0:
            return self.later_force
        return min(self.later_force, max(self.ramp_floor, int(self.later_ramp * self._prev_len)))

    def _avoid_token_split(self, cut: int, limit: int):
        """别把数字/URL 这类词串从中间掰开：只**往前**挪到词串起点（挪动幅度 ≤24）。

        只能往前（用切点之前的信息），不能往后看——否则又会引入"看未来"的分块不一致。
        """
        if not (self.committed_to < cut < len(self.text)):
            return cut
        prev, cur = self.text[cut - 1], self.text[cut]
        if not (is_word(prev) and (is_word(cur) or cur in ".:/%_-")):
            return cut
        s = cut - 1
        while s > self.committed_to and (is_word(self.text[s - 1]) or self.text[s - 1] in ".:/%_-"):
            s -= 1
        if s > self.committed_to and cut - s <= 24 and self._safe_cut(s):
            return s
        return cut                      # 超长词串只能硬切（行为在测试里钉死）

    def _has_speech(self, lo: int, limit: int):
        return any(self.kinds[i] == "speech" for i in range(lo, limit))

    # ---------- 推进 ----------
    def _advance(self, now: float, flush: bool = False):
        out = []
        while True:
            limit = len(self.text) - self._hold_len()
            if self.committed_to >= limit:
                break
            if self.hard_at is not None:
                end = self._terminal_run_end(self.hard_at)
                after = limit > end          # 收尾序列之后还有文本 → 立即提交
                if flush or after or now >= self.hard_armed_at + self.punct_window:
                    out.append(self._commit(end, "hard"))
                    continue
                break
            cand = self._next_candidate(limit)
            if cand is None:
                if flush:
                    break
                idle = self.idle_window if self.seq < self.soft_units else self.later_idle_window
                if (self.last_new_at is not None and now - self.last_new_at >= idle
                        and self._has_speech(self.committed_to, limit)):
                    out.append(self._commit(limit, "idle"))
                break
            idx, kind = cand
            if kind == "hard":
                self.hard_at = idx
                self.hard_armed_at = now
                continue
            out.append(self._commit(idx, kind))
        return out

    def _commit(self, end: int, reason: str):
        start, self.committed_to = self.committed_to, end
        self.hard_at = None
        self.hard_armed_at = None
        stext = self.speech_of_range(start, end)
        if not stext:
            kind, reason = "no_audio", "aside"
        elif not has_word(stext):
            kind, reason = "no_audio", "symbols"
        else:
            kind = "speech"
            self._prev_len = len(stext)
        u = Unit(unit_id="%s:%d:%d" % (self.session_id, self.generation, self.seq),
                 seq=self.seq, start=start, end=end, speech_text=stext, kind=kind, reason=reason)
        self.seq += 1
        self.units.append(u)
        return u

    # ---------- 派生视图 ----------
    def speech_of_range(self, start: int, end: int) -> str:
        return "".join(self.text[i] for i in range(start, end) if self.kinds[i] == "speech")

    def display_of_range(self, start: int, end: int) -> str:
        """显示视图：range 内的原文，**去掉 control 类字符**。

        `<|…|>` 是壳的表演元数据（ACT 情绪，2026-10-27 起壳会把它原样送进文字流），
        不是她说的原文 —— 字幕/聊天正文里不许出现 `<|ACT:{…}|>`。其余字符（心声、标点）
        原样保留：方案 §4.2「原消息始终完整」。没有 control 时与直接切片逐字节一致。
        """
        return "".join(self.text[i] for i in range(start, end) if self.kinds[i] != "control")

    def spans(self):
        """合并同 kind 的连续区间 —— 显示账本。"""
        out = []
        for i, k in enumerate(self.kinds):
            if out and out[-1][2] == k and out[-1][1] == i:
                out[-1] = (out[-1][0], i + 1, k)
            else:
                out.append((i, i + 1, k))
        return [Span(*s) for s in out]

    def ledger(self):
        return {"session_id": self.session_id, "generation": self.generation, "text": self.text,
                "spans": [s.as_tuple() for s in self.spans()],
                "unclosed_aside": self.unclosed_aside, "committed_to": self.committed_to,
                "cancelled": self.cancelled}
