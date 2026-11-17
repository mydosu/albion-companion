#!/usr/bin/env python3
"""grok 号池探活告警 —— bin/pool_probe.py 的自检（2026-11-17）。

不联网、不真发：假时钟 + 假 post + 临时 state 目录，本地起一个能切 200/401 的假号池，
再拿一个没人监听的端口当「连不上」。
验的规则（和 brain_alert.py 门铃那一段一致）：
  1) 第一次断只记日志、不发，state 里记下 since；后续每 2 分钟探一次不再重复记日志
  2) 断不满 600 秒不发
  3) 满 600 秒发一次告警（分钟数、reason、修复命令都对）
  4) 3 小时内不重发，满 3 小时再发一次
  5) 告警过的通了发恢复（分钟数从 since 算），然后 since/alerted_at 清零
  6) 没告警过的通了什么都不发，只清零
  7) 没有 webhook：满 600 秒也不算发过（alerted_at 还是 None），补上 webhook 后照发
  8) reason：401 → status=401；没人监听 → ConnectionRefusedError；不回包 → timeout
  9) key 不进 state 文件、不进日志、不进 --print-state 的输出

跑：python3 tests/pi-brain/check_pool_probe.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "brain", "bin"))

import brain_alert  # noqa: E402
import pool_probe   # noqa: E402

API_KEY = "test-key-not-real"


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Post:
    """同步的假发送：pool_probe 是当场发完才退出，所以不需要等后台线程。"""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def __call__(self, url, message):
        self.calls.append((url, message))
        if self.exc:
            raise self.exc
        return 204


class StubPool:
    """本地假号池：/v1/models 只在 key 对时回 200；mode=401 时一律 401；mode=slow 时不回包。"""

    def __init__(self, key=API_KEY):
        self.key = key
        self.mode = "ok"
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append((self.path, self.headers.get("Authorization")))
                if outer.mode == "slow":
                    time.sleep(2.5)
                good = (outer.mode == "ok" and self.path == "/v1/models"
                        and self.headers.get("Authorization") == "Bearer " + outer.key)
                body = b'{"data": []}' if good else b'{"error": "unauthorized"}'
                self.send_response(200 if good else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dead_base():
    """没人监听的端口（绑一下拿到号就关掉，再去连它）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return "http://127.0.0.1:%d/v1" % port


def setup(tmp, base, *, webhook=True, key=API_KEY):
    """每个用例一份干净的脑目录：agent/models.json + state/ 都放临时目录里。"""
    brain_alert.LOGS = Path(tmp) / "logs"
    # 两个 webhook 候选都指到临时目录，别读到真的
    brain_alert.WEBHOOK = Path(tmp) / "webhook"
    brain_alert.LOCAL_WEBHOOK = Path(tmp) / "local-webhook"
    if webhook:
        brain_alert.WEBHOOK.write_text("https://discord.test/api/webhooks/1/x\n", encoding="utf-8")
    else:
        brain_alert.WEBHOOK.unlink(missing_ok=True)
        brain_alert.LOCAL_WEBHOOK.unlink(missing_ok=True)
    root = Path(tmp) / "brain"
    (root / "agent").mkdir(parents=True, exist_ok=True)
    (root / "agent" / "models.json").write_text(json.dumps(
        {"providers": {"grokpool": {"baseUrl": base, "apiKey": key}}}), encoding="utf-8")
    shutil.rmtree(root / "state", ignore_errors=True)
    return root


def state(root):
    return json.loads((Path(root) / "state" / "pool_probe.json").read_text(encoding="utf-8"))


def logs(tmp):
    path = Path(tmp) / "logs" / "brain.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def down_logs(tmp):
    return [line for line in logs(tmp) if "grok pool down" in line]


# ── 1)–4) 断的那一段 ────────────────────────────────────────────────────────
def check_down_flow(tmp, stub):
    stub.mode = "401"
    root = setup(tmp, stub.base)
    clock, post = Clock(), Post()
    kw = dict(now=clock, post=post, root=root)

    # 1) 第一次断：只记一行日志，不发；already 有 since
    pool_probe.main(**kw)
    st = state(root)
    assert st["ok"] is False and st["since"] == clock.now and st["alerted_at"] is None, st
    assert st["reason"] == "status=401" and st["checked_at"] == clock.now, st
    assert post.calls == [], post.calls
    lines = down_logs(tmp)
    assert len(lines) == 1 and "reason=status=401" in lines[0], lines
    # 每 2 分钟再探一次（还断着）：不再重复记日志
    for _ in range(3):
        clock.now += 120
        pool_probe.main(**kw)
    assert len(down_logs(tmp)) == 1, down_logs(tmp)
    assert state(root)["since"] == 1_700_000_000.0, state(root)
    print("1) 第一次断只记一行日志、不发；后面每 2 分钟探一次不再重复记 OK：%s" % lines[0])

    # 2) 不满 600 秒不发
    clock.now = 1_700_000_000.0 + pool_probe.ALERT_AFTER_S - 1
    pool_probe.main(**kw)
    assert post.calls == [] and state(root)["alerted_at"] is None, (post.calls, state(root))
    print("2) 断不满 %d 秒不发 OK" % pool_probe.ALERT_AFTER_S)

    # 3) 满 600 秒发一次
    clock.now += 1
    pool_probe.main(**kw)
    assert len(post.calls) == 1, post.calls
    url, text = post.calls[0]
    assert url.startswith("https://discord.test/"), url
    assert "grok 号池（:8000）连不上 10 分钟（status=401）" in text, text
    assert "画图、看图转述、联网搜都用不了" in text, text
    assert "cd /root/grok-pool && docker compose up -d --force-recreate" in text and "别只 restart" in text, text
    assert state(root)["alerted_at"] == clock.now, state(root)
    sent = [line for line in logs(tmp) if "brain alert sent status=204" in line]
    assert len(sent) == 1, logs(tmp)
    print("3) 满 600 秒发一次、文案与分钟数都对 OK：%s" % text)

    # 4) 3 小时内不重发，满 3 小时再发
    clock.now += brain_alert.REPEAT_AFTER_S - 1
    pool_probe.main(**kw)
    assert len(post.calls) == 1, post.calls
    clock.now += 1
    pool_probe.main(**kw)
    assert len(post.calls) == 2, post.calls
    assert "连不上" in post.calls[1][1], post.calls[1][1]
    print("4) 3 小时内不重发、满 3 小时再发 OK")

    # 5) 告警过的通了：发恢复（分钟数从 since 算），然后清零
    stub.mode = "ok"
    clock.now = 1_700_000_000.0 + 15 * 60      # 从断的那一刻起 15 分钟
    pool_probe.main(**kw)
    assert len(post.calls) == 3, post.calls
    recover = post.calls[2][1]
    assert recover == "grok 号池恢复了：断了 15 分钟。", recover
    st = state(root)
    assert st["ok"] is True and st["since"] is None and st["alerted_at"] is None and st["reason"] is None, st
    # 恢复之后再通：什么都不发
    clock.now += 120
    pool_probe.main(**kw)
    assert len(post.calls) == 3, post.calls
    assert any("grok pool recovered minutes=15" in line for line in logs(tmp)), logs(tmp)
    print("5) 告警过的通了发恢复（%s）并清零 OK" % recover)


# ── 6) 没告警过的通了不发 ───────────────────────────────────────────────────
def check_ok_without_alert(tmp, stub):
    stub.mode = "401"
    root = setup(tmp, stub.base)
    clock, post = Clock(), Post()
    kw = dict(now=clock, post=post, root=root)
    pool_probe.main(**kw)                      # 断 120 秒，还没到 10 分钟
    clock.now += 120
    pool_probe.main(**kw)
    assert post.calls == [] and state(root)["alerted_at"] is None, state(root)
    stub.mode = "ok"
    clock.now += 20
    pool_probe.main(**kw)
    assert post.calls == [], post.calls
    st = state(root)
    assert st["ok"] is True and st["since"] is None and st["alerted_at"] is None, st
    print("6) 没告警过的通了不发、只清零 OK")


# ── 7) 没有 webhook ────────────────────────────────────────────────────────
def check_no_webhook(tmp, stub):
    stub.mode = "401"
    root = setup(tmp, stub.base, webhook=False)
    clock, post = Clock(), Post()
    kw = dict(now=clock, post=post, root=root)
    pool_probe.main(**kw)
    clock.now += pool_probe.ALERT_AFTER_S
    pool_probe.main(**kw)
    assert post.calls == [], "没 webhook 就不该发"
    assert state(root)["alerted_at"] is None, "没发出去就不算发过：%s" % state(root)
    assert any("brain alert skipped reason=no_webhook" in line for line in logs(tmp)), logs(tmp)
    # 补上 webhook：下一次探不通照发（skipped 不算发过）
    brain_alert.WEBHOOK.write_text("https://discord.test/api/webhooks/1/x", encoding="utf-8")
    clock.now += 120
    pool_probe.main(**kw)
    assert len(post.calls) == 1, post.calls
    assert state(root)["alerted_at"] == clock.now, state(root)
    print("7) 没有 webhook 只记 skipped、不记 alerted_at；补上之后照发 OK")


# ── 8) reason ──────────────────────────────────────────────────────────────
def check_reasons(tmp, stub):
    # 401
    stub.mode = "401"
    root = setup(tmp, stub.base)
    pool_probe.run_once(now=Clock(1_700_000_000.0), post=Post(), root=root)
    assert state(root)["reason"] == "status=401", state(root)
    assert stub.seen[-1] == ("/v1/models", "Bearer " + API_KEY), stub.seen[-1]
    # 端口没人监听
    root = setup(tmp, dead_base())
    pool_probe.run_once(now=Clock(1_700_000_000.0), post=Post(), root=root)
    assert state(root)["reason"] == "ConnectionRefusedError", state(root)
    # 不回包（超时）
    stub.mode = "slow"
    ok, reason = pool_probe.probe(stub.base, API_KEY, timeout=1)
    assert ok is False and reason == "timeout", (ok, reason)
    # 通了就是 ok=None reason
    stub.mode = "ok"
    ok, reason = pool_probe.probe(stub.base, API_KEY)
    assert ok is True and reason is None, (ok, reason)
    assert stub.seen[-1] == ("/v1/models", "Bearer " + API_KEY), stub.seen[-1]
    print("8) reason 判定 OK：status=401 / ConnectionRefusedError / timeout；且只打 /models、带对了 key")


# ── 9) key 不外泄 ──────────────────────────────────────────────────────────
def check_no_key_leak(tmp, stub):
    stub.mode = "401"
    root = setup(tmp, stub.base)
    clock, post = Clock(), Post()
    kw = dict(now=clock, post=post, root=root)
    pool_probe.main(**kw)
    clock.now += pool_probe.ALERT_AFTER_S
    pool_probe.main(**kw)
    stub.mode = "ok"
    clock.now += 60
    pool_probe.main(**kw)
    raw = (Path(root) / "state" / "pool_probe.json").read_text(encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        pool_probe.main(["--print-state"], **kw)
    printed = out.getvalue().strip()
    everything = raw + "\n" + printed + "\n" + "\n".join(logs(tmp)) + "\n".join(t for _, t in post.calls)
    assert API_KEY not in everything, "key 漏出去了：%s" % everything[:400]
    assert json.loads(raw) and json.loads(printed)  # 还是合法 JSON
    print("9) key 不进 state 文件 / 日志 / --print-state / 告警正文 OK；--print-state = %s" % printed)


def main():
    with tempfile.TemporaryDirectory(prefix="pool-probe-") as tmp:
        stub = StubPool()
        try:
            check_down_flow(tmp, stub)
            check_ok_without_alert(tmp, stub)
            check_no_webhook(tmp, stub)
            check_reasons(tmp, stub)
            check_no_key_leak(tmp, stub)
        finally:
            stub.close()
    print("POOL PROBE SELFTEST OK")


if __name__ == "__main__":
    main()
