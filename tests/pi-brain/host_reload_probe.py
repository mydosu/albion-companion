#!/usr/bin/env python3
"""经宿主（薄壳 8650）验"改扩展/提示词会换身子"这一条，顺便当一轮真实的壳对话。

第二轮改了 `bin/server.py` 的 `ext_signature()`（把 `settings.json`、`APPEND_SYSTEM.md` 也算进去），
但那时线上跑的还是旧进程，所以一直没验成。第四轮重启宿主之后可以真验了。

做法（不叫模型的两步 + 两步真请求）：
  1. 记下 `APPEND_SYSTEM.md` 的 mtime；
  2. 往 `127.0.0.1:8650` POST 一条闲聊（session 固定 `albion-dsh-reload`）→ 看回复、记 pi 子进程 pid；
  3. 只动 `APPEND_SYSTEM.md` 的 mtime（内容一字不改）；
  4. 同一个 session 再 POST 一条 → 再记 pid；
  5. 期望：pid 变了，且 `logs/brain.log` 里出现 "extensions changed, reloading pi session"
     （没换身子的话那里只会是别的行）；
  6. 把 mtime 还原。

用法（在 WSL 里）：python3 host_reload_probe.py --out /tmp/xxx
"""
import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

BRAIN = "/root/albion-pi"
HOST = "http://127.0.0.1:8650"
SESSION = "albion-dsh-reload"
APPEND = os.path.join(BRAIN, "agent", "APPEND_SYSTEM.md")
BRAIN_LOG = os.path.join(BRAIN, "logs", "brain.log")


def api_key():
    for line in open(os.path.join(BRAIN, "agent", ".env"), encoding="utf-8"):
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def post(text, timeout=400):
    body = json.dumps({"model": "albion", "stream": False, "session_id": SESSION,
                       "messages": [{"role": "user", "content": text}]}).encode("utf-8")
    req = urllib.request.Request(HOST + "/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + api_key()})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            return {"http": r.status, "seconds": round(time.time() - t0, 1), "reply": msg.get("content") or ""}
    except Exception as exc:  # noqa: BLE001
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
        return {"http": getattr(exc, "code", 0), "seconds": round(time.time() - t0, 1), "reply": "", "error": str(exc)[:200] + " " + detail}


def host_pid():
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ":8650 " in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return 0


def pi_children():
    """宿主的 pi 子进程。注意：pi 会把进程标题改成 `pi`，cmdline 只剩 "pi"，
    所以按 `pi --mode` 抓是抓不到的（第二轮我因此误判"没有长驻子进程"）。"""
    pid = host_pid()
    if not pid:
        return []
    out = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid=,args="], capture_output=True, text=True).stdout
    return sorted(int(l.split()[0]) for l in out.splitlines()
                  if l.strip() and l.split(None, 1)[-1].strip().startswith("pi"))


def log_tail(n=12):
    try:
        with open(BRAIN_LOG, encoding="utf-8", errors="replace") as f:
            return [l.rstrip() for l in f.readlines()[-n:]]
    except OSError:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--msg1", default="在吗？一句话就好")
    ap.add_argument("--msg2", default="嗯，那就好。再说一句就好")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    facts = {"brain_log_lines_before": len(log_tail(500))}
    before_mtime = os.path.getmtime(APPEND)
    facts["append_mtime_before"] = int(before_mtime)

    facts["pids_idle_before"] = pi_children()
    facts["turn1"] = post(a.msg1)
    facts["pids_after_turn1"] = pi_children()

    # 只动 mtime：内容一字不改，看宿主认不认这个签名
    os.utime(APPEND, None)
    facts["append_mtime_touched"] = int(os.path.getmtime(APPEND))
    time.sleep(0.5)
    facts["turn2"] = post(a.msg2)
    facts["pids_after_turn2"] = pi_children()

    tail = log_tail(60)
    facts["brain_log_tail"] = tail
    facts["mine"] = SESSION
    facts["reload_lines"] = [l for l in tail if "reloading pi session" in l and SESSION in l]
    facts["started_lines"] = [l for l in tail if "started pi session" in l and SESSION in l]
    # 判据：这一轮的会话被重载过（旧子进程活着才会走这条分支，日志里不会给没会话的请求重载）
    pids_changed = facts["pids_after_turn1"] != facts["pids_after_turn2"]
    facts["pids_changed"] = pids_changed
    facts["reloaded"] = bool(facts["reload_lines"]) and (pids_changed or not facts["pids_after_turn1"])

    # 还原 mtime（内容没变，只是别留一个假的新时间）
    os.utime(APPEND, (before_mtime, before_mtime))
    facts["append_mtime_restored"] = int(os.path.getmtime(APPEND)) == int(before_mtime)

    with open(os.path.join(a.out, "host-reload.json"), "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)

    print("换身子：%s" % ("换了" if facts["reloaded"] else "没验到"))
    print("  pid：%s → %s" % (facts["pids_after_turn1"], facts["pids_after_turn2"]))
    print("  重载日志：%s" % (facts["reload_lines"] or "（没有）"))
    print("  第一轮 HTTP %s %.1fs 她说：%s" % (facts["turn1"].get("http"), facts["turn1"].get("seconds", 0),
                                            (facts["turn1"].get("reply") or "").replace("\n", " ⏎ ")[:160]))
    print("  第二轮 HTTP %s %.1fs 她说：%s" % (facts["turn2"].get("http"), facts["turn2"].get("seconds", 0),
                                            (facts["turn2"].get("reply") or "").replace("\n", " ⏎ ")[:160]))
    print("  mtime 还原：%s" % facts["append_mtime_restored"])
    return 0 if facts["reloaded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
