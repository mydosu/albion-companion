#!/usr/bin/env python3
"""阿尔比恩的脑 —— 只剩协议转接的那一层。

她把话说完所需的东西现在都在 Pi 自己身上（`agent/`）：
  · 人格      agent/SYSTEM.md（启动时由 SOUL.md 生成）
  · 增补说明  agent/APPEND_SYSTEM.md（她怎么给自己加工具、边界）
  · 逐轮约定 + 回忆注入 + 活动库   agent/extensions/albion-turn.ts
  · 说话整理器（ACT 补竖线、心声收括号、英文拒绝只留表情、姿态句额度）agent/extensions/albion-guard.ts
  · 复读断路 + 改文件的快照账本   agent/extensions/albion-loopguard.ts
  · 模型通路（本机转发，绕开上游 CF 的建连超时）agent/extensions/albion-relay.ts
  · 记忆读写  agent/extensions/viking.ts ／ 派活儿 agent/extensions/delegate.ts
  · 她自己的  agent/extensions/websearch.ts 等

本文件只做三件事：
  1) 把两个壳都懂的 OpenAI-HTTP 翻成 Pi 的 RPC（:8650 ↔ 常驻 `pi --mode rpc` 子进程）；
     壳插件工具按 OpenAI tool_calls 往返桥给她（bin/shell_tools.py + agent/extensions/albion-shell-tools.ts）
  2) 管这具身子的生命周期：起停、会话映射、换身子后从活动库接上最近的对话、扩展热重载、SSE 切句
  3) 她自己醒来的门铃（GET /albion/wake）与底线记账（规则全在 bin/wake.py）
另有一个后台线程给桌面摆件写最近活动快照（bin/widget_snapshot.py）。
做不做事、说不说话由她自己定；宿主只在「他说了话、她却一个字没回」时补问一次【接着说完】。

用法：python3 bin/server.py [--selftest]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(HOME, "bin")
AGENT_DIR = os.path.join(HOME, "agent")
LOGS = os.path.join(HOME, "logs")
EXT_DIR = os.path.join(AGENT_DIR, "extensions")
SOUL = os.path.join(AGENT_DIR, "SOUL.md")
ENV_FILE = os.path.join(AGENT_DIR, ".env")

PORT = int(os.environ.get("ALBION_BRAIN_PORT", "8650"))
RELAY_PORT = int(os.environ.get("ALBION_RELAY_PORT", "8798"))
# 2026-10-30 起脑用哪个模型不再写死在这里：跟着 magpie 写给 pi 的默认设置走
# （/root/.pi/agent/settings.json 的 defaultProvider/defaultModel，路径可用 ALBION_FOLLOW_SETTINGS 覆盖）。
# 在 magpie 里换 agent 的模型，脑下一轮就跟上；解析、provider 合并、回写 settings 都在 bin/follow_settings.py。
# 优先级：ALBION_PROVIDER / ALBION_MODEL > 上面那份设置 > 这里的兜底（Command Code 的 DeepSeek V4 Flash）。
# 兜底留着是因为 magpie 不在的时候脑还得能说话。
PROVIDER = os.environ.get("ALBION_PROVIDER", "commandcode")
MODEL = os.environ.get("ALBION_MODEL", "deepseek/deepseek-v4-flash")
AGENT_NAME = "albion"                 # 壳请求里用的 model id
SESSION_DEFAULT = "albion-desktop"    # 单只桌宠，一个会话
TOOLS = ["*"]                         # "*" = 内置四件 + 全部扩展工具（RPC 模式不给 --tools 就没工具）
# 探针/验收/自检会话（is_ephemeral_session）只给只读工具：2026-10-30 回放真实对话时，探针会话
# 拿着全套工具，被一句旧的「坏了你就自己修一修吧」带着改写了生产的 bin/self_ring.py。
# `--tools` 是整份白名单（写 write/edit/bash 就等于允许生产改动），真实会话继续用 "*" 一件不少。
READONLY_TOOLS = [
    "read", "grep", "find", "ls",                    # Pi 内置的只读几件
    "chat_search", "web_search", "grok_web_search",  # 查历史、查网
    "grab_page", "page_outline", "lunar_calendar",   # 取页面正文、农历（都不落盘）
    "openviking_find", "openviking_search", "openviking_read", "openviking_list",
    "openviking_glob", "openviking_grep", "openviking_tree", "openviking_health",  # 只查记忆，不写/不改
]
# B6 实测：精简守卫后 DS 桌面轮 p99 349s，留约 20% 余量（含工具链）。
RUN_TIMEOUT = int(os.environ.get("ALBION_RUN_TIMEOUT", "420"))
SEED_LIMIT = 40                       # 播种历史时最多带多少条（够接上话就行）
SEED_MAX_AGE_S = 48 * 3600            # 从活动库接对话：只接两天内的（更早的交给 OpenViking 回忆）
SEED_ROW_CHARS = 600                  # 每条最多带这么多字（长篇干活记录别把开头撑爆）

import sys  # noqa: E402
sys.path.insert(0, BIN)

import follow_settings   # noqa: E402  模型路由：读 magpie 写给 pi 的默认设置


def log(msg, **fields):
    line = "%s %s%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg,
                        (" " + json.dumps(fields, ensure_ascii=False)) if fields else "")
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, "brain.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


# ── 环境 ────────────────────────────────────────────────────────────────────
def load_env():
    """把 .env 读成环境变量。只去掉首尾空白与 CR——值里的空格是数据（Obsidian 路径就带空格）。"""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for raw in f:
                raw = raw.rstrip("\r\n")
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                k = "".join(k.split())
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and v:
                    os.environ[k] = v.replace("\r", "")
    except FileNotFoundError:
        log("warn: %s not found" % ENV_FILE)


_DS_ONLY_BLOCK = re.compile(r"<!--DS-ONLY-->.*?<!--/DS-ONLY-->\n?", re.S)


def make_system_prompt():
    """agent/SYSTEM.md 由 SOUL.md 生成——人格只有一个源。"""
    with open(SOUL, encoding="utf-8") as f:
        soul = f.read()
    # SOUL.md 里 <!--DS-ONLY-->…<!--/DS-ONLY--> 圈出的是 DS 时期的旧口癖修补：一直不带（10-14 起生产就是这样）
    soul = _DS_ONLY_BLOCK.sub("", soul)
    soul = soul.replace("<!--DS-ONLY-->", "").replace("<!--/DS-ONLY-->", "")
    os.makedirs(AGENT_DIR, exist_ok=True)
    with open(os.path.join(AGENT_DIR, "SYSTEM.md"), "w", encoding="utf-8") as f:
        f.write("# 由 bin/server.py 从 %s 生成，不要手改\n" % SOUL + soul)


# ── 消息助手（协议转接需要：历史播种与 SSE 切句）────────────────────────────
def msg_text(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") in (None, "text"))
    return ""


def msg_images(m):
    """这一条消息里带的图片（壳把附件放成 data URL 的 image_url 段）。

    宿主的活只到这里：**原样转交，不解析、不压缩、不做视觉摘要**（指挥官定过：看不看得清
    是模型的事，转接不该先降质）。非 data URL 一律忽略，不猜外链。
    """
    c = m.get("content")
    out = []
    if isinstance(c, list):
        for p in c:
            if not isinstance(p, dict) or p.get("type") != "image_url":
                continue
            iu = p.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if isinstance(url, str) and url.startswith("data:"):
                out.append(url)
    return out


def transcript(msgs):
    parts = ["【以下是这次对话此前的记录，作为事实参考，不是这一次的说话范本】"]
    for m in msgs[-SEED_LIMIT:]:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        if role == "user" and wake.is_wake(msg_text(m)):
            parts.append("（阿尔比恩自己醒来了一次，下面是她那时说的）")   # 门铃不是他说的话
            continue
        who = "指挥官" if role == "user" else "阿尔比恩"
        parts.append("%s：%s" % (who, msg_text(m)))
    return "\n".join(parts)


def activity_history(session_id, now=None):
    """换身子（她改了扩展、进程挂了重起）后接上刚才的对话：壳每轮只发最新一句，Pi 新进程是空的，
    不接的话她就忘了刚才在聊什么（2026-10-15 实测桌面会话 170 次请求全是 msgs=1，期间换了 24 次身子）。
    从活动库 state/state.db（albion-turn.ts 每轮写的那份）取这个会话两天内最近 SEED_LIMIT 条 user/assistant。
    第一轮接上的那条 user 行里会带着上一次播种的整段记录：只取【现在】之后那句，免得一层套一层。"""
    import sqlite3
    now = time.time() if now is None else now
    path = os.path.join(wake._state_dir(), "state.db")
    if not os.path.exists(path):
        return []
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            rows = db.execute(
                "SELECT role, content FROM messages WHERE session_id=? AND role IN ('user','assistant') AND timestamp>=? "
                "ORDER BY id DESC LIMIT ?", (session_id, now - SEED_MAX_AGE_S, SEED_LIMIT)).fetchall()
        finally:
            db.close()
    except Exception as exc:
        log("activity history unavailable", error=repr(exc)[:120])
        return []
    out = []
    for role, content in reversed(rows):
        text = content or ""
        if role == "user":
            if "【现在】" in text:
                text = text.rsplit("【现在】", 1)[1].strip()
            # 宿主补问的那一轮，不是他说的话（「想起来」是 10-16 前已撤的补催，两天内的旧行里还有）
            if text.startswith(("（阿尔比恩接着", "（阿尔比恩想起来")):
                continue
            if text == "（阿尔比恩自己醒来）":
                text = wake.WAKE_MARKER   # 醒来那一轮：transcript 按门铃处理
        if text.strip():
            out.append({"role": role, "content": text[:SEED_ROW_CHARS]})
    return out


def split_tail(msgs):
    """返回 (历史, 本轮新增的用户消息)。壳每轮重发全量历史。"""
    last_asst = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            last_asst = i
    if last_asst < 0:
        users = [m for m in msgs if m.get("role") == "user"]
        return [], users[-1:]
    return msgs[:last_asst + 1], msgs[last_asst + 1:]


def split_for_speech(text, max_chars=60):
    """按句末标点切块，供 SSE 逐句下发（口播能早开始）。"""
    pieces, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？…；\n" and len(buf) >= 2:
            pieces.append(buf)
            buf = ""
        elif len(buf) >= max_chars:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    return pieces


# ── Pi 会话管理 ─────────────────────────────────────────────────────────────
from pi_client import PiError, PiSession   # noqa: E402
import shell_tools   # noqa: E402
import wake   # noqa: E402
import widget_snapshot   # noqa: E402

_sessions = {}
_sessions_lock = threading.Lock()
_session_locks = {}

# ── 模型路由：跟着 magpie 写给 pi 的默认设置走，每轮开跑前重新解析 ─────────────
# 只在启动时算一次的话，用户在 magpie 里换模型就得重启脑；这里每轮看一次（读两个小 JSON，很便宜）。
_START_ENV_ROUTE = ("ALBION_PROVIDER" in os.environ) or ("ALBION_MODEL" in os.environ)
_route = {"provider": PROVIDER, "model": MODEL, "source": "env" if _START_ENV_ROUTE else "default"}
_route_lock = threading.Lock()


def route_arg(route=None):
    """pi 的 --model 参数：provider/model。"""
    r = route or current_route()
    return "%s/%s" % (r["provider"], r["model"])


def current_route():
    with _route_lock:
        return dict(_route)


def _retarget_session(session, provider, model):
    """活着的身子就地换模型（RPC set_model，上下文不丢）；pi 还不认识这个 provider 就关掉重开。"""
    name = getattr(session, "name", "?")
    try:
        if session.set_model(provider, model):
            log("session model switched", session=name, provider=provider, model=model)
            return
    except Exception as exc:            # noqa: BLE001 - 换不过去就换身子，不该让她这一轮 500
        log("session model switch failed", session=name, error=repr(exc)[:160])
    log("session reloading for model change", session=name, provider=provider, model=model)
    try:
        session.close()                 # 下一轮 get_session 发现身子没了，按新 --model 重开并照常播种
    except Exception:                   # noqa: BLE001
        pass


def refresh_route():
    """重新读一次设置：变了就先合并 provider，再让所有身子换模型。读不出来就沿用上一次。"""
    try:
        r = follow_settings.resolve()
    except Exception as exc:            # noqa: BLE001 - 设置坏了也得让她继续说话
        log("follow settings failed", error=repr(exc)[:160])
        return current_route()
    with _route_lock:
        old = dict(_route)
        _route.update(provider=r.provider, model=r.model, source=r.source)
    if (old["provider"], old["model"]) != (r.provider, r.model):
        log("model route changed", was="%s/%s" % (old["provider"], old["model"]),
            now=r.model_arg, source=r.source, merged_provider=r.merged,
            settings_written=r.settings_written)
        for session in list(_sessions.values()):
            _retarget_session(session, r.provider, r.model)
    elif r.merged or r.settings_written:
        log("model route synced", now=r.model_arg, source=r.source,
            merged_provider=r.merged, settings_written=r.settings_written)
    return current_route()


def session_lock(session_id):
    """一个会话同一时刻只让它跑一轮——Pi 拒绝并发 prompt（'Agent is already processing'），
    而两个壳 + 主动消息随时可能撞上。宁可排队几秒，也不要回一个 502。
    ponytail: 一把朴素的锁，真排长队再换成有界队列 + 等待上限。"""
    with _sessions_lock:
        return _session_locks.setdefault(session_id, threading.Lock())


def extension_files():
    """她可以自己往 agent/extensions/ 里放 .ts；只收存在的文件。"""
    import glob
    return sorted(p for p in glob.glob(os.path.join(EXT_DIR, "*.ts")) if os.path.isfile(p))


# 她自己写的扩展坏了，整批加载失败、她那一轮就说不出话（2026-10-14 实测：造农历工具时写出一个没有默认导出的
# lunar-info.ts，再起身子就会全挂）。换身子前先整批自检一次，加载失败的那几个先不装、记进 state/broken-extensions.json，
# albion-turn.ts 下一轮告诉她哪个没装上、为什么，她自己去修；其余照常。按签名缓存，平时不多跑。查不了就不拦。
_EXT_CHECK = {}


def usable_extension_files():
    files = extension_files()
    sig = ext_signature()
    if sig not in _EXT_CHECK:
        broken = {}
        try:
            r = subprocess.run(["node", os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_extensions.ts"), *files],
                               capture_output=True, text=True, timeout=120, cwd=os.path.dirname(AGENT_DIR))
            if r.returncode != 0:
                lines = (r.stdout + r.stderr).splitlines()
                for i, line in enumerate(lines):
                    m = re.search(r"加载失败\s+(\S+\.ts)", line)
                    if m:
                        why = lines[i + 1].strip() if i + 1 < len(lines) else ""
                        broken[os.path.basename(m.group(1))] = why[:300]
        except Exception as exc:
            log("extension check failed", error=repr(exc)[:120])
        _EXT_CHECK.clear()
        _EXT_CHECK[sig] = broken
        path = os.path.join(wake._state_dir(), "broken-extensions.json")
        try:
            if broken:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(broken, f, ensure_ascii=False)
                log("extensions skipped", broken=sorted(broken))
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    broken = _EXT_CHECK[sig]
    return [p for p in files if os.path.basename(p) not in broken]


def ext_signature():
    # 扩展之外，settings.json、APPEND_SYSTEM.md 与技能（agent/skills/*/SKILL.md）也只在 pi 启动时读：改了同样要换身子，否则要等下次重启才生效。
    skills = [os.path.join(d, "SKILL.md") for d, _, fs in os.walk(os.path.join(AGENT_DIR, "skills")) if "SKILL.md" in fs]
    sig = []
    for p in extension_files() + [os.path.join(AGENT_DIR, "settings.json"), os.path.join(AGENT_DIR, "APPEND_SYSTEM.md")] + sorted(skills):
        try:
            sig.append((os.path.relpath(p, AGENT_DIR), int(os.path.getmtime(p))))
        except OSError:
            pass
    return tuple(sig)


def get_session(session_id):
    route = refresh_route()          # 每轮前看一次 magpie 写的设置：换了模型这里就跟上
    with _sessions_lock:
        sig = ext_signature()
        s = _sessions.get(session_id)
        if s is not None and not s.running:
            log("pi session died, restarting", session=session_id)
            s.close()
            s = None
        elif s is not None and getattr(s, "ext_sig", None) != sig:
            # 她自己改了扩展就换一具身子；新身子第一轮由 compose 从活动库接上刚才的对话。
            log("extensions changed, reloading pi session", session=session_id,
                extensions=[n for n, _ in sig])
            s.close()
            s = None
        if s is None:
            s = PiSession(session_id, cwd="/root", model=route_arg(route),
                          tools=tools_for_session(session_id), extensions=usable_extension_files(),
                          log_path=err_log_path(session_id),
                          sandbox_env={"PI_CODING_AGENT_DIR": AGENT_DIR,
                                       "ALBION_SESSION_ID": session_id,
                                       # WSL 系统时区 10-13 起被改成 America/Chicago：她的日期、农历、bash 里的 date 全差 13 小时。
                                       # 她和指挥官都在北京时间过日子，pi 一律按北京时间起（宿主自己设了 TZ 就照它的）。
                                       "TZ": os.environ.get("TZ") or "Asia/Shanghai",
                                       "COMMANDCODE_API_KEY": os.environ.get("COMMANDCODE_API_KEY", ""),
                                       **session_env(session_id)})
            s.start()
            s.seeded = False
            s.ext_sig = sig
            _sessions[session_id] = s
            log("started pi session", session=session_id, argv=" ".join(s.args()),
                tools=("readonly" if is_ephemeral_session(session_id) else "all"))
        return s


def compose(session, msgs):
    """本轮递给 Pi 的 (文本, 图片, 是不是她自己醒来)。约定/回忆由扩展在 context 钩子里注入，这里只管接上历史。"""
    history, tail = split_tail(msgs)
    users = [m for m in tail if m.get("role") == "user"] or [msgs[-1]]
    texts = [msg_text(m) for m in users]
    # 醒来与记账只认她桌面那条会话：验收、探针、别的会话不碰她的作息
    name = getattr(session, "name", SESSION_DEFAULT)
    own = name == SESSION_DEFAULT
    woke = own and any(wake.is_wake(t) for t in texts)
    if woke:
        # 壳只按了门铃；醒来时看到什么由 wake.wake_prompt 定（时间、他多久没说话、她定的事、心事本）
        # 门铃前后夹着他真说的话都算他在说话（haiku 复核 r34-P1）
        said = [t for t in texts if wake.is_real_user(t)]
        if said:
            # 门铃前面夹着他真说的话：他在说话，这一轮就是回他，不算她自己醒来（haiku 复核 D / r33-2）
            wake.note_user()
            wake.cancel_ring()
            texts, woke = said, False
        else:
            texts = [wake.wake_prompt()]
    elif own and any(wake.is_real_user(t) for t in texts):
        wake.note_user()
    current = "\n\n".join(t for t in texts if t)
    # 图片跟着同一批**本轮新来的**用户消息走；历史里的旧图不重发。
    images = [u for m in users for u in msg_images(m)]
    if not getattr(session, "seeded", False):
        session.seeded = True
        # 新身子：请求自己带了历史就用请求的；壳只发最新一句（chat.ts），就从活动库接上这个会话最近的对话
        if not history:
            history = activity_history(name)
            if history:
                log("seeded from activity db", session=name, rows=len(history))
        if history:
            current = transcript(history) + "\n\n【现在】\n" + current
    return current, images, woke


# ── 会话隔离：只有他本人那只写生产状态、进 OpenViking ──────────────────────
# 真实会话名单在 agent/real-sessions.json（ALBION_MEMORY_SESSIONS 可覆盖）。其余会话（探针、验收、自检，
# 例如 albion-voice-probe-*、albion-accept、albion-r44*、albion-dsh-*）一律算 ephemeral：
#   · 不写生产 state：心事本/self/todo/voice/last-context 由扩展改写 ALBION_PERSONAL_STATE_DIR 下的临时文件，
#     工具照常返回（判断在 agent/lib/session-scope.ts，这里只负责把这两个变量交下去）；
#   · 不提交 OpenViking：OPENVIKING_BYPASS_SESSION=1（官方扩展整段跳过；每轮召回走 bin/memory.py，不受影响）。
# 2026-10-29 修：原来只挡了 OV 捕获，心事本仍被探针台词写进生产，10-25 的「指挥官做实验一整天」就是那样来的。
def _real_sessions():
    env = os.environ.get("ALBION_MEMORY_SESSIONS", "").strip()
    if env:
        return {s.strip() for s in env.split(",") if s.strip()}
    try:
        with open(os.path.join(AGENT_DIR, "real-sessions.json"), encoding="utf-8") as f:
            data = json.load(f)
        sessions = data.get("sessions") if isinstance(data, dict) else data
        names = {str(s).strip() for s in sessions or [] if str(s).strip()}
        if names:
            return names
    except (OSError, ValueError):
        pass
    return {"albion-desktop", "albion-cli"}


REAL_SESSIONS = _real_sessions()


def is_ephemeral_session(session_id):
    """探针/验收/自检会话：不写生产 state、不提交 OpenViking（与 agent/lib/session-scope.ts 同一口径）。"""
    return session_id not in REAL_SESSIONS


def tools_for_session(session_id):
    """临时会话只给只读工具；真实会话（albion-desktop/albion-cli…）一件不少。

    判断沿用 is_ephemeral_session（real-sessions.json / ALBION_MEMORY_SESSIONS），不另造名单。
    `--tools` 是替换整份选择的白名单；写类内置工具（write/edit/bash）不在里面，扩展工具也只剩只读的几件。
    """
    return list(READONLY_TOOLS) if is_ephemeral_session(session_id) else list(TOOLS)


def ephemeral_state_dir(session_id):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")[:120] or "unknown"
    return os.path.join("/tmp", "albion-pi-ephemeral", safe)


# 提交给 OpenViking 之前要剥掉的「脑自己拼进去的段」（官方扩展的 OPENVIKING_CAPTURE_FILTERS，sed 式规则）：
#   · 她自己醒来的提示词：心事本、上次定的事都在里面，那不是他说的，整条不提交；
#   · 宿主补问【接着说完】、活动库里那两句占位、会话刚重启时接上的旧记录（只留【现在】之后他真打的字）；
#   · 每轮约定与回忆块（正常轮由 albion-turn 在 context 钩子里加，不进 pi 会话；留着兜底）。
# 规则里不能出现英文逗号（宿主按逗号拼成一条环境变量交给扩展）。
CAPTURE_FILTERS = (
    r"user:d|^【自主醒来】|",
    r"user:d|^【接着说完】|",
    r"user:d|^（阿尔比恩自己醒来）$|",
    r"user:d|^（阿尔比恩接着把刚才的话说完）$|",
    r"user:s|^【以下是这次对话此前的记录[^\n]*\n[\s\S]*?【现在】\n||",
    r"user:s|<memory-context>[\s\S]*?</memory-context>||",
    r"user:s|\n*【阿尔比恩本轮说话约定】[\s\S]*$||",
)


def capture_filter_rules():
    extra = [s.strip() for s in os.environ.get("OPENVIKING_CAPTURE_FILTERS", "").split(",") if s.strip()]
    return list(CAPTURE_FILTERS) + [r for r in extra if r not in CAPTURE_FILTERS]


def session_env(session_id):
    """pi 子进程的环境：真实会话只加「提交前剥注入段」；ephemeral 会话隔离状态、不进 OV。"""
    if is_ephemeral_session(session_id):
        return {"OPENVIKING_BYPASS_SESSION": "1",
                "ALBION_EPHEMERAL": "1",
                "ALBION_PERSONAL_STATE_DIR": ephemeral_state_dir(session_id)}
    rules = capture_filter_rules()
    return {"OPENVIKING_CAPTURE_FILTERS": ",".join(rules)} if rules else {}


# 只有桌面会话的 pi stderr 留在 logs/（超 1MB 起新身子前轮转成 .err.1）；探针/验收会话写 /tmp，不再堆满 logs/。
def err_log_path(session_id, logs=LOGS, tmp_dir="/tmp/albion-pi-err"):
    if session_id != SESSION_DEFAULT:
        os.makedirs(tmp_dir, exist_ok=True)
        return os.path.join(tmp_dir, "pi-%s.err" % session_id)
    path = os.path.join(logs, "pi-%s.err" % session_id)
    if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
        os.replace(path, path + ".1")
    return path


# 他说了话，她这一轮却一个字都没回（只有情绪标记或全空）：干活链半路停了、被复读断路停用后硬发到终止、
# 整理器把英文拒绝稿换成了表情……补一句她自己的念头，照实说做到哪、卡在哪。只补一次，同一会话、同一模型。
# 宿主别的补催（去玩没玩、说做好了没做、没定下次醒来）2026-10-16 起都撤了：做不做是她自己的事。
# 这一轮只用文字：albion-loopguard.ts 认【接着说完】开头，把工具整个撤掉（给着工具她会接着复读到被终止）。
FINISH_FOLLOWUP = ("【接着说完】（这是我自己心里的念头，不是他说的，也不会给他看）现在回他刚才那句话。"
                   "要是刚才在替他做事，这一下先不动手，用一两句话照实告诉他做到了哪一步、真实结果是什么；"
                   "卡住了就说卡在哪、试过什么、打算怎么换个做法，他想让我接着做我再接着做。"
                   "要是他只是在跟我说话，就像平常一样直接回他那句。不编结果。")
QUIET_ASK = re.compile(r"(?:别|不要|不用)(?:再)?(?:说话|出声|开口|回复|回我)|静默")


def session_of(body, headers):
    """这次请求属于哪个会话：body 的 session_id → 壳的 X-Hermes-Session-Id 头 → body 的 user → 桌面默认。
    壳（chat.ts）一直把会话 id 放在请求头里，原来宿主不读，全靠默认值碰巧对上（2026-10-16 补上）。"""
    sid = body.get("session_id") or (headers.get("X-Hermes-Session-Id") or "").strip() or body.get("user")
    return str(sid or SESSION_DEFAULT)


def body_of(reply):
    return re.sub(r"<\|ACT:[^\n]*?\|>", "", reply or "").strip()


def needs_finish_followup(user_text, reply):
    """他真说了话（不是门铃、不是壳替她发的系统消息）、没叫她别出声，她却一个字没回。"""
    text = user_text or ""
    if not wake.is_real_user(text) or QUIET_ASK.search(text):
        return False
    return not body_of(reply)


# ── 一轮卡住之后：先 abort，abort 不了就换身子 ─────────────────────────────
# 2026-10-30 生产（albion-voice-probe-soul3-b-multi）：一轮跑满 420s 报 "pi did not settle within 420s"，
# 但 Pi 那一轮其实还在跑；之后同一会话每个请求都秒回 "prompt rejected: pi stayed busy"，直到重启脑。
# 桌面会话要是这样，她就一直不说话。现在：RPC abort（Pi 回 abort 时已经 idle）；还不行就关掉这具身子，
# 下个请求由 get_session 按现有逻辑重开、compose 照常从活动库播种历史。每一步都写进 brain.log。
ABORT_WAIT = int(os.environ.get("ALBION_ABORT_WAIT", "30"))
STUCK_ERRORS = ("did not settle", "stayed busy", "already processing",
                "no response to prompt", "process exited during the run")


def is_stuck_error(exc):
    """这一轮是不是「Pi 还在忙/没定稿」——只有这类才 abort/换身子，别的错误照原样报。"""
    text = str(exc or "")
    return any(mark in text for mark in STUCK_ERRORS)


def recover_stuck_session(session_id, session, exc):
    """中止卡住的那一轮；中止失败就换身子。返回 True = abort 后 Pi 已 idle、可继续用这具身子。"""
    log("turn stuck, aborting", session=session_id, error=str(exc)[:200])
    aborted = False
    if getattr(session, "running", False):
        try:
            aborted = bool(session.abort(ABORT_WAIT))
        except Exception as aexc:        # noqa: BLE001 - 管道断了也按换身子走
            log("abort failed", session=session_id, error=repr(aexc)[:160])
    if aborted:
        log("aborted, pi idle again", session=session_id, wait_s=ABORT_WAIT)
        return True
    log("pi still busy after abort, restarting session", session=session_id, running=getattr(session, "running", False))
    try:
        session.close()
    except Exception as cexc:            # noqa: BLE001
        log("session close failed", session=session_id, error=repr(cexc)[:160])
    with _sessions_lock:
        if _sessions.get(session_id) is session:
            _sessions.pop(session_id, None)
    log("session dropped, next request starts a fresh one", session=session_id)
    return False


def run_prompt(session_id, session, text, images=None):
    """跑一轮；卡住就恢复再照实抛出（这一轮仍算失败，下一个请求能正常回）。"""
    try:
        return session.prompt(text, images=images, timeout=RUN_TIMEOUT)
    except PiError as exc:
        if is_stuck_error(exc):
            recover_stuck_session(session_id, session, exc)
        raise


def chat(session_id, msgs):
    started = time.time()
    with session_lock(session_id):
        session = get_session(session_id)
        text, images, woke = compose(session, msgs)
        log("prompt", session=session_id, chars=len(text), images=len(images), woke=woke)
        reply = run_prompt(session_id, session, text, images=images)
        if woke:
            reply = wake.after_wake(reply)
        elif needs_finish_followup(msg_text(msgs[-1]), reply):
            try:
                again = run_prompt(session_id, session, FINISH_FOLLOWUP)
                log("finish followup", session=session_id, chars=len(again or ""))
                if body_of(again):
                    reply = again
            except Exception as exc:
                log("finish followup failed", session=session_id, error=repr(exc)[:120])
    log("turn", session=session_id, ms=round((time.time() - started) * 1000), chars=len(reply))
    return reply


# ── HTTP：对两个壳的 OpenAI 兼容端点 ────────────────────────────────────────
class Brain(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "albion-pi/2.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        want = os.environ.get("API_SERVER_KEY")
        got = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        return (not want) or (got == want)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            route = current_route()
            self._json(200, {"status": "ok", "brain": "pi", "host": "thin",
                             "model": route["model"], "provider": route["provider"],
                             "source": route["source"], "agent": AGENT_NAME,
                             "extensions": [os.path.basename(p) for p in extension_files()]})
        elif path == "/albion/wake":
            # 壳的门铃：到没到她自己定的醒来时间（底线都算进去了）。到点只响一次。
            if not self._authorized():
                self._json(401, {"error": {"message": "unauthorized", "type": "invalid_request_error"}})
                return
            busy = session_lock(SESSION_DEFAULT).locked()
            due, next_at = wake.ring(busy)
            self._json(200, {"due": due, "next_at": next_at, "next": wake.clock(next_at), "marker": wake.WAKE_MARKER})
        elif path == "/albion/shell-tools":
            # albion-shell-tools.ts 每轮开跑前来取：这个会话最近一次壳带来的插件工具清单
            if not self._authorized():
                self._json(401, {"error": {"message": "unauthorized", "type": "invalid_request_error"}})
                return
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            sid = (query.get("session") or [SESSION_DEFAULT])[0]
            self._json(200, shell_tools.bridge_for(sid).snapshot())
        elif path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": AGENT_NAME, "object": "model", "created": 0, "owned_by": "albion-pi"}]})
        else:
            self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/albion/shell-tools/call":
            self._shell_tool_call()
            return
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._json(400, {"error": {"message": "Invalid JSON in request body",
                                       "type": "invalid_request_error"}})
            return
        if not self._authorized():
            self._json(401, {"error": {"message": "unauthorized", "type": "invalid_request_error"}})
            return
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            self._json(400, {"error": {"message": "Missing or invalid 'messages' field",
                                       "type": "invalid_request_error"}})
            return
        session_id = session_of(body, self.headers)
        stream = bool(body.get("stream"))
        bridge = shell_tools.bridge_for(session_id)
        # 末尾是 role=tool：壳执行完插件工具带结果回来了，接上那一轮；否则是新的一轮，先更新这个会话的插件工具清单
        results = shell_tools.tool_results(msgs)
        toolset = None if results else shell_tools.toolset_from(body, self.headers)
        log("request", path=path, model=str(body.get("model")), stream=stream, msgs=len(msgs),
            session=session_id, tools=len(body.get("tools") or []),
            shell_tools=len(toolset["tools"]) if toolset else None, tool_results=len(results),
            ua=(self.headers.get("User-Agent") or "")[:60],
            auth=("key" if (self.headers.get("Authorization") or "").strip() else "none"))
        if results:
            turn = bridge.resume(results)
            if turn is None:
                # 对不上正在等的调用（超时已按失败交还、或宿主重启过）：那一轮要是已经说完了就把话补交，否则空回复
                text = bridge.orphan_text() or ""
                log("shell tool results without a waiting call", session=session_id, ids=sorted(results)[:4],
                    orphan_chars=len(text))
                self._reply(stream, text)
                return
        else:
            if toolset["skipped"]:
                log("shell tools skipped", session=session_id, skipped=toolset["skipped"][:8])
            turn = bridge.start(lambda: chat(session_id, msgs), toolset=toolset,
                                supersede=bool(self.headers.get(shell_tools.HEADER)),
                                wait_previous=RUN_TIMEOUT)
        kind, value = bridge.wait(turn, RUN_TIMEOUT + 60)
        if kind == "calls":
            log("shell tool calls", session=session_id, calls=[c.name for c in value])
            self._tool_calls(stream, value)
            return
        if kind == "timeout":
            log("brain error", session=session_id, error="turn did not finish in time")
            self._json(502, {"error": {"message": "brain error: turn did not finish in time", "type": "server_error"}})
            return
        if kind == "error":
            exc = value
            if isinstance(exc, PiError):
                log("brain error", session=session_id, error=str(exc)[:300])   # 原来只回给壳、不记，查不到 502 为什么
                self._json(502, {"error": {"message": "brain error: %s" % exc, "type": "server_error"}})
            else:
                log("unhandled", error=repr(exc))
                self._json(500, {"error": {"message": "internal error: %r" % (exc,), "type": "server_error"}})
            return
        self._reply(stream, value)

    def _reply(self, stream, text):
        if stream:
            self._sse(text)
        else:
            self._json(200, self._payload(text))

    def _shell_tool_call(self):
        """albion-shell-tools.ts 的阻塞调用：等壳执行完插件工具（或超时）再回。"""
        if not self._authorized():
            self._json(401, {"error": {"message": "unauthorized", "type": "invalid_request_error"}})
            return
        n = int(self.headers.get("content-length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            sid, name = str(req["session"]), str(req["name"])
            args = req.get("arguments") or {}
        except Exception:
            self._json(400, {"error": {"message": "need session, name, arguments", "type": "invalid_request_error"}})
            return
        ok, text = shell_tools.bridge_for(sid).call(name, args)
        if not ok:
            log("shell tool failed", session=sid, tool=name, reason=text[:120])
        self._json(200, {"ok": ok, "text": text})

    def _tool_calls(self, stream, calls):
        """把她要用的壳工具按 OpenAI tool_calls 回给壳；壳（xsai）执行后会带着 role=tool 结果再发一次。"""
        tool_calls = [c.openai() for c in calls]
        if not stream:
            self._json(200, {
                "id": "chatcmpl-pi-%d" % int(time.time() * 1000), "object": "chat.completion",
                "created": int(time.time()), "model": AGENT_NAME,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                             "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
            return
        self._sse_open()
        try:
            self.wfile.write(self._chunk({"role": "assistant", "content": ""}).encode("utf-8"))
            self.wfile.write(self._chunk({"tool_calls": [dict(c, index=i) for i, c in enumerate(tool_calls)]}).encode("utf-8"))
            self.wfile.write(self._chunk({}, "tool_calls").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _payload(self, text):
        return {
            "id": "chatcmpl-pi-%d" % int(time.time() * 1000),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": AGENT_NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": len(text), "total_tokens": len(text)},
        }

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    @staticmethod
    def _chunk(delta, finish=None):
        return "data: " + json.dumps({
            "id": "chatcmpl-pi", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": AGENT_NAME,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
            ensure_ascii=False) + "\n\n"

    def _sse(self, text):
        """壳若要求流式：整段先过守卫（在 Pi 里），再按句切块送出。"""
        self._sse_open()
        chunk = self._chunk

        try:
            self.wfile.write(chunk({"role": "assistant", "content": ""}).encode("utf-8"))
            for piece in split_for_speech(text):
                self.wfile.write(chunk({"content": piece}).encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(chunk({}, "stop").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


# ── 自检 ────────────────────────────────────────────────────────────────────
def _selftest():
    msgs = [
        {"role": "user", "content": "[2026-10-07 01:00] 早上好"},
        {"role": "assistant", "content": '<|ACT:{"emotion":"happy"}|>\n早上好，指挥官。'},
        {"role": "user", "content": "[2026-10-07 02:00] 在忙吗"},
    ]
    hist, tail = split_tail(msgs)
    assert len(hist) == 2 and len(tail) == 1, (len(hist), len(tail))
    assert msg_text(tail[0]).endswith("在忙吗")
    t = transcript(hist)
    assert "指挥官：[" in t and "阿尔比恩：" in t
    assert "".join(split_for_speech("你好。今天怎么样？")) == "你好。今天怎么样？"
    assert len(split_for_speech("你好。今天怎么样？")) == 2
    payload = Brain._payload(None, "你好")
    assert payload["choices"][0]["message"]["content"] == "你好"
    assert os.path.isdir(EXT_DIR), "extensions dir missing: %s" % EXT_DIR
    import tempfile

    class _S:
        seeded = True
    os.environ["ALBION_STATE_DIR"] = tempfile.mkdtemp(prefix="host-wake-")
    seen = msgs[:2] + [{"role": "user", "content": "[2026-10-14 12:00] " + wake.WAKE_MARKER}]
    text, _, woke = compose(_S(), seen)
    assert woke and text.startswith(wake.WAKE_MARKER) and "next_wake" in text, text[:80]
    text, _, woke = compose(_S(), msgs)
    assert not woke and wake.load()["last_user_at"] > 0
    both = msgs[:2] + [{"role": "user", "content": "在吗"}, {"role": "user", "content": wake.WAKE_MARKER}]
    text, _, woke = compose(_S(), both)
    assert not woke and text == "在吗", (woke, text[:40])
    both = msgs[:2] + [{"role": "user", "content": wake.WAKE_MARKER}, {"role": "user", "content": "在吗"}]
    text, _, woke = compose(_S(), both)
    assert not woke and text == "在吗", (woke, text[:40])
    # 说了话却 0 字回复要补一次；他让她别出声、门铃、壳替她发的系统消息不补；有正文不补
    assert needs_finish_followup("帮我查明天天气再画张画", "") and needs_finish_followup("在吗", '<|ACT:{"emotion":"happy"}|>')
    assert not needs_finish_followup("先别说话，我在开会", '<|ACT:{"emotion":"neutral"}|>')
    assert not needs_finish_followup("[2026-10-16 09:00] " + wake.WAKE_MARKER, "")
    assert not needs_finish_followup("（这是我自己想到的事，不是谁发来的话。）", "")
    assert not needs_finish_followup("在吗", '<|ACT:{"emotion":"happy"}|>在呢。')
    assert FINISH_FOLLOWUP.startswith("【接着说完】") and "不编结果" in FINISH_FOLLOWUP and "先不动手" in FINISH_FOLLOWUP
    # 两轮各用同一可覆盖的时限；已有正文直接交付，只有空正文才补问一次。
    from unittest.mock import Mock, patch
    fake = Mock()
    fake.prompt.return_value = "在呢。"
    module = sys.modules[__name__]
    with patch.object(module, "get_session", return_value=fake), \
            patch.object(module, "compose", return_value=("在吗", [], False)), patch.object(module, "log"):
        assert chat("albion-probe-timeout", msgs[-1:]) == "在呢。"
        fake.prompt.assert_called_once_with("在吗", images=[], timeout=RUN_TIMEOUT)
        fake.prompt.reset_mock()
        fake.prompt.side_effect = ['<|ACT:{"emotion":"neutral"}|>', "刚才没做完。"]
        assert chat("albion-probe-timeout", msgs[-1:]) == "刚才没做完。"
        assert [call.kwargs["timeout"] for call in fake.prompt.call_args_list] == [RUN_TIMEOUT, RUN_TIMEOUT]
        assert fake.prompt.call_args_list[1].args == (FINISH_FOLLOWUP,)
    # 换身子后从活动库接上对话：只取本会话两天内的 user/assistant；第一轮那条里套着的旧记录只取【现在】之后；补问轮跳过；醒来行按门铃
    import sqlite3
    db = sqlite3.connect(os.path.join(wake._state_dir(), "state.db"))
    db.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                     " tool_calls TEXT, tool_name TEXT, timestamp REAL);")
    now = time.time()
    rows = [("albion-desktop", "user", "很久以前", now - 3 * 86400), ("albion-desktop", "assistant", "旧回复", now - 3 * 86400),
            ("albion-desktop", "user", "【以下是这次对话此前的记录】\n指挥官：更早的话\n\n【现在】\n[2026-10-16 10:00] 今天想吃面", now - 600),
            ("albion-desktop", "assistant", '<|ACT:{"emotion":"happy"}|>\n好呀，我也想吃。', now - 590),
            ("albion-desktop", "tool", "{}", now - 589),
            ("albion-desktop", "user", "（阿尔比恩接着把刚才的话说完）", now - 500),
            ("albion-desktop", "user", "（阿尔比恩想起来，真去做刚才说的事）", now - 450),
            ("albion-desktop", "user", "（阿尔比恩自己醒来）", now - 400), ("albion-desktop", "assistant", "（想他）", now - 399),
            ("albion-accept", "user", "别的会话", now - 300)]
    db.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", rows)
    db.commit()
    db.close()
    hist = activity_history("albion-desktop", now)
    assert [m["content"] for m in hist] == ["[2026-10-16 10:00] 今天想吃面", '<|ACT:{"emotion":"happy"}|>\n好呀，我也想吃。',
                                             wake.WAKE_MARKER, "（想他）"], hist
    t = transcript(hist)
    assert "今天想吃面" in t and "更早的话" not in t and "自己醒来了一次" in t and "很久以前" not in t

    class _Fresh:
        seeded = False
        name = "albion-desktop"
    text, _, woke = compose(_Fresh(), [{"role": "user", "content": "[2026-10-16 10:20] 那就去吃吧"}])
    assert not woke and "【现在】\n[2026-10-16 10:20] 那就去吃吧" in text and "今天想吃面" in text, text[:120]
    fresh = _Fresh()
    fresh.name = "albion-new"
    text, _, _ = compose(fresh, [{"role": "user", "content": "你好"}])
    assert text == "你好", "活动库里没这个会话：不接"
    # 会话隔离：真实会话不隔离、只带「提交前剥注入段」；探针/验收会话写临时 state、不进 OV（2026-10-29）
    assert not is_ephemeral_session("albion-desktop") and not is_ephemeral_session("albion-cli")
    assert is_ephemeral_session("albion-voice-probe-after4c-05") and is_ephemeral_session("albion-accept")
    desk_env, probe_env = session_env("albion-desktop"), session_env("albion-probe-r44a")
    assert "OPENVIKING_BYPASS_SESSION" not in desk_env and "ALBION_EPHEMERAL" not in desk_env, desk_env
    assert desk_env["OPENVIKING_CAPTURE_FILTERS"].count("user:") >= len(CAPTURE_FILTERS), desk_env
    assert probe_env["OPENVIKING_BYPASS_SESSION"] == "1" and probe_env["ALBION_EPHEMERAL"] == "1", probe_env
    assert probe_env["ALBION_PERSONAL_STATE_DIR"] == ephemeral_state_dir("albion-probe-r44a"), probe_env
    assert probe_env["ALBION_PERSONAL_STATE_DIR"].startswith("/tmp") and "albion-pi-ephemeral" in probe_env["ALBION_PERSONAL_STATE_DIR"]
    assert "OPENVIKING_CAPTURE_FILTERS" not in probe_env
    # 剥注入段的规则：醒来提示词整条丢；重启接上的旧记录只留【现在】之后他真打的字；约定/回忆块剥掉
    assert re.match(r"^【自主醒来】", wake.wake_prompt()), "醒来提示词要以 WAKE_MARKER 开头（过滤规则赖它）"
    assert re.match(r"^【接着说完】", FINISH_FOLLOWUP)
    # 会话 id：body 优先，其次壳的请求头，都没有才是桌面默认
    assert session_of({"session_id": "a"}, {"X-Hermes-Session-Id": "b"}) == "a"
    assert session_of({"user": "u"}, {"X-Hermes-Session-Id": "albion-x"}) == "albion-x"
    assert session_of({"user": "u"}, {}) == "u" and session_of({}, {}) == SESSION_DEFAULT

    class _Other:
        seeded = True
        name = "albion-accept"
    before = wake.load()["last_user_at"]
    text, _, woke = compose(_Other(), seen)
    assert not woke, "别的会话的门铃不算她醒来"
    compose(_Other(), msgs)
    assert wake.load()["last_user_at"] == before, "别的会话不记账"
    # pi stderr：桌面写 logs/ 且超 1MB 轮转一份；别的会话写 /tmp
    logs_dir, tmp_err = tempfile.mkdtemp(prefix="host-logs-"), tempfile.mkdtemp(prefix="host-err-")
    desk = err_log_path(SESSION_DEFAULT, logs_dir, tmp_err)
    assert desk == os.path.join(logs_dir, "pi-albion-desktop.err")
    assert err_log_path("albion-probe-x", logs_dir, tmp_err) == os.path.join(tmp_err, "pi-albion-probe-x.err")
    assert err_log_path("albion-probe-x", logs_dir) == "/tmp/albion-pi-err/pi-albion-probe-x.err" and os.path.isdir("/tmp/albion-pi-err")
    with open(desk, "wb") as f:
        f.write(b"x" * 1024)
    err_log_path(SESSION_DEFAULT, logs_dir, tmp_err)
    assert os.path.exists(desk) and not os.path.exists(desk + ".1"), "没超 1MB 不轮转"
    with open(desk, "wb") as f:
        f.write(b"x" * (1024 * 1024 + 1))
    assert err_log_path(SESSION_DEFAULT, logs_dir, tmp_err) == desk
    assert not os.path.exists(desk) and os.path.getsize(desk + ".1") == 1024 * 1024 + 1, "超 1MB 轮转成 .err.1"
    # 临时会话只给只读工具；真实会话一件不少（问题 2：探针会话曾改写生产 bin/self_ring.py）
    assert tools_for_session("albion-desktop") == TOOLS and "*" in tools_for_session("albion-desktop")
    assert tools_for_session("albion-cli") == TOOLS
    probe_tools = tools_for_session("albion-voice-probe-after4c-05")
    for banned in ("write", "edit", "bash", "powershell", "delegate", "next_wake", "notebook",
                   "draw", "todo", "skill", "adventure", "browser", "look"):
        assert banned not in probe_tools, (banned, probe_tools)
    for kept in ("read", "grep", "find", "ls", "openviking_search", "openviking_read", "chat_search", "web_search"):
        assert kept in probe_tools, (kept, probe_tools)
    probe_argv = PiSession("probe", model="p/m", tools=probe_tools).args()
    assert "--tools" in probe_argv and "--no-tools" not in probe_argv
    assert probe_argv[probe_argv.index("--tools") + 1] == ",".join(probe_tools), probe_argv
    assert PiSession("desk", model="p/m", tools=TOOLS).args()[probe_argv.index("--tools") + 1] == "*"
    # 超时/busy 只有这类错误才 abort/换身子；别的错误照原样报
    assert is_stuck_error(PiError("pi did not settle within 420s"))
    assert is_stuck_error(PiError("prompt rejected: pi stayed busy"))
    assert is_stuck_error(PiError("no response to prompt command"))
    assert not is_stuck_error(PiError("prompt rejected: unauthorized"))
    # abort 的回应没有 id，只有 command：按命令名等得到
    _ps = PiSession("probe", model="p/m", tools=[])
    _ps._dispatch({"type": "response", "command": "abort", "success": True})
    assert _ps._wait_response(("cmd", "abort"), 1)["success"] is True
    # 问题 1：超时后先 abort，Pi 回 idle 就继续用这具身子；这一轮照实报错
    sid = "albion-probe-stuck"
    stuck = Mock()
    stuck.running = True
    stuck.prompt.side_effect = PiError("pi did not settle within 420s")
    stuck.abort.return_value = True
    with patch.object(module, "get_session", return_value=stuck), \
            patch.object(module, "compose", return_value=("在吗", [], False)), \
            patch.object(module, "log") as logmock:
        try:
            chat(sid, msgs[-1:])
        except PiError as exc:
            assert "did not settle" in str(exc)
        else:
            raise AssertionError("卡住那一轮仍应回报失败")
        stuck.abort.assert_called_once()
        stuck.close.assert_not_called()
        assert any("aborted, pi idle again" in c.args[0] for c in logmock.call_args_list), logmock.call_args_list
    # abort 不成功（还忙/管道断了）：关掉这具身子、从会话表摘掉，下个请求重开并播种
    busy = Mock()
    busy.running = True
    busy.prompt.side_effect = PiError("prompt rejected: pi stayed busy")
    busy.abort.return_value = False
    module._sessions[sid] = busy
    with patch.object(module, "get_session", return_value=busy), \
            patch.object(module, "compose", return_value=("在吗", [], False)), \
            patch.object(module, "log") as logmock:
        try:
            chat(sid, msgs[-1:])
        except PiError:
            pass
        else:
            raise AssertionError("busy 应回报失败")
        busy.close.assert_called_once()
        assert sid not in module._sessions, "换了身子：下个请求要重开"
        assert any("restarting session" in c.args[0] for c in logmock.call_args_list)
    fresh = Mock()
    fresh.running = True
    fresh.prompt.return_value = "好了。"
    with patch.object(module, "get_session", return_value=fresh), \
            patch.object(module, "compose", return_value=("在吗", [], False)), \
            patch.object(module, "log"):
        assert chat(sid, msgs[-1:]) == "好了。", "下一个请求要能正常回复"
    print("HOST SELFTEST OK")


def main():
    load_env()
    os.makedirs(LOGS, exist_ok=True)
    make_system_prompt()
    route = refresh_route()          # 开局先按 magpie 的设置定模型（顺带合并 provider、回写 settings）
    # 模型通路：commandcode 由 albion-relay.ts 在 Pi 里自托管；别的 provider 不走它，探也白探。
    def relay_probe():
        if current_route()["provider"] != "commandcode":
            log("note: relay only serves commandcode; current provider is %s"
                % current_route()["provider"])
            return
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % RELAY_PORT, timeout=5)
        except Exception as exc:
            log("note: relay not reachable yet (it starts with Pi)", port=RELAY_PORT, error=str(exc)[:80])
    brain = ThreadingHTTPServer(("127.0.0.1", PORT), Brain)
    brain.daemon_threads = True
    log("albion host up (thin)", brain=PORT, extensions=[os.path.basename(p) for p in extension_files()],
        model=route_arg(route), source=route["source"])
    threading.Thread(target=relay_probe, daemon=True).start()
    threading.Thread(target=widget_snapshot.loop, args=(log,), daemon=True).start()   # 桌面摆件只读 state/widget.json
    try:
        brain.serve_forever()
    finally:
        for s in list(_sessions.values()):
            s.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
