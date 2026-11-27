#!/usr/bin/env python3
"""Fake `agy acp` for the AgySession tests (no network, no Google).

Speaks the ACP stdio JSON-RPC subset agy_client uses: initialize, session/new,
session/set_config_option, session/prompt, session/cancel. Behaviour is driven
by keywords in the prompt text:

  TOOL          call mcp_albion_say, ask permission first
  NATIVE        call run_command, ask permission first
  READONLY_TOOL call mcp_albion_web_search, ask permission first
  LONG          stream slowly until cancelled (or 20s)
  ERROR         answer with stopReason "error"
  anything else two text chunks + a final usage block

Every prompt/session/permission decision is appended to $ALBION_AGY_STUB_STATE
so tests can assert what the client actually sent.
"""
import json
import os
import sys
import threading
import time

VERSION = "1.5.18-stub"
STATE = os.environ.get("ALBION_AGY_STUB_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "agy-stub-state.log")
_lock = threading.Lock()
_next_id = [0]
_cancelled = set()
RESPONSES = {}
PROMPTS = {}


def record(text):
    with _lock:
        with open(STATE, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def next_id():
    _next_id[0] += 1
    return _next_id[0]


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def update(session_id, payload):
    notify("session/update", {"sessionId": session_id, "update": payload})


def ask_permission(session_id, tool_call):
    rid = next_id()
    send({
        "jsonrpc": "2.0",
        "id": rid,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": tool_call,
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
            ],
        },
    })
    deadline = time.time() + 10
    while time.time() < deadline:
        with _lock:
            if rid in RESPONSES:
                result = RESPONSES.pop(rid)
                break
        time.sleep(0.02)
    else:
        return "deny"
    outcome = (result.get("outcome") or {}).get("optionId")
    record("decision %s %s" % (tool_call.get("name"), outcome))
    return outcome


def finish(msg, text, usage, stop="end_turn"):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": stop, "usage": usage}})


def handle_prompt(msg):
    params = msg.get("params") or {}
    sid = params.get("sessionId")
    prompt = params.get("prompt") or []
    text = "".join(item.get("text", "") for item in prompt if item.get("type") == "text")
    images = [item for item in prompt if item.get("type") == "image"]
    record("prompt %s images=%d blocks=%d text=%s"
           % (sid, len(images), len(prompt), text[:160].replace("\n", " ")))
    update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": []})
    update(sid, {"sessionUpdate": "usage_update", "used": 0, "size": 1048576})
    usage = {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 5, "thoughtTokens": 3}
    note = " (saw %d image)" % len(images) if images else ""

    def tool_round(name, call_id, args, reply_ok, reply_denied, result_text):
        call = {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "name": name,
            "args": args,
            "status": "pending",
            "kind": "other",
            "title": name,
        }
        update(sid, call)
        decision = ask_permission(sid, call)
        if decision == "allow":
            update(sid, {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "name": name,
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": result_text}}],
            })
            reply = reply_ok
        else:
            update(sid, {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "name": name,
                "status": "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": "permission denied"}}],
            })
            reply = reply_denied
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply + note}})
        finish(msg, reply + note, usage)

    if "READONLY_TOOL" in text:
        return tool_round("mcp_albion_web_search", "call-3", {"query": "x"},
                          "searched", "refused-search", "RESULTS")
    if "TOOL" in text:
        return tool_round("mcp_albion_say", "call-1", {"Text": "banana"},
                          "she said banana", "denied", "SAID: banana")
    if "NATIVE" in text:
        return tool_round("run_command", "call-2", {"CommandLine": "echo hi"},
                          "ran it", "refused", "hi")

    if "LONG" in text:
        PROMPTS[sid] = msg
        for i in range(200):
            if sid in _cancelled:
                _cancelled.discard(sid)
                return finish(msg, "", usage, stop="cancelled")
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "%d " % i}})
            time.sleep(0.05)
        return finish(msg, "", usage, stop="cancelled")

    if "ERROR" in text:
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "boom"}})
        return finish(msg, "", usage, stop="error")

    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " world" + note}})
    return finish(msg, "hello world" + note, usage)


def main():
    if "--version" in sys.argv:
        sys.stdout.write(VERSION + "\n")
        return 0
    if "acp" not in sys.argv:
        sys.stderr.write("stub only speaks acp\n")
        return 2
    record("spawn %d" % os.getpid())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                    },
                    "agentInfo": {"name": "Antigravity ACP Server (stub)", "version": VERSION},
                    "authMethods": [],
                },
            })
        elif method == "session/new":
            params = msg.get("params") or {}
            record("session/new cwd=%s mcp=%s meta=%d"
                   % (params.get("cwd"), json.dumps(params.get("mcpServers")),
                      1 if params.get("_meta") else 0))
            persona = (((params.get("_meta") or {}).get("antigravity") or {}).get("systemPrompt") or {}).get("append")
            if persona:
                record("persona-chars %d" % len(persona))
            send({
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "sessionId": "stub-" + str(os.getpid()) + "-" + str(int(time.time() * 1000) % 100000),
                    "models": {
                        "availableModels": [
                            {"modelId": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
                            {"modelId": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
                            {"modelId": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
                            {"modelId": "gemini-4-neon-high", "name": "Gemini 4 Neon (High)"},
                        ]
                    },
                    "configOptions": [
                        {"id": "model", "name": "Model", "category": "model", "type": "select",
                         "currentValue": "gemini-4-neon", "options": []},
                        {"id": "thought_level", "name": "Thinking Level", "category": "thought_level",
                         "type": "select", "currentValue": "high", "options": []},
                    ],
                    "modes": {"currentModeId": "default", "availableModes": []},
                },
            })
        elif method == "session/set_config_option":
            params = msg.get("params") or {}
            record("set_config %s=%s" % (params.get("configId"), params.get("value")))
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": []}})
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(msg,), daemon=True).start()
        elif method == "session/cancel":
            sid = (msg.get("params") or {}).get("sessionId")
            _cancelled.add(sid)
            PROMPTS.pop(sid, None)
            record("cancel %s" % sid)
        elif rid is not None and "result" in msg:
            with _lock:
                RESPONSES[int(rid)] = msg["result"]
        elif rid is not None and "error" in msg:
            with _lock:
                RESPONSES[int(rid)] = {}
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
