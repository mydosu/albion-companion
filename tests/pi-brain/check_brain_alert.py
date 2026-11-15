#!/usr/bin/env python3
"""脑哑了要喊人 —— bin/brain_alert.py 的自检（2026-11-15）。

不联网：假时钟 + 假 post + 临时 webhook 文件，全程不碰生产。

验的规则：
  1) 连续第 1 轮失败不喊，第 2 轮喊一次
  2) 3 小时内继续失败不再喊；满 3 小时的那次失败再喊
  3) 告警后第一次成功发一条恢复（哑了多少分钟、失败几轮），然后清零；没告警过的成功什么都不发
  4) 脱敏：Bearer / sk- / URL 里 key=、token= 抹成 ***；错误摘要截 200 字
  5) post 抛异常不往外抛，这一轮照常算过
  6) webhook 文件缺失：记 brain alert skipped reason=no_webhook，不算发过
  7) 20 个线程同时记失败：计数不漏，同一秒只喊一条
  8) 外网走代理：默认 127.0.0.1:7897（和 chat_bridge 的 ALBION_CHAT_PROXY 同一个变量），空串则直连

跑：python3 tests/pi-brain/check_brain_alert.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import urllib.request
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "brain", "bin"))

import brain_alert  # noqa: E402

SID = "albion-desktop"


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Post:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def __call__(self, url, message):
        self.calls.append((url, message))
        if self.exc:
            raise self.exc
        return 204

    def wait(self, n, timeout=5.0):
        """发送在后台线程：等到第 n 条落地（或超时）。"""
        deadline = time.time() + timeout
        while len(self.calls) < n and time.time() < deadline:
            time.sleep(0.01)
        return len(self.calls)


def setup(tmp, webhook=True):
    """每个用例重置内存计数、日志目录和 webhook 文件。"""
    brain_alert._state.clear()
    brain_alert.LOGS = Path(tmp) / "logs"
    brain_alert.WEBHOOK = Path(tmp) / "webhook"          # 两个候选都指到临时目录，别读到真的
    brain_alert.LOCAL_WEBHOOK = Path(tmp) / "local-webhook"
    if webhook:
        brain_alert.WEBHOOK.write_text("https://discord.test/api/webhooks/1/x\n", encoding="utf-8")
    else:
        brain_alert.WEBHOOK.unlink(missing_ok=True)


def log_lines(tmp):
    path = Path(tmp) / "logs" / "brain.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def check_two_failures(tmp):
    setup(tmp)
    clock, post = Clock(), Post()
    kw = dict(clock=clock, post=post)
    assert brain_alert.record_failure(SID, "502 bad gateway", "cc/ds", **kw) is False, "第 1 轮不该喊"
    time.sleep(0.05)
    assert post.calls == [], post.calls
    clock.now += 60
    assert brain_alert.record_failure(SID, "502 bad gateway", "cc/ds", **kw) is True, "第 2 轮该喊"
    assert post.wait(1) == 1, "第 2 轮失败应该发一条告警"
    url, text = post.calls[0]
    assert url.startswith("https://discord.test/"), url
    assert "连续 2 轮" in text and SID in text and "cc/ds" in text and "502 bad gateway" in text, text
    print("1) 第 1 轮不发、第 2 轮发一条 OK")
    print("   告警示例：%s" % text)


def check_repeat_window(tmp):
    setup(tmp)
    clock, post = Clock(), Post()
    kw = dict(clock=clock, post=post)
    brain_alert.record_failure(SID, "403 unauthorized", None, **kw)
    clock.now += 60
    brain_alert.record_failure(SID, "403 unauthorized", None, **kw)
    assert post.wait(1) == 1
    clock.now += brain_alert.REPEAT_AFTER_S - 1
    assert brain_alert.record_failure(SID, "403 unauthorized", None, **kw) is False, "3 小时内不该再喊"
    time.sleep(0.05)
    assert len(post.calls) == 1, post.calls
    clock.now += 1
    assert brain_alert.record_failure(SID, "403 unauthorized", None, **kw) is True, "满 3 小时该再喊"
    assert post.wait(2) == 2
    assert "连续 4 轮" in post.calls[1][1], post.calls[1][1]
    print("2) 3 小时内不重复、满 3 小时再喊一次 OK")


def check_recovery(tmp):
    setup(tmp)
    clock, post = Clock(), Post()
    kw = dict(clock=clock, post=post)
    brain_alert.record_failure(SID, "boom", "cc/ds", **kw)
    clock.now += 30
    brain_alert.record_failure(SID, "boom", "cc/ds", **kw)
    assert post.wait(1) == 1
    clock.now += 300
    brain_alert.record_failure(SID, "boom", "cc/ds", **kw)
    clock.now += 3900                      # 从第一次失败起 70 分钟
    assert brain_alert.record_success(SID, **kw) is True, "告警过就该发恢复"
    assert post.wait(2) == 2
    recover = post.calls[1][1]
    assert "回魂" in recover and "70 分钟" in recover and "失败 3 轮" in recover, recover
    assert brain_alert.record_success(SID, **kw) is False, "清零后不该再发"
    time.sleep(0.05)
    assert len(post.calls) == 2, post.calls
    setup(tmp)
    post2 = Post()
    assert brain_alert.record_success(SID, clock=clock, post=post2) is False
    brain_alert.record_failure(SID, "boom", None, clock=clock, post=post2)
    assert brain_alert.record_success(SID, clock=clock, post=post2) is False, "只失败 1 轮不算告警"
    time.sleep(0.05)
    assert post2.calls == [], post2.calls
    print("3) 告警后成功发恢复 + 没告警过的成功不发 OK")
    print("   恢复示例：%s" % recover)


def check_scrub(tmp):
    setup(tmp)
    raw = ("401 Unauthorized: Authorization: Bearer abc.DEF-123 "
           "key sk-abcdef1234567890 via https://api.test/v1/chat?key=KEYVALUE123&x=1 "
           "and ?token=TOK456 " + "长" * 300)
    clean = brain_alert.scrub(raw)
    assert "abc.DEF-123" not in clean and "Bearer ***" in clean, clean
    assert "sk-abcdef1234567890" not in clean and "sk-***" in clean, clean
    assert "KEYVALUE123" not in clean and "TOK456" not in clean, clean
    assert "key=***" in clean and "token=***" in clean and "&x=1" in clean, clean
    assert len(clean) == brain_alert.ERROR_MAX_CHARS, len(clean)
    post = Post()
    brain_alert.record_failure(SID, "Bearer tkn-1 sk-zzz9 ?token=QQQ", None, clock=Clock(), post=post)
    brain_alert.record_failure(SID, "Bearer tkn-1 sk-zzz9 ?token=QQQ", None, clock=Clock(), post=post)
    assert post.wait(1) == 1
    text = post.calls[0][1]
    for leaked in ("tkn-1", "sk-zzz9", "QQQ"):
        assert leaked not in text, (leaked, text)
    print("4) 脱敏（Bearer/sk-/key=/token=）+ 截 200 字 OK：%s" % clean[:80])


def check_post_raises(tmp):
    setup(tmp)
    clock, post = Clock(), Post(exc=RuntimeError("network down"))
    kw = dict(clock=clock, post=post)
    brain_alert.record_failure(SID, "boom", None, **kw)
    clock.now += 60
    assert brain_alert.record_failure(SID, "boom", None, **kw) is True
    assert post.wait(1) == 1
    time.sleep(0.1)
    lines = log_lines(tmp)
    assert any("brain alert failed status=0 error=RuntimeError" in line for line in lines), lines
    assert brain_alert.record_success(SID, **kw) is True
    assert post.wait(2) == 2
    print("5) post 抛异常不外抛、只记一行 failed OK：%s" % lines[-1])


def check_no_webhook(tmp):
    setup(tmp, webhook=False)
    clock, post = Clock(), Post()
    brain_alert.record_failure(SID, "boom", None, clock=clock, post=post)
    clock.now += 60
    assert brain_alert.record_failure(SID, "boom", None, clock=clock, post=post) is False,         "没发出去就不算发过"
    time.sleep(0.1)
    assert post.calls == [], "没 webhook 就不该调用 post"
    lines = log_lines(tmp)
    assert any("brain alert skipped reason=no_webhook" in line for line in lines), lines
    brain_alert.WEBHOOK.write_text("https://discord.test/api/webhooks/1/x", encoding="utf-8")
    clock.now += 60
    assert brain_alert.record_failure(SID, "boom", None, clock=clock, post=post) is True, "skipped 不算发过"
    assert post.wait(1) == 1
    print("6) webhook 缺失只记 skipped、不算发过 OK：%s" % lines[-1])


def check_threads_safe(tmp):
    setup(tmp)
    post = Post()
    clock = Clock()

    def worker():
        brain_alert.record_failure(SID, "boom", None, clock=clock, post=post)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert brain_alert._state[SID]["fails"] == 20, brain_alert._state[SID]
    assert post.wait(1) == 1, "同一秒 20 次失败只喊一条"
    print("7) 20 线程并发计数正确（fails=20，只喊 1 条）OK")



def proxy_handler(opener):
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.ProxyHandler):
            return handler
    raise AssertionError("opener 里没有 ProxyHandler：%r" % (opener.handlers,))


def check_proxy(tmp):
    """只构造 opener、不发请求：WSL 直连 DNS 超时，必须显式挂代理。"""
    if not os.environ.get("ALBION_CHAT_PROXY"):
        assert brain_alert.PROXY == "http://127.0.0.1:7897", brain_alert.PROXY
    assert proxy_handler(brain_alert.proxy_opener()).proxies == {
        "http": brain_alert.PROXY, "https": brain_alert.PROXY}, "http/https 都要挂"
    # 空 proxies 的 ProxyHandler 会被 build_opener 直接省掉：总之不能留下任何代理
    assert not [h for h in brain_alert.proxy_opener("").handlers
                if isinstance(h, urllib.request.ProxyHandler) and h.proxies], "空串要直连，不挂代理"
    print("8) 走代理（默认 127.0.0.1:7897，空串直连）OK：%s" % brain_alert.PROXY)


def main():
    with tempfile.TemporaryDirectory(prefix="brain-alert-") as tmp:
        check_two_failures(tmp)
        check_repeat_window(tmp)
        check_recovery(tmp)
        check_scrub(tmp)
        check_post_raises(tmp)
        check_no_webhook(tmp)
        check_threads_safe(tmp)
        check_proxy(tmp)
    print("BRAIN ALERT SELFTEST OK")


if __name__ == "__main__":
    main()
