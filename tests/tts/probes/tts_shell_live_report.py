# -*- coding: utf-8 -*-
"""Analyze the passive live capture (task 95be04e0).

For every span recorded in `live/live-events.json`:
  * planned interval (AudioBufferSourceNode.start `when` + buffer.duration) vs real `ended`
  * whether a `stop()`/fade hit it before its scheduled end
  * overlap with any other span (same or different intent / clock instance)
  * alignment of the captured destination WAV against the matching
    `logs/tts-recent/<session>_<session>_<unit>.wav`, i.e. did the shell actually emit the
    full 9885 bytes (tail included)?

    python tests/tts/probes/tts_shell_live_report.py
"""
from __future__ import annotations

import argparse
import glob
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


def audible_end(x: np.ndarray, sr: int, rel: float = 0.01) -> float:
    if not len(x) or not np.max(np.abs(x)):
        return 0.0
    win = max(1, int(sr * 0.02))
    rms = np.sqrt(np.convolve(x.astype(np.float64) ** 2, np.ones(win) / win, "same"))
    idx = np.flatnonzero(rms > np.max(rms) * rel)
    return float(idx[-1] / sr) if len(idx) else 0.0


def recent_for(unit: str, rec_dir: pathlib.Path) -> pathlib.Path | None:
    # unit = "<session>:<gen>:<idx>:<sub>"; files are "<session>_<session>_<gen>_<idx>_<sub>.wav"
    session, gen, idx, sub = unit.split(":")
    hits = sorted(rec_dir.glob(f"{session}_{session}_{gen}_{idx}_{sub}.wav"))
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", default="tests/output/tts-shell-trunc-20261109/live")
    ap.add_argument("--recent", default="logs/tts-recent")
    args = ap.parse_args()
    live = pathlib.Path(args.live).resolve()
    recent = pathlib.Path(args.recent)
    data = json.loads((live / "live-events.json").read_text("utf8"))
    events = data["events"]
    wav_path = live / "live-destination.wav"
    graph, sr = read_wav(wav_path) if wav_path.exists() else (np.zeros(0, np.float32), 48000)
    pcm_start = data["meta"].get("pcm_start_ctx") or (events[0].get("ctx") if events else 0.0)

    starts = {e["id"]: e for e in events if e["k"] == "start"}
    ends = {e["id"]: e for e in events if e["k"] == "ended"}
    stops = [e for e in events if e["k"] == "stop"]
    rows = []
    for sid, st in sorted(starts.items(), key=lambda kv: kv[1]["when"]):
        dur = st.get("dur") or 0.0
        when = st.get("when") or 0.0
        planned_end = when + dur
        end_ev = ends.get(sid)
        ended_ctx = end_ev["ctx"] if end_ev else None
        stop_ev = next((s for s in stops if s["id"] == sid), None)
        early = None
        if ended_ctx is not None:
            early = round(planned_end - ended_ctx, 3)
        overlaps = []
        for oid, other in starts.items():
            if oid == sid:
                continue
            o_end = other["when"] + (other.get("dur") or 0)
            lo, hi = max(when, other["when"]), min(planned_end, o_end)
            if hi > lo:
                overlaps.append(dict(unit=other.get("unit"), intent=other.get("intent"),
                                     seconds=round(hi - lo, 3)))
        seg = graph[int((when - pcm_start) * sr):int((planned_end - pcm_start) * sr)] if len(graph) else np.zeros(0)
        f = recent_for(st["unit"], recent) if st.get("unit") else None
        cmp = {}
        if f is not None and len(seg):
            src, ssr = read_wav(f)
            if ssr == sr:
                # FFT cross-correlation: where inside the captured span does the file start?
                n = len(seg) + len(src)
                size = 1 << (n - 1).bit_length()
                cc = np.fft.irfft(np.fft.rfft(seg, size) * np.conj(np.fft.rfft(src, size)), size)[:len(seg)]
                best_lag = int(np.argmax(cc))
                norm = np.sqrt(np.sum(seg ** 2) * np.sum(src ** 2)) + 1e-12
                aligned = seg[best_lag:best_lag + len(src)]
                m = min(len(aligned), len(src))
                cmp = dict(recent_wav=f.name, recent_seconds=round(len(src) / ssr, 3),
                           align_offset_ms=round(best_lag / sr * 1000, 1),
                           align_corr=round(float(cc[best_lag] / norm), 4),
                           compared_seconds=round(m / sr, 3),
                           max_abs_diff=round(float(np.max(np.abs(aligned[:m] - src[:m]))), 5) if m else None,
                           rms_live=round(float(np.sqrt(np.mean(seg ** 2))), 5),
                           rms_recent=round(float(np.sqrt(np.mean(src ** 2))), 5),
                           live_audible_end=round(audible_end(seg, sr), 3),
                           recent_audible_end=round(audible_end(src, ssr), 3),
                           live_tail_silence=round(len(seg) / sr - audible_end(seg, sr), 3),
                           recent_tail_silence=round(len(src) / ssr - audible_end(src, ssr), 3))
        rows.append(dict(
            unit=st.get("unit"), intent=st.get("intent"),
            decoded_seconds=round(dur, 3), planned_start=round(when, 3), planned_end=round(planned_end, 3),
            ended_ctx=round(ended_ctx, 3) if ended_ctx is not None else None,
            ended_after_start=round(ended_ctx - when, 3) if ended_ctx is not None else None,
            cut_early_seconds=early, stop_event=stop_ev, overlaps=overlaps, audio=cmp))
    out = dict(meta=data["meta"], pcm_start_ctx=pcm_start, capture_seconds=round(len(graph) / sr, 2),
               stop_events=len(stops), fade_events=[e for e in events if e["k"] in ("ramp", "setv", "cancelv")],
               suspend_events=[e for e in events if e["k"] in ("suspend", "close")], rows=rows)
    (live / "live-analysis.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf8")

    print("| unit | 解码秒 | 计划 start→end | 实际 ended | 提前 stop | 与其它段重叠 | 图内/文件 有声尾静音 | max diff |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        a = r["audio"]
        ov = ", ".join(f"{o['unit']}+{o['seconds']}s" for o in r["overlaps"]) or "无"
        print("| %s | %.3f | %.3f→%.3f | %s | %s | %s | %s / %s | %s |" % (
            r["unit"], r["decoded_seconds"], r["planned_start"], r["planned_end"],
            r["ended_ctx"], ("是 -%.3fs" % r["cut_early_seconds"]) if (r["cut_early_seconds"] or 0) > 0.02 else "否",
            ov, a.get("live_tail_silence"), a.get("recent_tail_silence"), a.get("max_abs_diff")))
    print(json.dumps(dict(stops=out["stop_events"], fades=len(out["fade_events"]),
                          suspends=len(out["suspend_events"]), capture_s=out["capture_seconds"],
                          turns={k: v for k, v in data["meta"]["turns"].items()}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
