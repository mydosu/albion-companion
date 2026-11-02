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
  API_SERVER_KEY（8660 鉴权 + 调宿主）
  ALBION_CHAT_TTS_VOICE（默认 albion=Irodori 日语，带中文 caption；denia=GSV 中文，不带 caption）
缺 Discord 的 token/owner id 就只跑 TG，日志里说明。

定稿（主脑 11-02）：
  · 「决定不回」不加动作：通知轮没回就留箱，下一次醒来轮念给她，那轮结束后统统了结（wake.after_wake）；
  · 群唤醒：他发言或 @她 → 10 秒合批；别的 bot 只入箱当上下文；同一频道 120 秒最多一次；
  · 主动找他要守节奏（wake.can_speak），回复不受限；群里主动发言只受夜间安静限制；
  · 桌面忙时同一个会话串行等锁，超时把这批退回 unread。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

BIN = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(BIN)
if BIN not in sys.path:                       # 桥和宿主不在同一个进程，自己把 bin 放进 sys.path
    sys.path.insert(0, BIN)

import inbox                                   # noqa: E402
import wake                                     # noqa: E402

PLATFORM_MARKER = wake.PLATFORM_MARKER
SESSION_DM = "albion-desktop"                  # TG 私聊 / Discord 私信：和桌面同一个会话
SESSION_GROUP = "albion-discord"               # Discord 群：自己的真会话（长期记忆共享）
GROUP_BATCH_S = 10.0                           # 群里合批窗口
GROUP_COOLDOWN_S = 120.0                       # 同一频道最多这么久叫醒一次
TTS_URL = "http://127.0.0.1:9881/v1/audio/speech"
CHANNELS = ("telegram", "discord", "discord_group")

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
        "tts_url": get("ALBION_CHAT_TTS_URL", TTS_URL).strip(),
        "tts_voice": (get("ALBION_CHAT_TTS_VOICE", "albion").strip() or "albion"),
        "proxy": get("ALBION_CHAT_PROXY", "http://127.0.0.1:7897").strip(),
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
    print(line, flush=True)


# ── 纯判定与文字（自检和真跑共用）──────────────────────────────────────────
def is_owner(user_id, owner_id):
    """只认他：id 为空一律不认（fail-closed）。"""
    return bool(owner_id) and str(user_id) == str(owner_id)


def group_should_wake(author_id, owner_id, mentions_me):
    """群里这条消息值不值得叫醒她：他发言 / 有人 @她。别的 bot 只当上下文。"""
    if is_owner(author_id, owner_id):
        return "owner"
    if mentions_me:
        return "mention"
    return ""


def _stamp(ts):
    from datetime import datetime
    return datetime.fromtimestamp(float(ts or 0), wake.TZ).strftime("%m-%d %H:%M")


def _hhmm(ts):
    from datetime import datetime
    return datetime.fromtimestamp(float(ts or 0), wake.TZ).strftime("%H:%M")


def dm_notice(channel, items):
    """TG / Discord 私信的通知轮（主脑定稿的文字，照抄）。"""
    where = {"telegram": "Telegram", "discord": "Discord 私信"}.get(channel, inbox.channel_label(channel))
    lines = [PLATFORM_MARKER + "指挥官在%s上给我发了消息（写给自己看的，不用念）：" % where]
    for item in items:
        lines.append("#%s %s %s" % (item["id"], _stamp(item.get("at")), item.get("text")))
    lines.append("想回就用 send_message 回他（channel=%s）；这一轮我在这里写的字他看不到。也可以先不回，下次醒来还会看到。"
                 % channel)
    return "\n".join(lines)


def group_notice(channel_name, items, context=None):
    lines = [PLATFORM_MARKER + "Discord 群「%s」里有新消息（群里大家都看得到；我和指挥官之间的私事、当面说过的话不发这儿）："
             % (channel_name or "群")]
    if context:
        # 群里刚才还在聊的（别人的话）：只给她看个上下文，不改状态、不驱动叫醒
        lines.append("（刚才群里还在聊的：）")
        for item in context:
            lines.append("#%s %s %s：%s" % (item["id"], _hhmm(item.get("at")),
                                           item.get("author") or "有人", item.get("text")))
    for item in items:
        lines.append("#%s %s %s：%s" % (item["id"], _hhmm(item.get("at")),
                                       item.get("author") or "有人", item.get("text")))
    lines.append("想说话就用 send_message（channel=discord_group）；这里写的字群里看不到。")
    return "\n".join(lines)


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


def group_wait_seconds(items, now, last_notify_at, batch_s=GROUP_BATCH_S, cooldown_s=GROUP_COOLDOWN_S):
    """群里还要等多久才叫醒：合批窗口 + 同频道冷却，取更晚的那个。"""
    if not items:
        return 0.0
    first = min(float(i.get("at") or 0) for i in items)
    # 0.0 是「还没叫醒过」的哨兵：第一次不该等冷却
    cooldown_left = (float(last_notify_at or 0) + cooldown_s - now) if last_notify_at else 0.0
    return max(0.0, first + batch_s - now, cooldown_left)


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

    async def close(self):
        self.polling = False
        if self.task:
            self.task.cancel()
        if self.http:
            await self.http.close()


# ── Discord ────────────────────────────────────────────────────────────────
class DiscordBot:
    def __init__(self, cfg, on_message):
        self.cfg = cfg
        self.on_message = on_message
        self.client = None
        self.task = None
        self.ready = False

    @property
    def configured(self):
        return bool(self.cfg["dc_token"] and self.cfg["dc_owner"])

    def channel_name(self):
        if not self.client or not self.cfg["dc_channel"]:
            return ""
        channel = self.client.get_channel(int(self.cfg["dc_channel"]))
        return getattr(channel, "name", "") or ""

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
            log("discord_ready", user=str(self.client.user), guilds=len(self.client.guilds))

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

    async def send_dm(self, text):
        if not self.client or not self.ready:
            raise RuntimeError("discord not ready")
        user = self.client.get_user(int(self.cfg["dc_owner"]))
        if user is None:
            user = await self.client.fetch_user(int(self.cfg["dc_owner"]))
        channel = user.dm_channel or await user.create_dm()
        msg = await channel.send(text)
        return msg.id

    async def send_group(self, text):
        if not self.client or not self.ready:
            raise RuntimeError("discord not ready")
        if not self.cfg["dc_channel"]:
            raise RuntimeError("ALBION_DISCORD_CHANNEL_ID 没配")
        channel = self.client.get_channel(int(self.cfg["dc_channel"]))
        if channel is None:
            raise RuntimeError("群频道 %s 找不到" % self.cfg["dc_channel"])
        msg = await channel.send(text)
        return msg.id

    async def close(self):
        if self.client and not self.client.is_closed():
            await self.client.close()
        if self.task:
            self.task.cancel()


# ── 桥本体 ─────────────────────────────────────────────────────────────────
class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.host = Host(cfg)
        self.tg = Telegram(cfg, self.on_tg_update)
        self.dc = DiscordBot(cfg, self.on_discord_message)
        self._dm_worker = None
        self._group_worker = None
        self._last_group_notify = 0.0

    async def start(self):
        await self.tg.start()
        await self.dc.start()

    async def close(self):
        for worker in (self._dm_worker, self._group_worker):
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
        import discord
        if self.dc.client and message.author.id == self.dc.client.user.id:
            return
        author = getattr(message.author, "display_name", "") or str(message.author)
        text = (message.content or "").strip()
        if isinstance(message.channel, discord.DMChannel):
            if not is_owner(message.author.id, self.cfg["dc_owner"]):
                log("discord_ignored", reason="not owner", kind="dm")
                return
            item = inbox.add("discord", text or "（空消息）", platform_msg_id=str(message.id), author=author)
            if item is not None:
                log("inbox_add", channel="discord", id=item["id"])
                self.schedule_dm()
            return
        if not self.cfg["dc_channel"] or str(message.channel.id) != self.cfg["dc_channel"]:
            return
        mentioned = bool(self.dc.client and self.dc.client.user in message.mentions)
        reason = group_should_wake(message.author.id, self.cfg["dc_owner"], mentioned)
        if reason:
            item = inbox.add("discord_group", text or "（没有文字）", platform_msg_id=str(message.id),
                             author=author, why=reason)
            if item is not None:
                log("inbox_add", channel="discord_group", id=item["id"], why=reason, author=author)
                self.schedule_group()
        else:
            # 别的 bot / 别人：只入箱当上下文，不叫醒她
            item = inbox.add("discord_group", text or "（没有文字）",
                             from_="bot" if message.author.bot else "member",
                             platform_msg_id=str(message.id), author=author)
            if item is not None:
                log("inbox_context", channel="discord_group", id=item["id"], author=author)

    # ── 叫醒 ──
    def schedule_dm(self):
        if self._dm_worker is None or self._dm_worker.done():
            self._dm_worker = asyncio.create_task(self._drain_dm())

    def schedule_group(self):
        if self._group_worker is None or self._group_worker.done():
            self._group_worker = asyncio.create_task(self._drain_group())

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
            for channel in ("telegram", "discord"):
                batch = [i for i in items if i["channel"] == channel]
                if not batch:
                    continue
                ok = await notify(self.cfg, self.host, SESSION_DM, dm_notice(channel, batch),
                                  [i["id"] for i in batch])
                if not ok:
                    return                        # 宿主忙/挂了：留到下一次醒来，不在这里死等
            await asyncio.sleep(0)

    async def _drain_group(self):
        while True:
            items = self._unread("discord_group")
            if not items:
                return
            wait = group_wait_seconds(items, time.time(), self._last_group_notify)
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            context = inbox.context_items("discord_group", since_s=600, limit=10)
            ok = await notify(self.cfg, self.host, SESSION_GROUP,
                              group_notice(self.dc.channel_name(), items, context), [i["id"] for i in items])
            self._last_group_notify = time.time()
            if not ok:
                return

    # ── 她发消息 ──
    async def send(self, channel, text, voice=False):
        """返回 (ok, 文字/原因)。"""
        pending = inbox.pending(channel)
        proactive = not pending
        if proactive:
            ok, why = proactive_gate(channel, 0)
            if not ok:
                return False, "现在不该主动找他：%s。" % why
        notes = []
        voiced = False
        try:
            if channel == "telegram":
                ogg = await synth_ogg(self.cfg, text) if voice else None
                if voice and ogg is None:
                    notes.append("语音没合成出来，只发了文字。")
                if ogg:
                    await self.tg.send_voice(ogg, voice_caption(self.cfg, text))
                    voiced = True
                else:
                    await self.tg.send_text(text)
            elif channel == "discord":
                if voice:
                    notes.append("Discord 那边的语音条这次没做，发的是文字。")
                await self.dc.send_dm(text)
            elif channel == "discord_group":
                if voice:
                    notes.append("群里发的是文字。")
                await self.dc.send_group(text)
            else:
                return False, "channel 只能是 telegram / discord / discord_group。"
        except Exception as exc:                  # noqa: BLE001 - 平台没连上/被拒：把原因交回给她
            log("send_failed", channel=channel, error=repr(exc)[:200])
            return False, "没发出去：%s" % str(exc)[:200]
        answered = 0
        if pending:
            answered = inbox.mark_answered([i["id"] for i in pending])
        if proactive and channel != "discord_group":
            wake.note_spoken()
        log("sent", channel=channel, voice=voiced, answered=answered, proactive=proactive, chars=len(text))
        return True, send_result_text(channel, voiced=voiced, notes=notes,
                                      answered=answered, proactive=proactive)


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
    if channel not in CHANNELS:
        return web.json_response({"ok": False, "error": "channel 只能是 telegram / discord / discord_group"}, status=400)
    if not text:
        return web.json_response({"ok": False, "error": "text 是空的"}, status=400)
    ok, detail = await service.send(channel, text, voice=bool(body.get("voice")))
    return web.json_response({"ok": ok, "text": detail} if ok else {"ok": False, "error": detail})


async def handle_health(service, request):
    from aiohttp import web
    return web.json_response({
        "ok": True,
        "bridge": "chat",
        "tg": "polling" if service.tg.polling else "off",
        "discord": "ready" if service.dc.ready else ("starting" if service.dc.client else "off"),
        "voice": service.cfg["tts_voice"],
        "unread": len(inbox.pending()),
    })


async def serve(cfg):
    from aiohttp import web
    service = Service(cfg)
    app = web.Application()
    app.router.add_get("/health", lambda request: handle_health(service, request))
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
        # 群里叫醒条件
        assert group_should_wake(42, "42", False) == "owner"
        assert group_should_wake(9, "42", True) == "mention"
        assert group_should_wake(9, "42", False) == ""
        # 合批与冷却
        items = [{"id": 1, "at": 100.0}]
        assert group_wait_seconds(items, 101.0, 0.0) == 9.0
        assert group_wait_seconds(items, 200.0, 0.0) == 0.0
        assert group_wait_seconds(items, 200.0, 150.0) == 70.0, "同一频道 120 秒最多一次"
        assert group_wait_seconds([], 200.0, 150.0) == 0.0
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
        # 叫醒只由 unread 驱动：pending 里 notified 的不算新消息
        inbox.mark_notified([a["id"], c["id"]])
        assert Service._unread() == [], "notified 的不再驱动叫醒"
        assert Service._unread("discord_group") == []
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
