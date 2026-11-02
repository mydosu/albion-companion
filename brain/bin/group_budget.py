#!/usr/bin/env python3
"""群里的发言额度（2026-11-02 用户定）：两个 bot 共用一份，只算 Discord 群 #常规。

为什么要有：两个 bot 现在都看得到群里每条消息、都能自己决定回不回，逐条限制已经去掉了；
留一个总额度兜底，免得她俩聊起来没完。

规则（从频道最近 1 小时的消息按时间重放推出来）：
  · 起点 R = 8；
  · 指挥官本人（ALBION_DISCORD_OWNER_ID，默认 1508482140570914990）发一条：R = min(16, max(8, R+4))；
  · 任一 bot 发一条：R -= 1，最小到 0；
  · 兜底：最近 60 分钟两个 bot 合计 ≥ 60 条 → R 强制 0。
其它成员（非他、非 bot）说什么都不改 R。

算法只在这一份里；阿尔比恩的 chat_bridge 直接调，可畏那边走 bridge 的 `GET /group_budget`。
纯函数、只用标准库：输入是 [(作者 id, 是否 bot, 时间戳)]，时间戳单位秒。
"""
from __future__ import annotations

START = 8
CAP = 16
FLOOR = 8
OWNER_BONUS = 4
WINDOW_S = 3600
HARD_CAP_BOT_MESSAGES = 60

DEFAULT_OWNER_ID = "1508482140570914990"
DEFAULT_BOT_IDS = ("1566791156824805446", "1566802611611504680")   # Albion / Formidable


def _sid(value):
    return str(value).strip() if value is not None else ""


def compute(events, now, *, owner_id=DEFAULT_OWNER_ID, bot_ids=DEFAULT_BOT_IDS):
    """重放最近一小时的群消息，返回 {"remaining", "used", "owner_last_at"}。

    events: [(author_id, is_bot, ts)]，顺序不限；ts 是秒。
    used = 「这一轮」（他最后一次开口之后）两个 bot 已经说了几条；
          他这一小时没说过话时，算窗口里全部 bot 消息。
    """
    now = float(now)
    owner = _sid(owner_id)
    bots = {_sid(b) for b in (bot_ids or ())}
    recent = []
    for item in events or ():
        try:
            author, is_bot, ts = item[0], bool(item[1]), float(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if now - ts > WINDOW_S or ts > now + 60:      # 只算最近一小时（允许一点点时钟漂）
            continue
        recent.append((_sid(author), is_bot, ts))
    recent.sort(key=lambda e: e[2])

    remaining = START
    bot_messages = 0
    owner_last_at = 0.0
    for author, is_bot, ts in recent:
        if author == owner:
            owner_last_at = max(owner_last_at, ts)
            remaining = min(CAP, max(FLOOR, remaining + OWNER_BONUS))
        elif is_bot or author in bots:
            bot_messages += 1
            remaining = max(0, remaining - 1)

    if bot_messages >= HARD_CAP_BOT_MESSAGES:         # 一小时说了 60 条：不管 R 多少，强制闭嘴
        remaining = 0

    window_start = now - WINDOW_S
    since = owner_last_at if owner_last_at > window_start else window_start
    used = sum(1 for author, is_bot, ts in recent
               if (is_bot or author in bots) and ts >= since)

    return {"remaining": int(remaining), "used": int(used),
            "owner_last_at": float(owner_last_at) or None}


def _selftest():
    now = 1_800_000_000.0
    owner = DEFAULT_OWNER_ID
    albion, form = DEFAULT_BOT_IDS

    def ev(items):
        return compute(items, now)

    # 空频道：起点 8
    assert ev([])["remaining"] == 8
    # 他每说一句 +4，封顶 16
    assert ev([(owner, False, now - 300)])["remaining"] == 12
    assert ev([(owner, False, now - 300), (owner, False, now - 200)])["remaining"] == 16
    three = [(owner, False, now - 400), (owner, False, now - 300), (owner, False, now - 200)]
    assert ev(three)["remaining"] == 16, "封顶 16"
    # 不因为他说话掉到 8 以下（比如 R=0 时他说一句 → 8）
    drained = [(albion, True, now - 900 - i) for i in range(9)]
    assert ev(drained)["remaining"] == 0
    assert ev(drained + [(owner, False, now - 60)])["remaining"] == 8
    # bot 每说一句 -1，最小 0
    two_bots = [(albion, True, now - 120), (form, True, now - 60)]
    assert ev(two_bots)["remaining"] == 6
    # 他说话 + bot 说话交替
    mixed = [(owner, False, now - 600), (albion, True, now - 500), (albion, True, now - 400),
             (owner, False, now - 300), (form, True, now - 200)]
    # 8 -> 12 -> 11 -> 10 -> 14 -> 13
    assert ev(mixed)["remaining"] == 13, ev(mixed)
    assert ev(mixed)["used"] == 1, "这一轮＝他最后一句之后 bot 说了几条"
    assert ev(mixed)["owner_last_at"] == now - 300
    # 一小时 60 条：强制 0
    flood = [(albion if i % 2 else form, True, now - 3500 + i) for i in range(60)]
    flood = [(a, b, min(t, now - 5)) for a, b, t in flood]
    assert ev(flood)["remaining"] == 0, "60 条强制 0"
    assert ev(flood[:59])["remaining"] >= 0
    # 窗口外的老消息不算
    old = [(albion, True, now - 7 * 3600) for _ in range(20)]
    assert ev(old)["remaining"] == 8
    # 其它成员（不是他也不是 bot）不改 R
    assert ev([("999", False, now - 100), ("999", False, now - 90)])["remaining"] == 8
    # 入参脏了不炸
    assert ev([("x", "no", "bad"), (None, None, None), (owner, False, now - 10)])["remaining"] == 12
    print("GROUP BUDGET SELFTEST OK")


if __name__ == "__main__":
    _selftest()
