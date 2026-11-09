# -*- coding: utf-8 -*-
"""预测时长预算对照（2026-11-08）：同一批真实 ja 句子，量「尾巴还剩多少」与总时长。

背景：11-08 的 40 句对照实验证明——预算就是语速旋钮，模型会把预测时长吃掉约 95~99%；40 句里
3 句尾部静音只剩 0.14/0.19/0.23s（句尾被挤短），而 chars/5.0 的字数下限只真正生效 1/40。据此
`irodori_tts/inference_runtime.py` 给预测时长加了固定余量（`TTS_PRED_MARGIN_S`，默认 0.6s）。

这个探针**不改服务配置**，只量当前部署的行为，用来做「部署前 vs 部署后」对照：
  * 加余量前：`TTS_PRED_MARGIN_S=0` 重启 9889 → 跑一次 → `--tag margin0`
  * 加余量后：默认 0.6（或指定值）重启 9889 → 再跑一次 → `--tag margin06 --baseline <上一次的 json>`
两次用**同一批句子、同一个 seed 公式**，所以差异来自预算而不是采样。

用法::

    python tests/tts/probes/tts_budget_calib.py --out tests/output/tts-budget-20261108 --tag margin06 \
        [--count 40] [--margin 0.6] [--baseline tests/output/.../margin0.json] [--port 9889]

只读：只调 /v2/render（自开自关的临时会话），不碰壳、不碰翻译。会花一点 GPU 时间（每句一次渲染）。
`--margin` 只是**声明这次服务器该跑的值**：脚本会从 `logs/irodori.log` 里最近的 final 行读回
`margin=`，对不上就警告（读不到只提示，不失败）。
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import sys

import numpy as np
import requests
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))
from render_queue import _min_seconds_for, _spoken_chars  # noqa: E402

CAPTION = "少し落ち着いた声で、淡々と、話す。"
TIGHT_S = 0.30


def log_sentences(path, count):
    """从 tts_bridge.log 里取真实 ja（按朗读字数排序后均匀取样，长短散开）。"""
    seen, pool = set(), []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "[translate-text]" not in line or "ja=" not in line:
                continue
            ja = line.split("ja=", 1)[1].strip()
            if _spoken_chars(ja) < 6 or ja in seen:
                continue
            seen.add(ja)
            pool.append(ja)
    pool.sort(key=_spoken_chars)
    step = max(1, len(pool) // max(1, count))
    return pool[::step][:count]


def measure(path):
    d, sr = sf.read(path, dtype="float32")
    if d.ndim > 1:
        d = d.mean(axis=1)
    win = max(1, int(0.01 * sr))
    n = len(d) // win
    rms = np.sqrt((d[:n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(np.abs(d).max()) or 1e-9
    thr = max(peak * 10 ** (-40.0 / 20.0), 10 ** (-55.0 / 20.0))
    loud = np.flatnonzero(rms > thr)
    voiced = (int(loud[-1]) + 1) * win / sr if len(loud) else 0.0
    secs = len(d) / sr
    return secs, voiced, max(0.0, secs - voiced)


def seed_for(text, k=None):
    """固定的种子派生：同一个 (文本, k) 永远同一颗种子 ⇒ 跨次对照可复现。

    显式带 seed 还会让队列**跳过 tail_tight 重渲**（那是生产兜底），测量时必须这样：
    换 seed 的动作要由探针自己做，不能让服务在背后偷偷换掉。
    """
    h = hashlib.sha256(("%s|%s" % (text, k)).encode()).hexdigest()[:8]
    return int(h, 16) % (2 ** 31 - 1)


def render_units(units, outdir, port, voice, batch=20):
    """逐批提交（一批 20 段，别把 200 段塞进一个 SSE），返回 {unit_id: wav 路径}。"""
    url = "http://127.0.0.1:%d" % port
    got = {}
    for i in range(0, len(units), batch):
        chunk = units[i:i + batch]
        h = requests.post(url + "/v2/render/open", json={"voice": voice}, timeout=30).json()
        body = {"session_id": h["session_id"], "generation": h["generation"], "plan_version": 4,
                "voice": voice, "format": "wav", "units": chunk}
        r = requests.post(url + "/v2/render", json=body, stream=True, timeout=900)
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data:") and "audio_base64" in line:
                p = json.loads(line[5:])
                pth = os.path.join(outdir, "%s.wav" % p.get("unit_id", "u").replace(":", "_"))
                with open(pth, "wb") as f:
                    f.write(base64.b64decode(p.get("audio_base64") or ""))
                got[p.get("unit_id")] = pth
        requests.post(url + "/v2/render/close",
                      json={"session_id": h["session_id"], "generation": h["generation"]},
                      timeout=30)
        print("  批 %d-%d 完成" % (i, i + len(chunk) - 1), flush=True)
    return got


def calib_unit(uid, text, seed):
    return {"unit_id": uid, "sub_index": 0, "text": text, "caption": CAPTION, "cfg": 3.0,
            "family": "neutral", "scene_id": 2, "codebook_version": 2, "caption_mode": "free",
            "seed": seed}


def render(texts, outdir, port, voice):
    units = [calib_unit("calib:%d" % i, t, seed_for(t)) for i, t in enumerate(texts)]
    return render_units(units, outdir, port, voice)


def server_margin(logfile):
    """从最近的 final 行里读回服务器实际用的余量（新代码才会打 margin=）。"""
    try:
        tail = pathlib.Path(logfile).read_text("utf8", errors="replace").splitlines()[-400:]
    except OSError:
        return None
    for line in reversed(tail):
        m = re.search(r"unit=calib:.*margin=([\d.]+)s", line)
        if m:
            return float(m.group(1))
    return None


def run_seeds(texts, args, outdir):
    """同一句 × N 个 seed（同预算），看紧尾巴是「句子属性」还是「采样属性」。

    再对**每一次紧的**用一颗新 seed 重渲一次（也是同预算），量拒绝采样的修复率。
    这批请求全带显式 seed，所以生产队列的 tail_tight 重渲不会参与（它只在没给 seed 时才跑），
    换 seed 完全由探针自己做。
    """
    n = args.seeds
    units, meta = [], []
    for i, t in enumerate(texts):
        for k in range(n):
            uid = "s%d_%d" % (i, k)
            units.append(calib_unit(uid, t, seed_for(t, k)))
            meta.append((uid, i, k))
    print("渲染 %d 句 × %d seed = %d 段…" % (len(texts), n, len(units)), flush=True)
    files = render_units(units, str(outdir), args.port, args.voice, args.batch)

    rows = []
    for uid, i, k in meta:
        p = files.get(uid)
        if not p:
            continue
        secs, voiced, sil = measure(p)
        rows.append(dict(uid=uid, idx=i, k=k, text=texts[i], chars=_spoken_chars(texts[i]),
                         secs=secs, voiced=voiced, sil=sil, tight=sil < TIGHT_S, wav=p))

    tight_rows = [r for r in rows if r["tight"]]
    print("\n紧尾巴 %d / %d 段 ⇒ p=%.3f" % (len(tight_rows), len(rows),
                                           len(tight_rows) / max(1, len(rows))))

    # 每句 N 次里紧几次
    per = {}
    for r in rows:
        per.setdefault(r["idx"], []).append(r)
    hist = {}
    for i, rs in per.items():
        c = sum(1 for r in rs if r["tight"])
        hist[c] = hist.get(c, 0) + 1
    print("每句 %d 次里紧几次的分布：" % n
          + "  ".join("%d次:%d句" % (k, hist[k]) for k in sorted(hist)))
    all_tight = [i for i, rs in per.items() if rs and all(r["tight"] for r in rs)]
    any_tight = [i for i, rs in per.items() if any(r["tight"] for r in rs)]
    print("  至少紧过一次的句子 %d/%d，%d 次全紧的句子 %d 句" % (len(any_tight), len(per), n, len(all_tight)))

    # 对每一次紧的，换 seed 重渲一次
    retry_units, retry_meta = [], []
    for r in tight_rows:
        uid = "r%s" % r["uid"]
        retry_units.append(calib_unit(uid, r["text"], seed_for(r["text"], "retry-%s" % r["k"])))
        retry_meta.append((uid, r))
    fixed = still = 0
    retried = []
    if retry_units:
        print("\n对 %d 个紧段换 seed 重渲一次…" % len(retry_units), flush=True)
        rfiles = render_units(retry_units, str(outdir), args.port, args.voice, args.batch)
        for uid, r in retry_meta:
            p = rfiles.get(uid)
            if not p:
                continue
            secs, voiced, sil = measure(p)
            ok = sil >= TIGHT_S
            fixed += int(ok)
            still += int(not ok)
            retried.append(dict(src=r, retry_sil=sil, retry_secs=secs, fixed=ok, wav=p))
        total = fixed + still
        print("  换 seed 后修好 %d/%d = %.1f%%，两次都紧 %d（%.1f%%）"
              % (fixed, total, 100.0 * fixed / max(1, total), still,
                 100.0 * still / max(1, total)))

    summary = {"tag": args.tag, "seeds": n, "n_units": len(rows), "tight": len(tight_rows),
               "p_tight": len(tight_rows) / max(1, len(rows)),
               "hist_tight_per_sentence": {str(k): v for k, v in sorted(hist.items())},
               "sentences_any_tight": len(any_tight),
               "sentences_all_tight": len(all_tight),
               "retry": {"n": fixed + still, "fixed": fixed, "still_tight": still,
                         "fixed_rate": fixed / max(1, fixed + still)},
               "rows": [{k: v for k, v in r.items() if k != "wav"} for r in rows],
               "retried": [{k: v for k, v in t.items() if k != "wav"} for t in retried]}
    dst = outdir / ("seeds-%s.json" % args.tag)
    dst.write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf8")
    print("  明细 -> %s" % dst)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出目录（tests/output 下）")
    ap.add_argument("--tag", default="run", help="本次标签，写进文件名")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--margin", type=float, default=None, help="声明服务器该跑的余量（秒）")
    ap.add_argument("--baseline", default=None, help="上一次的 json，用来打差分")
    ap.add_argument("--port", type=int, default=9889)
    ap.add_argument("--voice", default="albion")
    ap.add_argument("--sentences-file", default=None, help="JSON 数组；不给就从 logs/tts_bridge.log 取")
    ap.add_argument("--log", default=str(ROOT / "logs" / "irodori.log"))
    ap.add_argument("--sentences-log", default=str(ROOT / "logs" / "tts_bridge.log"),
                    help="从哪个 bridge 日志里取句子（工作树里没有 logs/ 时指向生产那份）")
    ap.add_argument("--seeds", type=int, default=0,
                    help=">0 时跑「同句 N 个 seed」实验（p(紧)、每句紧几次、换 seed 修复率）")
    ap.add_argument("--batch", type=int, default=20, help="每批提交多少段（默认 20）")
    args = ap.parse_args()

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    if args.sentences_file:
        texts = json.loads(pathlib.Path(args.sentences_file).read_text("utf8"))
    else:
        texts = log_sentences(args.sentences_log, args.count)
    if args.seeds:
        return run_seeds(texts, args, outdir)
    print("句子 %d 句，开始渲染（: %d）…" % (len(texts), args.port), flush=True)

    files = render(texts, str(outdir), args.port, args.voice)
    rows = []
    for i, t in enumerate(texts):
        p = files.get("calib:%d" % i)
        if not p:
            continue
        secs, voiced, sil = measure(p)
        rows.append(dict(text=t, chars=_spoken_chars(t), floor=_min_seconds_for(t),
                         secs=secs, voiced=voiced, sil=sil, cps=_spoken_chars(t) / secs))
    if not rows:
        print("没拿到音频，检查 :%d 是否在跑" % args.port)
        return 1

    live = server_margin(args.log)
    tight = [r for r in rows if r["sil"] < TIGHT_S]
    summary = {
        "tag": args.tag, "n": len(rows),
        "declared_margin": args.margin, "server_margin": live,
        "tight": len(tight),
        "mean_secs": float(np.mean([r["secs"] for r in rows])),
        "mean_sil": float(np.mean([r["sil"] for r in rows])),
        "median_cps": float(np.median([r["cps"] for r in rows])),
        "rows": rows,
    }
    print("\n== %s ==" % args.tag)
    print("  句数 %d | 尾巴紧(<%.2fs) %d 句 | 平均总时长 %.2fs | 平均尾静音 %.2fs | cps 中位 %.2f"
          % (summary["n"], TIGHT_S, summary["tight"], summary["mean_secs"],
             summary["mean_sil"], summary["median_cps"]))
    if args.margin is not None:
        if live is None:
            print("  ⚠ 日志里没读到 margin=（服务器可能是旧代码或还没渲染过）")
        elif abs(live - args.margin) > 1e-3:
            print("  ⚠ 声明 --margin %.2f，但服务器实际 margin=%.2f（改 TTS_PRED_MARGIN_S 后要重启 :%d）"
                  % (args.margin, live, args.port))
        else:
            print("  服务器 margin=%.2fs ✓" % live)

    if args.baseline:
        base = json.loads(pathlib.Path(args.baseline).read_text("utf8"))
        print("  vs 基线 %s：尾巴紧 %d→%d 句，平均总时长 %+.2fs，平均尾静音 %+.2fs，cps %+.2f"
              % (base.get("tag"), base["tight"], summary["tight"],
                 summary["mean_secs"] - base["mean_secs"],
                 summary["mean_sil"] - base["mean_sil"],
                 summary["median_cps"] - base["median_cps"]))

    dst = outdir / ("%s.json" % args.tag)
    dst.write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf8")
    print("  明细 -> %s" % dst)
    print("  紧尾巴的句子：")
    for r in sorted(tight, key=lambda r: r["sil"]):
        print("    sil=%.2f chars=%d secs=%.2f cps=%.2f %s" % (r["sil"], r["chars"], r["secs"],
                                                             r["cps"], r["text"][:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
