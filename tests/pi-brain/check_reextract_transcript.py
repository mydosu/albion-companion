#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重提取脚本的「谁说的话」抽取自检（2026-11-02）。

为什么：平台会话（TG/Discord）里她的回复不走 text part，而是 `send_message` 工具的
`tool_input.text`；早先的 filter_transcript 只留 text part，重提取时她整轮都是哑的，
抽出来的记忆只剩他一边的话。这份自检盯住三件事：
  1) `send_message` 的 `tool_input.text` 要当成她（assistant）说的话保留；
  2) 其它工具调用（带 tool_output 的网页/绘图/读文件…）照旧丢掉，别把工具输出当人话；
  3) 平台通知按宿主规则剥壳后要留下「谁：原话」，整条不含【平台消息】。

跑：python3 tests/pi-brain/check_reextract_transcript.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import reextract_albion_memory as R  # noqa: E402

RULES = [R.parse_rule(r) for r in R.server.CAPTURE_FILTERS]


def rec(role, *parts, created="2026-11-02T14:10:00+00:00"):
    return {"role": role, "parts": list(parts), "created_at": created}


def main() -> int:
    records = [
        # 她被叫宝宝那轮的回复：只在 send_message 里
        rec("assistant", {"type": "tool", "tool_name": "send_message",
                          "tool_input": {"text": "收到啦。突然叫我宝宝什么的……", "channel": "discord"},
                          "tool_status": "running"}),
        # 纯工具轮：网页抓取，没有话
        rec("assistant", {"type": "tool", "tool_name": "browser",
                          "tool_output": "HTTP 200 <html>…", "tool_status": "ok"}),
        # 他说的话（平台通知剥壳后剩下「（Telegram）指挥官：原话」）
        rec("user", {"type": "text", "text": "【平台消息】下面是他刚发来的消息，回不回、怎么回你自己定。\n"
                                             "（Telegram）指挥官：我今晚在吃肯德基"}),
        # 他普通的一句话
        rec("user", {"type": "text", "text": "话说你在吗"}),
        # 工具回执（role=user + tool_output），不是人话
        rec("user", {"type": "tool", "tool_name": "send_message",
                     "tool_output": "{\"ok\": true}", "tool_status": "ok"}),
        # 她自己写的一段文字（text part）
        rec("assistant", {"type": "text", "text": "在的呀。"}),
        # 只有动作标记、没有话的一轮：不算她说过的话
        rec("assistant", {"type": "text", "text": "<|ACT:{\"emotion\":\"happy\",\"face\":\"smile\"}|>"}),
    ]

    kept, dropped, raw_chars, kept_chars = R.filter_transcript(records, RULES)
    roles = [k["role"] for k in kept]
    texts = [k["parts"][0]["text"] for k in kept]

    assert dropped == 3, "应当只丢「纯工具轮」「工具回执」「纯动作标记」三条，实际丢 %d" % dropped
    assert roles == ["assistant", "user", "user", "assistant"], roles
    assert "收到啦" in texts[0], "send_message 里她的话被丢了：%r" % texts[0]
    assert "肯德基" in texts[1] and "【平台消息】" not in texts[1], texts[1]
    assert texts[2] == "话说你在吗", texts[2]
    assert texts[3] == "在的呀。", texts[3]
    assert raw_chars > kept_chars > 0, (raw_chars, kept_chars)

    # user 记录里的 send_message 不算她的话（避免把她的话记成他说的）
    only_user_tool = [rec("user", {"type": "tool", "tool_name": "send_message",
                                   "tool_input": {"text": "不该被当成人话"}})]
    assert R.filter_transcript(only_user_tool, RULES)[0] == [], "user 记录里的 send_message 不该当人话"

    print("REEXTRACT TRANSCRIPT OK：send_message 保留 %d 字、工具输出丢弃、平台通知剥壳留原话（保留 %d 条 / 丢 %d 条）"
          % (len(texts[0]), len(kept), dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
