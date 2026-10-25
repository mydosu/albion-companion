# -*- coding: utf-8 -*-
"""TTS 桥：OpenAI /v1/audio/speech 兼容

- 五船：Irodori-TTS 单进程多船服务（:9889）
- denia：GSV 中文角色（api_v2 CPU 常驻 :9880），不过假名过滤、原样送中文
- input=文本；voice=角色（albion/taihou/shinano/brest/jinshi/denia，兼容旧名 xxx-irodori）
- 五船只念含假名的行（双语消息取日语行朗读）；无假名片段返回短静音保持流水线
- 情绪/风格由后端自动判断，本桥不传 caption
- 流式（可选）：请求带 `Accept: text/event-stream` 或 `stream_format:"sse"` → 透传后端 /tts_stream 的 SSE
  （每段一个 audio_chunk，选船方式不变）；不带 → 与原来完全一致（整条 wav）。denia(GSV) 不支持流式。
"""
import io, json, os, re, struct, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, wave
import queue
import hashlib
import logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import asyncio

# 日志口固定 UTF-8：桥的 stdout/stderr 被 `>> logs/tts_bridge.log` 重定向，Windows 下
# Python 按 cp936 打开文件句柄 ⇒ 译文里出现 GBK 没有的「〜」(U+301C) 时，`print` 直接抛
# UnicodeEncodeError，整句译文连音频一起丢掉（吞句）。转一次编码，坏字符也不许崩。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi.responses import StreamingResponse
import uvicorn

app = FastAPI()

BACKEND = "http://127.0.0.1:9889/tts"          # Irodori-TTS（单进程，5 船，整条 wav）
BACKEND_STREAM = "http://127.0.0.1:9889/tts_stream"   # 同一后端的 SSE 分块端点
VOICES = ("albion", "taihou", "shinano", "brest", "jinshi", "denia")
DEFAULT_VOICE = "albion"


def text_meta(text):
    text = str(text)
    return "len=%d sha256=%s" % (len(text), hashlib.sha256(text.encode("utf-8")).hexdigest())


_FILE_LOGGERS = {}
_LOG_LOCK = threading.Lock()


def _file_log(name, message):
    """日志至多 1 MiB × 4，初始化和轮转都受锁保护。调用者只传元数据。"""
    try:
        with _LOG_LOCK:
            logger = _FILE_LOGGERS.get(name)
            if logger is None:
                logger = logging.getLogger("albion.tts." + name)
                logger.setLevel(logging.INFO)
                logger.propagate = False
                log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "tts")
                os.makedirs(log_dir, exist_ok=True)
                handler = RotatingFileHandler(os.path.join(log_dir, name),
                                              maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                logger.addHandler(handler)
                _FILE_LOGGERS[name] = logger
        logger.info(message)
    except OSError:
        pass

# denia：GSV 中文角色，CPU 常驻 :9880（参考音/提示文本取自定稿配置）
GSV_TTS = "http://127.0.0.1:9880/tts"
DENIA_REF = r"D:/AI/Wife/tts/denia/audio/output.wav_0009342720_0009558400.wav"
DENIA_PROMPT = "怎么啊？如果有你在也不放心，那就干脆给我也装个限制器或者炸弹喽。"

# 语种判据与「网址/路径剥除」只留 bridge_v2 一份（v1 路径与 /v2/prepare 共用）
from bridge_v2 import has_readable, is_japanese, strip_links  # noqa: E402

# 句间留白：合成结果尾巴再补一段纯静音 —— 她一句接一句太赶（2026-09-21 用户反馈）时用来留换气。
# 实测每段本来就有 ~0.78s 低电平尾音，但阈值一过还是"连着说"的听感 ⇒ 叠一层真静音。
# 旋钮：环境变量 ALBION_TAIL_GAP 覆盖（秒，0 = 关）。
TAIL_GAP_SEC = float(os.environ.get("ALBION_TAIL_GAP", "0.35"))


def _pad_tail(wav: bytes, sec: float = None) -> bytes:
    """给 WAV 尾巴补纯静音。非 WAV / 解析失败原样返回（宁可不留白，也不能弄坏音频）。"""
    sec = TAIL_GAP_SEC if sec is None else sec
    if sec <= 0 or wav[:4] != b"RIFF":
        return wav
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            p = w.getparams()
            data = w.readframes(w.getnframes())
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(p)
            w.writeframes(data + b"\x00" * int(p.framerate * sec) * p.sampwidth * p.nchannels)
        return out.getvalue()
    except Exception as e:
        print("[pad_tail] 失败，原样返回:", type(e).__name__, flush=True)
        return wav


# 括号/星号里的心声：只显示不朗读（（…）(…)【…】[…] *…*）
_ASIDE = re.compile(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^\]]*\]|\*[^*]{1,80}\*|<\|[^|]*\|>")

# ---- 翻译层（参考 Open-LLM-VTuber 的 translate：TTS 前一步）------------------------------
# 中文聊天、日语发声：桥收到中文时先用 LLM 译成日语，再交给 TTS；模型自己写了日语行则原样使用。
# 关闭：环境变量 TTS_TRANSLATE=0。端点/模型/密钥可覆盖，默认用 Hermes 自己的 DeepSeek 凭据。
TRANSLATE_ENABLED = os.environ.get("TTS_TRANSLATE", "1").lower() not in ("0", "false", "no")
TRANSLATE_BASE = os.environ.get("TTS_TRANSLATE_BASE", "https://api.deepseek.com/v1").rstrip("/")
TRANSLATE_MODEL = os.environ.get("TTS_TRANSLATE_MODEL", "deepseek-flash")
TRANSLATE_PROMPT = (
    "你是碧蓝航线轻航母「アルビオン」的台词翻译。把下面的中文台词译成她在游戏里说台词的那种日语（用于语音朗读）：\n"
    "· 「アルビオン」是说话的人自己；要是中文说的是对方（指挥官）做了什么，就用「指揮官」当主语，别把指挥官的事安到アルビオン身上\n"
    "· 敬语但口语化：句尾常用「〜ですね」「〜ください」「〜ましょうか」「…ね？」\n"
    "· 慌或害羞时保留口吃重复（「す、すみません」），句尾可用「…」拖住\n"
    "· 娇喘/拟声（あっ、んっ、はぁ…）照写，不要译成词、不要删\n"
    "· 中文有几个句子，日语就几句：不许漏、不许合并、不许自己加说明\n"
    "· 括号里的心声原样保留；只输出译文，不要解释、不要加引号、不要罗马字。"
)
TRANSLATE_FALLBACK = os.environ.get("TTS_TRANSLATE_FALLBACK", "deepseek-v4-pro")
_SENT = re.compile(r"[^。！？!?\n]+")

# ---- 情绪提示（2026-09-28）：翻译同一次调用顺带判情绪 -------------------------------
# 背景：情绪判定原本是 `scene_plan.py` 的纯关键词表（emoji→拟声词→SHY/FLIRTY/…→calm），
# 只能认词、读不出语气，于是整段常被判成同一档。这里让**本来就要跑的那次翻译调用**顺带
# 输出一个情绪结构 —— 零额外网络往返；解析失败只丢情绪，**绝不动译文**。
EMOTION_HINT = os.environ.get("TTS_EMOTION_HINT", "1").lower() not in ("0", "false", "no")
EMOTION_PROMPT = (
    "\n\n另外，把这句话**说出来时**的情绪也判一下，和译文一起用 JSON 输出，不要输出任何其它内容：\n"
    '{"ja":"<日语译文，规则同上>","arousal":<0~1 激动程度>,"intensity":<0~1 语气强度>,'
    '"valence":<-1~1 情绪正负>,"phonation":"voiced|soft|breathy|whispered"}\n'
    "· 判的是**语气**，不是词义好坏：平静陈述 arousals 低；慌张/兴奋/生气高；耳语/气声用 whispered/breathy，正常发声用 voiced。\n"
    "· ja 必须完整、逐句对应；情绪字段拿不准就按 0.5 / voiced 给，**不许为了凑 JSON 改动或漏译正文**。"
)
_PHONATION = ("voiced", "soft", "breathy", "whispered")
_EMO_MISS = 0             # 情绪 JSON 没救回来的累计次数（退回率要看得见，别静默降级）


# 收尾引号后必须紧跟 , 或 }：模型在译文里写了没转义的 " 时，不带这条会把译文截在那个引号处（半句）
_JA_RE = re.compile(r'"ja"\s*:\s*"((?:[^"\\]|\\.)*)"(?=\s*[,}])', re.S)


_ZH_CUE = [
    (re.compile(r"（\s*(小声|低声|压低声音)[^）]*）|低声|小声"), "soft", False),
    (re.compile(r"（\s*(耳语|气声|悄悄)[^）]*）|耳语|气息"), "whisper", False),
    (re.compile(r"（\s*(喘|喘息|急促)[^）]*）|喘|气息不匀"), "breath", False),
    (re.compile(r"（\s*(害羞|羞涩|脸红)[^）]*）"), "shy", False),
    (re.compile(r"（\s*(兴奋|激动|高兴)[^）]*）"), "excite", False),
    (re.compile(r"（\s*(难过|伤心|低落)[^）]*）"), "sad", False),
    (re.compile(r"（\s*(冷漠|平静|平静地)[^）]*）"), "flat", False),
]
_ZH_EMOJI = [
    ("😭😢💧🥲", "sad"), ("😠😡💢", "excite"), ("😊😄🥰😍", "excite"),
    ("😳😖🫣", "shy"), ("❤️💕💗", "shy"), ("😶😐", "flat"),
]
_ZH_NEG = re.compile(r"(别|不要|不用|不许|不是)\s*[^，。！？]{0,4}$")


def zh_source_cues(text):
    """从**中文原文**抽结构化线索 —— 这些信息在日语译文里看不到。

    只做"看得见的明示"：括号里的短指令、emoji。不做语义解读（那是 LLM 那一路的事）。
    带否定识别（「别小声」= 反证），供融合器当**反证**用，而不是当正证据。
    """
    t = str(text or "")
    out = []
    for rx, kind, _ in _ZH_CUE:
        for m in rx.finditer(t):
            neg = bool(_ZH_NEG.search(t[max(0, m.start() - 8):m.start()]))
            out.append({"kind": kind, "negated": neg, "span": [m.start(), m.end()]})
    for chars, kind in _ZH_EMOJI:
        for ch in chars:
            if ch in t:
                out.append({"kind": kind, "negated": False})
                break
    if re.search(r"[！!]{2,}", t):
        out.append({"kind": "excite", "negated": False})
    return out


def _parse_emotion_json(txt: str):
    """从模型输出里抠出 JSON：返回 (ja, emotion|None)。抠不出就返回 ("", None)，绝不猜。

    三种输入都要扛住（复核问题 5）：模型多说一句尾巴、正文里出现花括号、输出里有多块 JSON。
    所以用 `raw_decode` 从**第一个** `{` 起解析第一个**完整**对象，而不是 find/rfind 跨块取区间。
    """
    if not txt:
        return "", None
    d = None
    for i in (m.start() for m in re.finditer(r"\{", txt[:4000])):
        try:                                  # 从每个 `{` 起试解析下一个**完整**对象
            cand, _ = json.JSONDecoder().raw_decode(txt[i:])
        except Exception:  # noqa: BLE001 - 模型偶尔多加尾巴/正文带花括号
            continue
        if isinstance(cand, dict) and ("ja" in cand or "japanese" in cand):
            d = cand
            break
    if not isinstance(d, dict):
        m = _JA_RE.search(txt)          # 结构没救回来就只抢救译文，免得白跑一次翻译
        if not m:
            return "", None
        try:
            ja = json.loads('"%s"' % m.group(1))
        except Exception:  # noqa: BLE001
            return "", None
        return (ja or "").strip(), None
    ja = str(d.get("ja") or d.get("japanese") or "").replace("\\n", "\n").strip()
    emo = {}
    for k, lo, hi in (("arousal", 0.0, 1.0), ("intensity", 0.0, 1.0), ("valence", -1.0, 1.0)):
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= float(v) <= hi:
            emo[k] = round(float(v), 3)
    ph = d.get("phonation")
    if isinstance(ph, str) and ph.strip().lower() in _PHONATION:
        emo["phonation"] = ph.strip().lower()
    return ja, (emo or None)


def to_japanese_ex(text: str, context: str = ""):
    """中文 → (日语, 情绪提示)。**与 `to_japanese` 同口径、同兜底**，只是多带一个情绪结构。

    情绪提示是"锦上添花"：拿不到就是 None，规划器会退回关键词表 + 文本先验。
    译文则要**过句数这道闸**：少句（模型为凑 JSON 漏译）视同失败，整段退回老路径重译 ——
    宁可多花一次调用，也不能让用户少听到一句话。
    """
    t = " ".join((text or "").split())
    if not t or not _SPEAKABLE.search(t):
        return "", None
    t = strip_links(t)                       # 网址/路径/代码不进翻译：模型不翻，只会自己编一句寒暄
    if not has_readable(t):                  # 剥完一个汉字、一个假名都不剩 ⇒ 整段就是那些东西，不念
        print("[skip] 去掉网址/路径后没有可念的文字，不翻译", flush=True)
        return "", None
    if is_japanese(t):                       # 日语直通：这里没有翻译调用可利用，情绪交给规则层
        return t, None
    if not TRANSLATE_ENABLED:
        return "", None
    if TRANSLATE_BACKEND == "codex":
        keys = _cc_keys()                    # 显式选择旧链时才尝试 Command Code。
        key = keys[0] if keys else ""
        model, endpoint = FALLBACK_MODEL, FALLBACK_BASE
    else:
        key = _translate_key()
        model, endpoint = TRANSLATE_MODEL, TRANSLATE_BASE
    if key and EMOTION_HINT:                 # 只在**主路**（同一次调用顺带出情绪）上开
        got = _ask(key, t, model, endpoint, prompt=TRANSLATE_PROMPT + EMOTION_PROMPT)
        ja, emo = _parse_emotion_json(got)
        if ja and _complete(t, ja):
            # 中文原文里的明示线索由**我们自己的规则**抽（确定性、不依赖模型回抄），
            # 它们在日语译文里已经看不到了（`（小声）` 会被译成日语、emoji 会被丢掉）。
            cues = zh_source_cues(t)
            if cues or emo:
                emo = dict(emo or {})
                if cues:
                    emo["src"] = cues
                print("[emotion] %s fields=%s" % (text_meta(ja), ",".join(sorted(emo))), flush=True)
                return ja, emo
        global _EMO_MISS
        _EMO_MISS += 1
        print("[emotion] 情绪 JSON 没救回来或译文缺句（累计 %d 次），退回老翻译路径" % _EMO_MISS,
              flush=True)
    ja = to_japanese(t)                      # 用剥好的文本：字数闸 `_complete` 比的也是它
    if ja and not _complete(t, ja) and len(_clauses(t)) > 1:
        print("[translate] 译文疑似漏分句（%s -> %s），逐分句重译" % (text_meta(t), text_meta(ja)), flush=True)
        ja = to_japanese(t, by_clause=True) or ja
    return ja, None


def _sent_count(text: str) -> int:
    return len([x for x in _SENT.findall(text or "") if x.strip()])


_CJK = re.compile(r"[一-鿿぀-ヿ]")
_CLAUSE = re.compile(r"[^，、；：—…,;]+")
CJK_FLOOR = 0.9           # 译文汉字+假名数 / 原文汉字+假名数 的下限：中→日假名展开，正常 ≥1.2，直通日语/代码约 1.0


def _complete(zh: str, ja: str) -> bool:
    """译文没少句、也没缩水。v2 一次只送一句，光比句数恒成立，句内漏掉的分句靠字数下限兜。"""
    return (_sent_count(ja) >= _sent_count(zh)
            and len(_CJK.findall(ja)) >= CJK_FLOOR * len(_CJK.findall(zh)))


def _clauses(text: str) -> list:
    """按逗号/顿号/冒号/破折号/省略号切分句；不足 3 个汉字的碎片（「指，」「标题」）并回上一段。"""
    out = []
    for c in (x.strip() for x in _CLAUSE.findall(text or "")):
        if not _SPEAKABLE.search(c):
            continue
        if out and len(_CJK.findall(c)) < 3:
            out[-1] += "，" + c
        else:
            out.append(c)
    return out


def _join_all(outs: list) -> str:
    """逐段译文拼回；任何一段为空就整体算失败 —— 静默滤掉那一段，用户听到的就是吞句。"""
    if not all(outs):
        print("[translate] %d/%d 段译文为空，整体按失败处理" % (sum(1 for o in outs if not o), len(outs)), flush=True)
        return ""
    return "\n".join(outs)


def _translate_key() -> str:
    key = (os.environ.get("TTS_TRANSLATE_KEY") or "").strip()
    if key:
        return key
    env = os.environ.get("HERMES_ENV_FILE", os.path.join(os.path.expanduser("~"), r"AppData\Local\hermes\.env"))
    try:
        with open(env, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


_SPEAKABLE = re.compile(r"[\w一-鿿぀-ヿ]")   # 至少有一个字/假名/数字才值得念


# ---------------- 跨句场景上下文（2026-09-21）----------------
# 后端判情绪看的是"一个请求的文字"里前后相邻的行；流式一句一请求 ⇒ 它只看得到这一句，
# 场景中间那句没信号的会掉回 calm（用户实听"很多字正常语气"）。这里把本轮**已经译好**
# 的前几句攒成 ctx 一起传下去 —— 译文是桥本来就要算的，所以零额外开销。
# ponytail: 单个滚动缓冲、不加锁（流式链路天然串行，与 _aside_state 同一假设）；
#           空闲 20s 视作新一轮并清空（跨轮粘滞会"出了场景再也回不来"，9-13 定的规矩）。
_CTX_IDLE = 20.0
_CTX_MAX = 400
_ctx_state: dict = {}


def _ctx_take(voice: str) -> str:
    """取走本句之前攒下的前文（顺手做过期清空）。"""
    st = _ctx_state.get(voice)
    now = time.time()
    if not st or now - st["since"] > _CTX_IDLE:
        st = {"text": "", "since": now}
        _ctx_state[voice] = st
    st["since"] = now
    return st["text"]


def _ctx_put(voice: str, ja: str) -> None:
    """把本句的日语（原文或译文）追加进滚动缓冲，只留尾部。"""
    ja = " ".join((ja or "").split())
    if not ja:
        return
    st = _ctx_state.setdefault(voice, {"text": "", "since": time.time()})
    st["text"] = (st["text"] + ja)[-_CTX_MAX:]


def to_japanese(text: str, by_clause: bool = False) -> str:
    """中文 → 日语（已有假名则原样返回）。失败时返回空串，让调用方走静音兜底。

    `by_clause`：按分句各译一次（整句译文漏了分句时的补救，见 `to_japanese_ex`）。
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    if not _SPEAKABLE.search(text):
        # 壳按标点切段，会切出「…」「——」这种纯符号段；送进日语引擎会变成十几秒的长音
        # （实测「…」= 13.2s / RMS 0.148 / 只有 20% 静音），听感就是句中一段莫名其妙的长停顿。
        # 返回空串 → 调用方回 0.05s 静音，不占合成时间。
        print("[skip] 纯符号段，不合成", flush=True)
        return ""
    text = strip_links(text)        # 网址/路径/代码不念（2026-10-25）：模型不翻，会给它们编一句寒暄
    if not has_readable(text):      # 剥完一个字都不剩 ⇒ 整段就是链接/路径/英文残片
        print("[skip] 去掉网址/路径后没有可念的文字，不合成", flush=True)
        return ""
    if is_japanese(text):
        return text
    if not TRANSLATE_ENABLED:
        return ""
    key = _translate_key()          # codex 后端不需要它；只有退回 HTTP 那条时才要
    parts = _clauses(text) if by_clause else [x.strip() for x in _SENT.findall(text) if x.strip()]
    if TRANSLATE_BACKEND == "codex":
        # 2026-09-24 指挥官定：主路 = Command Code 直连的 ds-v4.1（关思考，实测 ~2s）；
        # codex(GPT) 退为退路。分支名保留 "codex"（历史包袱），语义以本行为准。
        def one(seg: str) -> str:
            # 三重回退（2026-10-02 指挥官定）：HTTP 退路里先 key#1 再 key#2，最后 codex luna。
            # 额度用完的 key 在冷却期内直接跳过：白打一次 400 要 1~2s，
            # 情绪路径 + 老路径各打两次就把 prepare 的 10s 吃光了（2026-10-14 中文整句哑掉）
            keys = _cc_keys()
            for i, fk in enumerate(keys):
                got = _ask(fk, seg, FALLBACK_MODEL, FALLBACK_BASE)
                if got:
                    return got
                if i == len(keys) - 1:
                    print("[translate] Command Code 的 key 都失败，退 codex luna", flush=True)
            if not keys:
                print("[translate] 没有可用的 Command Code key（没拿到或额度用完冷却中），直接走 codex", flush=True)
            if not CODEX_API_FALLBACK:
                return ""
            return _ask_codex(seg)

        segs = parts if len(parts) > 1 else [text]
        with ThreadPoolExecutor(max_workers=2) as ex:
            outs = list(ex.map(one, segs))
        ja = _join_all(outs) if len(segs) > 1 else (outs[0] if outs else "")
    elif not key:
        print("[translate] 没拿到 API key，跳过翻译", flush=True)
        return ""
    elif len(parts) > 1:      # 逐句译：多句一次送，模型会整句漏掉（朗读就是漏台词）
        with ThreadPoolExecutor(max_workers=4) as ex:
            outs = list(ex.map(lambda x: _ask(key, x, TRANSLATE_MODEL), parts))
        ja = _join_all(outs)
    else:
        ja = _ask(key, text, TRANSLATE_MODEL)
    if ja:
        print(f"[{time.strftime('%H:%M:%S')}] [translate] {_sent_count(text)}句->{_sent_count(ja)}句 source={text_meta(text)} target={text_meta(ja)}", flush=True)
    return ja


# 2026-09-20：翻译腿关掉思维链。实测同一句、同一模型，译文逐字相同，耗时 1.71s → 0.67s。
# （deepseek-flash 是带思考的模型，思考过程纯属浪费：它先用英文想半天再吐译文。）
# 关不掉时自动退回不带的版本，所以后端换了也不会坏。TTS_TRANSLATE_NOTHINK=0 可关掉本优化。
TRANSLATE_NOTHINK = os.environ.get("TTS_TRANSLATE_NOTHINK", "1").lower() not in ("0", "false", "no")

# ---- 翻译腿后端：codex 是历史分支名，主路为 Command Code，Codex CLI 仅作最终退路 --------
# 显式 TTS_TRANSLATE_BACKEND=api 才使用需要独立 API key 的 HTTP 线路。
TRANSLATE_BACKEND = os.environ.get("TTS_TRANSLATE_BACKEND", "codex").strip().lower()
CODEX_BIN = os.environ.get("TTS_TRANSLATE_CODEX_BIN", r"D:\AI\codex\bin\codex.cmd")
CODEX_MODEL = os.environ.get("TTS_TRANSLATE_CODEX_MODEL", "gpt-6-luna")
CODEX_TIMEOUT = float(os.environ.get("TTS_TRANSLATE_CODEX_TIMEOUT", "90"))
# 关思考：实测 none 被 codex 接受、译文与 low 逐字相同（minimal 会被拒）。翻译不需要推理。
CODEX_EFFORT = os.environ.get("TTS_TRANSLATE_CODEX_EFFORT", "none")
# 失败冷却：白试一次要 ~30s（限额得等它回错）⇒ 失败后这段时间直接跳过 codex 走退路。
CODEX_COOLDOWN = float(os.environ.get("TTS_TRANSLATE_CODEX_COOLDOWN", "600"))
_codex_down_until = 0.0
# codex 挂了就退回 HTTP 那条（否则她直接哑掉）。不想花钱可设 TTS_TRANSLATE_API_FALLBACK=0
CODEX_API_FALLBACK = os.environ.get("TTS_TRANSLATE_API_FALLBACK", "1").lower() not in ("0", "false", "no")
# 退路端点：本机 commandcode 代理 + ds-v4.1（2026-09-23 指挥官定：codex 被限额就走它）。
# 实测 :3050 一句 1.6~2.6s；:3051 那把周额度用尽（09-25 重置）故不设它。
FALLBACK_BASE = os.environ.get("TTS_TRANSLATE_FALLBACK_BASE", "https://api.commandcode.ai/provider/v1").rstrip("/")
# 退路走 Command Code 线路，key#1 先、key#2 后。
FALLBACK_MODEL = os.environ.get("TTS_TRANSLATE_FALLBACK_MODEL", "deepseek/deepseek-v4.1-flash")
_FALLBACK_KEY: list[str] = []


def _fallback_key() -> str:
    """退路那把 key（key#1）：优先环境变量；否则从 hermes 配置里读（同一把 key 不复制到别处、不打印）。"""
    if not _FALLBACK_KEY:
        k = (os.environ.get("TTS_TRANSLATE_FALLBACK_KEY") or "").strip()
        if not k:
            try:
                # 2026-09-24：反代已删，key 改为从本机密钥文件读（不复制到别处、不打印）
                p = os.environ.get("TTS_TRANSLATE_KEY_FILE", r"D:\AI\_secrets\command code.txt")
                k = open(p, encoding="utf-8-sig").read().strip()
            except Exception as e:   # noqa: BLE001
                print(f"[translate] 读退路 key 失败：{type(e).__name__}", flush=True)
        if not k:
            return ""                # 没读到不缓存：下次再读，免得一次失败让这把 key 缺席到重启
        _FALLBACK_KEY.append(k)
    return _FALLBACK_KEY[0]


_FALLBACK_KEY2: list[str] = []


def _fallback_key2() -> str:
    """第二把 key（cc-daily-2，2026-10-02）：key#1 限额时先用它，再不行才落 codex luna。"""
    if not _FALLBACK_KEY2:
        k = (os.environ.get("TTS_TRANSLATE_FALLBACK_KEY2") or "").strip()
        if not k:
            try:
                env = os.environ.get("HERMES_ENV_FILE", os.path.join(os.path.expanduser("~"), r"AppData\Local\hermes\.env"))
                with open(env, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("HERMES_CUSTOM_COMMAND_CODE_DAILY_2_API_KEY="):
                            k = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            except OSError as e:
                print(f"[translate] 读 key#2 失败：{type(e).__name__}", flush=True)
        if not k:
            return ""                # 同 key#1：没读到不缓存
        _FALLBACK_KEY2.append(k)
    return _FALLBACK_KEY2[0]


_KEY_DOWN: dict[str, float] = {}   # key -> 冷却到什么时候（额度用完时设）
try:
    CREDIT_COOLDOWN = float(os.environ.get("TTS_TRANSLATE_CREDIT_COOLDOWN") or 1800)
except ValueError:                   # 写错了不该让桥起不来
    CREDIT_COOLDOWN = 1800.0


def _cc_keys() -> list[str]:
    """Command Code 的 key（#1、#2），去掉没拿到的和额度用完还在冷却的。"""
    now = time.time()
    out: list[str] = []
    for k in (_fallback_key(), _fallback_key2()):
        if k and k not in out and _KEY_DOWN.get(k, 0.0) <= now:
            out.append(k)
    return out


def emotion_degraded() -> bool:
    """情绪翻译主路是否不可用（没有可用 key，如两把都在额度冷却）：此时译文照出，情绪提示整段跳过。"""
    if not (TRANSLATE_ENABLED and EMOTION_HINT):
        return False                         # 主动关掉不算降级
    return not (_cc_keys() if TRANSLATE_BACKEND == "codex" else _translate_key())


_CODEX_TIMES: list[float] = []
_CODEX_MISSING_LOGGED = False
_CODEX_MISSING_LOCK = threading.Lock()


def _codex_available() -> bool:
    if os.path.isfile(CODEX_BIN):
        return True
    global _CODEX_MISSING_LOGGED
    with _CODEX_MISSING_LOCK:
        if not _CODEX_MISSING_LOGGED:
            print("[translate] Codex 可执行文件不存在，跳过最终退路", flush=True)
            _CODEX_MISSING_LOGGED = True
    return False


def _ask_codex_oneshot(text: str) -> str:
    """兜底：一次性起 codex CLI（常驻进程挂了才走这条，13~17s）。失败返回空串。"""
    if not _codex_available():
        return ""
    try:
        p = subprocess.run(
            [CODEX_BIN, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
             "-m", CODEX_MODEL, "-c", f"model_reasoning_effort={CODEX_EFFORT}", "-"],
            input=f"{TRANSLATE_PROMPT}\n\n{text}\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CODEX_TIMEOUT,
            cwd=os.environ.get("TEMP") or None,
        )
    except Exception as e:   # noqa: BLE001 — 翻译腿出问题绝不能把主链路打挂
        print(f"[translate] codex 一次性调用失败：{type(e).__name__}", flush=True)
        return ""
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        last = ((p.stderr or "").strip().splitlines() or [""])[-1]
        print(f"[translate] codex 一次性返回异常 rc={p.returncode} {text_meta(last)}", flush=True)
        return ""
    return out


class _CodexWarm:
    """常驻 codex app-server：一个进程吃下所有翻译请求。

    一次性 `codex exec` 每句 13~17s，大头是「起进程 + 刷新可用模型那次网络请求」；
    常驻后同一件事 **2.1s**（首句 7s 含建连）。每句开新 thread（避免上下文互相污染），进程复用。

    ponytail: 全局串行（一把锁）。翻译本来就 2s 量级，串行的代价远小于交错读通知流的复杂度；
    真要并行，把 pending 按 threadId 分桶、单读线程分发即可。
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.seq = 0
        self.thread = ""
        self._lines = None
        self._reader = None

    def _start_reader(self):
        """管道读取放到单独线程；请求线程用 Queue 超时，避免 readline 永久持锁。"""
        proc = self.proc
        lines = self._lines = queue.Queue()

        def read():
            try:
                for line in proc.stdout:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)
                proc.stdout.close()

        self._reader = threading.Thread(target=read, name="tts-codex-output", daemon=True)
        self._reader.start()

    def _readline(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("codex app-server 输出超时")
        try:
            line = self._lines.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("codex app-server 输出超时") from None
        if line is None:
            raise RuntimeError("codex app-server 输出断了")
        return line

    # -- 内部：起进程并握手（initialize + thread/start）-----------------------------------
    def _boot(self) -> bool:
        if not _codex_available():
            return False
        try:
            self.proc = subprocess.Popen(
                [CODEX_BIN, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=os.environ.get("TEMP") or None,
            )
            self._start_reader()
        except Exception as e:   # noqa: BLE001
            print(f"[translate] 起 codex app-server 失败：{type(e).__name__}", flush=True)
            self._kill()
            return False
        try:
            self._call("initialize", {"clientInfo": {"name": "albion-bridge", "version": "1.0"},
                                      "capabilities": None}, 30)
            self._notify("initialized", {})
            r = self._call("thread/start", {
                "model": CODEX_MODEL, "cwd": os.environ.get("TEMP"),
                "sandbox": "read-only", "approvalPolicy": "never",
                "config": {"model_reasoning_effort": CODEX_EFFORT},
                "ephemeral": True,          # 翻译用完即弃，别在磁盘上堆会话
                "baseInstructions": "你只做中日翻译，只输出译文，不要解释、不要使用任何工具。",
                }, 60)
            self.thread = (r or {}).get("result", {}).get("thread", {}).get("id", "") or ""
        except Exception as e:   # noqa: BLE001
            print(f"[translate] codex app-server 握手失败：{type(e).__name__}", flush=True)
            self._kill()
            return False
        print(f"[translate] codex 常驻会话就绪（model={CODEX_MODEL} effort={CODEX_EFFORT}）", flush=True)
        if not self.thread:
            self._kill()
            return False
        return True

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        reader, self._reader = self._reader, None
        self.thread = ""
        if proc:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if reader is not None:
                reader.join(timeout=1)
            elif proc.stdout:
                proc.stdout.close()
        self._lines = None

    def _notify(self, method: str, params: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()

    def _call(self, method: str, params: dict, timeout: float) -> dict | None:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.seq += 1
        rid = self.seq
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                          "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            line = self._readline(deadline)
            try:
                msg = json.loads(line)
            except Exception:   # noqa: BLE001
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    print(f"[translate] {method} 报错：{text_meta(json.dumps(msg['error']))}", flush=True)
                    return None
                return msg

    # -- 对外 ---------------------------------------------------------------------------
    def translate(self, text: str) -> str:
        with self.lock:
            if not self.proc or not self.thread:
                if not self._boot():
                    return ""
            try:
                assert self.proc and self.proc.stdin and self.proc.stdout
                self.seq += 1
                rid = self.seq
                # 每句一个新 thread：省得上一句的上下文影响这一句
                r = self._call("thread/start", {
                    "model": CODEX_MODEL, "cwd": os.environ.get("TEMP"),
                    "sandbox": "read-only", "approvalPolicy": "never",
                    "config": {"model_reasoning_effort": CODEX_EFFORT},
                    "ephemeral": True,          # 翻译用完即弃，别在磁盘上堆会话
                    "baseInstructions": "你只做中日翻译，只输出译文，不要解释、不要使用任何工具。",
                }, 60)
                tid = (r or {}).get("result", {}).get("thread", {}).get("id", "") or ""
                if not tid:
                    return ""
                self.proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": rid, "method": "turn/start",
                    "params": {"threadId": tid, "input": [{"type": "text", "text": f"{TRANSLATE_PROMPT}\n\n{text}"}]},
                }) + "\n")
                self.proc.stdin.flush()

                chunks: list[str] = []
                final = ""
                deadline = time.monotonic() + CODEX_TIMEOUT
                while True:
                    line = self._readline(deadline)
                    try:
                        msg = json.loads(line)
                    except Exception:   # noqa: BLE001
                        continue
                    meth = msg.get("method", "")
                    if not meth:
                        if msg.get("id") == rid and "error" in msg:
                            raise RuntimeError("codex turn/start 失败")
                        continue
                    params = msg.get("params") or {}
                    if params.get("threadId") not in (None, tid):
                        continue                      # 别的 thread 的事件，跳过
                    if meth == "item/agentMessage/delta":
                        chunks.append(params.get("delta", ""))
                    elif meth == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage" and item.get("text"):
                            final = item["text"]
                    elif meth in ("turn/failed", "error"):
                        raise RuntimeError("codex 翻译回合失败")
                    elif meth == "turn/completed":
                        if (params.get("turn") or {}).get("status") in ("failed", "interrupted"):
                            raise RuntimeError("codex 翻译回合未完成")
                        break
                out = (final or "".join(chunks)).strip()
                if not out:
                    print("[translate] 常驻会话没吐出译文，改走一次性 CLI", flush=True)
                    self._kill()
                    return _ask_codex_oneshot(text)
                return out
            except Exception as e:   # noqa: BLE001
                print(f"[translate] 常驻会话异常（{type(e).__name__}），改走一次性 CLI", flush=True)
                self._kill()
                return _ask_codex_oneshot(text)


_CODEX_WARM = _CodexWarm()


def _ask_codex_once(text: str) -> str:
    """把一句中文丢给 codex 翻成日语：优先常驻会话（~2s），挂了退回一次性 CLI（~15s）。"""
    t0 = time.time()
    out = _CODEX_WARM.translate(text)
    if out:
        _CODEX_TIMES.append(time.time() - t0)
    return out


def _ask_codex(text: str) -> str:
    """带冷却：被限额/挂掉后别再每句白试一次（实测白试一次要 ~30s）⇒ 冷却期内直接走退路。"""
    global _codex_down_until
    if not _codex_available():
        return ""
    if time.time() < _codex_down_until:
        return ""
    out = _ask_codex_once(text)
    if not out:
        _codex_down_until = time.time() + CODEX_COOLDOWN
        print(f"[translate] codex 没成 ⇒ 冷却 {CODEX_COOLDOWN:.0f}s（期间直接走退路）", flush=True)
        _tlog(f"codex luna 失败(冷却{CODEX_COOLDOWN:.0f}s) source={text_meta(text)}")
    return out


def _tlog(msg: str):
    """翻译腿失败原因落盘：桥控制台随 bat 窗口走、关了就没（2026-10-03 empty_translation 无从溯源）。"""
    _file_log("_translate_err.log", msg)


def _ask(key: str, text: str, model: str, endpoint: str | None = None,
         prompt: str | None = None) -> str:
    """翻译主路：走 **Responses API**。

    2026-09-24 实测（Command Code 这个第三方端点）：
      · Chat Completions 那条路【不认】关思考参数 —— thinking:{type:disabled} 与
        enable_thinking:false 都被忽略，reasoning 照吐 3000~17000 字，每句 10~30s。
      · Responses 这条路认：reasoning:{"effort":"none"} ⇒ 实测 1.6~2.7s、reasoning 0 字。
      · 旧 :3050 反代之所以快，是它走 /alpha/generate 且把思考参数丢了（那条路现已 403）。
    """
    base = {"model": model, "store": False,
            "input": [{"role": "system", "content": prompt or TRANSLATE_PROMPT},
                      {"role": "user", "content": text}]}
    variants = ([dict(base, reasoning={"effort": "none"}), base]
                if TRANSLATE_NOTHINK else [base])
    last = ""
    for i, payload in enumerate(variants):
        req = urllib.request.Request(f"{endpoint or TRANSLATE_BASE}/responses",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}",
                                              "User-Agent": "cli"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode("utf-8"))
            txt = ""
            for item in (out.get("output") or []):
                if item.get("type") == "message":
                    for c in (item.get("content") or []):
                        if c.get("type") in ("output_text", "text"):
                            txt += c.get("text") or ""
            txt = txt.strip().strip('"')
            if txt:
                return txt
            last = "空译文"
        except Exception as e:  # noqa: BLE001 - 翻译失败不该让整条链路 500
            last = type(e).__name__
            # 只记 "HTTP Error 400: Bad Request" 查不出原因（2026-10-14 两把 key 都 400）：带上服务端的错误体，去掉 key
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = e.read().decode("utf-8", "replace").replace(key, "***")
                    last += " status=%d detail=%s" % (e.code, text_meta(detail))
                except Exception:  # noqa: BLE001
                    detail = ""
                if "insufficient credits" in detail.lower():
                    # 额度用完：换个参数重试也没用，这把 key 冷却一段时间不再打
                    _KEY_DOWN[key] = time.time() + CREDIT_COOLDOWN
                    _tlog(f"{model} key 额度用完，冷却 {CREDIT_COOLDOWN:.0f}s")
                    print(f"[translate] 这把 key 额度用完，冷却 {CREDIT_COOLDOWN:.0f}s", flush=True)
                    break
            _tlog(f"{model} source={text_meta(text)} : {last}")
        if i == 0 and TRANSLATE_NOTHINK:
            print("[translate] 关思考那次没成，退回原样重试", flush=True)
    print(f"[translate] {model} 失败：{last}", flush=True)
    _tlog(f"{model} 最终失败 source={text_meta(text)} : {last}")
    return ""


# AIRI 等前端从 http://localhost:5174 调本桥 → 必须放行跨域（本地开发服务，全放开）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 心声/标记的剥离：跨请求状态机 ----------------------------------------
# 应用端是**逐 token 流式**喂文本、再按句切分请求，所以一对括号经常被切到两个请求里
# （日志实测：'（肩膀轻轻缩了缩，' / '）' / '让您戳。' 各是一个请求）。
# 单请求内做正则匹配治不了，这里用一个跨请求的小状态机：进入括号就丢弃，直到闭合。
_OPENERS = {"（": "）", "(": ")", "【": "】", "[": "]", "<|": "|>"}
_ASIDE_TIMEOUT = 15.0
_aside_state = {"open": False, "closer": "", "since": 0.0}


def strip_asides(text: str, now: float | None = None) -> str:
    """内心 OS / 标记不朗读；括号被切碎也认得出（应用端仍显示全文）。

    ponytail: 跨请求状态机 + 30 秒超时。够用且不会把她变哑巴——丢一次闭合括号
    最坏也就是少念一句；真要根治应该改 app 端在切句之前剥离（见 PLAN 第 5 条）。
    """
    st = _aside_state
    now = time.monotonic() if now is None else now
    if st["open"] and now - st["since"] > _ASIDE_TIMEOUT:
        st.update(open=False, closer="", since=0.0)
    out, i, n = [], 0, len(text or "")
    while i < n:
        if st["open"]:
            j = text.find(st["closer"], i)
            if j < 0:
                st["since"] = now
                return "".join(out)
            i = j + len(st["closer"])
            st.update(open=False, closer="", since=0.0)
            continue
        best = None
        for op, cl in _OPENERS.items():
            k = text.find(op, i)
            if k >= 0 and (best is None or k < best[0]):
                best = (k, op, cl)
        if best is None:
            out.append(text[i:])
            break
        k, op, cl = best
        out.append(text[i:k])
        j = text.find(cl, k + len(op))
        if j < 0:
            # 只有"整段以括号开头"才当心声碎片跨请求记住；正文中间冒出的半个括号
            # （打字错误、颜文字）一律只丢到本段末尾，避免把状态机卡住导致后面全哑。
            if not text[:k].strip():
                st.update(open=True, closer=cl, since=now)
            return "".join(out)
        i = j + len(cl)
    return "".join(out)


def extract_speech_text(text: str) -> str:
    """只保留含假名的行（双语消息里挑日语行朗读），去空行；括号里的心声不朗读。"""
    ja_lines = []
    for line in strip_asides(text).splitlines():
        line = line.strip()
        if line and is_japanese(line):
            ja_lines.append(line)
    return "\n".join(ja_lines) if ja_lines else ""


# 逐句流式：前端按标点切句，会把一句心声切成「（冷不防被摸了头，」「肩膀缩了一下…」「）那个…」
# 三个请求——单看一个请求看不到成对的括号，就会把心声念出来。用一个小状态机跨请求记住括号深度。
# ponytail: 进程内单计数器（本地单用户、请求串行）；多路并发聊天要改成按 voice/会话分桶。

# 别把尖括号当"心声括号"：AIRI 上层会把 <|ACT:{...}|> 这类标记抽掉，
# 那个 "<" 可能单独到、配对的 ">" 永远不来 ⇒ 深度再也归不了零 ⇒
# 之后每一句都被当成心声吞掉（2026-09-17：22:03 起没字幕、没语音就是这个）。
_ASIDE_DEPTH = 0
_ASIDE_DEPTH_SINCE = 0.0
_ASIDE_DEPTH_TIMEOUT = 15.0


def strip_asides_streaming(text: str) -> str:
    """跨请求剥掉括号内容：括号内的片段返回空串（不朗读），括号后的正文照常留下。"""
    global _ASIDE_DEPTH, _ASIDE_DEPTH_SINCE
    now = time.monotonic()
    # 兜底：卡住超过 15 秒就清零（正常一句话不可能跨这么久）
    if _ASIDE_DEPTH and now - _ASIDE_DEPTH_SINCE > _ASIDE_DEPTH_TIMEOUT:
        _ASIDE_DEPTH = 0
        _ASIDE_DEPTH_SINCE = 0.0
    out = []
    for ch in text:
        if ch in "（([【":
            _ASIDE_DEPTH += 1
            _ASIDE_DEPTH_SINCE = now
        elif ch in "）)]】":
            _ASIDE_DEPTH = max(0, _ASIDE_DEPTH - 1)
        elif _ASIDE_DEPTH == 0:
            out.append(ch)
    if _ASIDE_DEPTH == 0:
        _ASIDE_DEPTH_SINCE = 0.0
    return "".join(out).strip()


def silent_wav(seconds: float = 0.05) -> bytes:
    """合法的小段静音 wav，防止朗读流水线因空文件中断。"""
    rate, n = 24000, int(24000 * seconds)
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + n * 2, b"WAVE",
                      b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", n * 2)
    return hdr + b"\x00\x00" * n


def synth_denia(text: str) -> bytes:
    speech = " ".join((text or "").split())
    if not speech:
        return silent_wav()
    q = urllib.parse.urlencode({
        "text": speech, "text_lang": "zh", "ref_audio_path": DENIA_REF,
        "prompt_text": DENIA_PROMPT, "prompt_lang": "zh",
        "text_split_method": "cut5", "batch_size": "1",
        "media_type": "wav", "streaming_mode": "false"})
    try:
        with urllib.request.urlopen(f"{GSV_TTS}?{q}", timeout=600) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"denia {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")


def synth(text: str, voice: str, already_stripped: bool = False):
    if voice == "denia":
        return synth_denia(text), "denia"
    # 逐句流式：先跨请求剥掉心声，再挑朗读行。
    # already_stripped=True 时调用方已经在主线程按"到达顺序"剥过了——
    # 这段有跨请求状态机，必须保持顺序，不能丢进线程池里乱序执行。
    raw, text = text, (text if already_stripped else strip_asides_streaming(text))
    if not text and raw.strip() and "".join(raw.split()):
        print("[aside] 心声片段，不朗读", flush=True)
    speech = extract_speech_text(text)
    if not speech:
        speech = to_japanese(text)                 # 中文聊天也能出声：先译成日语再合成
    if not speech:
        return silent_wav(), ""
    ctx = _ctx_take(voice)                         # 本句之前的已译句子（跨句场景上下文）
    _ctx_put(voice, speech)
    payload = {"text": speech, "voice": voice, "ctx": ctx}
    req = urllib.request.Request(BACKEND, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read(), r.headers.get("X-Irodori-Styles", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"tts {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")


def synth_sse(text: str, voice: str, fmt: str = "mp3"):
    """把后端 /tts_stream 的 SSE 原样透传（真流式：调用方边收边放）。

    与 synth() 同规则：只念含假名的行；无日语行 → 只发一个 done，不产生音频。
    """
    text = strip_asides_streaming(text)
    speech = extract_speech_text(text)
    if not speech:
        speech = to_japanese(text)   # 同上：中文 → 日语
    if not speech:
        yield b'event: done\ndata: {"chunks": 0, "reason": "no-japanese-line"}\n\n'
        return
    _ctx_put(voice, speech)
    payload = {"text": speech, "voice": voice, "format": fmt, "ctx": _ctx_take(voice)}
    req = urllib.request.Request(BACKEND_STREAM, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        while True:                 # 逐块转发，不攒整条
            buf = r.read(4096)
            if not buf:
                break
            yield buf


@app.get("/v1/models")
async def models():
    """给 AIRI 这类前端做 provider 校验/模型下拉用：五船当成模型名。"""
    ships = VOICES
    return {"object": "list",
            "data": [{"id": v, "object": "model", "owned_by": "irodori-tts"} for v in ships]}


@app.post("/v1/audio/speech")
async def audio_speech(request: Request):
    body = await request.json()
    text = (body.get("input") or "").strip()
    voice = (body.get("voice") or DEFAULT_VOICE).strip().lower().replace("-irodori", "")
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] [req] voice={voice if voice in VOICES else 'unknown'} {text_meta(text)}", flush=True)
    _file_log("_breq.log", f"voice={voice if voice in VOICES else 'unknown'} sse={'event-stream' in (request.headers.get('accept') or '').lower()} {text_meta(text)}")
    if not text:
        return Response(status_code=400, content="missing input")
    if voice not in VOICES:
        return Response(status_code=404,
                        content=f"unknown voice '{voice}'; available: {', '.join(VOICES)}")
    accept = (request.headers.get("accept") or "").lower()
    wants_sse = "text/event-stream" in accept or str(body.get("stream_format", "")).lower() == "sse"
    if wants_sse:
        if voice == "denia":
            return Response(status_code=400, content="denia(GSV) 不支持流式，去掉 stream_format/Accept 即可")
        fmt = str(body.get("response_format", "mp3")).lower()
        return StreamingResponse(synth_sse(text, voice, fmt if fmt in ("mp3", "wav") else "mp3"),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Irodori-Ship": voice})
    # 2026-09-17：把"翻译(走网络) + 合成(走 GPU)"挪出事件循环。
    # 原来 async def 里同步阻塞 ⇒ 一个请求占死整个循环 ⇒ 后一句的翻译没法跟前一句的合成重叠
    # （两个不同资源，本该并行）⇒ 播放只能干等 ⇒ 每句之间空档 1.5~2 秒。
    # 剥心声有跨请求状态机，放在主线程按到达顺序做。
    try:
        prepared = strip_asides_streaming(text)
        wav, styles = await asyncio.to_thread(synth, prepared, voice, True)
    except Exception as e:
        return Response(status_code=502, content=f"tts failed: {e}")
    hdrs = {"X-Irodori-Styles": styles} if styles else None
    return Response(content=_pad_tail(wav), media_type="audio/wav", headers=hdrs)


# ── v2（P8）：旧 /v1/* 一行未改，只在其上新增 /v2/prepare、/v2/health ──
#    （v2 不读写本模块的进程级全局 _ASIDE_DEPTH / _ctx_state：心声剥离已在会话解析层做完）
import bridge_v2                                                        # noqa: E402
bridge_v2.install(app, translate=to_japanese, translate_ex=to_japanese_ex,
                  cues=zh_source_cues, emotion_degraded=emotion_degraded)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9881, access_log=False)
