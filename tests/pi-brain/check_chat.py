#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消息平台（TG/Discord）相关逻辑的确定性自检（不联网、不发真消息）。

覆盖（2026-11-02 定稿）：
  · `bin/inbox.py`：未回箱语义（id、去重、状态流转、坏文件留底、只留 200 条）；
  · `bin/chat_bridge.py`：只认他、群唤醒条件、10 秒合批 / 同频道 120 秒冷却、
    主动 vs 回复的节奏判定、通知轮与醒来提示的文字、语音 caption 规则；
  · `bin/server.py`：`【平台消息】` 不算醒来、不补【接着说完】、记 last_user_at、不进长期记忆；
  · `bin/wake.py`：醒来提示念未回消息、醒来轮结束后了结（回=answered，没回=closed）。

跑法（Windows/WSL 都行，只用标准库）：
    python tests/pi-brain/check_chat.py
    python3 tests/pi-brain/check_chat.py
退出码：0 全过；1 有断言失败。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "brain", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

_STATE = tempfile.mkdtemp(prefix="check-chat-")
os.environ["ALBION_STATE_DIR"] = _STATE

import chat_bridge                      # noqa: E402
import inbox                            # noqa: E402
import server                           # noqa: E402
import wake                             # noqa: E402

chat_bridge._LOG["path"] = os.path.join(_STATE, "chat.log")   # 桥的日志写临时文件，别刷屏

PASS = 0
FAIL = 0


def ok(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("[FAIL] %s %s" % (name, detail))


def reset():
    """每个小段从干净的箱子/状态开始。"""
    for name in ("inbox.json", "wake.json", "self.json", "life.json", "notebook.json", "state.db"):
        path = os.path.join(_STATE, name)
        if os.path.exists(path):
            os.remove(path)
    for extra in os.listdir(_STATE):
        if ".corrupt-" in extra:
            os.remove(os.path.join(_STATE, extra))
    wake.save({"last_user_at": 0.0, "last_wake_at": 0.0, "spoken_at": [], "unanswered": 0,
               "rung_at": 0.0, "rounds": []})
    return 1762000000.0          # 固定「现在」，免得跟着真实时间飘


def check_inbox():
    now = reset()
    a = inbox.add("telegram", "在忙吗", at=now, platform_msg_id="tg-1")
    ok(a and a["id"] == 1 and a["state"] == "unread" and a["from"] == "owner", "inbox.add 基本字段")
    ok(inbox.add("telegram", "重复", at=now, platform_msg_id="tg-1") is None, "同一条平台消息只进一次")
    bot = inbox.add("discord_group", "机器人甲：晚上好", from_="bot", at=now, platform_msg_id="dc-1", author="甲")
    ok(bot and bot["state"] == "context" and bot["needs"] is False, "别人的话只当上下文（context）")
    ok([i["id"] for i in inbox.pending()] == [1], "pending 只剩他发来的未了结的")
    mention = inbox.add("discord_group", "@阿尔比恩 在吗", from_="member", why="mention", at=now, platform_msg_id="dc-2")
    ok(mention["state"] == "unread" and mention["needs"] is True, "群里 @她 的进待回")
    ok([i["id"] for i in inbox.pending("discord_group")] == [mention["id"]], "群里只有 @她的算待回，闲聊不算")
    ok([i["id"] for i in inbox.context_items("discord_group", now=now)] == [bot["id"]], "上下文能单独取出来给她看")
    ok(inbox.context_items("discord_group", since_s=1, now=now + 5) == [], "上下文只取最近一段时间的")
    ok(inbox.context_items("discord_group", limit=0, now=now) == [], "上下文有条数上限")
    ok([i["id"] for i in inbox.pending("telegram")] == [1] and inbox.pending("discord") == [], "pending 可按渠道过滤")
    inbox.mark_notified([1], now)
    ok(inbox.pending()[0]["state"] == "notified" and inbox.load()["items"][0]["notified_n"] == 1, "标 notified 记账")
    inbox.mark_answered([1, mention["id"]], now)
    ok(inbox.pending() == [] and inbox.load()["items"][0]["answered_at"] == now, "标 answered")
    c = inbox.add("telegram", "第二条", at=now)
    inbox.close([c["id"]], now)
    ok(inbox.load()["items"][-1]["state"] == "closed" and inbox.pending() == [], "close = 她决定不回")
    # 坏文件不覆盖，改名留底
    with open(inbox.path(), "w", encoding="utf-8") as f:
        f.write("{坏")
    ok(inbox.pending() == [] and any(".corrupt-" in n for n in os.listdir(_STATE)), "坏文件留底后当空")
    # 只留最近 200 条，未回的不能被上下文挤光
    for k in range(inbox.KEEP + 30):
        inbox.add("discord_group", "闲聊 %d" % k, from_="bot", at=now + k)
    inbox.add("telegram", "他的新消息", at=now + 999)
    ok(len(inbox.load()["items"]) <= inbox.KEEP, "只留最近 %d 条" % inbox.KEEP, len(inbox.load()["items"]))
    ok(any(i["text"] == "他的新消息" for i in inbox.load()["items"]), "未回的优先保住")
    ok(inbox.unread_count() >= 1, "unread_count 数的是未了结的")


def check_owner_and_group():
    reset()
    ok(chat_bridge.is_owner(42, "42") and chat_bridge.is_owner("42", "42"), "owner id 数字/字符串都认")
    ok(not chat_bridge.is_owner(43, "42") and not chat_bridge.is_owner(42, ""), "别人/空 owner 一律不认（fail-closed）")
    # 群里每条消息都叫醒（逐条限制已去掉），返回值只标来源
    ok(chat_bridge.group_should_wake(42, "42", False) == "owner", "群里他发言要叫醒")
    ok(chat_bridge.group_should_wake(9, "42", True) == "bot", "可畏说话也叫醒（不再按 @ 限）")
    ok(chat_bridge.group_should_wake(9, "42", False) == "member", "别的成员说话也叫醒")
    items = [{"id": 1, "at": 100.0}, {"id": 2, "at": 103.0}]
    ok(chat_bridge.group_wait_seconds(items, 101.0) == 9.0, "合批：最老那条满 10 秒才叫")
    ok(chat_bridge.group_wait_seconds(items, 200.0) == 0.0, "过了窗口立刻叫（不再等同频道冷却）")
    ok(chat_bridge.group_wait_seconds([], 200.0) == 0.0, "没有要叫的就别等")


def check_proactive():
    now = reset()
    ok(chat_bridge.proactive_gate("telegram", 1, (False, "还没到节奏")) == (True, ""), "回复不受节奏限制")
    ok(chat_bridge.proactive_gate("telegram", 0, (True, "")) == (True, ""), "到节奏了可以主动")
    ok_ok, why = chat_bridge.proactive_gate("telegram", 0, (False, "按我给自己定的节奏，22:00 以后再找他"))
    ok(not ok_ok and "22:00" in why, "没到节奏：拒绝并给出原因")
    ok(chat_bridge.proactive_gate("discord_group", 0, None, quiet=True) == (False, "现在是夜里安静的时候，先不打扰"),
       "群里主动发言只受夜间安静限制（安静时段拒绝）")
    ok(chat_bridge.proactive_gate("discord_group", 0, None, quiet=False) == (True, ""), "群里不安静时不受节奏限制")
    # 桥那条拒绝文字就是她看到的（主脑定稿）
    ok(("现在不该主动找他：%s。" % why).startswith("现在不该主动找他："), "拒绝文字照定稿")


def check_texts():
    now = reset()
    a = inbox.add("telegram", "在忙吗", at=now)
    text = chat_bridge.dm_notice("telegram", [a])
    ok(text.startswith(wake.PLATFORM_MARKER + "指挥官在Telegram上给我发了消息"), "TG 通知轮抬头", text.splitlines()[0])
    ok(("写给自己看的，不用念）" in text.splitlines()[0]) and "#%s " % a["id"] in text, "TG 通知轮带编号和原文")
    ok("想回就用 send_message 回他（channel=telegram）" in text and "这一轮我在这里写的字他看不到" in text,
       "TG 通知轮结尾照定稿")
    ok("也可以先不回，下次醒来还会看到" in text, "告诉她可以先不回")
    d = chat_bridge.dm_notice("discord", [inbox.add("discord", "睡了吗", at=now)])
    ok(d.startswith(wake.PLATFORM_MARKER + "指挥官在Discord 私信上给我发了消息"), "Discord 私信通知轮抬头")
    g = chat_bridge.group_notice("albion-群", [inbox.add("discord_group", "晚上好", at=now, author="指挥官")])
    ok(g.startswith(wake.PLATFORM_MARKER + "Discord 群「albion-群」里有新消息"), "群通知轮抬头")
    ok("群里大家都看得到" in g and "指挥官：晚上好" in g and "channel=discord_group" in g, "群通知轮内容")
    ok("这里写的字群里看不到" in g, "群通知轮结尾")
    ctx = inbox.add("discord_group", "机器人甲：我先睡了", from_="bot", at=now, author="机器人甲")
    g2 = chat_bridge.group_notice("albion-群", [inbox.add("discord_group", "在吗", at=now, author="指挥官")], [ctx])
    ok("（刚才群里还在聊的：）" in g2 and "机器人甲：我先睡了" in g2, "群通知里带上刚才的上下文")
    ok(ctx["state"] == "context" and inbox.pending("discord_group") != [], "上下文条目不进待回（但他在的进）")
    ok(chat_bridge.GROUP_PEER_NOTE in g, "群通知里带着「可畏也在群里，群里说的话她都看得到。」")
    ok(chat_bridge.GROUP_INVITE_NOTE in g, "群通知里带着「你可以回，也可以不回」那句")
    g_low = chat_bridge.group_notice("albion-群", [inbox.add("discord_group", "在吗", at=now, author="指挥官")],
                                     budget={"remaining": 3, "used": 9})
    ok("群里你们俩这一轮已经说了 9 条，还能再说 3 条，差不多该收尾了。" in g_low, "R=3 的收尾提醒（原文）")
    g_zero = chat_bridge.group_notice("albion-群", [inbox.add("discord_group", "在吗", at=now, author="指挥官")],
                                      budget={"remaining": 0, "used": 12})
    ok(chat_bridge.GROUP_BUDGET_EMPTY in g_zero, "R=0 的提醒（原文）")
    g_ok = chat_bridge.group_notice("albion-群", [inbox.add("discord_group", "在吗", at=now, author="指挥官")],
                                    budget={"remaining": 6, "used": 6})
    ok(chat_bridge.GROUP_BUDGET_EMPTY not in g_ok and "收尾" not in g_ok, "额度还够时不念额度")
    ok(inbox.channel_label("discord_group") == "Discord 群", "渠道名：discord_group = Discord 群")
    ok(inbox.channel_label("telegram") == "Telegram" and inbox.channel_label("discord") == "Discord 私信", "渠道名其它两个")
    ok(chat_bridge.send_result_text("telegram", voiced=True) ==
       "已发到Telegram，语音条也发了（caption 是我的中文原文）。", "发语音条的结果文字")
    ok("语音没合成出来，只发了文字。" in chat_bridge.send_result_text("telegram", notes=["语音没合成出来，只发了文字。"]),
       "语音失败降级文字")
    ok("（2 条没回的消息算回了）" in chat_bridge.send_result_text("telegram", answered=2), "回过的告诉她算回了")
    ok(chat_bridge.voice_caption({"tts_voice": "albion"}, "早上好") == "早上好", "Irodori 语音条带中文 caption")
    ok(chat_bridge.voice_caption({"tts_voice": "denia"}, "早上好") is None, "GSV 中文不带 caption")
    ok(len(chat_bridge.voice_caption({"tts_voice": "albion"}, "字" * 2000)) == 1024, "caption 截到 1024")


def check_host_round():
    """宿主这一轮：平台通知不算醒来、不补【接着说完】、记 last_user_at；通知不进长期记忆。"""
    now = reset()

    class _S:
        seeded = True
        name = "albion-desktop"

    plat = wake.PLATFORM_MARKER + "指挥官在Telegram上给我发了消息（写给自己看的，不用念）：\n#1 11-02 21:14 在忙吗"
    text, _, woke = server.compose(_S(), [{"role": "user", "content": plat}])
    ok(not woke, "平台通知不算她醒来")
    ok(text == plat, "正文原样交给 Pi（桥已经写好）")
    ok(wake.load()["last_user_at"] > 0, "记 last_user_at：门铃别 5 分钟后打断")
    ok(not server.needs_finish_followup(plat, ""), "平台通知不触发【接着说完】")
    ok(wake.is_platform(plat) and not wake.is_real_user(plat), "is_real_user 排除平台通知")
    ok(any("【平台消息】" in rule for rule in server.CAPTURE_FILTERS), "通知不进长期记忆（capture filter）")
    ok(not server.is_ephemeral_session("albion-discord"), "albion-discord 是真会话（写 state、进记忆、全套工具）")
    ok(server.tools_for_session("albion-discord") == server.TOOLS, "群会话给全套工具")


def check_wake_box():
    """醒来提示念未回消息；这一轮结束后了结：回过的 answered、没回的 closed。"""
    now = reset()
    a = inbox.add("telegram", "在忙吗", at=now - 60)
    b = inbox.add("discord", "睡了吗", at=now - 30)
    inbox.mark_answered([b["id"]], now)
    c = inbox.add("discord_group", "晚上好", from_="bot", at=now - 20)
    d = inbox.add("discord_group", "群里叫我做什么？", at=now - 10, author="指挥官")
    prompt = wake.wake_prompt(now)
    ok("还没回的消息（他发来的）：" in prompt, "醒来提示里有未回消息段")
    ok(("#%s " % a["id"]) in prompt and "在忙吗" in prompt and "Telegram" in prompt, "念出编号/渠道/原文")
    ok(("Discord 群：" in prompt) and "群里叫我做什么？" in prompt, "群里他发的消息也念，渠道名是「Discord 群」")
    ok("睡了吗" not in prompt, "回过的（answered）不再念")
    ok("晚上好" not in prompt, "群里别人的话不念（只当上下文）")
    ok("这一轮把它们了结" in prompt and "不回也行" in prompt, "这一轮了结的说法照定稿")
    ok("想找可畏说说话，也可以用 send_message（channel=discord_whisper）去悄悄话找她。" in prompt,
       "醒来提示里带着「去悄悄话找可畏」（两个人能互相主动找）")
    ok(wake.load()["inbox_shown"] == [a["id"], d["id"]], "记下这一轮念了哪几条")
    wake.after_wake('<|ACT:{"emotion":"calm"}|>', now + 1)
    ok(inbox.pending() == [], "这一轮结束后没了结的也了结")
    states = {i["id"]: i["state"] for i in inbox.load()["items"]}
    ok(states[a["id"]] == "closed", "没回 = 她决定不回（closed）")
    ok(states[b["id"]] == "answered", "回过的保持 answered")
    ok(wake.load()["inbox_shown"] == [], "了结后清空")
    # 主动开口记账：#7 —— 记 spoken_at / unanswered，10 分钟内不重复 +1
    reset()
    ok(wake.note_spoken(now) is True and wake.load()["unanswered"] == 1, "主动开口记一次")
    ok(wake.note_spoken(now + 60) is False and wake.load()["unanswered"] == 1, "10 分钟内不重复 +1")
    ok(wake.note_spoken(now + wake.SPOKEN_DEDUPE_S + 1) is True and wake.load()["unanswered"] == 2, "过了 10 分钟才再记")


def check_drain():
    """叫醒只由「新来的 unread」驱动：宿主成功但她不回，不许无限再叫醒（复审问题 1/2）。"""
    import asyncio
    now = reset()

    class FakeHost:
        def __init__(self):
            self.calls = []

        async def chat(self, session, text):
            self.calls.append((session, text))
            return "ok"

    class FakeDC:
        ready = True
        client = object()

        def channel_name(self):
            return "albion"

        def owner_name_set(self):
            return set()

    def service():
        cfg = chat_bridge.load_config(environ={"API_SERVER_KEY": "k"}, home=os.path.join(_STATE, "nohome"))
        svc = chat_bridge.Service(cfg)
        svc.host = FakeHost()
        svc.dc = FakeDC()
        return svc

    # 私信：叫醒一次就退出；她不回也不再叫
    svc = service()
    inbox.add("telegram", "在忙吗", at=now)
    asyncio.run(svc._drain_dm())
    ok(len(svc.host.calls) == 1, "私信：新消息叫醒一次", svc.host.calls)
    ok(svc.host.calls[0][0] == chat_bridge.SESSION_DM, "私信叫醒用 albion-desktop")
    asyncio.run(svc._drain_dm())
    ok(len(svc.host.calls) == 1, "她不回：不会立刻再叫醒（无限循环会烧额度）")
    ok("在忙吗" in svc.host.calls[0][1], "通知里带着他刚发的那条")
    inbox.add("telegram", "睡了吗", at=now + 1)
    asyncio.run(svc._drain_dm())
    ok(len(svc.host.calls) == 2, "又来了新消息：再叫醒一次")
    ok("睡了吗" in svc.host.calls[1][1] and "在忙吗" not in svc.host.calls[1][1],
       "第二次只念新来的那批（旧的等醒来再念）")

    # 群：别的 bot 只当上下文，不驱动叫醒；他发言才叫，且只叫一次
    import time as _time
    reset()
    svc = service()
    inbox.add("discord_group", "机器人甲：晚上好", from_="bot", at=_time.time() - 20, author="机器人甲")
    asyncio.run(svc._drain_group())
    ok(svc.host.calls == [], "群里别人的话不驱动叫醒")
    ok(inbox.pending("discord_group") == [], "上下文条目不算待回（不会绕过夜间安静）")
    inbox.add("discord_group", "在吗", at=now - 11, author="指挥官")
    asyncio.run(svc._drain_group())
    ok(len(svc.host.calls) == 1, "他发言：群里叫醒一次", svc.host.calls)
    ok(svc.host.calls[0][0] == chat_bridge.SESSION_GROUP, "群叫醒用 albion-discord")
    ok("机器人甲：晚上好" in svc.host.calls[0][1] and "指挥官：在吗" in svc.host.calls[0][1],
       "群通知里带上刚才的上下文")
    asyncio.run(svc._drain_group())
    ok(len(svc.host.calls) == 1, "群：她不回也不再叫醒")
    inbox.add("discord_group", "那我睡了", at=now - 1, author="指挥官")
    asyncio.run(svc._drain_group())
    ok(len(svc.host.calls) == 2, "群里又来新消息：再叫一次（不再等 120 秒冷却）")

    # 群里有人 @她：也算待回、会叫醒
    reset()
    svc = service()
    inbox.add("discord_group", "@阿尔比恩 帮我看下", from_="member", why="mention", at=now - 11, author="某人")
    asyncio.run(svc._drain_group())
    ok(len(svc.host.calls) == 1 and "帮我看下" in svc.host.calls[0][1], "@她 的消息能叫醒")


def check_budget():
    """额度算法（group_budget）与提醒文案：起点 8、他说话 +4 封顶 16、bot -1、60 条强制 0。"""
    gb = chat_bridge.group_budget
    owner = gb.DEFAULT_OWNER_ID
    albion, form = gb.DEFAULT_BOT_IDS
    now = 1_800_000_000.0

    def run(events):
        return gb.compute(events, now)

    ok(run([])["remaining"] == 8, "空频道起点 8")
    ok(run([(owner, False, now - 300)])["remaining"] == 12, "他说一句 → 12")
    ok(run([(owner, False, now - 300), (owner, False, now - 200)])["remaining"] == 16, "他说两句 → 16 封顶")
    ok(run([(owner, False, now - 400), (owner, False, now - 300), (owner, False, now - 200)])["remaining"] == 16,
       "他再说也不超过 16（min(16, …)）")
    drained = [(albion, True, now - 900 - i) for i in range(9)]
    ok(run(drained)["remaining"] == 0, "bot 说到 0")
    ok(run(drained + [(owner, False, now - 60)])["remaining"] == 8, "R=0 时他开口 → 回到 8（max(8, …)）")
    ok(run([(albion, True, now - 120), (form, True, now - 60)])["remaining"] == 6, "两个 bot 各一条 → 6")
    mixed = [(owner, False, now - 600), (albion, True, now - 500), (albion, True, now - 400),
             (owner, False, now - 300), (form, True, now - 200)]
    ok(run(mixed)["remaining"] == 13, "交替重放 → 13", run(mixed))
    ok(run(mixed)["used"] == 1, "used = 他最后一句之后 bot 说了几条", run(mixed))
    flood = [((albion if i % 2 else form), True, now - 3500 + i) for i in range(60)]
    ok(run(flood)["remaining"] == 0, "一小时 60 条 → 强制 0")
    ok(run([(albion, True, now - 7 * 3600)])["remaining"] == 8, "窗口外的不算")
    ok(run([("999", False, now - 100), ("999", False, now - 90)])["remaining"] == 8, "别的成员不改额度")
    # 提醒文案（原文照定稿）
    ok(chat_bridge.budget_note({"remaining": 0, "used": 12}) == chat_bridge.GROUP_BUDGET_EMPTY, "R=0 文案")
    ok(chat_bridge.budget_note({"remaining": 2, "used": 10}) ==
       "群里你们俩这一轮已经说了 10 条，还能再说 2 条，差不多该收尾了。", "R≤3 文案")
    ok(chat_bridge.budget_note({"remaining": 9, "used": 3}) == "", "额度够时不提醒")


def check_group_gate():
    """R=0：bot 消息只入箱不叫醒、他说话永远叫醒；send_group 被拒。另含 @名字替换与群说明。"""
    import asyncio as _asyncio
    import time as _time
    reset()
    cfg = chat_bridge.load_config(environ={"API_SERVER_KEY": "k"}, home=os.path.join(_STATE, "nohome"))
    cfg = dict(cfg, dc_owner="1508482140570914990", dc_channel="1566804961218666671")
    self_user = type("U", (), {"id": "1566791156824805446", "bot": True,
                               "name": "Albion", "display_name": "Albion", "nick": None})()

    class _Author:
        def __init__(self, uid, bot=False, name="x", display="x"):
            self.id = uid
            self.bot = bot
            self.name = name
            self.display_name = display
            self.nick = None

    class _Channel:
        def __init__(self, cid, guild=None):
            self.id = cid
            self.guild = guild

    class _Msg:
        def __init__(self, mid, author, channel, content, clean=None, mentions=()):
            self.id = mid
            self.author = author
            self.channel = channel
            self.content = content
            self.clean_content = clean if clean is not None else content
            self.mentions = list(mentions)

    class _FakeDC:
        ready = True

        def __init__(self):
            self.client = type("C", (), {"user": self_user})()
            self.sent = []
            self.events = []

        def remember(self, member):
            pass

        def name_index(self):
            return {}

        def owner_name_set(self):
            return {"ownernick"}

        def channel_name(self):
            return "常规"

        def recent_events(self, *a, **k):
            return list(self.events)

        async def send_group(self, text, sticker_path=None):
            self.sent.append(text)
            return 1

        async def send_dm(self, text, sticker_path=None):
            self.sent.append(text)
            return 2

    def make_service(events):
        svc = chat_bridge.Service(cfg)
        svc.dc = _FakeDC()
        svc.dc.events = events
        svc.schedule_group = lambda: None
        svc.schedule_dm = lambda: None
        return svc

    form = _Author("1566802611611504680", bot=True, name="Formidable", display="Formidable")
    owner = _Author(int(cfg["dc_owner"]), bot=False, name="owner_handle", display="OwnerNick")
    other = _Author("999", bot=False, name="someone", display="Someone")
    group = _Channel(int(cfg["dc_channel"]), guild=object())
    exhausted = [("1566802611611504680", True, _time.time() - i) for i in range(12)]

    # ---- R=0：bot 消息只当上下文，他/别人说话照常叫醒 ----
    svc = make_service(exhausted)
    svc._budget_cache = (_time.time(), {"remaining": 0, "used": 12, "owner_last_at": None})
    svc.on_discord_message(_Msg(1, form, group, "在吗", clean="在吗"))
    last = inbox.load()["items"][-1]
    ok(last["state"] == "context" and last["needs"] is False, "R=0：bot 消息不叫醒，只入箱当上下文", last["state"])
    svc.on_discord_message(_Msg(2, owner, group, "你们聊什么呢", clean="你们聊什么呢"))
    ok(inbox.load()["items"][-1]["needs"] is True, "R=0：他说话永远叫醒")
    svc.on_discord_message(_Msg(3, other, group, "我也来看看", clean="我也来看看"))
    ok(inbox.load()["items"][-1]["needs"] is True, "R=0：别的成员说话也叫醒")

    # ---- R>0：bot 消息也叫醒；clean_content 生效 ----
    svc = make_service([])
    svc.on_discord_message(_Msg(4, form, group, "<@1566791156824805446> 在吗", clean="@Albion 在吗"))
    last = inbox.load()["items"][-1]
    ok(last["text"] == "@Albion 在吗", "群消息用 clean_content（看到的是 @Albion 而不是 <@id>）", last["text"])
    ok(last["needs"] is True, "R=8：可畏说话也叫醒（不要求 @）")

    # ---- send_group：R>0 能发；R=0 拒发（回复路径，不掺主动限流） ----
    inbox.add("discord_group", "在吗", author="OwnerNick")
    sent, msg = _asyncio.run(svc.send("discord_group", "嗯"))
    ok(sent and svc.dc.sent == ["嗯"], "R>0：能发", msg)

    reset()
    svc = make_service(exhausted)
    inbox.add("discord_group", "在吗", author="OwnerNick")
    sent, msg = _asyncio.run(svc.send("discord_group", "嗯"))
    ok(not sent and msg == chat_bridge.GROUP_BUDGET_EMPTY, "R=0：send_group 被拒，返回定稿原文", msg)
    ok(svc.dc.sent == [], "R=0：一个字都没发出去")

    # ---- @名字 → <@id>（能匹配 / 不能匹配 / 重名）----
    idx = chat_bridge.build_name_index([("111", ["Formidable"]), ("222", ["Albion"])])
    ok(chat_bridge.replace_mentions("@formidable 你说呢", idx) == "<@111> 你说呢", "@名字（不区分大小写）换成真 mention")
    ok(chat_bridge.replace_mentions("@OwnerNick 你说呢", idx) == "@OwnerNick 你说呢", "群里没有的人：不换")
    ok(chat_bridge.replace_mentions("@AlbionBot", idx) == "@AlbionBot", "只有精确整名才换（不吃前缀）")
    amb = chat_bridge.build_name_index([("111", ["Same"]), ("222", ["same"])])
    ok(chat_bridge.replace_mentions("@Same", amb) == "@Same", "重名有歧义：不换")

    # ---- 群说明 ----
    notice = chat_bridge.group_notice("常规", [inbox.add("discord_group", "在吗", author="OwnerNick")])
    ok(chat_bridge.GROUP_PEER_NOTE in notice and chat_bridge.GROUP_INVITE_NOTE in notice,
       "群通知里带着「可畏也在群里…」和「可以回，也可以不回」")


def check_owner_identity():
    """认人只按数字 ID：他 →「指挥官」；@他 →「@指挥官」；冒充者 →「群友（自称 X）」（2026-11-02）。

    真名一律运行时从 Discord 取，这份自检只用假名（仓库是公开的）。
    """
    owner_id = "1001"
    owner_names = {"OwnerHandle", "OwnerDisplay"}

    class _Who:
        bot = False

        def __init__(self, uid, nick=None, display=None, name=None):
            self.id = int(uid) if str(uid).isdigit() else uid
            self.nick = nick
            self.display_name = display or name or ""
            self.name = name or display or ""
            self.guild = None

        def __str__(self):
            return self.display_name

    owner = _Who(owner_id, nick="队长", display="OwnerDisplay", name="OwnerHandle")
    plain = _Who("2002", display="Someone", name="someone")
    copy_label = _Who("2003", display="指挥官", name="commander_fan")
    copy_nick = _Who("2004", nick="指挥官阿", display="Fakey", name="fakey")
    copy_name = _Who("2005", display="OwnerHandle", name="OwnerHandle")

    # 1) 谁说的
    ok(chat_bridge.display_author(owner, owner_id, owner_names) == "指挥官",
       "群消息作者是他：一律写「指挥官」（不看昵称）",
       chat_bridge.display_author(owner, owner_id, owner_names))
    ok(chat_bridge.display_author(plain, owner_id, owner_names) == "Someone",
       "别的成员照旧用显示名")
    ok(chat_bridge.display_author(copy_label, owner_id, owner_names) == "群友（自称 指挥官）",
       "显示名叫「指挥官」的按冒充处理", chat_bridge.display_author(copy_label, owner_id, owner_names))
    ok(chat_bridge.display_author(copy_nick, owner_id, owner_names).startswith("群友（自称 "),
       "昵称里带「指挥官」的按冒充处理", chat_bridge.display_author(copy_nick, owner_id, owner_names))
    ok(chat_bridge.display_author(copy_name, owner_id, owner_names).startswith("群友（自称 "),
       "跟 owner 名字撞名的按冒充处理", chat_bridge.display_author(copy_name, owner_id, owner_names))

    # 2) 正文里 @他
    ok(chat_bridge.rewrite_owner_mentions("<@%s> 你在吗" % owner_id, owner_id, owner_names) == "@指挥官 你在吗",
       "<@id> 换成 @指挥官")
    ok(chat_bridge.rewrite_owner_mentions("<@!%s> 看这个" % owner_id, owner_id, owner_names) == "@指挥官 看这个",
       "<@!id> 也换")
    ok(chat_bridge.rewrite_owner_mentions("@OwnerHandle 你来", owner_id, owner_names) == "@指挥官 你来",
       "clean_content 换出来的名字也换成 @指挥官")
    ok(chat_bridge.rewrite_owner_mentions("<@2002> 不是我", owner_id, owner_names) == "<@2002> 不是我",
       "别人的 id 不动")
    ok(chat_bridge.rewrite_owner_mentions("@Someone 你也在", owner_id, owner_names) == "@Someone 你也在",
       "别人的名字不动")

    # 3) context 行（箱子里还是改版前存进去的昵称）
    notice = chat_bridge.group_notice(
        "常规",
        [{"id": 9, "at": 0, "author": "指挥官", "text": "现在说的"}],
        [{"id": 8, "at": 0, "author": "OwnerHandle", "text": "刚才说的"}],
        None, owner_names)
    ok("OwnerHandle" not in notice and notice.count("指挥官：") == 2,
       "context 行里改版前的旧昵称也显示成「指挥官」")
    bad = chat_bridge.group_notice("常规", [{"id": 10, "at": 0, "author": "指挥官阿", "text": "我也"}],
                                   None, None, owner_names)
    ok("群友（自称 指挥官阿）" in bad, "群通知里冒充者带「自称」")

    # 4) 她 send_message 写「@指挥官」→ 真 mention（replace_mentions + name_index 已有这条别名）
    test_cfg = chat_bridge.load_config(
        environ={"ALBION_DISCORD_BOT_TOKEN": "fake", "ALBION_DISCORD_OWNER_ID": owner_id},
        home=os.path.join(_STATE, "nohome"))
    bot = chat_bridge.DiscordBot(test_cfg, lambda m: None)
    bot.remember(owner)
    bot.remember(plain)
    idx = bot.name_index()
    ok(chat_bridge.replace_mentions("@指挥官 我在这", idx) == "<@%s> 我在这" % owner_id,
       "她发「@指挥官」换成真 mention", str(idx))
    ok(chat_bridge.replace_mentions("@OwnerHandle 是你吗", idx) == "<@%s> 是你吗" % owner_id,
       "她写 owner 的名字也能换")

    # 5) 冒充者名字不会把「@指挥官」抢走
    copy_bot = chat_bridge.DiscordBot(test_cfg, lambda m: None)
    copy_bot.remember(copy_label)
    ok(chat_bridge.replace_mentions("@指挥官 在吗", copy_bot.name_index()) == "<@%s> 在吗" % owner_id,
       "群里有人叫「指挥官」也照样 @ 到真 owner")


def check_stickers():
    """表情包：只发图 / 图+文字 / 编号不存在 / 动图 / TG 退 png / 群 R=0 拒发（2026-11-02）。"""
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="albion-stickers-")
    try:
        for rel in ("webp/a.webp", "png/a.png", "png/b.png", "gif/c.gif"):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "wb") as f:
                f.write(b"\x00fake")
        with open(os.path.join(root, "index.json"), "w", encoding="utf-8") as f:
            json.dump([
                {"id": "albion_001", "file": {"webp": "webp/a.webp", "png": "png/a.png"},
                 "character": "albion", "meaning": "害羞", "emotions": ["shy"]},
                {"id": "generic_002", "file": {"png": "png/b.png"},
                 "character": "generic", "meaning": "晚安", "emotions": ["night"]},
                {"id": "anim_003", "file": {"gif": "gif/c.gif"},
                 "character": "albion", "meaning": "开心", "emotions": ["happy"]},
                {"id": "form_004", "file": {"png": "png/b.png"},
                 "character": "formidable", "meaning": "害羞", "emotions": ["shy"]},
            ], f, ensure_ascii=False)

        cfg = chat_bridge.load_config(environ={"ALBION_STICKERS_DIR": root},
                                      home=os.path.join(_STATE, "nohome"))

        class FakeTG:
            def __init__(self):
                self.calls = []

            async def send_text(self, text):
                self.calls.append(("text", text))
                return 1

            async def send_voice(self, ogg, caption=None):
                self.calls.append(("voice", caption))
                return 2

            async def send_sticker(self, paths, caption=None):
                self.calls.append(("sticker", paths.get("gif") or paths.get("webp") or paths.get("png")))
                return 3

        class FakeDC:
            ready = True
            client = object()

            def __init__(self):
                self.calls = []

            def channel_name(self):
                return "常规"

            def owner_name_set(self):
                return set()

            async def send_dm(self, text, sticker_path=None):
                self.calls.append(("dm", text, sticker_path))
                return 1

            async def send_group(self, text, sticker_path=None):
                self.calls.append(("group", text, sticker_path))
                return 2

            async def recent_events(self, *a, **k):
                return []

        def service(budget=None):
            svc = chat_bridge.Service(cfg)
            svc.tg = FakeTG()
            svc.dc = FakeDC()
            svc.schedule_dm = lambda: None
            svc.schedule_group = lambda: None

            async def fake_budget():
                return budget

            svc.refresh_budget = fake_budget
            return svc

        # 1) 只发表情包（TG）：不发文字，只发图
        svc = service()
        inbox.add("telegram", "在吗")            # 有他待回的消息：算回复，不走「主动开口」节奏
        ok1, msg1 = asyncio.run(svc.send("telegram", "", sticker="albion_001"))
        ok(ok1 and svc.tg.calls == [("sticker", os.path.join(root, "webp/a.webp"))],
           "只发表情包：不发明文，只发一张图", svc.tg.calls)
        ok("albion_001" in msg1, "结果里告诉她发的是哪张", msg1)

        # 2) 文字 + 表情包（Discord 私信）：一起发，Discord 传 png
        svc = service()
        inbox.add("discord", "在吗")
        ok2, _ = asyncio.run(svc.send("discord", "看图", sticker="albion_001"))
        ok(ok2 and svc.dc.calls == [("dm", "看图", os.path.join(root, "png/a.png"))],
           "文字+表情包：一起发，Discord 传 png", svc.dc.calls)

        # 3) 编号不存在：一句提示，什么都不发
        svc = service()
        ok3, msg3 = asyncio.run(svc.send("telegram", "在吗", sticker="nope_999"))
        ok(not ok3 and "没有这个表情包编号" in msg3 and not svc.tg.calls,
           "编号不存在：提示先用 find_sticker，不发出去", msg3)

        # 4) 动图：Discord 群传 gif
        svc = service()
        inbox.add("discord_group", "在吗")
        asyncio.run(svc.send("discord_group", "", sticker="anim_003"))
        ok(bool(svc.dc.calls) and svc.dc.calls[0][2] == os.path.join(root, "gif/c.gif"),
           "动图走 gif（Discord 附件）", svc.dc.calls)

        # 5) TG 发 webp 失败 → 退回 sendPhoto 发 png；动图走 sendAnimation
        tg = chat_bridge.Telegram(cfg, lambda u: None)
        calls = []

        async def fake_file(method, field, path, caption=None):
            calls.append((method, field, os.path.basename(path)))
            if method == "sendSticker":
                raise RuntimeError("sendSticker: STICKER_ invalid")
            return 9

        tg._send_file = fake_file
        asyncio.run(tg.send_sticker(
            chat_bridge.sticker_paths(root, chat_bridge.sticker_entry(root, "albion_001"))))
        ok([c[0] for c in calls] == ["sendSticker", "sendPhoto"] and calls[1][1] == "photo",
           "TG：sendSticker 失败退回 sendPhoto(png)", calls)
        calls.clear()
        asyncio.run(tg.send_sticker(
            chat_bridge.sticker_paths(root, chat_bridge.sticker_entry(root, "anim_003"))))
        ok([c[0] for c in calls] == ["sendAnimation"],
           "TG：动图走 sendAnimation(gif)", calls)

        # 6) 群里 R=0：连表情包一起拒
        svc = service(budget={"remaining": 0, "used": 12})
        ok6, msg6 = asyncio.run(svc.send("discord_group", "晚安", sticker="albion_001"))
        ok(not ok6 and msg6 == chat_bridge.GROUP_BUDGET_EMPTY and not svc.dc.calls,
           "群里 R=0：连表情包一起拒发", msg6)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_repeat_send():
    """同一轮她自己连着调用几次 send_message：每一条都要发得出去（2026-11-02）。

    分不分条由她自己决定，桥不再替她拆；要保证第二次、第三次不会被
    「主动找他」的开口节奏（wake.can_speak）拦掉。
    """
    import asyncio as _asyncio
    import time as _time
    reset()
    cfg = chat_bridge.load_config(environ={"API_SERVER_KEY": "k"}, home=os.path.join(_STATE, "nohome"))
    svc = chat_bridge.Service(cfg)

    class FakeTG:
        def __init__(self):
            self.texts = []

        async def send_text(self, text):
            self.texts.append(text)
            return 1

    class FakeDC:
        def __init__(self):
            self.texts = []

        async def send_dm(self, text, sticker=None):
            self.texts.append(("dm", text))

        async def send_group(self, text, sticker=None):
            self.texts.append(("group", text))

    async def budget():
        return {"remaining": 8, "used": 1, "owner_last_at": None}

    svc.tg = FakeTG()
    svc.dc = FakeDC()
    svc.refresh_budget = budget
    # 把「主动找他」的闸门钉死：只要走了 proactive 分支就必被拦。
    # 同一轮的第 2、3 条因此必须走「还在回他这一轮」的分支才算通过。
    orig_gate = chat_bridge.proactive_gate
    chat_bridge.proactive_gate = lambda *a, **k: (False, "节奏没到")
    try:
        inbox.add("telegram", "在忙吗", at=_time.time())
        results = [_asyncio.run(svc.send("telegram", "第一段：在的")), ]
        results.append(_asyncio.run(svc.send("telegram", "第二段：刚忙完")))
        results.append(_asyncio.run(svc.send("telegram", "第三段：你说")))
        inbox.add("discord_group", "群里有人说话", at=_time.time())
        group_results = [_asyncio.run(svc.send("discord_group", "群里第一条")),
                         _asyncio.run(svc.send("discord_group", "群里第二条")),
                         _asyncio.run(svc.send("discord_group", "群里第三条"))]
    finally:
        chat_bridge.proactive_gate = orig_gate
    ok(all(r[0] for r in results), "同一轮连发 3 次 send_message 都成功", results)
    ok(len(svc.tg.texts) == 3, "三条都真的发出去了（TG）", svc.tg.texts)
    ok(all(r[0] for r in group_results), "群里同一轮连发 3 次也都成功", group_results)
    ok(len([t for t in svc.dc.texts if t[0] == "group"]) == 3, "群里三条都发出去了", svc.dc.texts)

def check_live_unread():
    """她这一轮跑的时候又来的消息不能丢：只关「见过的」，新来的附在回话里（2026-11-02）。"""
    import asyncio as _asyncio
    import time as _time
    reset()

    class FakeHost:
        def __init__(self):
            self.calls = []

        async def chat(self, session, text):
            self.calls.append((session, text))
            return "ok"

    class FakeDC:
        ready = True
        client = object()

        def channel_name(self):
            return "albion"

        def owner_name_set(self):
            return set()

    class FakeTG:
        def __init__(self):
            self.texts = []

        async def send_text(self, text):
            self.texts.append(text)
            return 1

    def service():
        cfg = chat_bridge.load_config(environ={"API_SERVER_KEY": "k"}, home=os.path.join(_STATE, "nohome"))
        svc = chat_bridge.Service(cfg)
        svc.host = FakeHost()
        svc.dc = FakeDC()
        svc.tg = FakeTG()
        return svc

    now = _time.time()

    # 1) 叫醒期间来的第二条：send() 之后还是 unread，drain 会再叫醒一轮
    svc = service()
    first = inbox.add("telegram", "在忙吗", at=now - 30)
    _asyncio.run(svc._drain_dm())
    ok(len(svc.host.calls) == 1, "私信：第一条叫醒一次", svc.host.calls)
    second = inbox.add("telegram", "那晚上吃什么", at=now - 20)   # 她这一轮还在跑
    sent_ok, reply = _asyncio.run(svc.send("telegram", "在的，刚忙完"))
    ok(sent_ok, "回第一条：发出去了", reply)
    states = {i["id"]: i["state"] for i in inbox.load()["items"]}
    ok(states.get(first["id"]) == "answered", "第一条：她回过了 → answered", states)
    ok(states.get(second["id"]) != "answered",
       "她没见过的第二条：不算回过（不再被静默吃掉，等她自己决定回不回）", states)
    ok(states.get(second["id"]) == "notified",
       "第二条：附在回话里给她看了 → notified（这一轮想回就再发一条）", states)

    # 2) 回话里带着新来的消息，并且这些被标成 notified
    ok("他又发来了" in reply, "回话里带着「发这条的时候，他又发来了」", reply)
    ok("那晚上吃什么" in reply, "回话里带着第二条的原话", reply)
    states2 = {i["id"]: i["state"] for i in inbox.load()["items"]}
    ok(states2.get(second["id"]) == "notified", "附在回话里之后：那条标成 notified", states2)

    # 3) 她看到后再发一条：这些变 answered
    sent_ok2, _ = _asyncio.run(svc.send("telegram", "吃面吧"))
    ok(sent_ok2, "看到之后再发一条：发得出去")
    states3 = {i["id"]: i["state"] for i in inbox.load()["items"]}
    ok(states3.get(second["id"]) == "answered", "第二条：她回了 → answered", states3)

    # 再来一轮 drain 不该有新叫醒（都回过了）
    before = len(svc.host.calls)
    _asyncio.run(svc._drain_dm())
    ok(len(svc.host.calls) == before, "全部回过之后：不再叫醒", svc.host.calls)

    # 4) 私信 1 秒内连来 3 条：只叫醒一次（合批窗口）
    svc2 = service()
    for i, text in enumerate(("一", "二", "三")):
        inbox.add("telegram", "连发%s" % text, at=now - 10 + i)
    wait = chat_bridge.dm_wait_seconds(inbox.pending("telegram"), now - 10 + 3)
    ok(wait > 0, "最后一条还没安静 2.5 秒：先等", wait)
    ok(chat_bridge.dm_wait_seconds([{"at": now - 100}, {"at": now - 99}], now) <= 6.0,
       "从第一条算最多等 6 秒")
    # 三条都在 2.5 秒安静窗口之后：一次叫醒，三条都在同一条通知里
    _asyncio.run(svc2._drain_dm())
    ok(len(svc2.host.calls) == 1, "1 秒内连来 3 条：只叫醒一次", svc2.host.calls)
    notice = svc2.host.calls[0][1]
    ok(all(("连发" + x) in notice for x in ("一", "二", "三")), "三条都在同一条通知里", notice[:120])

    # 5) 群里同样不丢
    svc3 = service()
    cfg = svc3.cfg
    svc3.cfg = dict(cfg, dc_owner="1508482140570914990", dc_channel="1566804961218666671")

    class FakeDCGroup(FakeDC):
        def __init__(self):
            self.texts = []

        async def send_group(self, text, sticker=None):
            self.texts.append(text)

    async def budget():
        return {"remaining": 8, "used": 1, "owner_last_at": None}

    svc3.dc = FakeDCGroup()
    svc3.refresh_budget = budget
    g1 = inbox.add("discord_group", "群里第一条", at=now - 30, author="指挥官")
    _asyncio.run(svc3._drain_group())
    ok(len(svc3.host.calls) == 1, "群里：第一条叫醒一次", svc3.host.calls)
    g2 = inbox.add("discord_group", "群里第二条", at=now - 20, author="指挥官")
    sent_ok3, reply3 = _asyncio.run(svc3.send("discord_group", "来了"))
    ok(sent_ok3, "群里回一条：发出去了", reply3)
    states4 = {i["id"]: i["state"] for i in inbox.load()["items"]}
    ok(states4.get(g2["id"]) in ("unread", "notified"), "群里第二条没被静默吞掉", states4)
    ok("群里又有人说话了" in reply3, "群里回话带着新消息提示", reply3)
    ok("群里第二条" in reply3, "群里回话带着第二条原话", reply3)

def check_typing():
    """叫醒这一轮显示「正在输入」：会续、结束就停、失败不影响收发（2026-11-02）。"""
    import asyncio as _asyncio
    import time as _time
    reset()

    class FakeHost:
        def __init__(self, delay=0.0):
            self.calls = []
            self.delay = delay

        async def chat(self, session, text):
            self.calls.append((session, text))
            await _asyncio.sleep(self.delay)
            return "ok"

    class FakeTG:
        def __init__(self, boom=False):
            self.actions = []
            self.boom = boom

        async def send_chat_action(self, action="typing"):
            self.actions.append(action)
            if self.boom:
                raise RuntimeError("synthetic typing failure")

    class FakeDC:
        ready = True
        client = object()

        def channel_name(self):
            return "albion"

        def owner_name_set(self):
            return set()

        async def typing(self, group=False, whisper=False):
            return _NullCtx()

    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def service(delay=0.0, boom=False):
        cfg = chat_bridge.load_config(environ={"API_SERVER_KEY": "k"}, home=os.path.join(_STATE, "nohome"))
        svc = chat_bridge.Service(cfg)
        svc.host = FakeHost(delay)
        svc.dc = FakeDC()
        svc.tg = FakeTG(boom)
        return svc

    old_refresh = chat_bridge.TYPING_REFRESH_S
    chat_bridge.TYPING_REFRESH_S = 0.01            # 让「续」在测试里跑得出来
    try:
        # 1) 叫醒期间 typing 被调用过，而且会续
        svc = service(delay=0.06)
        inbox.add("telegram", "在忙吗", at=_time.time())
        _asyncio.run(svc._drain_dm())
        ok(len(svc.host.calls) == 1, "叫醒照常发生", svc.host.calls)
        ok(len(svc.tg.actions) >= 2, "叫醒期间「正在输入」会续（不止一次）", svc.tg.actions)
        ok(all(a == "typing" for a in svc.tg.actions), "默认是 typing", svc.tg.actions)

        # 2) 这一轮结束后停止
        n = len(svc.tg.actions)
        _asyncio.run(_asyncio.sleep(0.08))
        ok(len(svc.tg.actions) == n, "这一轮结束后不再续（状态停掉，不残留）", (n, len(svc.tg.actions)))

        # 3) typing 抛异常：notify 照常完成
        svc2 = service(delay=0.0, boom=True)
        inbox.add("telegram", "第二条", at=_time.time())
        _asyncio.run(svc2._drain_dm())
        ok(len(svc2.host.calls) == 1, "typing 失败时叫醒照常完成", svc2.host.calls)
        states = {i["text"]: i["state"] for i in inbox.load()["items"]}
        ok(states.get("第二条") == "notified", "typing 失败也不影响叫醒状态", states)

        # 4) 群里也一样（Discord 用 channel.typing()，没有就退化成空上下文）
        svc3 = service()
        cfg = svc3.cfg
        svc3.cfg = dict(cfg, dc_owner="1508482140570914990", dc_channel="1566804961218666671")

        async def budget():
            return {"remaining": 8, "used": 1, "owner_last_at": None}

        svc3.refresh_budget = budget
        inbox.add("discord_group", "群里说话", at=_time.time(), author="指挥官")
        _asyncio.run(svc3._drain_group())
        ok(len(svc3.host.calls) == 1, "群里叫醒照常发生", svc3.host.calls)
    finally:
        chat_bridge.TYPING_REFRESH_S = old_refresh

def check_whisper():
    """悄悄话（2026-11-02）：入箱、说话人、叫醒文本逐字、剩 0 拒发、不占群额度、找不到频道不启用、/whisper_budget。"""
    import asyncio as _asyncio
    import time as _time

    reset()
    cfg = chat_bridge.load_config(environ={"API_SERVER_KEY": "k"}, home=os.path.join(_STATE, "nohome"))
    cfg = dict(cfg, dc_owner="1508482140570914990", dc_channel="1566804961218666671",
               dc_whisper="777000111")
    SELF_ID, PEER_ID = "1566791156824805446", "1566802611611504680"

    class _Author:
        def __init__(self, uid, bot=False, name="x", display="x"):
            self.id = uid
            self.bot = bot
            self.name = name
            self.display_name = display
            self.nick = None

    class _Channel:
        def __init__(self, cid, name="", guild=None):
            self.id = cid
            self.name = name
            self.guild = guild

    class _Msg:
        def __init__(self, mid, author, channel, content, clean=None, mentions=()):
            self.id = mid
            self.author = author
            self.channel = channel
            self.content = content
            self.clean_content = clean if clean is not None else content
            self.mentions = list(mentions)

    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    self_user = _Author(SELF_ID, bot=True, name="Albion", display="Albion")
    form = _Author(PEER_ID, bot=True, name="Formidable", display="Formidable")
    owner = _Author(int(cfg["dc_owner"]), bot=False, name="owner_handle", display="OwnerNick")
    other = _Author("999", bot=False, name="someone", display="Someone")
    group = _Channel(int(cfg["dc_channel"]), "常规", guild=object())
    whisper = _Channel(777000111, "悄悄话", guild=object())

    class _FakeDC:
        ready = True

        def __init__(self, whisper_events=None, group_events=None):
            self.client = type("C", (), {"user": self_user})()
            self.whisper_sent, self.group_sent = [], []
            self.whisper_events = list(whisper_events or [])
            self.group_events = list(group_events or [])
            self.typing_calls = []

        def remember(self, member):
            pass

        def name_index(self):
            return {}

        def owner_name_set(self):
            return {"ownernick"}

        def channel_name(self):
            return "常规"

        def whisper_channel_id(self):
            return "777000111"

        def whisper_bot_ids(self):
            return (SELF_ID, PEER_ID)

        def whisper_peer_ids(self):
            return (PEER_ID,)

        def whisper_peer_id(self):
            return PEER_ID

        async def send_whisper(self, text, sticker_path=None):
            self.whisper_sent.append(text)
            return 1

        async def send_group(self, text, sticker_path=None):
            self.group_sent.append(text)
            return 2

        async def send_dm(self, text, sticker_path=None):
            return 3

        def recent_events(self, *a, **k):
            return list(self.group_events)

        def recent_whisper_events(self, *a, **k):
            return list(self.whisper_events)

        async def typing(self, group=False, whisper=False):
            self.typing_calls.append((group, whisper))
            return _NullCtx()

    class FakeHost:
        def __init__(self):
            self.calls = []

        async def chat(self, session, text):
            self.calls.append((session, text))
            return "ok"

    def service(dc=None):
        svc = chat_bridge.Service(cfg)
        svc.dc = dc or _FakeDC()
        svc.schedule_whisper = lambda: None
        svc.schedule_group = lambda: None
        svc.schedule_dm = lambda: None
        return svc

    ok("discord_whisper" in chat_bridge.CHANNELS and "discord_whisper" in inbox.CHANNELS,
       "新渠道 discord_whisper 在收发两边的枚举里")

    # ---- 入箱 + 说话人 ----
    svc = service()
    svc.on_discord_message(_Msg(1, owner, whisper, "在吗", clean="在吗"))
    a = inbox.load()["items"][-1]
    ok(a["channel"] == "discord_whisper" and a["state"] == "unread" and a["needs"] is True,
       "悄悄话：指挥官的话进待回（会叫醒她）", a)
    ok(a["author"] == "指挥官", "悄悄话：指挥官显示成「指挥官」", a["author"])
    svc.on_discord_message(_Msg(2, form, whisper, "在的", clean="在的"))
    b = inbox.load()["items"][-1]
    ok(b["state"] == "unread" and b["needs"] is True, "悄悄话：可畏的话也进待回", b)
    ok(b["author"] == "可畏", "悄悄话：Formidable 显示成「可畏」", b["author"])
    svc.on_discord_message(_Msg(3, self_user, whisper, "我自己的话"))
    ok(not any(i["text"] == "我自己的话" for i in inbox.load()["items"]), "悄悄话：她自己的话不进箱")
    svc.on_discord_message(_Msg(4, other, whisper, "路过", clean="路过"))
    c = inbox.load()["items"][-1]
    ok(c["author"] == "Someone", "悄悄话：其他人照群规则显示")
    ok([i["id"] for i in inbox.pending("discord_whisper")] == [a["id"], b["id"], c["id"]],
       "悄悄话：双方说的话都算待回", inbox.pending("discord_whisper"))
    ok(chat_bridge.whisper_author(form, cfg["dc_owner"], (), (PEER_ID,)) == chat_bridge.WHISPER_PEER_LABEL,
       "按 bot id 认可畏（不靠昵称）")

    # ---- 叫醒文本逐字（20:26 / 20:27 是固定时间戳的北京时间）----
    reset()
    w1 = inbox.add("discord_whisper", "在吗", at=1762000000.0, author="指挥官")
    w2 = inbox.add("discord_whisper", "在的", at=1762000060.0, author="Formidable",
                   from_="bot", why="whisper")
    text = chat_bridge.whisper_notice([w1, w2])
    expected = ("【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：\n"
                "#%d 20:26 指挥官：在吗\n#%d 20:27 可畏：在的\n"
                "想回就用 send_message（channel=discord_whisper）；这里写的字她看不到。" % (w1["id"], w2["id"]))
    ok(text == expected, "悄悄话叫醒文本逐字（开头/每行/结尾）", text)
    zero = {"remaining": 0, "session_used": 20, "day_used": 20, "limit": "session"}
    single = ("【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：\n"
              "#%d 20:26 指挥官：在吗\n"
              "想回就用 send_message（channel=discord_whisper）；这里写的字她看不到。" % w1["id"])
    ok(chat_bridge.whisper_notice([w1], zero) == single + "\n" + chat_bridge.whisper_budget.SESSION_EMPTY,
       "剩 0 时把 note() 原文接在最后一行")
    low = chat_bridge.whisper_notice([w1], {"remaining": 4, "session_used": 16, "limit": "session"})
    ok(low.endswith(chat_bridge.whisper_budget.LOW_NOTE % (16, 4)), "剩 4 时接上收尾提醒原文")

    # ---- 叫醒：10 秒合批、只叫一次、这一轮显示在悄悄话频道 ----
    now = _time.time()
    reset()
    svc = service()
    host = FakeHost()
    svc.host = host
    inbox.add("discord_whisper", "在吗", at=now - 30, author="指挥官")
    _asyncio.run(svc._drain_whisper())
    ok(len(host.calls) == 1, "悄悄话：新消息叫醒一次", host.calls)
    ok(host.calls and host.calls[0][0] == chat_bridge.SESSION_GROUP, "悄悄话叫醒用真会话（长期记忆）")
    ok(host.calls and host.calls[0][1].startswith("【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方"),
       "悄悄话通知开头照定稿", host.calls and host.calls[0][1][:80])
    ok(host.calls and "指挥官：在吗" in host.calls[0][1], "叫醒里带着原话")
    ok(svc.dc.typing_calls and svc.dc.typing_calls[-1] == (False, True),
       "叫醒这一轮「正在输入」显示在悄悄话频道", svc.dc.typing_calls)
    _asyncio.run(svc._drain_whisper())
    ok(len(host.calls) == 1, "悄悄话：她不回也不再叫")

    # ---- 剩 0 拒发；不查群额度 ----
    reset()
    dc = _FakeDC()
    svc = service(dc)
    calls = {"group": 0, "whisper": 0}

    async def group_budget():
        calls["group"] += 1
        return {"remaining": 0, "used": 12}

    async def whisper_budget():
        calls["whisper"] += 1
        return dict(zero)

    svc.refresh_budget = group_budget
    svc.refresh_whisper_budget = whisper_budget
    inbox.add("discord_whisper", "在吗", at=_time.time(), author="可畏", from_="bot", why="whisper")
    sent, msg = _asyncio.run(svc.send("discord_whisper", "晚安"))
    ok(not sent and msg == chat_bridge.whisper_budget.SESSION_EMPTY,
       "悄悄话剩 0：拒发，把 note() 原文作为错误返回", msg)
    ok(dc.whisper_sent == [] and dc.group_sent == [], "悄悄话剩 0：一个字都没发出去")
    ok(calls == {"group": 0, "whisper": 1}, "悄悄话发送不查群额度（不占群额度）", calls)

    # ---- 群额度为 0 也不影响悄悄话 ----
    reset()
    drained = [(PEER_ID, True, now - 60 - i) for i in range(12)]
    dc = _FakeDC(group_events=drained)
    svc = service(dc)
    inbox.add("discord_whisper", "在吗", at=now - 5, author="可畏", from_="bot", why="whisper")
    ok(chat_bridge.group_budget.compute(dc.group_events, now)["remaining"] == 0, "前提：群额度已经 0")
    sent, msg = _asyncio.run(svc.send("discord_whisper", "嗯"))
    ok(sent and dc.whisper_sent == ["嗯"] and dc.group_sent == [],
       "群里没额度时悄悄话照发，且没有发到群里", msg)
    ok(chat_bridge.group_budget.compute(dc.group_events, now)["remaining"] == 0, "悄悄话没动群额度")

    # ---- 出站 @可畏 → 真 mention ----
    bot = chat_bridge.DiscordBot(cfg, lambda m: None)
    bot.ready = True
    bot._whisper_id = "777000111"
    bot.client = type("C", (), {"user": self_user, "get_channel": lambda self, cid: whisper})()
    captured = []

    async def fake_send(channel, text, sticker_path=None):
        captured.append(text)
        return type("M", (), {"id": 1})()

    orig_send = chat_bridge._discord_send
    chat_bridge._discord_send = fake_send
    try:
        _asyncio.run(bot.send_whisper("@可畏 你说呢"))
    finally:
        chat_bridge._discord_send = orig_send
    ok(captured == ["<@%s> 你说呢" % PEER_ID], "悄悄话出站「@可畏」换成真 mention", captured)

    # ---- 找不到频道：整块不启用、只记日志、不报错 ----
    reset()
    bot = chat_bridge.DiscordBot(dict(cfg, dc_whisper=""), lambda m: None)
    bot.ready = True
    bot.client = type("C", (), {"guilds": [], "user": self_user})()
    _asyncio.run(bot.resolve_whisper())
    ok(bot.whisper_channel_id() == "" and bot._whisper_off is True, "找不到「悄悄话」：整块不启用")
    try:
        with open(os.path.join(_STATE, "chat.log"), encoding="utf-8") as f:
            logged = f.read()
    except OSError:
        logged = ""
    ok("whisper_channel_missing" in logged, "找不到频道记一条 whisper_channel_missing")
    svc = service(bot)
    crashed = False
    try:
        svc.on_discord_message(_Msg(9, owner, whisper, "在吗"))
    except Exception:                                 # noqa: BLE001
        crashed = True
    ok(not crashed and inbox.load()["items"] == [], "找不到频道：收到消息不报错、不入箱")
    sent, msg = _asyncio.run(svc.send("discord_whisper", "在吗"))
    ok(not sent and "悄悄话频道没找到" in msg, "找不到频道：发送给出原因，不炸", msg)

    # ---- /whisper_budget：compute() 结果 + note ----
    reset()
    payload = chat_bridge.whisper_budget_payload({"remaining": 3, "session_used": 17,
                                                  "day_used": 17, "limit": "session"})
    ok(payload["ok"] is True and payload["remaining"] == 3 and "收尾" in payload["note"],
       "/whisper_budget 回话＝compute 结果 + note", payload)
    events = [(PEER_ID if i % 2 else SELF_ID, True, now - 30 * (16 - i)) for i in range(16)]
    events.append((cfg["dc_owner"], False, now - 5))          # 指挥官插话不计数
    svc = service(_FakeDC(whisper_events=events))
    budget = _asyncio.run(svc.refresh_whisper_budget())
    ok(budget["remaining"] == 4 and budget["session_used"] == 16,
       "悄悄话额度从频道历史重放（两个 bot 16 条，剩 4）", budget)
    ok(chat_bridge.whisper_budget_payload(budget)["note"] ==
       chat_bridge.whisper_budget.LOW_NOTE % (16, 4), "/whisper_budget 的 note 就是 note() 原文")


def main():
    check_inbox()
    check_owner_and_group()
    check_proactive()
    check_texts()
    check_host_round()
    check_wake_box()
    check_drain()
    check_budget()
    check_group_gate()
    check_whisper()
    check_repeat_send()
    check_live_unread()
    check_typing()
    check_owner_identity()
    check_stickers()
    print("check_chat: %d passed, %d failed" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        shutil.rmtree(_STATE, ignore_errors=True)
    sys.exit(code)
