#!/usr/bin/env python3
"""Real-agy smoke probe for brain/agy/bridge.py, with a throwaway persona.

Starts a local mini MCP server, the bridge (with the real `agy`), creates one
session for the fake persona "Kettle", runs one prompt that must call the MCP
tool, then checks cancel on a second turn. Two model calls at most; no persona
or production session is touched.

Usage: python tests/agy-bridge/smoke_real_agy.py [--no-cancel]
"""
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
OUT = ROOT / "tests" / "output" / "agy-bridge-smoke"
BRIDGE = ROOT / "brain" / "agy" / "bridge.py"
PERSONA = (
    "You are Kettle, a small tin kettle used only for bridge smoke tests.\n"
    "Always end every reply with the exact token KETTLE-OK.\n"
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, timeout=120):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data = {}
        return resp.status, data

    def prompt(self, session, text, images=None, cancel_after=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        body = json.dumps({"text": text, "images": images or []}).encode("utf-8")
        conn.request("POST", "/sessions/%s/prompt" % session, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raw = resp.read()
            conn.close()
            return resp.status, [], json.loads(raw.decode("utf-8") or "{}")
        events = []
        started = time.time()
        cancelled = False
        while True:
            if cancel_after and not cancelled and time.time() - started > cancel_after:
                cancelled = True
                self.request("POST", "/sessions/%s/cancel" % session, {})
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if line:
                events.append(json.loads(line.decode("utf-8")))
        conn.close()
        return resp.status, events, {}


def wait_health(client, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data = client.request("GET", "/health", timeout=3)
            if status == 200 and data.get("ok"):
                return data
        except OSError:
            pass
        time.sleep(0.3)
    return None


def main():
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    mcp_port = free_port()
    bridge_port = free_port()
    mcp = subprocess.Popen([sys.executable, str(HERE / "mini_mcp.py"), str(mcp_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ)
    env.update(
        {
            "ALBION_AGY_HOME": str(OUT / "home"),
            "ALBION_AGY_LOG": str(OUT / "bridge.log"),
            "ALBION_AGY_BRIDGE_PORT": str(bridge_port),
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("ALBION_AGY_BIN", None)
    logf = (OUT / "bridge-stdio.log").open("wb")
    bridge = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, stdout=logf, stderr=subprocess.STDOUT)
    client = Client(bridge_port)
    failures = []
    try:
        health = wait_health(client)
        print("health:", health)
        if not health:
            failures.append("health")
            return 1
        status, created = client.request(
            "POST",
            "/sessions",
            {
                "session": "smoke",
                "system_prompt": PERSONA,
                "model": "Gemini 3.8 Flash (Low)",
                "mcp_url": "http://127.0.0.1:%d/mcp/smoke" % mcp_port,
            },
        )
        print("create:", status, created)
        if status != 200:
            failures.append("create")
            return 1

        t0 = time.time()
        status, events, body = client.prompt("smoke", "Call the say tool with text=hello, then reply with the single word PONG. End with KETTLE-OK.")
        elapsed = round(time.time() - t0, 1)
        final = [e for e in events if e.get("type") == "final"]
        starts = [e for e in events if e.get("type") == "tool_start"]
        ends = [e for e in events if e.get("type") == "tool_end"]
        text = final[-1].get("text", "") if final else ""
        print("turn:", elapsed, "s events:", [e.get("type") for e in events])
        print("tools:", [(e.get("name"), e.get("ok")) for e in ends])
        print("text:", text[:400].replace("\n", " "))
        if status != 200 or not final:
            failures.append("prompt")
        if "KETTLE-OK" not in text:
            failures.append("persona")
        if not any(e.get("name") == "say" for e in ends):
            failures.append("mcp tool round trip")
        if not any(s.get("ok") is True for s in ends):
            failures.append("mcp tool permission")

        if "--no-cancel" not in sys.argv:
            t0 = time.time()
            status, events, _ = client.prompt("smoke", "Count slowly from 1 to 60, one number per line. End with KETTLE-OK.", cancel_after=6)
            final = [e for e in events if e.get("type") == "final"]
            print("cancel turn:", round(time.time() - t0, 1), "s stop:", final[-1].get("stop") if final else None)
            if not final or final[-1].get("stop") != "cancelled":
                failures.append("cancel")

        status, body = client.request("POST", "/sessions/smoke/steer", {"text": "x"})
        print("steer:", status)
        if status != 409:
            failures.append("steer")
    finally:
        try:
            client.request("DELETE", "/sessions/smoke")
        except Exception:
            pass
        bridge.terminate()
        try:
            bridge.wait(10)
        except subprocess.TimeoutExpired:
            bridge.kill()
        mcp.terminate()
        logf.close()
    print("\nfailures:", failures or "none")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
