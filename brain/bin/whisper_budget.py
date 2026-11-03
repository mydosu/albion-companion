#!/usr/bin/env python3
"""Discord「悄悄话」频道的条数限制（2026-11-02 用户定：频率不重要，条数要管）。

悄悄话是阿尔比恩和可畏两个人说话的频道。只数两个 bot 的消息（指挥官偶尔插一句不算）：
  · 一次悄悄话：相邻两条间隔不超过 GAP_S（30 分钟）算同一次；隔得更久就是新的一次；
  · 每次最多 SESSION_CAP（20）条，两人合计；
  · 每天（北京时间 0 点起）最多 DAY_CAP（60）条，两人合计；
  · 两者取小的作为剩余；剩 LOW（4）条及以下提醒收尾，到 0 就不再发。

算法只在这一份里；阿尔比恩的 chat_bridge 直接调，可畏那边走 bridge 的 `GET /whisper_budget`。
纯函数、只用标准库：输入是 [(作者 id, 是否 bot, 时间戳)]，时间戳单位秒。
"""
from __future__ import annotations

SESSION_CAP = 20
DAY_CAP = 60
GAP_S = 1800
LOW = 4
WINDOW_S = 86400 + 3600                 # 翻历史只需要覆盖「今天」
BEIJING_OFFSET_S = 8 * 3600

LOW_NOTE = "悄悄话这次你们已经说了 %d 条，还能再说 %d 条，差不多该收尾、好好道个别了。"
SESSION_EMPTY = "这次悄悄话聊得够久了，就到这里吧；隔半小时以上再找对方。"
DAY_EMPTY = "今天的悄悄话已经聊到头了，明天再找对方吧。"


def day_start(now):
    """北京时间今天 0 点的时间戳。"""
    return (int(now) + BEIJING_OFFSET_S) // 86400 * 86400 - BEIJING_OFFSET_S


def compute(events, now, *, bot_ids=()):
    """返回 {"remaining", "session_used", "day_used", "limit"}；limit 是卡住的那一项（session/day）。"""
    now = float(now)
    bots = {str(b).strip() for b in (bot_ids or ())}
    times = []
    for item in events or ():
        try:
            author, is_bot, ts = str(item[0]).strip(), bool(item[1]), float(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if (is_bot or author in bots) and now - ts <= WINDOW_S and ts <= now + 60:
            times.append(ts)
    times.sort()

    day_used = sum(1 for ts in times if ts >= day_start(now))
    session_used = 0
    if times and now - times[-1] <= GAP_S:      # 最后一条离现在超过 30 分钟：已经是新的一次
        session_used = 1
        for prev, cur in zip(reversed(times[:-1]), reversed(times[1:])):
            if cur - prev > GAP_S:
                break
            session_used += 1
    left_session = max(0, SESSION_CAP - session_used)
    left_day = max(0, DAY_CAP - day_used)
    return {"remaining": min(left_session, left_day), "session_used": session_used,
            "day_used": day_used, "limit": "day" if left_day < left_session else "session"}


def note(budget):
    """写给她们看的提醒：快到头了提醒收尾，到头了告诉她们聊到这里。"""
    if not budget:
        return ""
    remaining = int(budget.get("remaining") or 0)
    if remaining <= 0:
        return DAY_EMPTY if budget.get("limit") == "day" else SESSION_EMPTY
    if remaining <= LOW:
        return LOW_NOTE % (int(budget.get("session_used") or 0), remaining)
    return ""


def _selftest():
    now = 1_800_000_000.0 + 12 * 3600          # 某天北京时间的白天
    a, f, owner = "1", "2", "9"
    assert compute([], now)["remaining"] == SESSION_CAP
    # 一次聊了 16 条（每 30 秒一条）：剩 4，提醒收尾
    chat = [(a if i % 2 else f, True, now - 30 * (16 - i)) for i in range(16)]
    b = compute(chat, now)
    assert b["session_used"] == 16 and b["remaining"] == 4 and "收尾" in note(b), b
    # 指挥官插话不算
    assert compute(chat + [(owner, False, now - 5)], now)["remaining"] == 4
    # 20 条：这次到头
    full = [(a, True, now - 30 * (20 - i)) for i in range(20)]
    b = compute(full, now)
    assert b["remaining"] == 0 and note(b) == SESSION_EMPTY, b
    # 隔了 31 分钟：新的一次，额度回来
    assert compute(full, now + GAP_S + 60)["remaining"] == SESSION_CAP
    # 中间断过 40 分钟：只数断点之后那一段
    split = [(a, True, now - 3600 - i) for i in range(10)] + [(f, True, now - 60 + i) for i in range(3)]
    assert compute(split, now)["session_used"] == 3
    # 今天已经 3 次 x 20 条：每天到头，就算隔了半小时也不行
    day = [(a, True, day_start(now) + 3600 * k + 10 * i) for k in range(3) for i in range(20)]
    later = day_start(now) + 3600 * 5
    b = compute(day, later)
    assert b["day_used"] == 60 and b["remaining"] == 0 and note(b) == DAY_EMPTY, b
    # 过了北京时间 0 点：新的一天
    assert compute(day, day_start(now) + 86400 + 60)["remaining"] == SESSION_CAP
    # 不是 bot、但在 bot 名单里也算
    assert compute([("2", False, now - 10)], now, bot_ids=("2",))["session_used"] == 1
    print("whisper_budget selftest ok")


if __name__ == "__main__":
    _selftest()
