# -*- coding: utf-8 -*-
"""句尾音高探针：量一段音频最后 ~0.4s 的 F0 走向（相对整段 F0 中位数），比较半句念成「收尾」还是「延续」。

粗糙的自相关 F0（70–450Hz，帧 40ms/步 10ms，自相关峰 >0.5 才算有声），只适合**同一说话人、同类句子的成组对比**，
不是精确的音高测量；样本少时差异不可靠，请看每组的个数和离散度。

用法::

    python tests/tts/probes/f0_tail_probe.py --group off="tests/output/x/off-*.wav" --group on="tests/output/x/on-*.wav"

输出每个文件一行 + 每组汇总：
  end_vs_body  最后 250ms 的 F0 中位数相对整段中位数，单位半音（负 = 句尾落到比平时低 = 收尾语调）
  slope        最后 400ms 的 F0 线性斜率，单位半音/秒（负 = 下降）
"""
import argparse
import glob
import statistics
import wave

import numpy as np


def load(path):
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768
    return sr, (a.reshape(-1, ch).mean(1) if ch > 1 else a)


def voiced_span(a, sr):
    win = int(0.01 * sr)
    env = np.array([np.sqrt((a[i:i + win] ** 2).mean()) for i in range(0, len(a) - win, win)])
    e = 20 * np.log10(np.maximum(env, 1e-9))
    act = np.where(e > e.max() - 40)[0]
    return act[0] * 0.01, (act[-1] + 1) * 0.01


def f0_track(a, sr, t0, t1):
    out, fl, hop = [], int(0.04 * sr), int(0.01 * sr)
    lo, hi = int(sr / 450), int(sr / 70)
    for s in range(int(t0 * sr), int(t1 * sr) - fl, hop):
        x = a[s:s + fl] - a[s:s + fl].mean()
        if np.sqrt((x ** 2).mean()) < 0.01:
            continue
        ac = np.correlate(x, x, "full")[fl - 1:]
        ac = ac / (ac[0] + 1e-9)
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.5:
            out.append((s / sr, sr / k))
    return out


def analyze(path):
    sr, a = load(path)
    v0, v1 = voiced_span(a, sr)
    body = f0_track(a, sr, v0, v1)
    end = [f for t, f in body if t >= v1 - 0.25]
    last = [(t, f) for t, f in body if t >= v1 - 0.4]
    if len(body) < 10 or len(end) < 3 or len(last) < 5:
        return None
    med = statistics.median(f for _, f in body)
    st = lambda f: 12 * np.log2(f / med)
    ts = np.array([t for t, _ in last]); ss = np.array([st(f) for _, f in last])
    return dict(end_vs_body=round(float(st(statistics.median(end))), 2),
                slope=round(float(np.polyfit(ts, ss, 1)[0]), 2), voiced=round(v1 - v0, 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", action="append", required=True, help="名字=通配符，可重复")
    args = ap.parse_args()
    for spec in args.group:
        name, pattern = spec.split("=", 1)
        rows = []
        for p in sorted(glob.glob(pattern)):
            r = analyze(p)
            print("%-8s %-40s %s" % (name, p.replace("\\", "/")[-40:], r if r else "无足够有声帧"))
            if r:
                rows.append(r)
        if rows:
            for key in ("end_vs_body", "slope"):
                v = [r[key] for r in rows]
                print("== %s %s: n=%d mean=%+.2f median=%+.2f min=%+.2f max=%+.2f" % (
                    name, key, len(v), statistics.mean(v), statistics.median(v), min(v), max(v)))


if __name__ == "__main__":
    main()
