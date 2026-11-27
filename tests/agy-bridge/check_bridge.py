#!/usr/bin/env python3
"""Self-test for brain/agy/bridge.py against a fake agy (no network, no Google).

Usage: python tests/agy-bridge/check_bridge.py
Exit code 0 when every check passes.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "tests" / "output" / "agy-bridge"
BRIDGE = ROOT / "brain" / "agy" / "bridge.py"

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:300]) if detail and not ok else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, timeout=90):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data = {"raw": raw.decode("utf-8", "replace")}
        return resp.status, data

    def prompt(self, session, text, images=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        body = json.dumps({"text": text, "images": images or []}).encode("utf-8")
        conn.request("POST", "/sessions/%s/prompt" % session, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        events = []
        if status != 200:
            raw = resp.read()
            conn.close()
            return status, events, json.loads(raw.decode("utf-8") or "{}")
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line.decode("utf-8")))
        conn.close()
        return status, events, {}


def wait_health(client, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data = client.request("GET", "/health", timeout=3)
            if status == 200 and data.get("ok"):
                return data
        except OSError:
            pass
        time.sleep(0.2)
    return None


def main():
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    state = OUT / "stub-state.log"
    home = OUT / "home"
    log_path = OUT / "bridge.log"
    port = free_port()

    env = dict(os.environ)
    env.update(
        {
            "ALBION_AGY_BIN": str(HERE / "fake_agy.py"),
            "ALBION_AGY_HOME": str(home),
            "ALBION_AGY_LOG": str(log_path),
            "ALBION_AGY_BRIDGE_PORT": str(port),
            "ALBION_AGY_STUB_STATE": str(state),
            "PYTHONUNBUFFERED": "1",
        }
    )
    logf = (OUT / "bridge-stdio.log").open("wb")
    mcp_port = free_port()
    mcp = subprocess.Popen([sys.executable, str(HERE / "mini_mcp.py"), str(mcp_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mcp_url = "http://127.0.0.1:%d/mcp/t1" % mcp_port
    proc = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, stdout=logf, stderr=subprocess.STDOUT)
    client = Client(port)
    try:
        health = wait_health(client)
        check("health endpoint answers", health is not None, health)
        if health is None:
            return 1
        check("health reports stub version", health.get("agy_version") == "1.5.18-stub", health.get("agy_version"))

        persona = "You are a test kettle.\nSECRET-PERSONA-LINE\n"
        status, created = client.request("POST", "/sessions", {"session": "t1", "system_prompt": persona, "model": "Gemini 3.8 Flash (Medium)", "mcp_url": mcp_url})
        check("create session", status == 200 and created.get("id") == "t1" and created.get("created") is True, created)

        agent_md = home / "workspace" / "t1" / ".agents" / "agents" / "albion.md"
        text = agent_md.read_text(encoding="utf-8") if agent_md.exists() else ""
        check("agent file has persona", "SECRET-PERSONA-LINE" in text, text[:200])
        check("agent file disables native tools", "tools: []" in text and "mainAgent: true" in text, text[:200])
        check("agent briefed on her mcp tools", "call_mcp_tool" in text and "`say(" in text and "`clock(" in text, text[-400:])
        cfg = json.loads((home / "workspace" / "t1" / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
        check("workspace mcp config carries the session url", cfg.get("mcpServers", {}).get("albion", {}).get("url") == mcp_url, cfg)

        status, events, _ = client.prompt("t1", "hello")
        kinds = [e.get("type") for e in events]
        text = "".join(e.get("text", "") for e in events if e.get("type") == "text_delta")
        final = [e for e in events if e.get("type") == "final"]
        check("prompt streams text_delta", status == 200 and "text_delta" in kinds, kinds)
        check("prompt final text", bool(final) and final[-1].get("text") == "hello world", final)
        check("prompt final usage", bool(final) and final[-1].get("usage", {}).get("input") == 100, final)

        spawns_before = state.read_text(encoding="utf-8").count("spawn ")
        status, again = client.request("POST", "/sessions", {"session": "t1", "system_prompt": persona, "model": "Gemini 3.8 Flash (Medium)", "mcp_url": mcp_url})
        spawns_after = state.read_text(encoding="utf-8").count("spawn ")
        check("same settings reuse the session", again.get("created") is False and spawns_after == spawns_before, (again, spawns_before, spawns_after))

        status, rebuilt = client.request("POST", "/sessions", {"session": "t1", "system_prompt": persona + "changed\n", "model": "Gemini 3.8 Flash (Medium)", "mcp_url": mcp_url})
        spawns_after2 = state.read_text(encoding="utf-8").count("spawn ")
        check("changed persona rebuilds the session", rebuilt.get("created") is True and spawns_after2 == spawns_after + 1, (rebuilt, spawns_after, spawns_after2))

        status, events, _ = client.prompt("t1", "please TOOL now")
        starts = [e for e in events if e.get("type") == "tool_start"]
        ends = [e for e in events if e.get("type") == "tool_end"]
        check("mcp tool renamed in tool_start", bool(starts) and starts[0].get("name") == "say" and starts[0].get("args", {}).get("text") == "banana", starts)
        check("mcp tool_end ok with result", bool(ends) and ends[0].get("ok") is True and "SAID: banana" in ends[0].get("result", ""), ends)
        decisions = state.read_text(encoding="utf-8")
        check("her mcp tool permission allowed", "decision call_mcp_tool allow" in decisions, decisions[-300:])

        status, events, _ = client.prompt("t1", "please NATIVE now")
        starts = [e for e in events if e.get("type") == "tool_start"]
        ends = [e for e in events if e.get("type") == "tool_end"]
        check("native tool call surfaced", bool(starts) and starts[0].get("name") == "run_command", starts)
        check("native tool denied", bool(ends) and ends[0].get("ok") is False, ends)
        decisions = state.read_text(encoding="utf-8")
        check("native tool permission denied", "decision run_command deny" in decisions, decisions[-300:])

        status, events, _ = client.prompt("t1", "show me a picture", images=[{"mime": "image/png", "data": "aGVsbG8="}])
        check("image content forwarded", status == 200 and any("saw 1 image" in (e.get("text") or "") for e in events), events)
        check("stub saw one image", "images=1" in state.read_text(encoding="utf-8"), "")

        status, body = client.request("POST", "/sessions/t1/steer", {"text": "wait"})
        check("steer returns 409", status == 409, (status, body))

        result = {}

        def long_turn():
            result["turn"] = client.prompt("t1", "LONG please")

        th = threading.Thread(target=long_turn, daemon=True)
        th.start()
        time.sleep(1.0)
        status, body = client.request("POST", "/sessions/t1/prompt", {"text": "second"})
        check("second prompt while busy is 409", status == 409, (status, body))
        status, body = client.request("POST", "/sessions/t1/cancel", {})
        check("cancel accepted", status == 200 and body.get("cancelled") is True, body)
        th.join(timeout=30)
        events = result.get("turn", (0, [], {}))[1]
        finals = [e for e in events if e.get("type") == "final"]
        check("cancel ends the turn as cancelled", bool(finals) and finals[-1].get("stop") == "cancelled", finals)
        check("cancel produced some text first", any(e.get("type") == "text_delta" for e in events), events[:3])

        workspace = home / "workspace" / "t1"
        status, body = client.request("DELETE", "/sessions/t1")
        time.sleep(0.5)
        check("delete removes the session", status == 200 and body.get("ok") is True, body)
        check("delete removes the workspace (persona)", not workspace.exists(), str(workspace))

        log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        check("log never contains the persona", "SECRET-PERSONA-LINE" not in log_text, log_text[-200:])
        check("log records permission decisions", "permission" in log_text, "")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mcp.terminate()
        logf.close()

    print("\npassed %d, failed %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
