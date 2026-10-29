#!/usr/bin/env python3
"""现实时间观念（2026-10-29）的确定性自检：回忆条目带日期、心事本带“几天前”。

覆盖：
  1) memory.py：events 的 uri 路径取日期 → （MM-DD，今天/昨天/N天前）；没有日期的条目不 gate；坏日期/未来日期不加；
     经 _build_prefetch_entries 拼出来的召回条目里确实带着这一行。
  2) wake.py：_note_item 的「（MM-DD 记，几天前）」；wake_prompt 里那一行也带；没 at 的省掉括号。

跑：python3 tests/pi-brain/check_time_context.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRAIN = os.path.join(ROOT, "integrations", "pi-brain")
sys.path.insert(0, os.path.join(BRAIN, "bin"))

import memory  # noqa: E402
import wake  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def check_memory() -> None:
    today = date(2026, 10, 29)
    cases = [
        ("viking://user/albion/memories/events/2026/10/29/x.md", "（10-29，今天）"),
        ("viking://user/albion/memories/events/2026/10/28/x.md", "（10-28，昨天）"),
        ("viking://user/albion/memories/events/2026/10/26/x.md", "（10-26，3天前）"),
        ("viking://user/albion/memories/events/2026/09/14/x.md", "（09-14，45天前）"),
        ("viking://user/albion/memories/preferences/2026/10/26/x.md", ""),  # 不是 events：没有日期
        ("viking://user/albion/memories/entities/某人.md", ""),
        ("viking://user/albion/memories/events/2026/13/01/x.md", ""),  # 坏日期
        ("viking://user/albion/memories/events/2026/10/30/x.md", ""),  # 未来日期：宁可不说
    ]
    for uri, want in cases:
        got = memory._age_label(memory._memory_date(uri), today)
        assert got == want, "%s → %r，期望 %r" % (uri, got, want)

    # 经条目拼装：日期行在 <uri> 之后、正文之前
    items = [{"uri": "viking://user/albion/memories/events/2026/10/26/x.md", "abstract": "旧事一句",
              "category": "memory", "level": 1}]
    entries = memory._build_prefetch_entries(None, items, prefer_abstract=True, max_injected_chars=10_000,
                                             deadline=0, request_timeout=1, full_read_limit=0)
    assert len(entries) == 1, entries
    lines = entries[0].splitlines()
    assert lines[0] == "- [memory]", lines
    assert lines[1].startswith("  <uri>"), lines
    assert lines[2].strip() == memory._age_label(date(2026, 10, 26), datetime.now(TZ).date()), lines
    print("memory 时间标签 OK（今天/昨天/N天前/无日期/坏日期/未来；条目里带这一行）")


def check_wake() -> None:
    now = datetime(2026, 10, 29, 18, 20, tzinfo=TZ).timestamp()
    def at(y, mo, d, h=12, mi=0):
        return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()
    assert wake._note_item({"id": 16, "text": "旧事", "at": at(2026, 10, 26, 14, 32)}, now) == "#16（10-26 记，3天前）旧事"
    assert wake._note_item({"id": 17, "text": "今事", "at": at(2026, 10, 29, 9)}, now) == "#17（10-29 记，今天）今事"
    assert wake._note_item({"id": 18, "text": "昨事", "at": at(2026, 10, 28, 9)}, now) == "#18（10-28 记，昨天）昨事"
    assert wake._note_item({"id": 19, "text": "没时间"}, now) == "#19 没时间"

    state = tempfile.mkdtemp(prefix="albion-time-")
    os.environ["ALBION_STATE_DIR"] = state
    with open(os.path.join(state, "notebook.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": 16, "text": "旧事", "at": at(2026, 10, 26, 14, 32)},
                   {"id": 30, "text": "没时间的一条"}], f, ensure_ascii=False)
    prompt = wake.wake_prompt(now)
    line = [l for l in prompt.split("\n") if l.startswith("心事本里还开着的")][0]
    assert "#16（10-26 记，3天前）旧事" in line, line
    assert "#30 没时间的一条" in line and "#30（" not in line, line
    print("wake 心事本时间标签 OK（今天/昨天/N天前/没 at；wake_prompt 里也带）")


def main() -> int:
    check_memory()
    check_wake()
    print("TIME CONTEXT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
