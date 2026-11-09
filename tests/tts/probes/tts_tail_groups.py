# -*- coding: utf-8 -*-
"""B: 句尾强弱分组统计（任务 46cbd1a6）。

把 `logs/tts-recent/*.wav` 按 `logs/tts_bridge.log` 里对应的日文句尾标点分组，量：
  * 最后一个有声样点到它之前 300 ms / 100 ms 的 RMS（dB，相对整个文件的峰值）
  * 文件总长、尾部静音长度
读只，不碰服务；结果写 tests/output/tts-shell-trunc-20261109/tail-groups.json。
"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
import re
import wave
from collections import defaultdict

import numpy as np

THRESH = 10 ** (-50 / 20)          # -50 dBFS：文件都归一化过，绝对门限够用


def read_wav(p):
    with wave.open(str(p), "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1)
    return x, sr


def dB(v):
    return round(float(20 * np.log10(max(v, 1e-9))), 1)


def measure(x, sr):
    peak = float(np.max(np.abs(x))) or 1e-9
    voiced = np.flatnonzero(np.abs(x) > THRESH)
    if not len(voiced):
        return None
    end = int(voiced[-1])
    def rms(ms):
        a = max(0, end - int(sr * ms / 1000))
        seg = x[a:end + 1]
        return float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) if len(seg) else 0.0
    return dict(seconds=round(len(x) / sr, 3), voiced_end=round(end / sr, 3),
                tail_silence=round((len(x) - end) / sr, 3), peak=round(peak, 4),
                last300_dB=dB(rms(300) / peak), last100_dB=dB(rms(100) / peak),
                last30_dB=dB(rms(30) / peak), end_dB=dB(abs(float(x[end])) / peak))


def load_texts(path):
    """unit ('sess:gen:idx') -> last ja text seen in the bridge log."""
    rx = re.compile(r"\[translate-text\] unit=(\S+) .*?ja=(.*)$")
    texts = {}
    for line in pathlib.Path(path).read_text("utf8", errors="replace").splitlines():
        m = rx.search(line)
        if m:
            texts[m.group(1)] = m.group(2).strip()
    return texts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recent", default="logs/tts-recent")
    ap.add_argument("--bridge", default="logs/tts_bridge.log")
    ap.add_argument("--out", default="tests/output/tts-shell-trunc-20261109/tail-groups.json")
    args = ap.parse_args()
    texts = load_texts(args.bridge)
    rows = []
    for f in sorted(glob.glob(str(pathlib.Path(args.recent) / "*.wav"))):
        p = pathlib.Path(f)
        parts = p.name[:-4].split("_")
        if len(parts) != 5 or not all(q.isdigit() for q in parts[2:]):
            continue
        sess, gen, idx, sub = parts[0], parts[2], parts[3], parts[4]
        key = f"{sess}:{gen}:{idx}"
        text = texts.get(key)
        x, sr = read_wav(p)
        mm = measure(x, sr)
        if not mm:
            continue
        rows.append(dict(file=p.name, unit=f"{sess}:{gen}:{idx}:{sub}", text=text,
                         ending=(text[-1] if text else None), **mm))
    groups = defaultdict(list)
    for r in rows:
        groups[r["ending"] or "?"].append(r)
    summary = []
    for ending, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        summary.append(dict(
            ending=ending, count=len(items),
            last300_median=float(np.median([i["last300_dB"] for i in items])),
            last100_median=float(np.median([i["last100_dB"] for i in items])),
            last30_median=float(np.median([i["last30_dB"] for i in items])),
            end_median=float(np.median([i["end_dB"] for i in items])),
            tail_silence_median=float(np.median([i["tail_silence"] for i in items])),
            seconds_median=float(np.median([i["seconds"] for i in items]))))
    data = dict(threshold_dBFS=-50, files=len(rows), groups=summary, rows=rows)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    print("| 句尾 | 个数 | 末300ms 中位 dB | 末100ms 中位 dB | 末30ms 中位 dB | 尾静音中位 s |")
    print("|---|---|---|---|---|---|")
    for g in summary:
        print("| %s | %d | %.1f | %.1f | %.1f | %.3f |" % (
            g["ending"], g["count"], g["last300_median"], g["last100_median"],
            g["last30_median"], g["tail_silence_median"]))
    print()
    print("| file | ending | last300 | last100 | end | tail_silence |")
    for r in sorted(rows, key=lambda r: r["last100_dB"]):
        print("| %s | %s | %.1f | %.1f | %.1f | %.3f |" % (
            r["file"], r["ending"], r["last300_dB"], r["last100_dB"], r["end_dB"], r["tail_silence"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
