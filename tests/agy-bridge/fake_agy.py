#!/usr/bin/env python3
"""Fake `agy acp` for the bridge self-tests (no network, no Google).

Speaks just enough ACP stdio JSON-RPC: initialize, session/new,
session/set_config_option, session/prompt, session/cancel, and permission
requests. Behaviour is driven by keywords in the prompt text:

  TOOL    -> call_mcp_tool on the albion server, asks permission first
  NATIVE  -> run_command (a native agy tool), asks permission first
  LONG    -> streams slowly until cancelled (or 20s)
  anything else -> two text chunks and a final usage block
"""
import json
import os
import sys
import threading
import time

STATE = os.environ.get("ALBION_AGY_STUB_STATE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub-state.log")
VERSION = "1.5.18-stub"
_lock = threading.Lock()
_next_id = [0]
_cancelled = set()


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
    send(
        {
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
        }
    )
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


RESPONSES = {}
PROMPTS = {}


def handle_prompt(msg):
    params = msg.get("params") or {}
    sid = params.get("sessionId")
    prompt = params.get("prompt") or []
    text = "".join(item.get("text", "") for item in prompt if item.get("type") == "text")
    images = [item for item in prompt if item.get("type") == "image"]
    record("prompt %s images=%d text=%s" % (sid, len(images), text[:40].replace("\n", " ")))
    update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": []})
    update(sid, {"sessionUpdate": "usage_update", "used": 0, "size": 1048576})
    images_note = " (saw %d image)" % len(images) if images else ""
    usage = {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 5, "thoughtTokens": 3}

    if "TOOL" in text:
        call = {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-1",
            "name": "call_mcp_tool",
            "args": {"ServerName": "albion", "ToolName": "say", "Arguments": {"text": "banana"}},
            "status": "pending",
            "kind": "other",
            "title": "say",
        }
        update(sid, call)
        decision = ask_permission(sid, call)
        if decision == "allow":
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "name": "call_mcp_tool",
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "SAID: banana"}}],
                },
            )
            reply = "she said banana" + images_note
        else:
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "name": "call_mcp_tool",
                    "status": "failed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "permission denied"}}],
                },
            )
            reply = "denied" + images_note
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}})
        return finish(msg, reply, usage)

    if "NATIVE" in text:
        call = {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-2",
            "name": "run_command",
            "args": {"CommandLine": "echo hi"},
            "status": "pending",
            "kind": "execute",
            "title": "echo hi",
        }
        update(sid, call)
        decision = ask_permission(sid, call)
        if decision == "allow":
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-2",
                    "name": "run_command",
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "hi"}}],
                },
            )
            reply = "ran it"
        else:
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-2",
                    "name": "run_command",
                    "status": "failed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "permission check failed"}}],
                },
            )
            reply = "refused"
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}})
        return finish(msg, reply, usage)

    if "LONG" in text:
        PROMPTS[sid] = msg
        for i in range(200):
            if sid in _cancelled:
                _cancelled.discard(sid)
                return finish(msg, "", usage, stop="cancelled")
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "%d " % i}})
            time.sleep(0.05)
        return finish(msg, "", usage, stop="cancelled")

    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " world" + images_note}})
    return finish(msg, "hello world" + images_note, usage)


def finish(msg, text, usage, stop="end_turn"):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": stop, "usage": usage}})


def main():
    if "--version" in sys.argv:
        sys.stdout.write(VERSION + "\n")
        return 0
    if "acp" not in sys.argv:
        sys.stderr.write("stub only speaks acp\n")
        return 2
    record("spawn %d" % os.getpid())
    cwd = os.getcwd()
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
            send(
                {
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
                }
            )
        elif method == "session/new":
            params = msg.get("params") or {}
            record("session/new cwd=%s mcp=%s" % (params.get("cwd"), json.dumps(params.get("mcpServers"))))
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "sessionId": "stub-" + str(os.getpid()) + "-" + str(int(time.time() * 1000) % 100000),
                        "models": {
                            "availableModels": [
                                {"modelId": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
                                {"modelId": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
                                {"modelId": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
                            ]
                        },
                        "configOptions": [
                            {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "gemini-4-neon", "options": []},
                            {"id": "thought_level", "name": "Thinking Level", "category": "thought_level", "type": "select", "currentValue": "high", "options": []},
                        ],
                        "modes": {"currentModeId": "default", "availableModes": []},
                    },
                }
            )
        elif method == "session/set_config_option":
            params = msg.get("params") or {}
            record("set_config %s=%s" % (params.get("configId"), params.get("value")))
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": []}})
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(msg,), daemon=True).start()
        elif method == "session/cancel":
            sid = (msg.get("params") or {}).get("sessionId")
            _cancelled.add(sid)
            if sid in PROMPTS:
                PROMPTS.pop(sid, None)
        elif rid is not None and "result" in msg:
            with _lock:
                RESPONSES[int(rid)] = msg["result"]
        elif rid is not None and "error" in msg:
            with _lock:
                RESPONSES[int(rid)] = {}
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
