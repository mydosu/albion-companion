"""Long-lived `pi --mode rpc` client: one Pi process per shell conversation.

Albion's brain runs on Pi (https://pi.dev). The shell talks OpenAI-compatible
HTTP to our server, the server talks JSONL to this subprocess.

Protocol notes (docs/rpc.md): stdout is reserved for protocol records, one JSON
object per LF-terminated line; diagnostics go to stderr. Do not use a line
reader that also splits on U+2028/U+2029 — split raw bytes on b"\n".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time


def image_blocks(data_urls):
    """`data:image/png;base64,<...>` → pi 的 ImageContent 块（`prompt` 的 `images` 字段）。

    只认 data URL：壳就是这么发附件的；外链要另外考虑取图失败/白名单，现在不猜。
    """
    out = []
    for u in data_urls or []:
        if not isinstance(u, str) or not u.startswith("data:"):
            continue
        head, _, b64 = u.partition(",")
        if not b64 or "base64" not in head:
            continue
        mime = (head[5:].split(";")[0] or "image/png").strip() or "image/png"
        out.append({"type": "image", "data": b64.strip(), "mimeType": mime})
    return out


class PiError(RuntimeError):
    pass


class PiSession:
    """One conversation with Pi, kept in a live RPC subprocess.

    `--no-session` keeps Pi's own store out of the picture: this process *is* the
    session, so the host owns history and there is no on-disk state to duplicate.
    """

    def __init__(self, name, *, agent_dir=None, cwd="/root", model=None,
                 tools=None, extensions=(), skills=(), log_path=None, sandbox_env=None):
        self.name = name
        self.agent_dir = agent_dir
        self.cwd = cwd
        self.model = model
        self.tools = list(tools or [])
        self.extensions = list(extensions)
        self.skills = list(skills)
        self.log_path = log_path
        self.sandbox_env = dict(sandbox_env or {})

        self.proc: subprocess.Popen | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._responses: dict[str, dict] = {}
        self._resp_event = threading.Condition(self._lock)
        self._text: list[str] = []
        # Pi 定稿时给的消息才是权威版本：守卫扩展可能已经把正文换成纠正后的稿子，
        # 也可能是它拒稿后留下的哨兵。流式 delta 只用于早显示，交付以这里为准。
        self._final: str | None = None
        # steer 插进来一句话之后，同一轮里会有不止一条正文（她先说了一半，又回他补的那句）。
        # 每条不带 toolCall 的定稿正文按顺序收在这里，交付时拼起来，别只留最后一段。
        self._finals: list[str] = []
        self._error: str | None = None
        self._settled = threading.Event()
        self._alive = threading.Event()
        # 一轮真正收尾看 agent_settled（docs/json.md：agent_end 只是低层这一跑结束，
        # steering / 重试 / 压缩之后 Pi 还会继续）。steer 能不能插，也看这里。
        self._run_lock = threading.Lock()
        self._run_active = False
        # Pi 队列里还没递出去的 steer（queue_update 事件是权威版本）；收尾后才回应的 steer 要撤时，
        # 只有确认队列里只剩这一句才敢 clear_queue，别把别人补的话一起撤掉。
        self._queued_steers: list[str] = []
        self._steer_lock = threading.Lock()
        self._req = 0
        self.turns = 0
        self.steered = 0
        self.tools_used: list[str] = []

    # ── lifecycle ────────────────────────────────────────────────────────────
    def args(self):
        a = ["pi", "--mode", "rpc", "--no-session", "--model", self.model or ""]
        if self.tools is None:
            pass                       # 不加白名单：内置工具 + 全部扩展工具
        elif self.tools:
            a += ["--tools", ",".join(self.tools)]
        else:
            a += ["--no-tools"]
        if self.agent_dir:
            a += ["--session-dir", os.path.join(self.agent_dir, "sessions")]
        for e in self.extensions:
            a += ["-e", e]
        for s in self.skills:
            a += ["--skill", s]
        return [x for x in a if x != ""]

    def start(self):
        env = dict(os.environ)
        env.update(self.sandbox_env)
        env.setdefault("PI_OFFLINE", "1")  # pi.dev is unreachable from this host
        err = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            self.args(), cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, env=env, bufsize=0,
        )
        self._alive.set()
        threading.Thread(target=self._reader, name=f"pi-{self.name}", daemon=True).start()
        return self

    def close(self):
        self._alive.clear()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()

    @property
    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    # ── protocol ─────────────────────────────────────────────────────────────
    def _send(self, record):
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _reader(self):
        fd = self.proc.stdout
        try:
            while self._alive.is_set():
                chunk = fd.read(1)
                if not chunk:
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    self._dispatch(rec)
        finally:
            self._alive.clear()
            self._settled.set()
            with self._lock:
                self._resp_event.notify_all()

    def _dispatch(self, rec):
        t = rec.get("type")
        if t == "response":
            # 大多数回应带 id；`abort` 这类没有，只有 command，按 ("cmd", 名字) 等。
            rid = rec.get("id") or ("cmd", rec.get("command"))
            with self._lock:
                self._responses[rid] = rec
                self._resp_event.notify_all()
            return
        if t == "message_update":
            ev = rec.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_delta":
                self._text.append(ev.get("delta") or "")
            return
        if t == "tool_execution_start":
            name = rec.get("toolName")
            if name:
                self.tools_used.append({"name": name, "args": rec.get("args") or {}})
            return
        if t == "message_end":
            msg = rec.get("message") or {}
            if msg.get("role") == "assistant":
                if msg.get("stopReason") == "error":
                    self._error = msg.get("errorMessage") or "provider error"
                blocks = msg.get("content")
                if isinstance(blocks, list):
                    text = "".join(b.get("text", "") for b in blocks
                                   if isinstance(b, dict) and b.get("type") == "text")
                    if text:
                        self._final = text
                        # 还带着 toolCall 的那条是半路的话，不是交付；出错那次（自动重试前的残稿）也不算。
                        if msg.get("stopReason") != "error" and \
                                not any(isinstance(b, dict) and b.get("type") == "toolCall" for b in blocks):
                            self._finals.append(text)
            return
        if t == "queue_update":
            # 读者线程单独写、整体替换；不抢 _run_lock，免得 steer 发报文时把读者堵住。
            self._queued_steers = [s for s in (rec.get("steering") or []) if isinstance(s, str)]
            return
        # agent_end 不置 _settled：它只是低层这一跑结束，steering / 重试 / 压缩之后还会继续（docs/json.md）。
        if t == "agent_settled":
            self._settled.set()

    def _wait_response(self, rid, timeout):
        end = time.time() + timeout
        with self._lock:
            while rid not in self._responses:
                left = end - time.time()
                if left <= 0 or not self._alive.is_set():
                    return None
                self._resp_event.wait(min(left, 1.0))
            return self._responses.pop(rid)

    def context_usage(self, timeout=5):
        """当前上下文占用（RPC get_session_stats 的 contextUsage）→ (tokens, window)；读不到 (None, None)。

        只给日志看：进程没了、回包慢了、字段缺了都当没有，绝不影响这一轮（2026-11-04）。
        """
        if not self.running:
            return (None, None)
        try:
            with self._lock:
                self._req += 1
                rid = "%s-ctx-%d" % (self.name, self._req)
            self._send({"id": rid, "type": "get_session_stats"})
            rec = self._wait_response(rid, timeout)
        except Exception:
            return (None, None)
        data = rec.get("data") if isinstance(rec, dict) else None
        usage = data.get("contextUsage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return (None, None)
        tokens, window = usage.get("tokens"), usage.get("contextWindow")
        try:
            return (int(tokens), int(window))
        except (TypeError, ValueError):
            return (None, None)

    # ── prompting ────────────────────────────────────────────────────────────
    def prompt(self, text, images=None, timeout=600):
        """Send one user turn, wait for Pi to settle, return the assistant text.

        实测：上一轮 agent_end 之后 Pi 还要收尾一会儿，这期间新 prompt 会被拒
        （`Agent is already processing`）——那是排队，不是错误；退一步重发就是了。
        """
        if not self.running:
            raise PiError("pi process is not running")
        deadline = time.time() + timeout
        resp = None
        for attempt in range(6):
            with self._lock:
                self._req += 1
                rid = f"{self.name}-{self._req}"
                self._text = []
                self._final = None
                self._finals = []
                self._error = None
                self._settled.clear()
            with self._run_lock:
                self._queued_steers = []
            rec = {"id": rid, "type": "prompt", "message": text}
            blocks = image_blocks(images)          # 图片只在第一次尝试里带上，重发不去重
            if blocks:
                rec["images"] = blocks
            self._send(rec)
            resp = self._wait_response(rid, min(max(5, deadline - time.time()), 60))
            if resp is None:
                raise PiError("no response to prompt command")
            if resp.get("success"):
                break
            err = str(resp.get("error") or "")
            if "already processing" in err and time.time() < deadline - 10:
                time.sleep(min(3 * (attempt + 1), 10))
                continue
            raise PiError("prompt rejected: %s" % err)
        if not (resp or {}).get("success"):
            raise PiError("prompt rejected: pi stayed busy")
        if (resp.get("data") or {}).get("disposition") == "handled":
            raise PiError("prompt was consumed by an extension and produced no run")
        with self._run_lock:
            self._run_active = True
        try:
            # 等 agent_settled，不是 agent_end：steer 插进来的话会在低层这一跑之后继续跑，
            # 等早了会把她回补话的那一段丢掉。
            if not self._settled.wait(max(5, deadline - time.time())):
                raise PiError(f"pi did not settle within {timeout}s")
        finally:
            with self._run_lock:
                self._run_active = False
        if not self._alive.is_set() and not self._text:
            raise PiError("pi process exited during the run")
        self.turns += 1
        out = self.reply_text()
        if self._error and not out:
            raise PiError(self._error)
        return out

    def reply_text(self):
        """这一轮她说出口的话：每条定稿正文按顺序拼起来。

        steer 之后一轮里会有不止一条正文（先说了一半，再回他补的那句），只取最后一条会丢前半段；
        流式 delta（`_text`）含被守卫换掉的草稿，只在一条定稿都没有时兜底。
        """
        finals = [t.strip() for t in self._finals if t and t.strip()]
        if finals:
            return "\n\n".join(finals)
        return "".join(self._text).strip()

    def steer(self, text, images=None, timeout=10):
        """RPC `steer`：把一句话插进正在跑的那一轮（docs/rpc-commands.md §steer）。

        `{"type": "steer", "message": "...", "images": [...]}`；回应
        `{"type": "response", "command": "steer", "success": true, "data": {"disposition": "queued"}}`。
        返回 True = 已排进这一轮（她会在这轮里回）；False = 没在跑 / 已经收尾 / RPC 报错，调用方退回排队。

        Pi 的 steer 在 idle 时也不报错，只会静静排进队列等下一次 prompt——那种「话要等到下一轮才递」
        正是要避免的，所以先看 `_run_active`，回应回来再确认这一轮还没 settle；已经 settle 就 clear_queue 撤回。
        同一时刻只发一条（`_steer_lock`）：撤的时候要确认队列里只剩这一句，别把别的话一起撤掉。
        """
        with self._steer_lock:
            if not self.running:
                return False
            with self._run_lock:
                if not self._run_active:
                    return False
                with self._lock:
                    self._req += 1
                    rid = "%s-steer-%d" % (self.name, self._req)
                rec = {"id": rid, "type": "steer", "message": text}
                blocks = image_blocks(images)
                if blocks:
                    rec["images"] = blocks
                try:
                    self._send(rec)
                except Exception:               # noqa: BLE001 - 管道断了按插不进去处理，调用方排队
                    return False
            resp = self._wait_response(rid, timeout)
            if resp is None:
                # 旧版 Pi 的回应不带 id（文档示例如此），按命令名收一次。
                resp = self._wait_response(("cmd", "steer"), 0.2)
            if not (resp and resp.get("success")):
                return False
            with self._run_lock:
                if self._run_active:
                    self.steered += 1
                    return True
                only_ours = list(self._queued_steers) == [text]
            if not only_ours:
                # 队列里还有别的补话：不撤；这一句会留到下一次递给她（不丢），这个请求照常回空。
                return True
            # 回应回来时那一轮已经收尾、队列里只剩这一句：撤回，调用方退回排队。
            return not self.retract(text)

    def retract(self, text, timeout=5):
        """RPC `clear_queue`：把还在排队的 steer 撤回。True = 那句话确实撤回来了（没被这一轮吃掉）。"""
        if not self.running:
            return False
        with self._lock:
            self._req += 1
            rid = "%s-clear-%d" % (self.name, self._req)
        try:
            self._send({"id": rid, "type": "clear_queue"})
        except Exception:                       # noqa: BLE001
            return False
        resp = self._wait_response(rid, timeout)
        if resp is None:
            resp = self._wait_response(("cmd", "clear_queue"), 0.2)
        if not (resp and resp.get("success")):
            return False
        return text in ((resp.get("data") or {}).get("steering") or [])

    def abort(self, timeout=30):
        """RPC `abort`：中止当前这一轮；Pi 回 abort 时它已经回到 idle（docs/rpc-commands.md）。

        回应没有 id，只有 `command: "abort"`，所以按命令名等。返回 False = 没中止成功
        （进程没了、管道断了、或超时仍卡着），调用方据此换身子。
        """
        if not self.running:
            return False
        key = ("cmd", "abort")
        with self._lock:
            self._responses.pop(key, None)
        try:
            self._send({"type": "abort"})
        except Exception:                   # noqa: BLE001 - 管道断了按中止失败
            return False
        resp = self._wait_response(key, timeout)
        if not (resp and resp.get("success")):
            return False
        # abort 等到 idle 才回应；卡住那一轮的 agent_end 也许不会再来了，这里替它放行。
        self._settled.set()
        return True

    def interrupt(self):
        """兼容旧名：只发 abort、不等回应。新代码用 abort()。"""
        self._send({"type": "abort"})

    def set_model(self, provider, model_id):
        """RPC `set_model`：就地换 provider/模型，上下文不丢，返回 True/False。

        pi 只在启动时读一次 models.json：刚从 magpie 合并进来的 provider 它还不认识，
        这时会回 `Model not found: <provider>/<id>` —— 调用方据此换身子重开（新进程会重读）。
        """
        if not self.running:
            return False
        with self._lock:
            self._req += 1
            rid = "%s-model-%d" % (self.name, self._req)
        try:
            self._send({"id": rid, "type": "set_model", "provider": provider, "modelId": model_id})
        except Exception:                       # noqa: BLE001 - 管道断了按失败处理，交给换身子
            return False
        resp = self._wait_response(rid, 15)
        if not resp or not resp.get("success"):
            return False
        self.model = "%s/%s" % (provider, model_id)
        return True


def _selftest_steer():
    """假 Pi：steer 的报文/时机、多段正文拼交付、收尾后才回应的 steer 撤回。不连模型、不起进程。"""
    import json as _json
    sent = []
    state = {"settle_on_reply": False, "omit_id": False, "queued": []}

    class _Stdin:
        def __init__(self, owner):
            self.owner = owner

        def write(self, data):
            rec = _json.loads(data.decode("utf-8"))
            sent.append(rec)
            owner = self.owner
            if rec.get("type") == "steer":
                # 假的读者线程：真实里这些是 _dispatch 在别的线程处理的，不抢 _run_lock
                state["queued"] = [rec.get("message")]
                owner._queued_steers = list(state["queued"])
                if state["settle_on_reply"]:
                    owner._run_active = False
                resp = {"type": "response", "command": "steer", "success": True,
                        "data": {"disposition": "queued"}}
                if not state["omit_id"]:
                    resp["id"] = rec.get("id")
                owner._dispatch(resp)
            elif rec.get("type") == "clear_queue":
                removed, state["queued"] = list(state["queued"]), []
                owner._queued_steers = []
                owner._dispatch({"type": "response", "id": rec.get("id"), "command": "clear_queue",
                                 "success": True, "data": {"steering": removed, "followUp": []}})

        def flush(self):
            pass

        def close(self):
            pass

    class _Proc:
        def __init__(self, owner):
            self.stdin = _Stdin(owner)

        def poll(self):
            return None

    s = PiSession("probe", model="p/m", tools=[])
    s.proc = _Proc(s)

    # 没在跑（收尾/没这一轮）：不发，退回排队
    assert s.steer("不该发") is False and not sent

    # 在跑：发 steer，带上图片
    s._run_active = True
    assert s.steer("插一句", images=["data:image/png;base64,QUJD"]) is True
    rec = sent[-1]
    assert rec["type"] == "steer" and rec["message"] == "插一句" and "id" in rec
    assert rec["images"] == [{"type": "image", "data": "QUJD", "mimeType": "image/png"}], rec
    assert s.steered == 1

    # 旧版 Pi 回应不带 id：按命令名也能收到
    state["omit_id"] = True
    assert s.steer("旧版也行", timeout=0.2) is True
    state["omit_id"] = False

    # 回应回来时这一轮已经收尾：撤回，返回 False 让调用方去排队
    state["settle_on_reply"] = True
    assert s.steer("太晚了", timeout=0.5) is False
    assert sent[-1]["type"] == "clear_queue", sent[-1]
    state["settle_on_reply"] = False

    # agent_end 不是收尾（steer 之后还会继续）；每条不带 toolCall 的定稿正文都要留着
    s._finals, s._text, s._final = [], [], None
    s._settled.clear()
    s._dispatch({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "前半句"}})
    s._dispatch({"type": "message_end",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "前半句"}]}})
    s._dispatch({"type": "agent_end", "messages": [], "willRetry": False})
    assert not s._settled.is_set(), "agent_end 只是低层这一跑结束，steer 之后还会继续"
    s._dispatch({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "好"}})
    s._dispatch({"type": "message_end",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "好，我听见了。"}]}})
    s._dispatch({"type": "agent_settled"})
    assert s._settled.is_set()
    assert s.reply_text() == "前半句\n\n好，我听见了。", s.reply_text()

    # 半路带 toolCall 的话不算交付（和原来只给最后一段的行为一致）
    s._finals, s._final = [], None
    s._dispatch({"type": "message_end", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "我查一下"},
        {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {}}]}})
    assert s._finals == [] and s._final == "我查一下"
    print("PI STEER SELFTEST OK")


def _selftest():
    """Smallest thing that fails if the RPC plumbing breaks: two turns, memory kept."""
    _selftest_steer()
    s = PiSession("selftest", model="proxytest/deepseek/deepseek-v4-flash",
                  cwd="/root", tools=[], log_path="/root/_tmp/pi_selftest.err")
    s.start()
    try:
        first = s.prompt("记住一个数：十七。只回复两个字：好的")
        second = s.prompt("刚才那个数是多少？只回复数字")
        print("turn1:", repr(first))
        print("turn2:", repr(second))
        assert first, "empty first reply"
        assert "17" in second or "十七" in second, "no memory across turns"
        assert s.turns == 2
        print("PI RPC SELFTEST OK")
    finally:
        s.close()


if __name__ == "__main__":
    if "--steer-only" in sys.argv:
        _selftest_steer()
    else:
        _selftest()
