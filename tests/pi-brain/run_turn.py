#!/usr/bin/env python3
"""跑一轮真实对话（生产分支），把 `pi --mode json` 的事件带到达时间压成一行摘要。

为什么不用 `pi -p` 的纯文本：纯文本看不到她到底调了哪件工具、工具报没报错、
`read` 返回的是不是真的画面，这些才是本轮要验的东西。

用法（在 WSL 里跑）：
  PI_EXT_ARGS="-e /root/albion-pi/agent/extensions/albion-turn.ts ..." \
  python3 run_turn.py --id T1 --kind chat --prompt "你好" --out /tmp/probe

落盘：
  <out>/<id>.events.jsonl   每行 {"ts": 秒, "event": pi 原样事件}
  <out>/<id>.err            pi 的 stderr
  <out>/turns.jsonl         追加一行摘要（check_speech.py 的输入）
"""
import argparse
import json
import os
import re
import subprocess
import threading
import time


def text_of(content):
    """assistant.content 里会朗读的正文（thinking / toolCall 块不算）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def result_size(result):
    """(字符数, 图片块数)：read 一张图时 result 里应当是 image 块而不是报错文本。"""
    if result is None:
        return 0, 0
    raw = json.dumps(result, ensure_ascii=False)
    images = 0
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    return len(raw), images


def read_state(brain):
    """pi 跑完一轮后，state/last-context.json 里就是这一轮真正注入的原文与约定。"""
    path = os.path.join(brain, "state", "last-context.json")
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return {
            "at": d.get("at"),
            "source": d.get("source", ""),
            "guidance_chars": d.get("guidance_chars"),
            "recall_chars": d.get("recall_chars"),
            "has_marker": "【阿尔比恩本轮说话约定】" in (d.get("block") or ""),
        }
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--kind", required=True, help="chat / work / image / ext")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--brain", default="/root/albion-pi")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--cmd", default="pi")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    ext_args = [x for x in os.environ.get("PI_EXT_ARGS", "").split() if x]
    cmd = [a.cmd, "-p", a.prompt, "--no-session", "--tools", "*", "--mode", "json", *ext_args]

    dump = os.path.join(a.out, "%s.events.jsonl" % a.id)
    err = os.path.join(a.out, "%s.err" % a.id)
    events = []
    t0 = time.time()
    with open(err, "w", encoding="utf-8") as errfh, open(dump, "w", encoding="utf-8") as dumpfh:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfh, text=True, bufsize=1)
        killer = threading.Timer(a.timeout, proc.kill)
        killer.start()
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                dumpfh.write(line + "\n")
                dumpfh.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                events.append((round(time.time() - t0, 2), event))
        finally:
            killer.cancel()
        rc = proc.wait()
    seconds = round(time.time() - t0, 2)
    timed_out = rc != 0 and seconds >= a.timeout - 1

    # ── 从事件里取事实 ────────────────────────────────────────────────
    reply = None
    stop = None
    assistant_msgs = 0
    usage = None
    pending = {}
    calls = []
    for ts, event in events:
        kind = event.get("type")
        if kind == "message_end":
            msg = event.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            assistant_msgs += 1
            text = text_of(msg.get("content"))
            if msg.get("stopReason") == "stop" and text:
                reply, stop = text, msg.get("stopReason")
            elif reply is None:
                reply, stop = text, msg.get("stopReason")
        elif kind == "message_update":
            u = event.get("usage") or {}
            if u.get("input"):
                usage = u
        elif kind == "tool_execution_start":
            pending[event.get("toolCallId")] = (ts, event)
        elif kind == "tool_execution_end":
            cid = event.get("toolCallId")
            start_ts, start = pending.pop(cid, (None, {}))
            chars, images = result_size(event.get("result"))
            args = start.get("args")
            calls.append({
                "name": event.get("toolName"),
                "args": json.dumps(args, ensure_ascii=False)[:500],
                "secs": round(ts - start_ts, 2) if start_ts is not None else None,
                "result_chars": chars,
                "images": images,
                "is_error": bool(event.get("isError")),
            })

    wrote = any(
        c["name"] in ("edit", "write") and not c["is_error"] for c in calls
    )
    summary = {
        "id": a.id,
        "kind": a.kind,
        "prompt": a.prompt,
        "seconds": seconds,
        "rc": rc,
        "timed_out": timed_out,
        "stop": stop,
        "assistant_msgs": assistant_msgs,
        "reply": reply,
        "tool_calls": calls,
        "wrote_file": wrote,
        "usage": usage,
        "injected": read_state(a.brain),
    }
    with open(os.path.join(a.out, "turns.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    print("[%s] rc=%s %.1fs tools=%s reply=%s" % (
        a.id, rc, seconds, ",".join(c["name"] for c in calls) or "-", (reply or "")[:80].replace("\n", " ")))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
