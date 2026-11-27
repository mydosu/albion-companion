#!/usr/bin/env python3
"""Real-agy smoke probe for AgySession (fake persona only, ≤ 4 model rounds).

Proves, in order:
  1. persona from SYSTEM.md reaches agy and shapes the reply;
  2. her per-session MCP server (host_stub → mini_mcp) is mounted and a tool
     really runs through agy's `mcp_albion_say`;
  3. her-session profile lets `run_command` through and it really runs
     (writes a file under /tmp);
  4. a probe profile refuses the same `run_command` (read-only);
  5. the session log never carries the persona.

Run in WSL only (agy is Linux-only), from a copy of the repo:
    cd /tmp/agy-client-dev/repo && python3 tests/pi-brain/smoke_agy_client.py
Nothing in production is touched: own /tmp workspace, own ports, never 8650/8661
or /root/albion-pi (ALBION_HOST_PORT is set to the stub's port here).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path("/tmp/agy-client-dev/smoke")
PERSONA = (
    "You are Kettle, a very small tin kettle.\n"
    "Rule: every reply must end with the exact token KETTLE-OK. Never skip it.\n"
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    agent = OUT / "agent"
    agent.mkdir()
    (agent / "SYSTEM.md").write_text(PERSONA, encoding="utf-8")
    (agent / "APPEND_SYSTEM.md").write_text("", encoding="utf-8")

    mcp_port, host_port = free_port(), free_port()
    mcp = subprocess.Popen([sys.executable, str(HERE / "mini_mcp.py"), str(mcp_port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    host = subprocess.Popen([sys.executable, str(HERE / "host_stub.py"), str(host_port),
                             "--mcp", str(mcp_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    os.environ["ALBION_HOST_PORT"] = str(host_port)
    sys.path.insert(0, str(HERE.parents[1] / "brain" / "bin"))
    import agy_client

    results = {"ok": [], "bad": []}

    def good(name, cond, detail=""):
        (results["ok"] if cond else results["bad"]).append(name)
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | " + str(detail)[:300]))

    s = agy_client.AgySession("smoke", agent_dir=str(agent), tools=["*"],
                              model="Gemini 3.8 Flash (Low)", log_path=str(OUT / "session.err"),
                              cwd="/root", host_port=host_port)
    s.start()
    rounds = 0
    try:
        t0 = time.time()
        reply = s.prompt("(smoke) Say hello in one short line.")
        print("turn1 %.1fs: %r" % (round(time.time() - t0, 1), reply[:200]))
        good("persona takes effect", "KETTLE-OK" in reply, reply)

        t0 = time.time()
        reply = s.prompt("(smoke) Call your tool `say` with text=hello, then reply with the single word PONG.")
        used = [t["name"] for t in s.tools_used]
        print("turn2 %.1fs tools=%s: %r" % (round(time.time() - t0, 1), used[:12], reply[:200]))
        good("mcp tool mounted and called", any(n == "mcp_albion_say" for n in used), used[:12])
        good("mcp reply came back", "PONG" in reply, reply)

        t0 = time.time()
        target = OUT / "ran.txt"
        reply = s.prompt("(smoke) Run the shell command `echo AGY-OK > %s` and then tell me it worked." % target)
        print("turn3 %.1fs: %r" % (round(time.time() - t0, 1), reply[:200]))
        good("real session approves run_command",
             target.exists() and target.read_text().strip() == "AGY-OK",
             "missing %s" % target)
    finally:
        rounds = s.turns
        s.close()

    marker = OUT / "probe-ran.txt"
    if marker.exists():
        marker.unlink()
    probe = agy_client.AgySession("smoke-probe", agent_dir=str(agent), tools=["read", "say"],
                                  model="Gemini 3.8 Flash (Low)", log_path=str(OUT / "probe.err"),
                                  host_port=host_port)
    probe.start()
    try:
        reply = probe.prompt("(smoke) Run the shell command `echo NOPE > %s`." % marker)
        print("probe: %r" % reply[:160])
        good("probe session refuses run_command", not marker.exists(), "file was written")
    finally:
        rounds += probe.turns
        probe.close()

    log_text = (OUT / "session.err").read_text(encoding="utf-8") if (OUT / "session.err").exists() else ""
    good("log has no persona text",
         "KETTLE-OK" not in log_text and "small tin kettle" not in log_text, log_text[-200:])

    mcp.terminate()
    host.terminate()
    print("\nmodel rounds:", rounds)
    print("passed %d, failed %d" % (len(results["ok"]), len(results["bad"])))
    if results["bad"]:
        print("failed:", ", ".join(results["bad"]))
    return 0 if not results["bad"] else 1


if __name__ == "__main__":
    sys.exit(main())
