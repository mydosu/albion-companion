"""壳插件工具的桥（2026-10-20）：壳里插件运行时注册的工具，让 Pi 里的她也能用，不用重启。

谁拿什么：
  · 壳（AIRI）每次请求照 OpenAI 协议把当前所有工具放进 body.tools，并用 `X-Albion-Toolset` 头
    标出其中哪几件是插件贡献的（附插件的 toolset 提示词）。只有头里点名、body 里也有定义的才桥接，
    壳自己的调试 / MCP / 搜索工具不过来。
  · 宿主（server.py）每个新请求都 publish 一次清单：插件注销了，下一轮清单里就没有它。
  · 扩展 agent/extensions/albion-shell-tools.ts 每轮开跑前 GET 清单，注册代理工具、把没了的改成 hidden。
  · 她调用代理工具 → 扩展 POST /albion/shell-tools/call（阻塞）→ 宿主把这次调用当成 OpenAI tool_calls
    回给壳 → 壳执行插件、带着 role=tool 的结果再发一次请求 → 宿主把结果交还给阻塞着的那次调用，Pi 接着跑。

状态模型（每个会话一个 Bridge）：
  toolset   最近一次 publish 的清单（内存，宿主重启即空；壳下一轮会再带来）
  current   正在跑的那一轮 Turn。Turn 在自己的线程里跑 server.chat()（会话锁在 chat 里拿），
            HTTP 处理线程只等 Turn 的事件：calls（要壳执行）/ done（她说完了）/ error。
  pending   已交给壳、还没拿到结果的调用。CALL_TIMEOUT 内壳不回就按失败交还给她，不让一轮卡死。
隔离：一件工具定义坏了只跳过那一件；壳那边插件抛错由 xsai 变成错误结果送回来，她看到的是失败的工具结果。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import re
import threading
import time
import uuid

HEADER = "X-Albion-Toolset"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_HEADER_CHARS = 48 * 1024
MAX_TOOLS = 32
MAX_DESCRIPTION = 2000
MAX_PROMPT = 4000
MAX_PROMPTS = 16
MAX_RESULT = 16000
CALL_TIMEOUT = float(os.environ.get("ALBION_SHELL_TOOL_TIMEOUT", "120"))
# 她一条消息里并发的几次调用前后只差几毫秒；收齐再一起交给壳，壳一轮执行完一起回。
GATHER_S = 0.3


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…（截断）"


def parse_manifest(headers):
    """X-Albion-Toolset 头 → (插件工具名集合, 提示词列表, 问题列表)。头坏了就当没有，不连累这一轮。"""
    raw = (headers.get(HEADER) or "").strip() if headers else ""
    if not raw:
        return set(), [], []
    if len(raw) > MAX_HEADER_CHARS:
        return set(), [], ["toolset header too large"]
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError) as exc:
        return set(), [], ["toolset header unreadable: %s" % type(exc).__name__]
    if not isinstance(data, dict):
        return set(), [], ["toolset header is not an object"]
    names = {n for n in data.get("tools") or [] if isinstance(n, str)}
    prompts = []
    for p in (data.get("prompts") or [])[:MAX_PROMPTS]:
        if isinstance(p, dict) and isinstance(p.get("content"), str) and p["content"].strip():
            prompts.append({"id": str(p.get("id") or ""), "title": str(p.get("title") or ""),
                            "content": _clip(p["content"].strip(), MAX_PROMPT)})
    return names, prompts, []


def toolset_from(body, headers):
    """本轮要桥接的插件工具：头里点名 ∩ body.tools 里有合法定义。逐件校验，坏的只跳过那一件。

    返回 {"tools": [{name, description, parameters}], "prompts": [...], "skipped": [{name, reason}]}。
    """
    names, prompts, problems = parse_manifest(headers)
    skipped = [{"name": "", "reason": p} for p in problems]
    tools, seen = [], set()
    if names:
        for item in body.get("tools") or []:
            fn = item.get("function") if isinstance(item, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if not isinstance(name, str) or name not in names:
                continue
            if name in seen:
                skipped.append({"name": name, "reason": "duplicate name in request"})
                continue
            seen.add(name)
            if not NAME_RE.match(name):
                skipped.append({"name": name, "reason": "invalid tool name"})
                continue
            params = fn.get("parameters")
            if params is None:
                params = {"type": "object", "properties": {}}
            if not isinstance(params, dict) or params.get("type") != "object":
                skipped.append({"name": name, "reason": "parameters root is not an object schema"})
                continue
            if len(tools) >= MAX_TOOLS:
                skipped.append({"name": name, "reason": "too many shell tools"})
                continue
            tools.append({"name": name, "description": _clip(fn.get("description"), MAX_DESCRIPTION),
                          "parameters": params})
        for name in sorted(names - seen):
            skipped.append({"name": name, "reason": "named in header but missing from request tools"})
    return {"tools": tools, "prompts": prompts if tools else [], "skipped": skipped}


def result_text(content):
    """role=tool 消息的 content → 交给她的文字。壳（xsai）给的是字符串或内容段数组。"""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False)
    return _clip(text, MAX_RESULT)


def tool_results(msgs):
    """请求末尾那串 role=tool 消息 → {tool_call_id: 文字}。不是末尾的旧结果不算（那是已经交过的）。"""
    out = {}
    for m in reversed(msgs or []):
        if not isinstance(m, dict) or m.get("role") != "tool":
            break
        cid = m.get("tool_call_id")
        if isinstance(cid, str) and cid not in out:
            out[cid] = result_text(m.get("content"))
    return out


class Call:
    """她发起的一次壳工具调用。done 置位后 ok/text 就是交还给她的结果。"""

    def __init__(self, name, args):
        self.id = "call_shell_" + uuid.uuid4().hex[:16]
        self.name = name
        self.args = args if isinstance(args, dict) else {}
        self.handed_at = None          # 交给壳的时间；None = 还在队里没交出去
        self.ok = False
        self.text = ""
        self.done = threading.Event()

    def resolve(self, ok, text):
        if self.done.is_set():
            return False
        self.ok, self.text = ok, text
        self.done.set()
        return True

    def openai(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.args, ensure_ascii=False)}}


class Turn:
    """一轮 chat()：在自己的线程里跑，HTTP 线程通过 events 拿结果。

    events 里只有三种：("call", Call) / ("done", text) / ("error", exc)。同一时刻只有一个 HTTP 线程在等。
    """

    def __init__(self, fn):
        self.events = queue.Queue()
        self.pending = {}              # call id → Call（交给壳、等结果的）
        self.abandoned = False
        self.delivered = False         # done 文本有没有交到某个 HTTP 响应里
        self.final = None
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._fn = fn

    def run(self):
        try:
            text = self._fn()
            self.final = text
            self.events.put(("done", text))
        except Exception as exc:       # 交给等着的 HTTP 线程按原来的 502/500 处理
            self.events.put(("error", exc))
        finally:
            self.finished.set()

    def outstanding(self):
        with self._lock:
            return [c for c in self.pending.values() if not c.done.is_set()]

    def abandon(self, reason):
        """壳那边这一轮的工具循环已经结束（发来了新消息）：还在等的调用全部按失败交还，之后的调用直接拒。"""
        with self._lock:
            self.abandoned = True
            calls = list(self.pending.values())
        for c in calls:
            c.resolve(False, reason)


class Bridge:
    """一个会话的桥：最新清单 + 当前这一轮。"""

    def __init__(self, session_id):
        self.session_id = session_id
        self.toolset = {"tools": [], "prompts": []}
        self.version = 0
        self.current = None
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()

    # ── 清单 ────────────────────────────────────────────────────────────────
    def publish(self, toolset):
        snap = {"tools": toolset.get("tools") or [], "prompts": toolset.get("prompts") or []}
        with self._lock:
            if snap != self.toolset:
                self.toolset = snap
                self.version += 1
            return self.version

    def snapshot(self):
        with self._lock:
            return {"version": self.version, "tools": list(self.toolset["tools"]),
                    "prompts": list(self.toolset["prompts"])}

    def has_tool(self, name):
        with self._lock:
            return any(t["name"] == name for t in self.toolset["tools"])

    # ── 一轮 ────────────────────────────────────────────────────────────────
    def start(self, fn, *, toolset=None, supersede=False, wait_previous=900.0):
        """开新的一轮。上一轮没跑完：supersede=True（同一个会桥接的壳发来了新消息，说明它那边的工具循环已结束）
        就把上一轮还在等的调用按失败交还；否则照旧排队等它跑完（和原来会话锁的行为一样）。
        清单在上一轮结束后才换：另一个不桥接的客户端排队进来，不会把正在跑那一轮的工具撤掉。"""
        with self._start_lock:
            prev = self.current
            if prev is not None and not prev.finished.is_set():
                if supersede and prev.outstanding():
                    prev.abandon("这次调用作废了：指挥官那边已经开始了新的一轮，壳没有回这件工具的结果。")
                prev.finished.wait(wait_previous)
            if toolset is not None:
                self.publish(toolset)
            turn = Turn(fn)
            with self._lock:
                self.current = turn
            threading.Thread(target=turn.run, name="turn-%s" % self.session_id, daemon=True).start()
            return turn

    def call(self, name, args, timeout=None):
        """扩展那边的阻塞调用：返回 (ok, 文字)。没有正在跑的一轮、这轮已作废、清单里没这件工具都直接拒。"""
        timeout = CALL_TIMEOUT if timeout is None else timeout
        with self._lock:
            turn = self.current
        if turn is None or turn.finished.is_set():
            return False, "壳工具现在用不了：这一轮不是壳发起的。"
        if not self.has_tool(name):
            return False, "壳工具 %s 现在不在了（插件可能已经卸载）。" % name
        c = Call(name, args)
        with turn._lock:
            if turn.abandoned:
                return False, "这一轮已经作废，壳不会再执行工具。"
            turn.pending[c.id] = c
        turn.events.put(("call", c))
        if not c.done.wait(timeout):
            c.resolve(False, "壳没在 %d 秒内回这件工具的结果，当作失败。" % int(timeout))
        with turn._lock:
            turn.pending.pop(c.id, None)
        return c.ok, c.text

    def wait(self, turn, timeout):
        """等这一轮的下一件事：("calls", [Call…]) / ("done", text) / ("error", exc) / ("timeout", None)。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return "timeout", None
            try:
                kind, value = turn.events.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if kind == "call":
                calls = [value] if not value.done.is_set() else []
                end = time.time() + GATHER_S
                while time.time() < end:
                    try:
                        k2, v2 = turn.events.get(timeout=max(0.01, end - time.time()))
                    except queue.Empty:
                        break
                    if k2 != "call":
                        turn.events.put((k2, v2))   # 不是调用就放回去，下一次 wait 再拿
                        break
                    if not v2.done.is_set():
                        calls.append(v2)
                if not calls:
                    continue                      # 都已超时 / 作废：接着等
                now = time.time()
                for c in calls:
                    c.handed_at = now
                return "calls", calls
            if kind == "done":
                turn.delivered = True
            return kind, value

    def resume(self, results):
        """壳带回来的 role=tool 结果交给对应的调用。返回接上的那一轮；一件都对不上返回 None。"""
        with self._lock:
            turn = self.current
        if turn is None:
            return None
        matched = False
        with turn._lock:
            calls = [turn.pending.get(cid) for cid in results]
        for cid, call in zip(results, calls):
            if call is not None and call.resolve(True, results[cid]):
                matched = True
        return turn if matched else None

    def orphan_text(self):
        """上一轮已经跑完、结果却没交到任何响应里（壳的结果来晚了）：把那段话补交一次。"""
        with self._lock:
            turn = self.current
        if turn is None or not turn.finished.is_set() or turn.delivered or turn.final is None:
            return None
        turn.delivered = True
        return turn.final


_bridges = {}
_bridges_lock = threading.Lock()


def bridge_for(session_id):
    with _bridges_lock:
        b = _bridges.get(session_id)
        if b is None:
            b = _bridges[session_id] = Bridge(session_id)
        return b
