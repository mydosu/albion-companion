"""S 的新实现（P4）：FastAPI WS + 会话协调器。

壳那侧的契约**一个字都不改**（`shell/packages/stage-ui/src/libs/speech/streaming-pipeline.ts`）：

    路径：/api/v1/audio/speech/ws（旧实现接受任意路径，两个都挂）
    上行：{"event":"start",model,voice,response_format,extra_body?} / {"event":"text","text"} /
          {"event":"finish"} / {"event":"cancel"}
    下行：{"event":"session.started"} → 每单元 {"event":"sentence.start","payload":{"text"}}
          → 若干二进制音频帧 → {"event":"sentence.end","payload":{"text"}}
          → {"event":"session.finished"} ；致命错误 {"event":"error","code","message"}

本模块只做"协调"：切句交给 `session_text`（P1）、翻译交给桥 `/v2/prepare`（P2）、
场景/风格交给 I `/v2/plan`（P3a）、音频交给 I `/v2/render`（P3b/c，全局单 GPU 队列）。
读循环始终活着，所以 cancel/finish 在合成期间也进得来（方案 §3.3 的硬要求）。
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import hashlib
import logging
import math
import os
import re
import sys
import time
import uuid
from collections import deque

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

import gap_shaper as GS
import session_text as ST

WS_PATHS = ("/api/v1/audio/speech/ws", "/v1/audio/speech/ws")   # 壳用前者；后者兼容旧测试脚本
MAX_UNITS = 64               # 方案 §3.4：每会话未提交单元上限
MAX_CHARS = 16384            # 方案 §3.4：每会话原文上限（code point）
MAX_DOWN_BYTES = 16 * 2 ** 20   # 待下行音频总字节
MAX_PAYLOAD = 8 * 2 ** 20       # 单个 WAV/SSE payload
TICK_S = 0.20                # 空闲冲刷的检查间隔（切句器自己的 idle_window 是 0.8s）
PROTOCOL_VERSION = 2         # 本机 v2：版本不匹配一律**明确失败**，不静默跑半新半旧
EMOTION_CTX_CHARS = 400      # 送进规划器的前文上限（≈2 个自然句；够判"这句没信号该继承谁"）
CAPABILITIES = ["display.span", "unit.no_audio", "unit.failed", "playback.credit",
                "playback.ack", "session.counts"]


def log(*a):
    """运维要看得到：协调器不能把异常吞成"没声音"。"""
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), "[stream_v2]", *a, file=sys.stderr, flush=True)


def text_meta(text):
    text = str(text)
    return "len=%d sha256=%s" % (len(text), hashlib.sha256(text.encode("utf-8")).hexdigest())


_QUERY_TOKEN = re.compile(r"([?&]token=)[^&\s\"'#]+")


class RedactQueryToken(logging.Filter):
    """uvicorn 的 WS 握手日志走 `uvicorn.error` 并打印带 query 的路径，`access_log=False` 拦不住；
    壳把 token 放在 query 里，这里把值换成 `<TOKEN>` 再落盘。"""

    def filter(self, record):
        msg = record.getMessage()
        redacted = _QUERY_TOKEN.sub(r"\1<TOKEN>", msg)
        if redacted != msg:
            record.msg, record.args = redacted, ()
        return True


def redact_uvicorn_logs():
    """必须在 uvicorn 配好日志之后调用（`uvicorn.Config` 构造时会重配 logger）。"""
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactQueryToken) for f in logger.filters):
            logger.addFilter(RedactQueryToken())


class BadProtocol(Exception):
    """壳侧协议版本不对：fail fast，让指挥官把整套切回配套版本（方案 §4.1）。"""


class StalledCredit(Exception):
    """限流闸门等到 `credit_wait` 还没拿到任何播放进度：砍掉这个单元，不继续灌音频。"""


def credit_patience(credit_wait, end_wall, sent_s, acked_s, last_move):
    """ack 静默多久才算「卡死」= 静默上界 + `credit_wait`。

    上界取两者较大（codex 复核第 3 条：**有效 ack 来了就要重新锚定**）：
      ① `end_wall`：已写出音频按「上一段播完接着播、收到晚就从收到那刻起播」折出来的墙钟终点
         —— 把服务端自己的渲染空档算进去（客户端那段时间在等音频，本来就不会有 ack）；
      ② 上次 ack 时刻 + 还没播完的音频秒数（`sent_s - acked_s`）—— 用真实进度兜底，
         覆盖客户端自己的解码/起播延迟与暂停。

    壳只在**一段播完**时才 ack ⇒ 一段十几秒在播期间一个 ack 都没有，这是必然静默、不是卡死。
    老判据只看「`credit_wait`(5s) 内没有播放进度」就砍单元 ⇒ 该单元无音频 ⇒ 后半句整句没声
    （2026-09-30「漏句」；当天 33 次 `FAILED stage=credit`，现场 `已发 20.24s/已播 3.28s/差额 16.96s`）。

    注：早期「按差额必然低估」的说法来自一个**口径错的探针**（时长多算一倍，见 `_tmp/credit_repro.py`
    docstring），不能当证据。两个上界取 max 只是比任一模型都保守，**不等于「只会多等、不会误杀」**：
    客户端长暂停（或干脆不 ack）而上界已过时，仍会按 `credit_wait` 判失败 —— 这是有意留的兜底。
    另：终点是在**排程写出时**折的，不是「帧真正落 socket 之后」，慢 writer 场景未验收（codex 复核第 2 条）。
    """
    bound = float(last_move) + max(0.0, float(sent_s) - float(acked_s))
    if end_wall is not None:
        bound = max(bound, float(end_wall))
    return float(credit_wait) + max(0.0, bound - float(last_move))


class StageError(RuntimeError):
    """带阶段的失败：让 render_unit 的兜底 except 报对 stage（默认 render）。

    现在整条流水线（prepare/plan/渲染）共用一个 try，plan 的 400 会被记成「渲染失败」，
    排障时被 stage 标签带偏（2026-09-30 实测：真因在 plan，日志写的是 render）。
    """

    def __init__(self, stage, msg):
        super().__init__(msg)
        self.stage = stage


class Config:
    """运行时配置。**用到的值都要能报出来**（方案 §3.4：配置可调但必须记录使用值）。"""

    def __init__(self, **kw):
        self.bridge_url = kw.get("bridge_url") or os.environ.get("BRIDGE_URL", "http://127.0.0.1:9881")
        self.irodori_url = kw.get("irodori_url") or os.environ.get("IRODORI_URL", "http://127.0.0.1:9889")
        self.voices = list(kw.get("voices") or ("albion", "taihou", "shinano", "brest", "jinshi"))
        self.default_voice = kw.get("default_voice") or self.voices[0]
        self.prepare_timeout = kw.get("prepare_timeout", 60.0)    # 实测翻译 13–43s，所有单元留足余量
        # 单单元总预算（含一次后备）。**真机实测（P7，2026-09-28）**：
        #   暖机 30 个单元：prepare 3.0–6.4s + gpu_send 1.8–2.5s，total 高分位 **8.5s**；
        #   冷启动首单元 15.46s（联调）/ 19.9–21.3s（生产，含翻译腿冷启）。
        # ⇒ 原 15s 会把**冷启动首单元**砍掉（`stage="budget"`，实测踩到）；
        #   30s = 冷启上界 21.3s × 1.4，暖机单元 3.5x 余量（codex 定的规则：高端样本 + 余量）。
        #   `UNIT_BUDGET` 可在窗口覆盖（P7 采样用过 60/25）。
        # ponytail: 首单元与后续共用一个预算，够用；若冷启动更慢再拆双档。
        # 2026-10-11 复测（同一台、同一份资产，句子 3–39 字）：单句 render 7.1 / 10.6 / 17.9 / 21.6 / 22.2 / 27.3s，
        #   稳定落在「20s 软超时 + 30s 单元预算」里 ⇒ 庄家把**慢但成功**的单元砍掉 = 吞句（stream.log 里
        #   `FAILED stage=render … ReadTimeout` 3 例）。本轮按老规矩重定：45 = 实测上界 27.3 × 1.6，
        #   60 = 45 + 翻译 5 + 余量。**GPU 当前被功耗墙压到 45W（默认 55W / 上限 115W，见 nvidia-smi -q -d POWER），
        #   提速应先把功率墙放开，而不是缩图/加预算。**
        # 2026-10-14 复测：显存吃满（Irodori ~7GB + 其他程序 >8GB）时 Windows 把显存挤到共享内存，
        #   单句 gpu_send 涨到 30–51s，45s 软超时把慢但能成的句子整句丢掉（吞句）。
        #   放宽到 90 / 110：慢句晚一点出声，不再丢；提速要靠腾显存 / 电源方案，不是靠缩超时。
        # 切单元策略（环境变量可调，默认值依据 2026-10-25 对最近 279 条回复的回放，见 session_text.py）：
        #   soft_units：开头几个单元按软切规则抢首音；之后只在句末硬切
        #   later_force：后续单元累计到这么多可说字符还没句末，才退到最近的逗号切（没有逗号才硬切）
        #   later_soft_min：逗号至少落在第几个字之后才算（免得切出碎片）
        #   later_idle：后续单元的空闲冲刷窗口（LLM 句中停顿别把半句冲出去）
        #   later_ramp / ramp_floor：后续单元长度爬坡（上限 = max(floor, ramp × 上一单元字数)，0 = 关）
        self.cut = {"soft_units": int(os.environ.get("ALBION_CUT_SOFT_UNITS", kw.get("soft_units", 1))),
                    "later_force": int(os.environ.get("ALBION_CUT_LATER_FORCE", kw.get("later_force", 64))),
                    "later_soft_min": int(os.environ.get("ALBION_CUT_LATER_SOFT_MIN", kw.get("later_soft_min", 12))),
                    "later_idle_window": float(os.environ.get("ALBION_CUT_LATER_IDLE", kw.get("later_idle", 2.0))),
                    "later_ramp": float(os.environ.get("ALBION_CUT_RAMP", kw.get("later_ramp", 3.5))),
                    "ramp_floor": int(os.environ.get("ALBION_CUT_RAMP_FLOOR", kw.get("ramp_floor", 24)))}
        self.unit_budget = float(os.environ.get("UNIT_BUDGET") or kw.get("unit_budget", 110.0))
        self.render_timeout = float(os.environ.get("RENDER_TIMEOUT") or kw.get("render_timeout", 90.0))  # GPU 软超时
        self.max_units = kw.get("max_units", MAX_UNITS)
        self.max_chars = kw.get("max_chars", MAX_CHARS)
        # P6 限流：壳没报 credit 时按方案 §3.4 的硬上限（最多提前缓存 15 秒）兜底；
        # 等不到 ack 最多等这么久，然后**记一次超时继续**（宁可如实报空档，也不无限阻塞 ——
        # cancel 还得进得来）。
        self.max_ahead_s = kw.get("max_ahead_s", 15.0)
        self.max_ahead_units = kw.get("max_ahead_units", 8)
        self.credit_wait = kw.get("credit_wait", 5.0)
        # 情绪条件化模式（2026-09-28）：
        #   "v3"     = 训练锚码本 + 强度档 + 逐句滞回（新，默认）
        #   "legacy" = 旧 v2（族级一句固定 caption），一条环境变量即可切回
        self.emotion_mode = (os.environ.get("ALBION_EMOTION_MODE")
                             or kw.get("emotion_mode") or "v3").strip().lower()
        if self.emotion_mode not in ("v3", "legacy"):
            self.emotion_mode = "v3"
        # 判定策略：cascade = 规则+提示+滞回（现状）；fusion_v1 = 五路证据融合
        # （方案第一问；**尚未用标注集校准**，默认不开 —— 想听效果就 ALBION_EMOTION_POLICY=fusion_v1）
        self.emotion_policy = (os.environ.get("ALBION_EMOTION_POLICY")
                               or kw.get("emotion_policy") or "cascade").strip().lower()
        if self.emotion_policy not in ("cascade", "fusion_v1"):
            print("[stream] 未知 ALBION_EMOTION_POLICY=%r，回落 cascade"
                  % self.emotion_policy, flush=True)
            self.emotion_policy = "cascade"
        # I 侧计划版本：3 = 现行 v3；4 = v3 + codebook v2（astra-3 §5.1：neutral 说耳语时借既有耳语锚）；
        # 5 = v4 + 拒绝/制止/安抚类的语义闸门（astra 第 11 轮 ②，`ALBION_REFUSAL_GUARD` 默认开）；
        # 6 = v5 + 四缺口（astra 第 12 轮：① `ALBION_HINT_PHONATION` hint 自述发声等级
        #     ② `ALBION_CROSS_SENTENCE` 跨句指令 ③ `ALBION_JA_STAGE_CUE` 日语表层舞台提示
        #     ④ `ALBION_LEVEL_REBASE` 换族后 level 重算）—— 四个开关在 v6 下默认**开**。
        # 版本在**进程启动时**固定（要换就重启服务）—— 会话里不热切，避免一轮里半路换版本。
        # 回退到 4/5 就是回到 v4/v5 的冻结输出（`tests/tts/v4_*_golden.json` / `v5_frozen_golden.json`
        # 守着）；未配置时默认仍是 4，v5/v6 必须显式指定。
        pv = os.environ.get("ALBION_PLAN_VERSION") or kw.get("plan_version") or 4
        try:
            self.plan_version = int(pv)
        except (TypeError, ValueError):
            # 误配置必须拦在启动时：默默回落会把生产版本悄悄换掉（Sol 第 7 轮第二条）。
            raise ValueError("ALBION_PLAN_VERSION 只认 3/4/5/6，收到 %r" % (pv,))
        if self.plan_version not in (3, 4, 5, 6):
            raise ValueError("ALBION_PLAN_VERSION 只认 3/4/5/6，收到 %r" % (pv,))

    def as_dict(self):
        return {"bridge_url": self.bridge_url, "irodori_url": self.irodori_url,
                "voices": self.voices, "prepare_timeout": self.prepare_timeout,
                "unit_budget": self.unit_budget, "render_timeout": self.render_timeout,
                "max_units": self.max_units, "max_chars": self.max_chars,
                "max_ahead_s": self.max_ahead_s, "max_ahead_units": self.max_ahead_units,
                "credit_wait": self.credit_wait, "emotion_mode": self.emotion_mode,
                "emotion_policy": self.emotion_policy,
                "plan_version": self.plan_version, "cut": dict(self.cut),
                "gap": {"enabled": GS.ENABLED, "lead_s": GS.LEAD_S, "tail_s": dict(GS.TAIL_S)},
                "max_down_bytes": MAX_DOWN_BYTES, "max_payload": MAX_PAYLOAD}


class Session:
    """一个 WS 会话：状态机（方案 §3.3）+ 单 writer 出站队列 + 单元队列。"""

    def __init__(self, cfg, session_id, generation, voice, fmt):
        self.cfg = cfg
        self.id = session_id
        self.generation = generation
        self.voice = voice
        self.fmt = fmt
        self.render_session_id = None
        self.render_generation = None
        self.render_epoch = None
        self.render_lock = asyncio.Lock()
        self.state = "OPEN"                      # OPEN → INPUT_CLOSED → DRAINING → FINISHED / CANCELLED / FAILED
        self.text = ST.SessionText(session_id, generation, **cfg.cut)
        self.out = asyncio.Queue()               # ("text"|"bytes", payload)；单 writer 消费
        self.units = asyncio.Queue()             # 已切好的单元（Unit）
        self.pending = deque()                   # 尚未 dispatch 的单元号（算容量）
        # 翻译预取槽：(unit, task)，只提前 prepare，不提前渲染（codex 裁决边界）
        self.prefetch = None
        self.cur_pf = None                       # 正在被处理的预取（dispatcher → _pipeline）
        self.errors = []
        self.audio_bytes = 0
        self.pending_bytes = 0                   # 还在出站队列里没发出去的字节（有界，方案 §3.4）
        self.credits = 0                         # 壳报过几次 credit
        self.credit_s = cfg.max_ahead_s          # 客户端最多提前缓存多少秒（壳可改小，方案 §3.4）
        self.sent_s = 0.0                        # 已发出去的可听秒数（WAV 头实读）
        self.sent_end_wall = None                # 已写出音频按 1x 顺播的墙钟终点（阶梯折叠成标量）
        self.acked_s = 0.0                       # 壳报"已经播完到第几秒"（单调取大）
        self.sent_units = 0                      # 已经发完的音频段数
        self.acked_units = 0                     # 壳报"已经播完多少段"（单调取大）
        self.credit_timeouts = 0                 # 闸门等 ack 等到超时、因而砍掉单元的次数
        self.credit_waited = 0.0                 # 累计"等客户端播放"的秒数（从单元预算里扣除）
        self._PREP_S = {}                        # unit_id → 真·准备耗时（不含"准备完等前一单元"的干等）
        self.acks = 0
        self.last_ack = None
        self.counts = {"units": 0, "audio": 0, "no_audio": 0, "failed": 0}
        # 情绪条件化的会话状态（只在 v3 用）：
        #   emotion_prev = 上一次规划返回的档位（族/caption/强度），下次规划当作滞回基准；
        #   emotion_ctx  = 已译好的前文（有界），让"这句没信号"的句子能借上文判，而不是掉回 calm。
        # 状态挂在会话上、随会话销毁，**不做进程级全局缓存**（旧桥 `_ctx_state` 按 voice 全局共享
        # 那种做法会跨会话串味，v2 明确不要）。
        self.emotion_prev = None
        self.emotion_ctx = ""
        # 服务端把 v3 降级回 v2 后，本会话锁定 v2（避免同一段里两种语气交替）
        self.plan_v2_only = False
        self.cancelled = False
        self.closed_ws = False

    # ---------- 出站（只有 writer 任务真的往 socket 写） ----------
    async def send_json(self, obj):
        await self.out.put(("text", json.dumps(obj, ensure_ascii=False)))

    async def send_bytes(self, payload):
        await self.out.put(("bytes", payload))

    def send_json_nowait(self, obj):
        """取消/超预算路径上用：被取消的协程里 `await` 会立刻抛，出站只能非阻塞投递。"""
        try:
            self.out.put_nowait(("text", json.dumps(obj, ensure_ascii=False)))
        except asyncio.QueueFull:                    # out 无界，理论上不会满
            pass

    # ---------- 状态 ----------
    async def fail(self, code, message):
        """终态只进不退：已经是 FAILED 就不再改写（复核：容量错误后被 finish 改回 complete）。"""
        if self.state == "FAILED":
            return
        self.state = "FAILED"
        self.errors.append("%s: %s" % (code, message))
        await self.send_json({"event": "error", "code": code, "message": message})

    def info(self):
        return {"session_id": self.id, "generation": self.generation, "state": self.state,
                "voice": self.voice, "format": self.fmt, **self.counts,
                "audio_bytes": self.audio_bytes, "pending_bytes": self.pending_bytes,
                "feedback": {"credits": self.credits, "credit_s": self.credit_s, "acks": self.acks,
                             "sent_s": round(self.sent_s, 3), "acked_s": round(self.acked_s, 3),
                             "sent_units": self.sent_units, "acked_units": self.acked_units,
                             "credit_timeouts": self.credit_timeouts, "last_ack": self.last_ack},
                "errors": list(self.errors)}


class ActiveBudget:
    """整单元的"实际工作时间"预算，**可暂停** —— 等客户端播放的时间不计入。

    codex 裁决的第三种方案：固定的 `asyncio.wait_for` 一启动就不认暂停，表达不了
    "准备＋渲染计时、等播放不计时"。这里用 `call_later` 定时器：暂停时取消定时并扣掉已用，
    恢复时按剩余额度重定时。watchdog 只取消**它自己持有的那个 task**；外部取消时
    `expired` 保持 False，`CancelledError` 原样上抛（不会被误报成预算超时）。

    模块级定义：函数体查找名字会跳过类作用域，放进 `Coordinator` 里会 `NameError`。
    """

    def __init__(self, total, coro, on_expire=None):
        self.total = total
        self.left = total
        self.expired = False
        self._on_expire = on_expire
        self._timer = None
        self._since = None
        self.task = asyncio.ensure_future(coro)

    def _arm(self):
        self._since = time.monotonic()
        self._timer = asyncio.get_running_loop().call_later(max(self.left, 0.001), self._fire)

    def _disarm(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._since is not None:                    # 扣掉这一段实际工作时间
            self.left -= time.monotonic() - self._since
            self._since = None

    def _fire(self):
        self.expired = True
        if self._on_expire:
            self._on_expire()
        self.task.cancel()

    def start(self):
        self._arm()

    def pause(self):
        if not self.expired:
            self._disarm()

    def resume(self):
        if self.expired:
            return
        if self.left <= 0:
            self._fire()
        else:
            self._arm()

    def close(self):
        self._disarm()


class Coordinator:
    """把壳的 token 流接成"单元 → 译文 → 计划 → 音频"的流水线。"""

    def __init__(self, cfg=None, transport=None):
        self.cfg = cfg or Config()
        self._transport = transport
        self._client = None
        self.sessions = {}
        # 单元开始时刻（unit_id → monotonic）：失败单元也要能报已耗时间/阶段（codex 复核）
        self._T0 = {}
        self.started_at = time.time()

    def _http(self):
        if self._client is None:
            # transport 可注入（离线测试把上游换成 in-process ASGI）；必须在**本 loop** 里建 client
            self._client = httpx.AsyncClient(transport=self._transport,
                                             timeout=self.cfg.render_timeout)
        return self._client

    async def aclose(self):
        """关闭连接池；应用停机后可安全重复调用。"""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # ---------------- 上游三段 ----------------
    async def prepare(self, sess, unit, context_before=""):
        """桥 /v2/prepare：结构化译文（unit_id 去重、全局限流都在桥里）。"""
        full = getattr(sess.text, "text", "") or ""
        body = {"session_id": sess.id, "generation": sess.generation, "unit_id": unit.unit_id,
                "voice": sess.voice, "source_range": [unit.start, unit.end],
                "speech_text": unit.speech_text, "context_before": context_before,
                # 切分原因：桥据此判断这一单元是不是「句子没说完」的半句（译文要以「、」收尾）
                "unit_reason": unit.reason,
                # 原始切片：`speech_text` 按会话解析把旁白剥了，「（小声）」这类线索只在原文里
                # （Sol r2 阻塞①：闸门之前根本收不到这些线索，因为桥只看得到剥离后的文本）
                "raw_text": full[unit.start:unit.end],
                # 规划版本**随请求**发出去：桥按版本定这个开关（v4/v5 一律关）。跨句开关本身
                # **不由壳下发**（Sol 第 14 轮第一条）：壳替桥写死 True 会把桥侧
                # `ALBION_CROSS_SENTENCE=0` 短路掉，而规划器仍按环境读 —— 同一次请求两边算出
                # 不同的值（显示已关、声音条件还在变）。两边各自按「版本 + 环境」算，值才一致。
                "plan_version": self.cfg.plan_version,
                # 本 unit **之前**的原文字符串：跨 unit 的指令延续由桥重放它算出
                # （`cue_scope.active_directive`）。不靠"流式状态"传递 —— 翻译预取会提前
                # prepare 下一单元，那时上一单元的指令还没解析；只按前文文本算没有顺序问题。
                "source_prefix": full[:unit.start]}
        r = await self._http().post(self.cfg.bridge_url.rstrip("/") + "/v2/prepare", json=body,
                                    timeout=self.cfg.prepare_timeout)
        r.raise_for_status()
        return r.json()

    async def plan(self, text, unit_id, context_before="", prev=None, emotion=None,
                   advance=True, force_v2=False):
        """I /v2/plan：纯 CPU 的场景/风格规划。

        v3（默认）：把**前文**与**上一档状态**一起送上去，让规划器能做逐句滞回；
        返回 (单元规划, 新状态, 是否被服务端降级退回 v2)。

        legacy（或本会话已被降级）：只发 v2 认识的字段 —— `emotion`/`prev` 这些 v3 专属键
        一个都不塞，回滚才是**字段级**干净，而不是"多发几个键但对方不看"。
        `context_before` 两个版本都要发：v2 自己的 `_ctx_style` 就靠它判邻句。
        """
        v3 = self.cfg.emotion_mode == "v3" and not force_v2
        unit = {"unit_id": unit_id, "text": text}
        body = {"units": [unit], "context_before": context_before,
                "plan_version": (self.cfg.plan_version if v3 else 2)}
        if v3:
            unit["emotion"] = emotion
            unit["advance"] = bool(advance)
            body["emotion_policy"] = self.cfg.emotion_policy
            if prev:
                body["prev"] = prev
        r = await self._http().post(self.cfg.irodori_url.rstrip("/") + "/v2/plan", json=body)
        if r.status_code != 200:
            # I 侧把真因放在 body 的 error 里（v4–v6 版本隔离下这是唯一线索）：只报状态码等于
            # 把线索丢掉。不回显请求原文，只留状态码 + 截长的响应体。
            raise StageError("plan", "plan %d: %s" % (r.status_code, (r.text or "")[:200]))
        data = r.json()
        if data.get("status") == "failed":
            raise StageError("plan", data.get("error") or "plan failed")
        units = data.get("units") or []
        if not units:
            raise StageError("plan", "plan 未返回单元")
        got = int(data.get("plan_version") or 2)
        if got != self.cfg.plan_version:
            if self.cfg.plan_version >= 4:
                # v4 的版本隔离是硬要求：拿到的不是请求的版本就**明确失败**，不按实得版本继续
                # （Sol 第 7 轮第二条）；v3 原有的"如实记日志继续"保留。
                raise StageError("plan", "计划版本不符：请求 %d 实得 %d（v4 不接受降级）"
                                 % (self.cfg.plan_version, got))
            if v3:
                # 计划版本与请求不一致：可能是 I 侧回落（fallback=v2），也可能是两边配置不同步。
                # 如实记日志、并按**实际拿到的版本**往下走，不假装拿到的是请求的那个版本。
                print("[stream] 计划版本不符：请求 %d 实得 %d%s"
                      % (self.cfg.plan_version, got,
                         "（I 侧回落）" if data.get("fallback") else ""), flush=True)
        fell_back = bool(data.get("fallback")) or got < 3
        return units[0], (None if (fell_back or not v3) else data.get("state")), (fell_back and v3)

    async def open_render(self, sess):
        async with sess.render_lock:
            if sess.cancelled:
                raise RuntimeError("render session cancelled")
            if sess.render_session_id is None:
                r = await self._http().post(self.cfg.irodori_url.rstrip("/") + "/v2/render/open",
                                            json={"voice": sess.voice})
                r.raise_for_status()
                data = r.json()
                if (data.get("render_lifecycle_version") != 2 or not data.get("epoch")
                        or not isinstance(data.get("session_id"), str)
                        or type(data.get("generation")) is not int):
                    raise RuntimeError("render lifecycle version mismatch")
                sess.render_session_id = data["session_id"]
                sess.render_generation = data["generation"]
                sess.render_epoch = data["epoch"]
            return sess.render_session_id, sess.render_generation

    async def close_render(self, sess):
        async with sess.render_lock:
            await self._end_render(sess, "close")

    async def _end_render(self, sess, action):
        if sess.render_session_id is None:
            return
        try:
            r = await self._http().post(self.cfg.irodori_url.rstrip("/") + "/v2/render/" + action,
                                        json={"session_id": sess.render_session_id,
                                              "generation": sess.render_generation})
            r.raise_for_status()
        except Exception as e:                                    # 收尾尽力而为，不掩盖主流程结果
            sess.errors.append("close: %s: %s" % (type(e).__name__, str(e)[:120]))

    async def cancel_render(self, sess):
        async with sess.render_lock:
            await self._end_render(sess, "cancel")

    # ---------------- 单单元流水线 ----------------
    async def process_unit(self, sess, unit):
        """一个单元：翻译 → 规划 → 渲染（逐子段）。失败只影响这个单元（方案 §3.4）。

        复核问题 3：15s 是**整个单元**（prepare 含在内、plan、全部子段渲染）的**绝对**截止时间，
        所以整条流水线套 `wait_for`。原来只在渲染前查一次剩余时间、且 `max(1.0, …)` 会越过
        截止点，HTTP 超时也只是"每次读"超时，慢滴流能拖到天荒地老。
        """
        if unit.kind == "no_audio":
            sess.cur_pf = None                     # 预取槽也要清（codex 复核：no_audio 会漏账）
            await self._no_audio(sess, unit)
            return
        # 上限从**预取发起时**算起（codex 复核）：外层 wait_for 也必须扣掉预取已耗掉的时间，
        # 否则"翻译+渲染合计 30s"要到取回后才开始计时，整条流水线就不是同一个截止。
        t0 = self._T0.get(unit.unit_id) or time.monotonic()
        # 预取单元的预算从"**取用时刻** − 真·准备耗时"起算：准备完却等前一单元的那段干等不算
        # （codex 复核）。等价于把准备时间挪到取用时刻，后面的 remaining / deadline 全部沿用这个 t0。
        if unit.unit_id in sess._PREP_S:
            t0 = time.monotonic() - sess._PREP_S.pop(unit.unit_id)
            self._T0[unit.unit_id] = t0
        # 初始余额 = 单元预算 − **取用前已实际跑掉的准备时间**（预取完等前一单元的干等不算，
        # 见 `_PREP_S`；预取还在跑的那部分准备时间要算）。
        remaining = self.cfg.unit_budget - (time.monotonic() - t0)
        if remaining <= 0:
            log("%s 单元 %s 预取已耗尽预算 %.1fs" % (sess.id, unit.unit_id, self.cfg.unit_budget))
            await self._fail_span(sess, unit, "budget",
                                  "预取已耗尽 %.1fs 预算" % self.cfg.unit_budget)
            return
        budget = ActiveBudget(
            remaining, self._pipeline(sess, unit),
            lambda: log("%s 单元 %s 超预算 %.1fs" % (sess.id, unit.unit_id, self.cfg.unit_budget)))
        sess._budget = budget
        budget.start()
        try:
            await budget.task
        except asyncio.CancelledError:
            if not budget.expired:
                raise                        # 外部取消（会话终止等）：原样上抛，不报预算
            await self._fail_span(sess, unit, "budget",
                                  "单元实际工作超过 %.1fs 预算" % self.cfg.unit_budget)
        except StalledCredit as e:
            await self._fail_span(sess, unit, "credit", str(e))
        finally:
            budget.close()
            sess._budget = None

    async def _pipeline(self, sess, unit):
        # t0 沿用**预取发起时**记下的时刻：预算口径始终是"本单元从被调度起算的翻译+渲染合计"，
        # 取回预取结果时**不重新起算**（codex 裁决边界）。
        t0 = self._T0.setdefault(unit.unit_id, time.monotonic())
        deadline = t0 + self.cfg.unit_budget
        pf = sess.cur_pf                         # 预取结果（dispatcher 已按队列顺序取到并交过来）
        sess.cur_pf = None
        try:
            # `pf[1]` 为空有两种可能：根本没预取，或它是**已知无声**的单元（`_prefetch_next` 不为它建 task）。
            # 后者其实到不了这行 —— `process_unit` 已对 `kind == "no_audio"` 提前返回；这里只是兜底。
            prepared = await (pf[1] if pf and pf[1] else self.prepare(sess, unit))
        except Exception as e:
            await self._fail_span(sess, unit, "prepare", "%s: %s" % (type(e).__name__, str(e)[:160]))
            return
        t_prep = time.monotonic()
        status = prepared.get("status")
        if status == "no_audio":
            await self._no_audio(sess, unit, reason=prepared.get("reason"))
            return
        if status != "ready":
            await self._fail_span(sess, unit, prepared.get("stage") or "prepare",
                                  prepared.get("error") or "not ready")
            return
        if sess.cancelled:
            return
        self._prefetch_next(sess)     # 当前单元先拿翻译槽，随后与渲染重叠准备下一单元。
        # 真契约（P2 `PrepareResult.as_dict()`）：ready 时给的是**子段数组** `ja_segments`，
        # 不是一个 `translated_text`——多句单元就该在这里逐子段送下去（复核问题 1）。
        # 2026-09-28：子段上可能带 `emotion`（桥的翻译腿顺带判的情绪，见 bridge.to_japanese_ex），
        # 一起带走送进规划；缺就当没有，绝不因此丢译文。
        segments = [(s.get("sub_index", i), (s.get("text") or "").strip(), s.get("emotion"))
                    for i, s in enumerate(prepared.get("ja_segments") or [])]
        segments = [s for s in segments if s[1]]
        if not segments:
            await self._fail_span(sess, unit, "prepare", "empty ja_segments")
            return
        await self.render_unit(sess, unit, segments, deadline)
        # 阶段耗时（P7 真机取数用；codex 要求冷热分开、别只用一个总数拍预算）：
        # prepare = 翻译腿（含排队）；gpu_send = 各子段 GPU 合成 **+ 发帧 + 限流等待** 的合计（不是纯 GPU）。
        t_end = time.monotonic()
        self._T0.pop(unit.unit_id, None)
        log("[timing] %s %s segs=%d prepare=%.0fms gpu_send=%.0fms total=%.0fms"
            % (sess.id, unit.unit_id, len(segments), (t_prep - t0) * 1000.0,
               (t_end - t_prep) * 1000.0, (t_end - t0) * 1000.0))

    async def _no_audio(self, sess, unit, reason=None):
        """无音频单元：不发二进制、不造零长度帧；**显示资格立即生效**（方案 §4.2）。

        事件是 `display.span(eligible=immediate)` + `unit.no_audio`，**不**发 sentence.* ——
        壳侧对无音频段一个字节都不该 decode（P:188 的零字节 return 不产生字幕回调，只省帧不够）。
        """
        self._T0.pop(unit.unit_id, None)   # 预取留下的时间账要清（codex 复核：no_audio 会漏账）
        sess.counts["no_audio"] += 1
        display = self.source_of(sess, unit)
        await sess.send_json({"event": "display.span", "payload": {
            "session_id": sess.id, "generation": sess.generation, "unit_id": unit.unit_id,
            "source_unit_id": unit.unit_id, "source_range": [unit.start, unit.end],
            "text": display, "eligible": "immediate", "source": "no_audio"}})
        await sess.send_json({"event": "unit.no_audio", "payload": {
            "session_id": sess.id, "generation": sess.generation, "unit_id": unit.unit_id,
            "source_range": [unit.start, unit.end], "reason": reason or unit.reason,
            "text": display}})

    async def _fail_span(self, sess, unit, stage, error):
        """失败段也要给一次显示资格（复核第 3 条）：合成不出来是我的问题，但"这段字不存在"
        不是事实 —— 原文守恒要靠它。同一个 `unit_id` 重复给 span 无害（壳侧账本按
        `unit_id` 去重），所以不做"是否已给过"的分支，少一处状态就少一处错。
        """
        t0 = self._T0.pop(unit.unit_id, None)
        if t0 is not None:
            log("[timing] %s %s FAILED stage=%s elapsed=%.0fms error=%s"
                % (sess.id, unit.unit_id, stage, (time.monotonic() - t0) * 1000.0,
                   text_meta(error)))
        sess.counts["failed"] += 1
        await sess.send_json({"event": "display.span", "payload": {
            "session_id": sess.id, "generation": sess.generation, "unit_id": unit.unit_id,
            "source_unit_id": unit.unit_id, "source_range": [unit.start, unit.end],
            "text": self.source_of(sess, unit), "eligible": "immediate", "source": "failed"}})
        await sess.send_json({"event": "unit.failed", "payload": {
            "unit_id": unit.unit_id, "stage": stage, "source_range": [unit.start, unit.end],
            "error": error}})

    async def render_unit(self, sess, unit, segments, deadline):
        """一个单元的**每个子段**各一组 `sentence.start` → N 帧 → `sentence.end`。

        为什么句级事件要按音频段分（复核第 3 轮抓到的真 bug）：I 侧每个子段给的是一份
        **完整 WAV**，壳侧把一组 sentence 的全部字节拼起来只 decode 一次 —— `WAV1+WAV2`
        不是合法 WAV（普通解码器报 `No 'RIFF' chunk marker`）。所以一组 sentence 只能装
        一份完整音频，用 `unit_id + sub_index` 配对。
        `display.span` 相反：一个**源单元只给一次**（否则壳侧账本会把同一段原文记两遍）。

        每个子段一个 `/v2/render` 请求，且**必须**读到 `done` 才算完整（复核问题 3）。
        """
        display = self.source_of(sess, unit)
        span_sent = False
        seg = None                       # 当前子段的账：start 发过没、帧数、样本数
        try:
            for sub_index, ja_text, emotion in segments:
                if sess.cancelled:
                    return
                # 每个子段开跑前也看一眼余额（省一次白烧的 GPU 渲染）；帧级检查仍在发帧前兜底。
                await self._wait_credit(sess)   # 等播放由 ActiveBudget 暂停计时，不再改 deadline
                left = (sess._budget.left if getattr(sess, "_budget", None)
                        else deadline - time.monotonic())
                if left <= 0:                                  # 复核问题 3：先查剩余预算，再规划
                    raise RuntimeError("unit budget exhausted")
                uid = "%s:%d" % (unit.unit_id, sub_index)
                # 规划带上：本会话已译前文 + 上一次档位（滞回基准）+ 桥给的情绪提示。
                # 服务端若把 v3 降级回 v2（fallback），本会话**锁定** v2，避免一段里两种语气交替。
                plan, state, fell_back = await self.plan(
                    ja_text, uid, context_before=sess.emotion_ctx,
                    prev=sess.emotion_prev, emotion=emotion, force_v2=sess.plan_v2_only,
                    # soft/force 切出的单元是**同一自然句的续片**，不重复推进"无信号"计数
                    advance=(unit.reason not in ("soft", "force")))
                if fell_back and not sess.plan_v2_only:
                    sess.plan_v2_only = True
                    log("%s 规划被降级退回 v2，本会话改为只用 v2：%s" % (sess.id, uid))
                # 状态**等这一子段真把音频发完**再提交：渲染失败/预算耗尽时，
                # 没出声的那句不该把情绪带到下一句（复核问题 4）。
                pending_state = state
                pending_ctx = (sess.emotion_ctx + " " + ja_text)[-EMOTION_CTX_CHARS:]
                u_body = {"unit_id": uid, "sub_index": sub_index, "text": ja_text,
                          "family": plan.get("family", "neutral"), "caption": plan.get("caption", ""),
                          "cfg": plan.get("cfg", 3.0), "scene_id": plan.get("scene_id", 1)}
                if plan.get("tier") is not None:        # v3 才有的诊断字段，v2 请求里不出现
                    u_body["tier"] = plan["tier"]
                if plan.get("level") is not None:
                    u_body["level"] = plan["level"]
                if plan.get("codebook_version") is not None:
                    u_body["codebook_version"] = plan["codebook_version"]   # v4 才有
                render_sid, render_gen = await self.open_render(sess)
                body = {"session_id": render_sid, "generation": render_gen,
                        "plan_version": plan.get("plan_version", 2),
                        "voice": sess.voice, "format": sess.fmt, "units": [u_body]}
                log("%s %s 情绪 %s/%s cfg=%s scene=%s %s"
                    % (sess.id, uid, plan.get("family"), plan.get("tier") or "-",
                       plan.get("cfg"), plan.get("scene_id"), text_meta(plan.get("reason") or "")))
                # 剩余预算同时约束**这一个**渲染请求（不再有 `max(1.0, …)` 的越界下限）
                timeout = min(self.cfg.render_timeout, left)
                if seg is not None and seg["started"]:
                    raise RuntimeError("上一子段没收尾")        # 只有异常路径会到这（finally 兜底）
                seg = {"sub_index": sub_index, "started": False, "frames": 0, "samples": 0, "sr": None}
                saw_done = False
                async with self._http().stream("POST", self.cfg.irodori_url.rstrip("/") + "/v2/render",
                                               json=body, timeout=timeout) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode("utf-8", "ignore")[:200]
                        raise RuntimeError("render %d: %s" % (r.status_code, text))
                    async for name, payload in self._sse(r):
                        if sess.cancelled:
                            return
                        if name == "audio_chunk":
                            audio = base64.b64decode(payload.get("audio_base64") or "")
                            if sess.fmt == "wav":
                                # 统一气口：非末段子段必是句子收尾；末段按单元末尾的标点/切分原因定尾静音
                                audio = GS.shape(audio, GS.tail_kind(unit.speech_text, unit.reason)
                                                 if sub_index == segments[-1][0] else "sentence")
                            if len(audio) > MAX_PAYLOAD:
                                raise RuntimeError("payload 超过 %d 字节上限" % MAX_PAYLOAD)
                            if sess.pending_bytes + len(audio) > MAX_DOWN_BYTES:
                                raise RuntimeError("下行待发字节超过 %d" % MAX_DOWN_BYTES)
                            n_samples, sr = self._wav_info(audio)
                            if sess.fmt == "wav" and not n_samples:
                                # 无法计时的 WAV 帧**不许过闸**：它绕过的正是秒数上限（复核护栏）。
                                # 生产链路里一个 audio_chunk 就是一份完整 WAV（serve_v2 encode 后
                                # 整段发出），所以这里失败＝上游给的东西不对，该明说。
                                raise RuntimeError("WAV 会话收到无法计时的帧（%d 字节，无 RIFF 头）"
                                                   % len(audio))
                            # P6：**发之前**按这一帧的真实时长看余额（方案 §3.4 "15 秒或 8 段"，
                            # 先到为准）。到顶就等 ack；等不到 ack 抛 StalledCredit 砍掉本单元 ——
                            # 不在单元边界放行，那样一个单元就能把缓冲冲过上限（复核第 3 条）。
                            # 段数只在**本段第一帧**记账：一个子段 = 客户端的一个播放段。
                            frame_s = (n_samples / sr) if n_samples else 0.0
                            await self._wait_credit(
                                sess, need_s=frame_s, need_units=0 if seg["started"] else 1)
                            if not seg["started"]:
                                if not span_sent:
                                    # 方案 §4.2：可说 span 在**首个音频真的开始**时获得显示资格；
                                    # 一个源单元只给一次，后面的子段共用这一份原文。
                                    await sess.send_json({"event": "display.span", "payload": {
                                        "session_id": sess.id, "generation": sess.generation,
                                        "unit_id": unit.unit_id, "source_unit_id": unit.unit_id,
                                        "source_range": [unit.start, unit.end], "text": display,
                                        "eligible": "audio", "source": "audio"}})
                                    span_sent = True
                                await sess.send_json({"event": "sentence.start", "payload": {
                                    "session_id": sess.id, "generation": sess.generation,
                                    "unit_id": unit.unit_id, "sub_index": sub_index,
                                    "text": display, "format": sess.fmt}})
                                seg["started"] = True
                            if n_samples:
                                seg["samples"] += n_samples
                                seg["sr"] = sr
                                sess.sent_s += n_samples / sr       # P6：已发出去的可听秒数（WAV 实读）
                            seg["frames"] += 1
                            sess.pending_bytes += len(audio)      # writer 发完再减
                            await sess.send_bytes(audio)
                            sess.audio_bytes += len(audio)
                            sess.counts["audio"] += 1
                        elif name == "unit.failed":
                            raise RuntimeError(payload.get("error") or "render failed")
                        elif name == "render.cancelled":
                            return
                        elif name == "done":
                            saw_done = True
                            if payload.get("failed"):
                                raise RuntimeError("render 内部失败 %s 段" % payload["failed"])
                if not saw_done:
                    raise RuntimeError("render 流提前结束（没有 done）")
                await sess.send_json(self._segment_end(sess, unit, seg, display))  # 这一段收尾；下一段另起一组
                seg = None
                if pending_state:
                    # 提交交给**唯一真正写 socket 的 writer**（队列 FIFO 就是回执）：
                    # 之前这里只是"入队成功"就提交，断线/取消时会把没听到的语气带下去。
                    await sess.out.put(("commit", (pending_state, pending_ctx)))
        except StalledCredit:
            raise                                   # 限流砍单元：留给人记 stage="credit"，别报成 render 失败
        except Exception as e:
            # stage 从异常里取（plan 的失败走 StageError）：兜底默认 render。
            st = getattr(e, "stage", "render")
            log("%s 单元失败 stage=%s %s: %s: %s"
                % (sess.id, st, unit.unit_id, type(e).__name__, text_meta(e)))
            await self._fail_span(sess, unit, st, "%s: %s" % (type(e).__name__, str(e)[:160]))
        finally:
            if seg is not None and seg["started"]:
                # 非阻塞投递：预算到点/取消时本协程已处于被取消状态，`await` 会立刻抛
                sess.send_json_nowait(self._segment_end(sess, unit, seg, display))

    @staticmethod
    def _segment_end(sess, unit, seg, display):
        """一个子段（= 一份完整音频）的 `sentence.end`，用 `unit_id + sub_index` 配对。"""
        sess.sent_units += 1                     # P6：一份完整音频 = 对客户端的一个播放段
        # 折进客户端的播放时间轴：它是「上一段播完就接着播，收到晚了（渲染空档）就从收到那刻起播」的
        # 阶梯。折成一个标量终点就够（不留历史就不必处理裁剪 —— codex 复核第 4 条点出的那个坑）。
        if seg["samples"] and seg["sr"]:
            now = time.monotonic()
            prev = sess.sent_end_wall
            sess.sent_end_wall = (now if prev is None or now > prev else prev) \
                + seg["samples"] / float(seg["sr"])
        return {"event": "sentence.end", "payload": {
            "session_id": sess.id, "generation": sess.generation, "unit_id": unit.unit_id,
            "sub_index": seg["sub_index"], "text": display, "format": sess.fmt,
            "frames": seg["frames"], "samples": seg["samples"] or None, "sr": seg["sr"]}}

    @staticmethod
    def _wav_info(audio):
        """从 WAV 头**实读** (样本数, 采样率)；非 WAV 返回 (None, None)。方案 §4.1 要上报实际样本数。"""
        if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            return None, None
        i, ch, sr, bits, data = 12, None, None, None, None
        while i + 8 <= len(audio):
            cid, size = audio[i:i + 4], int.from_bytes(audio[i + 4:i + 8], "little")
            body = audio[i + 8:i + 8 + size]
            if cid == b"fmt " and len(body) >= 16:
                ch = int.from_bytes(body[2:4], "little")
                sr = int.from_bytes(body[4:8], "little")
                bits = int.from_bytes(body[14:16], "little")
            elif cid == b"data":
                data = size
                break
            i += 8 + size + (size & 1)
        if not (ch and sr and bits and data is not None):
            return None, sr
        return data // (ch * (bits // 8)), sr

    @staticmethod
    async def _sse(response):
        """按**完整 SSE 事件**增量读取（跨 chunk、多行 data 都要对；不能 read() 聚合全响应）。"""
        name, buf = None, []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                buf.append(line.split(":", 1)[1].strip())
            elif line == "" and name is not None:
                try:
                    yield name, json.loads("\n".join(buf) or "{}")
                except json.JSONDecodeError:
                    yield name, {}
                name, buf = None, []

    @staticmethod
    def source_of(sess, unit):
        """显示文本 = 源账本里这一段**原文**（含心声/标点，方案 §4.2：原消息始终完整）。"""
        return sess.text.text[unit.start:unit.end]

    # ---------------- 会话 ----------------
    async def run(self, ws: WebSocket):
        await ws.accept()
        sess = None
        writer = dispatcher = ticker = None
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "text" not in msg:                     # 客户端不该发二进制
                    break
                try:
                    frame = json.loads(msg["text"])
                except json.JSONDecodeError:
                    if sess:
                        await sess.fail("bad_frame", "上行不是合法 JSON")
                        await self._flush(sess)
                    break
                ev = frame.get("event")
                if ev == "start":
                    try:
                        sess = self._open(frame)
                    except BadProtocol as e:
                        await ws.send_text(json.dumps({"event": "error", "code": "bad_protocol_version",
                                                       "message": str(e)}, ensure_ascii=False))
                        await ws.close(code=1002)      # 协议错：显式关连接（复核要求冒烟断言关闭）
                        break
                    self.sessions[sess.id] = sess
                    writer = asyncio.ensure_future(self._writer(ws, sess))
                    dispatcher = asyncio.ensure_future(self._dispatcher(sess))
                    ticker = asyncio.ensure_future(self._ticker(sess))
                    await sess.send_json({"event": "session.started", "protocol_version": PROTOCOL_VERSION,
                                          "capabilities": CAPABILITIES,
                                          # 闸门只在 WAV 上真成立：限流靠"已发秒数"，而秒数是从 WAV 头
                                          # 实读的。mp3 认不出样本数就没法算余额 —— 如实报 false，
                                          # 别让壳以为有保护（复核第 1 条）。
                                          "flow_control": sess.fmt == "wav",
                                          "audio_format": sess.fmt, "session_id": sess.id,
                                          "generation": sess.generation, "voice": sess.voice})
                elif sess is None:
                    break                                  # 没 start 就来 text/finish：协议错误
                elif ev == "text":
                    if sess.state == "OPEN":
                        await self._on_text(sess, frame.get("text") or "")
                elif ev == "finish":
                    if sess.state != "OPEN":                   # 终态/已收尾：后续输入一律忽略
                        continue                               # （复核：别把 FAILED 改写回 complete）
                    sess.state = "INPUT_CLOSED"
                    for u in sess.text.finish(time.monotonic()):
                        self._log_unit(sess, u)
                        await self._enqueue(sess, u)
                    if sess.units.empty() and not sess.pending:
                        await self._finish(sess)          # 整段没有可说的单元（纯心声/纯符号）也要收尾
                elif ev == "cancel":
                    await self._cancel(sess)
                elif ev in ("playback.credit", "playback.ack"):
                    self._on_feedback(sess, ev, frame)      # 只记账：闸门等 P6 播放时钟接上再上
                else:
                    await sess.fail("bad_event", "未知事件: %r" % ev)
        except Exception as e:                            # 断流/异常：会话作废，别把异常吞成静默
            if sess is not None:
                sess.errors.append("%s: %s" % (type(e).__name__, str(e)[:160]))
        finally:
            if sess is not None:
                if sess.state not in ("FINISHED", "CANCELLED", "FAILED"):
                    await self._cancel(sess)
                await self._flush(sess)
                for t in (writer, dispatcher, ticker):
                    if t is not None:
                        t.cancel()
                self.sessions.pop(sess.id, None)

    def _on_feedback(self, sess, ev, frame):
        """壳的上行反馈（方案 §4.1）：credit=可接收时长、ack=**已播到第几秒**（壳按自己的
        播放时钟算，P6 起这是限流的唯一依据 —— S 不拿"发出去多少"冒充"播了多少"）。

        上行是**信任边界**：ack 是唯一能解开闸门的输入，所以非法/越界值一律丢弃、只留合法的单调前进。
        （codex 复核实测：`seconds=inf`、`units=999` 曾被原样吃下 ⇒ 余额保护被整个绕过、
        `last_move` 被反复重置。`bool` 是 `int` 的子类，`float(True)==1.0` 也会冒充进度，一并挡掉。）
        """
        raw = frame.get("seconds")
        secs = None
        if not isinstance(raw, bool):
            try:
                secs = float(raw)
            except (TypeError, ValueError, OverflowError):   # 10**400 这种极大整数：float() 会 OverflowError
                secs = None
            if secs is not None and (not math.isfinite(secs) or secs < 0.0):
                secs = None
        if ev == "playback.credit":
            if secs is not None:
                sess.credits += 1
                # 壳报的 credit 钳在上限内（方案 §3.4：15 秒是**硬**上限，客户端说再多也不认）
                sess.credit_s = min(sess.cfg.max_ahead_s, secs)
            return
        raw_u = frame.get("units")
        units = None
        if not isinstance(raw_u, bool) and isinstance(raw_u, (int, float)):
            try:
                if float(raw_u).is_integer():
                    units = int(raw_u)
            except (OverflowError, ValueError):              # 同上：极大整数不许打崩 WS 处理
                units = None
        # 超过「已发出去的量」的声明不算播放进度（只留服务端累计与客户端 round 的毫秒级正差）。
        if secs is not None and secs > sess.sent_s + 1.0:
            log("[stream_v2] %s ack 丢弃 seconds=%.3f > 已发 %.3f+1s（口径不符）" % (sess.id, secs, sess.sent_s))
            secs = None
        if units is not None and not 0 <= units <= sess.sent_units:
            log("[stream_v2] %s ack 丢弃 units=%s > 已发 %d 段（口径不符）" % (sess.id, units, sess.sent_units))
            units = None
        sess.acks += 1
        if secs is not None:
            sess.acked_s = max(sess.acked_s, secs)     # 单调取大：迟到的 ack 不许把进度拨回去
        if units is not None:
            sess.acked_units = max(sess.acked_units, units)
        sess.last_ack = {"unit_id": frame.get("unit_id"), "seconds": secs, "units": units,
                         "at": round(time.monotonic(), 3)}

    async def _wait_credit(self, sess, need_s=0.0, need_units=0):
        """发**每个音频段之前**按它的真实时长检查余额（方案 §3.4："最多提前 15 秒或 8 段"，
        先到为准）。到顶就停在这儿等壳的 ack —— 不假装时钟排了还不存在的音频。

        等过 `credit_wait` 就抛 `StalledCredit`：**砍掉这个单元**（`unit.failed stage="credit"`），
        不再继续放行。复核第 3 条的原话是"超时明确报失败或停止该单元，不能继续放行"——
        放行等于把缓冲撑爆，客户端只会更晚开口。
        返回**等待了多少秒**，调用方把这部分从单元预算里扣除 ——
        等客户端播放不属于这个单元的"准备/渲染耗时"（实测长文本里预算被等待吃光，
        连带出 `stage=budget` 的次生失败）。

        **契约放宽（2026-09-28，codex 复核后与指挥官确认）**：当**一整帧的时长就超过窗口**
        时，只要求"待播余额归零"就放行，不再要求塞得进 `credit_s` —— 单帧大于窗口是
        **不可满足**条件，靠等永远等不到（1280 字长文实测：20/36 段因此被判 `credit` 丢掉）。
        等价说法：秒数上限从"15 秒"变为"**15 秒，或一整帧，取较大者**"；段数上限 8 不变。

        **背压不是故障**：只要 ack 还在推进就继续等；只有连续 `credit_wait` 秒
        **没有任何播放进度**才判 `StalledCredit`。原先固定等满 `credit_wait` 就砍，
        把"按 1× 播放需要 6 秒才腾出整帧空间"这种**有进展**的等待也误杀了。
        """
        # 等客户端播放**暂停本单元的工作预算**：这段时间不是这个单元的耗时（codex 裁决）。
        # `finally` 里恢复：一次 `pause()` 必有一次对应恢复 —— 正常返回、`StalledCredit`、
        # 被取消、任何异常都一样，计时器不会停在暂停态（codex 复核）。最终 `close()` 仍归上层。
        _bud = getattr(sess, "_budget", None)
        if _bud:
            _bud.pause()
        try:
            return await self._wait_credit_body(sess, need_s, need_units)
        finally:
            if _bud:
                _bud.resume()

    async def _wait_credit_body(self, sess, need_s=0.0, need_units=0):
        started = time.monotonic()
        # 非 WAV 我们已在开流时宣告 `flow_control=false`（客户端不发 ack、也没有可靠时长可核），
        # 再设闸门就是"声明一套、执行另一套"（codex 复核实测：mp3 客户端会在第 9 段被段数闸门砍）。
        if sess.fmt != "wav":
            return 0.0
        # 只有**单帧超过服务端窗口**（max_ahead_s）才是"不可满足"、才放行。
        # 客户端把 credit_s 调小是它自己的选择，服务端必须尊重（P6 契约：一个单元也不能把余额冲过去）。
        # **但本特例优先**：单帧真的塞不进服务端窗口时，尊重小额度会变成死等（codex 指出的口径冲突）。
        oversize = need_s > sess.cfg.max_ahead_s
        # 浮点残差容差：服务端按 WAV 样本数累加秒数，客户端 ack 是 round(…,3) 后发的，
        # 播完仍可能留下不到 1ms 的正差 —— 用严格 `> 0.0` 会永远不放行（实测卡死 5s 丢单元）。
        EPS = 0.001

        def over():
            if oversize:
                return (sess.sent_s - sess.acked_s > EPS
                        or sess.sent_units + need_units - sess.acked_units > sess.cfg.max_ahead_units)
            return (sess.sent_s + need_s - sess.acked_s > sess.credit_s
                    or sess.sent_units + need_units - sess.acked_units > sess.cfg.max_ahead_units)

        progress = sess.acked_s + sess.acked_units * 1e-6
        last_move = time.monotonic()
        # 宽限 = 静默上界（写出时间轴终点 ∪ 上次 ack + 未播完秒数）+ credit_wait，见 credit_patience。
        patience = credit_patience(sess.cfg.credit_wait, sess.sent_end_wall,
                                   sess.sent_s, sess.acked_s, last_move)
        while not sess.cancelled and over():
            now = time.monotonic()
            now_s = sess.acked_s + sess.acked_units * 1e-6
            if now_s > progress:                   # ack 还在走 ⇒ 正常背压，继续等
                progress, last_move = now_s, now
                patience = credit_patience(sess.cfg.credit_wait, sess.sent_end_wall,
                                           sess.sent_s, sess.acked_s, last_move)
            elif now - last_move > patience:
                sess.credit_timeouts += 1
                log("%s credit 卡死：已发 %.4fs/%d 段、已播 %.4fs/%d 段、本帧 %.4fs/%d 段、"
                    "差额 %.4fs、上限 %.1fs/%d 段、%.1fs 无播放进度（宽限 %.1fs）"
                    % (sess.id, sess.sent_s, sess.sent_units, sess.acked_s, sess.acked_units,
                       need_s, need_units, sess.sent_s - sess.acked_s,
                       sess.credit_s, sess.cfg.max_ahead_units, sess.cfg.credit_wait, patience))
                raise StalledCredit("客户端 %.1fs 内没有任何播放进度" % patience)
            await asyncio.sleep(0.05)
        return time.monotonic() - started

    def _open(self, frame):
        extra = frame.get("extra_body") or {}
        # 只认 `extra_body.protocol_version`（复核：顶层字段一律不认，免得两处口径打架）。
        try:
            ver = int(extra.get("protocol_version"))
        except (TypeError, ValueError):
            ver = None
        if ver != PROTOCOL_VERSION:
            # 方案 §4.1：本机强制版本匹配。旧壳（不带 protocol_version）也会落到这里——
            # 回滚是**整套**切回旧版本，不做长期混搭兼容层。
            raise BadProtocol("本机 v2 只接 protocol_version=%d（收到 %r）；请把壳与服务整套切回配套版本"
                              % (PROTOCOL_VERSION, ver))
        gen = extra.get("generation", extra.get("turn_id", 1))
        try:
            gen = int(gen)
        except (TypeError, ValueError):
            gen = 1
        voice = (frame.get("voice") or self.cfg.default_voice).strip().lower()
        voice = voice.replace("-irodori", "") or self.cfg.default_voice
        fmt = (frame.get("response_format") or "wav").strip().lower()
        return Session(self.cfg, "s" + uuid.uuid4().hex[:12], gen, voice, fmt)

    @staticmethod
    def _log_unit(sess, unit):
        log("%s 单元 %s [%d,%d) reason=%s %s" % (sess.id, unit.unit_id, unit.start, unit.end, unit.reason,
                                              text_meta(unit.speech_text)))

    async def _on_text(self, sess, piece):
        if sess.state != "OPEN" or not piece:
            return
        if len(sess.text.text) + len(piece) > self.cfg.max_chars:
            await sess.fail("capacity_text", "原文超过 %d code point" % self.cfg.max_chars)
            await self._terminate(sess)
            return
        for unit in sess.text.feed(piece, time.monotonic()):
            self._log_unit(sess, unit)
            await self._enqueue(sess, unit)

    async def _enqueue(self, sess, unit):
        if sess.cancelled:
            return
        # 复核问题 2：这里原来查的是**累计** `counts["units"]`，多轮下来第 65 个就报错，
        # 而契约（方案 §3.4）说的是"**未提交**单元最多 64"→ 查待处理数。
        if len(sess.pending) >= self.cfg.max_units:
            await sess.fail("capacity_units",
                            "未提交单元超过 %d（待处理 %d）" % (self.cfg.max_units, len(sess.pending)))
            await self._terminate(sess)
            return
        sess.counts["units"] += 1
        sess.pending.append(unit.unit_id)
        await sess.units.put(unit)

    async def _cancel(self, sess):
        if sess.cancelled:
            return
        sess.cancelled = True
        sess.state = "CANCELLED"
        await self._terminate(sess)

    async def _terminate(self, sess):
        """丢掉没跑的单元、**作废还在出站队列里的待发帧**、撤掉渲染。

        状态由调用方决定（cancel 走 CANCELLED，容量错误走 FAILED）。
        复核问题 1：取消时不清出站队列、writer 也不看状态 → `CANCELLED` 之后仍会把
        排队里的音频发出去。已经在 `ws.send_bytes` 里的那帧撤不回来，但**排队中的一律丢**。
        """
        sess.text.cancel()
        if sess.prefetch:                       # 预取也不能漏：取消后它的结果没人取
            self._T0.pop(sess.prefetch[0].unit_id, None)   # 它的时间账也没人清了（codex 复核）
            if sess.prefetch[1]:                # 无声单元的预取槽没有 task（codex 复核）
                sess.prefetch[1].cancel()
            sess.prefetch = None
        # 当前单元若正卡在 `_pipeline()` 里，取消会让它直接抛 CancelledError 出栈，
        # 那条清理路径根本走不到 —— 它的账只能在这里按 pending 清（codex 复核）。
        for uid in sess.pending:
            self._T0.pop(uid, None)
        sess.pending.clear()
        while not sess.units.empty():                      # 没跑的单元直接丢
            try:
                sess.units.get_nowait()
            except asyncio.QueueEmpty:
                break
        keep = []
        while not sess.out.empty():                        # 待发**音频帧**作废；控制事件（error 等）保留
            try:
                kind, payload = sess.out.get_nowait()
            except asyncio.QueueEmpty:
                break
            if kind == "text":
                keep.append((kind, payload))
        for item in keep:
            sess.out.put_nowait(item)
        sess.pending_bytes = 0
        await self.cancel_render(sess)

    def _prefetch_next(self, sess):
        """提前翻译**下一单元**，与当前单元的渲染重叠 —— 修 P7 量到的单元间空档。

        codex 裁决的边界：只提前 `prepare`（**不提前渲染**，所以预取段不会越到当前单元前面发声）；
        一次只预取一个（桥的"每会话 2 单元在途"窗口：当前 + 预取各 1）；
        预取任务由会话持有，`_pipeline` 取回时才用；失败仍在**它自己的 unit_id** 上记。
        """
        if sess.cancelled or sess.prefetch:
            return
        try:
            nxt = sess.units.get_nowait()
        except asyncio.QueueEmpty:
            return
        sess.prefetch = self._prepare_ahead(sess, nxt)

    def _prepare_ahead(self, sess, nxt):
        self._T0.setdefault(nxt.unit_id, time.monotonic())   # 预算从此刻起算
        # codex 复核：`session_text` 在提交单元时就定好了 kind，所以无声段在这里**已经知道**。
        # 不建 task（省掉一次没用的 /v2/prepare）；但**必须留着这个单元占住预取槽** ——
        # 直接跳过/丢回队列会打乱原文显示顺序（"有声 → 心声 → 有声"）。
        task = None
        if nxt.kind != "no_audio":
            # 记**真准备耗时**：预取常在前一单元还在渲染时就完成了，从"预取发起"到"被取用"
            # 之间那段干等不能算进这个单元的预算（codex 复核：实测 prepare=17.6s 里大半是干等）。
            async def _timed(sess=sess, unit=nxt, t_start=time.monotonic()):
                out = await self.prepare(sess, unit)
                sess._PREP_S[unit.unit_id] = time.monotonic() - t_start
                return out
            task = asyncio.create_task(_timed())
        return (nxt, task)

    # ---------------- 任务 ----------------
    async def _writer(self, ws, sess):
        """**唯一**往 socket 写的任务：JSON 与二进制不会交错写坏帧。"""
        while True:
            kind, payload = await sess.out.get()
            if sess.cancelled and kind in ("bytes", "commit"):
                continue                                        # 取消后不再吐音频（复核问题 1）
            try:
                if kind == "commit":
                    # 情绪状态提交：队列 FIFO 保证这里执行时，这一子段的音频与 `sentence.end`
                    # **真的写出去了**。断线/取消时 writer 一退出，这条就永远不执行 → 没听到的
                    # 语气不会带到下一句（Sol r2 第 4 条：状态要挂在"真发出去"上）。
                    sess.emotion_prev, sess.emotion_ctx = payload
                elif kind == "text":
                    await ws.send_text(payload)
                else:
                    await ws.send_bytes(payload)
                    sess.pending_bytes = max(0, sess.pending_bytes - len(payload))
            except Exception as e:                                  # 断线：出站就到此为止
                log("writer 退出: %s: %s" % (type(e).__name__, text_meta(e)))
                return

    async def _dispatcher(self, sess):
        """顺序处理单元：一次一个，让 cancel 有确定的切点。任何异常都必须变成事件，不能静默死。"""
        while True:
            # 预取的单元**就是**下一个要处理的单元（否则预取结果会被丢掉、白翻译一遍）
            pf = sess.prefetch
            sess.prefetch = None
            if pf is None:
                unit = await sess.units.get()
            else:
                unit = pf[0]
            sess.cur_pf = pf                     # 交给 _pipeline 复用（t0 不重新起算）
            if sess.cancelled:
                continue
            try:
                if pf is None and unit.kind != "no_audio":
                    pf = self._prepare_ahead(sess, unit)
                    sess.cur_pf = pf  # 首单元取出即准备，与 credit 等待重叠。
                # P6：开跑前先看余额（超时就砍这个单元，不白烧 GPU）。**必须在 try 里** ——
                # 这里的等待也是"这个单元失败了"，不能让它逃出 dispatcher：那样会话会带着
                # OPEN 状态假活着、单元烂在队列里、客户端一条事件都收不到（codex 复核第 1 条）。
                sess.credit_waited += await self._wait_credit(sess)
                if sess.cancelled:
                    continue
                if unit.kind == "no_audio":
                    self._prefetch_next(sess)
                await self.process_unit(sess, unit)
            except asyncio.CancelledError:
                raise
            except StalledCredit as e:
                await self._fail_span(sess, unit, "credit", str(e))
            except Exception as e:                                   # 兜底：单单元异常不拖垮会话
                log("unit %s 未捕获异常: %s: %s" % (unit.unit_id, type(e).__name__, text_meta(e)))
                await self._fail_span(sess, unit, "internal", "%s: %s" % (type(e).__name__, str(e)[:160]))
            finally:
                if pf and pf[1] and not pf[1].done():
                    pf[1].cancel()
                    await asyncio.gather(pf[1], return_exceptions=True)
                sess.cur_pf = None
                sess._PREP_S.pop(unit.unit_id, None)
                self._T0.pop(unit.unit_id, None)
                if unit.unit_id in sess.pending:
                    sess.pending.remove(unit.unit_id)
                # 预取槽里的单元还没处理，不能算"队列空了"—— 否则它被取回之前就收尾，
                # 最后一个单元（很可能正是被预取的那个）会被整个吞掉（测试实锤）。
                if (sess.state == "INPUT_CLOSED" and sess.units.empty()
                        and not sess.pending and not sess.prefetch):
                    await self._finish(sess)

    async def _ticker(self, sess):
        """空闲冲刷：LLM 停顿但没换行时也要把最后一句切出来（切句器自己的 idle 窗口 0.8s）。"""
        while True:
            await asyncio.sleep(TICK_S)
            if sess.state == "OPEN" and not sess.cancelled:
                for unit in sess.text.tick(time.monotonic()):
                    self._log_unit(sess, unit)
                    await self._enqueue(sess, unit)

    async def _finish(self, sess):
        if sess.state in ("FINISHED", "CANCELLED", "FAILED"):
            return
        sess.state = "FINISHED"
        await self.close_render(sess)
        c = sess.counts
        await sess.send_json({"event": "session.finished", "session_id": sess.id,
                              "generation": sess.generation, "units": c["units"],
                              "audio": c["audio"], "no_audio": c["no_audio"], "failed": c["failed"],
                              "status": "partial" if c["failed"] else "complete"})

    async def _flush(self, sess):
        """等 writer 把队列里剩下的帧发完（断线/出错时也尽力），最多 1s。"""
        t0 = time.monotonic()
        while not sess.out.empty() and time.monotonic() - t0 < 1.0:
            await asyncio.sleep(0.02)


def install(app, cfg=None, transport=None):
    """把 WS 与既有 HTTP 路由挂到 app 上（同端口，壳那侧只换地址不换协议）。"""
    co = Coordinator(cfg, transport)
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application) as state:
            try:
                yield state
            finally:
                await co.aclose()

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health():
        return {"ok": True, "voices": list(co.cfg.voices), "config": co.cfg.as_dict(),
                "sessions": [s.info() for s in co.sessions.values()]}

    @app.get("/api/v1/audio/speech/models")
    async def models():
        return {"models": [{"id": "albion-stream", "name": "本机流式（Irodori 桥）"}]}

    @app.get("/api/v1/audio/speech/voices")
    async def voices():
        return {"voices": [{"id": v, "name": v} for v in co.cfg.voices]}

    for path in WS_PATHS:
        app.add_api_websocket_route(path, co.run)
    app.state.coordinator = co
    return app


def create_app(cfg=None, transport=None):
    app = FastAPI()
    return install(app, cfg, transport)


if __name__ == "__main__":
    import uvicorn
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "9885")),
                            log_level="info", access_log=False)
    redact_uvicorn_logs()
    uvicorn.Server(config).run()
