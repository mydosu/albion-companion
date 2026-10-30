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
        self._error: str | None = None
        self._settled = threading.Event()
        self._alive = threading.Event()
        self._req = 0
        self.turns = 0
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
            return
        if t == "agent_end":
            self._settled.set()
            return
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
                self._error = None
                self._settled.clear()
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
        if not self._settled.wait(max(5, deadline - time.time())):
            raise PiError(f"pi did not settle within {timeout}s")
        if not self._alive.is_set() and not self._text:
            raise PiError("pi process exited during the run")
        self.turns += 1
        out = (self._final if self._final else "".join(self._text)).strip()
        if self._error and not out:
            raise PiError(self._error)
        return out

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


def _selftest():
    """Smallest thing that fails if the RPC plumbing breaks: two turns, memory kept."""
    import sys
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
    _selftest()
