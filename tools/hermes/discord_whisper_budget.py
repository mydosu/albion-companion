#!/usr/bin/env python3
"""Discord #悄悄话 的条数额度 hook（2026-11-02，可畏 / Hermes 侧）。

#悄悄话 是阿尔比恩和可畏两个人说话的频道（指挥官看得到）。条数限制由阿尔比恩 bridge 的
`bin/whisper_budget.py` 算，这里只做两件事：
  · pre_llm_call：问 `GET /whisper_budget`，有 note 就注入给可畏（剩 4 条收尾 / 剩 0 聊到头）；
  · transform_llm_output：remaining=0 时把这一轮改成 [SILENT]（硬拦）。
别的频道一个字都不插；bridge 连不上（阿尔比恩不在、服务没起）就放行，fail-open。

和 `discord_group_budget.py` 是同一套写法、分开一个文件：两个 hook 都挂在同样的事件上，
各自只认自己的频道，互不影响。频道 id 优先取环境变量（HERMES_WHISPER_CHANNEL_ID 或
ALBION_DISCORD_WHISPER_CHANNEL_ID，和阿尔比恩那边填同一个）；没配就按负载里有没有「悄悄话」认。

stdin: Hermes 的 hook JSON；stdout: 一行 JSON（空 {} 表示不改）。不打印任何 token。

自检（不联网）：python discord_whisper_budget.py --selftest
"""
import json
import os
import sys
import urllib.request

BRIDGE = "http://127.0.0.1:8660/whisper_budget"
TIMEOUT = 3.0
CHANNEL_NAME = "悄悄话"


def _channel_id():
    return (os.environ.get("HERMES_WHISPER_CHANNEL_ID")
            or os.environ.get("ALBION_DISCORD_WHISPER_CHANNEL_ID")
            or "").strip()


def _key():
    """和 group hook 一样：环境变量优先，其次 Hermes 根目录的 .env。"""
    key = (os.environ.get("ALBION_CHAT_KEY") or "").strip()
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ALBION_CHAT_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _in_whisper(payload):
    """这条 hook 负载是不是来自 #悄悄话：配了 id 认 id，没配就认频道名。"""
    blob = json.dumps(payload, ensure_ascii=False)
    channel_id = _channel_id()
    if channel_id:
        return channel_id in blob
    return CHANNEL_NAME in blob


def _budget():
    headers = {}
    key = _key()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(BRIDGE, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def decide(payload, budget):
    """根据 hook 负载和 /whisper_budget 的回话决定输出；{} = 什么都不改。"""
    if not _in_whisper(payload):
        return {}
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    remaining = int((budget or {}).get("remaining") or 0)
    note = str((budget or {}).get("note") or "")
    if remaining <= 0:
        if event.endswith("transform_llm_output"):
            return {"decision": "modify", "output": "[SILENT]"}
        return {"context": note} if note else {}
    if note and event.endswith("pre_llm_call"):
        return {"context": note}
    return {}


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        print("{}")
        return 0
    if not isinstance(payload, dict) or not _in_whisper(payload):
        print("{}")
        return 0
    try:
        budget = _budget()
    except Exception:                               # noqa: BLE001 - 连不上就放行
        print("{}")
        return 0
    print(json.dumps(decide(payload, budget), ensure_ascii=False))
    return 0


def _selftest():
    old = os.environ.get("HERMES_WHISPER_CHANNEL_ID")
    os.environ["HERMES_WHISPER_CHANNEL_ID"] = "777000111"
    try:
        pre = {"hook_event_name": "pre_llm_call", "channel_id": "777000111", "content": "在吗"}
        out = {"hook_event_name": "transform_llm_output", "channel_id": "777000111"}
        assert decide(pre, {"remaining": 4, "note": "收尾"}) == {"context": "收尾"}
        assert decide(out, {"remaining": 0, "note": "到头了"}) == {"decision": "modify", "output": "[SILENT]"}
        assert decide(out, {"remaining": 4, "note": "收尾"}) == {}, "不该改正文"
        assert decide(pre, {"remaining": 0, "note": "到头了"}) == {"context": "到头了"}
        assert decide(pre, {"remaining": 8, "note": ""}) == {}
        assert decide({"channel_id": "999"}, {"remaining": 0, "note": "x"}) == {}, "别的频道不插"
        del os.environ["HERMES_WHISPER_CHANNEL_ID"]
        assert decide({"channel_name": CHANNEL_NAME}, {"remaining": 0, "note": "x"}) == {"context": "x"}, \
            "没配 id 时按频道名认"
    finally:
        if old is None:
            os.environ.pop("HERMES_WHISPER_CHANNEL_ID", None)
        else:
            os.environ["HERMES_WHISPER_CHANNEL_ID"] = old
    print("discord_whisper_budget selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main())
