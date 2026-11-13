"""壳输出对照分析（v2）：用 10ms RMS 包络做全局+逐段对齐，避开极性与电平差。

对齐口径：先按包络全局对齐，再在每段期望位置 ±0.5s 内做局部细化。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

SR = 48000
GAP = 0.10
FR = int(SR * 0.01)  # 10ms
WAVS = [
    r"D:\AI\Wife\logs\tts-recent\sa9483fc3291f_sa9483fc3291f_1_0_0.wav",
    r"D:\AI\Wife\logs\tts-recent\sa9483fc3291f_sa9483fc3291f_1_1_0.wav",
    r"D:\AI\Wife\logs\tts-recent\sa9483fc3291f_sa9483fc3291f_1_2_1.wav",
    r"D:\AI\Wife\logs\tts-recent\s30ce7859f920_s30ce7859f920_1_2_1.wav",
]
OUT = Path(sys.argv[1])


def mono(p: str) -> np.ndarray:
    x, _ = sf.read(p, dtype="float32", always_2d=True)
    return x.mean(axis=1)


def env(x: np.ndarray) -> np.ndarray:
    k = len(x) // FR
    return np.sqrt(np.mean(x[: k * FR].reshape(k, FR) ** 2, axis=1) + 1e-18)


def db(v: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.maximum(v, 1e-9))


def main() -> None:
    srcs = [mono(p) for p in WAVS]
    starts, t = [], 0.0
    for s in srcs:
        starts.append(t)
        t += len(s) / SR + GAP
    timeline = np.zeros(int(t * SR) + SR, np.float32)
    for s, st in zip(srcs, starts):
        i = int(st * SR)
        timeline[i:i + len(s)] += s
    et = env(timeline)

    report = {"modes": {}}
    for mode in [m for m in ("baseline", "shell", "live", "live2") if (OUT / f"{m}.wav").exists()]:
        rec, sr = sf.read(str(OUT / f"{mode}.wav"), dtype="float32", always_2d=True)
        assert sr == SR
        er = env(rec.mean(axis=1))
        g = signal.correlate(er, et, mode="valid")
        lagf = int(np.argmax(g))
        denom = np.linalg.norm(et) * np.linalg.norm(er[lagf:lagf + len(et)]) or 1e-12
        rep = {"lag_s": round(lagf * 0.01, 3), "env_corr": round(float(g[lagf] / denom), 3), "segments": []}
        for idx, (s, st) in enumerate(zip(srcs, starts)):
            es = env(s)
            center = lagf + int(st / 0.01)
            best, bestoff = -2.0, 0
            for off in range(-50, 51):
                a = center + off
                if a < 0 or a + len(es) > len(er):
                    continue
                w = er[a:a + len(es)]
                d = (np.linalg.norm(es) * np.linalg.norm(w)) or 1e-12
                c = float(np.dot(es, w) / d)
                if c > best:
                    best, bestoff = c, off
            a = center + bestoff
            w = er[a:a + len(es)]
            odb, rdb = db(es), db(w)
            ov = np.where(odb > -60)[0]
            o_end = (ov[-1] + 1) * 0.01 if len(ov) else 0.0
            floor = float(np.percentile(rdb, 5))
            rv = np.where(rdb > max(-60.0, floor + 12))[0]
            r_end = (rv[-1] + 1) * 0.01 if len(rv) else 0.0
            holes, run = [], None
            for i in range(len(odb)):
                if odb[i] > -45 and rdb[i] < odb[i] - 25:
                    run = i if run is None else run
                elif run is not None:
                    holes.append({"at_s": round(run * 0.01, 2), "len_s": round((i - run) * 0.01, 2)})
                    run = None
            if run is not None:
                holes.append({"at_s": round(run * 0.01, 2), "len_s": round((len(odb) - run) * 0.01, 2)})
            mask = odb > -40
            lvl = float(np.median(rdb[mask] - odb[mask])) if mask.any() else None
            rep["segments"].append({
                "wav": Path(WAVS[idx]).name,
                "dur": round(len(s) / SR, 3),
                "seg_corr": round(best, 3),
                "offset_ms": bestoff * 10,
                "orig_voiced_end_s": round(o_end, 2),
                "rec_voiced_end_s": round(r_end, 2),
                "tail_lost_ms": round((o_end - r_end) * 1000),
                "level_diff_db": None if lvl is None else round(lvl, 1),
                "holes": holes,
                "hole_total_s": round(sum(h["len_s"] for h in holes), 2),
            })
        report["modes"][mode] = rep
    (OUT / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for mode, rep in report["modes"].items():
        print(f"== {mode} lag={rep['lag_s']}s env_corr={rep['env_corr']}")
        for s in rep["segments"]:
            print(f"   {s['wav'][-28:]:28s} dur={s['dur']:5.2f} corr={s['seg_corr']:5.2f} off={s['offset_ms']:+4d}ms "
                  f"tail_lost={s['tail_lost_ms']:+5d}ms lvl={s['level_diff_db']:+5.1f}dB holes={len(s['holes'])}/{s['hole_total_s']}s")
            if s["holes"]:
                print("      holes:", s["holes"][:6])


if __name__ == "__main__":
    main()
