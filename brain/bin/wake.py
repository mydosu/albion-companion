"""她自己醒来（2026-10-14；2026-10-16 起节奏也由她定；2026-11-02 起回归一条门铃）。

一条计时（state/self.json 里 wake_at/intent/set_at + 开口节奏 speak_every_hours）：
  · 下次醒来：她定的优先，夹在 30 分钟～3 小时（没定过 60 分钟），再守下面的底线；
  · 每次醒来做什么由她自己选：找他、做自己的事，或者都做——门铃不分种类，也不挡她看谁。
  旧的「说话轮 / 生活轮」两条计时（self.json 里 speak / life 各一份）读的时候合成一条：
  取 wake_at 更早的那条，intent 两条都有就用「；」连起来，set_at 取较大值。

流程：
  她在任意一轮里调用 next_wake（agent/extensions/albion-self.ts）→ state/self.json 记下时间、想做的事、
  多久主动开口一次（speak_every_hours）；做了自己的事可以用 did 记一句，进 state/life.json；
  壳每 30 秒问一次 GET /albion/wake → 这里按 self.json + 底线算出到没到点；
  到点了壳就发一条 WAKE_MARKER（壳只是门铃，不带任何内容）；
  server.compose 认出门铃，换成 wake_prompt()：现在几点、他多久没说话、她上次给自己定的事、心事本；
  她这一轮自己决定看不看、说不说、说多少、做什么，想定下次就定（不定就 60 分钟后醒）。

三条底线（为他的作息，她改不了）：
  · 夜里安静时段（北京时间 QUIET_START～QUIET_END）不醒，顺延到早上
  · 他刚说过话（RECENT_USER_S 内）不醒——别打断正在进行的对话
  · 两次醒来至少隔 MIN_GAP_S（next_wake 的下限）

能不能开口由 can_speak 决定（只影响这一轮说不说话，不推迟醒来）：
  · 离她上次主动开口不到她定的 speak_every_hours（1～6 小时，默认 3）——按节奏等；
  · 离上次开口不到 MIN_GAP_S * backoff、或连着 SILENT_AFTER 次没回且不到 SILENT_GAP_S——他在忙/可能不在，先不打扰。
  不能开口的那一轮 after_wake 只留第一个表情，不出声也不显示心声。
"""
from __future__ import annotations

import inbox
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WAKE_MARKER = "【自主醒来】"
# 桥（bin/chat_bridge.py）叫醒她时用的标记：那是「他给我发了消息」的通知，不是他这一轮说的话（2026-11-02）
PLATFORM_MARKER = "【平台消息】"
TZ = ZoneInfo(os.environ.get("ALBION_TZ", "Asia/Shanghai"))   # WSL 是 America/Chicago，指挥官在北京时间
QUIET_START = (0, 30)
QUIET_END = (9, 0)
MIN_GAP_S = 10 * 60
RECENT_USER_S = 5 * 60
WAKE_MIN_S, WAKE_MAX_S, WAKE_DEFAULT_S = 30 * 60, 180 * 60, 60 * 60   # 她定的一轮：30 分钟～3 小时，没定过 1 小时
LIFE_KEEP = 20            # state/life.json 只留最近 20 条
ROUNDS_KEEP = 10          # wake.json 的 rounds 只留最近 10 轮
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
    st.setdefault("rounds", [])
    return st


def save(st):
    _write("wake.json", st)


def self_plan():
    return _read_obj("self.json")


def merged_plan(plan):
    """self.json 摊平成一条计时。

    单条格式 {"wake_at","intent","set_at","speak_every_hours"} 照用；
    旧的两线格式 {"speak": {...}, "life": {...}} 取 wake_at 更早的那条，intent 两条都有就用「；」连起来，
    set_at 取较大值；最早的顶层旧格式照旧。
    """
    if not isinstance(plan, dict):
        return {}
    lanes = [v for k in ("speak", "life") for v in (plan.get(k),) if isinstance(v, dict) and _num(v.get("wake_at"))]
    if not lanes:
        return {k: plan.get(k) for k in ("wake_at", "intent", "set_at", "speak_every_hours") if k in plan}
    lanes.sort(key=lambda x: _num(x.get("wake_at")))
    intents = [str(v.get("intent")).strip() for v in lanes if str(v.get("intent") or "").strip()]
    out = {
        "wake_at": _num(lanes[0].get("wake_at")),
        "intent": "；".join(intents),
        "set_at": max(_num(v.get("set_at")) for v in lanes),
    }
    if "speak_every_hours" in plan:
        out["speak_every_hours"] = plan["speak_every_hours"]
    return out


def life_items():
    """state/life.json：她自己做过的事（最新的在后）；只留最近 LIFE_KEEP 条，坏条目顺手清掉。"""
    v = _read("life.json", [])
    if not isinstance(v, list):
        return []
    items = [x for x in v if isinstance(x, dict) and str(x.get("did") or "").strip()]
    items = items[-LIFE_KEEP:]
    if len(items) != len(v):
        try:
            _write("life.json", items)
        except OSError:
            pass
    return items


def _life_item(x, now=None):
    """生活记录一行：「今天/昨天/N天前 做了什么」。"""
    at = _num(x.get("at"))
    if at:
        d = datetime.fromtimestamp(at, TZ).date()
    else:
        d = datetime.fromtimestamp(now, TZ).date() if now else datetime.now(TZ).date()
    return "%s %s" % (_days_ago_label(d, now), x.get("did") or "")


def clamp_wake_s(seconds):
    """醒来间隔夹在 30 分钟～3 小时（next_wake 那边已经夹过一次，手改坏的 self.json 这里再兜一层）。"""
    return min(WAKE_MAX_S, max(WAKE_MIN_S, _num(seconds, WAKE_DEFAULT_S)))


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


def _days_ago_label(d, now=None):
    """今天 / 昨天 / N天前（按北京时间的日期算，不按小时）。"""
    today = datetime.fromtimestamp(now, TZ).date() if now else datetime.now(TZ).date()
    n = (today - d).days
    if n <= 0:
        return "今天"
    if n == 1:
        return "昨天"
    return "%d天前" % n


def _note_item(n, now=None):
    """心事本一行：#编号（月-日 记，几天前）内容；条目没记时间就省掉括号。

    2026-10-29 起带日期和“几天前”：不然她自己分不清哪条是几天前记的，一条旧事会被反复当成"最近的事"问。
    """
    body = "%s" % (n.get("text") or "")
    at = _num(n.get("at"))
    if not at:
        return "#%s %s" % (n.get("id"), body)
    dt = datetime.fromtimestamp(at, TZ)
    return "#%s（%s 记，%s）%s" % (n.get("id"), dt.strftime("%m-%d"), _days_ago_label(dt.date(), now), body)


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
    """下次该醒的时刻（时间戳）。她定的时间优先（夹在 30 分钟～3 小时），但不早于任何一条底线。"""
    now = time.time() if now is None else now
    last_wake = _num(st.get("last_wake_at"))
    last_user = _num(st.get("last_user_at"))
    anchor = max(last_wake, last_user) or now
    p = merged_plan(plan)
    set_at = _num(p.get("set_at"))
    planned = 0.0
    if set_at >= last_wake and _num(p.get("wake_at")):
        planned = set_at + clamp_wake_s(_num(p.get("wake_at")) - set_at)
    due = planned or (anchor + WAKE_DEFAULT_S)
    due = max(due, last_wake + MIN_GAP_S)
    due = max(due, last_user + RECENT_USER_S)
    return push_out_of_quiet(due)


def can_speak(st, plan, now=None):
    """这一轮能不能开口：(能不能, 不能开口时写给她的原因)。原因不推迟醒来，只决定这一轮说不说话。"""
    now = time.time() if now is None else now
    spoken = [t for t in (st.get("spoken_at") or []) if isinstance(t, (int, float))]
    if not spoken:
        return True, ""
    last = max(spoken)
    since = now - last
    gap = speak_every_s(plan)
    if since < gap:
        hours = "%g" % (gap / 3600)
        return False, "按我给自己定的节奏（大约每 %s 小时主动开口一次），%s 以后再找他" % (hours, clock(last + gap))
    n = int(_num(st.get("unanswered")))
    quiet = "我主动说的话他已经连着 %d 次没回了，可能不在，先不打扰他" % n
    if since < MIN_GAP_S * backoff(st):
        return False, quiet
    if n >= SILENT_AFTER and since < SILENT_GAP_S:
        return False, quiet
    return True, ""


def ring(busy, now=None):
    """壳问门铃：到点且没在忙就响一次（RING_HOLD_S 内不重复响）。返回 (响没响, 下次该醒的时刻)。"""
    now = time.time() if now is None else now
    with _lock:
        st = load()
        due = next_due(st, self_plan(), now)
        if busy or now < due or now - _num(st.get("rung_at")) < RING_HOLD_S:
            return False, due
        st["rung_at"] = now
        save(st)
        return True, due


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


def is_platform(text):
    """桥叫醒她时用的通知（【平台消息】）：不是他这一轮说的话，也不算她醒来。"""
    return _TS_PREFIX.sub("", (text or "").strip()).startswith(PLATFORM_MARKER)


def is_real_user(text):
    body = _TS_PREFIX.sub("", (text or "").strip())
    return (bool(body) and not body.startswith(WAKE_MARKER)
            and not body.startswith(PLATFORM_MARKER) and not _NOT_HIM.match(body))


def note_user(now=None):
    with _lock:
        st = load()
        st["last_user_at"] = time.time() if now is None else now
        st["unanswered"] = 0
        save(st)


SPOKEN_DEDUPE_S = 10 * 60      # 桥主动开口：#7 定稿——10 分钟内已经记过就不重复 +1


def note_spoken(now=None):
    """她在桥那边主动找他（不是回复）记一次：和桌面说出口共用 spoken_at / unanswered 这一套。

    10 分钟内已经记过就不重复记（她那一下可能连发两条）。返回 True = 这次真记上了。
    """
    now = time.time() if now is None else now
    with _lock:
        st = load()
        plan = self_plan()
        keep = max(speak_every_s(plan), SILENT_GAP_S)
        spoken = [t for t in (st.get("spoken_at") or [])
                  if isinstance(t, (int, float)) and now - t < keep]
        if spoken and now - max(spoken) < SPOKEN_DEDUPE_S:
            st["spoken_at"] = spoken
            save(st)
            return False
        spoken.append(now)
        st["spoken_at"] = spoken
        st["unanswered"] = int(_num(st.get("unanswered"))) + 1
        save(st)
        return True


def _inbox_line(item):
    """未回消息一行：#编号 月-日 时:分 渠道：原文。"""
    at = _num(item.get("at"))
    when = datetime.fromtimestamp(at, TZ).strftime("%m-%d %H:%M") if at else ""
    return "#%s %s %s：%s" % (item.get("id"), when, inbox.channel_label(item.get("channel")),
                              item.get("text"))


def wake_prompt(now=None):
    """门铃换成的那段话：她醒来时自己知道的事，用她自己的口吻写；没有"你应该说什么"。"""
    now = time.time() if now is None else now
    # 未回箱：他发来的、还没了结的（群里的上下文条目不算）。这一轮要念给她，结束后由 after_wake 了结。
    unseen = [i for i in inbox.pending() if i.get("from") == "owner"]
    seen = unseen[:10]                      # 一次最多念 10 条，多出来的留到下次
    with _lock:
        st = load()
        prev_wake = _num(st.get("last_wake_at"))
        st["last_wake_at"] = now
        st["rung_at"] = 0.0
        st["inbox_shown"] = [int(i["id"]) for i in seen]
        save(st)
    plan = merged_plan(self_plan())
    t = datetime.fromtimestamp(now, TZ)
    week = "一二三四五六日"[t.weekday()]
    # 这些是她醒来时"自己知道"的事，不是要念给他听的：标明了，她才不会把"40 分钟没说话"照着说出口（r31 实测）
    lines = [WAKE_MARKER + "我醒了。下面是醒来时我自己知道的事，写给自己看，不用念出来。",
             "现在是 %s（星期%s）。" % (t.strftime("%Y-%m-%d %H:%M"), week)]
    if plan.get("intent") and _num(plan.get("set_at")) >= prev_wake:
        lines.append("上次我给自己定的事：%s" % plan["intent"])
    recent_life = life_items()
    if recent_life:
        lines.append("最近我自己做过的事：" + "；".join(_life_item(x, now) for x in recent_life[-3:]))
    rounds = [r for r in (st.get("rounds") or []) if isinstance(r, dict)]
    if len(rounds) >= 3 and all(r.get("self") is False for r in rounds[-3:]):
        last_at = _num(recent_life[-1].get("at")) if recent_life else 0.0
        tail = "自己的事已经%s没碰了。" % ago(now - last_at) if last_at else "自己的事一直没碰过。"
        lines.append("最近几次醒来都只顾着他，" + tail)
    lines.append("这台电脑里我自己能做的：接着玩没玩完的游戏（adventure）、画画（draw）、"
                 "上网看看感兴趣的东西（browser）、在 /root/家 里读书、写点什么、整理画廊。")
    last_user = _num(st.get("last_user_at"))
    lines.append("指挥官上次跟我说话是 %s 前。" % ago(now - last_user) if last_user else "我还不知道指挥官上次什么时候跟我说过话。")
    notes = [n for n in _notes() if not n.get("done_at")]
    if notes:
        lines.append("心事本里还开着的（我自己记的，可能记错；等合适的时候再问，问过一次就划掉）："
                     + "；".join(_note_item(n, now) for n in notes[:10]))
    # 未回箱（2026-11-02）：他发来的、还没回的消息。这一轮了结：回就用 send_message，不回也算处理完。
    if seen:
        lines.append("还没回的消息（他发来的）：" + "；".join(_inbox_line(i) for i in seen))
        lines.append("这一轮把它们了结：想回就用 send_message 回；不回也行，这轮过后就不再提醒。")
    ok, reason = can_speak(st, self_plan(), now)
    if ok:
        lines.append("想他了就去找他：可以先用 look 看看他（cam 是摄像头里真实的他）或屏幕；"
                     "想说就说一两句，像给他发一条消息，说他、说我想他，不是汇报；真有一件想分享的小事就说那一件。")
    else:
        lines.append("%s；这次不跟他说话，回复只留一个表情。" % reason)
    lines.append("这一轮做什么我自己选：找他、做自己的事，或者都做；什么都不想做就安静待一会儿。"
                 "做完用 next_wake 定下次什么时候醒（30 分钟到 3 小时）、醒来想做什么；做了自己的事就用 did 记一句做了什么。"
                 "不定就一小时后再醒。")
    # 悄悄话（2026-11-02）：两个人都能主动找对方。桥是否启用了悄悄话 wake.py 看不到（另一个进程），
    # 所以这句无条件给；频道没启用时 send_message 会把「频道没找到」交回给她，不会说错话。
    lines.append("想找可畏说说话，也可以用 send_message（channel=discord_whisper）去悄悄话找她。")
    # 11-03 指挥官定：醒来轮里用 send_message 回过他，最后又在桌面把同一番话说一遍，他看了两遍。
    lines.append("这一轮最后写的话会显示在桌面上、念给他听；用 send_message 跟他说过的，桌面上就不再说一遍，"
                 "只说消息里没说过的，没有就只留一个表情。")
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
    """醒来那一轮的回复：不能开口时只留第一个表情（不出声、不显示心声）；说出口了就记 spoken_at、unanswered+1。

    同时记一条平衡记录 st["rounds"]：这一轮醒来时刻、这轮有没有新的生活记录、有没有说出口。
    （英文拒绝稿已由整理器 albion-guard.ts 换成表情，这里不再重复判定。）"""
    now = time.time() if now is None else now
    with _lock:
        st = load()
        wake_at = _num(st.get("last_wake_at")) or now
        plan = self_plan()
        ok, _ = can_speak(st, plan, now)
        spoke = False
        if not ok:
            # 没到能开口的时候：心声也会冒成气泡，一样算"主动消息"，只留表情（2026-10-15）
            acts = _ACT.findall(reply or "")
            reply = acts[0] if acts else '<|ACT:{"emotion":"neutral"}|>'
        elif speech_of(reply):
            spoke = True
            # 只留还用得上的开口记录（节奏 / 连着没回那两道），别让 wake.json 越写越长
            keep = max(speak_every_s(plan), SILENT_GAP_S)
            spoken = [t for t in (st.get("spoken_at") or [])
                      if isinstance(t, (int, float)) and now - t < keep]
            spoken.append(now)
            st["spoken_at"] = spoken
            st["unanswered"] = int(_num(st.get("unanswered"))) + 1
        has_self = any(_num(x.get("at")) >= wake_at for x in life_items())
        rounds = [r for r in (st.get("rounds") or []) if isinstance(r, dict)][-(ROUNDS_KEEP - 1):]
        rounds.append({"at": wake_at, "self": has_self, "spoke": spoke})
        st["rounds"] = rounds[-ROUNDS_KEEP:]
        # 这一轮醒来提示里念过的未回消息，到这里了结：回过的已经是 answered，没回的就是她决定不回（2026-11-02）
        shown = [int(x) for x in (st.get("inbox_shown") or []) if isinstance(x, (int, float))]
        st["inbox_shown"] = []
        save(st)
    if shown:
        try:
            inbox.close(shown, now)
        except OSError:
            pass
    return reply


def _selftest():
    import tempfile
    os.environ["ALBION_STATE_DIR"] = tempfile.mkdtemp(prefix="wake-test-")
    noon = datetime(2026, 10, 14, 12, 0, tzinfo=TZ).timestamp()
    night = datetime(2026, 10, 14, 2, 0, tzinfo=TZ).timestamp()
    assert not in_quiet(noon) and in_quiet(night)
    assert datetime.fromtimestamp(push_out_of_quiet(night), TZ).hour == 9
    st = load()
    # 没定时间：上次醒来/他上次说话起 60 分钟
    st.update(last_user_at=noon, last_wake_at=0.0)
    assert next_due(st, {}, noon) == noon + WAKE_DEFAULT_S
    # 她定了 15 分钟后：夹到 30 分钟；定了 5 小时后：夹到 3 小时
    assert next_due(st, {"wake_at": noon + 900, "set_at": noon}, noon) == noon + WAKE_MIN_S
    assert next_due(st, {"wake_at": noon + 5 * 3600, "set_at": noon}, noon) == noon + WAKE_MAX_S
    # 她定了 1 分钟后：不早于"他刚说过话"的 5 分钟，也不早于 30 分钟
    assert next_due(st, {"wake_at": noon + 60, "set_at": noon}, noon) == noon + WAKE_MIN_S
    # 刚醒过：至少隔 MIN_GAP_S
    st.update(last_user_at=0.0, last_wake_at=noon, unanswered=2)
    assert next_due(st, {"wake_at": noon + 60, "set_at": noon}, noon) == noon + WAKE_MIN_S
    # 连着没回不再推迟醒来：定了 1 小时就 1 小时后
    st["unanswered"] = 3
    assert next_due(st, {"wake_at": noon + 3600, "set_at": noon}, noon) == noon + 3600
    assert next_due(st, {}, noon) == noon + WAKE_DEFAULT_S
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
    # 默认 3 小时：2 小时前开过口，这一轮不能开口（只有表情），醒来照常
    save({"spoken_at": [noon - 2 * 3600], "unanswered": 0, "last_wake_at": noon})
    ok, why = can_speak(load(), {}, noon)
    assert ok is False and "大约每 3 小时" in why and "以后再找他" in why, why
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官！（想他（很久）了）', noon)
    assert out == '<|ACT:{"emotion":"happy"}|>', out
    assert load()["spoken_at"] == [noon - 2 * 3600]
    p = wake_prompt(noon)
    assert "大约每 3 小时" in p and "这次不跟他说话，回复只留一个表情" in p
    assert "想他了就去找他" not in p
    # 她把节奏改成 1 小时：同样 2 小时前开过口，这次就能说
    _write("self.json", {"speak_every_hours": 1})
    save({"spoken_at": [noon - 2 * 3600], "unanswered": 0, "last_wake_at": noon})
    assert can_speak(load(), self_plan(), noon)[0] is True
    assert "想他了就去找他" in wake_prompt(noon) and "50 字" not in wake_prompt(noon)
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官，下午好。', noon)
    assert speech_of(out) == "指挥官，下午好。", out
    assert load()["spoken_at"] == [noon - 2 * 3600, noon] and load()["unanswered"] == 1
    _write("self.json", {})
    save({"spoken_at": [noon - 3 * 3600 - 60], "unanswered": 0, "last_wake_at": noon})
    assert "想他了就去找他" in wake_prompt(noon)
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官，早。', noon)
    assert speech_of(out) == "指挥官，早。" and load()["spoken_at"] == [noon], load()
    save({"spoken_at": [noon - 60], "unanswered": 0, "last_wake_at": noon})
    assert after_wake("（只有心声）", noon) == '<|ACT:{"emotion":"neutral"}|>'
    # 连着没回：醒来不推迟，但不能开口（节奏已到，退避/安静期还没到）
    _write("self.json", {"speak_every_hours": 1})
    save({"spoken_at": [noon - 2 * 3600], "unanswered": 3, "last_wake_at": noon - 3600})
    assert next_due(load(), {"wake_at": noon + 3600, "set_at": noon}, noon) == noon + 3600
    ok, why = can_speak(load(), self_plan(), noon)
    assert ok is False and "连着 3 次没回" in why and "先不打扰他" in why, why
    out = after_wake('<|ACT:{"emotion":"happy"}|> 在吗？', noon)
    assert out == '<|ACT:{"emotion":"happy"}|>', out
    # 隔满 SILENT_GAP_S 又能开口
    save({"spoken_at": [noon - SILENT_GAP_S], "unanswered": 3, "last_wake_at": noon - 3600})
    assert can_speak(load(), self_plan(), noon)[0] is True
    save({"spoken_at": [noon - SILENT_GAP_S + 60], "unanswered": 3, "last_wake_at": noon - 3600})
    assert can_speak(load(), self_plan(), noon)[0] is False
    # 退避本身也生效：连着 4 次没回、2 小时前开过口，间隔还没拉到 80 分钟以上
    save({"spoken_at": [noon - 2 * 3600], "unanswered": 4, "last_wake_at": noon - 3600})
    ok, why = can_speak(load(), self_plan(), noon)
    assert ok is False and "连着 4 次没回" in why, why
    _write("self.json", {})
    # 坏的状态文件：改名留底，不覆盖
    with open(os.path.join(_state_dir(), "wake.json"), "w", encoding="utf-8") as f:
        f.write("{坏")
    assert load()["unanswered"] == 0 and any(n.startswith("wake.json.corrupt-") for n in os.listdir(_state_dir()))
    save({"spoken_at": [noon - 60, noon - 120, noon - 180], "unanswered": 0, "last_wake_at": noon})
    out = after_wake('<|ACT:{"emotion":"happy"}|> 指挥官！（好想他）', noon)
    assert out == '<|ACT:{"emotion":"happy"}|>', out
    save({"spoken_at": [], "unanswered": 0, "last_wake_at": noon})
    after_wake('<|ACT:{"emotion":"happy"}|> 指挥官，在吗？', noon)
    assert load()["unanswered"] == 1
    note_user(noon + 10)
    assert load()["unanswered"] == 0
    # 门铃：到点响一次，RING_HOLD_S 内不重复；忙时不响
    save({"last_user_at": noon - 3600, "last_wake_at": noon - 3600})
    fired, at = ring(False, noon)
    assert fired is True and at == noon, (fired, at)
    assert ring(False, noon + 5)[0] is False
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
    # 悄悄话（2026-11-02）：她也能主动去找可畏（这句无条件在）
    assert "想找可畏说说话，也可以用 send_message（channel=discord_whisper）去悄悄话找她。" in p
    # 醒来提示里不再有「规矩/底线/余量」这类像在下指令的说法（r35：醒来轮的英文拒绝来自这类措辞）
    assert not any(w in p for w in ("底线", "余量", "规矩", "以下只给我自己看"))
    # ── 一条计时（2026-11-02 回归）─────────────────────────────────────────
    _write("self.json", {})
    _write("life.json", [])
    base = noon
    empty = {"last_user_at": 0.0, "last_wake_at": base, "unanswered": 0, "spoken_at": [],
             "rung_at": 0.0, "rounds": []}
    save(dict(empty))
    assert next_due(empty, {}, base) == base + WAKE_DEFAULT_S
    assert next_due(empty, {"wake_at": base + 600, "set_at": base}, base) == base + WAKE_MIN_S
    assert next_due(empty, {"wake_at": base + 2700, "set_at": base}, base) == base + 2700
    assert clamp_wake_s(60) == WAKE_MIN_S and clamp_wake_s(10 ** 6) == WAKE_MAX_S and clamp_wake_s("坏") == WAKE_DEFAULT_S
    # 今天的两线格式：取 wake_at 更早的一条，intent 用「；」连起来，set_at 取较大值
    plan = {"speak": {"wake_at": base + 3600, "intent": "跟他说说今天的梦", "set_at": base},
            "life": {"wake_at": base + 1800, "intent": "画完那只猫", "set_at": base + 60},
            "speak_every_hours": 2}
    merged = merged_plan(plan)
    assert merged == {"wake_at": base + 1800, "intent": "画完那只猫；跟他说说今天的梦",
                      "set_at": base + 60, "speak_every_hours": 2}, merged
    assert next_due(empty, plan, base) == base + 60 + WAKE_MIN_S
    # 只有一条（旧代码写下的 life: {}）也认；只有开口节奏就只留节奏
    assert merged_plan({"speak": {"wake_at": base + 600, "intent": "x", "set_at": base}, "life": {}})["intent"] == "x"
    assert merged_plan({"speak_every_hours": 2}) == {"speak_every_hours": 2}
    # 最早的顶层旧格式照旧
    _write("self.json", {"wake_at": base + 900, "intent": "想跟他说说今天的梦", "set_at": base})
    save(dict(empty, last_wake_at=base - 1000))
    assert next_due(load(), self_plan(), base) == base + WAKE_MIN_S
    p_old = wake_prompt(base + WAKE_MIN_S)
    assert "上次我给自己定的事：想跟他说说今天的梦" in p_old
    # 门铃：一条计时到点响一次，不带种类
    _write("self.json", {"wake_at": base + 3600, "intent": "画完那只猫", "set_at": base})
    save(dict(empty))
    fired, at = ring(False, base + 3600)
    assert fired and at == base + 3600, (fired, at)
    assert "ring_kind" not in load(), "门铃不再记种类"
    p = wake_prompt(base + 3600)
    assert "上次我给自己定的事：画完那只猫" in p
    # 生活记录只留最近 20 条；醒来提示里带上最近做过的事（今天/昨天/N天前）
    _write("life.json", [{"did": "第%d件" % i, "at": base - (30 - i) * 3600} for i in range(30)])
    items = life_items()
    assert len(items) == LIFE_KEEP and len(_read("life.json", [])) == LIFE_KEEP
    assert items[-1]["did"] == "第29件"
    save(dict(empty))
    _write("self.json", {})
    p2 = wake_prompt(base + 60)
    assert "最近我自己做过的事：今天 第27件；今天 第28件；今天 第29件" in p2, p2
    assert "这台电脑里我自己能做的：" in p2
    assert "最近几次醒来都只顾着他" not in p2, "轮数不够不加平衡行"
    # 平衡行：最近 3 轮都有记录且 self 全是 False 才出现
    _write("life.json", [{"did": "画了猫", "at": base - 5 * 3600}])
    rounds_false = [{"at": base - i * 3600, "self": False, "spoke": True} for i in (1, 2, 3)]
    save(dict(empty, last_wake_at=base - 600, rounds=rounds_false[:2]))
    assert "最近几次醒来都只顾着他" not in wake_prompt(base), "只有两轮不加平衡行"
    save(dict(empty, last_wake_at=base - 600, rounds=rounds_false))
    p3 = wake_prompt(base)
    assert "最近几次醒来都只顾着他，自己的事已经5 小时没碰了。" in p3, p3
    _write("life.json", [])
    save(dict(empty, last_wake_at=base - 600, rounds=rounds_false))
    assert "最近几次醒来都只顾着他，自己的事一直没碰过。" in wake_prompt(base)
    # 其中一轮做了自己的事：不出现平衡行
    save(dict(empty, last_wake_at=base - 600,
              rounds=[{"at": base - 3600, "self": False, "spoke": True},
                      {"at": base - 7200, "self": True, "spoke": False},
                      {"at": base - 10800, "self": False, "spoke": True}]))
    assert "最近几次醒来都只顾着他" not in wake_prompt(base)
    # after_wake 记平衡：本轮醒来时刻 / 有没有新生活记录 / 有没有说出口
    _write("life.json", [{"did": "画了猫", "at": base - 300}])
    save(dict(empty, last_wake_at=base - 600))
    after_wake("（不想说话）", base)
    assert load()["rounds"][-1] == {"at": base - 600, "self": True, "spoke": False}, load()["rounds"]
    _write("life.json", [])
    save(dict(empty, last_wake_at=base - 600))
    _write("self.json", {"speak_every_hours": 1})
    after_wake('<|ACT:{"emotion":"happy"}|> 在呢。', base)
    assert load()["rounds"][-1] == {"at": base - 600, "self": False, "spoke": True}, load()["rounds"]
    # 只留最近 10 轮
    _write("self.json", {})
    save(dict(empty, last_wake_at=base - 600))
    for i in range(12):
        wake_prompt(base + i)
        after_wake("（安静）", base + i + 1)
    assert len(load()["rounds"]) == ROUNDS_KEEP, load()["rounds"]
    # ── 未回箱与平台通知（2026-11-02）────────────────────────────────────
    assert is_platform("【平台消息】Telegram 上他给我发了消息") and is_platform("[2026-11-02 21:00] 【平台消息】…")
    assert not is_real_user("【平台消息】Telegram 上他给我发了消息"), "平台通知不是他这一轮说的话"
    assert not is_wake("【平台消息】…") and is_real_user("在吗")
    # 桥替她主动开口：和桌面说出口同一套；10 分钟内不重复 +1
    save(dict(empty, last_wake_at=0.0))
    assert note_spoken(base) is True and load()["spoken_at"] == [base] and load()["unanswered"] == 1
    assert note_spoken(base + 60) is False and load()["spoken_at"] == [base] and load()["unanswered"] == 1
    assert note_spoken(base + SPOKEN_DEDUPE_S + 1) is True and load()["unanswered"] == 2
    # 醒来提示里念还没回的；醒来轮结束后了结（没回 = 她决定不回）
    item = inbox.add("telegram", "在忙吗", at=base - 60)
    p_box = wake_prompt(base)
    assert "还没回的消息（他发来的）：#%d" % item["id"] in p_box, p_box
    assert "这一轮把它们了结" in p_box
    assert load()["inbox_shown"] == [item["id"]]
    after_wake("（安静）", base + 1)
    assert load()["inbox_shown"] == [] and inbox.pending() == []
    assert inbox.load()["items"][-1]["state"] == "closed"
    # 已经回过的（answered）不再念
    item2 = inbox.add("discord", "睡了吗", at=base - 30)
    inbox.mark_answered([item2["id"]])
    assert "睡了吗" not in wake_prompt(base + 2)
    print("WAKE SELFTEST OK")


if __name__ == "__main__":
    _selftest()
