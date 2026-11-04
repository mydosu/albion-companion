#!/usr/bin/env python3
"""可畏侧的「每轮约定」插件（2026-11-03，Hermes plugin，不是 shell hook）。

会话一长，模型就容易不守人设里的说话约定；把约定写进 system prompt 会在每次压缩时被
重建/遗忘，也会破前缀缓存。这里改用 Hermes 官方的 `pre_llm_call` 钩子，每轮把一小段
提醒拼在**最新一条 user 消息末尾**（`agent/turn_context.py:793 _collect_pre_llm_call_context`
→ `:1284-1299 compose_user_api_content`，只改本轮 API 视图，历史前缀字节不变）。

只管 Telegram / Discord：
  · platform == "telegram" 或 "discord" 才返回 {"context": REMINDER}；
  · cron 轮（platform == "cron"）、weixin（可畏不再在微信）、desktop/acp/subagent、
    以及 assistant / tongxi profile 的会话一律不返回；
  · 识别不到平台就什么都不做，fail-open（绝不因为这段把入站打挂）。

为什么不用 shell hook：`pre_llm_call` 的返回值只有插件通道能拿到 context
（`hermes_cli/lifecycle.py:26 invoke_hook` → `agent/turn_context.py:793`）。

自检（不联网、不写库）：
    python tools/hermes/turn-reminder/__init__.py --selftest
"""
from __future__ import annotations

# 每轮提醒原文（主脑定稿，一字不改）
REMINDER = (
    "（每轮提醒，只给我自己看：说话用现在的人平常说的大白话，不用「便是」「罢了」「歇息」这类古旧字眼；"
    "不用 emoji，也不用颜文字；闲聊像发消息那样连着写，想分开发就在两条之间单独写一行 [分条]；"
    "没什么想说的时候整条只写 [SILENT]，指挥官问我事情、交代我办事时一定回答。）"
)

# 只有这两个平台是「可畏」在跟指挥官说话的地方
ALLOWED_PLATFORMS = ("telegram", "discord")

# 别的 profile 的会话 id 里带这些标记（gateway 多 profile 的 session key 形如
# `agent:main:telegram:dm:<id>` / `agent:assistant:weixin:...`）；防御性排除，正常也碰不到。
_FOREIGN_PROFILE_MARKERS = (":assistant:", "assistant:weixin", ":tongxi:", "tongxi:")


def _platform_name(platform) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def _is_foreign_profile(session_id) -> bool:
    session = str(session_id or "").lower()
    return any(marker in session for marker in _FOREIGN_PROFILE_MARKERS)


def _pre_llm_call(session_id="", user_message="", platform="", **kwargs):
    """每轮把约定的原文附到最新 user 消息末尾；非 TG/DC 轮次返回 None（不注入）。"""
    try:
        if _platform_name(platform) not in ALLOWED_PLATFORMS:
            return None
        if _is_foreign_profile(session_id):
            return None
        return {"context": REMINDER}
    except Exception:  # noqa: BLE001 - 任何异常都放行，绝不能挡住入站
        return None


def register(ctx):
    ctx.register_hook("pre_llm_call", _pre_llm_call)


def _selftest():
    tg = _pre_llm_call(session_id="20260910_050802_1982faa5", platform="telegram")
    dc = _pre_llm_call(session_id="20261102_072021_921d33d4", platform="discord")
    assert tg == {"context": REMINDER}, tg
    assert dc == {"context": REMINDER}, dc
    assert tg["context"] == dc["context"]
    # 一字不改：关键片段逐条在
    for piece in ("不用「便是」「罢了」「歇息」这类古旧字眼", "不用 emoji，也不用颜文字",
                  "[分条]", "[SILENT]", "指挥官问我事情、交代我办事时一定回答"):
        assert piece in REMINDER, piece

    # cron / weixin / 别的平台：一律不注入
    assert _pre_llm_call(session_id="cron_d5201d4f2deb_20261104_1", platform="cron") is None
    assert _pre_llm_call(session_id="cron_abc_20261103_110000", platform="cron",
                         user_message="现在可以去「悄悄话」找阿尔比恩说说话") is None
    assert _pre_llm_call(session_id="20261018_072815_12a83b3e", platform="weixin") is None
    assert _pre_llm_call(session_id="20260906_004558_d1a062", platform="desktop") is None
    assert _pre_llm_call(session_id="x", platform="acp") is None
    assert _pre_llm_call(session_id="x", platform="subagent") is None
    assert _pre_llm_call(session_id="x", platform="") is None
    assert _pre_llm_call(session_id="x") is None

    # 别的 profile：就算平台对也不注入
    assert _pre_llm_call(session_id="agent:assistant:weixin:o9cq801", platform="telegram") is None
    assert _pre_llm_call(session_id="agent:tongxi:telegram:dm:1", platform="discord") is None

    # 平台值可能是枚举
    class _P:
        value = "telegram"

    assert _pre_llm_call(session_id="x", platform=_P()) == {"context": REMINDER}
    print("turn-reminder plugin selftest ok")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    print("this is a Hermes plugin; copy the directory under %LOCALAPPDATA%\\hermes\\plugins\\ "
          "and enable it in config.yaml (see tools/hermes/README.md)")
    raise SystemExit(0)
