#!/usr/bin/env python3
"""平台桥（2026-11-02）：她的 Telegram 私聊与 Discord（私信 + 一个群频道）由这里收发。

它不是转发网关，只是「她的手」：
  · 收到他的消息 → 写未回箱 `state/inbox.json` → 叫醒宿主一轮（POST 8650，正文以【平台消息】开头）；
  · 她要说的话，只有她调 `send_message`（agent/extensions/albion-chat.ts）→ 本文件 `POST /send` 才发得出去；
    她那一轮的回复正文不会自动发到任何平台。

进程与端口：用 `/root/albion-pi/chat-venv/bin/python bin/chat_bridge.py` 跑，本地 aiohttp 绑 127.0.0.1:8660
（`POST /send`、`GET /health`，Bearer = agent/.env 的 API_SERVER_KEY）。日志 logs/chat.log（不记 token）。

外面只能走代理 127.0.0.1:7897（WSL 直连 DNS 超时）；回环（8650 / 9881）不走代理。

凭据读 `<albion 根>/.env` 与 `<albion 根>/agent/.env`（根目录 = 本文件上一级，`ALBION_HOME` 可覆盖）：
  ALBION_TG_BOT_TOKEN / ALBION_TG_OWNER_ID
  ALBION_DISCORD_BOT_TOKEN / ALBION_DISCORD_OWNER_ID / ALBION_DISCORD_CHANNEL_ID
  ALBION_DISCORD_WHISPER_CHANNEL_ID（可选；没配就在服务器里按名字「悄悄话」找）
  API_SERVER_KEY（8660 鉴权 + 调宿主）
  ALBION_CHAT_TTS_VOICE（默认 albion=Irodori 日语，带中文 caption；denia=GSV 中文，不带 caption）
缺 Discord 的 token/owner id 就只跑 TG，日志里说明。

定稿（主脑 11-02）：
  · 「决定不回」不加动作：通知轮没回就留箱，下一次醒来轮念给她，那轮结束后统统了结（wake.after_wake）；
  · 群唤醒：他发言或 @她 → 10 秒合批；别的 bot 只入箱当上下文；不按频道冷却，条数由 group_budget 兜底；
  · 主动找他要守节奏（wake.can_speak），回复不受限；群里主动发言只受夜间安静限制；
  · 桌面忙时同一个会话串行等锁，超时把这批退回 unread。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

BIN = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(BIN)
if BIN not in sys.path:                       # 桥和宿主不在同一个进程，自己把 bin 放进 sys.path
    sys.path.insert(0, BIN)

import inbox                                   # noqa: E402
import wake                                     # noqa: E402
import group_budget                             # noqa: E402
import whisper_budget                           # noqa: E402

PLATFORM_MARKER = wake.PLATFORM_MARKER
SESSION_DM = "albion-desktop"                  # TG 私聊 / Discord 私信：和桌面同一个会话
SESSION_GROUP = "albion-discord"               # Discord 群：自己的真会话（长期记忆共享）
GROUP_BATCH_S = 10.0                           # 群里合批窗口（逐条限制已去掉，只留额度兜底）
DM_QUIET_S = 2.5                               # 私信合批：最后一条之后安静这么久再叫醒
DM_MAX_WAIT_S = 6.0                            # 但从第一条算最多等这么久
TYPING_REFRESH_S = 4.0                         # TG 的「正在输入」5 秒过期，4 秒续一次
TTS_URL = "http://127.0.0.1:9881/v1/audio/speech"
CHANNELS = ("telegram", "discord", "discord_group", "discord_whisper")
WHISPER_CHANNEL_NAME = "悄悄话"                # 没配 ALBION_DISCORD_WHISPER_CHANNEL_ID 时按名字找
WHISPER_PEER_LABEL = "可畏"                    # 悄悄话里另一个 bot（Formidable）的显示名
WHISPER_PEER_ALIASES = ("可畏", "formidable")
WHISPER_HEAD = "悄悄话里有新消息（这里是我和可畏两个人说话的地方）："
WHISPER_TAIL = "想回就用 send_message（channel=discord_whisper）；这里写的字她看不到。"
# 群里还有可畏（Hermes 那边的 bot）。这句是她群聊说明的一部分，原文照定稿（2026-11-02 用户改）。
GROUP_PEER_NOTE = "可畏也在群里，群里说的话她都看得到。"
GROUP_INVITE_NOTE = "这些你可以回，也可以不回，想说再说。"
GROUP_BUDGET_LOW = "群里你们俩这一轮已经说了 %d 条，还能再说 %d 条，差不多该收尾了。"
GROUP_BUDGET_EMPTY = "这一轮群里说得够多了，等指挥官再开口吧。"
SAME_REPLY_S = 120.0                          # 同一轮连发几条时，这么久内都算「还在回他这一轮」

_LOG = {"path": ""}


# ── 配置与日志 ──────────────────────────────────────────────────────────────
def parse_env(text):
    """KEY=VALUE 一行一条；只去首尾空白和一层引号（值里的空格是数据）。"""
    out = {}
    for raw in (text or "").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        key = "".join(key.split())
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val.replace("\r", "")
    return out


def load_config(environ=None, home=None):
    """环境变量优先，其次 `<home>/.env`，再 `<home>/agent/.env`（后者只有 API_SERVER_KEY 等）。"""
    env = dict(os.environ if environ is None else environ)
    home = home or env.get("ALBION_HOME") or HOME
    file_env = {}
    for rel in (".env", os.path.join("agent", ".env")):
        path = os.path.join(home, rel)
        try:
            with open(path, encoding="utf-8") as f:
                file_env.update(parse_env(f.read()))
        except OSError:
            pass                                     # 没这个文件不算错：可能只用环境变量

    def get(key, default=""):
        return env.get(key) or file_env.get(key) or default

    try:
        chat_port = int(get("ALBION_CHAT_PORT", "8660"))
    except ValueError:
        chat_port = 8660
    return {
        "home": home,
        "chat_port": chat_port,
        "brain_url": "http://127.0.0.1:%s" % get("ALBION_BRAIN_PORT", "8650"),
        "api_key": get("API_SERVER_KEY", "").strip(),
        "tg_token": get("ALBION_TG_BOT_TOKEN", "").strip(),
        "tg_owner": str(get("ALBION_TG_OWNER_ID", "")).strip(),
        "dc_token": get("ALBION_DISCORD_BOT_TOKEN", "").strip(),
        "dc_owner": str(get("ALBION_DISCORD_OWNER_ID", "")).strip(),
        "dc_channel": str(get("ALBION_DISCORD_CHANNEL_ID", "")).strip(),
        "dc_whisper": str(get("ALBION_DISCORD_WHISPER_CHANNEL_ID", "")).strip(),
        "tts_url": get("ALBION_CHAT_TTS_URL", TTS_URL).strip(),
        "tts_voice": (get("ALBION_CHAT_TTS_VOICE", "albion").strip() or "albion"),
        "proxy": get("ALBION_CHAT_PROXY", "http://127.0.0.1:7897").strip(),
        # 放 WSL 本地盘：/mnt/d 走 9p，在上面找文件能慢到卡死一整轮（11-02）；11-03 起她的表情包就在这里做和维护，不再从 Windows 同步
        "stickers_dir": (get("ALBION_STICKERS_DIR", "/root/albion-pi/stickers").strip()
                         or "/root/albion-pi/stickers"),
        "log": get("ALBION_CHAT_LOG", os.path.join(home, "logs", "chat.log")),
        "timeout": float(get("ALBION_CHAT_NOTIFY_TIMEOUT", "600") or 600),
    }


def log(msg, **fields):
    line = "%s %s%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg,
                        (" " + json.dumps(fields, ensure_ascii=False)) if fields else "")
    path = _LOG.get("path")
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    # start.sh 把 stdout 也接到 logs/chat.log：同时开文件又打印会让每行出现两次（2026-11-02 部署时发现）。
    # 所以只在终端里手动跑（stdout 是 tty）才回显；有文件日志时以文件为准。
    if not path or sys.stdout.isatty():
        print(line, flush=True)


# ── 纯判定与文字（自检和真跑共用）──────────────────────────────────────────
def is_owner(user_id, owner_id):
    """只认他：id 为空一律不认（fail-closed）。"""
    return bool(owner_id) and str(user_id) == str(owner_id)


def group_should_wake(author_id, owner_id, is_bot=False):
    """群里每条消息都叫醒她看一眼（2026-11-02 用户定：两个 bot 都看得到每条，自己决定回不回）。

    返回这条消息的来源：owner（指挥官）/ bot / member。够不够额度由 group_budget 决定，
    不再按「谁 @ 了谁」逐条限制。
    """
    if is_owner(author_id, owner_id):
        return "owner"
    return "bot" if is_bot else "member"


def message_text(message):
    """群/私信里给她看的正文：优先 clean_content。

    discord.py 的 ``clean_content`` 已经把 ``<@id>`` / ``<@&id>`` / ``<#id>`` 换成了成员名/频道名，
    她看到的才是「@可畏」而不是「<@1566802611611504680>」（2026-11-02）。
    """
    return (getattr(message, "clean_content", "") or getattr(message, "content", "") or "").strip()


def _member_names(member):
    """一个成员可能被叫的所有名字：昵称(nick)、显示名(display_name)、用户名(name)。"""
    out = []
    for attr in ("nick", "display_name", "name"):
        value = getattr(member, attr, None)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def build_name_index(entries):
    """[(id, [名字…]), …] → {小写名字: id 或 None}。

    同一个名字对上两个成员时记 None（有歧义就不替换，宁可不 @ 也不 @ 错人）。
    """
    index = {}
    for member_id, names in entries:
        mid = str(member_id or "").strip()
        if not mid:
            continue
        for name in names:
            key = str(name or "").strip().lower()
            if not key:
                continue
            if key in index and index[key] != mid:
                index[key] = None
            else:
                index[key] = mid
    return index


def replace_mentions(text, index):
    """把文本里的 ``@名字`` 换成 Discord 的真 mention ``<@id>``。

    只替换能**精确**（整名、不区分大小写、名字后面不是字母数字下划线）对上的名字；
    长名字先换，避免 ``@Albion`` 抢掉 ``@Albion Bot``；对不上的原样留着。
    """
    out = str(text or "")
    if not out or not index:
        return out
    for name in sorted((n for n in index if index[n]), key=len, reverse=True):
        target = "<@%s>" % index[name]
        pattern = re.compile(r"@(" + re.escape(name) + r")(?=$|[^0-9A-Za-z_])", re.IGNORECASE)
        out = pattern.sub(target, out)
    return out


# ── 认人：只认数字 ID；名字一律运行时从 Discord 取（仓库是公开的，不写死真名）──
OWNER_LABEL = "指挥官"          # 她看到的「他」的称呼；正文里的 @他 也统一写成这个
IMPERSONATOR_LABEL = "群友（自称 %s）"


def owner_alias_set(owner_names=()):
    """{小写名字…}：owner 在 Discord 上的各种名字（name/global_name/nick）＋「指挥官」。"""
    aliases = {OWNER_LABEL.lower()}
    for name in owner_names or ():
        text = str(name or "").strip()
        if text:
            aliases.add(text.lower())
    return aliases


def claims_owner_identity(member, owner_names=()):
    """这人是不是在冒充他：名字里带「指挥官」，或跟 owner 的任一名字撞名（不分大小写）。"""
    aliases = owner_alias_set(owner_names)
    for name in _member_names(member):
        if OWNER_LABEL in name:
            return True
        if name.lower() in aliases:
            return True
    return False


def display_author(member, owner_id, owner_names=()):
    """群里「谁说的」：他 →「指挥官」；冒充者 →「群友（自称 X）」；其他人 → 昵称/显示名。"""
    if is_owner(getattr(member, "id", None), owner_id):
        return OWNER_LABEL
    if claims_owner_identity(member, owner_names):
        shown = (getattr(member, "display_name", "") or getattr(member, "name", "") or "").strip()
        return IMPERSONATOR_LABEL % (shown or "某人")
    return (getattr(member, "display_name", "") or getattr(member, "name", "")
            or str(member)).strip()


def render_author(author, owner_names=()):
    """箱子里存着的作者名（可能是改版前写进去的昵称）也按同一套规则显示。

    新入箱的消息写的就是「指挥官」，但旧数据和 context 行里还是昵称；渲染时统一。
    """
    text = str(author or "").strip()
    if not text:
        return "有人"
    if text.startswith("群友（自称"):
        return text
    if text == OWNER_LABEL or text.lower() in owner_alias_set(owner_names):
        return OWNER_LABEL
    if OWNER_LABEL in text:
        return IMPERSONATOR_LABEL % text
    return text


def rewrite_owner_mentions(text, owner_id, owner_names=()):
    """正文里 @到他 → 「@指挥官」：``<@id>`` / ``<@!id>``，以及 clean_content 换出来的他的名字。"""
    out = str(text or "")
    if not out:
        return out
    owner = str(owner_id or "").strip()
    if owner:
        out = re.sub(r"<@!?%s>" % re.escape(owner), "@" + OWNER_LABEL, out)
    others = {n for n in owner_alias_set(owner_names) if n and n != OWNER_LABEL.lower()}
    for name in sorted(others, key=len, reverse=True):
        out = re.sub(r"@(" + re.escape(name) + r")(?=$|[^0-9A-Za-z_])", "@" + OWNER_LABEL, out,
                     flags=re.IGNORECASE)
    return out


def whisper_author(member, owner_id, owner_names=(), peer_ids=()):
    """悄悄话里「谁说的」：指挥官 / 可畏（另一个 bot）/ 其他人照群规则。

    Formidable 一律显示成「可畏」（按 bot id 认，不靠昵称）；真实名字仍只在运行时取。
    """
    mid = str(getattr(member, "id", "") or "").strip()
    if mid and mid in {str(p).strip() for p in (peer_ids or ())}:
        return WHISPER_PEER_LABEL
    return display_author(member, owner_id, owner_names)


def render_whisper_author(author, owner_names=()):
    """箱子里存的作者名也按悄悄话规则显示：Formidable / 可畏 →「可畏」，他 →「指挥官」。"""
    text = render_author(author, owner_names)
    if text.lower() in WHISPER_PEER_ALIASES:
        return WHISPER_PEER_LABEL
    return text


# ── 表情包（2026-11-02）：素材在 D:\AI\stickers（不进 Git），index.json 每项有 id/file/… ──
_STICKER_CACHE = {}                      # 目录 → (mtime, {id: entry})，按 mtime 缓存，别每条都读盘


def load_sticker_index(dirpath):
    """读 ``<dir>/index.json``（按 mtime 缓存）→ ``{id: entry}``；读不到就返回空表。"""
    path = os.path.join(dirpath or "", "index.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _STICKER_CACHE.get(dirpath)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return {}
    index = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip()
        if sid:
            index[sid] = item
    _STICKER_CACHE[dirpath] = (mtime, index)
    return index


def sticker_entry(dirpath, sticker_id):
    return load_sticker_index(dirpath).get(str(sticker_id or "").strip())


def sticker_paths(dirpath, entry):
    """``{kind: 绝对路径}``：只保留 entry.file 里写了的那种（webp/png/gif）。"""
    files = (entry or {}).get("file") or {}
    if isinstance(files, str):
        files = {"png": files}
    out = {}
    if isinstance(files, dict):
        for kind, rel in files.items():
            if rel:
                out[str(kind).lower()] = os.path.join(dirpath, str(rel))
    return out


def is_animated(paths):
    """有 gif 就是动图（TG 走 sendAnimation、Discord 传 gif）。"""
    return bool((paths or {}).get("gif"))


def _sticker_upload(paths):
    """Discord 上传用哪个文件：动图 gif，静态 png（没有 png 就退回 webp）。"""
    if not paths:
        return None
    if paths.get("gif"):
        return paths["gif"]
    return paths.get("png") or paths.get("webp")


def _stamp(ts):
    from datetime import datetime
    return datetime.fromtimestamp(float(ts or 0), wake.TZ).strftime("%m-%d %H:%M")


def _hhmm(ts):
    from datetime import datetime
    return datetime.fromtimestamp(float(ts or 0), wake.TZ).strftime("%H:%M")


def dm_wait_seconds(items, now=None):
    """私信合批还要等多久：最后一条之后 2.5 秒没有新的，就从第一条算最多等 6 秒。

    他连着发几条（间隔很短）时，这些会合成一轮叫醒，而不是一条一轮。
    """
    if not items:
        return 0.0
    now = time.time() if now is None else now
    first = min(float(i.get("at") or 0) for i in items)
    last = max(float(i.get("at") or 0) for i in items)
    return max(0.0, min(last + DM_QUIET_S, first + DM_MAX_WAIT_S) - now)


def dm_line(channel, item):
    """通知里的一行（dm_notice 与「发这条的时候他又发来了」共用，格式不变）。"""
    where = {"telegram": "Telegram", "discord": "Discord 私信"}.get(channel, inbox.channel_label(channel))
    return "（%s）#%s %s 指挥官：%s" % (where, item["id"], _stamp(item.get("at")), item.get("text"))


def group_line(item, owner_names=()):
    """群里通知的一行（group_notice 与流式补话共用）。"""
    return "（Discord 群）#%s %s %s：%s" % (item["id"], _hhmm(item.get("at")),
                                            render_author(item.get("author"), owner_names),
                                            item.get("text"))


def live_note(channel, items, owner_names=()):
    """她刚发完一条，这期间又来了新消息：把新的附在 send_message 的回话里（2026-11-02）。

    她可以在同一轮里再发一条回应；不回也行，这些已经算「叫醒过」，
    不回的会在这一轮结束时由 wake 的 close() 兜住。
    """
    if not items:
        return ""
    if channel == "discord_group":
        head = "发这条的时候，群里又有人说话了："
        lines = [group_line(i, owner_names) for i in items]
    elif channel == "discord_whisper":
        head = "发这条的时候，悄悄话里又有新消息了："
        lines = [whisper_line(i, owner_names) for i in items]
    else:
        head = "发这条的时候，他又发来了："
        lines = [dm_line(channel, i) for i in items]
    return "\n".join([head] + lines + ["想回就再发一条，不回也可以。"])


def dm_notice(channel, items):
    """TG / Discord 私信的通知轮（主脑定稿的文字，照抄）。

    每行消息前面加了两个前缀（2026-11-02，为长期记忆）：``（Telegram）`` / ``（Discord 私信）``
    标明渠道、``指挥官：`` 标明是谁说的。宿主提交给 OpenViking 前会把 ``#编号 时间`` 和说明行剥掉，
    留下的正好是「（渠道）谁：原话」；她看到的定稿文字一个字没改，只是多了前缀。
    """
    where = {"telegram": "Telegram", "discord": "Discord 私信"}.get(channel, inbox.channel_label(channel))
    lines = [PLATFORM_MARKER + "指挥官在%s上给我发了消息（写给自己看的，不用念）：" % where]
    for item in items:
        lines.append(dm_line(channel, item))
    lines.append("想回就用 send_message 回他（channel=%s）；这一轮我在这里写的字他看不到。也可以先不回，下次醒来还会看到。"
                 % channel)
    return "\n".join(lines)


def budget_note(budget):
    """额度提醒（原文照定稿）：R≤3 时提醒收尾，R=0 时让她等指挥官开口。"""
    if not budget:
        return ""
    remaining = int(budget.get("remaining") or 0)
    if remaining <= 0:
        return GROUP_BUDGET_EMPTY
    if remaining <= 3:
        return GROUP_BUDGET_LOW % (int(budget.get("used") or 0), remaining)
    return ""


def group_notice(channel_name, items, context=None, budget=None, owner_names=()):
    """群通知。每行消息前加 ``（Discord 群）`` 前缀（同样只为长期记忆剥壳用，定稿文字没改）。

    作者名统一走 render_author：他永远是「指挥官」，冒充者是「群友（自称 X）」，
    改版前存进箱子的旧昵称在渲染时也会被换掉（2026-11-02）。
    """
    lines = [PLATFORM_MARKER + "Discord 群「%s」里有新消息（群里大家都看得到；我和指挥官之间的私事、当面说过的话不发这儿）："
             % (channel_name or "群"),
             GROUP_PEER_NOTE]
    if context:
        # 群里刚才还在聊的（别人的话）：只给她看个上下文，不改状态、不驱动叫醒
        lines.append("（刚才群里还在聊的：）")
        for item in context:
            lines.append(group_line(item, owner_names))
    for item in items:
        lines.append(group_line(item, owner_names))
    lines.append(GROUP_INVITE_NOTE)
    note = budget_note(budget)
    if note:
        lines.append(note)
    lines.append("想说话就用 send_message（channel=discord_group）；这里写的字群里看不到。")
    return "\n".join(lines)


def whisper_line(item, owner_names=()):
    """悄悄话通知的一行：#编号 时:分 说话人：原文（说话人是「指挥官」/「可畏」）。"""
    return "#%s %s %s：%s" % (item["id"], _hhmm(item.get("at")),
                              render_whisper_author(item.get("author"), owner_names),
                              item.get("text"))


def whisper_notice(items, budget=None, owner_names=()):
    """悄悄话的通知轮（2026-11-02 定稿的文字，照抄）：开头必须是【平台消息】，结尾问她要怎么回。"""
    lines = [PLATFORM_MARKER + WHISPER_HEAD]
    for item in items:
        lines.append(whisper_line(item, owner_names))
    lines.append(WHISPER_TAIL)
    note = whisper_budget.note(budget)
    if note:
        lines.append(note)
    return "\n".join(lines)


def whisper_budget_payload(budget):
    """GET /whisper_budget 的回话：compute() 的结果 + note（照 /group_budget 的写法）。"""
    out = dict(budget or {})
    out["ok"] = True
    out["note"] = whisper_budget.note(budget)
    return out


def proactive_gate(channel, pending_count, can_speak_result=None, quiet=None):
    """#7：该渠道箱里没有待回 = 主动找他。返回 (能不能, 不能的原因)。

    主动要守她自己的开口节奏与退避（wake.can_speak）；群里主动发言只受夜间安静限制。
    """
    if pending_count:
        return True, ""                              # 回复：不受限
    if channel == "discord_group":
        if quiet is None:
            quiet = wake.in_quiet(time.time())
        return (False, "现在是夜里安静的时候，先不打扰") if quiet else (True, "")
    if can_speak_result is None:
        can_speak_result = wake.can_speak(wake.load(), wake.self_plan())
    ok, why = can_speak_result
    return (True, "") if ok else (False, why)


def group_wait_seconds(items, now, batch_s=GROUP_BATCH_S):
    """群里还要等多久才叫醒：只等合批窗口（同频道冷却已去掉，额度由 group_budget 兜底）。"""
    if not items:
        return 0.0
    first = min(float(i.get("at") or 0) for i in items)
    return max(0.0, first + batch_s - now)


def send_result_text(channel, *, voiced=False, notes=(), answered=0, proactive=False):
    label = inbox.channel_label(channel)
    if voiced:
        text = "已发到%s，语音条也发了（caption 是我的中文原文）。" % label
    else:
        text = "已发到%s。" % label
    for note in notes:
        text += note
    if answered:
        text += "（%d 条没回的消息算回了）" % answered
    if proactive and channel != "discord_group":
        text += "（这次是主动找他）"
    return text


# ── 宿主：叫醒一轮（同一会话串行）───────────────────────────────────────────
class Host:
    def __init__(self, cfg):
        self.cfg = cfg
        self._locks = {}

    def _lock(self, session):
        if session not in self._locks:
            self._locks[session] = asyncio.Lock()
        return self._locks[session]

    async def chat(self, session, text):
        import aiohttp
        payload = {"model": "albion", "stream": False, "session_id": session,
                   "messages": [{"role": "user", "content": text}]}
        headers = {"Content-Type": "application/json"}
        if self.cfg["api_key"]:
            headers["Authorization"] = "Bearer " + self.cfg["api_key"]
        timeout = aiohttp.ClientTimeout(total=self.cfg["timeout"])
        async with self._lock(session):
            async with aiohttp.ClientSession(timeout=timeout) as session_http:   # 回环：不走代理
                async with session_http.post(self.cfg["brain_url"] + "/v1/chat/completions",
                                             json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError("brain HTTP %s: %s" % (resp.status, body[:160]))
                    return body


async def notify(cfg, host, session, text, ids):
    """标 notified → 叫醒 → 失败退回 unread（下一轮醒来还会看到）。"""
    inbox.mark_notified(ids)
    log("notify_start", session=session, ids=ids, chars=len(text))
    try:
        await host.chat(session, text)
    except Exception as exc:                      # noqa: BLE001 - 宿主忙/挂了都不该让桥倒
        inbox.mark(ids, "unread")
        log("notify_failed", session=session, ids=ids, error=repr(exc)[:200])
        return False
    log("notify_ok", session=session, ids=ids)
    return True


# ── TTS：Irodori/GSV → ogg/opus ────────────────────────────────────────────
async def synth_ogg(cfg, text):
    """合成一条语音条（ogg/opus）。失败返回 None（调用方降级成文字）。"""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as http:      # 回环：不走代理
            async with http.post(cfg["tts_url"], json={"input": text, "voice": cfg["tts_voice"]}) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log("tts_failed", status=resp.status, body=body[:120])
                    return None
                wav = await resp.read()
    except Exception as exc:                      # noqa: BLE001
        log("tts_failed", error=repr(exc)[:160])
        return None
    import tempfile
    with tempfile.TemporaryDirectory(prefix="albion-chat-tts-") as tmp:
        src = os.path.join(tmp, "in.wav")
        dst = os.path.join(tmp, "out.ogg")
        with open(src, "wb") as f:
            f.write(wav)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-v", "error", "-i", src,
                "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1", dst,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _out, err = await proc.communicate()
        except FileNotFoundError:
            log("tts_failed", error="ffmpeg missing")
            return None
        if proc.returncode != 0 or not os.path.exists(dst):
            log("tts_failed", error="ffmpeg rc=%s %s" % (proc.returncode, (err or b"")[:160].decode("utf-8", "replace")))
            return None
        with open(dst, "rb") as f:
            return f.read()


def voice_caption(cfg, text):
    """Irodori 是日语——语音条要带她的中文原文；GSV(denia) 是中文，不用 caption。"""
    if cfg["tts_voice"].lower() == "denia":
        return None
    return (text or "")[:1024]


# ── Telegram ───────────────────────────────────────────────────────────────
class Telegram:
    API = "https://api.telegram.org/bot%s/%s"

    def __init__(self, cfg, on_update):
        self.cfg = cfg
        self.on_update = on_update
        self.http = None
        self.task = None
        self.polling = False

    @property
    def configured(self):
        return bool(self.cfg["tg_token"] and self.cfg["tg_owner"])

    def _url(self, method):
        return self.API % (self.cfg["tg_token"], method)

    async def _api(self, method, timeout=45, **params):
        if self.http is None:
            raise RuntimeError("telegram not connected")
        import aiohttp
        async with self.http.post(self._url(method), json=params,
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError("%s: %s" % (method, str(data.get("description"))[:160]))
        return data.get("result")

    async def start(self):
        if not self.configured:
            log("tg_off", reason="missing ALBION_TG_BOT_TOKEN or ALBION_TG_OWNER_ID")
            return
        import aiohttp
        # 外网只走代理；aiohttp 不会自己读 HTTPS_PROXY，必须显式给
        self.http = aiohttp.ClientSession(proxy=self.cfg["proxy"] or None)
        try:
            me = await self._api("getMe")
            log("tg_ready", username=str(me.get("username")))
        except Exception as exc:                  # noqa: BLE001
            log("tg_getme_failed", error=repr(exc)[:160])
        try:
            # 以前设过 webhook 时 getUpdates 会 409：启动先删一次（不动已排队的消息）
            await self._api("deleteWebhook", drop_pending_updates=False)
        except Exception as exc:                  # noqa: BLE001
            log("tg_delete_webhook_failed", error=repr(exc)[:160])
        self.polling = True
        self.task = asyncio.create_task(self._poll())
        log("tg_polling")

    async def _poll(self):
        offset = 0
        while True:
            try:
                updates = await self._get_updates(offset)
            except asyncio.CancelledError:
                raise
            except Exception as exc:              # noqa: BLE001
                log("tg_poll_failed", error=repr(exc)[:160])
                await asyncio.sleep(5)
                continue
            for update in updates or []:
                try:
                    offset = max(offset, int(update.get("update_id") or 0) + 1)
                except (TypeError, ValueError):
                    continue
                try:
                    self.on_update(update)
                except Exception as exc:          # noqa: BLE001
                    log("tg_handle_failed", error=repr(exc)[:160])

    async def _get_updates(self, offset):
        import aiohttp
        async with self.http.post(self._url("getUpdates"),
                                  json={"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                                  timeout=aiohttp.ClientTimeout(total=45)) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError("getUpdates: %s" % str(data.get("description"))[:160])
        return data.get("result")

    async def send_text(self, text):
        res = await self._api("sendMessage", chat_id=int(self.cfg["tg_owner"]), text=text,
                              disable_web_page_preview=True)
        return (res or {}).get("message_id")

    async def send_chat_action(self, action="typing"):
        """「正在输入 / 在录语音 / 在挑表情」（TG 的状态 5 秒过期，调用方自己续）。"""
        res = await self._api("sendChatAction", chat_id=int(self.cfg["tg_owner"]), action=action)
        return bool(res)

    async def send_voice(self, ogg, caption=None):
        if self.http is None:
            raise RuntimeError("telegram not connected")
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("chat_id", str(int(self.cfg["tg_owner"])))
        if caption:
            form.add_field("caption", caption)
        form.add_field("voice", ogg, filename="voice.ogg", content_type="audio/ogg")
        async with self.http.post(self._url("sendVoice"), data=form,
                                  timeout=aiohttp.ClientTimeout(total=120)) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError("sendVoice: %s" % str(data.get("description"))[:160])
        return (data.get("result") or {}).get("message_id")

    async def _send_file(self, method, field, path, caption=None):
        """上传一个本地文件给 Telegram（multipart）。"""
        if self.http is None:
            raise RuntimeError("telegram not connected")
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("chat_id", str(int(self.cfg["tg_owner"])))
        if caption:
            form.add_field("caption", caption)
        with open(path, "rb") as fh:
            form.add_field(field, fh.read(), filename=os.path.basename(path))
        async with self.http.post(self._url(method), data=form,
                                  timeout=aiohttp.ClientTimeout(total=120)) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError("%s: %s" % (method, str(data.get("description"))[:160]))
        return (data.get("result") or {}).get("message_id")

    async def send_sticker(self, paths, caption=None):
        """表情包：动图走 sendAnimation；静态优先 sendSticker(webp)，失败退回 sendPhoto(png)。"""
        if is_animated(paths):
            return await self._send_file("sendAnimation", "animation", paths["gif"], caption)
        if paths.get("webp"):
            try:
                return await self._send_file("sendSticker", "sticker", paths["webp"])
            except Exception as exc:              # noqa: BLE001 - 老客户端/尺寸不合会拒，退 png
                log("tg_sticker_failed", error=repr(exc)[:120])
                if not paths.get("png"):
                    raise
        if paths.get("png"):
            return await self._send_file("sendPhoto", "photo", paths["png"], caption)
        raise RuntimeError("这张表情包没有可发的文件（webp/png/gif 都没有）")

    async def close(self):
        self.polling = False
        if self.task:
            self.task.cancel()
        if self.http:
            await self.http.close()


async def _discord_send(channel, text, sticker_path=None):
    """发一条消息；有表情包就以附件上传（静态 png、动图 gif），文字当 content。"""
    text = (text or "").strip()
    if not sticker_path:
        return await channel.send(text)
    import discord
    with open(sticker_path, "rb") as fh:
        return await channel.send(content=text or None,
                                  file=discord.File(fh, filename=os.path.basename(sticker_path)))


# ── Discord ────────────────────────────────────────────────────────────────

class _NullContext:
    """什么都不做的异步上下文（状态提示拿不到目标时用）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

class DiscordBot:
    def __init__(self, cfg, on_message):
        self.cfg = cfg
        self.on_message = on_message
        self.client = None
        self.task = None
        self.ready = False
        # 成员名 → id：给她发群消息时把「@可畏」换成真 mention（2026-11-02）
        self._members = {}
        # owner 在 Discord 上的真名（name/global_name/nick），只在运行时取；用来认 @他 和防冒充
        self._owner_names = set()
        # 悄悄话频道（2026-11-02）：ready 后解析一次；找不到就整块不启用
        self._whisper_id = ""
        self._whisper_off = False
        self._whisper_source = ""

    @property
    def configured(self):
        return bool(self.cfg["dc_token"] and self.cfg["dc_owner"])

    def remember(self, member):
        """记下一个成员的所有名字（昵称/显示名/用户名）；发群消息时用来认 @。"""
        mid = getattr(member, "id", None)
        if not mid:
            return
        names = self._members.setdefault(str(mid), set())
        names.update(_member_names(member))
        if is_owner(mid, self.cfg["dc_owner"]):
            self._owner_names.update(_member_names(member))

    def remember_owner(self, user):
        """把他的 Discord 名字记下来（name / global_name）：防冒充和 @他 要用，但不写死在代码里。"""
        mid = getattr(user, "id", None)
        if not is_owner(mid, self.cfg["dc_owner"]):
            return
        for attr in ("name", "global_name"):
            value = getattr(user, attr, None)
            if isinstance(value, str) and value.strip():
                self._owner_names.add(value.strip())
        self._owner_names.update(_member_names(user))

    def owner_name_set(self):
        """owner 现在已知的所有名字（运行时取；没有就返回空集合，调用方自己兜底）。"""
        return set(self._owner_names)

    def name_index(self):
        index = build_name_index([(mid, sorted(names)) for mid, names in self._members.items()])
        # 「@指挥官」→ 他的 <@id>：她 send_message 只写得出这个称呼（2026-11-02）。
        # 群里若有人叫「指挥官」（冒充者），这里强制指向真 owner，别 @ 到冒牌货。
        if self.cfg["dc_owner"]:
            index[OWNER_LABEL.lower()] = str(self.cfg["dc_owner"])
        return index

    def channel_name(self):
        if not self.client or not self.cfg["dc_channel"]:
            return ""
        try:
            channel = self.client.get_channel(int(self.cfg["dc_channel"]))
        except (TypeError, ValueError):
            return ""                              # 配错了别在事件回调里炸
        return getattr(channel, "name", "") or ""

    # ── 悄悄话频道（2026-11-02）──
    def whisper_channel_id(self):
        """悄悄话频道的 id；没解析到 / 整块不启用时返回空串（其它功能照旧）。"""
        return "" if self._whisper_off else str(self._whisper_id or "")

    def whisper_bot_ids(self):
        """悄悄话里算额度的两个 bot：可畏（Formidable）和阿尔比恩自己。"""
        return tuple(str(b) for b in (self.cfg.get("dc_bot_ids") or group_budget.DEFAULT_BOT_IDS))

    def whisper_peer_ids(self):
        """另一个 bot 的 id（不算自己）：她看到的「可畏」。"""
        me = str(getattr(getattr(self.client, "user", None), "id", "") or "")
        return tuple(b for b in self.whisper_bot_ids() if b != me)

    def whisper_peer_id(self):
        peers = self.whisper_peer_ids()
        return peers[-1] if peers else ""       # 自己还没解析出来时默认取 Formidable（名单第二个）

    async def resolve_whisper(self):
        """ready 后找一次悄悄话频道：先认环境变量里的 id，没配就按名字「悄悄话」找文本频道。

        找不到只记一条 `whisper_channel_missing`，整块不启用；私信、群、其它功能都不受影响。
        """
        configured = str(self.cfg.get("dc_whisper") or "").strip()
        channel, source = None, ""
        if configured:
            source = "env"
            try:
                channel = self.client.get_channel(int(configured))
            except (TypeError, ValueError):
                channel = None
            if channel is None:
                try:
                    channel = await self.client.fetch_channel(int(configured))
                except Exception as exc:          # noqa: BLE001 - 看不到就算了，下面统一记 missing
                    log("whisper_channel_fetch_failed", error=repr(exc)[:120])
        else:
            source = "name"
            for guild in list(getattr(self.client, "guilds", None) or []):
                for candidate in list(getattr(guild, "text_channels", None) or []):
                    if str(getattr(candidate, "name", "") or "").strip() == WHISPER_CHANNEL_NAME:
                        channel = candidate
                        break
                if channel is not None:
                    break
        if channel is None or getattr(channel, "guild", None) is None:
            self._whisper_id, self._whisper_off, self._whisper_source = "", True, source
            log("whisper_channel_missing", configured=bool(configured), name=WHISPER_CHANNEL_NAME)
            return ""
        self._whisper_id = str(channel.id)
        self._whisper_off = False
        self._whisper_source = source
        log("whisper_channel", id=self._whisper_id, name=getattr(channel, "name", "") or "",
            source=source)
        return self._whisper_id

    async def start(self):
        if not self.configured:
            log("discord_off", reason="missing ALBION_DISCORD_BOT_TOKEN or ALBION_DISCORD_OWNER_ID")
            return
        if not self.cfg["dc_channel"]:
            log("discord_note", reason="ALBION_DISCORD_CHANNEL_ID 没配：只收私信，不收群")
        import discord
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True            # 读消息正文要它（开发者面板里也得开）
        # discord.py 自己管 aiohttp：代理要显式给
        self.client = discord.Client(intents=intents, proxy=self.cfg["proxy"] or None)

        @self.client.event
        async def on_ready():
            self.ready = True
            # 频道名解析成什么、有没有解析到，部署时一眼能看出来（2026-11-02）
            log("discord_ready", user=str(self.client.user), guilds=len(self.client.guilds),
                channel=self.channel_name() or "(未配置或看不到)")
            try:
                owner = self.client.get_user(int(self.cfg["dc_owner"]))
                if owner is None:
                    owner = await self.client.fetch_user(int(self.cfg["dc_owner"]))
                self.remember_owner(owner)
            except Exception as exc:              # noqa: BLE001 - 取不到就等收到他消息时再记
                log("discord_owner_names_failed", error=repr(exc)[:120])
            await self._warm_members()
            log("discord_owner_names", known=len(self._owner_names))
            await self.resolve_whisper()          # 悄悄话频道只找一次；找不到只记日志，整块不启用

        @self.client.event
        async def on_message(message):
            try:
                self.on_message(message)
            except Exception as exc:              # noqa: BLE001
                log("discord_handle_failed", error=repr(exc)[:160])

        self.task = asyncio.create_task(self._run())
        log("discord_starting")

    async def _run(self):
        """discord.py 自己的循环：连不上/令牌坏了只写日志，别让整个桥陪葬。"""
        try:
            await self.client.start(self.cfg["dc_token"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:                  # noqa: BLE001
            self.ready = False
            log("discord_failed", error=repr(exc)[:200])

    async def _warm_members(self):
        """启动时先把群成员名字收进来（她第一次发 @可畏 时就能换成真 mention）。

        没有 GUILD_MEMBERS intent 时 guild.members 可能是空的，所以再 best-effort 走一次 REST；
        拿不到也不影响，之后每收到一条消息都会顺手把作者/被 @ 的人记进来。
        """
        for guild in list(self.client.guilds or []):
            members = list(getattr(guild, "members", []) or [])
            try:
                if hasattr(guild, "fetch_members"):
                    members.extend([m async for m in guild.fetch_members(limit=200)])
            except Exception as exc:              # noqa: BLE001 - 没 intent / 没权限就算了
                log("discord_members_partial", guild=str(guild.id), error=repr(exc)[:120])
            for member in members:
                self.remember(member)
        log("discord_members", known=len(self._members))

    async def typing(self, group=False, whisper=False):
        """他那边的「正在输入」：discord.py 的 channel.typing() 自己会续，退出上下文就停。

        频道还没连上时返回一个空上下文，调用方不用特判。
        """
        channel = None
        try:
            if self.client and self.ready:
                # 私信那一轮显示在他的私聊里，群那一轮显示在 #常规，悄悄话那一轮显示在 #悄悄话，不能串
                if whisper:
                    channel = self.client.get_channel(int(self._whisper_id or 0)) \
                        if self.whisper_channel_id() else None
                elif group:
                    channel = self.client.get_channel(int(self.cfg["dc_channel"] or 0))
                else:
                    # 没开 members intent 时 get_user 拿不到他，和 send_dm 一样 fetch 一次
                    user = (self.client.get_user(int(self.cfg["dc_owner"] or 0))
                            or await self.client.fetch_user(int(self.cfg["dc_owner"])))
                    channel = user.dm_channel or await user.create_dm()
        except Exception as exc:                # noqa: BLE001 - 只是状态提示，拿不到就算了
            log("typing_channel_failed", group=group, whisper=whisper, error=repr(exc)[:120])
            channel = None
        return channel.typing() if channel is not None else _NullContext()

    async def send_dm(self, text, sticker_path=None):
        if not self.client or not self.ready:
            raise RuntimeError("discord not ready")
        user = self.client.get_user(int(self.cfg["dc_owner"]))
        if user is None:
            user = await self.client.fetch_user(int(self.cfg["dc_owner"]))
        channel = user.dm_channel or await user.create_dm()
        msg = await _discord_send(channel, text, sticker_path)
        return msg.id

    async def send_group(self, text, sticker_path=None):
        if not self.client or not self.ready:
            raise RuntimeError("discord not ready")
        if not self.cfg["dc_channel"]:
            raise RuntimeError("ALBION_DISCORD_CHANNEL_ID 没配")
        channel = self.client.get_channel(int(self.cfg["dc_channel"]))
        if channel is None:
            raise RuntimeError("群频道 %s 找不到" % self.cfg["dc_channel"])
        # 「@可畏」→「<@id>」，否则 Discord 只当普通文字，不会真的 @ 到她（2026-11-02）
        msg = await _discord_send(channel, replace_mentions(text, self.name_index()), sticker_path)
        return msg.id

    async def send_whisper(self, text, sticker_path=None):
        """发到 #悄悄话；「@可畏」→ 她的真 mention（和群里同一套）。"""
        if not self.client or not self.ready:
            raise RuntimeError("discord not ready")
        if not self.whisper_channel_id():
            raise RuntimeError("悄悄话频道没找到")
        channel = self.client.get_channel(int(self._whisper_id))
        if channel is None:
            raise RuntimeError("悄悄话频道 %s 找不到" % self._whisper_id)
        index = self.name_index()
        peer = self.whisper_peer_id()
        if peer:                                  # 有人在群里叫「可畏」也照样 @ 到 Formidable
            index[WHISPER_PEER_LABEL.lower()] = peer
        msg = await _discord_send(channel, replace_mentions(text, index), sticker_path)
        return msg.id

    async def _recent_events(self, channel, limit, window):
        """频道最近 window 秒的 (作者 id, 是否 bot, 时间戳)，按时间顺序。"""
        import datetime as _dt
        after = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=window)
        events = []
        async for message in channel.history(limit=limit, after=after, oldest_first=True):
            author = getattr(message, "author", None)
            if author is None:
                continue
            events.append((str(author.id), bool(getattr(author, "bot", False)),
                           message.created_at.timestamp()))
        return events

    async def recent_events(self, limit=300, since_s=None):
        """群里最近一小时的 (作者 id, 是否 bot, 时间戳)，给 group_budget 重放用。"""
        window = group_budget.WINDOW_S if since_s is None else since_s
        if not self.client or not self.ready or not self.cfg["dc_channel"]:
            return []
        channel = self.client.get_channel(int(self.cfg["dc_channel"]))
        if channel is None:
            return []
        return await self._recent_events(channel, limit, window)

    async def recent_whisper_events(self, limit=500, since_s=None):
        """悄悄话频道最近的消息（覆盖「今天」），给 whisper_budget 从频道历史重放用。"""
        window = whisper_budget.WINDOW_S if since_s is None else since_s
        if not self.client or not self.ready or not self.whisper_channel_id():
            return []
        channel = self.client.get_channel(int(self._whisper_id))
        if channel is None:
            return []
        return await self._recent_events(channel, limit, window)

    async def close(self):
        if self.client and not self.client.is_closed():
            await self.client.close()
        if self.task:
            self.task.cancel()


# ── 桥本体 ─────────────────────────────────────────────────────────────────

class _Typing:
    """叫醒这一轮（等 host.chat 的整段时间）里显示「正在输入」，退出时一定停。

    TG：sendChatAction 每 TYPING_REFRESH_S 秒续一次（TG 的提示 5 秒过期）。
    Discord：用 discord.py 的 channel.typing()，它自己续，退出上下文就停。
    失败只记一条日志，绝不影响收发（2026-11-02）。
    """

    def __init__(self, svc, channel, action="typing"):
        self.svc = svc
        self.channel = channel
        self.action = action
        self.task = None
        self.cm = None

    async def __aenter__(self):
        try:
            if self.channel == "telegram":
                self.task = asyncio.create_task(self._loop())
            elif self.channel in ("discord", "discord_group", "discord_whisper"):
                self.cm = await self.svc.dc.typing(group=self.channel == "discord_group",
                                                   whisper=self.channel == "discord_whisper")
                await self.cm.__aenter__()
        except Exception as exc:                  # noqa: BLE001 - 只是状态提示
            log("typing_start_failed", channel=self.channel, error=repr(exc)[:120])
        return self

    async def _loop(self):
        while True:
            try:
                await self.svc.tg.send_chat_action(self.action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:              # noqa: BLE001 - 失败就不再续，别刷日志
                log("typing_failed", channel=self.channel, error=repr(exc)[:120])
                return
            await asyncio.sleep(TYPING_REFRESH_S)

    async def __aexit__(self, *exc):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except BaseException:                 # noqa: BLE001 - 取消/异常都不往外抛
                pass
            self.task = None
        if self.cm is not None:
            try:
                await self.cm.__aexit__(*exc)
            except Exception:                     # noqa: BLE001
                pass
            self.cm = None
        return False


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.host = Host(cfg)
        self.tg = Telegram(cfg, self.on_tg_update)
        self.dc = DiscordBot(cfg, self.on_discord_message)
        self._dm_worker = None
        self._group_worker = None
        self._whisper_worker = None
        self._budget_cache = (0.0, None)          # (取的时刻, 结果)：5 秒内复用，别每句话都翻频道历史
        self._replied_at = {}                     # channel -> 刚回过他的时刻（同一轮她自己连发几条时用）

    def typing(self, channel, action="typing"):
        """「正在输入」上下文：包住 notify() 或合成语音/挑表情那一段。"""
        return _Typing(self, channel, action)

    async def start(self):
        await self.tg.start()
        await self.dc.start()

    async def close(self):
        for worker in (self._dm_worker, self._group_worker, self._whisper_worker):
            if worker:
                worker.cancel()
        await self.tg.close()
        await self.dc.close()

    # ── 收消息 ──
    def on_tg_update(self, update):
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        sender = msg.get("from") or {}
        if not is_owner(sender.get("id"), self.cfg["tg_owner"]):
            log("tg_ignored", reason="not owner", chat_type=chat.get("type"))
            return
        text = msg.get("text") or msg.get("caption") or ""
        if msg.get("text"):
            kind = "text"
        elif msg.get("voice"):
            kind = "voice"
        elif msg.get("photo"):
            kind = "photo"
        else:
            kind = "other"
        if not text:
            text = "（他发来一条%s，没有文字）" % {"voice": "语音", "photo": "照片"}.get(kind, "消息")
        item = inbox.add("telegram", text, kind=kind, platform_msg_id=str(msg.get("message_id") or ""))
        if item is None:
            return                                # 重启后重放的同一条
        log("inbox_add", channel="telegram", id=item["id"], kind=kind)
        self.schedule_dm()

    def on_discord_message(self, message):
        if self.dc.client and message.author.id == self.dc.client.user.id:
            return
        # 谁说的只按数字 ID 认：他永远是「指挥官」，冒充他名字的是「群友（自称 X）」（2026-11-02）
        owner_names = self.dc.owner_name_set()
        author = display_author(message.author, self.cfg["dc_owner"], owner_names)
        # 正文里 @他（<@id> 或 clean_content 换出来的名字）一律写成「@指挥官」
        text = rewrite_owner_mentions(message_text(message), self.cfg["dc_owner"], owner_names)
        self.dc.remember(message.author)
        for member in (getattr(message, "mentions", None) or []):
            self.dc.remember(member)              # 被 @ 的人也在里面，名字顺手记住
        is_dm = getattr(message.channel, "guild", None) is None
        if is_dm:
            if not is_owner(message.author.id, self.cfg["dc_owner"]):
                log("discord_ignored", reason="not owner", kind="dm")
                return
            item = inbox.add("discord", text or "（空消息）", platform_msg_id=str(message.id), author=author)
            if item is not None:
                log("inbox_add", channel="discord", id=item["id"])
                self.schedule_dm()
            return
        # 悄悄话（2026-11-02）：可畏和指挥官说话都叫醒她；她自己的话在上面已经筛掉。
        # 没配/找不到频道时 whisper_channel_id() 是空串，这一段整个跳过，落到下面的群逻辑。
        whisper_id = getattr(self.dc, "whisper_channel_id", lambda: "")()
        if whisper_id and str(message.channel.id) == whisper_id:
            is_bot = bool(getattr(message.author, "bot", False))
            reason = group_should_wake(message.author.id, self.cfg["dc_owner"], is_bot)
            speaker = whisper_author(message.author, self.cfg["dc_owner"], owner_names,
                                     self.dc.whisper_peer_ids())
            item = inbox.add("discord_whisper", text or "（没有文字）",
                             from_=("owner" if reason == "owner" else "bot"), why="whisper",
                             platform_msg_id=str(message.id), author=speaker)
            if item is not None:
                log("inbox_add", channel="discord_whisper", id=item["id"], why=reason, author=speaker)
                self.schedule_whisper()
            return
        if not self.cfg["dc_channel"] or str(message.channel.id) != self.cfg["dc_channel"]:
            return
        is_bot = bool(getattr(message.author, "bot", False))
        reason = group_should_wake(message.author.id, self.cfg["dc_owner"], is_bot)
        if is_bot and reason != "owner":
            # 额度用完时，bot 的话只当上下文，不叫醒；他本人的消息永远叫醒（2026-11-02 用户定）
            try:
                budget = self.budget_cached()
            except Exception:                     # noqa: BLE001 - 算不出来就别拦
                budget = None
            if budget is not None and int(budget.get("remaining") or 0) <= 0:
                item = inbox.add("discord_group", text or "（没有文字）", from_="bot",
                                 platform_msg_id=str(message.id), author=author)
                if item is not None:
                    log("group_budget_empty", channel="discord_group", id=item["id"], author=author)
                return
        item = inbox.add("discord_group", text or "（没有文字）", platform_msg_id=str(message.id),
                         author=author, why=reason)
        if item is not None:
            log("inbox_add", channel="discord_group", id=item["id"], why=reason, author=author)
            self.schedule_group()

    # ── 额度（两个 bot 共用）──
    async def refresh_budget(self):
        """翻一遍频道最近一小时，重放出剩余额度；失败返回 None（拿不到就不拦）。"""
        import inspect
        try:
            events = self.dc.recent_events()
            if inspect.isawaitable(events):       # 真身是 async；自检里的假 dc 是同步的
                events = await events
            value = group_budget.compute(events, time.time(), owner_id=self.cfg["dc_owner"],
                                         bot_ids=self.cfg.get("dc_bot_ids") or group_budget.DEFAULT_BOT_IDS)
        except Exception as exc:                  # noqa: BLE001
            log("group_budget_failed", error=repr(exc)[:160])
            return None
        self._budget_cache = (time.time(), value)
        return value

    def budget_cached(self, max_age_s=120.0):
        """同步取最近一次额度；太旧就在后台刷一次（没有事件循环时只返回旧值）。"""
        ts, value = self._budget_cache
        if value is None or time.time() - ts > max_age_s:
            try:
                asyncio.get_running_loop().create_task(self.refresh_budget())
            except RuntimeError:
                pass                              # 自检/同步上下文
        return value

    # ── 悄悄话额度（只数两个 bot，不占群额度）──
    async def refresh_whisper_budget(self):
        """翻一遍悄悄话频道最近的消息，用 whisper_budget.compute 重放；失败返回 None（拿不到就不拦）。"""
        import inspect
        try:
            events = self.dc.recent_whisper_events()
            if inspect.isawaitable(events):       # 真身是 async；自检里的假 dc 是同步的
                events = await events
            value = whisper_budget.compute(events, time.time(), bot_ids=self._whisper_bot_ids())
        except Exception as exc:                  # noqa: BLE001
            log("whisper_budget_failed", error=repr(exc)[:160])
            return None
        return value

    def _whisper_bot_ids(self):
        try:
            return self.dc.whisper_bot_ids()
        except AttributeError:                    # 自检里的假 dc 没有这个方法
            return tuple(self.cfg.get("dc_bot_ids") or group_budget.DEFAULT_BOT_IDS)

    # ── 叫醒 ──
    def schedule_dm(self):
        if self._dm_worker is None or self._dm_worker.done():
            self._dm_worker = asyncio.create_task(self._drain_dm())

    def schedule_group(self):
        if self._group_worker is None or self._group_worker.done():
            self._group_worker = asyncio.create_task(self._drain_group())

    def schedule_whisper(self):
        if self._whisper_worker is None or self._whisper_worker.done():
            self._whisper_worker = asyncio.create_task(self._drain_whisper())

    @staticmethod
    def _unread(channel=None):
        """只拿「新来的」：叫醒只由 unread 驱动。

        pending() 里还有之前叫醒过、她还没回的（notified）——那些下一次醒来时会念给她，
        不能在这里再叫醒一轮（否则就是无限循环、每轮一次真模型调用）。2026-11-02 复审。
        """
        return [i for i in inbox.pending(channel) if i["state"] == "unread"]

    async def _drain_dm(self):
        """私信：把两个渠道的新消息分开念；念完就不再叫，除非又来了新的 unread。"""
        while True:
            items = [i for i in self._unread() if i["channel"] in ("telegram", "discord")]
            if not items:
                return
            waited = False
            for channel in ("telegram", "discord"):
                batch = [i for i in items if i["channel"] == channel]
                if not batch:
                    continue
                wait = dm_wait_seconds(batch, time.time())
                if wait > 0:                       # 他还在连着发：等这个渠道安静下来再一起叫醒
                    await asyncio.sleep(min(wait, 1.0))
                    waited = True
                    break
                async with self.typing(channel):
                    ok = await notify(self.cfg, self.host, SESSION_DM, dm_notice(channel, batch),
                                      [i["id"] for i in batch])
                if not ok:
                    return                        # 宿主忙/挂了：留到下一次醒来，不在这里死等
            if waited:
                continue
            await asyncio.sleep(0)

    async def _drain_group(self):
        while True:
            items = self._unread("discord_group")
            if not items:
                return
            wait = group_wait_seconds(items, time.time())
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            context = inbox.context_items("discord_group", since_s=600, limit=10)
            budget = await self.refresh_budget()
            async with self.typing("discord_group"):
                ok = await notify(self.cfg, self.host, SESSION_GROUP,
                                  group_notice(self.dc.channel_name(), items, context, budget,
                                 self.dc.owner_name_set()),
                                  [i["id"] for i in items])
            if not ok:
                return

    async def _drain_whisper(self):
        """悄悄话：和群一样的 10 秒合批，额度单独算（whisper_budget），不占群额度。"""
        while True:
            items = self._unread("discord_whisper")
            if not items:
                return
            wait = group_wait_seconds(items, time.time())
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            budget = await self.refresh_whisper_budget()
            async with self.typing("discord_whisper"):
                ok = await notify(self.cfg, self.host, SESSION_GROUP,
                                  whisper_notice(items, budget, self.dc.owner_name_set()),
                                  [i["id"] for i in items])
            if not ok:
                return

    # ── 她发消息 ──
    async def send(self, channel, text, voice=False, sticker=""):
        """返回 (ok, 文字/原因)。sticker 是 find_sticker 查到的编号（可只发表情包）。"""
        text = (text or "").strip()
        sticker = str(sticker or "").strip()
        entry = sticker_entry(self.cfg.get("stickers_dir") or "", sticker) if sticker else None
        if sticker and entry is None:
            return False, "没有这个表情包编号，先用 find_sticker 找一张"
        paths = sticker_paths(self.cfg.get("stickers_dir") or "", entry) if entry else {}
        if sticker and not paths:
            return False, "这张表情包没有可发的文件（webp/png/gif 都没有）"
        if not text and not sticker:
            return False, "text 是空的"
        if channel == "discord_group":
            budget = await self.refresh_budget()
            if budget is not None and int(budget.get("remaining") or 0) <= 0:
                log("group_send_rejected", remaining=0)
                return False, GROUP_BUDGET_EMPTY
        if channel == "discord_whisper":
            # 悄悄话单独算条数（whisper_budget 只数两个 bot），不占群额度
            if not (self.dc.whisper_channel_id() if hasattr(self.dc, "whisper_channel_id") else ""):
                return False, "悄悄话频道没找到（ALBION_DISCORD_WHISPER_CHANNEL_ID 没配，服务器里也没有「悄悄话」）"
            budget = await self.refresh_whisper_budget()
            if budget is not None and int(budget.get("remaining") or 0) <= 0:
                log("whisper_send_rejected", remaining=0)
                return False, whisper_budget.note(budget)
        pending = inbox.pending(channel)
        # 同一轮里她自己分几条发：刚回过（SAME_REPLY_S 内）的第二、三条不算「主动找他」，
        # 不受 wake.can_speak 的开口节奏约束（2026-11-02，分不分条由她自己决定）。
        same_reply = (not pending) and (time.time() - self._replied_at.get(channel, 0.0) <= SAME_REPLY_S)
        proactive = (not pending) and not same_reply
        if channel == "discord_whisper":
            proactive = False                     # 悄悄话不占群额度，也不走「开口节奏」（2026-11-02）
        if proactive:
            ok, why = proactive_gate(channel, 0)
            if not ok:
                return False, "现在不该主动找他：%s。" % why
        notes = []
        voiced = False
        sent_sticker = False
        try:
            if channel == "telegram":
                if voice and text:
                    async with self.typing("telegram", "record_voice"):
                        ogg = await synth_ogg(self.cfg, text)
                else:
                    ogg = None
                if voice and text and ogg is None:
                    notes.append("语音没合成出来，只发了文字。")
                if ogg:
                    await self.tg.send_voice(ogg, voice_caption(self.cfg, text))
                    voiced = True
                elif text:
                    await self.tg.send_text(text)
                if sticker:
                    async with self.typing("telegram", "choose_sticker"):
                        await self.tg.send_sticker(paths, text or None)
                    sent_sticker = True
            elif channel == "discord":
                if voice:
                    notes.append("Discord 那边的语音条这次没做，发的是文字。")
                await self.dc.send_dm(text, _sticker_upload(paths))
                sent_sticker = bool(sticker)
            elif channel == "discord_group":
                if voice:
                    notes.append("群里发的是文字。")
                await self.dc.send_group(text, _sticker_upload(paths))
                sent_sticker = bool(sticker)
            elif channel == "discord_whisper":
                if voice:
                    notes.append("悄悄话里发的是文字。")
                await self.dc.send_whisper(text, _sticker_upload(paths))
                sent_sticker = bool(sticker)
            else:
                return False, "channel 只能是 telegram / discord / discord_group / discord_whisper。"
        except Exception as exc:                  # noqa: BLE001 - 平台没连上/被拒：把原因交回给她
            log("send_failed", channel=channel, error=repr(exc)[:200], sticker=sticker or None)
            return False, "没发出去：%s" % str(exc)[:200]
        if sent_sticker:
            notes.append("表情包 %s 发出去了" % sticker)
        # 只把她「见过的」算作回了（notified）；她这一轮跑的时候才到的 unread 一律不动，
        # 否则第二条会被悄悄吃掉（2026-11-02，间隔很短的连发丢消息）。
        seen = [i for i in pending if i.get("state") == "notified"]
        answered = 0
        if seen:
            answered = inbox.mark_answered([i["id"] for i in seen])
        self._replied_at[channel] = time.time()          # 同一轮接下来的几条算「还在回他」
        # 发这条的时候又来了新消息：附在回话里给她看，她可以同一轮再回一条
        fresh = [i for i in inbox.pending(channel) if i.get("state") == "unread"]
        live = ""
        if fresh:
            inbox.mark_notified([i["id"] for i in fresh])
            try:
                owner_names = self.dc.owner_name_set()
            except Exception:                      # noqa: BLE001 - 假 dc / 没连上：用默认显示
                owner_names = ()
            live = live_note(channel, fresh, owner_names)
        if proactive and channel not in ("discord_group", "discord_whisper"):
            wake.note_spoken()
        log("sent", channel=channel, voice=voiced, answered=answered, proactive=proactive,
            chars=len(text), sticker=sticker or None)
        result = send_result_text(channel, voiced=voiced, notes=notes,
                                  answered=answered, proactive=proactive)
        if live:
            result = result + "\n\n" + live
        return True, result


# ── 本地 HTTP（她/测试用）──────────────────────────────────────────────────
async def handle_send(service, request):
    from aiohttp import web
    cfg = service.cfg
    if cfg["api_key"]:
        got = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        if got != cfg["api_key"]:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:                             # noqa: BLE001
        return web.json_response({"ok": False, "error": "body 不是 JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "body 不是对象"}, status=400)
    channel = str(body.get("channel") or "").strip()
    text = str(body.get("text") or "").strip()
    sticker = str(body.get("sticker") or "").strip()
    if channel not in CHANNELS:
        return web.json_response({"ok": False, "error": "channel 只能是 telegram / discord / discord_group / discord_whisper"}, status=400)
    if not text and not sticker:
        return web.json_response({"ok": False, "error": "text 是空的"}, status=400)
    ok, detail = await service.send(channel, text, voice=bool(body.get("voice")), sticker=sticker)
    return web.json_response({"ok": ok, "text": detail} if ok else {"ok": False, "error": detail})


async def handle_health(service, request):
    from aiohttp import web
    return web.json_response({
        "ok": True,
        "bridge": "chat",
        "tg": "polling" if service.tg.polling else "off",
        "discord": "ready" if service.dc.ready else ("starting" if service.dc.client else "off"),
        "whisper": service.dc.whisper_channel_id() or "off",
        "voice": service.cfg["tts_voice"],
        "unread": len(inbox.pending()),
    })


async def handle_group_budget(service, request):
    """给可畏（Hermes）那边用的额度查询：GET /group_budget，Bearer 和 /send 一样。"""
    from aiohttp import web
    cfg = service.cfg
    if cfg["api_key"]:
        got = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        if got != cfg["api_key"]:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    budget = await service.refresh_budget()
    if budget is None:
        return web.json_response({"ok": False, "error": "budget unavailable"}, status=503)
    out = dict(budget)
    out["ok"] = True
    out["note"] = budget_note(budget)
    return web.json_response(out)


async def handle_whisper_budget(service, request):
    """给可畏（Hermes）那边用的悄悄话额度查询：GET /whisper_budget，Bearer 和 /send 一样。"""
    from aiohttp import web
    cfg = service.cfg
    if cfg["api_key"]:
        got = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        if got != cfg["api_key"]:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if not (service.dc.whisper_channel_id() if hasattr(service.dc, "whisper_channel_id") else ""):
        return web.json_response({"ok": False, "error": "whisper channel unavailable"}, status=503)
    budget = await service.refresh_whisper_budget()
    if budget is None:
        return web.json_response({"ok": False, "error": "budget unavailable"}, status=503)
    return web.json_response(whisper_budget_payload(budget))


async def serve(cfg):
    from aiohttp import web
    service = Service(cfg)
    app = web.Application()
    app.router.add_get("/health", lambda request: handle_health(service, request))
    app.router.add_get("/group_budget", lambda request: handle_group_budget(service, request))
    app.router.add_get("/whisper_budget", lambda request: handle_whisper_budget(service, request))
    app.router.add_post("/send", lambda request: handle_send(service, request))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg["chat_port"])
    await site.start()
    if not cfg["api_key"]:
        log("warn", reason="API_SERVER_KEY 没配：8660 的 /send 不鉴权（只在回环上）")
    log("bridge_up", port=cfg["chat_port"], brain=cfg["brain_url"],
        tg=bool(cfg["tg_token"] and cfg["tg_owner"]),
        discord=bool(cfg["dc_token"] and cfg["dc_owner"]), voice=cfg["tts_voice"])
    await service.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await service.close()
        await runner.cleanup()


# ── 自检（不联网）──────────────────────────────────────────────────────────
def _selftest():
    import shutil
    import tempfile
    global _LOG
    _LOG = {"path": ""}
    old = os.environ.get("ALBION_STATE_DIR")
    os.environ["ALBION_STATE_DIR"] = tempfile.mkdtemp(prefix="chat-bridge-test-")
    try:
        # 配置：环境变量优先，agent/.env 只补缺的
        home = tempfile.mkdtemp(prefix="chat-home-")
        with open(os.path.join(home, ".env"), "w", encoding="utf-8") as f:
            f.write("ALBION_TG_BOT_TOKEN=tg-token\nALBION_TG_OWNER_ID=42\n")
        os.makedirs(os.path.join(home, "agent"))
        with open(os.path.join(home, "agent", ".env"), "w", encoding="utf-8") as f:
            f.write("API_SERVER_KEY=k1\n")
        cfg = load_config(environ={"ALBION_TG_OWNER_ID": "77"}, home=home)
        assert cfg["tg_owner"] == "77", "环境变量优先"
        assert cfg["tg_token"] == "tg-token" and cfg["api_key"] == "k1", cfg
        assert cfg["dc_token"] == "" and cfg["chat_port"] == 8660
        shutil.rmtree(home, ignore_errors=True)
        # 只认他
        assert is_owner(42, "42") and is_owner("42", "42") and not is_owner(43, "42") and not is_owner(42, "")
        # 群里每条消息都叫醒（逐条限制已去掉），来源只用来区分他 / bot / 别人
        assert group_should_wake(42, "42", False) == "owner"
        assert group_should_wake(9, "42", True) == "bot"
        assert group_should_wake(9, "42", False) == "member"
        # 合批（不再有同频道冷却）
        items = [{"id": 1, "at": 100.0}]
        assert group_wait_seconds(items, 101.0) == 9.0
        assert group_wait_seconds(items, 200.0) == 0.0
        assert group_wait_seconds([], 200.0) == 0.0
        # 主动 vs 回复
        assert proactive_gate("telegram", 1, (False, "还没到节奏")) == (True, "")
        assert proactive_gate("telegram", 0, (True, "")) == (True, "")
        ok, why = proactive_gate("telegram", 0, (False, "按我给自己定的节奏，22:00 以后再找他"))
        assert not ok and "22:00" in why
        assert proactive_gate("discord_group", 0, None, quiet=True)[0] is False
        assert proactive_gate("discord_group", 0, None, quiet=False)[0] is True
        # 通知轮文字（定稿原文）
        wake.save({"last_user_at": 0.0, "last_wake_at": 0.0, "spoken_at": [], "unanswered": 0,
                   "rung_at": 0.0, "rounds": []})
        a = inbox.add("telegram", "在忙吗", at=1762000000.0)
        notice = dm_notice("telegram", [a])
        assert notice.startswith(PLATFORM_MARKER + "指挥官在Telegram上给我发了消息"), notice
        assert "#1 " in notice and "在忙吗" in notice
        assert "channel=telegram" in notice and "这一轮我在这里写的字他看不到" in notice
        b = inbox.add("discord_group", "晚上好", at=1762000000.0, author="机器人甲", from_="bot")
        gnotice = group_notice("albion", [b])
        assert gnotice.startswith(PLATFORM_MARKER + "Discord 群「albion」里有新消息"), gnotice
        assert "机器人甲：晚上好" in gnotice and "channel=discord_group" in gnotice
        c = inbox.add("discord_group", "在吗", at=1762000000.0, author="指挥官")
        gnotice2 = group_notice("albion", [c], [b])
        assert "（刚才群里还在聊的：）" in gnotice2 and "机器人甲：晚上好" in gnotice2, gnotice2
        assert "指挥官：在吗" in gnotice2
        # clean_content 优先（她看到「@可畏」而不是「<@id>」）、@名字→真 mention（2026-11-02）
        class _Msg:
            clean_content = "@可畏 在吗 <@123>"
            content = "@可畏 在吗 <@1566802611611504680>"
        assert message_text(_Msg()).startswith("@可畏")
        idx = build_name_index([("111", ["Formidable"]), ("222", ["Albion"])])
        assert replace_mentions("@formidable 你看", idx) == "<@111> 你看"
        assert replace_mentions("@Albion 和 @Formidable", idx) == "<@222> 和 <@111>"
        assert replace_mentions("@Nobody 你看", idx) == "@Nobody 你看", "对不上的人不替换"
        assert replace_mentions("@AlbionBot", idx) == "@AlbionBot", "只有精确整名才替换"
        amb = build_name_index([("111", ["Same"]), ("222", ["same"])])
        assert replace_mentions("@Same", amb) == "@Same", "重名有歧义就不替换"
        assert GROUP_PEER_NOTE == "可畏也在群里，群里说的话她都看得到。"
        # 额度提醒文案（原文照定稿）
        assert budget_note({"remaining": 0, "used": 9}) == GROUP_BUDGET_EMPTY
        assert budget_note({"remaining": 3, "used": 9}) == GROUP_BUDGET_LOW % (9, 3)
        assert budget_note({"remaining": 4, "used": 8}) == ""
        assert budget_note(None) == ""
        # 叫醒只由 unread 驱动：pending 里 notified 的不算新消息
        inbox.mark_notified([a["id"], c["id"]])
        assert Service._unread() == [], "notified 的不再驱动叫醒"
        assert Service._unread("discord_group") == []
        # 悄悄话（2026-11-02）：通知文字照定稿、说话人「指挥官 / 可畏」、额度回话带 note
        wowner = type("A", (), {"id": "42", "bot": False, "nick": None,
                                "display_name": "OwnerNick", "name": "owner"})()
        wpeer = type("A", (), {"id": group_budget.DEFAULT_BOT_IDS[1], "bot": True, "nick": None,
                               "display_name": "Formidable", "name": "Formidable"})()
        assert whisper_author(wowner, "42") == OWNER_LABEL
        assert whisper_author(wpeer, "42", (), (group_budget.DEFAULT_BOT_IDS[1],)) == WHISPER_PEER_LABEL
        w1 = inbox.add("discord_whisper", "在吗", at=1762000000.0, author=OWNER_LABEL)
        w2 = inbox.add("discord_whisper", "在的", at=1762000060.0, author="Formidable",
                       from_="bot", why="whisper")
        wnotice = whisper_notice([w1, w2])
        assert wnotice.splitlines()[0] == PLATFORM_MARKER + WHISPER_HEAD
        assert wnotice.splitlines()[-1] == WHISPER_TAIL
        assert "指挥官：在吗" in wnotice and "可畏：在的" in wnotice
        zero = {"remaining": 0, "session_used": 20, "day_used": 20, "limit": "session"}
        assert wnotice.endswith(WHISPER_TAIL)
        assert whisper_notice([w1], zero).endswith(whisper_budget.note(zero))
        payload = whisper_budget_payload(zero)
        assert payload["ok"] is True and payload["note"] == whisper_budget.SESSION_EMPTY
        # 结果文字
        assert send_result_text("telegram", voiced=True) == "已发到Telegram，语音条也发了（caption 是我的中文原文）。"
        assert "语音没合成出来，只发了文字。" in send_result_text("telegram", notes=["语音没合成出来，只发了文字。"])
        assert "（2 条没回的消息算回了）" in send_result_text("telegram", answered=2)
        # 语音 caption：Irodori 带中文原文，GSV 不带
        assert voice_caption({"tts_voice": "albion"}, "早上好") == "早上好"
        assert voice_caption({"tts_voice": "denia"}, "早上好") is None
        print("CHAT BRIDGE SELFTEST OK")
    finally:
        shutil.rmtree(os.environ["ALBION_STATE_DIR"], ignore_errors=True)
        if old is None:
            os.environ.pop("ALBION_STATE_DIR", None)
        else:
            os.environ["ALBION_STATE_DIR"] = old


def main(argv=None):
    parser = argparse.ArgumentParser(description="阿尔比恩平台桥（TG + Discord）")
    parser.add_argument("--selftest", action="store_true", help="不联网的确定性自检")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    cfg = load_config()
    _LOG["path"] = cfg["log"]
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
