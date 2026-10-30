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
