#!/usr/bin/env python3
"""Read-only brain.log latency analysis; never export messages, tool args or credentials.

Run in WSL (the host log uses WSL local time). See README.md for prerequisites.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo = int(index)
    return round(values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo), 3)


def stats(values):
    return {"n": len(values), **({name: percentile(values, q) for name, q in
            [("p50", .5), ("p90", .9), ("p99", .99), ("max", 1)]} if values else {})}


def measure(text, session="albion-desktop"):
    model, models, pending, rows, boot = "unknown", {}, [], [], "unknown"
    counts = Counter()
    for line in text.splitlines():
        match = re.match(r"^(.{19}) ([^{]+)(\{.*\})$", line)
        if not match:
            continue
        stamp, event, raw = match.groups()
        event = event.strip()
        try:
            data = json.loads(raw)
            at = datetime.fromisoformat(stamp).timestamp()
        except (ValueError, TypeError):
            continue
        if event in ("albion brain up", "albion host up (thin)"):
            boot = stamp
            model, models = data.get("model", "unknown"), {}
            counts["interrupted_at_restart"] += len(pending)
            pending = []
        sid = data.get("session")
        if event == "started pi session":
            found = re.search(r"--model (\S+)", data.get("argv", ""))
            if found:
                models[sid] = found[1]
        if sid != session:
            continue
        if event == "request":
            counts["requests"] += 1
            pending.append({"request": stamp, "request_at": at, "boot": boot,
                            "model": models.get(sid, model), "stream": data.get("stream", False),
                            "overlap": bool(pending), "followups": []})
        elif event == "prompt":
            item = next((r for r in reversed(pending) if "prompt_at" not in r), None)
            if item is not None:
                item.update(prompt_at=at, woke=data.get("woke"), model=models.get(sid, model))
        elif "followup" in event and pending:
            pending[-1]["followups"].append(event)
        elif event in ("turn", "brain error", "pi run failed"):
            if not pending:
                counts["unpaired_" + event] += 1
                continue
            if event == "turn":
                # Older logs omit some failed-request terminal events; don't pair those with a later turn.
                started = at - data["ms"] / 1000
                index = min(range(len(pending)), key=lambda i: abs(pending[i]["request_at"] - started))
                counts["unmatched_before_turn"] += index
                pending = pending[index:]
            item = pending.pop(0)
            item.update(end=stamp, end_at=at, request_to_reply_s=at - item["request_at"], outcome=event)
            if "prompt_at" in item:
                item["before_prompt_s"] = item["prompt_at"] - item["request_at"]
            if event == "turn":
                item["turn_s"] = data["ms"] / 1000
                item["chars"] = data.get("chars", 0)
                item["tools"] = dict(Counter(t for t in data.get("tools", []) if isinstance(t, str)))
            else:
                error = str(data.get("error", "")).lower()
                item["error_class"] = next((tag for tag in ["403", "429", "settle", "timeout", "busy"]
                                             if tag in error), "other")
            rows.append(item)
    counts["pending_at_eof"] = len(pending)
    return rows, dict(counts)


def attach_tools(rows, path, session):
    # Activity rows: user/assistant are still stamped together at agent_end, but since 2026-10-25 each tool row
    # carries its own completion time and sits before its turn's user row (old rows: tools after the user row,
    # same timestamp). Walk in id order: hold tool rows until the next user row stamps its batch; tools sharing a
    # timestamp with an existing batch still join it. Either way a tool row only proves tool presence, not duration.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        metadata = db.execute("SELECT timestamp, role, tool_name FROM messages WHERE session_id=? ORDER BY id",
                              (session,)).fetchall()
    finally:
        # connect() 的 with 只管事务不关连接：Windows 上不显式关，临时目录就删不掉。
        db.close()
    batches = defaultdict(Counter)
    pending = Counter()
    for at, role, name in metadata:
        if role == "user":
            batches[at]  # 这轮没有工具也要有批次键，offset 才对得上
            batches[at].update(pending)
            pending.clear()
        elif role == "tool" and name:
            if at in batches:
                batches[at][name] += 1  # 旧数据：工具行在 user 行后、同一时间戳
            else:
                pending[name] += 1  # 新数据：先记着，挂到下一条 user 行
    by_boot = defaultdict(list)
    for row in rows:
        if row["outcome"] == "turn":
            by_boot[row["boot"]].append(row)
    for group in by_boot.values():
        # The host inherited different local time zones across restarts; DB timestamps are Unix time.
        scores = {offset: sum(any(abs(at - row["end_at"] - offset * 3600) < 2 for at in batches)
                              for row in group) for offset in range(-14, 15)}
        best = max(scores.values())
        offsets = [offset for offset, score in scores.items() if score == best]
        if not best or len(offsets) != 1:
            continue
        offset = offsets[0] * 3600
        for row in group:
            matches = [at for at in batches if abs(at - row["end_at"] - offset) < 2]
            if len(matches) == 1:
                row["activity_time_offset_s"] = offset
                row["tools"] = dict(batches[matches[0]])


def summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["model"]].append(row)
    out = {}
    for model, group in sorted(grouped.items()):
        good = [r for r in group if r["outcome"] == "turn"]
        out[model] = {"requests_completed_or_failed": len(group), "success": len(good),
                      "errors": dict(Counter(r.get("error_class") for r in group if r["outcome"] != "turn"))}
        for label, subset in [("all", good), ("stream", [r for r in good if r["stream"]]),
                              ("wake", [r for r in good if r.get("woke") is True]),
                              ("user", [r for r in good if r.get("woke") is False]),
                              ("since_1016", [r for r in good if r["request"] >= "2026-10-16 00:00:00"])]:
            out[model][label] = {"request_to_reply_s": stats([r["request_to_reply_s"] for r in subset]),
                                 "turn_s": stats([r["turn_s"] for r in subset])}
    return out


def selftest():
    def line(second, event, data):
        return f"2026-10-22 01:00:{second:02} {event} " + json.dumps(data)
    desktop = {"session": "albion-desktop"}
    sample = [line(0, "albion host up (thin)", {"model": "ds"}),
              line(1, "request", {**desktop, "stream": True}),
              line(2, "started pi session", {**desktop, "argv": "pi --model beam"}),
              line(3, "started pi session", {"session": "probe", "argv": "pi --model other"}),
              line(3, "prompt", {**desktop, "woke": False}),
              line(6, "turn", {**desktop, "ms": 4500, "chars": 12}),
              line(7, "request", desktop), line(8, "brain error", {**desktop, "error": "403 secret"}),
              line(9, "request", desktop), line(10, "albion host up (thin)", {"model": "ds"}),
              line(11, "request", desktop), line(12, "turn", {**desktop, "ms": 1000})]
    rows, counts = measure("\n".join(sample))
    assert len(rows) == 3 and counts["interrupted_at_restart"] == 1
    assert rows[0]["model"] == "beam" and rows[0]["request_to_reply_s"] == 5
    assert rows[0]["before_prompt_s"] == 2 and rows[0]["turn_s"] == 4.5
    assert rows[1]["error_class"] == "403" and rows[2]["model"] == "ds"
    assert "secret" not in json.dumps(rows)
    assert percentile([1, 2, 3, 4], .5) == 2.5 and percentile([5], .99) == 5
    assert summarize(rows)["beam"]["success"] == 1
    stale = [line(0, "request", desktop), line(10, "request", desktop),
             line(15, "turn", {**desktop, "ms": 5000})]
    paired, counts = measure("\n".join(stale))
    assert paired[0]["request_to_reply_s"] == 5 and counts["unmatched_before_turn"] == 1
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "activity.db"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp REAL,"
                       " role TEXT, tool_name TEXT)")
            # 旧顺序（2026-10-25 前）：user 行先写，工具行同一时间戳跟在后面；
            # 新顺序：工具行各自带执行时间、排在本轮 user 行之前。
            db.executemany("INSERT INTO messages (session_id, timestamp, role, tool_name)"
                           " VALUES ('albion-desktop', ?, ?, ?)", [
                               (3700, "user", None), (3700, "tool", "look"),
                               (3704, "tool", "look"), (3705, "tool", "read_file"), (3710, "user", None),
                               (200, "user", None), (200, "tool", "look"),
                               (4100, "user", None), (499, "user", None), (501, "user", None),
                               (4200, "tool", "look"),  # 后面没有 user 行，不归任何一轮
                           ])
            db.commit()
        finally:
            db.close()  # Windows 上临时目录要等连接关掉才删得掉
        joined = [{"outcome": "turn", "boot": boot, "end_at": at, "tools": {}}
                  for boot, at in [("a", 100), ("a", 110), ("b", 200), ("ambiguous", 500)]]
        attach_tools(joined, path, "albion-desktop")
        assert joined[0]["activity_time_offset_s"] == 3600 and joined[0]["tools"] == {"look": 1}, "旧顺序：同时间戳工具行进该批"
        assert (joined[1]["activity_time_offset_s"] == 3600
                and joined[1]["tools"] == {"look": 1, "read_file": 1}), "新顺序：工具行攒到下一条 user 行"
        assert joined[2]["activity_time_offset_s"] == 0 and joined[2]["tools"] == {"look": 1}
        assert joined[3]["tools"] == {} and "activity_time_offset_s" not in joined[3], "对不上的轮不乱挂"
    print("LATENCY ANALYZER SELFTEST OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/root/albion-pi"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    if args.out is None:
        parser.error("--out is required unless --selftest")
    raw = (args.root / "logs/brain.log").read_bytes()
    rows, counts = measure(raw.decode("utf-8"))
    attach_tools(rows, args.root / "state/state.db", "albion-desktop")
    result = {"log_md5": hashlib.md5(raw).hexdigest(), "counts": counts,
              "groups": summarize(rows), "rows": rows,
              "caveats": ["request/turn timestamps have 1s resolution; no historical first-chunk timestamp",
                          "activity tool rows share agent_end timestamp; no per-tool elapsed times",
                          "turn.ms anchors pairing; requests with no terminal event are counted separately",
                          "activity timezone offset inferred per host boot; ambiguous batches not joined"]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": counts, "groups": result["groups"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
