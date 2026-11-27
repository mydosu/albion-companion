#!/usr/bin/env python3
"""Albion's Windows agy bridge.

Owns `agy acp` (Agent Client Protocol, stdio JSON-RPC) child processes and
exposes the HTTP/NDJSON interface the WSL brain talks to (design doc:
`.tongxi/任务书/阿尔比恩agy内核.md`, section A).

Hard rules baked in here:

* Every session gets its own empty workspace under
  ``%LOCALAPPDATA%\\albion-agy\\workspace\\<session>``. The persona arrives over
  HTTP, is written to ``.agents/agents/<agent>.md`` and is never logged.
* The custom main agent declares ``tools: []`` so agy's own file/command/web
  tools are not registered at all.
* Each session runs agy with its own ``--gemini-dir`` (``<home>/gemini/<session>``):
  the user's global ``~/.gemini/config`` plugins (hooks), skills and MCP never
  load for her. Her MCP server is declared in that dir's ``config/mcp_config.json``
  (the workspace ``.agents/mcp_config.json`` alone is not picked up there); a tool
  whitelist cannot name MCP tools, so they go through agy's ``call_mcp_tool``.
* Every ACP permission request is answered here: only ``call_mcp_tool`` calls
  whose ``ServerName`` is her MCP server (or a direct ``mcp_<server>_*`` tool)
  are allowed; everything else is denied.

Run: ``python brain/agy/bridge.py`` (listens on 127.0.0.1:8670 only).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("ALBION_AGY_BRIDGE_PORT", "8670"))
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
AGY_BIN = os.environ.get("ALBION_AGY_BIN") or str(_LOCALAPPDATA / "agy" / "bin" / "agy.exe")
AGY_HOME = Path(os.environ.get("ALBION_AGY_HOME") or (_LOCALAPPDATA / "albion-agy"))
WORKSPACE_ROOT = AGY_HOME / "workspace"
# 每个会话一份自己的 .gemini（agy acp --gemini-dir）：用户全局 ~/.gemini/config 里的插件 hooks（openviking-memory、rtk）、
# skills、MCP 不进她的会话；她的 MCP 写进这份的 config/mcp_config.json，各会话的 URL 互不相干。
# 登录凭据在系统 keyring 里、不在这个目录（11-27 实测：独立目录照样认出 Google AI Pro），不用重新登录。
GEMINI_ROOT = AGY_HOME / "gemini"
AGENT_NAME = os.environ.get("ALBION_AGY_AGENT", "albion")
MCP_SERVER_NAME = os.environ.get("ALBION_AGY_MCP_SERVER", "albion")
REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = Path(os.environ.get("ALBION_AGY_LOG") or (REPO_ROOT / "logs" / "agy-bridge.log"))
PROMPT_IDLE_TIMEOUT = float(os.environ.get("ALBION_AGY_PROMPT_TIMEOUT", "600"))
MAX_RESULT_CHARS = 2000
MAX_LOG_BYTES = 2 * 1024 * 1024

_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    """Append one line to the bridge log. Never pass persona or prompt bodies."""
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _LOG_LOCK:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
                try:
                    LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
                except OSError:
                    pass
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass


def short(text, limit=300) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def agy_argv(*args) -> list:
    """Command line for agy, allowing a Python stub in tests (ALBION_AGY_BIN=*.py)."""
    if AGY_BIN.lower().endswith(".py"):
        return [sys.executable, AGY_BIN] + list(args)
    return [AGY_BIN] + list(args)


def ensure_gemini_dir(gemini_dir: Path, mcp_url: str) -> None:
    """把一份会话专用的 .gemini 备齐：她的 MCP、默认项目、信任她的工作区根。"""
    cfg = {"mcpServers": {MCP_SERVER_NAME: {"url": mcp_url}}}
    (gemini_dir / "config").mkdir(parents=True, exist_ok=True)
    (gemini_dir / "config" / "mcp_config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    # 没有默认项目时 agy 不认工作区的 .agents（11-27 对照日志：缺 "Conversation using project ID"）；内容照用户目录里那份
    project = gemini_dir / "config" / "projects" / "default-cli-project.json"
    if not project.exists():
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text(json.dumps({"id": "default-cli-project", "name": "CLI Project", "projectResources": {}}) + "\n", encoding="utf-8")
    settings = gemini_dir / "antigravity-cli" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except (OSError, ValueError):
        data = {}
    trusted = data.get("trustedWorkspaces") if isinstance(data.get("trustedWorkspaces"), list) else []
    root = str(WORKSPACE_ROOT)
    if root in trusted:
        return
    data["trustedWorkspaces"] = trusted + [root]
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def agy_version() -> str:
    try:
        out = subprocess.run(agy_argv("--version"), capture_output=True, timeout=20)
        text = (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()
        return text[0].strip() if text else ""
    except Exception as exc:  # pragma: no cover - depends on installed binary
        return "unknown (%s)" % exc.__class__.__name__


# --------------------------------------------------------------------------- #
# ACP plumbing
# --------------------------------------------------------------------------- #
class AcpError(RuntimeError):
    pass


class AcpProcess:
    """One `agy acp` child: newline-delimited JSON-RPC over stdin/stdout."""

    def __init__(self, cwd: Path, extra_args=None, on_update=None, on_permission=None, on_done=None):
        self.cwd = cwd
        self.on_update = on_update or (lambda upd: None)
        self.on_permission = on_permission or (lambda call: "deny")
        self.on_done = on_done or (lambda: None)
        argv = agy_argv("acp", "--agent", AGENT_NAME) + list(extra_args or [])
        self.argv = argv
        self.proc = None
        self._id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self.alive = False
        self.stderr_tail = []
        self._reader = None
        self._errreader = None
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.alive = True
        self._reader = threading.Thread(target=self._read_stdout, daemon=True, name="agy-out")
        self._reader.start()
        self._errreader = threading.Thread(target=self._read_stderr, daemon=True, name="agy-err")
        self._errreader.start()

    # -- io ---------------------------------------------------------------- #
    def _read_stderr(self):
        try:
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-40]
        except Exception:
            pass

    def _read_stdout(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self._dispatch(msg)
        except Exception as exc:  # reader died with the pipe
            log("acp stdout reader stopped: %s" % exc.__class__.__name__)
        finally:
            self.alive = False
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for slot in pending:
                slot.put(None)
            try:
                self.on_done()
            except Exception:
                pass

    def _dispatch(self, msg):
        method = msg.get("method")
        if method:
            if "id" in msg:
                self._handle_request(msg)
            else:
                self._handle_notification(msg)
            return
        with self._lock:
            slot = self._pending.pop(msg.get("id"), None)
        if slot is not None:
            slot.put(msg)

    def _handle_notification(self, msg):
        if msg.get("method") == "session/update":
            try:
                self.on_update(msg.get("params") or {})
            except Exception as exc:
                log("update handler failed: %s: %s" % (exc.__class__.__name__, short(exc, 200)))
        else:
            log("acp notification %s" % msg.get("method"))

    def _handle_request(self, msg):
        method = msg.get("method")
        rid = msg.get("id")
        if method == "session/request_permission":
            params = msg.get("params") or {}
            call = params.get("toolCall") or {}
            try:
                decision = self.on_permission(call)
            except Exception:
                decision = "deny"
            options = params.get("options") or []
            option_id = None
            for opt in options:
                kind = str(opt.get("kind") or "")
                if decision == "allow" and kind.startswith("allow"):
                    option_id = opt.get("optionId")
                    break
                if decision != "allow" and kind.startswith("reject"):
                    option_id = opt.get("optionId")
                    break
            if option_id is None:
                option_id = "allow" if decision == "allow" else "deny"
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": {"outcome": "selected", "optionId": option_id}}})
            log("permission %s tool=%s -> %s" % (method, short(call.get("name"), 60), decision))
            return
        # Unknown client-side request: answer with an error instead of hanging.
        self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unsupported client method: %s" % method}})

    def _send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            if not self.alive or self.proc is None or self.proc.stdin is None:
                raise AcpError("agy process is not running")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.alive = False
                raise AcpError("agy process pipe closed") from exc

    # -- api --------------------------------------------------------------- #
    def request(self, method, params, timeout=120):
        if not self.alive:
            raise AcpError("agy process is not running")
        self._id += 1
        rid = self._id
        slot = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = slot.get(timeout=timeout)
        except queue.Empty:
            raise AcpError("%s timed out after %ss" % (method, timeout))
        with self._lock:
            self._pending.pop(rid, None)
        if msg is None:
            raise AcpError("agy process exited during %s" % method)
        if "error" in msg:
            raise AcpError("%s failed: %s" % (method, short(msg["error"].get("message"), 300)))
        return msg.get("result") or {}

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def stop(self, timeout=5.0):
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
LEVELS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "extra": "xhigh", "max": "xhigh"}
MODEL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<level>[A-Za-z]+)\)\s*$")


def tool_result_text(update) -> str:
    parts = []
    for item in update.get("content") or []:
        if not isinstance(item, dict):
            continue
        inner = item.get("content")
        if isinstance(inner, dict) and inner.get("text"):
            parts.append(str(inner["text"]))
        elif item.get("text"):
            parts.append(str(item["text"]))
    if not parts:
        raw = update.get("rawOutput")
        if isinstance(raw, dict):
            if raw.get("output"):
                parts.append(str(raw["output"]))
            elif raw.get("error"):
                parts.append(str(raw["error"]))
        elif raw:
            parts.append(str(raw))
    return "\n".join(parts)[:MAX_RESULT_CHARS]


def fetch_mcp_tools(url: str, timeout: float = 8.0):
    """Ask her MCP server for its tool list (used to brief the agent).

    agy hides MCP tools from the tool schema whenever the custom agent carries a
    ``tools`` whitelist, so the bridge lists them in the agent body instead and
    the agent reaches them through ``call_mcp_tool``.
    """
    if not url:
        return []
    import urllib.request

    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def rpc(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        raw = raw.strip()
        if raw.startswith("event:") or "\ndata:" in raw:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
        return json.loads(raw) if raw else {}

    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "albion-agy-bridge", "version": "1.0"}}})
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        data = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = (data.get("result") or {}).get("tools") or []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]
    except Exception as exc:
        log("mcp tool discovery failed: %s" % exc.__class__.__name__)
        return []


def tool_briefing(tools) -> str:
    """Short, private-agnostic description of her tools for the agent body."""
    if not tools:
        return ""
    lines = [
        "",
        "## Your tools",
        "You have no built-in tools. Every tool of yours is on the MCP server `%s`." % MCP_SERVER_NAME,
        'Call one with `call_mcp_tool`: {"ServerName": "%s", "ToolName": "<name>", "Arguments": {…}}.' % MCP_SERVER_NAME,
        "",
    ]
    for tool in tools[:80]:
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        args = ", ".join(props.keys())
        desc = str(tool.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 160:
            desc = desc[:160] + "…"
        lines.append("- `%s(%s)` %s" % (tool["name"], args, desc))
    return "\n".join(lines) + "\n"


class Session:
    def __init__(self, name: str, system_prompt: str, model: str, mcp_url: str):
        self.name = name
        self.persona_hash = hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()
        self.system_prompt = system_prompt
        self.model = model
        self.mcp_url = mcp_url
        self.workspace = WORKSPACE_ROOT / name
        self.gemini_dir = GEMINI_ROOT / name
        self.proc: AcpProcess | None = None
        self.acp_session = None
        self.models = []
        self.config_options = []
        self.started_at = time.time()
        self.busy = False
        self.dead = False
        self.turn = None
        self._turn_lock = threading.Lock()
        self._start()

    # -- setup ------------------------------------------------------------- #
    def _write_workspace(self):
        if self.workspace.exists():
            shutil.rmtree(self.workspace, ignore_errors=True)
        agents = self.workspace / ".agents"
        (agents / "agents").mkdir(parents=True, exist_ok=True)
        front = [
            "---",
            "name: %s" % AGENT_NAME,
            "description: Albion, running on the agy kernel.",
            "mainAgent: true",
            "subagent: false",
            # No native tools: file/command/web access on Windows would be the
            # user's own permissions. Her MCP tools arrive over MCP instead.
            "tools: []",
            "---",
            "",
        ]
        persona = (self.system_prompt or "").strip()
        briefing = tool_briefing(fetch_mcp_tools(self.mcp_url))
        (agents / "agents" / ("%s.md" % AGENT_NAME)).write_text("\n".join(front) + persona + "\n" + briefing, encoding="utf-8")
        cfg = {"mcpServers": {MCP_SERVER_NAME: {"url": self.mcp_url}}}
        (agents / "mcp_config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        # Keep the workspace truly empty apart from her configuration.
        (self.workspace / "README.txt").write_text(
            "Albion's agy workspace. Managed by brain/agy/bridge.py; contents are rewritten per session.\n",
            encoding="utf-8",
        )

    def _start(self):
        self._write_workspace()
        ensure_gemini_dir(self.gemini_dir, self.mcp_url)
        self.proc = AcpProcess(
            self.workspace,
            extra_args=["--gemini-dir", str(self.gemini_dir)],
            on_update=self._on_update,
            on_permission=self._decide_permission,
            on_done=self._on_proc_done,
        )
        self.proc.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "albion-agy-bridge", "version": "1.0"},
            },
            timeout=60,
        )
        created = self.proc.request("session/new", {"cwd": str(self.workspace), "mcpServers": []}, timeout=180)
        self.acp_session = created.get("sessionId")
        if not self.acp_session:
            raise AcpError("agy returned no sessionId")
        self.models = ((created.get("models") or {}).get("availableModels")) or []
        self.config_options = created.get("configOptions") or []
        self._apply_model()
        self.started_at = time.time()
        log("session %s started (agy %s, pid %s)" % (self.name, short(self.acp_session, 12), self.proc.proc.pid))

    def _apply_model(self):
        wanted = (self.model or "").strip()
        if not wanted:
            return
        model_id, level = self._resolve_model(wanted)
        if model_id is None:
            log("session %s: model %r not in agy list, keeping default" % (self.name, wanted))
            return
        for config_id, value in (("model", model_id), ("thought_level", level)):
            if not value:
                continue
            try:
                self.proc.request(
                    "session/set_config_option",
                    {"sessionId": self.acp_session, "configId": config_id, "value": value},
                    timeout=30,
                )
            except AcpError as exc:
                log("session %s: set %s failed (%s)" % (self.name, config_id, short(exc, 120)))
        log("session %s: model=%s level=%s" % (self.name, model_id, level or "-"))

    def _resolve_model(self, wanted: str):
        match = MODEL_RE.match(wanted)
        base = match.group("name").strip() if match else wanted.strip()
        level = LEVELS.get(match.group("level").lower()) if match else None
        for item in self.models:
            if item.get("name") == base or item.get("modelId") == wanted:
                return item.get("modelId"), level
        for item in self.models:
            if str(item.get("name", "")).startswith(base):
                return item.get("modelId"), level
        return None, level

    # -- ACP callbacks ----------------------------------------------------- #
    def _decide_permission(self, tool_call):
        """Allow only her own MCP tools; refuse every native agy tool."""
        name = str(tool_call.get("name") or "")
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name == "call_mcp_tool":
            server = str(args.get("ServerName") or args.get("server") or "")
            allowed = server == MCP_SERVER_NAME
            log("permission: call_mcp_tool server=%s tool=%s -> %s" % (short(server, 40), short(args.get("ToolName"), 60), "allow" if allowed else "deny"))
            return "allow" if allowed else "deny"
        if name.startswith("mcp_%s_" % MCP_SERVER_NAME):
            return "allow"
        log("permission: native tool %s -> deny" % short(name, 60))
        return "deny"

    def _on_update(self, params):
        turn = self.turn
        if turn is None:
            return
        if params.get("sessionId") not in (None, self.acp_session):
            return
        turn.feed(params.get("update") or {})

    def _on_proc_done(self):
        self.dead = True
        turn = self.turn
        if turn is not None and not turn.finished:
            turn.fail("agy process exited")

    # -- prompt ------------------------------------------------------------ #
    def prompt(self, text, images=None):
        with self._turn_lock:
            if self.busy:
                raise AcpError("session busy")
            self.busy = True
            turn = Turn(self, text, images or [])
            self.turn = turn
        threading.Thread(target=self._run_turn, args=(turn,), daemon=True, name="turn-%s" % self.name).start()
        return turn

    def _run_turn(self, turn):
        try:
            blocks = []
            if turn.text:
                blocks.append({"type": "text", "text": turn.text})
            for image in turn.images:
                mime = image.get("mime") or "image/png"
                data = image.get("data") or ""
                if data:
                    blocks.append({"type": "image", "mimeType": mime, "data": data})
            result = self.proc.request(
                "session/prompt",
                {"sessionId": self.acp_session, "prompt": blocks},
                timeout=PROMPT_IDLE_TIMEOUT,
            )
            turn.finish(result)
        except AcpError as exc:
            turn.fail(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log("turn crashed: %s" % traceback.format_exc(limit=3))
            turn.fail("bridge error: %s" % exc.__class__.__name__)
        finally:
            self.busy = False
            self.turn = None

    def cancel(self):
        if not self.busy:
            return False
        try:
            self.proc.notify("session/cancel", {"sessionId": self.acp_session})
        except AcpError as exc:
            log("cancel failed: %s" % short(exc, 120))
            return False
        log("session %s: cancel sent" % self.name)
        return True

    def close(self):
        if self.proc is not None:
            self.proc.stop()
        self.dead = True
        shutil.rmtree(self.workspace, ignore_errors=True)
        shutil.rmtree(self.gemini_dir, ignore_errors=True)  # agy 的对话存档里也有人设，一起清
        log("session %s closed" % self.name)

    def info(self):
        return {
            "id": self.name,
            "model": self.model,
            "mcp": self.mcp_url,
            "busy": self.busy,
            "dead": self.dead,
            "age": round(time.time() - self.started_at, 1),
        }


class Turn:
    """Collects ACP updates for one prompt and hands out bridge events."""

    def __init__(self, session: Session, text: str, images):
        self.session = session
        self.text = text
        self.images = images
        self.events = queue.Queue()
        self.parts = []
        self.finished = False
        self.usage = {}
        self.stop = None
        self._tools = {}

    # -- event sink -------------------------------------------------------- #
    def feed(self, update):
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") or ""
            if text:
                self.parts.append(text)
                self.events.put({"type": "text_delta", "text": text})
        elif kind == "tool_call":
            name, args, _server = self._real_tool(update)
            call_id = update.get("toolCallId") or ""
            self._tools[call_id] = {"name": name, "args": args}
            self.events.put({"type": "tool_start", "id": call_id, "name": name, "args": args})
        elif kind == "tool_call_update":
            status = update.get("status")
            if status in ("completed", "failed", "cancelled"):
                call_id = update.get("toolCallId") or ""
                known = self._tools.get(call_id) or {}
                name, _args, _server = self._real_tool(update)
                name = name or known.get("name") or "tool"
                result = tool_result_text(update)
                self.events.put(
                    {
                        "type": "tool_end",
                        "id": call_id,
                        "name": name,
                        "ok": status == "completed",
                        "result": result,
                    }
                )
        elif kind == "usage_update":
            used = update.get("used")
            if isinstance(used, int):
                self.usage["input"] = used
        # agent_thought_chunk, session_info_update, available_commands_update,
        # config_option_update, *: not part of the bridge interface.

    def _real_tool(self, update):
        """Map agy's generic MCP wrapper back to her real tool name/args."""
        name = str(update.get("name") or "")
        args = update.get("args") or update.get("rawInput") or {}
        if not isinstance(args, dict):
            args = {}
        if name == "call_mcp_tool":
            inner = args.get("Arguments")
            server = args.get("ServerName") or args.get("server")
            tool = args.get("ToolName") or args.get("tool") or "mcp_tool"
            return tool, inner if isinstance(inner, dict) else {}, server
        return name, args, None

    # -- terminal states --------------------------------------------------- #
    def finish(self, result):
        if self.finished:
            return
        usage = result.get("usage") or {}
        usage_out = {
            "input": usage.get("inputTokens", self.usage.get("input", 0)),
            "output": usage.get("outputTokens", 0),
            "cached": usage.get("cachedReadTokens", 0),
            "thought": usage.get("thoughtTokens", 0),
        }
        self.usage = usage_out
        self.stop = result.get("stopReason") or "end_turn"
        self.events.put({"type": "final", "text": "".join(self.parts), "usage": usage_out, "stop": self.stop})
        self.finished = True
        self.events.put(None)

    def fail(self, message):
        if self.finished:
            return
        self.events.put({"type": "error", "message": message})
        self.events.put({"type": "final", "text": "".join(self.parts), "usage": self.usage or {}, "stop": "error"})
        self.finished = True
        self.events.put(None)

    def stream(self, timeout=None):
        while True:
            try:
                item = self.events.get(timeout=timeout)
            except queue.Empty:
                return
            if item is None:
                return
            yield item


# --------------------------------------------------------------------------- #
# Session registry
# --------------------------------------------------------------------------- #
class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}

    def ensure(self, name, system_prompt, model, mcp_url):
        with self.lock:
            existing = self.sessions.get(name)
            if existing is not None and not existing.dead:
                same_persona = existing.persona_hash == hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest()
                if same_persona and existing.model == model and existing.mcp_url == mcp_url:
                    return existing, False
                log("session %s: rebuilding (persona/model/url changed)" % name)
                existing.close()
                self.sessions.pop(name, None)
            elif existing is not None:
                self.sessions.pop(name, None)
            session = Session(name, system_prompt, model, mcp_url)
            self.sessions[name] = session
            return session, True

    def get(self, name):
        with self.lock:
            return self.sessions.get(name)

    def drop(self, name):
        with self.lock:
            session = self.sessions.pop(name, None)
        if session is not None:
            session.close()
            return True
        return False

    def list(self):
        with self.lock:
            return [s.info() for s in self.sessions.values()]


REGISTRY = Registry()
VERSION = agy_version()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "albion-agy-bridge/1.0"

    def log_message(self, fmt, *args):  # keep the console clean
        return

    # -- helpers ----------------------------------------------------------- #
    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("body is not JSON")
        return data if isinstance(data, dict) else {}

    def _parts(self):
        path = urllib.parse.urlparse(self.path).path.strip("/")
        return [urllib.parse.unquote(p) for p in path.split("/") if p]

    # -- routes ------------------------------------------------------------ #
    def do_GET(self):
        parts = self._parts()
        if parts == ["health"]:
            self._json(200, {"ok": True, "agy_version": VERSION, "sessions": REGISTRY.list()})
        elif parts == ["sessions"]:
            self._json(200, {"sessions": REGISTRY.list()})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parts = self._parts()
        try:
            if parts == ["sessions"]:
                return self._create()
            if len(parts) == 3 and parts[0] == "sessions":
                session = REGISTRY.get(parts[1])
                if session is None:
                    return self._json(404, {"ok": False, "error": "unknown session"})
                if parts[2] == "prompt":
                    return self._prompt(session)
                if parts[2] == "steer":
                    # agy has no mid-turn injection: a second prompt is queued as
                    # a fresh turn, so the brain must queue instead.
                    return self._json(409, {"ok": False, "error": "steer unsupported by agy acp"})
                if parts[2] == "cancel":
                    active = session.cancel()
                    return self._json(200, {"ok": True, "cancelled": active})
            self._json(404, {"ok": False, "error": "not found"})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            log("POST %s failed: %s" % (self.path, traceback.format_exc(limit=2)))
            self._json(500, {"ok": False, "error": "%s" % exc})

    def do_DELETE(self):
        parts = self._parts()
        if len(parts) == 2 and parts[0] == "sessions":
            return self._json(200, {"ok": REGISTRY.drop(parts[1])})
        self._json(404, {"ok": False, "error": "not found"})

    # -- handlers ---------------------------------------------------------- #
    def _create(self):
        body = self._read_json()
        name = str(body.get("session") or body.get("id") or "").strip()
        if not name or "/" in name or "\\" in name:
            return self._json(400, {"ok": False, "error": "session name required"})
        system_prompt = body.get("system_prompt") or ""
        model = str(body.get("model") or "")
        mcp_url = str(body.get("mcp_url") or "")
        if not mcp_url:
            return self._json(400, {"ok": False, "error": "mcp_url required"})
        session, created = REGISTRY.ensure(name, system_prompt, model, mcp_url)
        self._json(200, {"id": name, "created": created, "model": session.model})

    def _prompt(self, session):
        body = self._read_json()
        text = str(body.get("text") or "")
        images = body.get("images") or []
        if not isinstance(images, list):
            images = []
        if not text and not images:
            return self._json(400, {"ok": False, "error": "text or images required"})
        try:
            turn = session.prompt(text, images)
        except AcpError as exc:
            return self._json(409, {"ok": False, "error": str(exc)})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for event in turn.stream():
                payload = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            session.cancel()
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    log("bridge starting on %s:%s (agy %s, workspace %s)" % (HOST, PORT, VERSION, WORKSPACE_ROOT))
    httpd = BridgeServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for info in REGISTRY.list():
            REGISTRY.drop(info["id"])
        httpd.server_close()
        log("bridge stopped")


if __name__ == "__main__":
    main()
