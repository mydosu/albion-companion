"""她自己醒来（2026-10-14；2026-10-16 起节奏也由她定）：这里只守为他作息定的三条底线。

流程：
  她在任意一轮里调用 next_wake（agent/extensions/albion-self.ts）→ state/self.json 记下时间、想做的事、
  多久主动开口一次（speak_every_hours）；
  壳每 30 秒问一次 GET /albion/wake → 这里按 self.json + 底线算出到没到点；
  到点了壳就发一条 WAKE_MARKER（壳只是门铃，不带任何内容）；
  server.compose 认出门铃，换成 wake_prompt()：现在几点、他多久没说话、她上次给自己定的事、心事本；
  她这一轮自己决定看不看、说不说、说多少、做什么，想定下次就定（不定就按 DEFAULT_NEXT_S 后醒）。

三条底线（为他的作息，她改不了）：
  · 夜里安静时段（北京时间 QUIET_START～QUIET_END）不醒，顺延到早上
  · 他刚说过话（RECENT_USER_S 内）不醒——别打断正在进行的对话
  · 她主动说的话他连着没回：间隔按 2 的幂拉长（最多 BACKOFF_CAP 倍）；连着 SILENT_AFTER 次没回，至少隔 SILENT_GAP_S 才醒
另有两条是她自己的设定在这里落实：两次醒来至少隔 MIN_GAP_S（next_wake 的下限）；
主动开口按她定的 speak_every_hours（1～6 小时，默认 3）——没到点的那一轮只留表情，不出声也不显示心声。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WAKE_MARKER = "【自主醒来】"
TZ = ZoneInfo(os.environ.get("ALBION_TZ", "Asia/Shanghai"))   # WSL 是 America/Chicago，指挥官在北京时间
QUIET_START = (0, 30)
QUIET_END = (9, 0)
MIN_GAP_S = 10 * 60
DEFAULT_NEXT_S = 30 * 60
RECENT_USER_S = 5 * 60
SPEAK_H_MIN, SPEAK_H_MAX, SPEAK_H_DEFAULT = 1.0, 6.0, 3.0   # 2026-10-15 指挥官嫌每小时都出声太勤，3 小时是那时定的
BACKOFF_CAP = 8
SILENT_AFTER = 3
SILENT_GAP_S = 3 * 3600
RING_HOLD_S = 120          # 门铃响过之后这么久内不再响（等壳把那一轮发过来）

_lock = threading.Lock()


def _state_dir():
    return os.environ.get("ALBION_STATE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")


def _read(name, default):
    p = os.path.join(_state_dir(), name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 坏了不当成空的覆盖掉：原样改名留一份（haiku 复核 r33-6）
        try:
            os.replace(p, "%s.corrupt-%d" % (p, int(time.time() * 1000)))
        except OSError:
            pass
        return default


def _write(name, value):
    d = _state_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "." + name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(d, name))


def _read_obj(name):
    """读成 dict；合法 JSON 但不是对象（[]、null、5）也当坏了，改名留底（haiku 复核 r34-P6）。"""
    v = _read(name, {})
    if isinstance(v, dict):
        return v
    p = os.path.join(_state_dir(), name)
    try:
        os.replace(p, "%s.corrupt-%d" % (p, int(time.time() * 1000)))
    except OSError:
        pass
    return {}


def load():
    st = _read_obj("wake.json")
    st.setdefault("last_user_at", 0.0)
    st.setdefault("last_wake_at", 0.0)
    st.setdefault("spoken_at", [])
    st.setdefault("unanswered", 0)
    st.setdefault("rung_at", 0.0)
    return st


def save(st):
    _write("wake.json", st)


def self_plan():
    return _read_obj("self.json")


def _num(v, default=0.0):
    """状态文件里的数字字段：手改坏了（字符串、null）当默认值，不让门铃每 30 秒报错（haiku 复核 r35-3）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def speak_every_s(plan):
    """她定的主动开口间隔（秒）：1～6 小时，没定过是 3 小时。"""
    h = _num(plan.get("speak_every_hours"), SPEAK_H_DEFAULT) if isinstance(plan, dict) else SPEAK_H_DEFAULT
    return min(SPEAK_H_MAX, max(SPEAK_H_MIN, h)) * 3600


def _notes():
    v = _read("notebook.json", [])
    return [n for n in v if isinstance(n, dict)] if isinstance(v, list) else []


# ── 时间 ────────────────────────────────────────────────────────────────────
def in_quiet(ts):
    t = datetime.fromtimestamp(ts, TZ)
    hm = (t.hour, t.minute)
    if QUIET_START <= QUIET_END:
        return QUIET_START <= hm < QUIET_END
    return hm >= QUIET_START or hm < QUIET_END


def push_out_of_quiet(ts):
    """落在安静时段里就顺延到安静时段结束那一刻。"""
    if not in_quiet(ts):
        return ts
    t = datetime.fromtimestamp(ts, TZ)
    end = t.replace(hour=QUIET_END[0], minute=QUIET_END[1], second=0, microsecond=0)
    if end <= t:
        end += timedelta(days=1)
    return end.timestamp()


def clock(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M")


def ago(seconds):
    m = int(seconds // 60)
    if m < 1:
        return "不到一分钟"
    if m < 60:
        return "%d 分钟" % m
    h, m = divmod(m, 60)
    return "%d 小时%s" % (h, (" %d 分钟" % m) if m else "")


# ── 到点没 ──────────────────────────────────────────────────────────────────
def backoff(st):
    return min(2 ** int(_num(st.get("unanswered"))), BACKOFF_CAP)


def next_due(st, plan, now=None):
    """下次该醒的时刻（时间戳）。她定的时间优先，但不早于任何一条底线。"""
    now = time.time() if now is None else now
    last_wake = _num(st.get("last_wake_at"))
    last_user = _num(st.get("last_user_at"))
    anchor = max(last_wake, last_user) or now
    planned = _num(plan.get("wake_at")) if _num(plan.get("set_at")) >= last_wake else 0.0
    due = planned or (anchor + DEFAULT_NEXT_S * backoff(st))
    due = max(due, last_wake + MIN_GAP_S * backoff(st))
    due = max(due, last_user + RECENT_USER_S)
    if int(_num(st.get("unanswered"))) >= SILENT_AFTER:
        due = max(due, last_wake + SILENT_GAP_S)
    return push_out_of_quiet(due)


def ring(busy, now=None):
    """壳问门铃：到点且没在忙就响一次（RING_HOLD_S 内不重复响）。返回 (due, next_at)。"""
    now = time.time() if now is None else now
    with _lock:
        st = load()
        due_at = next_due(st, self_plan(), now)
        if busy or now < due_at or now - _num(st.get("rung_at")) < RING_HOLD_S:
            return False, due_at
        st["rung_at"] = now
        save(st)
        return True, due_at


# ── 一轮的前后 ──────────────────────────────────────────────────────────────
_TS_PREFIX = re.compile(r"^\[[^\]\n]{0,40}\]\s*")


def cancel_ring():
    """门铃响了但这一轮被他的话接走了：不算醒过，下次照常按底线算。"""
    with _lock:
        st = load()
        st["rung_at"] = 0.0
        save(st)


def is_wake(text):
    return _TS_PREFIX.sub("", (text or "").strip()).startswith(WAKE_MARKER)


# 壳替她发的系统消息（取图回传、旧的主动开口旁白）不算他说话；旧壳还在跑的过渡期也认得
_NOT_HIM = re.compile(r"^（(?:你要的【|这是我自己|这是一次自主开口|一次自主开口)")


def is_real_user(text):
    body = _TS_PREFIX.sub("", (text or "").strip())
    return bool(body) and not body.startswith(WAKE_MARKER) and not _NOT_HIM.match(body)


def note_user(now=None):
    with _lock:
        st = load()
        st["last_user_at"] = time.time() if now is None else now
        st["unanswered"] = 0
        save(st)


def spoken_recently(st, now, gap):
    v = st.get("spoken_at")
    return [t for t in (v if isinstance(v, list) else []) if isinstance(t, (int, float)) and now - t < gap]


def wake_prompt(now=None):
    """门铃换成的那段话：她醒来时自己知道的事，用她自己的口吻写；没有"你应该说什么"。"""
    now = time.time() if now is None else now
    with _lock:
        st = load()
        prev_wake = _num(st.get("last_wake_at"))
        st["last_wake_at"] = now
        st["rung_at"] = 0.0
        save(st)
    plan = self_plan()
    gap = speak_every_s(plan)
    notes = [n for n in _notes() if not n.get("done_at")]
    t = datetime.fromtimestamp(now, TZ)
    week = "一二三四五六日"[t.weekday()]
    # 这些是她醒来时"自己知道"的事，不是要念给他听的：标明了，她才不会把"40 分钟没说话"照着说出口（r31 实测）
    lines = [WAKE_MARKER + "我醒了。下面是醒来时我自己知道的事，写给自己看，不用念出来。",
             "现在是 %s（星期%s）。" % (t.strftime("%Y-%m-%d %H:%M"), week)]
    last_user = _num(st.get("last_user_at"))
    lines.append("指挥官上次跟我说话是 %s 前。" % ago(now - last_user) if last_user else "我还不知道指挥官上次什么时候跟我说过话。")
    if plan.get("intent") and _num(plan.get("set_at")) >= prev_wake:
        lines.append("上次我给自己定的事：%s" % plan["intent"])
    if notes:
        lines.append("心事本里还开着的：" + "；".join("#%s %s" % (n.get("id"), n.get("text")) for n in notes[:10]))
    un = int(_num(st.get("unanswered")))
    if un:
        lines.append("我主动说的话，他已经连着 %d 次没回了，可能不在电脑前。" % un)
    recent = spoken_recently(st, now, gap)
    hours = ("%g" % (gap / 3600))
    if recent:
        lines.append("按我给自己定的节奏（大约每 %s 小时主动开口一次），%s 以后再跟他说话；这次先不说——"
                     "可以看看他、记心事、做自己的事，回复只留一个表情。" % (hours, clock(max(recent) + gap)))
    else:
        lines.append("想跟他说话就说，像给他发一条消息：一两句，说他、说我想他，不是汇报——看了哪一路、玩到哪儿、记了什么都不用交代，"
                     "真有一件想跟他分享的小事就说那一件；没什么想说的，只留一个表情也行。")
    lines.append("想做什么都行：用 look 看看他（cam 是摄像头里真实的他）或屏幕，用 notebook 记心事，或者做点这台电脑里自己的事。"
                 "想好了就用 next_wake 定下次什么时候醒、醒来想做什么（也能改多久主动开口一次）；不定就半小时后再醒。")
    return "\n".join(lines)


_ACT = re.compile(r"<\|[^|]*\|>")
_ASIDE = re.compile(r"（[^（）]*）|\([^()]*\)")
_MARK = re.compile(r"\[\[[^\]\n]{0,80}\]\]")


def speech_of(reply):
    """说出口的部分（去掉 ACT、括号心声、[[…]] 标记）。括号可能嵌套，由内向外剥到不剩为止。"""
    t = _MARK.sub("", _ACT.sub("", reply or ""))
    while True:
        u = _ASIDE.sub("", t)
        if u == t:
            return t.strip()
        t = u


def after_wake(reply, now=None):
    """醒来那一轮的回复：离她上次主动开口还没到她定的间隔，就只留表情；说出口了就记账。返回最终回复。
    （英文拒绝稿已由整理器 albion-guard.ts 换成表情，这里不再重复判定。）"""
    now = time.time() if now is None else now
    gap = speak_every_s(self_plan())
    with _lock:
        st = load()
        spoken = spoken_recently(st, now, gap)
        if spoken:
            # 没到能开口的时候：心声也会冒成气泡，一样算"主动消息"，只留表情（2026-10-15）
            acts = _ACT.findall(reply or "")
            reply = acts[0] if acts else '<|ACT:{"emotion":"neutral"}|>'
        elif speech_of(reply):
            spoken.append(now)
            st["unanswered"] = int(_num(st.get("unanswered"))) + 1
        st["spoken_at"] = spoken
        save(st)
    return reply


def _selftest():
    import tempfile
    os.environ["ALBION_STATE_DIR"] = tempfile.mkdtemp(prefix="wake-test-")
    noon = datetime(2026, 10, 14, 12, 0, tzinfo=TZ).timestamp()
    night = datetime(2026, 10, 14, 2, 0, tzinfo=TZ).timestamp()
    assert not in_quiet(noon) and in_quiet(night)
    assert datetime.fromtimestamp(push_out_of_quiet(night), TZ).hour == 9
    st = load()
    # 没定时间：半小时后
    st.update(last_user_at=noon, last_wake_at=0.0)
    assert next_due(st, {}, noon) == noon + DEFAULT_NEXT_S
    # 她定了 15 分钟后：照她的
    assert next_due(st, {"wake_at": noon + 900, "set_at": noon}, noon) == noon + 900
    # 她定了 1 分钟后：不早于"他刚说过话"的 5 分钟
    assert next_due(st, {"wake_at": noon + 60, "set_at": noon}, noon) == noon + RECENT_USER_S
    # 刚醒过：至少隔 MIN_GAP_S；连着没回：按倍数拉长
    st.update(last_user_at=0.0, last_wake_at=noon, unanswered=2)
    assert next_due(st, {"wake_at": noon + 60, "set_at": noon}, noon) == noon + MIN_GAP_S * 4
    st["unanswered"] = 3
    assert next_due(st, {}, noon) >= noon + SILENT_GAP_S
    # 落在夜里：顺延到 9 点
    st.update(unanswered=0, last_wake_at=night - 3600)
    assert datetime.fromtimestamp(next_due(st, {"wake_at": night, "set_at": night}, night), TZ).hour == 9
    # 说出口的判定
    assert speech_of('<|ACT:{"emotion":"happy"}|> （想他……）') == ""
    assert speech_of('<|ACT:{"emotion":"happy"}|> 指挥官，在忙吗？') == "指挥官，在忙吗？"
    assert speech_of("（a（b）c）") == "" and speech_of("嗯（a（b）c）好") == "嗯好"
    # 开口节奏由她定：没定过 3 小时；定了照她的（夹在 1～6 小时）；坏值当没定
    assert speak_every_s({}) == 3 * 3600 and speak_every_s({"speak_every_hours": 2}) == 2 * 3600
    assert speak_every_s({"speak_every_hours": 0.2}) == 3600 and speak_every_s({"speak_every_hours": 99}) == 6 * 3600
    assert speak_every_s({"speak_every_hours": "坏"}) == 3 * 3600
    # 默认 3 小时：2 小时前开过口，这一轮只剩一个表情（心声也不留）
    save({"spoken_at": [noon - 2 * 3600], "unanswered": 0})
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官！（想他（很久）了）', noon)
    assert out == '<|ACT:{"emotion":"happy"}|>', out
    assert "这次先不说" in wake_prompt(noon) and "大约每 3 小时" in wake_prompt(noon)
    assert load()["spoken_at"] == [noon - 2 * 3600]
    # 她把节奏改成 1 小时：同样 2 小时前开过口，这次就能说
    _write("self.json", {"speak_every_hours": 1})
    assert "像给他发一条消息" in wake_prompt(noon) and "50 字" not in wake_prompt(noon)
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官，下午好。', noon)
    assert speech_of(out) == "指挥官，下午好。", out
    _write("self.json", {})
    save({"spoken_at": [noon - 3 * 3600 - 60], "unanswered": 0})
    assert "像给他发一条消息" in wake_prompt(noon)
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官，早。', noon)
    assert speech_of(out) == "指挥官，早。" and load()["spoken_at"] == [noon], load()
    save({"spoken_at": [noon - 60], "unanswered": 0})
    assert after_wake("（只有心声）", noon) == '<|ACT:{"emotion":"neutral"}|>'
    # 坏的状态文件：改名留底，不覆盖
    with open(os.path.join(_state_dir(), "wake.json"), "w", encoding="utf-8") as f:
        f.write("{坏")
    assert load()["unanswered"] == 0 and any(n.startswith("wake.json.corrupt-") for n in os.listdir(_state_dir()))
    save({"spoken_at": [noon - 60, noon - 120, noon - 180], "unanswered": 0})
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官！（好想他）', noon)
    assert out == '<|ACT:{"emotion":"happy"}|>', out
    save({"spoken_at": [], "unanswered": 0})
    after_wake('<|ACT:{"emotion":"happy"}|> 指挥官，在吗？', noon)
    assert load()["unanswered"] == 1
    note_user(noon + 10)
    assert load()["unanswered"] == 0
    # 门铃：到点响一次，RING_HOLD_S 内不重复；忙时不响
    save({"last_user_at": noon - 3600, "last_wake_at": noon - 3600})
    assert ring(False, noon)[0] is True and ring(False, noon + 5)[0] is False
    save({"last_user_at": noon - 3600, "last_wake_at": noon - 3600})
    assert ring(True, noon)[0] is False
    # 门铃识别、他说的话识别
    assert is_wake("[2026-10-14 12:00] " + WAKE_MARKER) and not is_wake("在吗")
    assert is_real_user("[2026-10-14 12:00] 在吗") and not is_real_user("（你要的【摄像头】画面取来了…）")
    assert not is_real_user(WAKE_MARKER)
    # 非对象 JSON 也改名留底
    with open(os.path.join(_state_dir(), "wake.json"), "w", encoding="utf-8") as f:
        f.write("[]")
    assert isinstance(load(), dict)
    with open(os.path.join(_state_dir(), "self.json"), "w", encoding="utf-8") as f:
        f.write("5")
    assert self_plan() == {} and speak_every_s(self_plan()) == 3 * 3600
    # 字段坏了不炸：门铃照常算，醒来提示照常拼
    save({"last_wake_at": "坏", "spoken_at": "x", "unanswered": None})
    _write("self.json", {"wake_at": "坏", "set_at": "x", "speak_every_hours": None})
    _write("notebook.json", {"不是": "列表"})
    ring(False, noon)
    assert wake_prompt(noon).startswith(WAKE_MARKER)
    p = wake_prompt(noon)
    assert "next_wake" in p and "look" in p
    # 醒来提示里不再有「规矩/底线/余量」这类像在下指令的说法（r35：醒来轮的英文拒绝来自这类措辞）
    assert not any(w in p for w in ("底线", "余量", "规矩", "以下只给我自己看"))
    print("WAKE SELFTEST OK")


if __name__ == "__main__":
    _selftest()
