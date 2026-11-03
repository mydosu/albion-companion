#!/usr/bin/env python3
"""可畏侧的额度插件（2026-11-02，Hermes plugin，不是 shell hook）。

管两个 Discord 频道，共用一套「查 bridge → 提醒 → 额度 0 整轮 [SILENT]」：
  · #悄悄话（`GET /whisper_budget`）：Discord 轮 + 「去悄悄话」的 cron 轮；
  · #常规（`GET /group_budget`）：Discord 群轮。
      - pre_llm_call：把 bridge 回话里的 note 原文注入（悄悄话剩 4 条收尾 / 剩 0 聊到头；
        群里 R≤3 收尾 / R=0 等指挥官开口），并把这个 session 的 remaining 记在进程内；
      - transform_llm_output：同一个 session 在 pre 时 remaining=0 → 整轮 `[SILENT]`
        （Discord 轮由 gateway 的 [SILENT] 过滤拦下，cron 轮由 cron scheduler 拦下，都不会投递）。

为什么不用 shell hook：`agent/shell_hooks.py` 的响应解析只认 block/modify/context，
`transform_llm_output` 的改写拿不到通道；插件是 Hermes 官方支持的、per-turn 真正能替换正文的钩子。
原来的 `scripts/discord_group_budget.py`（群）和 `scripts/discord_whisper_budget.py`（悄悄话）
只是 shell hook，remaining=0 的硬拦从来没生效过；插件启用后要把它们从 config.yaml 的 hooks 段撤掉。

识别方式：
  · Discord 轮：session_id 是 gateway 的 `...:discord:<type>:<chat_id>[:thread][:user]`，
    里面带频道 id（悄悄话 id 取 HERMES_WHISPER_CHANNEL_ID / ALBION_DISCORD_WHISPER_CHANNEL_ID，
    群 id 取 HERMES_GROUP_CHANNEL_ID / ALBION_DISCORD_CHANNEL_ID，最后回落到本机 #常规 的 id）；
  · cron 轮：platform == "cron"，且这一轮的 user_message 里带着 cron 任务提示词的开头
    「现在可以去「悄悄话」找阿尔比恩说说话」（不需要频道 id）。
识别不到就什么都不做；bridge 连不上也放行，fail-open。

频道 id / key 优先读环境变量，其次读 Hermes 根目录的 `.env`。不打印任何 token。
自检（不联网）：python tools/hermes/whisper-budget/__init__.py --selftest
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

BRIDGE_BASE = "http://127.0.0.1:8660"
BUDGET_PATHS = {"whisper": "/whisper_budget", "group": "/group_budget"}
TIMEOUT = 3.0
# cron 任务的提示词（一字不改）开头；用它认出「这一轮 cron 是去悄悄话找阿尔比恩」
CRON_MARKER = "现在可以去「悄悄话」找阿尔比恩说说话"
# 本机 #常规 的频道 id（生产 shell hook 里写死的那一个）；环境变量/.env 优先
DEFAULT_GROUP_CHANNEL_ID = "1566804961218666671"
MAX_TRACKED_SESSIONS = 200

_LOCK = threading.Lock()
_REMAINING: dict[str, int] = {}


def _hermes_home_candidates():
    seen = set()
    candidates = [Path(__file__).resolve().parents[2]]
    if (os.environ.get("HERMES_HOME") or "").strip():
        candidates.append(Path(os.environ["HERMES_HOME"].strip()))
    candidates += [Path.home() / "AppData" / "Local" / "hermes", Path.home() / ".hermes"]
    for base in candidates:
        key = str(base)
        if key in seen:
            continue
        seen.add(key)
        yield base


def _dotenv_value(name):
    for base in _hermes_home_candidates():
        path = base / ".env"
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            continue
    return ""


def _setting(name):
    value = (os.environ.get(name) or "").strip()
    return value or _dotenv_value(name)


def _whisper_channel_id():
    return _setting("HERMES_WHISPER_CHANNEL_ID") or _setting("ALBION_DISCORD_WHISPER_CHANNEL_ID")


def _group_channel_id():
    return (_setting("HERMES_GROUP_CHANNEL_ID") or _setting("ALBION_DISCORD_CHANNEL_ID")
            or DEFAULT_GROUP_CHANNEL_ID)


def _chat_key():
    return _setting("ALBION_CHAT_KEY")


def _is_cron_whisper(platform, user_message):
    """这一轮是不是「去悄悄话」的 cron 任务（cron 的 pre_llm_call 拿不到频道 id）。"""
    return str(platform or "").strip().lower() == "cron" and CRON_MARKER in str(user_message or "")


def _classify(platform, session_id, user_message):
    """这一轮属于哪个额度域：""（不管）/ "whisper" / "group"。"""
    if _is_cron_whisper(platform, user_message):
        return "whisper"
    session = str(session_id or "")
    whisper_id = _whisper_channel_id()
    if whisper_id and whisper_id in session:
        return "whisper"
    group_id = _group_channel_id()
    if group_id and group_id in session:
        return "group"
    return ""


def _fetch_budget(kind):
    headers = {}
    key = _chat_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(BRIDGE_BASE + BUDGET_PATHS[kind], headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _remember(session_id, remaining):
    with _LOCK:
        _REMAINING[str(session_id or "")] = int(remaining)
        while len(_REMAINING) > MAX_TRACKED_SESSIONS:
            _REMAINING.pop(next(iter(_REMAINING)), None)


def _take(session_id):
    with _LOCK:
        return _REMAINING.pop(str(session_id or ""), None)


def _pre_llm_call(session_id="", user_message="", platform="", **kwargs):
    """额度提醒：只在 #悄悄话 / #常规 的轮次里注入 bridge 的 note 原文。"""
    try:
        kind = _classify(platform, session_id, user_message)
        if not kind:
            return None
        data = _fetch_budget(kind)
        remaining = int(data.get("remaining") or 0)
        _remember(session_id, remaining)
        note = str(data.get("note") or "")
        return {"context": note} if note else None
    except Exception:                            # noqa: BLE001 - 桥不在/超时：放行
        return None


def _transform_llm_output(response_text="", session_id="", platform="", **kwargs):
    """remaining=0 的悄悄话 / 群轮：整轮 [SILENT]（不投递）。别的轮原样放行。"""
    try:
        remaining = _take(session_id)
        if remaining is None or remaining > 0:
            return None
        return "[SILENT]"
    except Exception:                            # noqa: BLE001
        return None


def register(ctx):
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)


def _selftest():
    global _fetch_budget
    old_env = {k: os.environ.get(k) for k in
               ("HERMES_WHISPER_CHANNEL_ID", "ALBION_DISCORD_WHISPER_CHANNEL_ID",
                "HERMES_GROUP_CHANNEL_ID", "ALBION_DISCORD_CHANNEL_ID", "ALBION_CHAT_KEY")}
    os.environ["HERMES_WHISPER_CHANNEL_ID"] = "777000111"
    os.environ["ALBION_CHAT_KEY"] = "k"
    os.environ.pop("HERMES_GROUP_CHANNEL_ID", None)
    os.environ.pop("ALBION_DISCORD_CHANNEL_ID", None)
    orig_fetch = _fetch_budget

    whisper_sid = "default:discord:group:777000111:123"
    group_sid = "default:discord:group:" + DEFAULT_GROUP_CHANNEL_ID + ":123"
    cron_sid = "cron_abc123_20261103_110000"
    cron_turn = {"session_id": cron_sid, "platform": "cron",
                 "user_message": "[IMPORTANT: cron hint]\n\n" + CRON_MARKER + "。想跟她说什么就直接说……"}

    def set_budget(remaining, note):
        global _fetch_budget
        _fetch_budget = lambda kind: {"remaining": remaining, "note": note}

    try:
        # ---- 悄悄话 ----
        set_budget(0, "到头了")
        assert _pre_llm_call(session_id=whisper_sid, user_message="在吗", platform="discord") == \
            {"context": "到头了"}
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") == "[SILENT]"
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None, "只拦本轮一次"

        set_budget(4, "收尾")
        assert _pre_llm_call(session_id=whisper_sid, user_message="在吗", platform="discord") == \
            {"context": "收尾"}
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None, "剩 4 不硬拦"

        set_budget(8, "")
        assert _pre_llm_call(session_id=whisper_sid, user_message="在吗", platform="discord") is None
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None

        # ---- cron（只有悄悄话 cron 受管）----
        set_budget(0, "到头了")
        assert _pre_llm_call(**cron_turn) == {"context": "到头了"}
        assert _transform_llm_output(session_id=cron_sid, platform="cron") == "[SILENT]"
        assert _pre_llm_call(session_id="cron_other_20261103_090000", platform="cron",
                             user_message="检查服务器状态") is None
        assert _transform_llm_output(session_id="cron_other_20261103_090000", platform="cron") is None

        # ---- #常规 群：R=0 硬拦，R≤3 只提醒，R>3 不插 ----
        set_budget(0, "这一轮群里说得够多了，等指挥官再开口吧。")
        assert _pre_llm_call(session_id=group_sid, user_message="你们聊什么", platform="discord") == \
            {"context": "这一轮群里说得够多了，等指挥官再开口吧。"}
        assert _transform_llm_output(session_id=group_sid, platform="discord") == "[SILENT]"

        set_budget(3, "群里你们俩这一轮已经说了 9 条，还能再说 3 条，差不多该收尾了。")
        assert _pre_llm_call(session_id=group_sid, user_message="在吗", platform="discord") == \
            {"context": "群里你们俩这一轮已经说了 9 条，还能再说 3 条，差不多该收尾了。"}
        assert _transform_llm_output(session_id=group_sid, platform="discord") is None, "R=3 不硬拦"

        set_budget(8, "")
        assert _pre_llm_call(session_id=group_sid, user_message="在吗", platform="discord") is None
        assert _transform_llm_output(session_id=group_sid, platform="discord") is None

        # 群的端点/悄悄话端点各查各的
        seen = []

        def record(kind):
            seen.append(kind)
            return {"remaining": 8, "note": ""}

        _fetch_budget = record
        _pre_llm_call(session_id=group_sid, platform="discord")
        _pre_llm_call(session_id=whisper_sid, platform="discord")
        assert seen == ["group", "whisper"], seen

        # 别的频道 / 私信：不碰
        assert _pre_llm_call(session_id="default:discord:group:999999:123", platform="discord",
                             user_message="在吗") is None
        assert _transform_llm_output(session_id="default:discord:group:999999:123",
                                     platform="discord") is None

        # 桥挂了：fail-open
        def _boom(kind):
            raise RuntimeError("bridge down")
        _fetch_budget = _boom
        assert _pre_llm_call(session_id=whisper_sid, platform="discord") is None
        assert _pre_llm_call(session_id=group_sid, platform="discord") is None
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None
    finally:
        _fetch_budget = orig_fetch
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("whisper-budget plugin selftest ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    print("this is a Hermes plugin; copy the directory under %LOCALAPPDATA%\\hermes\\plugins\\ "
          "and enable it in config.yaml (see tools/hermes/README.md)")
    raise SystemExit(0)
