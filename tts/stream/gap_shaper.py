# -*- coding: utf-8 -*-
"""单元音频的气口整形（纯函数，不联网、不读时钟）。

背景（2026-10-24 摸底）：后端给的每个单元自带随机长度的首静音（0.07–0.85s）和尾静音（0.3–1.1s），
壳只在段间固定加 0.10s，于是可听间隔在 0.5–2.3s 之间忽长忽短，节奏显得乱。这里把首静音定长成
`LEAD_S`、把尾静音**定长**成由单元末尾标点决定的值（可用环境变量调），壳那 0.10s 照旧叠在后面。

只处理 16-bit PCM WAV；别的格式/解析失败一律原样返回（宁可不整形，也不能弄坏音频）。
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
        end = min(len(pcm), (int(idx[-1]) + 1) * win + int(TAIL_KEEP_S * sr))
        # 首静音定长：多了裁掉，不够（有声从第 0 个样本就起，句首会显得被咬掉）补齐
        head = np.zeros((max(0, lead - onset), ch), dtype="<i2")
        body = np.concatenate([head, pcm[max(0, onset - lead):end]])
        tail = np.zeros((int(tail_s * sr), ch), dtype="<i2")
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(np.concatenate([body, tail]).tobytes())
        return out.getvalue()
    except Exception:       # noqa: BLE001 - 整形失败不许影响出声
        return wav
