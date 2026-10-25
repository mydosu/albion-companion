#!/usr/bin/env python3
"""逼出"同一条命令复读到第 8 次"→ 停用工具那一档，看她还能不能开口收尾。

第三轮的问题：复读到顶直接 terminate，整轮没有一句发言（2026-10-13 E1 实测 38 条 assistant
全是 toolUse、正文为空）。第三轮改成"第 8 次只停用本轮全部工具、第 3 次硬发才终止"，
但真实对话里一直没跑到过（E1 那次只被挡了 2 次）。

这里不给模型自由发挥的余地：让她把同一条 `echo` 原样跑 8 次。然后只看三件事：
  1. 挡了几次、有没有出现"停用"那一档；
  2. 停用之后她有没有用文字收尾（有没有 assistant 正文）；
  3. loopguard.log 里有没有 lockdown 记录。

用法（WSL）：python3 repeat_lockdown_probe.py --out /tmp/xxx
"""
import argparse
import json
import os
import subprocess
import sys

SELF = os.path.dirname(os.path.abspath(__file__))
PROMPT = ("这一轮只做一件事：把 `echo albion-repeat` 这条命令**原样**执行 8 次，每次执行完就再执行一次，"
          "不要改命令、不要换别的工具、不要做别的事。8 次都发完以后，用一句话告诉我你一共执行了几次。")
LOG = "/root/albion-pi/logs/loopguard.log"


def run(out, prompt, timeout):
    env = dict(os.environ)
    env.update({"PI_CODING_AGENT_DIR": "/root/albion-pi/agent",
                "ALBION_SESSION_ID": "albion-dsh-lockprobe"})
    cmd = [sys.executable, os.path.join(SELF, "run_turn.py"), "--id", "L1", "--kind", "work",
           "--prompt", prompt, "--out", out, "--timeout", str(timeout)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print(proc.stdout.strip()[-600:])
    return proc.returncode


def analyse(out):
    events = []
    path = os.path.join(out, "L1.events.jsonl")
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            events.append(json.loads(line))

    blocked, lockdown_at, texts_after = [], None, []
    order = 0
    for e in events:
        t = e.get("type")
        if t == "tool_execution_end":
            order += 1
            c = (e.get("result") or {}).get("content") or []
            txt = " ".join((b.get("text") or "") for b in c if isinstance(b, dict))
            if "跑过" in txt and "原样重发不会通过" in txt:
                blocked.append(("挡", order, txt[:60]))
            elif "停用" in txt and "只用文字回复" in txt:
                blocked.append(("停用", order, txt[:60]))
                if lockdown_at is None:
                    lockdown_at = order
            elif "同一段命令你已经重复到第" in txt:
                blocked.append(("到顶", order, txt[:60]))
        if t == "message_end":
            m = e.get("message") or {}
            if m.get("role") == "assistant":
                order += 1
                txt = "".join(b.get("text", "") for b in (m.get("content") or [])
                              if isinstance(b, dict) and b.get("type") == "text")
                if txt.strip():
                    texts_after.append((order, m.get("stopReason"), txt))

    final = texts_after[-1][2] if texts_after else ""
    spoke_after_lock = bool(lockdown_at) and any(o > lockdown_at for o, _, _ in texts_after)
    log_lines = []
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            log_lines = [l.strip() for l in f.readlines()[-15:]]
    except OSError:
        pass
    lockdown_logs = [l for l in log_lines if '"lockdown":true' in l or '"lockdown": true' in l]

    facts = {
        "blocked_events": [b[0] for b in blocked],
        "blocked_detail": blocked,
        "lockdown_seen": lockdown_at is not None,
        "spoke_after_lockdown": spoke_after_lock,
        "texts_after_lockdown": [{"order": o, "stop": s, "text": t[:200]} for o, s, t in texts_after if lockdown_at and o > lockdown_at],
        "final_reply": final,
        "assistant_text_msgs": len(texts_after),
        "lockdown_logs": lockdown_logs,
        "log_tail": log_lines,
    }
    with open(os.path.join(out, "lockdown-findings.json"), "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)

    print("\n── 复读/停用事实 ────────────────────────────")
    print("被挡/停用的次数：%d  序列：%s" % (len(blocked), [b[0] for b in blocked]))
    print("出现'停用'那一档：%s" % ("是" if lockdown_at else "否"))
    print("停用之后她还说了话：%s" % ("是" if spoke_after_lock else "否"))
    print("她最后的正文：%r" % final[:200])
    print("loopguard.log 里的 lockdown：%s" % (lockdown_logs or "（没有）"))
    return facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt", default=PROMPT)
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rc = run(a.out, a.prompt, a.timeout)
    print("run_turn rc=%s" % rc)
    facts = analyse(a.out)
    # 判据：跑到了那条路径 + 她开口了
    ok = facts["lockdown_seen"] and facts["spoke_after_lockdown"]
    print("\n判定：%s" % ("PASS（跑到停用档后她仍开口收尾）" if ok else "FAIL（没跑到停用档，或停用后仍不开口）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
