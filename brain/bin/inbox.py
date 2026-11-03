#!/usr/bin/env python3
"""未回箱（2026-11-02）：平台上他发来的、她还没回的消息。

谁碰它：
  · `bin/chat_bridge.py`（唯一写者）：收到消息 `add()`，叫醒前 `mark_notified()`,她回了 `mark_answered()`；
  · `bin/wake.py`：醒来提示里念 `pending()` 里他发来的那几条，这一轮结束后 `close()`
    （回过的已经是 answered，没回的就是她决定不回）；
  · `bin/server.py`：只认 `【平台消息】` 这个标记，不直接读写这份文件。

约定：
  · 只用标准库（宿主和桥都能 import）；
  · 原子写：先写 `.inbox.json.tmp` 再 `os.replace`；
  · 坏文件不覆盖：改名 `inbox.json.corrupt-<毫秒>` 留底，再从空的开始（和 `wake._read` 一个口径）；
  · 只留最近 `KEEP` 条；
  · 状态：`unread`（新来的）→ `notified`（叫醒过）→ `answered`（她回了）/ `closed`（这轮醒来了结）。
    群里别的 bot 的话只当上下文：进来就是 `closed`，永远不进「待回」。
    悄悄话（discord_whisper）例外：可畏说的话也进待回（why=whisper），她得能回。
  · 跨进程读改写用 `fcntl` 文件锁（Windows 上没有 fcntl，退化成进程内线程锁，够自检用）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

VERSION = 1
KEEP = 200                     # 只留最近这么多条
MAX_TEXT = 4000                # 单条正文上限（TG 一条消息上限 4096）
MAX_AUTHOR = 80
# context = 群里别人的话：只给她当上下文看，永远不进待回、也不驱动叫醒（2026-11-02 复审）
STATES = ("unread", "notified", "answered", "closed", "context")
CHANNELS = ("telegram", "discord", "discord_group", "discord_whisper")
CHANNEL_LABELS = {"telegram": "Telegram", "discord": "Discord 私信", "discord_group": "Discord 群",
                  "discord_whisper": "Discord 悄悄话"}


def channel_label(channel):
    """写给她/日志看的渠道名（未回箱、通知轮、醒来提示共用一套）。"""
    return CHANNEL_LABELS.get(str(channel or ""), str(channel or "?"))

_LOCK = threading.RLock()

try:                                        # 生产是 Linux；Windows 自检时没有 fcntl
    import fcntl
except ImportError:                         # pragma: no cover - 平台差异
    fcntl = None


def _state_dir():
    return os.environ.get("ALBION_STATE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")


def path():
    return os.path.join(_state_dir(), "inbox.json")


def _blank():
    return {"version": VERSION, "seq": 0, "items": []}


def _corrupt(p):
    try:
        os.replace(p, "%s.corrupt-%d" % (p, int(time.time() * 1000)))
    except OSError:
        pass


def _norm(data):
    """读回来的东西收拾成合法结构；不像就直接当坏文件（调用方改名留底）。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("bad inbox shape")
    items = []
    for x in data["items"]:
        if not isinstance(x, dict):
            continue
        try:
            iid = int(x.get("id"))
        except (TypeError, ValueError):
            continue
        state = x.get("state") if x.get("state") in STATES else "closed"
        sender = str(x.get("from") or "owner")
        why = str(x.get("why") or "")
        needs = bool(x.get("needs")) if "needs" in x else (sender == "owner" or why in ("mention", "whisper"))
        if not needs and state in ("unread", "notified"):
            state = "context"          # 旧数据/上下文条目：留在箱里当上下文，不进待回
        items.append({
            "id": iid,
            "channel": str(x.get("channel") or "telegram"),
            "at": float(x.get("at") or 0.0),
            "from": sender,
            "why": why,
            "needs": needs,
            "kind": str(x.get("kind") or "text"),
            "text": str(x.get("text") or "")[:MAX_TEXT],
            "author": str(x.get("author") or "")[:MAX_AUTHOR],
            "platform_msg_id": str(x.get("platform_msg_id") or ""),
            "state": state,
            "notified_at": float(x.get("notified_at") or 0.0),
            "notified_n": int(x.get("notified_n") or 0),
            "answered_at": float(x.get("answered_at") or 0.0),
        })
    items.sort(key=lambda i: i["id"])
    seq = int(data.get("seq") or 0)
    if items:
        seq = max(seq, items[-1]["id"])
    return {"version": VERSION, "seq": seq, "items": items}


def _read():
    p = path()
    if not os.path.exists(p):
        return _blank()
    try:
        with open(p, encoding="utf-8") as f:
            return _norm(json.load(f))
    except Exception:
        _corrupt(p)
        return _blank()


def _write(data):
    d = _state_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".inbox.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path())


@contextmanager
def _guard():
    """进程内线程锁 + （有 fcntl 时）跨进程文件锁：桥和宿主可能同时进来。"""
    with _LOCK:
        if fcntl is None:
            yield
            return
        d = _state_dir()
        os.makedirs(d, exist_ok=True)
        lock_path = os.path.join(d, ".inbox.lock")
        with open(lock_path, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _prune(items):
    """只留最近 KEEP 条；先丢已经了结的/上下文，再丢没了的（不让未回的被上下文挤掉）。"""
    if len(items) <= KEEP:
        return items
    done = [i for i in items if i["state"] in ("answered", "closed", "context")]
    live = [i for i in items if i["state"] in ("unread", "notified")]
    live = live[-(KEEP // 2):]                      # 未回的留一半配额，够用
    room = KEEP - len(live)
    return sorted((done[-room:] if room > 0 else []) + live, key=lambda i: i["id"])


def load():
    """只读快照（自检/调试用）。"""
    with _guard():
        return _read()


def pending(channel=None):
    """真正要她处理的：他发来的，或群里有人 @她的（state=unread/notified 且 needs）。

    群里别人的话是上下文（state=context），永远不在这里——否则会让她把「主动发言」误判成回复，
    也会把叫醒循环点着（2026-11-02 复审）。
    """
    with _guard():
        items = _read()["items"]
    out = [i for i in items if i["state"] in ("unread", "notified") and i.get("needs", True)]
    if channel:
        out = [i for i in out if i["channel"] == channel]
    return out


def context_items(channel=None, since_s=600, limit=10, now=None):
    """群里别人的话（上下文）：最近 since_s 秒内、最多 limit 条，按时间顺序。只读，不改状态。"""
    now = time.time() if now is None else now
    with _guard():
        items = _read()["items"]
    out = [i for i in items if i["state"] == "context"
           and (channel is None or i["channel"] == channel)
           and now - float(i["at"] or 0) <= since_s]
    return out[-limit:] if limit else []


def unread_count():
    return len(pending())


def add(channel, text, *, from_="owner", kind="text", at=None, platform_msg_id="", author="", why=""):
    """收一条消息。返回条目；同一条平台消息重复投递（重启后重放）返回 None。

    needs = 他发的（from=owner）或群里 @她（why=mention）→ unread，进待回；
    其余（群里别人/别的 bot）→ context，只当上下文，不驱动叫醒。
    悄悄话里可畏的话（why=whisper）也进待回：那是两个人说话，她得看得到、回得了。
    """
    if channel not in CHANNELS:
        raise ValueError("unknown channel: %r" % (channel,))
    body = str(text or "").strip()[:MAX_TEXT]
    if not body and kind == "text":
        return None
    pid = str(platform_msg_id or "")
    sender = str(from_ or "owner")
    why = str(why or "")
    needs = sender == "owner" or why in ("mention", "whisper")
    with _guard():
        data = _read()
        if pid and any(i["channel"] == channel and i["platform_msg_id"] == pid for i in data["items"]):
            return None
        data["seq"] = int(data.get("seq") or 0) + 1
        item = {
            "id": data["seq"],
            "channel": channel,
            "at": float(time.time() if at is None else at),
            "from": sender,
            "why": why,
            "needs": needs,
            "kind": str(kind or "text"),
            "text": body,
            "author": str(author or "")[:MAX_AUTHOR],
            "platform_msg_id": pid,
            "state": "unread" if needs else "context",
            "notified_at": 0.0,
            "notified_n": 0,
            "answered_at": 0.0,
        }
        data["items"] = _prune(data["items"] + [item])
        _write(data)
        return item


def mark(ids, state, now=None):
    """把若干条改成某个状态；返回真正改到的条数。"""
    if state not in STATES:
        raise ValueError("unknown state: %r" % (state,))
    wanted = {int(i) for i in ids or []}
    if not wanted:
        return 0
    ts = float(time.time() if now is None else now)
    with _guard():
        data = _read()
        n = 0
        for item in data["items"]:
            if item["id"] not in wanted:
                continue
            item["state"] = state
            if state == "notified":
                item["notified_at"] = ts
                item["notified_n"] = int(item["notified_n"]) + 1
            elif state == "answered":
                item["answered_at"] = ts
            n += 1
        if n:
            _write(data)
        return n


def mark_answered(ids, now=None):
    return mark(ids, "answered", now)


def mark_notified(ids, now=None):
    return mark(ids, "notified", now)


def close(ids, now=None):
    """醒来轮末尾了结：回过的（answered）不动，其余的算她决定不回。"""
    wanted = {int(i) for i in ids or []}
    if not wanted:
        return 0
    ts = float(time.time() if now is None else now)
    with _guard():
        data = _read()
        n = 0
        for item in data["items"]:
            if item["id"] in wanted and item["state"] in ("unread", "notified"):
                item["state"] = "closed"
                item["answered_at"] = ts
                n += 1
        if n:
            _write(data)
        return n


def _selftest():
    import shutil
    import tempfile
    old = os.environ.get("ALBION_STATE_DIR")
    os.environ["ALBION_STATE_DIR"] = tempfile.mkdtemp(prefix="inbox-test-")
    try:
        assert pending() == [] and unread_count() == 0
        a = add("telegram", "在忙吗", platform_msg_id="1")
        assert a["id"] == 1 and a["state"] == "unread"
        assert add("telegram", "重复投递", platform_msg_id="1") is None, "同一条平台消息不该进两次"
        b = add("discord_group", "机器人甲：晚上好", from_="bot", platform_msg_id="2")
        assert b["state"] == "context" and b["needs"] is False, "别的 bot 的话只是上下文，不进待回"
        assert [i["id"] for i in pending()] == [1], "上下文条目不算待回"
        m = add("discord_group", "阿尔比恩在吗", from_="member", why="mention", platform_msg_id="3")
        assert m["state"] == "unread" and m["needs"] is True, "群里 @她 的进待回"
        assert [i["id"] for i in pending("discord_group")] == [m["id"]]
        w = add("discord_whisper", "睡了吗", from_="bot", why="whisper", platform_msg_id="w-1")
        assert w["state"] == "unread" and w["needs"] is True, "悄悄话里可畏的话进待回"
        assert [i["id"] for i in pending("discord_whisper")] == [w["id"]]
        assert channel_label("discord_whisper") == "Discord 悄悄话"
        assert [i["id"] for i in context_items("discord_group")] == [b["id"]], "上下文能单独取出来给她看"
        assert context_items("discord_group", since_s=0) == [], "上下文只取最近的"
        assert [i["id"] for i in pending()] == [1, m["id"], w["id"]], "待回＝他发的 + 群里 @她的 + 悄悄话"
        assert [i["id"] for i in pending("telegram")] == [1] and pending("discord") == []
        mark_notified([1])
        assert pending()[0]["state"] == "notified" and load()["items"][0]["notified_n"] == 1
        mark_answered([1, m["id"], w["id"]])
        assert pending() == [] and load()["items"][0]["answered_at"] > 0
        c = add("telegram", "第二条")
        close([c["id"]])
        assert load()["items"][-1]["state"] == "closed" and pending() == []
        # 坏文件留底、不覆盖
        with open(path(), "w", encoding="utf-8") as f:
            f.write("{坏")
        assert pending() == []
        assert any(n.startswith("inbox.json.corrupt-") for n in os.listdir(_state_dir()))
        # 只留最近 KEEP 条，未回的优先保住
        for k in range(KEEP + 20):
            add("telegram", "x%d" % k)
        assert len(load()["items"]) <= KEEP
        assert pending(), "未回的不能被挤光"
        # seq 只增不减
        before = load()["seq"]
        assert add("telegram", "最后一条")["id"] == before + 1
        # 状态名拼错要炸，不要静默
        try:
            mark([1], "done")
        except ValueError:
            pass
        else:
            raise AssertionError("非法状态应当报错")
        print("INBOX SELFTEST OK")
    finally:
        shutil.rmtree(os.environ["ALBION_STATE_DIR"], ignore_errors=True)
        if old is None:
            os.environ.pop("ALBION_STATE_DIR", None)
        else:
            os.environ["ALBION_STATE_DIR"] = old


if __name__ == "__main__":
    _selftest()
