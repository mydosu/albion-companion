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
    ok(bot and bot["state"] == "closed", "别人的话只当上下文（closed）")
    ok([i["id"] for i in inbox.pending()] == [1], "pending 只剩他发来的未了结的")
    ok([i["id"] for i in inbox.pending("telegram")] == [1] and inbox.pending("discord") == [], "pending 可按渠道过滤")
    inbox.mark_notified([1], now)
    ok(inbox.pending()[0]["state"] == "notified" and inbox.load()["items"][0]["notified_n"] == 1, "标 notified 记账")
    inbox.mark_answered([1], now)
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
    ok(chat_bridge.group_should_wake(42, "42", False) == "owner", "群里他发言要叫醒")
    ok(chat_bridge.group_should_wake(9, "42", True) == "mention", "群里 @她 要叫醒")
    ok(chat_bridge.group_should_wake(9, "42", False) == "", "别的 bot 闲聊不叫醒")
    items = [{"id": 1, "at": 100.0}, {"id": 2, "at": 103.0}]
    ok(chat_bridge.group_wait_seconds(items, 101.0, 0.0) == 9.0, "合批：最老那条满 10 秒才叫")
    ok(chat_bridge.group_wait_seconds(items, 200.0, 0.0) == 0.0, "过了窗口立刻叫")
    ok(chat_bridge.group_wait_seconds(items, 200.0, 150.0) == 70.0, "同频道 120 秒内不再叫")
    ok(chat_bridge.group_wait_seconds([], 200.0, 150.0) == 0.0, "没有要叫的就别等")


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
    g = chat_bridge.group_notice("albion-群", [inbox.add("discord_group", "晚上好", at=now, author="机器人甲")])
    ok(g.startswith(wake.PLATFORM_MARKER + "Discord 群「albion-群」里有新消息"), "群通知轮抬头")
    ok("群里大家都看得到" in g and "机器人甲：晚上好" in g and "channel=discord_group" in g, "群通知轮内容")
    ok("这里写的字群里看不到" in g, "群通知轮结尾")
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
    prompt = wake.wake_prompt(now)
    ok("还没回的消息（他发来的）：" in prompt, "醒来提示里有未回消息段")
    ok(("#%s " % a["id"]) in prompt and "在忙吗" in prompt and "Telegram" in prompt, "念出编号/渠道/原文")
    ok("睡了吗" not in prompt, "回过的（answered）不再念")
    ok("晚上好" not in prompt, "群里别人的话不念（只当上下文）")
    ok("这一轮把它们了结" in prompt and "不回也行" in prompt, "这一轮了结的说法照定稿")
    ok(wake.load()["inbox_shown"] == [a["id"]], "记下这一轮念了哪几条")
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


def main():
    check_inbox()
    check_owner_and_group()
    check_proactive()
    check_texts()
    check_host_round()
    check_wake_box()
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
