#!/usr/bin/env python3
"""经宿主（薄壳 8650）发一条闲聊，看：①回复；②宿主有没有因为扩展变了而给这个会话换身子。

第五轮 claude 说"改的是扩展和每次现调的 guard.py，她下一次收到消息会自动换上，不用重启宿主"——
这条就是验它：新会话第一次请求时 brain.log 应出现 reload（扩展 mtime 变了），回复要正常。

用法（WSL）：python3 host_chat_probe.py --out /tmp/xxx --text "……" [--session albion-dsh-r5]
"""
import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

BRAIN = "/root/albion-pi"
BRAIN_LOG = os.path.join(BRAIN, "logs", "brain.log")


def api_key():
    for line in open(os.path.join(BRAIN, "agent", ".env"), encoding="utf-8"):
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def host_pid():
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ":8650 " in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return 0


def pi_children():
    pid = host_pid()
    if not pid:
        return []
    out = subprocess.run(["ps", "--ppid", str(pid), "-o", "pid=,args="], capture_output=True, text=True).stdout
    return sorted(int(l.split()[0]) for l in out.splitlines()
                  if l.strip() and l.split(None, 1)[-1].strip().startswith("pi"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--session", default="albion-dsh-r5")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    before = pi_children()
    log_before = 0
    try:
        with open(BRAIN_LOG, encoding="utf-8", errors="replace") as f:
            log_before = len(f.readlines())
    except OSError:
        pass

    body = json.dumps({"model": "albion", "stream": False, "session_id": a.session,
                       "messages": [{"role": "user", "content": a.text}]}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8650/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + api_key()})
    t0 = time.time()
    out = {"text": a.text, "session": a.session, "pids_before": before}
    try:
        with urllib.request.urlopen(req, timeout=400) as r:
            data = json.load(r)
            out["http"] = r.status
            out["reply"] = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as exc:  # noqa: BLE001
        out["http"] = getattr(exc, "code", 0)
        out["error"] = str(exc)[:200]
    out["seconds"] = round(time.time() - t0, 1)
    out["pids_after"] = pi_children()
    out["session_reloaded"] = out["pids_before"] != out["pids_after"]

    try:
        with open(BRAIN_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        new_lines = [l.rstrip() for l in lines[log_before:]]
    except OSError:
        new_lines = []
    out["brain_log_new"] = new_lines
    out["reload_lines"] = [l for l in new_lines if "reloading pi session" in l and a.session in l]

    with open(os.path.join(a.out, "host-chat.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("HTTP %s  %.1fs" % (out["http"], out["seconds"]))
    print("她说：%s" % (out.get("reply") or out.get("error") or "").replace("\n", " ⏎ ")[:300])
    print("换身子（会话级）：%s   子进程 %s → %s" % (out["session_reloaded"], out["pids_before"], out["pids_after"]))
    print("brain.log 新行 %d 条，其中本会话重载 %d 条" % (len(new_lines), len(out["reload_lines"])))
    for l in out["reload_lines"][:1]:
        print("  " + l[:220])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
