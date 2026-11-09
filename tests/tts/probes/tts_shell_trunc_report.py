# -*- coding: utf-8 -*-
"""Collapse the task-14d05275 artifacts into one per-sentence table.

Reads only ``tests/output/tts-shell-trunc-20261109/`` (shell-renderer.json, shell-graph.wav,
loopback.wav, 9885 ws wavs) and prints/writes the comparison:
  9885 wav seconds | shell decoded seconds | scheduled interval | loopback audible end | gap to next start
"""
from __future__ import annotations

import argparse
import json
import pathlib
import wave

import numpy as np


def read_wav(path: pathlib.Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1)
    return x, sr


def _active(x: np.ndarray, sr: int, rel_floor: float = 0.01) -> np.ndarray:
    """Indices of 20 ms blocks still above (peak RMS * rel_floor)."""
    if not len(x):
        return np.array([], dtype=int)
    win = max(1, int(sr * 0.02))
    rms = np.sqrt(np.convolve(x.astype(np.float64) ** 2, np.ones(win) / win, "same"))
    if not np.max(rms):
        return np.array([], dtype=int)
    return np.flatnonzero(rms > np.max(rms) * rel_floor)


def audible_end(x: np.ndarray, sr: int, rel_floor: float = 0.01) -> float:
    if not len(x) or not np.max(np.abs(x)):
        return 0.0
    active = _active(x, sr, rel_floor)
    return float(active[-1] / sr) if len(active) else 0.0


def audible_start(x: np.ndarray, sr: int, rel_floor: float = 0.01) -> float:
    active = _active(x, sr, rel_floor)
    return float(active[0] / sr) if len(active) else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="tests/output/tts-shell-trunc-20261109")
    args = ap.parse_args()
    d = pathlib.Path(args.dir).resolve()
    data = json.loads((d / "shell-renderer.json").read_text("utf8"))
    graph, sr = read_wav(d / "shell-graph.wav")
    loop, lsr = read_wav(d / "loopback.wav")
    spans = data["spans"]
    nodes = data["nodes"]
    # loopback t = ctx t + offset; calibrate on the first audible block of both
    # first audible sample inside the first span (20 ms RMS window, 1% of that span's peak)
    head = graph[int(nodes[0]["when"] * sr):int((nodes[0]["when"] + nodes[0]["duration"]) * sr)]
    first_ctx = nodes[0]["when"] + audible_start(head, sr)
    # first activity in loopback (threshold on rms windows)
    win = int(lsr * 0.02)
    rms = np.sqrt(np.convolve(loop.astype(np.float64) ** 2, np.ones(win) / win, "same"))
    idx = np.flatnonzero(rms > max(np.max(rms) * 0.01, 1e-4))
    loop_off = (idx[0] / lsr) - first_ctx if len(idx) else 0.0

    rows = []
    for i, (span, node) in enumerate(zip(spans, nodes)):
        start, dur = node["when"], node["duration"]
        end = start + dur
        seg = graph[int(start * sr):int(end * sr)]
        g_end = start + audible_end(seg, sr)
        a = max(0, int((start + loop_off) * lsr))
        b = min(len(loop), int((end + loop_off) * lsr))
        lseg = loop[a:b]
        l_end = (start + audible_end(lseg, lsr)) if len(lseg) else None
        nxt = nodes[i + 1]["when"] if i + 1 < len(nodes) else None
        rows.append(dict(
            sentence=i + 1, unit_id=span["unitId"],
            wav_9885_seconds=round(span["decodeBytes"] and ((span["decodeBytes"] - 44) / 2 / 48000), 3),
            shell_decoded_seconds=round(span["bufferDuration"], 3),
            shell_bytes=span["decodeBytes"], shell_decoded_ok=span["decodeOk"],
            scheduled_start=round(start, 3), scheduled_end=round(end, 3),
            graph_audible_end=round(g_end, 3), graph_tail_silence=round(end - g_end, 3),
            loopback_audible_end=round(l_end, 3) if l_end else None,
            loopback_tail_silence=round(end - l_end, 3) if l_end else None,
            next_start=round(nxt, 3) if nxt is not None else None,
            gap_to_next_start=round(nxt - end, 3) if nxt is not None else None,
            node_ended_ctx=round(node["ctxEndedAt"], 3) if node["ctxEndedAt"] is not None else None,
        ))
    out = dict(loopback_offset_s=round(loop_off, 3), nodes=len(nodes), spans=len(spans),
               stop_calls=data["stops"], stats=data["stats"], errors=data["errors"], rows=rows)
    (d / "per-sentence.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf8")
    print("| # | 9885 wav | shell 解码 | 播放区间 | 图内有声结尾 | loopback 有声结尾 | 下一句起点间隔 |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %d | %s s | %s s | %s→%s | %s (余 %s) | %s (余 %s) | %s s |" % (
            r["sentence"], r["wav_9885_seconds"], r["shell_decoded_seconds"], r["scheduled_start"],
            r["scheduled_end"], r["graph_audible_end"], r["graph_tail_silence"],
            r["loopback_audible_end"], r["loopback_tail_silence"],
            r["gap_to_next_start"] if r["gap_to_next_start"] is not None else "-"))
    print(json.dumps({k: out[k] for k in ("loopback_offset_s", "stop_calls", "stats", "errors")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
