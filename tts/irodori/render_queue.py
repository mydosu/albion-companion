# -*- coding: utf-8 -*-
"""渲染队列与 `/v2/render`（P3 第二部分，规格见方案 §3.4）。

解决 I 侧三个真问题：
1. **没有互斥**：`_run_infer()` 靠改全局 `sys.argv` 调 `infer.main()`，同进程并发进来会互踩（而且
   GPU 只有一块）→ 这里做**全局单 GPU 队列**，同一时刻只有一段在推理；
2. **临时文件重名**：老路径写死 `irodori_{ship}_seg_{i}.wav`，两个会话同时到第 i 段就互相覆盖
   → 每段用 `mkdtemp()` 唯一目录，读完立刻删；
3. **没有取消语义**：老 SSE 生成器一旦开始就只能跑到尾 → 这里按 `{session_id, generation}` 支持取消：
   未起跑的段立刻出队、正在跑的 GPU 段跑完（独占不能中断）但**结果丢弃**、旧世代事件一律不发出。

取消契约（与监督方敲定，逐条实现）：
- 取消是**幂等**的，按 `{session_id, generation}` 定位；
- 未起跑的段从队列移除；正在跑的 GPU 段继续独占到推理返回，随后丢弃结果；
- SSE 还连着时发 `render.cancelled`；连接已断则不依赖该事件；
- 旧 generation 的事件一律不播放、不更新字幕（由调用方按 generation 判，服务端这一层直接不发）；
- 取消既不算 `unit.failed`，也不算成功完成。
"""
import asyncio
import base64
import collections
from contextlib import asynccontextmanager
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
import uuid

import scene_plan as SP          # family/caption/cfg 的标准表就在这里（纯 CPU，无副作用）
import emotion_codebook as CB    # v3 白名单码本（同样纯 CPU）

PLAN_VERSION = 2

DEFAULT_SHIP = "albion"          # 与 irodori_serve.DEFAULT_SHIP 同值：单元没带 voice 时用于自检与日志

# ---- 渲染后自检（2026-10-30）-----------------------------------------------------
# 生产不传 `--seed` ⇒ 每句随机采样。宣传片录制时听到两件偶发的事：同一条回复里某几句基频掉到
# 121–148Hz（正常 296–380Hz，听起来是男声），以及原始渲染把话截在响处（末 2ms 还有 -25～-28dBFS，
# 下一刻就是数字零）。后者 gap_shaper 无法用 30ms 淡出接（门槛过不了），只能硬接零。
# 这里渲完量两样，命中就换一个随机 seed 重渲**一次**，按「不男声 > 不截尾 > 原结果」取更好的那次。
MALE_GUARD_SHIPS = ("albion",)   # 只查 albion：其他船的声线本来就不同，套这个阈值没意义
MALE_F0_MAX_HZ = 200.0           # albion 参考音 337–368Hz；实测漂移 121–148Hz，200 是中间的空档
TAIL_CLIP_DB = 30.0              # 原始 wav 末 30ms 峰值 > 全段峰值 -30dB ⇒ 上游把话截在响处
TAIL_PROBE_S = 0.03              # 「末段」取多长（与 gap_shaper.FADE_GATE_DB 的 30ms 同口径）


def run_infer_synth(unit, out_wav, run_infer, default_ship):
    """把**当前进程**的 `_run_infer` 包成队列要的合成腿：生产接线用这个。

    为什么不让队列自己去 `import irodori_serve`：I 服务支持以脚本方式启动，那时模块名是
    `__main__`，按名字导入会把同一个文件再导入一份（两套模块状态、两份模型缓存）。

    `unit["seed"]`（可选）原样透传 —— 只给听测配对种子用（astra-3 §4.1），不改生产默认行为。
    """
    import soundfile as sf
    ship = unit.get("voice") or default_ship
    run_infer(unit["text"], unit.get("caption") or "", unit.get("cfg") or 3.0, out_wav, ship,
              seed=unit.get("seed"))
    d, sr = sf.read(out_wav, dtype="float32")
    return sr, d


def _default_synth(unit, out_wav):
    """离线兜底合成腿（懒加载，测试环境不该被 torch 拖垮）。

    生产**别用它**——它按模块名导入 `irodori_serve`，脚本启动时会导到第二份模块；
    生产用 `RenderQueue(synth=functools.partial(run_infer_synth, ...))` 注入本进程的 `_run_infer`。
    """
    from irodori_serve import DEFAULT_SHIP, _run_infer
    return run_infer_synth(unit, out_wav, _run_infer, DEFAULT_SHIP)


def _default_encode(wave, sr, fmt):
    """单段波形 → 该格式的完整音频文件字节（与老 `/tts_stream` 同一套：wav 走 soundfile，其余 ffmpeg）。"""
    import io
    import numpy as np
    import soundfile as sf
    if fmt == "wav":
        buf = io.BytesIO()
        sf.write(buf, wave, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()
    import subprocess
    r = subprocess.run([os.environ.get("IRODORI_FFMPEG", "ffmpeg"), "-hide_banner", "-loglevel", "error",
                        "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                        "-f", fmt, "pipe:1"],
                       input=np.ascontiguousarray(np.asarray(wave), dtype="<f4").tobytes(),
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg %s 失败: %s" % (fmt, r.stderr.decode("utf-8", "ignore")[:200]))
    return r.stdout


# ---- 渲染结果自检：量尾电平与 F0 ------------------------------------------------
# 纯 numpy（不加依赖），跑在 GPU 队列的合成线程里：一段几秒的音频只多几毫秒 CPU。

def _random_seed():
    """本模块自己挑 seed：`infer.py` 不传 `--seed` 时内部用 `secrets.randbits(63)` 随机采样，
    外面看不到到底用了什么。这里显式传一个同范围的随机值，日志里的 seed 才是**真用过**的。"""
    return secrets.randbelow(2 ** 31)          # 与 `_check_unit` 允许的 0..2^31-1 同范围


def _mono(wave):
    import numpy as np
    a = np.asarray(wave, dtype="float64")
    return a.mean(axis=1) if a.ndim > 1 else a


def _tail_db(wave, sr):
    """原始 wav 末 `TAIL_PROBE_S` 的峰值相对全段峰值（dB，≤0）；短于该窗或全零 → None。

    > `-TAIL_CLIP_DB` 就是「上游把话截在响处」：gap_shaper 的 30ms 淡出门槛用的是同一判据
    （`FADE_GATE_DB`），过不了门就只能硬接数字零，听起来是最后一个字被掐掉。
    """
    import numpy as np
    a = _mono(wave)
    n = int(TAIL_PROBE_S * sr) if sr > 0 else 0
    if n <= 0 or len(a) < n:
        return None
    peak = float(np.abs(a).max())
    if peak <= 0:
        return None
    return 20.0 * float(np.log10(max(float(np.abs(a[-n:]).max()), 1e-12) / peak))


def _f0_median(wave, sr, voicing=0.5, lo_hz=70.0, hi_hz=450.0):
    """自相关 F0 中位数（Hz）；无声段不计入，有效帧少于 5 个返回 None。

    判据与 `tests/tts/probes/emotion_bench.f0_track` 一致（40ms 窗 / 10ms 步、归一化自相关峰
    ≥ 0.5、比峰值低 40dB 的帧当无声）。自相关走 FFT：这段代码在生产里每句都要跑，
    O(n²) 的 `np.correlate` 会把几秒的音频拖成秒级 CPU 开销，白占 GPU 队列。
    """
    import numpy as np
    a = _mono(wave)
    if sr <= 0 or hi_hz <= lo_hz:
        return None
    fl, hop = max(2, int(0.04 * sr)), max(1, int(0.01 * sr))
    if len(a) <= fl:
        return None
    peak = float(np.abs(a).max())
    if peak <= 0:
        return None
    silent = max(peak * 10 ** (-40.0 / 20.0), 1e-3)
    lo = max(1, int(sr / hi_hz))
    hi = min(fl, int(sr / lo_hz))
    if hi - lo < 2:
        return None
    nfft = 1
    while nfft < 2 * fl:
        nfft <<= 1
    out = []
    for i in range(0, len(a) - fl, hop):
        fr = a[i:i + fl]
        if float(np.sqrt((fr ** 2).mean())) < silent:
            continue
        fr = fr - fr.mean()
        spec = np.fft.rfft(fr, nfft)
        ac = np.fft.irfft(spec * np.conj(spec), nfft)[:fl]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi] / ac[0]
        k = int(np.argmax(seg))
        if seg[k] < voicing:
            continue
        out.append(sr / float(lo + k))
    return float(np.median(out)) if len(out) >= 5 else None


def _quality(sr, wave, ship):
    """一段渲染结果的两项自检。`male` 只对 `MALE_GUARD_SHIPS` 里的船成立。"""
    tail_db = _tail_db(wave, sr)
    f0 = _f0_median(wave, sr)
    return dict(tail_db=tail_db, f0_hz=f0,
                male=bool(ship in MALE_GUARD_SHIPS and f0 is not None and f0 < MALE_F0_MAX_HZ),
                clip=bool(tail_db is not None and tail_db > -TAIL_CLIP_DB))


def _bad_reasons(q):
    return ",".join(k for k in ("male", "clip") if q.get(k))


def _rank(q):
    """「不男声 > 不截尾 > 原结果」：元组逐项比较，平手保留原结果。"""
    return (0 if q.get("male") else 1, 0 if q.get("clip") else 1)


def _fmt_db(v):
    return "—" if v is None else "%.1fdB" % v


def _fmt_hz(v):
    return "—" if v is None else "%.0fHz" % v


def _quality_note(job, res):
    """最终日志行里的自检字段：caption/cfg + 每次尝试的 seed/尾电平/F0 + 是否重渲及原因。"""
    tries = res.get("quality")
    if not tries:
        return "seed=—"
    parts = []
    for i, t in enumerate(tries):
        flags = _bad_reasons(t)
        parts.append("try%d(seed=%s tail=%s f0=%s%s)"
                     % (i + 1, t.get("seed"), _fmt_db(t.get("tail_db")),
                        _fmt_hz(t.get("f0_hz")), " " + flags if flags else ""))
    note = "caption=%r cfg=%s %s picked=%s" % ((job.unit.get("caption") or "")[:40],
                                              job.unit.get("cfg"), " ".join(parts),
                                              res.get("picked"))
    if res.get("rerender_skipped"):
        note += " rerender=%s" % res["rerender_skipped"]
    elif res.get("explicit_seed"):
        note += " rerender=explicit_seed"
    elif res.get("rerender"):
        note += " rerender=%s" % res["rerender"]
    else:
        note += " rerender=none"
    if res.get("retry_error"):
        note += " retry_failed=%s" % res["retry_error"]
    return note


class _Job:
    __slots__ = ("session_id", "generation", "unit", "future", "queued_at", "state")

    def __init__(self, session_id, generation, unit, future):
        self.session_id, self.generation, self.unit, self.future = session_id, generation, unit, future
        self.queued_at = time.monotonic()  # 随 job 释放，取消/撤回不再留下全局计时记录。
        self.state = None

    @property
    def key(self):
        return (self.session_id, self.generation)


class RenderQueue:
    """进程内全局单 GPU 队列。同一时刻只跑一段推理，其余按提交顺序等。

    ponytail: 队列只在进程内、只有一个消费者（这块 GPU），不引 broker/外部队列——没有第二个消费者。
    """

    def __init__(self, synth=None, encode=None, tmp_root=None, max_queued=8,
                 max_sessions=128, lease_seconds=600, clock=time.monotonic, send_timeout=30):
        self._synth = synth or _default_synth
        self._encode = encode or _default_encode
        self._tmp_root = tmp_root or tempfile.gettempdir()
        self.max_queued = max_queued                   # 方案 §3.4：GPU 待发上限（不是无界队列）
        self._jobs = collections.deque()
        self._aborted_streams = 0
        self._dropped = {}                       # (session,generation) -> 断流时没送达的 unit_id
        self._wake = asyncio.Event()
        self._retired = {}                       # Only closing live sessions; never historical tombstones.
        self._sessions = {}
        self.max_sessions, self.lease_seconds = max_sessions, lease_seconds
        self._clock = clock
        self.send_timeout = send_timeout
        self.epoch = uuid.uuid4().hex
        self._sweeper = None
        self.expired_sessions = self.rejected_unknown = 0
        self._done_units = {}                    # (session_id, generation) -> 已渲染完成的 unit_id
        self._inflight = {}                      # 同上 -> 队列里/在跑的 unit_id（并发重发也只跑一次）
        self._cancel_events = {}                 # (session_id, generation) → asyncio.Event
        self._watch_refs = {}                    # 同世代可有多条 SSE，最后一条退出才释放信号。
        self._worker = None
        self._running = None
        self.stats = {"queued": 0, "started": 0, "ok": 0, "failed": 0, "rejected": 0,
                      "cancelled": 0, "duplicate": 0, "discarded_running": 0, "withdrawn": 0}

    def encode(self, wave, sr, fmt):
        """编码是纯 CPU（wav 走 soundfile、其余 ffmpeg），不必占着 GPU 槽。"""
        return self._encode(wave, sr, fmt)

    # ---------------- 队列 ----------------
    @property
    def queued(self):
        return len(self._jobs)

    @property
    def running(self):
        return None if self._running is None else dict(self._running.unit)

    def open(self, voice=None):
        """Only the backend issues IDs. Unknown IDs can never reactivate a session."""
        self.sweep()
        if len(self._sessions) >= self.max_sessions:
            raise OverflowError("render_session_capacity")
        sid = self.epoch + "-" + uuid.uuid4().hex
        self._sessions[sid] = dict(generation=1, voice=voice, touched=self._clock(),
                                   closed=False, cancelled=False, expired=False)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if self._sweeper is None or self._sweeper.done():
                self._sweeper = loop.create_task(self._sweep_loop())
        return sid, 1

    def accepts(self, sid, generation, voice=None):
        state = self._sessions.get(sid) if isinstance(sid, str) else None
        return bool(state and type(generation) is int and generation == state["generation"]
                    and not state["closed"] and not state["cancelled"]
                    and (voice is None or state["voice"] in (None, voice)))

    def _release(self, sid, generation):
        state = self._sessions.get(sid)
        key = (sid, generation)
        if (state and state["closed"] and not self._watch_refs.get(key)
                and not any(j.key == key for j in self._jobs)
                and not (self._running and self._running.key == key)):
            self._sessions.pop(sid, None)
            self._retired.pop(sid, None)
            self._drop_ledgers(sid, generation)
            self._cancel_events.pop(key, None)

    def sweep(self):
        for sid, state in list(self._sessions.items()):
            if not state["expired"] and self._clock() - state["touched"] >= self.lease_seconds:
                state["expired"] = True
                self.expired_sessions += 1
                self.cancel(sid, state["generation"])

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(30)
            self.sweep()

    async def aclose(self):
        for sid, state in list(self._sessions.items()):
            self.cancel(sid, state["generation"])
        # A running GPU thread cannot be stopped. Keep its state until it returns.
        while self._running is not None:
            await asyncio.sleep(0.01)
        tasks = [t for t in (self._worker, self._sweeper) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def resources(self):
        return dict(active_sessions=len(self._sessions),
                    closing_sessions=sum(s["closed"] for s in self._sessions.values()),
                    retired_entries=len(self._retired), watchers=sum(self._watch_refs.values()),
                    inflight_units=sum(map(len, self._inflight.values())),
                    done_units=sum(map(len, self._done_units.values())),
                    dropped_units=sum(map(len, self._dropped.values())),
                    expired_sessions=self.expired_sessions, rejected_unknown=self.rejected_unknown,
                    epoch=self.epoch, render_lifecycle_version=2)

    def is_cancelled(self, session_id, generation):
        """Unknown or cancelled sessions cannot emit. INPUT_CLOSED still drains accepted work."""
        state = self._sessions.get(session_id) if isinstance(session_id, str) else None
        return not state or generation != state["generation"] or state["cancelled"]

    def watch(self, session_id, generation):
        """取该世代的取消信号（SSE 端等它来收尾）。"""
        key = (session_id, generation)
        if not self.accepts(session_id, generation):
            raise ValueError("unknown_or_closed_render_session")
        self._sessions[session_id]["touched"] = self._clock()
        self._watch_refs[key] = self._watch_refs.get(key, 0) + 1
        return self._cancel_events.setdefault(key, asyncio.Event())

    def unwatch(self, session_id, generation):
        key = (session_id, generation)
        refs = self._watch_refs.get(key, 0) - 1
        if refs > 0:
            self._watch_refs[key] = refs
        else:
            self._watch_refs.pop(key, None)
            self._cancel_events.pop(key, None)
        self._release(session_id, generation)

    def submit(self, session_id, generation, unit):
        """把一段排进 GPU 队列，返回 Future（结果只可能是 ok / failed / cancelled）。"""
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        if not isinstance(generation, int) or isinstance(generation, bool):
            fut.set_result({"status": "failed", "error": "bad_generation: 必须是整数"})
            return fut
        if not self.accepts(session_id, generation, unit.get("voice")):
            self.rejected_unknown += 1
            fut.set_result({"status": "failed", "error": "unknown_or_closed_render_session"})
            return fut
        self._sessions[session_id]["touched"] = self._clock()
        uid = unit.get("unit_id")
        key = (session_id, generation)
        if uid and uid in self._dropped.get(key, ()):
            # 断流时没送达的段：不算"已完成"，重发要真的再合成一次（下面会把它从作废账里划掉）
            self._dropped[key].discard(uid)
            self._done_units.get(key, set()).discard(uid)
        elif uid and (uid in self._done_units.get(key, ()) or uid in self._inflight.get(key, ())):
            # 同世代同 unit 只渲染一次：已完成的和**正在途中的**都算（并发重发也只跑一次推理）
            self.stats["duplicate"] += 1
            fut.set_result({"status": "duplicate", "unit_id": uid})
            return fut
        if len(self._jobs) >= self.max_queued:
            # 方案 §3.4 的待发上限：宁可明确拒绝一段，也不让队列无界堆积
            self.stats["rejected"] += 1
            fut.set_result({"status": "failed",
                            "error": "queue_full: %d 段在等（上限 %d）" % (len(self._jobs), self.max_queued)})
            return fut
        if uid:
            self._inflight.setdefault((session_id, generation), set()).add(uid)   # 登记在途，失败/取消时释放
        job = _Job(session_id, generation, dict(unit), fut)
        job.state = self._sessions[session_id]
        self._jobs.append(job)
        self.stats["queued"] += 1
        self._ensure_worker()
        self._wake.set()
        return fut

    def _retire(self, session_id, generation):
        """把该世代标记为退役（取消或显式 close），此后同世代的新段一律拒绝。

        语义（与方案 §3.3 的会话状态机对齐）：**一个世代 = 一个回合**，回合内单元会**陆续到达**，
        所以同一个 generation 允许送多批 `/v2/render`；收尾由协调器显式 `close`（或 `cancel`）宣告，
        不拿"某批发完 done"当回合结束——那只代表这一批渲染完了。
        """
        self._retired[session_id] = max(self._retired.get(session_id, generation), generation)

    def close(self, session_id, generation):
        """Close input; drain accepted work/watchers before releasing the registration."""
        if not self.accepts(session_id, generation):
            return False
        self._sessions[session_id]["closed"] = True
        self._retire(session_id, generation)
        self._release(session_id, generation)
        return True

    def _drop_ledgers(self, session_id, generation):
        self._done_units.pop((session_id, generation), None)
        self._inflight.pop((session_id, generation), None)
        self._dropped.pop((session_id, generation), None)      # 作废账同生共死

    def cancel(self, session_id, generation):
        """幂等取消：未起跑的段出队（结果置 cancelled），在跑的段跑完丢结果，SSE 端收到信号。"""
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ValueError("generation 必须是整数（世代单调递增，不能拿字符串当世代）")
        key = (session_id, generation)
        state = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if not state or generation != state["generation"] or state["cancelled"]:
            return False
        state["cancelled"] = state["closed"] = True
        self._retire(session_id, generation)
        self._drop_ledgers(session_id, generation)        # 取消也清账：退役的世代不再收新段，账本没用
        for job in list(self._jobs):
            if job.key == key:
                self._jobs.remove(job)
                self.stats["cancelled"] += 1
                if not job.future.done():
                    job.future.set_result({"status": "cancelled"})
        ev = self._cancel_events.get(key)
        if ev is not None:
            ev.set()
        self._release(session_id, generation)
        return True

    def withdraw(self, future):
        """摘掉**还没起跑**的 job（中断的那条流）：在跑的摘不掉，跑完丢结果。

        在途登记要一起释放——否则同 `unit_id` 重发会被判 duplicate 静默吞掉（复核二轮问题 1）。
        """
        for job in list(self._jobs):
            if job.future is future:
                self._jobs.remove(job)
                uid = job.unit.get("unit_id")
                if uid:
                    self._inflight.get(job.key, set()).discard(uid)
                if not job.future.done():
                    job.future.set_result({"status": "cancelled"})
                self.stats["withdrawn"] += 1
                self._release(job.session_id, job.generation)
                return True
        return False

    def _ensure_worker(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_event_loop().create_task(self._run())

    async def _run(self):
        while True:
            if not self._jobs:
                self._wake.clear()
                await self._wake.wait()
                continue
            job = self._jobs.popleft()
            if job.state["cancelled"]:
                if not job.future.done():
                    job.future.set_result({"status": "cancelled"})
                continue
            self._running = job
            self.stats["started"] += 1
            _uid = job.unit.get("unit_id") or "-"
            _t_run = time.monotonic()
            _mode = caption_mode_of(job.unit)      # free 实验单元在日志里一眼能看出来
            print("[render_queue] %s unit=%s queue_ms=%d%s"
                  % (job.session_id, _uid, int((_t_run - job.queued_at) * 1000),
                     " caption_mode=%s" % _mode if _mode else ""), flush=True)
            try:
                res = await asyncio.to_thread(self._render_one, job)
            except Exception as e:
                res = {"status": "failed", "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
            finally:
                self._running = None
            if job.state["cancelled"]:
                # 在跑的 GPU 段不能中断，但**结果丢弃**（旧世代一律不播）
                self.stats["discarded_running"] += 1
                res = {"status": "cancelled"}
            else:
                self.stats["ok" if res["status"] == "ok" else "failed"] += 1
                uid = job.unit.get("unit_id")
                if uid:
                    self._inflight.get(job.key, set()).discard(uid)     # 一出结果就放掉在途登记
                    if res["status"] == "ok":
                        # 同世代同 unit 只渲染一次：同一回合可以分多批请求，重发的那份不能再出一次声
                        self._done_units.setdefault(job.key, set()).add(uid)
            if not job.future.done():
                # 最终状态（codex：S 断流后 I 侧继续跑的任务也必须有结局记录，否则看不出
                # 后续请求是不是被一个已超时但仍在跑的任务堵住）。synth_ms 是**合成调用墙钟耗时**，
                # 含 `_run_infer` 与其文件读写，不是纯 GPU 时间。
                print("[render_queue] %s unit=%s final=%s synth_ms=%s wall_ms=%d cancelled=%s %s"
                      % (job.session_id, _uid, res.get("status"), res.get("synth_ms"),
                         int((time.monotonic() - _t_run) * 1000),
                         self.is_cancelled(job.session_id, job.generation),
                         _quality_note(job, res)), flush=True)
                job.future.set_result(res)
            self._release(job.session_id, job.generation)

    # ---------------- 单段渲染 ----------------
    def _render_one(self, job):
        """一段 = 一个唯一临时目录；读完立刻删（两个会话不会互相覆盖文件）。

        渲完先自检（2026-10-30）：原始 wav 截在响处、或 albion 的 F0 中位掉进男声区，就换一个
        随机 seed 重渲**一次**，按「不男声 > 不截尾 > 原结果」取更好的那次。
        单元显式带 seed（听测配对）时原样透传、**不重渲**：那是别人量过的条件，不能偷偷换。
        日志里的 seed 是真正传给 `_run_infer` 的值（见 `_random_seed`）。
        """
        d = tempfile.mkdtemp(prefix="irodori_%s_" % _safe_name(job.unit.get("unit_id") or "u"),
                             dir=self._tmp_root)
        try:
            unit = job.unit
            ship = str(unit.get("voice") or DEFAULT_SHIP).strip().lower()
            fixed = unit.get("seed")
            tries = []
            _t_synth = time.monotonic()

            def attempt(n):
                seed = fixed if (n == 1 and fixed is not None) else _random_seed()
                sr, wave = self._synth(dict(unit, seed=seed), os.path.join(d, "seg%d.wav" % n))
                q = _quality(sr, wave, ship)
                tries.append(dict(q, seed=seed, sr=sr, wave=wave))
                return tries[-1]

            first = attempt(1)
            reason = _bad_reasons(first)
            picked = 0
            retry_error = None
            skipped = None
            if fixed is None and reason:
                sid, gen = getattr(job, "session_id", None), getattr(job, "generation", None)
                if sid is not None and self.is_cancelled(sid, gen):
                    # 这一段已经取消了：重渲出来的结果照样会被丢弃，别再白烧一次 GPU（OBS 抢卡时尤其贵）
                    skipped = "skipped_cancelled"
                else:
                    try:
                        second = attempt(2)
                    except Exception as e:      # noqa: BLE001 - 重渲失败不能连累已经拿到的那一次
                        retry_error = "%s: %s" % (type(e).__name__, str(e)[:120])
                        print("[render_queue] %s unit=%s rerender=%s 失败，保留原结果: %s"
                              % (getattr(job, "session_id", "-"),
                                 job.unit.get("unit_id") or "-", reason, retry_error), flush=True)
                    else:
                        if _rank(second) > _rank(first):
                            picked = 1
            best = tries[picked]
            return {"status": "ok", "sr": best["sr"], "wave": best["wave"],
                    "synth_ms": int((time.monotonic() - _t_synth) * 1000),
                    "seed": best["seed"],
                    "quality": [{k: t[k] for k in ("seed", "tail_db", "f0_hz", "male", "clip")}
                                for t in tries],
                    "picked": picked + 1, "rerender": reason,
                    "rerender_skipped": skipped,
                    "retry_error": retry_error,
                    "explicit_seed": fixed is not None}
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---------------- 事件流 ----------------
    async def events(self, session_id, generation, units, fmt="wav"):
        """逐段产出事件（SSE 用）：audio_chunk 成组、unit.failed 明确原因、done 汇总、render.cancelled 收尾。"""
        key = (session_id, generation)
        cancel_ev = self.watch(session_id, generation)
        cancel_task = asyncio.ensure_future(cancel_ev.wait())   # 不给 wait() 传协程（3.11 起移除）
        done_units = failed = chunks = 0
        delivered_idx = 0         # 已交付到第几个单元（**在发帧那一处**推进，别拿 chunks 反推）
        finished = False          # 正常收尾（发完 done）才置 True；否则视为断线/中断 → 只作废这一段
        cur_fut = None
        try:
            for i, u in enumerate(units):
                if self.is_cancelled(session_id, generation):
                    yield self._ev("render.cancelled", {"session_id": session_id,
                                                        "generation": generation, "emitted": chunks})
                    return
                fut = self.submit(session_id, generation, u)
                cur_fut = fut
                await asyncio.wait([fut, cancel_task], return_when=asyncio.FIRST_COMPLETED)
                if self.is_cancelled(session_id, generation) or not fut.done():
                    yield self._ev("render.cancelled", {"session_id": session_id,
                                                        "generation": generation, "emitted": chunks})
                    return
                res = fut.result()
                base = {"unit_id": u.get("unit_id"), "sub_index": u.get("sub_index", i),
                        "session_id": session_id, "generation": generation}
                if res["status"] == "cancelled":
                    yield self._ev("render.cancelled", dict(base, emitted=chunks))
                    return
                if res["status"] == "duplicate":
                    delivered_idx = i + 1     # 早先那条流已经送达过：不算"未交付"
                    continue          # 同世代同 unit 已出过声：不当失败，也不重发音频
                if res["status"] == "failed":
                    failed += 1
                    delivered_idx = i + 1     # 失败已明确报给客户端（unit.failed），也不算"未交付"
                    yield self._ev("unit.failed", dict(base, error=res.get("error"),
                                                       text=(u.get("text") or "")[:60]))
                    continue
                try:
                    _t_enc = time.monotonic()
                    audio = await asyncio.to_thread(self._encode, res["wave"], res["sr"], fmt)
                    print("[render_queue] %s unit=%s encode_ms=%d"
                          % (session_id, u.get("unit_id") or "-",
                             int((time.monotonic() - _t_enc) * 1000)), flush=True)
                except Exception as e:
                    failed += 1
                    delivered_idx = i + 1
                    yield self._ev("unit.failed", dict(base, error="encode: %s: %s"
                                                       % (type(e).__name__, str(e)[:160])))
                    continue
                if self.is_cancelled(session_id, generation):
                    # 编码期间被取消：编码结果必须丢掉，一个字节都不发（复核发现的问题 1）
                    yield self._ev("render.cancelled", dict(base, emitted=chunks))
                    return
                chunks += 1
                done_units += 1
                delivered_idx = i + 1          # 这一段的帧**真的发出去了**才算交付
                yield self._ev("audio_chunk", dict(
                    base, format=fmt, sr=res["sr"], samples=len(res["wave"]),
                    style=u.get("style") or u.get("family"), scene=u.get("scene_id"),
                    audio_base64=base64.b64encode(audio).decode()))
            if self.is_cancelled(session_id, generation):
                yield self._ev("render.cancelled", {"session_id": session_id,
                                                    "generation": generation, "emitted": chunks})
                return
            yield self._ev("done", {"session_id": session_id, "generation": generation,
                                    "units": len(units), "audio": done_units, "failed": failed,
                                    "status": "partial" if failed else "complete"})
            finished = True
        finally:
            if not finished:
                # 断线 / 生成器被关闭 / 中途异常：**只作废这一段**——在跑的 GPU 段跑完、结果丢弃、
                # 一个字节都不发；**世代不失效**（原来这里调 cancel()，等于客户端断一次就把整轮
                # 后续单元全判死）。v2 契约下世代由协调器**显式** close/cancel 决定；单个单元流被
                # 中断（例如单元预算到点）不能毒死同回合剩下的单元——P4 集成时发现。
                self._aborted_streams += 1
                if cur_fut is not None and not cur_fut.done():
                    self.withdraw(cur_fut)     # 还没起跑的段直接摘掉：不许白烧一次 GPU（复核二轮问题 1）
                # 还没送达的段记进"作废账"：合成过也可能，但帧没到客户端 ⇒ 同世代重发**要真的再
                # 合成一次**，不能被判 duplicate 静默吞掉。按**遍历位置**记，不能拿出音频数反推
                # （遇到 duplicate 跳过或 unit.failed 时两者不等 —— 复核二轮问题 2）。
                if self.accepts(session_id, generation):
                    dropped = self._dropped.setdefault(key, set())
                    for u in units[delivered_idx:]:
                        if u.get("unit_id"):
                            dropped.add(u["unit_id"])
            cancel_task.cancel()
            # Release synchronously before another cancellation can interrupt cleanup awaits.
            self.unwatch(session_id, generation)
            await asyncio.gather(cancel_task, return_exceptions=True)

    @staticmethod
    def _ev(name, payload):
        import json
        return "event: %s\ndata: %s\n\n" % (name, json.dumps(payload, ensure_ascii=False))


def _safe_name(name):
    """临时目录名必须**文件系统安全**：unit_id 形如 `会话:世代:序号`，Windows 上 `:` 直接报
    WinError 123（P4 联调时真撞到：整条链路都通了，卡在建临时目录）。"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(name))[:24] or "u"


def _mount(app, path, endpoint, methods):
    """挂路由：**先删同路径的旧路由**再挂。

    生产 app 里早就注册过 `/tts`、`/tts_stream`（也许还有 `/v2/render`），而 FastAPI 不会用后注册的
    覆盖先注册的——"装上了新接线，请求却仍走旧处理函数"就是这么来的（复核发现的问题 1）。
    """
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != path]
    app.add_api_route(path, endpoint, methods=methods)


TOKEN_COUNT = None       # 由服务进程注入真 tokenizer 计数（见 irodori_serve._token_count）；None = 不检查

# ---- 实验用自由描述（caption_mode="free"）----------------------------------------
# 生产为什么要白名单：**为音质**，不是为安全（后端只听 127.0.0.1，网关也不会发这个字段）。
# 训练集外的 caption 是分布外条件，模型只能"猜"—— 所以默认路径必须继续只认 7 条训练锚。
# 但"能不能听懂"这件事本身要能量：这个 opt-in 模式只为评测台开关，不进任何生产路径。
CAPTION_MODES = ("free",)
FREE_CAPTION_MAX = 120          # 自由描述上限（字）。底模 caption 预算 512 token，120 字远在以内。
FREE_CFG_MIN, FREE_CFG_MAX = 1.5, 7.0


def caption_mode_of(u):
    """单元上的实验模式：None = 生产码本路径；"free" = 自由描述实验路径。"""
    mode = (u or {}).get("caption_mode")
    return mode if isinstance(mode, str) and mode in CAPTION_MODES else None


def _codebook_version_for(plan_version):
    """该计划版本该用哪个码本号（v4 起是 v2 码本）。"""
    return (CB.CODEBOOK_VERSION_V2 if plan_version >= SP.PLAN_VERSION_V4
            else CB.CODEBOOK_VERSION)


def free_caption_error(caption):
    """自由描述能不能进推理：非空、≤120 字、无控制字符（Cc/Cf/Cs）。返回原因或 None。"""
    if not isinstance(caption, str) or not caption.strip():
        return "必须是非空字符串"
    if len(caption) > FREE_CAPTION_MAX:
        return "过长（%d 字 > %d），先压短再送" % (len(caption), FREE_CAPTION_MAX)
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in caption):
        return "含控制字符（换行/制表/零宽/代理）：只收可见字符"
    return None


def _finite_number(x):
    """合法数值：非 bool、可转 float、有限。"""
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _check_unit(u, i, plan_version=2):
    """校验一份计划单元：字段类型、数值范围、以及 family/caption/cfg **三者自洽**。

    "字段齐全"不等于"计划可信"（复核发现的问题 4）：caption/cfg 必须等于该 family 的标准值，
    否则任意 caption、越界或 NaN 的 cfg 都能进推理。

    v3（2026-09-28）：族与 caption 不再一一对应（同族有弱/强两条训练锚），所以改成
    **白名单校验** —— (family, caption) 必须是 `emotion_codebook` 里训过的组合，cfg 必须等于
    该族的规定值。v2 走下面原样不变的老路径（回归逐字节不变）。

    v2 码本之后（2026-10-26）：带 `caption_mode: "free"` 的单元走**实验路径** —— 放行任意
    自由描述（≤120 字、无控制字符）与 1.5–7.0 的任意 cfg，只为让评测台量"现有 LoRA 能不能
    听懂训练集外的描述"。**不带这个字段时行为逐条不变**（生产网关从不发它）；family 仍须合法，
    codebook_version 若给出仍要与计划版本对得上。
    """
    for k in ("unit_id", "text", "caption", "family"):
        if not isinstance(u.get(k), str) or not u[k].strip():
            return "unit[%d] 计划不可信：%s 必须是非空字符串" % (i, k)
    if len(u["text"]) > 200:
        return "unit[%d] text 过长（%d 字符）" % (i, len(u["text"]))
    if TOKEN_COUNT is not None:
        # astra-3 §2.3.4：200 字符只是队列自己的守卫，**底模真正吃的是 token**（256 含 BOS）。
        # 超预算时模型会**静默右截断**（尾部条件整句丢失）—— 这里宁可显式失败，也不假装成功。
        try:
            n = TOKEN_COUNT(u["text"])
        except Exception as e:
            # 计数失败**不能**放过：本机实测 200 字符可达 801 token，放过就等于让它被底模
            # 静默右截断（尾部条件整句丢失）。宁可显式失败（Sol 第 6 轮第二反例）。
            return ("unit[%d] token 计数失败（%s: %s）：无法确认预算，拒绝放行"
                    % (i, type(e).__name__, str(e)[:80]))
        if n is not None and n > SP.TOKEN_LIMIT:
            return ("unit[%d] text 超 token 预算（%d > %d）：模型会静默截断，"
                    "请先按句界切开再送" % (i, n, SP.TOKEN_LIMIT))
    sd = u.get("seed")            # 可选：听测/实验用的配对随机种子（astra-3 §4.1）；生产默认不带
    if sd is not None and (isinstance(sd, bool) or not isinstance(sd, int)
                           or not 0 <= sd <= 2 ** 31 - 1):
        return "unit[%d] seed 非法：%r（要 0..2^31-1 的整数）" % (i, sd)
    fam = u["family"]
    if fam not in SP.FAMILIES:
        return "unit[%d] 未知 family %r（合法：%s）" % (i, fam, ",".join(sorted(SP.FAMILIES)))
    cfg = _finite_number(u.get("cfg"))
    if cfg is None or cfg <= 0 or cfg > 20:
        return "unit[%d] cfg 非法：%r" % (i, u.get("cfg"))
    mode = u.get("caption_mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in CAPTION_MODES:
            return ("unit[%d] caption_mode 非法：%r（只认 %s；缺省=生产码本路径）"
                    % (i, mode, "/".join(CAPTION_MODES)))
        why = free_caption_error(u["caption"])
        if why:
            return "unit[%d] free 模式 caption %s" % (i, why)
        if not FREE_CFG_MIN <= cfg <= FREE_CFG_MAX:
            return ("unit[%d] free 模式 cfg 越界：%s（允许 %.1f–%.1f；生产锚才锁族值）"
                    % (i, u.get("cfg"), FREE_CFG_MIN, FREE_CFG_MAX))
        if u.get("scene_id") is not None and _finite_number(u["scene_id"]) is None:
            return "unit[%d] scene_id 非法：%r" % (i, u.get("scene_id"))
        if u.get("codebook_version") is not None:
            cb_ver = _codebook_version_for(plan_version)
            if u["codebook_version"] != cb_ver:
                return ("unit[%d] codebook_version 与 plan_version 不匹配：%r 应为 %r"
                        % (i, u["codebook_version"], cb_ver))
        return None
    if plan_version >= SP.PLAN_VERSION_V3:
        cb_ver = _codebook_version_for(plan_version)
        if u.get("codebook_version") is not None and u["codebook_version"] != cb_ver:
            return ("unit[%d] codebook_version 与 plan_version 不匹配：%r 应为 %r"
                    % (i, u.get("codebook_version"), cb_ver))
        if cb_ver >= CB.CODEBOOK_VERSION_V2 and u.get("codebook_version") is None:
            # v4 必须**显式**声明码本号：缺字段就无从核对，不能默默按新码本放行（复核反例）
            return "unit[%d] plan v4 必须带 codebook_version（应为 %d）" % (i, cb_ver)
        want = CB.allowed_pairs(cb_ver).get((fam, u["caption"]))
        if want is None:
            return ("unit[%d] v3 码本外的条件：family=%s caption=%r（只允许训练集里出现过的锚）"
                    % (i, fam, u["caption"]))
        # cfg 必须等于**该族**的值，不是"某个族的值"：跨族组合是没训过的条件
        if abs(cfg - want) > 1e-6:
            return ("unit[%d] v3 cfg 与该族不符：family=%s 应为 cfg=%s，收到 cfg=%s"
                    % (i, fam, want, cfg))
        if u.get("scene_id") is not None and _finite_number(u["scene_id"]) is None:
            return "unit[%d] scene_id 非法：%r" % (i, u.get("scene_id"))
        return None
    cap, want_cfg = SP.FAMILIES[fam]
    if u["caption"] != cap or abs(cfg - want_cfg) > 1e-6:
        return ("unit[%d] 计划与 family 不符：family=%s 应为 caption=%r cfg=%s，"
                "收到 caption=%r cfg=%s" % (i, fam, cap, want_cfg, u["caption"], cfg))
    if u.get("scene_id") is not None and _finite_number(u["scene_id"]) is None:
        return "unit[%d] scene_id 非法：%r" % (i, u.get("scene_id"))
    return None


def install(app, queue=None, all_ships=None):
    """把 `/v2/render`、`/v2/render/cancel`、`/v2/render/stats` 挂到 I 已有 app 上。

    生产落地：`iq = RenderQueue(synth=...)` 后
    `render_queue.install(app, iq, all_ships=all_ships())`；
    旧的 `/tts`、`/tts_stream` 交给 `serve_v2.install` 重挂到同一条队列（本模块不管它们）。
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    q = queue or RenderQueue()
    ships = {str(s).strip().lower() for s in (all_ships or ()) if str(s).strip()}

    class RenderResponse(StreamingResponse):
        async def stream_response(self, send):
            async def bounded_send(message):
                await asyncio.wait_for(send(message), timeout=q.send_timeout)
            try:
                await super().stream_response(bounded_send)
            finally:
                # A peer that stops reading cannot retain a watcher indefinitely.
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()

    async def v2_render_open(request: Request):
        try:
            body = await request.json()
            voice = body.get("voice")
            if not isinstance(voice, str) or (ships and voice not in ships):
                raise ValueError("unknown_voice")
            sid, generation = q.open(voice)
        except OverflowError:
            return JSONResponse({"ok": False, "error": "render_session_capacity"}, status_code=429)
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        return {"ok": True, "session_id": sid, "generation": generation,
                "epoch": q.epoch, "render_lifecycle_version": 2}

    async def v2_render(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse({"status": "failed", "stage": "render",
                                 "error": "bad_request: %s" % str(e)[:120]}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        session_id = body.get("session_id")
        generation = body.get("generation")
        units = body.get("units") or []
        if not isinstance(units, list) or any(not isinstance(u, dict) for u in units):
            return JSONResponse({"error": "bad_units"}, status_code=400)
        if session_id is None or generation is None or not units:
            return JSONResponse({"status": "failed", "stage": "render",
                                 "error": "missing session_id/generation/units"}, status_code=400)
        if not isinstance(generation, int) or isinstance(generation, bool):
            return JSONResponse({"status": "failed", "stage": "render",
                                 "error": "bad_generation: 必须是整数"}, status_code=400)
        if not isinstance(session_id, str):
            return JSONResponse({"error": "bad_session_id"}, status_code=400)
        if not q.accepts(session_id, generation, body.get("voice")):
            q.rejected_unknown += 1
            return JSONResponse({"error": "unknown_or_closed_render_session"}, status_code=410)
        pv = body.get("plan_version")
        if pv not in (PLAN_VERSION, SP.PLAN_VERSION_V3, SP.PLAN_VERSION_V4, SP.PLAN_VERSION_V5,
                      SP.PLAN_VERSION_V6):
            return JSONResponse({"status": "failed", "stage": "render",
                                 "error": "plan_version_mismatch: 需要 %d/%d/%d/%d/%d，收到 %r"
                                          % (PLAN_VERSION, SP.PLAN_VERSION_V3,
                                             SP.PLAN_VERSION_V4, SP.PLAN_VERSION_V5,
                                             SP.PLAN_VERSION_V6, pv)},
                                status_code=400)
        voice = str(body.get("voice") or "").strip().lower()
        if ships and voice not in ships:
            return JSONResponse({"status": "failed", "stage": "render",
                                 "error": "unknown_voice: %r（本机合法音色：%s）"
                                          % (voice, ",".join(sorted(ships)))}, status_code=400)
        for i, u in enumerate(units):
            bad = _check_unit(u, i, pv)
            if bad:
                return JSONResponse({"status": "failed", "stage": "render", "error": bad},
                                    status_code=400)
        fmt = body.get("format") or "wav"
        if not isinstance(fmt, str):
            return JSONResponse({"error": "bad_format"}, status_code=400)
        fmt = fmt.strip().lower()
        if fmt not in ("wav", "mp3", "ogg", "opus", "flac"):
            return JSONResponse({"status": "failed", "stage": "render",
                                 "error": "unsupported_format: %r" % fmt}, status_code=400)
        for u in units:
            u["voice"] = voice
        return RenderResponse(q.events(session_id, generation, units, fmt),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def v2_render_cancel(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse({"ok": False, "error": "bad_request: %s" % str(e)[:120]}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        session_id, generation = body.get("session_id"), body.get("generation")
        if not isinstance(session_id, str) or generation is None:
            return JSONResponse({"ok": False, "error": "missing session_id/generation"}, status_code=400)
        if not isinstance(generation, int) or isinstance(generation, bool):
            return JSONResponse({"ok": False, "error": "bad_generation: 必须是整数"}, status_code=400)
        return JSONResponse({"ok": True, "cancelled": q.cancel(session_id, generation),
                             "queued": q.queued, "running": q.running is not None})

    async def v2_render_close(request: Request):
        """回合正常结束（方案 §3.3 的 session.finished）：世代退役，之后同世代不再收新段。

        与 `cancel` 的区别是语义：close 表示"这一回合我要的活儿已经发完了"，不丢结果、不发取消信号；
        协调器每回合收尾都要调它，否则 `_done_units` 会一直挂着这一代的账本。
        """
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse({"ok": False, "error": "bad_request: %s" % str(e)[:120]}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        session_id, generation = body.get("session_id"), body.get("generation")
        if not isinstance(session_id, str) or not isinstance(generation, int) or isinstance(generation, bool):
            return JSONResponse({"ok": False, "error": "missing/bad session_id or generation"},
                                status_code=400)
        changed = q.close(session_id, generation)
        return JSONResponse({"ok": True, "closed": changed, "session_id": session_id,
                             "generation": generation, "queued": q.queued})

    async def v2_render_stats():
        q.sweep()
        return {"ok": True, "queued": q.queued, "running": q.running is not None, "stats": q.stats,
                "max_queued": q.max_queued, **q.resources()}

    _mount(app, "/v2/render", v2_render, ["POST"])
    _mount(app, "/v2/render/open", v2_render_open, ["POST"])
    _mount(app, "/v2/render/cancel", v2_render_cancel, ["POST"])
    _mount(app, "/v2/render/close", v2_render_close, ["POST"])
    _mount(app, "/v2/render/stats", v2_render_stats, ["GET"])
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application) as state:
            try:
                yield state
            finally:
                await q.aclose()

    app.router.lifespan_context = lifespan
    return app
