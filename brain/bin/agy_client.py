"""Long-lived `agy acp` client: one Antigravity CLI process per shell conversation.

Albion's brain can run on Antigravity CLI (agy) instead of Pi. The shell still
talks OpenAI-compatible HTTP to our server; the server talks ACP stdio
JSON-RPC to this subprocess.

What lives where under the agy kernel:

* agy owns the model loop and its own built-in tools (run_command, read_file,
  write/edit, web search, subagents…). We do not customise the agent, its tool
  set or its prompt (11-27 decision: no custom agent, no tool whitelist).
* Her persona goes in through `session/new`:
  `_meta.antigravity.systemPrompt.append`. Her tools come from the extension
  host (`bin/agy_host.ts`, 127.0.0.1:8661) mounted as a per-session MCP server.
* Every turn: host `/turn/<s>/before` → ACP prompt (streaming events mirrored
  to host `/event`) → host `/final` for the delivered text.

Nothing here logs the persona or the user's words: only counts, names, model
ids and errors go to the session's stderr log.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AGY_BIN = os.environ.get("ALBION_AGY_BIN", "/root/.local/bin/agy")
AGY_PROXY = os.environ.get("ALBION_AGY_PROXY", "http://127.0.0.1:7897")
AGY_MODEL = os.environ.get("ALBION_AGY_MODEL", "Gemini 3.8 Flash (Medium)")
HOST_PORT = int(os.environ.get("ALBION_HOST_PORT", "8661"))
MCP_SERVER = "albion"
PROMPT_TIMEOUT = int(os.environ.get("ALBION_RUN_TIMEOUT", "420"))

# agy's built-in tools that can change the machine. Ephemeral (probe/accept)
# sessions refuse these, matching Pi's read-only tool set.
EPHEMERAL_DENY = {"run_command", "write_file", "edit_file", "delete_file",
                  "manage_task", "schedule", "invoke_subagent", "define_subagent",
                  "manage_subagents", "send_message", "run_workflow", "swarm"}

LEVELS = {"low": "low", "medium": "medium", "high": "high",
          "xhigh": "xhigh", "extra": "xhigh", "max": "xhigh"}


def log_line(path, text):
    """Append one diagnostic line; never pass persona or user text."""
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def parse_model(name):
    """`Gemini 3.8 Flash (Medium)` → ("Gemini 3.8 Flash", "medium")."""
    text = (name or "").strip()
    if text.endswith(")") and "(" in text:
        base, _, level = text[:-1].rpartition("(")
        lv = LEVELS.get(level.strip().lower())
        if lv:
            return base.strip(), lv
    return text, None


def image_blocks(data_urls):
    """`data:image/png;base64,<...>` → ACP image content blocks."""
    out = []
    for u in data_urls or []:
        if not isinstance(u, str) or not u.startswith("data:"):
            continue
        head, _, b64 = u.partition(",")
        if not b64 or "base64" not in head:
            continue
        mime = (head[5:].split(";")[0] or "image/png").strip() or "image/png"
        out.append({"type": "image", "mimeType": mime, "data": b64.strip()})
    return out


class AgyError(RuntimeError):
    pass


def _tool_result_text(update):
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
    return "\n".join(parts)[:4000]


class AgySession:
    """One conversation with agy, kept in a live ACP subprocess."""

    def __init__(self, name, *, agent_dir=None, cwd="/root", model=None,
                 tools=None, extensions=(), skills=(), log_path=None,
                 sandbox_env=None, host_port=None, readonly=None):
        self.name = name
        self.agent_dir = agent_dir
        self.model = model or AGY_MODEL
        self.tools = list(tools or [])
        self.extensions = list(extensions)          # kept for interface parity; agy_host loads them
        self.skills = list(skills)
        self.log_path = log_path
        self.sandbox_env = dict(sandbox_env or {})
        self.host_port = int(host_port or HOST_PORT)
        # tools == ["*"] → her real session, everything allowed. Otherwise the
        # caller handed us the read-only list: probes/accept runs.
        self.readonly = (readonly if readonly is not None
                         else bool(self.tools) and self.tools != ["*"])
        self.readonly_tools = {t for t in self.tools if t != "*"}
        self.ephemeral_dir = None
        self.cwd = cwd
        if self.readonly:
            self.ephemeral_dir = tempfile.mkdtemp(prefix="albion-agy-%s-" % _safe(name))
            self.cwd = self.ephemeral_dir

        self.proc = None
        self._alive = threading.Event()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._req = 0
        self._acp_session = None
        self._models = []
        self._text = []
        self._finals = []
        self._error = None
        self._settled = threading.Event()
        self._run_lock = threading.Lock()
        self._run_active = False
        self._abort_sent = False
        self._text_only = False
        self._context = (None, None)
        self.turns = 0
        self.steered = 0
        self.tools_used = []
        self.seeded = False
        self.ext_sig = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def args(self):
        """Command line for logs (no secrets). A `*.py` ALBION_AGY_BIN is a test stub."""
        if AGY_BIN.lower().endswith(".py"):
            return [sys.executable, AGY_BIN, "acp"]
        return [AGY_BIN, "acp"]

    def env(self):
        env = dict(os.environ)
        env.update(self.sandbox_env)
        proxy = (AGY_PROXY or "").strip()
        if proxy:
            env["HTTPS_PROXY"] = proxy
            env["HTTP_PROXY"] = proxy
            env["https_proxy"] = proxy
            env["http_proxy"] = proxy
            env["NO_PROXY"] = "127.0.0.1,localhost"
            env["no_proxy"] = "127.0.0.1,localhost"
        env.setdefault("TZ", "Asia/Shanghai")
        return env

    def start(self):
        persona = self.system_prompt()
        err = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            self.args(), cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, env=self.env(), bufsize=0,
        )
        self._alive.set()
        threading.Thread(target=self._reader, name="agy-%s" % self.name, daemon=True).start()
        self._initialize(persona)
        return self

    def system_prompt(self):
        """SYSTEM.md + APPEND_SYSTEM.md, in that order."""
        if not self.agent_dir:
            return ""
        parts = []
        for name in ("SYSTEM.md", "APPEND_SYSTEM.md"):
            path = os.path.join(self.agent_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
            except OSError:
                continue
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def close(self):
        self._alive.clear()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:                   # noqa: BLE001
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:                   # noqa: BLE001
                self.proc.kill()
        if self.ephemeral_dir:
            shutil.rmtree(self.ephemeral_dir, ignore_errors=True)
            self.ephemeral_dir = None

    @property
    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    # ── ACP plumbing ─────────────────────────────────────────────────────────
    def _send(self, record):
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if not self.proc or not self.proc.stdin:
                raise AgyError("agy process is not running")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._alive.clear()
                raise AgyError("agy process pipe closed") from exc

    def _reader(self):
        try:
            for raw in self.proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:               # noqa: BLE001 - 非协议行直接丢
                    continue
                self._dispatch(msg)
        finally:
            self._alive.clear()
            self._settled.set()
            with self._lock:
                pending, self._pending = self._pending, {}
            for slot in pending.values():
                slot.put(None)

    def _dispatch(self, msg):
        method = msg.get("method")
        if method:
            if "id" in msg:
                self._handle_request(msg)
            elif method == "session/update":
                self._on_update((msg.get("params") or {}).get("update") or {})
            return
        with self._lock:
            slot = self._pending.pop(msg.get("id"), None)
        if slot is not None:
            slot.put(msg)

    def _handle_request(self, msg):
        """agy → client requests. Only permissions matter on the ACP surface."""
        rid = msg.get("id")
        method = msg.get("method")
        if method != "session/request_permission":
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": "unsupported client method"}})
            return
        params = msg.get("params") or {}
        call = params.get("toolCall") or {}
        allow = self._decide_permission(call)
        option = None
        for opt in params.get("options") or []:
            kind = str(opt.get("kind") or "")
            if allow and kind.startswith("allow"):
                option = opt.get("optionId")
                break
            if not allow and kind.startswith("reject"):
                option = opt.get("optionId")
                break
        option = option or ("allow" if allow else "deny")
        self._send({"jsonrpc": "2.0", "id": rid,
                    "result": {"outcome": {"outcome": "selected", "optionId": option}}})
        log_line(self.log_path, "permission tool=%s -> %s" % (_safe(str(call.get("name"))), "allow" if allow else "deny"))

    def _decide_permission(self, call):
        """Her real session: everything. Probes: read-only, like Pi's tool list."""
        name = str(call.get("name") or "")
        if not self.readonly and not self._text_only:
            return True
        if self._text_only:
            return False                       # 这一轮不给工具：能拦的都拦
        if not self.readonly:
            return True
        base = self.mcp_tool_name(name)
        if base is not None:
            return base in self.readonly_tools
        return False                           # run_command / write / edit …

    @staticmethod
    def mcp_tool_name(name):
        """`mcp_albion_web_search` → `web_search`; native tools → None."""
        prefix = "mcp_%s_" % MCP_SERVER
        if name.startswith(prefix):
            return name[len(prefix):]
        if name == "mcp_%s" % MCP_SERVER:
            return ""
        return None

    def _wait_response(self, rid, timeout):
        end = time.time() + timeout
        with self._lock:
            slot = self._pending.get(rid)
        if slot is None:
            raise AgyError("agy process exited during the run")
        try:
            msg = slot.get(timeout=max(0.1, end - time.time()))
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            # 措辞对上 server.py 的 STUCK_ERRORS（"did not settle"）：超时走卡住恢复，不是普通报错
            raise AgyError("agy did not settle within %ss" % timeout)
        with self._lock:
            self._pending.pop(rid, None)
        if msg is None:
            raise AgyError("agy process exited during the run")
        if "error" in msg:
            raise AgyError("%s" % ((msg.get("error") or {}).get("message") or "acp error"))
        return msg.get("result") or {}

    def request(self, method, params, timeout=120):
        if not self.running:
            raise AgyError("agy process is not running")
        with self._lock:
            self._req += 1
            rid = "%s-%d" % (self.name, self._req)
            self._pending[rid] = queue.Queue(maxsize=1)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self._wait_response(rid, timeout)

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self, persona):
        self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False},
            "clientInfo": {"name": "albion-agy-client", "version": "1.0"},
        }, timeout=60)
        params = {
            "cwd": self.cwd,
            "mcpServers": [{
                "type": "http",
                "name": MCP_SERVER,
                "url": "http://127.0.0.1:%d/mcp/%s" % (self.host_port, self.name),
                "headers": [],
            }],
        }
        if persona:
            params["_meta"] = {"antigravity": {"systemPrompt": {"append": persona}}}
        new = self.request("session/new", params, timeout=180)
        self._acp_session = new.get("sessionId")
        if not self._acp_session:
            raise AgyError("agy returned no session id")
        self._models = ((new.get("models") or {}).get("availableModels")) or []
        self._apply_model()
        log_line(self.log_path, "session started (sid=%s model=%s tools=%s cwd=%s)"
                 % (_safe(self._acp_session)[:12], self.model,
                    "readonly" if self.readonly else "all", self.cwd))

    def _apply_model(self):
        base, level = parse_model(self.model)
        model_id = None
        for item in self._models:
            if item.get("name") == base or item.get("modelId") == self.model:
                model_id = item.get("modelId")
                break
        if model_id is None:
            for item in self._models:
                if str(item.get("name", "")).startswith(base or "\0"):
                    model_id = item.get("modelId")
                    break
        if model_id is None:
            log_line(self.log_path, "model %r not in agy list, keeping default" % base)
            return
        for config_id, value in (("model", model_id), ("thought_level", level)):
            if not value:
                continue
            try:
                self.request("session/set_config_option",
                             {"sessionId": self._acp_session, "configId": config_id, "value": value},
                             timeout=30)
            except AgyError as exc:
                log_line(self.log_path, "set %s failed: %s" % (config_id, str(exc)[:120]))
        log_line(self.log_path, "model=%s level=%s" % (model_id, level or "-"))

    # ── ACP events ───────────────────────────────────────────────────────────
    def _on_update(self, update):
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (update.get("content") or {}).get("text") or ""
            if text:
                self._text.append(text)
            return
        if kind == "usage_update":
            used, size = update.get("used"), update.get("size")
            if isinstance(used, int) and isinstance(size, int) and size > 0:
                self._context = (used, size)
            return
        if kind == "tool_call":
            self._on_tool_start(update)
            return
        if kind == "tool_call_update" and update.get("status") in ("completed", "failed", "cancelled"):
            self._on_tool_end(update)
            return

    def _on_tool_start(self, update):
        name = str(update.get("name") or "")
        args = update.get("args") or update.get("rawInput") or {}
        if not isinstance(args, dict):
            args = {}
        call_id = str(update.get("toolCallId") or "")
        self.tools_used.append({"name": name, "args": args})
        out = self._host("event", {"type": "tool_call", "toolCallId": call_id,
                                   "toolName": name, "input": args})
        if out.get("abort"):
            log_line(self.log_path, "host aborted turn at tool %s" % _safe(name))
            self._cancel()

    def _on_tool_end(self, update):
        name = str(update.get("name") or "")
        call_id = str(update.get("toolCallId") or "")
        text = _tool_result_text(update)
        self._host("event", {"type": "tool_result", "toolCallId": call_id, "toolName": name,
                             "text": text, "isError": update.get("status") != "completed"})

    def _cancel(self):
        if self._abort_sent:
            return
        self._abort_sent = True
        try:
            self.notify("session/cancel", {"sessionId": self._acp_session})
        except AgyError:
            pass

    # ── extension host ───────────────────────────────────────────────────────
    def _host(self, action, body, timeout=60):
        url = "http://127.0.0.1:%d/turn/%s/%s" % (self.host_port, urllib.parse.quote(self.name), action)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:                # noqa: BLE001 - host 不在就照常跑，别断一轮
            if not getattr(self, "_host_warned", False):
                self._host_warned = True
                log_line(self.log_path, "host %s unavailable: %s" % (action, exc.__class__.__name__))
            return {}

    # ── prompting ────────────────────────────────────────────────────────────
    def prompt(self, text, images=None, timeout=None):
        if not self.running:
            raise AgyError("agy process is not running")
        timeout = timeout or PROMPT_TIMEOUT
        before = self._host("before", {"text": text, "images": images or []}, timeout=120)
        body = before.get("prompt") or text
        append = (before.get("system_append") or "").strip()
        self._text_only = bool(before.get("text_only"))
        # agy only honours systemPrompt.append at session/new, so the per-turn
        # block rides in front of the user text (probe 11-27).
        blocks = []
        if append:
            blocks.append({"type": "text", "text": append})
        if self._text_only:
            blocks.append({"type": "text", "text": "（这一轮不要调用任何工具，直接回答。）"})
        blocks.append({"type": "text", "text": body})
        blocks.extend(image_blocks(images))
        self._finals = []
        self._text = []
        self._error = None
        self._abort_sent = False
        with self._run_lock:
            self._run_active = True
        try:
            result = self.request("session/prompt",
                                  {"sessionId": self._acp_session, "prompt": blocks},
                                  timeout=timeout)
        except AgyError as exc:
            if "did not settle" in str(exc):
                # 超时那一轮的迟到文本会混进下一轮：取消后整具身子换掉，下一轮由 compose 重新播种
                self._cancel()
                self.close()
            raise
        finally:
            with self._run_lock:
                self._run_active = False
        stop = result.get("stopReason") or "end_turn"
        usage = result.get("usage") or {}
        if usage.get("inputTokens") is not None:
            self._context = (int(usage.get("inputTokens") or 0), self._context[1] or 0)
        self.turns += 1
        draft = "".join(self._text).strip()
        final = self._host("final", {"text": draft, "source": text}, timeout=120)
        out = final.get("text") if isinstance(final.get("text"), str) else draft
        if stop == "error":
            raise AgyError(self._error or "agy turn failed")
        if not out and self._error:
            raise AgyError(self._error)
        self._finals = [out] if out else []
        log_line(self.log_path, "turn stop=%s in=%s out=%s text_only=%s chars=%d"
                 % (stop, usage.get("inputTokens"), usage.get("outputTokens"),
                    int(self._text_only), len(out or "")))
        return out

    def reply_text(self):
        return "\n\n".join(t for t in self._finals if t).strip() or "".join(self._text).strip()

    def steer(self, text, images=None, timeout=10):
        """ACP has no mid-turn injection: tell the caller to queue the line."""
        return False

    def retract(self, text, timeout=5):
        return False

    def abort(self, timeout=30):
        """`session/cancel` is a notification; wait for the prompt to come back."""
        if not self.running:
            return False
        with self._run_lock:
            active = self._run_active
        self._cancel()
        if not active:
            return True
        end = time.time() + timeout
        while time.time() < end:
            with self._run_lock:
                if not self._run_active:
                    return True
            time.sleep(0.2)
        return not self._run_active

    def interrupt(self):
        self._cancel()

    def context_usage(self, timeout=5):
        """(used, window) from the last usage_update; (None, None) when unknown."""
        used, size = self._context
        if used is None:
            return (None, None)
        return (used, size or None)

    def set_model(self, provider, model_id):
        """In-place model switch. Accepts `agy` names (`Gemini 3.8 Flash (Medium)`)."""
        if not self.running:
            return False
        wanted = model_id if isinstance(model_id, str) else str(model_id)
        if provider and provider not in ("agy", "antigravity") and "/" in wanted:
            wanted = wanted.split("/", 1)[-1]
        base, level = parse_model(wanted)
        target = None
        for item in self._models:
            if item.get("name") == base or item.get("modelId") == wanted:
                target = item.get("modelId")
                break
        if target is None:
            return False
        try:
            self.request("session/set_config_option",
                         {"sessionId": self._acp_session, "configId": "model", "value": target}, timeout=30)
            if level:
                self.request("session/set_config_option",
                             {"sessionId": self._acp_session, "configId": "thought_level", "value": level},
                             timeout=30)
        except AgyError:
            return False
        self.model = wanted
        return True


def _safe(text):
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (text or ""))[:80]
