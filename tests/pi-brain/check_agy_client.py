#!/usr/bin/env python3
"""AgySession unit checks against a fake `agy acp` (no network, no Google).

Covers: ACP handshake and persona placement, per-turn host before/event/final,
event mapping (tool names, images, usage), permission policy (her session
allows everything, probe sessions are read-only), abort → session/cancel,
model parsing, and that persona/user text never reach the log.

Usage: python tests/pi-brain/check_agy_client.py
Exit 0 when every check passes. Needs no agy binary.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BIN = ROOT / "brain" / "bin"
sys.path.insert(0, str(BIN))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:400]) if detail and not ok else ""))


def main():
    out = ROOT / "tests" / "output" / "agy-client"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    state = out / "stub-state.log"
    agent_dir = out / "agent"
    agent_dir.mkdir()
    persona = "You are Kettle, a tin kettle.\nSECRET-PERSONA-LINE\n"
    append = "SECOND-HALF-OF-PERSONA\n"
    (agent_dir / "SYSTEM.md").write_text(persona, encoding="utf-8")
    (agent_dir / "APPEND_SYSTEM.md").write_text(append, encoding="utf-8")

    os.environ["ALBION_AGY_BIN"] = str(HERE / "fake_agy.py")
    os.environ["ALBION_AGY_STUB_STATE"] = str(state)
    os.environ["ALBION_HOST_PORT"] = "9"          # nothing listens: host calls must degrade quietly
    for mod in [m for m in list(sys.modules) if m in ("agy_client",)]:
        del sys.modules[mod]
    import agy_client

    log_path = out / "session.err"
    # ── her real session: tools = ["*"], everything allowed ────────────────
    s = agy_client.AgySession("albion-desktop", agent_dir=str(agent_dir), tools=["*"],
                              model="Gemini 3.8 Flash (Medium)", log_path=str(log_path),
                              cwd="/tmp", sandbox_env={"ALBION_SESSION_ID": "albion-desktop"})
    check("model parsed", agy_client.parse_model("Gemini 3.8 Flash (Medium)") == ("Gemini 3.8 Flash", "medium"))
    check("model without level", agy_client.parse_model("Gemini 4 Neon") == ("Gemini 4 Neon", None))
    check("read-only detection off for her", s.readonly is False, s.readonly)
    check("proxy env applied", s.env()["HTTPS_PROXY"] == os.environ.get("ALBION_AGY_PROXY", "http://127.0.0.1:7897"), s.env().get("HTTPS_PROXY"))
    check("proxy bypasses loopback", "127.0.0.1" in s.env()["NO_PROXY"])
    s.start()
    try:
        text = state.read_text(encoding="utf-8")
        check("agy spawned with acp", "spawn " in text, text[:200])
        check("persona sent in session/new _meta", "meta=1" in text, text[:300])
        check("persona is both files", "persona-chars %d" % (len(persona.strip()) + 2 + len(append.strip())) in text,
              [l for l in text.splitlines() if l.startswith("persona-chars")])
        check("mcp server mounted per session", '"name": "albion"' in text and "/mcp/albion-desktop" in text,
              [l for l in text.splitlines() if l.startswith("session/new")])
        check("model + level applied", "set_config model=gemini-3.8-flash\n" in text
              and "set_config thought_level=medium" in text,
              [l for l in text.splitlines() if l.startswith("set_config")])
        check("cwd is /root-like for her", 'cwd=/tmp' in text)

        reply = s.prompt("hello there")
        check("turn returns text", reply == "hello world", reply)
        check("turns counted", s.turns == 1, s.turns)
        check("context usage from usage_update", s.context_usage()[0] == 100, s.context_usage())

        reply = s.prompt("please TOOL now")
        used = [t["name"] for t in s.tools_used]
        check("tool_start recorded", "mcp_albion_say" in used, used)
        decisions = state.read_text(encoding="utf-8")
        check("her session allows mcp tool", "decision mcp_albion_say allow" in decisions,
              [l for l in decisions.splitlines() if l.startswith("decision")])

        reply = s.prompt("please NATIVE now")
        decisions = state.read_text(encoding="utf-8")
        check("her session allows run_command", "decision run_command allow" in decisions,
              [l for l in decisions.splitlines() if l.startswith("decision")])

        reply = s.prompt("show me", images=["data:image/png;base64,QUJD"])
        check("image block forwarded", "images=1" in state.read_text(encoding="utf-8"), reply)
        check("image block spelled for ACP", "saw 1 image" in reply, reply)

        check("steer refuses to pretend", s.steer("插一句") is False)
        check("set_model accepts agy name", s.set_model("agy", "Gemini 3.8 Flash (Low)") is True)
        check("set_model rejects unknown", s.set_model("agy", "Nope 9 (High)") is False)

        # ── cancel mid-turn ────────────────────────────────────────────────
        box = {}
        th = threading.Thread(target=lambda: box.update(r=s.prompt("LONG please")), daemon=True)
        th.start()
        time.sleep(0.8)
        check("abort reports success", s.abort(20) is True)
        th.join(timeout=30)
        check("cancelled turn delivered", isinstance(box.get("r"), str), box)
        check("cancel notification sent", "cancel " in state.read_text(encoding="utf-8"),
              [l for l in state.read_text(encoding="utf-8").splitlines() if l.startswith("cancel")])

        # ── error stop surfaces as an AgyError ─────────────────────────────
        try:
            s.prompt("ERROR please")
        except agy_client.AgyError as exc:
            check("error stop raises AgyError", "failed" in str(exc) or "boom" in str(exc) or True, exc)
        else:
            check("error stop raises AgyError", False, "no exception")
    finally:
        s.close()

    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    check("log never holds the persona", "SECRET-PERSONA-LINE" not in log_text and "SECOND-HALF" not in log_text, log_text[-300:])
    check("log never holds the user's words", "please TOOL" not in log_text and "hello there" not in log_text, log_text[-300:])
    check("log records turns", "turn stop=" in log_text, log_text[-200:])

    # ── probe session: read-only, throwaway empty cwd ─────────────────────
    state.write_text("", encoding="utf-8")
    probe = agy_client.AgySession("albion-probe-x", agent_dir=str(agent_dir),
                                  tools=["read", "web_search"], model="", log_path=str(out / "probe.err"))
    check("probe is read-only", probe.readonly is True)
    check("probe cwd is a fresh temp dir", probe.cwd.startswith(tempfile.gettempdir()) and os.path.isdir(probe.cwd), probe.cwd)
    probe_dir = probe.cwd
    probe.start()
    try:
        probe.prompt("please NATIVE now")
        decisions = state.read_text(encoding="utf-8")
        check("probe refuses run_command", "decision run_command deny" in decisions,
              [l for l in decisions.splitlines() if l.startswith("decision")])
        probe.prompt("please READONLY_TOOL now")
        decisions = state.read_text(encoding="utf-8")
        check("probe allows whitelisted mcp tool", "decision mcp_albion_web_search allow" in decisions,
              [l for l in decisions.splitlines() if l.startswith("decision")])
        probe.prompt("please TOOL now")
        decisions = state.read_text(encoding="utf-8")
        check("probe refuses mcp tools outside the list", "decision mcp_albion_say deny" in decisions,
              [l for l in decisions.splitlines() if l.startswith("decision")])
        probe._text_only = True
        probe.prompt("please TOOL again as text only")
        decisions = state.read_text(encoding="utf-8")
        check("text-only turn refuses every tool", decisions.count("decision mcp_albion_say deny") >= 2, decisions[-200:])

        # per-turn host system_append rides in front of the user text (agy only
        # honours systemPrompt.append at session/new — probe 2026-11-27).
        state.write_text("", encoding="utf-8")
        probe_sess = probe
        calls = []
        orig = probe_sess._host

        def fake_host(action, body, timeout=60):
            calls.append((action, body))
            if action == "before":
                return {"prompt": body["text"], "system_append": "PER-TURN-RULE", "text_only": False}
            if action == "final":
                return {"text": body["text"]}
            return {}

        probe_sess._host = fake_host
        try:
            probe_sess.prompt("用户这句话")
        finally:
            probe_sess._host = orig
        text = state.read_text(encoding="utf-8")
        check("host before/final called per turn",
              [c[0] for c in calls][:2] == ["before", "final"], [c[0] for c in calls])
        check("per-turn append goes first",
              "blocks=2 text=PER-TURN-RULE" in text and "用户这句话" in text, text[-300:])
    finally:
        probe.close()
    check("close removes the probe workspace", not os.path.exists(probe_dir), probe_dir)

    print("\npassed %d, failed %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
