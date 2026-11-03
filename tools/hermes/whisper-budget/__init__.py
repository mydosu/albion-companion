#!/usr/bin/env python3
"""可畏侧的「悄悄话」额度插件（2026-11-02，Hermes plugin，不是 shell hook）。

#悄悄话 是阿尔比恩和可畏两个人说话的频道（指挥官看得见，但不告诉她们）。条数由阿尔比恩 bridge 的
`bin/whisper_budget.py` 算，这个插件只做两件事：
  · pre_llm_call：问 `GET /whisper_budget`，有 note 就注入（剩 4 条收尾 / 剩 0 聊到头），
    并把这个 session 的 remaining 记在进程内；
  · transform_llm_output：同一个 session 在 pre 时 remaining=0 → 整轮改成 `[SILENT]`
    （Discord 轮由 gateway 的 [SILENT] 过滤拦下，cron 轮由 cron scheduler 拦下，都不会投递）。

为什么不用 shell hook：`agent/shell_hooks.py` 的响应解析只认 block/modify/context，
`transform_llm_output` 的改写拿不到通道；插件是 Hermes 官方支持的、per-turn 真正能替换正文的钩子。

识别方式：
  · Discord 轮：session_id 是 gateway 的 `...:discord:<type>:<chat_id>[:thread][:user]`，
    里面含 `HERMES_WHISPER_CHANNEL_ID` / `ALBION_DISCORD_WHISPER_CHANNEL_ID`；
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

BRIDGE = "http://127.0.0.1:8660/whisper_budget"
TIMEOUT = 3.0
# cron 任务的提示词（一字不改）开头；用它认出「这一轮 cron 是去悄悄话找阿尔比恩」
CRON_MARKER = "现在可以去「悄悄话」找阿尔比恩说说话"
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


def _channel_id():
    return _setting("HERMES_WHISPER_CHANNEL_ID") or _setting("ALBION_DISCORD_WHISPER_CHANNEL_ID")


def _chat_key():
    return _setting("ALBION_CHAT_KEY")


def _is_cron_whisper(platform, user_message):
    """这一轮是不是「去悄悄话」的 cron 任务（cron 的 pre_llm_call 拿不到频道 id）。"""
    return str(platform or "").strip().lower() == "cron" and CRON_MARKER in str(user_message or "")


def _is_whisper_session(session_id):
    """Discord 轮的 session_id 里带着频道 id（gateway 的 session key 含 chat_id）。"""
    channel_id = _channel_id()
    return bool(channel_id) and channel_id in str(session_id or "")


def _fetch_budget():
    headers = {}
    key = _chat_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(BRIDGE, headers=headers)
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
    """额度提醒：只在 #悄悄话 的 Discord 轮或「去悄悄话」的 cron 轮里注入。"""
    try:
        if not (_is_cron_whisper(platform, user_message) or _is_whisper_session(session_id)):
            return None
        data = _fetch_budget()
        remaining = int(data.get("remaining") or 0)
        _remember(session_id, remaining)
        note = str(data.get("note") or "")
        return {"context": note} if note else None
    except Exception:                            # noqa: BLE001 - 桥不在/超时：放行
        return None


def _transform_llm_output(response_text="", session_id="", platform="", **kwargs):
    """remaining=0 的悄悄话轮：整轮 [SILENT]（不投递）。别的轮原样放行。"""
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
    old_id = os.environ.get("HERMES_WHISPER_CHANNEL_ID")
    old_key = os.environ.get("ALBION_CHAT_KEY")
    os.environ["HERMES_WHISPER_CHANNEL_ID"] = "777000111"
    os.environ["ALBION_CHAT_KEY"] = "k"
    orig_fetch = _fetch_budget
    whisper_sid = "default:discord:group:777000111:123"
    cron_sid = "cron_abc123_20261103_110000"
    cron_turn = {"session_id": cron_sid, "platform": "cron",
                 "user_message": "[IMPORTANT: cron hint]\n\n" + CRON_MARKER + "。想跟她说什么就直接说……"}
    try:
        _fetch_budget = lambda: {"remaining": 0, "note": "到头了"}
        assert _pre_llm_call(session_id=whisper_sid, user_message="在吗", platform="discord") == \
            {"context": "到头了"}
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") == "[SILENT]"
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None, "只拦本轮一次"

        _fetch_budget = lambda: {"remaining": 4, "note": "收尾"}
        assert _pre_llm_call(session_id=whisper_sid, user_message="在吗", platform="discord") == \
            {"context": "收尾"}
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None, "剩 4 不硬拦"

        _fetch_budget = lambda: {"remaining": 8, "note": ""}
        assert _pre_llm_call(session_id=whisper_sid, user_message="在吗", platform="discord") is None
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None

        _fetch_budget = lambda: {"remaining": 0, "note": "到头了"}
        assert _pre_llm_call(**cron_turn) == {"context": "到头了"}
        assert _transform_llm_output(session_id=cron_sid, platform="cron") == "[SILENT]"

        # 别的 cron 任务：不碰
        assert _pre_llm_call(session_id="cron_other_20261103_090000", platform="cron",
                             user_message="检查服务器状态") is None
        assert _transform_llm_output(session_id="cron_other_20261103_090000", platform="cron") is None
        # 别的频道：不碰
        assert _pre_llm_call(session_id="default:discord:group:999999:123", platform="discord",
                             user_message="在吗") is None
        # 桥挂了：fail-open
        def _boom():
            raise RuntimeError("bridge down")
        _fetch_budget = _boom
        assert _pre_llm_call(session_id=whisper_sid, platform="discord") is None
        assert _transform_llm_output(session_id=whisper_sid, platform="discord") is None
    finally:
        _fetch_budget = orig_fetch
        if old_id is None:
            os.environ.pop("HERMES_WHISPER_CHANNEL_ID", None)
        else:
            os.environ["HERMES_WHISPER_CHANNEL_ID"] = old_id
        if old_key is None:
            os.environ.pop("ALBION_CHAT_KEY", None)
        else:
            os.environ["ALBION_CHAT_KEY"] = old_key
    print("whisper-budget plugin selftest ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    print("this is a Hermes plugin; copy the directory under %LOCALAPPDATA%\\hermes\\plugins\\ "
          "and enable it in config.yaml (see tools/hermes/README.md)")
    raise SystemExit(0)
