#!/usr/bin/env python3
"""验"长驻 RPC 会话里每一轮都会发 agent_start"——albion-loopguard 每轮清零的前提。

为什么单独验：loopguard 的计数/写文件记录原来攒在整个会话上，改成 `agent_start` 清零；
如果 RPC 模式一轮不重发 agent_start，这个修法就是空转（上一件活儿的痕迹还会漏到下一件）。
单元自检（bin/test_loopguard.ts 第七件事）是手动喂事件的，这里用**宿主自己那份 pi_client**
起真正的 `pi --mode rpc`，发两轮最简单的话，数事件探针记下的 agent_start。

前置：在 WSL 里跑；号池 127.0.0.1:8000 可用；会花两次模型调用。
用法：python3 rpc_turn_reset_probe.py [--brain /root/albion-pi] [--model grokpool/grok-chat-fast]
"""
import argparse
import json
import os
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default="/root/albion-pi")
    ap.add_argument("--model", default="grokpool/grok-chat-fast")
    ap.add_argument("--fixtures", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
    a = ap.parse_args()

    sys.path.insert(0, os.path.join(a.brain, "bin"))
    from pi_client import PiSession  # 生产同一份客户端

    tap_log = os.path.join(tempfile.gettempdir(), "event-tap-%d.log" % os.getpid())
    if os.path.exists(tap_log):
        os.remove(tap_log)
    os.environ["EVENT_TAP_LOG"] = tap_log

    session = PiSession(
        "dsh-reset-probe",
        agent_dir=os.path.join(a.brain, "agent"),
        cwd="/root",
        model=a.model,
        tools=["bash", "read"],
        extensions=[
            os.path.join(a.fixtures, "event_tap.ts"),
            os.path.join(a.brain, "agent/extensions/albion-loopguard.ts"),
        ],
        log_path=os.path.join(tempfile.gettempdir(), "dsh-reset-probe.err"),
        sandbox_env={"PI_CODING_AGENT_DIR": os.path.join(a.brain, "agent"), "PI_OFFLINE": "1",
                     # 探针起的 pi 也会加载 loopguard：日志写到临时目录，不混进生产日志
                     "ALBION_LOOPGUARD_LOG": os.path.join(tempfile.gettempdir(), "loopguard-reset-probe.log")},
    )
    session.start()
    try:
        r1 = session.prompt("只回复两个字：收到", timeout=300)
        r2 = session.prompt("再回复两个字：好的", timeout=300)
    finally:
        session.close()

    events = []
    if os.path.exists(tap_log):
        for line in open(tap_log, encoding="utf-8"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    starts = [e for e in events if e["event"] == "agent_start"]
    ends = [e for e in events if e["event"] == "agent_end"]
    print("turn1=%r turn2=%r" % (r1, r2))
    print("事件计数：agent_start=%d agent_end=%d turn_start=%d message_end=%d" % (
        len(starts), len(ends),
        sum(1 for e in events if e["event"] == "turn_start"),
        sum(1 for e in events if e["event"] == "message_end")))
    print("agent_start 间隔(ms)：%s" % [starts[i + 1]["at"] - starts[i]["at"] for i in range(len(starts) - 1)])

    problems = []
    if len(starts) != 2:
        problems.append("两轮应当各发一次 agent_start，实际 %d 次" % len(starts))
    if len(ends) < 2:
        problems.append("agent_end 少于两次（%d）" % len(ends))
    if session.turns != 2:
        problems.append("PiSession.turns=%s" % session.turns)
    if problems:
        for p in problems:
            print("[FAIL] " + p)
        return 1
    print("[PASS] 长驻 RPC 里每轮各发一次 agent_start —— loopguard 的每轮清零在真进程里成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
