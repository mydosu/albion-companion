# -*- coding: utf-8 -*-
"""单元音频的气口整形（纯函数，不联网、不读时钟）。

背景（2026-10-24 摸底）：后端给的每个单元自带随机长度的首静音（0.07–0.85s）和尾静音（0.3–1.1s），
壳只在段间固定加 0.10s，于是可听间隔在 0.5–2.3s 之间忽长忽短，节奏显得乱。这里把首静音定长成
`LEAD_S`、把尾静音**定长**成由单元末尾标点决定的值（可用环境变量调），壳那 0.10s 照旧叠在后面。

只处理 16-bit PCM WAV；别的格式/解析失败一律原样返回（宁可不整形，也不能弄坏音频）。

2026-10-26 句尾截断修复：裁剪「哪里算说完」原来用 -40dB 判据，句尾清化音（です/ます 的 す）落在
-45～-60dB，会被当成静音掐掉（生产实测 6/32 句裁掉 27–59ms 的可听尾音），且裁点硬拼数字零没有淡出
（最大 -46dB 的台阶）。现在尾判据放到峰值 -55dB / 绝对 -65dBFS（把整段自然衰减留住），
裁点前 30ms 做线性淡出；首判据与各种气口长度都不变。

2026-10-30 截在响处：上游（Irodori）有时自己就把话停在响处（宣传片录制实测：结束前 2ms 还有
-25～-28dBFS）。旧实现在这种时候**故意不淡出**，直接把还在响的样本接上数字零，听感是最后一个字
被掐掉。现在改做 `LOUD_FADE_S`（12ms）短线性淡出：短到不会把响亮的句尾压成渐弱收尾，又足以
消掉接零的台阶。安静收尾时的 30ms 淡出不变。

2026-11-08 尾部取证：`tail_profile()` 只量、不改音频（最后一个有声帧的位置 + 之后的静音长度），
生产在整形前后各记一份，用来分清「模型少念了收尾」和「链路上被掐」。
"""
import io
import os
import wave

import numpy as np

LEAD_S = float(os.environ.get("ALBION_GAP_LEAD", "0.06"))
# 末尾标点 → 尾静音秒数。0 以下的值表示该类不整形。
TAIL_S = {
    "sentence": float(os.environ.get("ALBION_GAP_SENTENCE", "0.7")),    # 。！？!?
    "comma": float(os.environ.get("ALBION_GAP_COMMA", "0.3")),          # ，、；：
    "ellipsis": float(os.environ.get("ALBION_GAP_ELLIPSIS", "0.8")),    # …… …
    "cut": float(os.environ.get("ALBION_GAP_CUT", "0.15")),             # 力断/空闲冲刷（句子没说完）
    # 亲密句（桥 `/v2/prepare` 判了码本锚、子段带 `intimate`）：不看末尾标点，句尾一律短气口。
    # 2026-11-09 样张实测：句尾 0.7/0.8s + 壳 0.10s ⇒ 可听间隔 0.9–1.05s，喘息间接不上。
    # 只有桥明确判过亲密的子段走这一类；别的句子一个字节都不变（`tail_kind` 不会返回它）。
    "intimate": float(os.environ.get("ALBION_GAP_INTIMATE", "0.3")),
}
ENABLED = os.environ.get("ALBION_GAP_SHAPE", "1").lower() not in ("0", "false", "no", "off")

_CLOSERS = "」』）)】]\"'”’》〉 \t\r\n"
_SENTENCE = "。！？!?．."
_COMMA = "，、；：,;:"
VOICED_DB = 40.0          # 有声判据：比全段峰值低不到 40dB
FLOOR_DB = -55.0          # 且高于 -55dBFS（编码器底噪不算有声）
TAIL_KEEP_S = 0.03        # 有声结束后保留的自然衰减
# 句尾的真正「可听结束」比 -40dB 低得多：日语句尾的清化音（です/ます 的 す、つ、く、し）和气声收尾
# 通常落在峰值下 45–60dB。2026-10-26 生产 :9889 实测 32 句：按 VOICED_DB 定尾时，裁点前 30ms 的包络
# 还有 -40～-57dB（还在出声中就切），6 句因此丢掉 5–29ms 高于 -60dBFS 的尾音，而且裁点硬拼数字零。
# 所以尾静音用**更低的**判据找，把整段自然衰减留住；起始判据（VOICED_DB/FLOOR_DB）保持不变。
TAIL_DB = 55.0            # 尾判据：比全段峰值低不到 55dB
TAIL_FLOOR_DB = -65.0     # 且高于 -65dBFS（低于此是编码器/水印底噪，留住只是噪声）
FADE_S = 0.03             # 安静收尾时裁点前的淡出：硬拼零会有台阶（实测裁点前 30ms 还有 -40dB）
FADE_GATE_DB = 30.0       # 裁点前 30ms 已低于峰值 30dB ⇒ 安静收尾，用 FADE_S 淡出
LOUD_FADE_S = 0.012       # 还在响处被截时改用的短淡出：只消接零台阶，不把响亮的尾巴压成渐弱


def tail_kind(text: str, reason: str = "") -> str:
    """单元末尾属于哪一类气口。标点优先；没有标点时按切分原因（软切/力断/空闲 = 句子没说完）。"""
    t = (text or "").rstrip(_CLOSERS) if text else ""
    if t.endswith("…") or t.endswith("..."):
        return "ellipsis"
    if t and t[-1] in _SENTENCE:
        return "sentence"
    if t and t[-1] in _COMMA:
        return "comma"
    if reason in ("force", "idle"):
        return "cut"
    return "sentence"       # finish/hard 且无标点：一整段的收尾，按句末处理


def shape_kind(text: str, reason: str = "", intimate: bool = False, last: bool = True) -> str:
    """一个子段该用哪一类气口（2026-11-09）。

    * `intimate`（桥判了亲密码本锚的子段）⇒ 一律 `intimate`（默认 0.3s），不看末尾标点：
      亲密戏里「……」结尾的句子占多数，按 ellipsis 给 0.8s 会让喘息之间空太久；
    * 非末子段（同一单元被桥再切开的前几片）⇒ `sentence`：那是句子收尾，不是单元收尾；
    * 末子段 ⇒ 按单元末尾标点/切分原因（与加这条之前逐字相同）。
    """
    if intimate:
        return "intimate"
    if not last:
        return "sentence"
    return tail_kind(text, reason)


def _fade_out(body, peak, sr):
    """把 `body` 末尾线性降到 0（原地改）：安静收尾 30ms，上游截在响处 12ms。

    裁点前 30ms 仍高于「峰值 -FADE_GATE_DB」= 上游把话截在了响处。旧实现此时直接 return，
    还在响的样本硬接数字零，听起来就是最后一个字被掐掉（2026-10-30 录制：结束前 2ms 还有
    -25～-28dBFS，下一刻就是 0）。现在改做 LOUD_FADE_S 短淡出：够短，不至于替上游「压尾」成
    渐弱收尾；又够长，接零时没有台阶。安静时的 30ms 淡出不变（那一段本来就是自然衰减）。
    """
    if peak <= 0:
        return
    n = min(int(FADE_S * sr), len(body))
    seg = body[-n:] if n > 0 else body[:0]
    if n > 0 and float(np.abs(seg).max()) / 32768.0 > peak * 10 ** (-FADE_GATE_DB / 20.0):
        n = min(int(LOUD_FADE_S * sr), len(body))      # 截在响处：短淡出，只消接零的台阶
        seg = body[-n:] if n > 0 else body[:0]
    if n <= 0:
        return
    ramp = np.linspace(1.0, 0.0, n, dtype="float32")[:, None]
    body[-n:] = np.rint(seg.astype("float32") * ramp).astype("<i2")


POINT_FRAME_S = 0.01      # 包络探针的帧长，与 shape() 的 10ms 窗同粒度
POINT_FAILED = -1.0       # 量不出来就记 -1：日志字段绝不能连累出声


def tail_profile(wav: bytes):
    """(最后一个有声帧离开头的秒数, 之后的静音秒数)；算不出来一律 (-1, -1)。

    2026-11-08：排查「句尾尾音被截断」时发现，只看末段电平（`tail=`）和 `cps` 分不清
    「模型少念了收尾」和「链路上被掐」——两种情况的 wav 末尾都是静音。这里记下「话说到第
    几秒 / 后面还剩多少静音」，整形前后各记一份（整形后那行字段名带 `_shaped`），下次一读
    日志就能判层。判据与 shape() 的有声判据同源（帧 RMS 低于全段峰值 `VOICED_DB` 或低于
    `FLOOR_DB`dBFS 算没声）；只是这里用帧 **RMS** 而不是窗内峰值——峰值会被单个咔哒声骗过去。
    """
    try:
        if not wav or wav[:4] != b"RIFF":
            return POINT_FAILED, POINT_FAILED
        with wave.open(io.BytesIO(wav), "rb") as w:
            params = w.getparams()
            raw = w.readframes(w.getnframes())
        if params.sampwidth != 2 or not raw:
            return POINT_FAILED, POINT_FAILED
        ch, sr = params.nchannels, params.framerate
        if sr <= 0:
            return POINT_FAILED, POINT_FAILED
        pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype("float32") / 32768.0
        mono = pcm.mean(axis=1) if ch > 1 else pcm[:, 0]
        peak = float(np.abs(mono).max())
        if peak <= 0:
            return POINT_FAILED, POINT_FAILED
        win = max(1, int(POINT_FRAME_S * sr))
        frames = len(mono) // win
        if frames <= 0:
            return POINT_FAILED, POINT_FAILED
        rms = np.sqrt((mono[:frames * win].reshape(frames, win) ** 2).mean(axis=1))
        thr = max(peak * 10 ** (-VOICED_DB / 20.0), 10 ** (FLOOR_DB / 20.0))
        loud = np.flatnonzero(rms > thr)
        if not len(loud):
            return POINT_FAILED, POINT_FAILED
        voiced_end = (int(loud[-1]) + 1) * win / float(sr)
        return voiced_end, max(0.0, len(mono) / float(sr) - voiced_end)
    except Exception:      # noqa: BLE001 - 量不出来只记 -1
        return POINT_FAILED, POINT_FAILED


def shape(wav: bytes, kind: str) -> bytes:
    """首静音定长为 LEAD_S，尾静音定长为 TAIL_S[kind]。失败/不适用原样返回。"""
    tail_s = TAIL_S.get(kind, 0.0)
    if not ENABLED or tail_s <= 0 or wav[:4] != b"RIFF":
        return wav
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            params = w.getparams()
            raw = w.readframes(w.getnframes())
        if params.sampwidth != 2 or not raw:
            return wav
        ch, sr = params.nchannels, params.framerate
        pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)
        mono = np.abs(pcm.astype("float32")).max(axis=1) / 32768.0
        peak = float(mono.max())
        if peak <= 0:
            return wav
        thr = max(peak * 10 ** (-VOICED_DB / 20.0), 10 ** (FLOOR_DB / 20.0))
        # 10ms 窗内最大值过阈值才算有声：单个孤立样本（点击声）不当作有声起点
        win = max(1, int(0.01 * sr))
        frames = len(mono) // win
        if frames == 0:
            return wav
        loud = mono[:frames * win].reshape(frames, win).max(axis=1) > thr
        idx = np.flatnonzero(loud)
        if len(idx) == 0:
            return wav
        onset = int(idx[0]) * win
        lead = int(LEAD_S * sr)
        # 尾判定用**更低**的判据：句尾清化音/气声衰减比 -40dB 低得多，按 VOICED_DB 判会把它掐掉
        tail_thr = max(peak * 10 ** (-TAIL_DB / 20.0), 10 ** (TAIL_FLOOR_DB / 20.0))
        tail_loud = mono[:frames * win].reshape(frames, win).max(axis=1) > tail_thr
        tail_idx = np.flatnonzero(tail_loud)
        last_voiced = int(tail_idx[-1]) if len(tail_idx) else int(idx[-1])
        end = min(len(pcm), (last_voiced + 1) * win + int(TAIL_KEEP_S * sr))
        # 首静音定长：多了裁掉，不够（有声从第 0 个样本就起，句首会显得被咬掉）补齐
        head = np.zeros((max(0, lead - onset), ch), dtype="<i2")
        body = np.concatenate([head, pcm[max(0, onset - lead):end]])
        tail = np.zeros((int(tail_s * sr), ch), dtype="<i2")
        _fade_out(body, peak, sr)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(np.concatenate([body, tail]).tobytes())
        return out.getvalue()
    except Exception:       # noqa: BLE001 - 整形失败不许影响出声
        return wav
