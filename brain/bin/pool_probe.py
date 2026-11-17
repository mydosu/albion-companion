#!/usr/bin/env python3
"""grok 号池（:8000）探活告警 —— 一次性脚本，给 WSL 的定时任务每 2 分钟跑一次（2026-11-17）。

11-12 到 11-16 号池断了 5 天没人发现：她画图、看图转述、联网搜全走它，断了她不吭声，日志里也没痕迹。
每次跑只做三件事：GET 一次号池的 /models（只探活，不调任何模型接口、不占号池额度）→
按需要同步发一条 Discord（断的告警 / 恢复）→ 写一次 state/pool_probe.json，然后退出。
**不碰宿主的 server.py**：这一份自己管自己。

规则和 bin/brain_alert.py 里门铃那一段一致：
  · 连续断满 ALERT_AFTER_S（600 秒）发一次，之后还断着就每隔 brain_alert.REPEAT_AFTER_S（3 小时）再发；
  · 告警过的，通了发一条恢复，然后清掉 since / alerted_at；没告警过的，通了只清零、不发；
  · 没有 webhook 就不记 alerted_at（不算发过），日志写 brain alert skipped reason=no_webhook；
  · 只在状态变化时写日志：第一次断、恢复。每 2 分钟通一次不写——是不是还活着看 state 里的 checked_at。
key 只从 agent/models.json 现读现用，绝不写进日志、state 文件或输出。

用法（生产）：python3 /root/albion-pi/bin/pool_probe.py
             python3 /root/albion-pi/bin/pool_probe.py --print-state   # 只看状态，不探、不发
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import brain_alert          # 同目录：webhook 读法、post_webhook、proxy、log 都复用它这一份

BRAIN_DIR = Path(__file__).resolve().parent.parent       # bin/ 的上一级就是脑目录
STATE_NAME = "pool_probe.json"
DEFAULT_BASE = "http://127.0.0.1:8000/v1"                # 和 agent/extensions/albion-draw.ts 的 pool() 一致
ALERT_AFTER_S = 600                                      # 连续断满 10 分钟发第一次
PROBE_TIMEOUT = 10
USER_AGENT = "albion-pool-probe/1.0"


# ── 读号池配置（每次现读：key 换了不用重启）─────────────────────────────────
def pool_target(root=None):
    """agent/models.json 的 providers.grokpool → (base, key)；缺了就用默认地址、空 key。"""
    base, key = DEFAULT_BASE, ""
    try:
        with open(Path(root or BRAIN_DIR) / "agent" / "models.json", encoding="utf-8") as f:
            cfg = json.load(f)
        p = ((cfg or {}).get("providers") or {}).get("grokpool") or {}
        base = str(p.get("baseUrl") or base)
        key = str(p.get("apiKey") or "")
    except (OSError, ValueError, AttributeError, TypeError):
        pass                                                  # 读不到就用默认地址继续探
    return base.rstrip("/"), key


# ── 探一次 ──────────────────────────────────────────────────────────────────
def _reason(exc):
    """把一次失败压成短词：status=401 / ConnectionRefusedError / timeout。不带 URL、不带 key。"""
    if isinstance(exc, urllib.error.HTTPError):
        return "status=%d" % exc.code
    inner = getattr(exc, "reason", None)
    if inner is not None:
        if isinstance(inner, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(inner, str):
            return "timeout" if "timed out" in inner.lower() else inner[:60]
        return type(inner).__name__
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if "timed out" in str(exc).lower():
        return "timeout"
    return type(exc).__name__


def probe(base, key="", *, opener=None, timeout=PROBE_TIMEOUT):
    """GET <base>/models：只有 200 算通。返回 (ok, reason)，reason 只在断的时候有。"""
    request = urllib.request.Request(base.rstrip("/") + "/models", headers={"User-Agent": USER_AGENT})
    if key:
        request.add_header("Authorization", "Bearer " + key)
    # 号池在本机：不走代理（WSL 的 http_proxy 会把它转去 7897）
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status == 200:
                return True, None
            return False, "status=%d" % response.status
    except Exception as exc:                                  # noqa: BLE001 - 连不上/超时/401 都算断
        return False, _reason(exc)


# ── state/pool_probe.json ───────────────────────────────────────────────────
def state_path(root=None):
    return Path(root or BRAIN_DIR) / "state" / STATE_NAME


def _blank_state():
    return {"ok": None, "checked_at": None, "since": None, "alerted_at": None, "reason": None}


def load_state(path=None):
    """读上一次的状态；文件缺失、坏了都当「还没探过」。"""
    st = _blank_state()
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            for k in st:
                if k in got:
                    st[k] = got[k]
    except (OSError, ValueError):
        pass
    return st


def save_state(st, path=None):
    """先写临时文件再 rename：定时任务和手工查看撞上也不会读到半个 JSON。"""
    path = Path(path or state_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        brain_alert.log("grok pool state write failed error=%s" % type(exc).__name__)


# ── 文案（照主脑给的原文）────────────────────────────────────────────────────
def alert_text(minutes, reason):
    return ("grok 号池（:8000）连不上 %d 分钟（%s）：她画图、看图转述、联网搜都用不了。"
            "先在 WSL 里 cd /root/grok-pool && docker compose up -d --force-recreate，别只 restart。"
            % (minutes, reason))


def recover_text(minutes):
    return "grok 号池恢复了：断了 %d 分钟。" % minutes


def _minutes(since, now):
    return max(0, int((now - (since or now)) / 60))


def _clock(now):
    """now 可以是个取时间的函数（默认 time.time），也接受直接给的时间戳。"""
    return float(now() if callable(now) else now)


def _post_now(text, url, post):
    """同步发完再返回 —— 不用 brain_alert._send：它起后台线程，脚本一退出消息可能还在路上。"""
    try:
        brain_alert.log("brain alert sent status=%s" % post(url, text))
        return True
    except urllib.error.HTTPError as exc:
        brain_alert.log("brain alert failed status=%s" % exc.code)
    except Exception as exc:                                  # noqa: BLE001
        brain_alert.log("brain alert failed status=0 error=%s" % type(exc).__name__)
    return False


# ── 一次探活 + 按规则告警 ───────────────────────────────────────────────────
def run_once(*, now=time.time, post=None, root=None, opener=None):
    """探一次、该发就发、落一次 state。返回这次写下的 state（测试直接看它）。"""
    root = Path(root) if root is not None else BRAIN_DIR
    path = state_path(root)
    post = post or brain_alert.post_webhook
    stamp = _clock(now)
    base, key = pool_target(root)
    ok, reason = probe(base, key, opener=opener)

    st = load_state(path)
    st["ok"] = ok
    st["checked_at"] = stamp

    if ok:
        st["reason"] = None
        started = st.get("since")
        alerted = st.get("alerted_at") is not None
        st["since"] = None
        st["alerted_at"] = None
        if not alerted:
            # 没告警过：只清零，不读 webhook、不发东西（也不写日志）
            save_state(st, path)
            return st
        minutes = _minutes(started, stamp)
        save_state(st, path)
        brain_alert.log("grok pool recovered minutes=%d" % minutes)
        url = brain_alert.webhook_url()
        if not url:
            brain_alert.log("brain alert skipped reason=no_webhook")
        else:
            _post_now(recover_text(minutes), url, post)
        return st

    st["reason"] = reason
    if st.get("since") is None:
        st["since"] = stamp
        # 每 2 分钟探一次，只在开头记一行：断了至少日志里有痕迹
        brain_alert.log("grok pool down reason=%s" % (reason or "unknown"))
    due = (stamp - float(st["since"]) >= ALERT_AFTER_S and
           (st.get("alerted_at") is None or stamp - float(st["alerted_at"]) >= brain_alert.REPEAT_AFTER_S))
    if not due:
        save_state(st, path)
        return st
    url = brain_alert.webhook_url()
    if not url:
        # 没 webhook 就不算发过：不记 alerted_at，下一次探不通还会再试
        brain_alert.log("brain alert skipped reason=no_webhook")
        save_state(st, path)
        return st
    st["alerted_at"] = stamp
    save_state(st, path)          # 先落盘再发：发到一半被杀掉也不会每 2 分钟重发一遍
    _post_now(alert_text(_minutes(st["since"], stamp), reason or "unknown"), url, post)
    return st


def main(argv=None, *, now=time.time, post=None, root=None, opener=None):
    """命令行入口：默认探一次就走；--print-state 只读 state 文件（不探、不发、不写）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(root) if root is not None else BRAIN_DIR
    if "--print-state" in argv:
        print(json.dumps(load_state(state_path(root)), ensure_ascii=False))
        return 0
    try:
        run_once(now=now, post=post, root=root, opener=opener)
    except Exception as exc:                                  # noqa: BLE001 - 定时任务不能因为探针自己出错就炸
        brain_alert.log("grok pool probe failed error=%s" % type(exc).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
