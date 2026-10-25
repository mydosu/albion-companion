#!/usr/bin/env python3
"""端到端实测：向本机大脑发两个回合，检查人格化发言格式与多轮连续性。

用法：ALBION_BRAIN_PORT=8651 python3 bin/test_brain.py
"""
import json
import os
import re
import sys
import time
import urllib.request

PORT = int(os.environ.get("ALBION_BRAIN_PORT", "8650"))
KEY = os.environ.get("API_SERVER_KEY") or ""
BASE = "http://127.0.0.1:%d" % PORT


def post(msgs, timeout=300):
    body = json.dumps({"model": "albion", "messages": msgs}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body, method="POST", headers=headers)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"], time.time() - t0


EMOTIONS = {"happy", "sad", "angry", "think", "surprised", "awkward", "question", "curious", "neutral"}


def assert_act(name, reply):
    m = re.match(r'^<\|ACT:(\{[^\n]*?\})\|>', reply)
    assert m, "%s: no ACT tag at start: %r" % (name, reply[:80])
    act = json.loads(m.group(1))
    assert act.get("emotion") in EMOTIONS, "%s: bad emotion: %r" % (name, act)


def main():
    msgs = [{"role": "user", "content": "[2026-10-07 03:40] 我叫老马，记一下。"}]
    r1, t1 = post(msgs)
    print("turn1 (%.1fs): %s" % (t1, r1))
    msgs += [{"role": "assistant", "content": r1},
             {"role": "user", "content": "[2026-10-07 03:41] 刚才我说我叫什么？只回复名字。"}]
    r2, t2 = post(msgs)
    print("turn2 (%.1fs): %s" % (t2, r2))
    assert_act("turn1", r1)
    assert_act("turn2", r2)
    assert "老马" in r2, "no multi-turn continuity: %r" % r2
    print("BRAIN E2E OK")


if __name__ == "__main__":
    main()
