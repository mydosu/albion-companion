#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 本地补丁合集（2026-11-02；2026-11-03 补 D/E 段）。

五件事：
  A. 让 ``[SILENT]`` 在 Telegram / Discord 真正生效（入站 MessageEvent 补 ``reply_expected=False``）。
  B. 让 ``[分条]`` 生效：她单独写一行 ``[分条]`` 就把一条回复断成几条发（base 加拆分 + 两个适配器打开开关）。
  C. 每条消息接成一行：拆完之后，没有代码块、没有两行以上列表的消息，把所有行接成一行
     （上一行末尾有标点就直接接，没有就补「。」；空行去掉；带 ``MEDIA:`` 的整条不动）。
  D. cron 投递（2026-11-03）：cron/scheduler_delivery.py 发 Telegram / Discord 时也走同一套
     ``[分条]`` 拆条 + 接行，条间用同一份 human_delay（700–1800ms）；``[SILENT]`` 判定仍在拆之前。
  E. 跨平台近况（2026-11-03）：TG 和 Discord 各是各的会话（key 里带平台名，不该合并），
     于是「TG 上不记得刚在 Discord 聊的」。入站拼上下文处补一段 neutral note：他本人在别的平台
     最近 6 小时内、上次注入之后的新增消息（≤12 行 / 1500 字，每行带北京时间 + 平台名）。
     悄悄话频道、cron、微信 assistant profile 一律不读。

身份映射（段 E）在 ``%LOCALAPPDATA%\\hermes\\identity_links.json``：文件里写本人各平台的 id、
永不读取的会话和上限。这个脚本不写死任何个人 id —— 文件不存在时按 hermes ``.env`` 里的
``TELEGRAM_ALLOWED_USERS`` / ``DISCORD_ALLOWED_USERS`` 生成一份骨架，读不到就留空（段 E 暂不生效，
把 id 填进 ``platforms`` 即可）；已有文件绝不覆盖。

Hermes 升级会覆盖这些文件 —— **升级后重跑本脚本**（幂等：打过的点跳过；锚点找不到就退出，绝不硬改）。
打补丁前把原文件备份到 ``<仓库>/backup/code/<YYYYMMDD>/hermes-agent/<原相对路径>``（已存在不覆盖）。

用法（在仓库根目录；默认目标是 ``%LOCALAPPDATA%\\hermes\\hermes-agent``）：
  python tools/hermes/hermes-local-patches.py                       # 打补丁（幂等）
  python tools/hermes/hermes-local-patches.py --check                # 0=全部已打，1=有没打的，2=锚点变了
  python tools/hermes/hermes-local-patches.py --selftest             # 跑拆分/接行/跨平台近况的断言（不改文件）
  python tools/hermes/hermes-local-patches.py --hermes-agent <目录> --hermes-home <目录> --backup-root <目录>
                                                                     # 对一份副本演练，不碰生产

打完要重启网关（``hermes gateway stop``，再等计划任务「Hermes Gateway Watchdog」拉起）才生效。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERMES_HOME = (os.environ.get("HERMES_HOME") or "").strip() or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "hermes")
HERMES_AGENT = os.path.join(HERMES_HOME, "hermes-agent")
BACKUP_ROOT = os.path.join(REPO_ROOT, "backup", "code", date.today().strftime("%Y%m%d"), "hermes-agent")

TG = "plugins/platforms/telegram/adapter.py"
DC = "plugins/platforms/discord/adapter.py"
BASE = "gateway/platforms/base.py"
CRON = "cron/scheduler_delivery.py"

SILENT_LINE = "            reply_expected=False,   # 2026-11-02: [SILENT] 在私信和群都生效（hermes-local-patches.py）"
MARKER_LINE = ("    splits_on_break_marker = True  "
               "# 2026-11-02: 她写的单独一行 [分条] 就在这里断成几条发（hermes-local-patches.py）")

SPLITTER_BLOCK = '''    # 2026-11-02: 分不分条、分几条、在哪儿分都由她自己决定 —— 她单独写一行 "[分条]" 就是一处断点。
    # 适配器用 splits_on_break_marker = True 打开（目前只有 Telegram 和 Discord）。
    BREAK_MARKER = "[分条]"
    splits_on_break_marker = False

    @staticmethod
    def split_on_break_marker(content: str) -> list[str]:
        """按单独一行的 ``[分条]`` 拆段；行内出现这几个字不拆。

        每段 ``strip()``、空段丢掉（标记在开头或结尾不会产出空消息）。不设条数上限、
        不对代码块做特殊处理：分点在哪儿写是她自己的事。
        """
        parts: list[str] = []
        buf: list[str] = []
        for line in str(content or "").splitlines():
            if line.strip() == BasePlatformAdapter.BREAK_MARKER:
                parts.append("\\n".join(buf))
                buf = []
                continue
            buf.append(line)
        parts.append("\\n".join(buf))
        return [part.strip() for part in parts if part.strip()]

'''

SEND_HEAD_OLD = '''    @records_delivery
    async def _send_with_retry(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Any = None,
        max_retries: int = 2, base_delay: float = 2.0) -> SendResult:
        """Send with exponential-backoff retry on transient network errors; permanent
        failures fall back to a plain-text send, exhausted retries notify the user."""
        async def _send(text: str) -> SendResult:'''

SPLIT_DISPATCH = '''        if self.splits_on_break_marker:
            parts = self.split_on_break_marker(content)
            if len(parts) > 1:
                last: Optional[SendResult] = None
                for index, part in enumerate(parts):
                    if index:
                        delay = self._get_human_delay()   # 只调发送节奏，不替她分条
                        if delay > 0:
                            await asyncio.sleep(delay)
                    last = await BasePlatformAdapter._send_with_retry(
                        self, chat_id, part, reply_to if index == 0 else None, metadata,
                        max_retries=max_retries, base_delay=base_delay)
                    if not last.success:
                        return last
                return last
'''

FLATTEN_BLOCK = '''    # 2026-11-02: 她那种一句一换行、中间还空行的写法影响观感 —— 每条消息发出去之前接成一行。
    # 结构化内容（代码块、两行以上的列表）原样保留：讲题、列步骤要靠排版。
    _LIST_ITEM_RE = re.compile(r"^\\s*(?:[-*•]|\\d{1,3}[.、)）])\\s*")
    _SENTENCE_END = "。！？!?…~～，,、；;：:」』””)）】》"
    _CODE_FENCE_RE = re.compile(r"^\\s*```", re.MULTILINE)

    @classmethod
    def flatten_message_lines(cls, text: str) -> str:
        """把一条消息里的多行接成一行（去掉换行和空行）；代码块/列表原样返回。

        - 上一行结尾已经是标点（``。！？，、；：」』“”）】`` 这些）就直接接上，否则补一个「。」；
        - ``MEDIA:`` 标签不归这里管：它在更早的 ``_extract_response_content`` 里就已经被摘走了，
          这里再兜一层「带 MEDIA: 就整条不动」，免得将来顺序变了把标签揉进正文。
        """
        raw = str(text or "")
        if not raw.strip():
            return raw
        lines = raw.splitlines()
        if cls._CODE_FENCE_RE.search(raw):
            return raw                                      # 有代码块：排版是内容的一部分
        if any(line.lstrip().startswith("MEDIA:") for line in lines):
            return raw                                      # 兜底：MEDIA 标签绝不在这里被揉进正文
        if sum(1 for line in lines if cls._LIST_ITEM_RE.match(line)) >= 2:
            return raw                                      # 两行以上列表：保留
        out = ""
        for line in (ln.strip() for ln in lines):
            if not line:
                continue
            if not out:
                out = line
            elif out[-1] in cls._SENTENCE_END:
                out += line
            else:
                out += "。" + line
        return out or raw

'''

FLATTEN_DISPATCH = '''        if self.splits_on_break_marker:
            parts = [self.flatten_message_lines(part) for part in self.split_on_break_marker(content)]
            parts = [part for part in parts if part]
            if len(parts) > 1:
                last: Optional[SendResult] = None
                for index, part in enumerate(parts):
                    if index:
                        delay = self._get_human_delay()   # 只调发送节奏，不替她分条
                        if delay > 0:
                            await asyncio.sleep(delay)
                    last = await BasePlatformAdapter._send_with_retry(
                        self, chat_id, part, reply_to if index == 0 else None, metadata,
                        max_retries=max_retries, base_delay=base_delay)
                    if not last.success:
                        return last
                return last
            if parts:
                content = parts[0]
'''

CRON_HELPER_BLOCK = '''# 2026-11-03: cron 投递按 [分条] 拆条（hermes-local-patches.py）。
# 复用网关回复那套 BasePlatformAdapter.split_on_break_marker / flatten_message_lines，
# 只有 Telegram / Discord 拆；[SILENT] 的判定在 _deliver_result 之前，照旧。
def _cron_split_text(platform_name: str, text: str) -> list[str]:
    from gateway.platforms.base import BasePlatformAdapter
    name = str(platform_name or "").lower()
    if name not in ("telegram", "discord"):
        return [text]
    parts = BasePlatformAdapter.split_on_break_marker(text)
    parts = [BasePlatformAdapter.flatten_message_lines(part) for part in parts]
    return [part for part in parts if part] or [text]


def _cron_split_delay_seconds() -> float:
    """条间节奏：和网关回复用同一份 human_delay 配置（默认 700–1800ms）。"""
    import random
    try:
        cfg = _sched.load_config() or {}
        hd = cfg.get("human_delay") or {}
        if str(hd.get("mode") or "").lower() in ("off", "none", "false"):
            return 0.0
        lo = int(hd.get("min_ms", 700))
        hi = int(hd.get("max_ms", 1800))
        if hi < lo:
            lo, hi = hi, lo
        return random.uniform(lo / 1000.0, hi / 1000.0)
    except Exception:
        return 0.0


def _cron_adapter_delay(adapter) -> float:
    try:
        return float(adapter._get_human_delay())
    except Exception:
        return _cron_split_delay_seconds()

'''

CRON_LIVE_ANCHOR = '''        elif text_to_send:
            adapter_ok, timed_out, delivered_message_id = _live_send_text(
                t, text_to_send, route_thread_id, route_metadata,
                target_errors=target_errors, delivery_errors=delivery_errors,
                unverified_targets=unverified_targets,
            )
'''

CRON_LIVE_REPLACEMENT = '''        elif text_to_send:
            parts = _cron_split_text(t.platform_name, text_to_send)
            adapter_ok, timed_out, delivered_message_id = True, False, None
            last_id = None
            for index, part in enumerate(parts):
                if index:
                    delay = _cron_adapter_delay(t.runtime_adapter)
                    if delay > 0:
                        time.sleep(delay)
                adapter_ok, timed_out, delivered_message_id = _live_send_text(
                    t, part, route_thread_id, route_metadata,
                    target_errors=target_errors, delivery_errors=delivery_errors,
                    unverified_targets=unverified_targets,
                )
                if not adapter_ok or timed_out:
                    if index and not adapter_ok:
                        # 前几条已经发出去了：算送达，免得退回 standalone 把整段再发一遍（剩下的记一笔）。
                        _note_target_error(
                            job, f"{len(parts) - index} of {len(parts)} [分条] part(s) not delivered to {t.where}",
                            delivery_errors)
                        adapter_ok, delivered_message_id = True, last_id
                    break
                last_id = delivered_message_id
'''

CRON_STANDALONE_ANCHOR = "    result, err = _standalone_send(t, content, media_files)\n"

CRON_STANDALONE_REPLACEMENT = '''    parts = _cron_split_text(t.platform_name, content)
    result, err = None, None
    for index, part in enumerate(parts):
        if index:
            delay = _cron_split_delay_seconds()
            if delay > 0:
                time.sleep(delay)
        result, err = _standalone_send(t, part, media_files if index == len(parts) - 1 else [])
        if err is not None:
            break
'''

# ---------------------------------------------------------------- E. 跨平台近况注入
# 2026-11-03：TG 和 Discord 是两套会话（session key 里带平台名，不该合并），于是「在 TG 上不记得刚在
# Discord 聊的事」。段 E 在入站拼上下文处补一段 neutral note：他本人在**别的**平台最近说过/被回过的
# 话，最多回看 6 小时 / 12 行 / 1500 字，只取该当前会话上次注入之后的新增。不改 session key。
# 悄悄话频道（他看得见、她们以为他看不到）、cron、微信 assistant profile 一律不读。
RUN_INBOUND = "gateway/run_inbound.py"
IDENTITY_HEAD = "logger = logging.getLogger(\"gateway.run\")"
IDENTITY_PATCH_MARKER = "def build_cross_platform_recency_note("

CROSS_PLATFORM_BLOCK = '''# 2026-11-03: 跨平台近况注入（TG ↔ Discord）—— hermes-local-patches.py 段 E。
# 只在入站消息上拼一段「他最近在别的平台聊过什么」，不改 session key、不合并会话。
# 配置：%LOCALAPPDATA%\\\\hermes\\\\identity_links.json（谁算同一个人、哪些会话永不读、上限）
# 状态：%LOCALAPPDATA%\\\\hermes\\\\cross_platform_inject.json（每个当前会话已注入到的时间戳）
_CROSS_PLATFORM_LABELS = {"telegram": "Telegram", "discord": "Discord"}
import threading  # 2026-11-03: 水位文件读-改-写的进程内锁（hermes-local-patches.py 段 E）
# 水位文件（cross_platform_inject.json）是读-改-写，多个入站协程/线程可能同时进来：加一把进程内锁，
# 写盘仍是「临时文件 + os.replace」原子替换。网关本身是单事件循环，锁顺带兜住 cron / asyncio.to_thread。
_CROSS_PLATFORM_STATE_LOCK = threading.Lock()
# 提示语按「当前会话」分三种（定稿，原文照抄）：TG 附的是 Discord，Discord 私信附的是 TG，
# Discord 群附的是 TG 私聊（群里额外要求别主动提私聊）。
_CROSS_PLATFORM_NOTE_HEADS = {
    "telegram": "[System note: 指挥官最近在 Discord 上和你聊过这些，供你知道，不用逐条回应]",
    "discord_dm": "[System note: 指挥官最近在 Telegram 上和你聊过这些，供你知道，不用逐条回应]",
    "discord_private": (
        "[System note: 指挥官最近私下和你聊过这些，供你知道，不用逐条回应；"
        "私聊的事不要在群里主动提起，除非他自己先提]"
    ),
}
_CROSS_PLATFORM_ROLE_LABELS = {"user": "他", "assistant": "你"}


def _cross_platform_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes")


def _cross_platform_read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _cross_platform_write_json(path: str, payload) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.debug("cross-platform note: state write failed", exc_info=True)


def _cross_platform_note_head(platform: str, chat_type: str) -> str:
    """按「当前会话」挑提示语：TG 私聊附的是 Discord；Discord 私信附的是 TG；Discord 群附的是 TG 私聊。"""
    if str(platform or "") == "telegram":
        return _CROSS_PLATFORM_NOTE_HEADS["telegram"]
    if str(chat_type or "").lower() == "dm":
        return _CROSS_PLATFORM_NOTE_HEADS["discord_dm"]
    return _CROSS_PLATFORM_NOTE_HEADS["discord_private"]


def _cross_platform_bjt(ts: float) -> str:
    """epoch → 北京时间 HH:MM（这台机器本地时区是 -07:00，固定 +8，不跟机器时区走）。"""
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(float(ts), tz=timezone(timedelta(hours=8))).strftime("%H:%M")


def build_cross_platform_recency_note(source, session_key: str, *, home=None, now=None, write_state=True):
    """他本人在别的平台（TG / Discord）最近的新增消息，拼成一段 neutral note。

    返回 ``None`` 表示没有可注入内容：不是他本人、没有新增、或只剩该排除的会话。
    只读 ``state.db``，失败一律静默返回 ``None``（绝不能因为这段把入站打挂）。
    """
    import sqlite3

    home = home or _cross_platform_home()
    cfg = _cross_platform_read_json(os.path.join(home, "identity_links.json"))
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    platform = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", ""))
    user_id = str(getattr(source, "user_id", "") or "")
    platform_ids = cfg.get("platforms") or {}
    all_ids = {str(v) for values in platform_ids.values() for v in (values or [])}
    if not user_id or user_id not in all_ids:
        return None
    limits = cfg.get("limits") or {}
    lookback_hours = float(limits.get("lookback_hours", 6))
    max_lines = int(limits.get("max_lines", 12))
    max_chars = int(limits.get("max_chars", 1500))
    exclude_ids = {str(v) for v in (cfg.get("exclude_chat_ids") or [])}
    exclude_names = [str(v) for v in (cfg.get("exclude_name_contains") or []) if str(v)]
    db_path = os.path.join(home, "state.db")
    if not os.path.isfile(db_path):
        return None
    now = float(now) if now is not None else time.time()
    state_path = os.path.join(home, str(cfg.get("state_file") or "cross_platform_inject.json"))
    state = _cross_platform_read_json(state_path)
    state = state if isinstance(state, dict) else {}
    try:
        since = max(float(state.get(session_key) or 0.0), now - lookback_hours * 3600.0)
    except Exception:
        since = now - lookback_hours * 3600.0

    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\\\", "/"), uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, session_key, chat_id, display_name, profile_name, source FROM sessions "
            "WHERE source IN ('telegram','discord') AND last_activity_at > ? "
            "ORDER BY last_activity_at DESC LIMIT 40",
            (since,),
        )
        current_chat = str(getattr(source, "chat_id", "") or "")
        targets, labels = [], {}
        for sid, key, chat_id, display, profile, src in cur.fetchall():
            key, chat_id = str(key or ""), str(chat_id or "")
            if key == session_key or chat_id == current_chat:
                continue
            if str(src or "") == platform:      # 只跨平台：同一平台的别的会话不搬过来（DM 内容不许漏进群）
                continue
            # 目标会话的 key 以「他自己在该平台的 id」结尾（DM 和群里的 per-user 会话都是）；
            # 跨平台时那个 id 和当前平台的 id 不是同一个，所以要对全部已知 id 逐个比。
            if not any(key.endswith(":" + other_id) for other_id in all_ids):
                continue
            if chat_id in exclude_ids:
                continue
            if any(name in str(display or "") or name in key for name in exclude_names):
                continue
            if profile not in (None, "", "default"):
                continue
            targets.append(sid)
            labels[sid] = _CROSS_PLATFORM_LABELS.get(str(src or ""), str(src or ""))
        if not targets:
            return None
        marks = ",".join("?" for _ in targets)
        cur.execute(
            "SELECT session_id, role, content, timestamp FROM messages "
            "WHERE session_id IN (%s) AND timestamp > ? AND role IN ('user','assistant') "
            "ORDER BY timestamp DESC LIMIT 80" % marks,
            tuple(targets) + (since,),
        )
        rows = cur.fetchall()
    except Exception:
        logger.debug("cross-platform note: state.db query failed", exc_info=True)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    newest = max((float(row[3]) for row in rows if row[3] is not None), default=0.0)
    # 按来源会话分组（组内 rows 仍是倒序 = 最新在前），再按会话均衡分名额：
    # 12 行 ÷ 有新增的会话数，余数给「最新一条更新」的会话；每个会话至少 1 行。
    by_session = {}
    for _sid, role, content, ts in rows:
        text = " ".join(str(content or "").split())
        if not text or text.startswith("[System note") or text.startswith("[SILENT]"):
            continue
        if "MEDIA:" in text:
            text = text.split("MEDIA:")[0].strip()
        text = " ".join(text.replace("[分条]", " ").split())
        if not text:
            continue
        if len(text) > 200:
            text = text[:200] + "…"
        by_session.setdefault(_sid, []).append((float(ts), "%s [%s] %s：%s" % (
            _cross_platform_bjt(ts), labels.get(_sid, ""), _CROSS_PLATFORM_ROLE_LABELS.get(role, role), text)))
    if not by_session:
        return None
    total_sessions = len(by_session)
    quota = max(1, max_lines // total_sessions)
    quotas = {_sid: quota for _sid in by_session}
    leftover = max(max_lines - quota * total_sessions, 0)      # 余数给「最新一条更新」的会话
    for _sid in sorted(by_session, key=lambda s: by_session[s][0][0], reverse=True)[:leftover]:
        quotas[_sid] += 1
    picked = []
    for _sid, items in by_session.items():
        picked.extend((ts, _sid, line) for ts, line in items[:quotas[_sid]])
    if not picked:
        return None
    counts = {}
    for _ts, _sid, _line in picked:
        counts[_sid] = counts.get(_sid, 0) + 1
    while sum(len(line) + 1 for _ts, _sid, line in picked) > max_chars:
        # 超字数：先丢最旧的，但每个来源会话至少留一行
        victim = None
        for item in sorted(picked, key=lambda x: x[0]):
            if counts.get(item[1], 0) > 1:
                victim = item
                break
        if victim is None:
            break
        picked.remove(victim)
        counts[victim[1]] -= 1
    picked.sort(key=lambda x: x[0])
    entries = [line for _ts, _sid, line in picked]
    if not entries:
        return None
    note = _cross_platform_note_head(platform, getattr(source, "chat_type", "")) + "\\n" + "\\n".join(entries)
    if write_state and newest > 0:
        # 读-改-写全程加锁（网关是单事件循环，但 cron / asyncio.to_thread 可能并发进来），
        # 写用临时文件 + os.replace 原子替换，避免半个 JSON 或丢键。
        with _CROSS_PLATFORM_STATE_LOCK:
            state = _cross_platform_read_json(state_path)
            state = state if isinstance(state, dict) else {}
            state[session_key] = newest
            if len(state) > 60:                     # 别让状态文件无限长：留最近的 40 条
                for key in sorted(state, key=lambda k: float(state[k] or 0))[:len(state) - 40]:
                    state.pop(key, None)
            _cross_platform_write_json(state_path, state)
    return note

'''

CROSS_PLATFORM_CALL_ANCHOR = '''        # After the sender-prefix so the prefix applies only to the trigger message, not the backfill.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\\n\\n[New message]\\n{message_text}"
        return message_text
'''

CROSS_PLATFORM_CALL_REPLACEMENT = '''        # After the sender-prefix so the prefix applies only to the trigger message, not the backfill.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\\n\\n[New message]\\n{message_text}"
        # 2026-11-03: 跨平台近况（TG ↔ Discord）—— hermes-local-patches.py 段 E。
        # 只在他本人、且是 TG/Discord 入站时生效；任何异常都静默跳过，不影响这条消息。
        _cp_note = None
        try:
            from gateway.session import build_session_key as _cp_build_key
            _cp_session_key = _cp_build_key(
                source,
                group_sessions_per_user=getattr(self.config, "group_sessions_per_user", True),
                thread_sessions_per_user=getattr(self.config, "thread_sessions_per_user", False),
            )
            _cp_note = build_cross_platform_recency_note(source, _cp_session_key)
        except Exception:
            _cp_note = None
        if _cp_note:
            message_text = f"{_cp_note}\\n\\n{message_text}"
        return message_text
'''

# identity_links.json：谁算同一个人、哪些会话永不读、上限。
# 个人 id 不进这个脚本：文件不存在时按 hermes .env 里的 allowed/owner 列表生成骨架，读不到就留空。
OWNER_ENV_KEYS = {
    "telegram": ("TELEGRAM_ALLOWED_USERS", "TELEGRAM_OWNER_ID", "TELEGRAM_CHAT_ID"),
    "discord": ("DISCORD_ALLOWED_USERS", "DISCORD_OWNER_ID"),
}
WHISPER_ENV_KEYS = ("HERMES_WHISPER_CHANNEL_ID", "ALBION_DISCORD_WHISPER_CHANNEL_ID")


def _hermes_env_values() -> dict:
    """读 ``<hermes home>/.env``：只解析 KEY=VALUE，返回字典（不打印任何值）。"""
    env_path = os.path.join(HERMES_HOME, ".env")
    values = {}
    if not os.path.isfile(env_path):
        return values
    try:
        with open(env_path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception:
        return {}
    return values


def _first_numeric_id(raw) -> str:
    """从 ``a,b,c`` 这样的列表里取第一个像平台 id 的数字（纯数字、至少 5 位）。"""
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and len(part) >= 5:
            return part
    return ""


def _owner_ids_from_env(env) -> dict:
    """本人各平台 id：按 OWNER_ENV_KEYS 的顺序取第一个非空数字 id。"""
    out = {}
    for platform, keys in OWNER_ENV_KEYS.items():
        ids = []
        for key in keys:
            found = _first_numeric_id(env.get(key) or "")
            if found:
                ids = [found]
                break
        out[platform] = ids
    return out


def _env_whisper_channel(env=None) -> str:
    """悄悄话频道 id：只从 hermes 的 .env 读，读不到就空（靠 display_name 里的「悄悄话」兜底）。"""
    env = _hermes_env_values() if env is None else env
    for key in WHISPER_ENV_KEYS:
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return ""


def ensure_identity_links(*, check_only: bool) -> str:
    """identity_links.json 不存在就建一份（id 从 .env 取）。已存在不覆盖。"""
    path = os.path.join(HERMES_HOME, "identity_links.json")
    if os.path.isfile(path):
        return "already"
    if check_only:
        return "missing"
    env = _hermes_env_values()
    platforms = _owner_ids_from_env(env)
    payload = {
        "_note": ("跨平台近况注入（hermes-local-patches.py 段 E）：同一人的各平台 id、永不读取的"
                  "会话、上限。platforms 为空表示不从那个平台取；补上 id 后重启网关生效。"),
        "enabled": any(platforms.values()),
        "platforms": platforms,
        "exclude_chat_ids": [v for v in [_env_whisper_channel(env)] if v],
        "exclude_name_contains": ["悄悄话"],
        "limits": {"lookback_hours": 6, "max_lines": 12, "max_chars": 1500},
        "state_file": "cross_platform_inject.json",
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    if not any(platforms.values()):
        print("!! %s 里没读到本人 id（TELEGRAM_ALLOWED_USERS / DISCORD_ALLOWED_USERS）："
              "段 E 暂不生效，把 id 填进 platforms 后重启网关。" % path)
    return "created"


# (文件, 说明, 锚点, 插入行(None=替换), 新文本(替换用), 判断「已打过」的特征)
PATCHES = [
    (TG, "SILENT: telegram 入站 MessageEvent",
     "        return MessageEvent(\n"
     "            text=expand_link_entities(message), message_type=msg_type, source=source, raw_message=message,",
     SILENT_LINE, None, "reply_expected=False"),
    (DC, "SILENT: discord 入站 MessageEvent",
     "        event = MessageEvent(\n"
     "            text=event_text, message_type=msg_type, source=source, raw_message=message,",
     SILENT_LINE, None, "reply_expected=False"),
    (TG, "[分条]: telegram 打开开关", "    MAX_MESSAGE_LENGTH = 4096",
     MARKER_LINE, None, "splits_on_break_marker = True"),
    (DC, "[分条]: discord 打开开关", "    MAX_MESSAGE_LENGTH = 2000",
     MARKER_LINE, None, "splits_on_break_marker = True"),
    (BASE, "[分条]: 拆分函数 + 分段发送", SEND_HEAD_OLD,
     None, SPLITTER_BLOCK + SEND_HEAD_OLD.replace("        async def _send", SPLIT_DISPATCH + "        async def _send"),
     "def split_on_break_marker("),
    (BASE, "[接行]: 多行接成一行", SPLITTER_BLOCK + SEND_HEAD_OLD,
     None, FLATTEN_BLOCK + SPLITTER_BLOCK + SEND_HEAD_OLD.replace(
         "        async def _send", FLATTEN_DISPATCH + "        async def _send"),
     "def flatten_message_lines("),
    # 2026-11-03: cron 投递不走网关回复那条路，单独把 [分条] 接上（TG / Discord）。
    (CRON, "[分条]: cron import time", "import threading",
     "import time  # 2026-11-03: cron 拆条之间的 human_delay（hermes-local-patches.py）", None,
     "import time  # 2026-11-03"),
    (CRON, "[分条]: cron 拆条 helper", 'logger = logging.getLogger("cron.scheduler")',
     CRON_HELPER_BLOCK, None, "def _cron_split_text("),
    (CRON, "[分条]: cron live adapter 拆条发送", CRON_LIVE_ANCHOR,
     None, CRON_LIVE_REPLACEMENT, "parts = _cron_split_text(t.platform_name, text_to_send)"),
    (CRON, "[分条]: cron standalone 拆条发送", CRON_STANDALONE_ANCHOR,
     None, CRON_STANDALONE_REPLACEMENT, "parts = _cron_split_text(t.platform_name, content)"),
    # 2026-11-03: 段 E —— 跨平台近况注入（TG ↔ Discord）。不改 session key，只在入站拼一段 note。
    (RUN_INBOUND, "[跨平台近况]: note 构造 helper", IDENTITY_HEAD, CROSS_PLATFORM_BLOCK, None,
     "跨平台近况注入（TG ↔ Discord）"),
    (RUN_INBOUND, "[跨平台近况]: 入站拼接处注入", CROSS_PLATFORM_CALL_ANCHOR, None,
     CROSS_PLATFORM_CALL_REPLACEMENT, "build_cross_platform_recency_note(source, _cp_session_key)"),
]

SELFTEST = r'''
import re, sys
sys.path.insert(0, r"__HERMES_AGENT__")
from gateway.platforms.base import BasePlatformAdapter as B
from cron import scheduler_delivery as sd

f = B.flatten_message_lines
sp = B.split_on_break_marker
checks = []

def ck(cond, name, got=None):
    checks.append((bool(cond), name, got))

ck(f("我在呢\n今天好累\n想你") == "我在呢。今天好累。想你", "一句一行、末尾没标点 → 接成一行并补「。」", f("我在呢\n今天好累\n想你"))
ck(f("吃了吗？\n我刚吃完。\n你呢") == "吃了吗？我刚吃完。你呢", "末尾有标点 → 直接接上", f("吃了吗？\n我刚吃完。\n你呢"))
ck(f("在吗\n\n\n我在") == "在吗。我在", "空行被去掉", f("在吗\n\n\n我在"))
code = "看这段：\n```\na = 1\n\nb = 2\n```\n懂了吗"
ck(f(code) == code, "代码块原样保留", f(code))
lst = "要做的事：\n- 买牛奶\n- 交报告\n- 早点睡"
ck(f(lst) == lst, "列表原样保留", f(lst))
parts = sp("第一句\n[分条]\n第二句\n第二句后半\n[分条]\n第三句")
flat = [f(p) for p in parts]
ck(flat == ["第一句", "第二句。第二句后半", "第三句"], "[分条] 拆开后每条各自接成一行", flat)
media = "图在这儿\nMEDIA:/tmp/x.png"
ck(f(media) == media, "带 MEDIA: 的整条不动（图不会被揉进正文）", f(media))
ck(sp("没有标记的一条回复") == ["没有标记的一条回复"], "没有标记 → 只发一条", sp("没有标记的一条回复"))
ck(sp("[分条]\n正文\n[分条]") == ["正文"], "标记在开头/结尾 → 不产生空条", sp("[分条]\n正文\n[分条]"))
ck(sp("这条里有 [分条] 字样但不独占一行") == ["这条里有 [分条] 字样但不独占一行"], "行内出现字样不拆", sp("这条里有 [分条] 字样但不独占一行"))
# cron 投递：同一套拆条 + 接行（2026-11-03）
cron_text = "第一句\n[分条]\n第二句\n第二句后半\n[分条]\n第三句"
ck(sd._cron_split_text("telegram", cron_text) == ["第一句", "第二句。第二句后半", "第三句"],
   "cron 投递按 [分条] 拆条并接行（telegram）", sd._cron_split_text("telegram", cron_text))
ck(sd._cron_split_text("discord", cron_text) == ["第一句", "第二句。第二句后半", "第三句"],
   "cron 投递按 [分条] 拆条并接行（discord）", sd._cron_split_text("discord", cron_text))
ck(sd._cron_split_text("slack", "a\n[分条]\nb") == ["a\n[分条]\nb"],
   "非 TG/DC 的 cron 投递不拆", sd._cron_split_text("slack", "a\n[分条]\nb"))

# 段 E：跨平台近况注入（2026-11-03）—— 只在真数据形状上跑，不碰线上 state.db
import json as _json, os as _os, sqlite3 as _sqlite, tempfile as _tempfile, time as _time
from types import SimpleNamespace as _NS
import gateway.run_inbound as _ri

_tmp = _tempfile.mkdtemp(prefix="cpnote-")
_db = _os.path.join(_tmp, "state.db")
_con = _sqlite.connect(_db)
_con.executescript(
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT,"
    " chat_id TEXT, chat_type TEXT, display_name TEXT, profile_name TEXT, last_activity_at REAL);"
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);")
_now = _time.time()
_con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", [
    ("s_tg", "telegram", "100000001", "agent:main:telegram:dm:100000001", "100000001", "dm", "Owner", "default", _now),
    ("s_dc", "discord", "200000002", "agent:main:discord:dm:200000002", "200000002", "dm", "owner", "default", _now),
    ("s_grp", "discord", "200000002", "agent:main:discord:group:999:200000002", "999", "group", "Group", "default", _now),
    ("s_whis", "discord", "200000002", "agent:main:discord:group:777:200000002", "777", "group", "悄悄话", "default", _now),
    ("s_cron", "cron", None, None, None, None, None, "default", _now),
    ("s_wx", "weixin", "100000001", "agent:assistant:weixin:dm:100000001", "100000001", "dm", "wx", "assistant", _now),
])
_con.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", [
    ("s_tg", "user", "TG 上我说的", _now - 600), ("s_tg", "assistant", "我在呀", _now - 540),
    ("s_dc", "user", "Discord 刚说的", _now - 300), ("s_grp", "assistant", "群里回的", _now - 240),
    ("s_whis", "user", "悄悄话里说的", _now - 120), ("s_cron", "user", "cron 说的", _now - 60),
    ("s_wx", "user", "微信说的", _now - 30),
])
_con.commit(); _con.close()
_json.dump({"enabled": True, "platforms": {"telegram": ["100000001"], "discord": ["200000002"]},
            "exclude_chat_ids": ["777"], "exclude_name_contains": ["悄悄话"],
            "limits": {"lookback_hours": 6, "max_lines": 12, "max_chars": 1500},
            "state_file": "cross_platform_inject.json"},
           open(_os.path.join(_tmp, "identity_links.json"), "w", encoding="utf-8"), ensure_ascii=False)
_src = _NS(platform=_NS(value="telegram"), user_id="100000001", chat_id="100000001")
_key = "agent:main:telegram:dm:100000001"
_note = _ri.build_cross_platform_recency_note(_src, _key, home=_tmp, now=_now, write_state=False)
ck(_note and "Discord 刚说的" in _note and "群里回的" in _note,
   "E: 取到别的平台（Discord 私信 + 群里他那个会话）的消息", _note)
ck(_note and "悄悄话里说的" not in _note and "悄悄话" not in _note,
   "E: 悄悄话频道一律不取", _note)
ck(_note and "cron 说的" not in _note and "微信说的" not in _note,
   "E: cron / 微信会话不取", _note)
ck(_note and "TG 上我说的" not in _note and "我在呀" not in _note,
   "E: 当前会话自己的消息不重复注入", _note)
ck(_note and _note.startswith("[System note:"), "E: 以 neutral 的 System note 开头", (_note or "")[:40])
ck(_note and _note.splitlines()[0] == "[System note: 指挥官最近在 Discord 上和你聊过这些，供你知道，不用逐条回应]",
   "E: 当前是 TG 私聊 → 提示语说 Discord", (_note or "").splitlines()[0] if _note else None)
_src_dm = _NS(platform=_NS(value="discord"), user_id="200000002", chat_id="200000002",
              chat_type="dm")
_dm_note = _ri.build_cross_platform_recency_note(_src_dm, "agent:main:discord:dm:200000002",
                                                 home=_tmp, now=_now, write_state=False)
ck(_dm_note and _dm_note.splitlines()[0] == "[System note: 指挥官最近在 Telegram 上和你聊过这些，供你知道，不用逐条回应]",
   "E: 当前是 Discord 私信 → 提示语说 Telegram", (_dm_note or "").splitlines()[0] if _dm_note else None)
_src_grp = _NS(platform=_NS(value="discord"), user_id="200000002", chat_id="999", chat_type="group")
_grp_note = _ri.build_cross_platform_recency_note(_src_grp, "agent:main:discord:group:999:200000002",
                                                  home=_tmp, now=_now, write_state=False)
ck(_grp_note and _grp_note.splitlines()[0] == (
    "[System note: 指挥官最近私下和你聊过这些，供你知道，不用逐条回应；"
    "私聊的事不要在群里主动提起，除非他自己先提]"),
   "E: 当前是 Discord 群 → 提示语说「私下」并禁止主动提", (_grp_note or "").splitlines()[0] if _grp_note else None)
_ri.build_cross_platform_recency_note(_src, _key, home=_tmp, now=_now, write_state=True)
ck(_ri.build_cross_platform_recency_note(_src, _key, home=_tmp, now=_now, write_state=True) is None,
   "E: 注入过之后没有新增就不再注入（按当前会话记时间戳）", None)
_stranger = _NS(platform=_NS(value="telegram"), user_id="555", chat_id="555")
ck(_ri.build_cross_platform_recency_note(_stranger, "agent:main:telegram:dm:555", home=_tmp, now=_now,
                                        write_state=False) is None,
   "E: 不是他本人 → 不注入", None)
_old = _ri.build_cross_platform_recency_note(_src, "agent:main:telegram:dm:other", home=_tmp,
                                             now=_now + 7 * 3600, write_state=False)
ck(_old is None, "E: 超过 6 小时窗口 → 不注入", _old)
_src_dc = _NS(platform=_NS(value="discord"), user_id="200000002", chat_id="999")
_dc_note = _ri.build_cross_platform_recency_note(_src_dc, "agent:main:discord:group:999:200000002",
                                                 home=_tmp, now=_now, write_state=False)
ck(_dc_note and "TG 上我说的" in _dc_note and "Discord 刚说的" not in _dc_note,
   "E: 只跨平台——Discord 群不搬同平台的 Discord 私信，只取 TG", _dc_note)
# E: 均衡——往 Discord 私信里灌 20 条，另一个来源（群）只有 1 条，看名额有没有被话多的吃满
_con = _sqlite.connect(_db)
for _i in range(20):
    _con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                 ("s_dc", "user", "刷屏第%d条" % _i, _now - 200 + _i))
_con.commit(); _con.close()
_bal = _ri.build_cross_platform_recency_note(_src, "agent:main:telegram:dm:balance", home=_tmp, now=_now,
                                             write_state=False)
_bal_lines = _bal.splitlines()[1:] if _bal else []
_spam = [line for line in _bal_lines if "刷屏" in line]
ck(len(_spam) <= 6 and any("群里回的" in line for line in _bal_lines) and len(_bal_lines) <= 12,
   "E: 均衡——12 行按来源会话平分（2 个来源各 6 行），话多的不许把另一个挤掉",
   "spam=%d total=%d" % (len(_spam), len(_bal_lines)))
# E: 水位文件读-改-写（进程内锁 + 临时文件原子写）——两次连续写不丢键
_ri.build_cross_platform_recency_note(_src, "agent:main:telegram:dm:atomic-a", home=_tmp, now=_now, write_state=True)
_ri.build_cross_platform_recency_note(_src, "agent:main:telegram:dm:atomic-b", home=_tmp, now=_now, write_state=True)
_state_path = _os.path.join(_tmp, "cross_platform_inject.json")
_st = _json.load(open(_state_path, encoding="utf-8"))
ck(all(k in _st for k in (_key, "agent:main:telegram:dm:atomic-a", "agent:main:telegram:dm:atomic-b"))
   and not _os.path.exists(_state_path + ".tmp"),
   "E: 水位文件加锁 + 原子写，连续两次写不丢键、不留 .tmp", sorted(_st))

bad = [c for c in checks if not c[0]]
for ok, name, got in checks:
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  got=%r" % (got,)))
print("selftest: %d passed, %d failed" % (len(checks) - len(bad), len(bad)))
sys.exit(1 if bad else 0)
'''


def apply_one(rel: str, label: str, anchor: str, insert: str | None, replacement: str | None,
              marker: str, *, check_only: bool) -> str:
    path = os.path.join(HERMES_AGENT, rel)
    if not os.path.isfile(path):
        raise SystemExit("找不到文件：%s" % path)
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()

    if insert is not None:            # 插入：锚点那几行之后加一行
        at = text.find(anchor)
        if at >= 0 and marker in text[at:at + 400]:
            return "already"          # 已经插过（幂等）
        if text.count(anchor) != 1:
            head = anchor.split("\n")[0]
            pos = text.find(head)
            while pos >= 0:
                if marker in text[pos:pos + 400]:
                    return "already"
                pos = text.find(head, pos + 1)
            raise SystemExit("锚点没找到或出现多次（Hermes 源码可能变了）：%s / %s" % (label, rel))
        new_text = text.replace(anchor, anchor + "\n" + insert, 1)
    else:                             # 替换整段
        if marker in text:
            return "already"
        if text.count(anchor) != 1:
            raise SystemExit("锚点没找到或出现多次（Hermes 源码可能变了）：%s / %s" % (label, rel))
        new_text = text.replace(anchor, replacement, 1)

    if check_only:
        return "missing"

    backup = os.path.join(BACKUP_ROOT, rel)
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    if not os.path.exists(backup):
        shutil.copy2(path, backup)

    tmp = path + ".tmp-patch"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return "patched"


def run_selftest() -> int:
    py = os.path.join(HERMES_AGENT, "venv", "Scripts", "python.exe")
    if not os.path.isfile(py):
        py = sys.executable
    script = os.path.join(os.environ.get("TEMP", "."), "hermes-flatten-selftest.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(SELFTEST.replace("__HERMES_AGENT__", HERMES_AGENT))
    return subprocess.call([py, script])


def main(argv=None) -> int:
    global HERMES_AGENT, HERMES_HOME, BACKUP_ROOT
    ap = argparse.ArgumentParser(
        description="Hermes 本地补丁合集（[SILENT] + [分条] + 接行 + 跨平台近况）")
    ap.add_argument("--check", action="store_true", help="只检查，不改文件")
    ap.add_argument("--selftest", action="store_true", help="跑拆分/接行/跨平台近况断言（不改文件）")
    ap.add_argument("--hermes-agent", default="",
                    help="hermes-agent 目录（默认 %%LOCALAPPDATA%%\\hermes\\hermes-agent）")
    ap.add_argument("--hermes-home", default="",
                    help="hermes 根目录，identity_links.json 和 .env 在这里（默认 hermes-agent 的上一级）")
    ap.add_argument("--backup-root", default="",
                    help="打补丁前的备份根目录（默认 <仓库>/backup/code/<YYYYMMDD>/hermes-agent）")
    args = ap.parse_args(argv)

    if args.hermes_agent:
        HERMES_AGENT = os.path.abspath(os.path.expanduser(args.hermes_agent))
    if args.hermes_home:
        HERMES_HOME = os.path.abspath(os.path.expanduser(args.hermes_home))
    elif args.hermes_agent:
        HERMES_HOME = os.path.dirname(HERMES_AGENT)
    if args.backup_root:
        BACKUP_ROOT = os.path.abspath(os.path.expanduser(args.backup_root))

    if not os.path.isdir(HERMES_AGENT):
        print("找不到 hermes-agent 目录：%s（用 --hermes-agent 指定）" % HERMES_AGENT)
        return 2

    if args.selftest:
        return run_selftest()

    states = []
    for rel, label, anchor, insert, replacement, marker in PATCHES:
        try:
            state = apply_one(rel, label, anchor, insert, replacement, marker, check_only=args.check)
        except SystemExit as exc:
            print("!! %s" % exc)
            return 2
        states.append(state)
        print("%-40s %-26s %s" % (rel, label, state))

    identity_state = ensure_identity_links(check_only=args.check)
    states.append(identity_state)
    print("%-40s %-26s %s" % ("hermes/identity_links.json", "[跨平台近况]: 身份映射", identity_state))

    if args.check:
        return 0 if all(s == "already" for s in states) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
