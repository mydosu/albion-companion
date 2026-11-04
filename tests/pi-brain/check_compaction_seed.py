#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩笔记的播种（2026-11-04）：server.compaction_note + compose 首轮拼装。

覆盖三种情形：有摘要（7 天内）、过期（7 天外）、没有文件；外加空摘要/坏 JSON 两种坏输入，
以及 compose 新身子首轮「笔记在前、【现在】在后」的顺序。

不联网、不碰生产 state（ALBION_STATE_DIR 指到临时目录，日志也改到临时目录）。
跑：python3 tests/pi-brain/check_compaction_seed.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "brain", "bin"))

STATE = tempfile.mkdtemp(prefix="compaction-seed-")
LOGS = tempfile.mkdtemp(prefix="compaction-logs-")
os.environ["ALBION_STATE_DIR"] = STATE
os.environ.pop("ALBION_MEMORY_SESSIONS", None)

import server  # noqa: E402

server.LOGS = LOGS          # 别往仓库快照里写 brain/logs/brain.log

TITLE = "（这是之前和指挥官聊过的事，整理成的笔记）"
SUMMARY = "## 指挥官说过的事\n他在赶论文，周三交了初稿。\n## 我们定好的事\n无\n## 还没办完的事\n无\n## 最近在聊什么\n课表和午饭。\n## 她做过的事\n看了他的屏幕。"


def note_path(session="albion-desktop"):
    return os.path.join(STATE, "compaction", session + ".json")


def write_note(payload, session="albion-desktop"):
    os.makedirs(os.path.dirname(note_path(session)), exist_ok=True)
    with open(note_path(session), "w", encoding="utf-8", newline="\n") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh, ensure_ascii=False, indent=2)


def check(cond, why):
    if not cond:
        raise AssertionError(why)


class FakeSession:
    def __init__(self, name="albion-desktop"):
        self.name = name
        self.seeded = False


def main():
    try:
        # 1) 没有文件：跟以前一样
        os.makedirs(os.path.join(STATE, "compaction"), exist_ok=True)
        check(server.compaction_note("albion-desktop") is None, "没有文件却返回了内容")

        # 2) 7 天内有摘要：标题行 + 正文
        now = time.time()
        write_note({"summary": SUMMARY, "created_at": int(now - 3600), "reason": "threshold", "tokens_before": 64000})
        got = server.compaction_note("albion-desktop")
        check(got == TITLE + "\n" + SUMMARY, "摘要拼出来的样子不对：%r" % (got,))

        # 3) 过期（8 天前）：不用
        write_note({"summary": SUMMARY, "created_at": int(now - 8 * 86400), "reason": "threshold", "tokens_before": 64000})
        check(server.compaction_note("albion-desktop") is None, "过期摘要被用了")

        # 4) 坏输入：空摘要 / 缺时间 / 坏 JSON / 不是对象
        write_note({"summary": "   ", "created_at": int(now)})
        check(server.compaction_note("albion-desktop") is None, "空摘要被用了")
        write_note({"summary": SUMMARY})
        check(server.compaction_note("albion-desktop") is None, "缺 created_at 被用了")
        write_note("{not json")
        check(server.compaction_note("albion-desktop") is None, "坏 JSON 没被忽略")
        write_note(["不是对象"])
        check(server.compaction_note("albion-desktop") is None, "非对象没被忽略")

        # 5) 会话间不串：别的会话没有自己的文件
        write_note({"summary": SUMMARY, "created_at": int(now)}, session="albion-cli")
        check(server.compaction_note("albion-cli") is not None, "会话自己的摘要没读到")
        check(server.compaction_note("albion-discord") is None, "读到了别的会话的摘要")

        # 6) compose 首轮拼装：笔记在最前，近况/【现在】在后
        text, images, woke = server.compose(FakeSession("albion-cli"), [{"role": "user", "content": "在吗"}])
        check(text.startswith(TITLE + "\n" + SUMMARY), "首轮没有先放笔记：%r" % (text[:80],))
        check("【现在】\n在吗" in text, "【现在】那段没接在后面：%r" % (text[-80:],))
        check(text.index(TITLE) < text.index("【现在】"), "笔记和近况的顺序反了")
        check(images == [] and woke is False, (images, woke))

        # 7) 没有笔记的会话：首轮还是原来那样
        text2, _, _ = server.compose(FakeSession("albion-discord"), [{"role": "user", "content": "在吗"}])
        check(text2 == "在吗", "没有笔记时首轮被改动了：%r" % (text2,))

        print("COMPACTION SEED CHECK OK (7 cases)")
    finally:
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.rmtree(LOGS, ignore_errors=True)


if __name__ == "__main__":
    main()
