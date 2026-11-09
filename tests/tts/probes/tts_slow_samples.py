# -*- coding: utf-8 -*-
"""慢速样张 + 整形削尾对照（2026-11-09）。

给指定句子按「预测时长 × 1.0 / 1.15 / 1.3」渲染三档，同一句三档共用一颗 seed，每档存两份：
  * `raw_*.wav`     —— 9889 原始输出（/v2/render 的 audio_chunk 原样）
  * `shaped_*.wav`  —— 直接 import `tts/stream/gap_shaper.py` 离线整形，和生产 9885 同一个函数
                        （`GS.shape(bytes, GS.tail_kind(text, reason))`），用来量「整形有没有削掉
                        弱的句尾（-40~-55dB 那段）」。

关于预算：/v2/render 的每单元字段里**没有**秒数预算，唯一的请求级旋钮是 `unit["min_seconds"]`
（`render_queue.run_infer_synth`：有就覆盖字数下限，见 render_queue.py:124-130）；预测时长本身
不落日志也不进 SSE，所以这里先用**默认预算**渲一档，拿它的实际时长当预测基线（`TTS_PRED_MARGIN_S`
默认 0，模型会把预算吃掉 ~95%，见 inference_runtime.py 的注释），再按比例给 1.15/1.3 档。

用法::

    python tests/tts/probes/tts_slow_samples.py --out tests/output/tts-trunc-20261109/slow

只调生产 :9889 的 /v2/render（自开自关临时会话），不碰壳、不碰 9885、不改任何配置。
"""
import argparse
import base64
import hashlib
import json
import pathlib
import sys

import numpy as np
import requests
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))
sys.path.insert(0, str(ROOT / "tts" / "stream"))
from render_queue import _spoken_chars  # noqa: E402
import gap_shaper as GS  # noqa: E402

# 2026-11-09 01:06 那一轮（session sc75f2f8cb80c）的三句，参数照抄生产：
#   voice=albion / cfg=3.0 / caption_mode=free / plan_version=4(codebook v2)
#   family & scene 来自 logs/stream.log 的「情绪」行
#   caption 来自 logs/irodori.log final 行的 caption=（GBK 存的中文/日文描述，已解码）
SENTENCES = [
    ("s1", "えっ……わ、私のフィギュアも指揮官の机の上に置くのですか…？",
     "neutral", 1),
    ("s2", "先輩方に囲まれるなんて、想像するだけでも少し恥ずかしいですね…",
     "intimate", 2),
    ("s3", "でも、指揮官のすぐお傍に置いていただけるなら、本当はすごく嬉しいです…",
     "bright", 3),
]
CAPTION = "明るく弾んだ声で、早口に、話す。"
TIERS = (1.0, 1.15, 1.30)


def seed_for(text):
    h = hashlib.sha256(("%s|slow-sample" % text).encode()).hexdigest()[:8]
    return int(h, 16) % (2 ** 31 - 1)


def unit_body(uid, text, family, scene, seed, min_seconds=None, caption=CAPTION):
    u = {"unit_id": uid, "sub_index": 0, "text": text, "family": family, "caption": caption,
         "cfg": 3.0, "scene_id": scene, "codebook_version": 2, "caption_mode": "free",
         "seed": seed}
    if min_seconds is not None:
        u["min_seconds"] = round(float(min_seconds), 3)
    return u


def render_one(unit, port, voice="albion"):
    url = "http://127.0.0.1:%d" % port
    h = requests.post(url + "/v2/render/open", json={"voice": voice}, timeout=30).json()
    body = {"session_id": h["session_id"], "generation": h["generation"], "plan_version": 4,
            "voice": voice, "format": "wav", "units": [unit]}
    r = requests.post(url + "/v2/render", json=body, stream=True, timeout=300)
    r.raise_for_status()
    blob, meta = None, {}
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        p = json.loads(line[5:])
        if "audio_base64" in p:
            blob = base64.b64decode(p["audio_base64"])
            meta = {k: p.get(k) for k in ("sr", "samples", "format", "style")}
        elif p.get("error"):
            print("  ! 服务报错:", p["error"], flush=True)
    requests.post(url + "/v2/render/close",
                  json={"session_id": h["session_id"], "generation": h["generation"]}, timeout=30)
    if blob is None:
        raise RuntimeError("没拿到音频：%s" % unit["unit_id"])
    return blob, meta


def measure(path):
    """与 9889 final 行同口径：10ms 帧 RMS，阈值 max(峰值-40dB, -55dBFS)。"""
    d, sr = sf.read(str(path), dtype="float32")
    if d.ndim > 1:
        d = d.mean(axis=1)
    win = max(1, int(0.01 * sr))
    n = len(d) // win
    rms = np.sqrt((d[:n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(np.abs(d).max()) or 1e-12
    thr = max(peak * 10 ** (-40.0 / 20.0), 10 ** (-55.0 / 20.0))
    loud = np.flatnonzero(rms > thr)
    onset = loud[0] * win / sr if len(loud) else 0.0
    voiced = (int(loud[-1]) + 1) * win / sr if len(loud) else 0.0
    secs = len(d) / sr
    k50 = max(1, int(0.05 * sr))
    end = int(round(voiced * sr))
    seg = d[max(0, end - k50):end] if end > 0 else d[-k50:]
    rms50 = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
    return dict(secs=secs, onset=onset, voiced=voiced, span=max(0.0, voiced - onset),
                sil=max(0.0, secs - voiced),
                rms50_db=20 * np.log10(max(rms50, 1e-12) / peak),
                peak=peak, sr=sr)


CAPTIONS = {
    "A": "明るく弾んだ声で、早口に、話す。",        # 生产原样（voice_caption.py MANNER["exc"]）
    "B": "明るく弾んだ声で、話す。",                # 去掉「早口に」
    "C": "明るく弾んだ声で、語尾を上げて、話す。",  # 换成语尾上扬
}


def run_captions(args, out):
    """同一句 s3、同 seed、默认预算，只换描述；只留 shaped 版 + 盲听副本。"""
    sid, text, family, scene = SENTENCES[2]
    seed = seed_for(text)
    chars = _spoken_chars(text)
    rows = []
    for tag, cap in CAPTIONS.items():
        blob = render_one(unit_body(sid, text, family, scene, seed, caption=cap),
                          args.port, args.voice)[0]
        raw = out / ("s3_%s_raw.wav" % tag)
        raw.write_bytes(blob)
        shaped = out / ("s3_%s.wav" % tag)
        shaped.write_bytes(GS.shape(blob, GS.tail_kind(text, "finish")))
        m = measure(shaped)
        rows.append(dict(tag=tag, caption=cap, chars=chars, secs=m["secs"], span=m["span"],
                         sil=m["sil"], vcps=chars / m["span"] if m["span"] else 0,
                         rms50=m["rms50_db"], wav=str(shaped)))
        print("[%s] %s -> 有声 %.2fs 语速 %.2f 尾静音 %.2fs 末50ms %.1fdB"
              % (tag, cap, m["span"], chars / m["span"], m["sil"], m["rms50_db"]), flush=True)
        raw.unlink()                                  # 只要 shaped

    import random
    blind_dir = out / "blind"
    blind_dir.mkdir(parents=True, exist_ok=True)
    order = list(CAPTIONS)
    random.SystemRandom().shuffle(order)
    mapping = {}
    for i, tag in enumerate(order, 1):
        (blind_dir / ("%d.wav" % i)).write_bytes((out / ("s3_%s.wav" % tag)).read_bytes())
        mapping[str(i)] = tag
    (out / "key.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1), "utf8")
    (out / "summary.json").write_text(
        json.dumps({"sentence": sid, "seed": seed, "rows": rows}, ensure_ascii=False, indent=1),
        "utf8")

    print("\n| 版本 | 描述 | 有声 | 语速 | 尾静音 | 末50ms dB |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print("| %s | %s | %.2fs | %.2f | %.2fs | %.1f |"
              % (r["tag"], r["caption"], r["span"], r["vcps"], r["sil"], r["rms50"]))
    print("\n-> %s（盲听副本 %s，对应关系见 key.json）" % (out, blind_dir))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=9889)
    ap.add_argument("--voice", default="albion")
    ap.add_argument("--mode", choices=("tiers", "captions"), default="tiers",
                    help="tiers=预算三档；captions=s3 换描述（A/B/C）")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.mode == "captions":
        return run_captions(args, out)

    rows = []
    for sid, text, family, scene in SENTENCES:
        seed = seed_for(text)
        chars = _spoken_chars(text)
        print("[%s] %d 字 seed=%d" % (sid, chars, seed), flush=True)
        raw_path = out / ("%s_x1.00_raw.wav" % sid)
        blob, meta = render_one(unit_body(sid, text, family, scene, seed), args.port, args.voice)
        raw_path.write_bytes(blob)
        base = measure(raw_path)
        # 预测时长 ≈ 默认预算下的**总**时长（margin=0 时模型把预算吃掉 ~95%+，
        # 且 9889 的 wav 含句首静音；用 voiced 会低估，1.15 档就落不到预算上）
        pred = base["secs"]
        print("  1.00 默认预算 %.2fs（voiced %.2fs）" % (base["secs"], pred), flush=True)

        for f in TIERS:
            if f == 1.0:
                rp = raw_path
            else:
                rp = out / ("%s_x%.2f_raw.wav" % (sid, f))
                blob = render_one(
                    unit_body(sid, text, family, scene, seed, min_seconds=pred * f),
                    args.port, args.voice)[0]
                rp.write_bytes(blob)
            shaped = out / ("%s_x%.2f_shaped.wav" % (sid, f))
            shaped.write_bytes(GS.shape(rp.read_bytes(), GS.tail_kind(text, "finish")))
            rm, sm = measure(rp), measure(shaped)
            rows.append(dict(sentence=sid, tier=f, chars=chars, text=text, seed=seed,
                             raw_secs=rm["secs"], raw_onset=rm["onset"], raw_span=rm["span"],
                             raw_sil=rm["sil"], raw_rms50=rm["rms50_db"],
                             raw_vcps=chars / rm["span"] if rm["span"] else 0,
                             shaped_secs=sm["secs"], shaped_onset=sm["onset"],
                             shaped_span=sm["span"], shaped_sil=sm["sil"],
                             shaped_rms50=sm["rms50_db"],
                             shaped_vcps=chars / sm["span"] if sm["span"] else 0,
                             span_delta=sm["span"] - rm["span"],
                             raw_wav=str(rp), shaped_wav=str(shaped)))
            print("  x%.2f raw %.2fs/onset %.2f/span %.2fs/sil %.2fs | shaped %.2fs/onset %.2f/"
                  "span %.2fs/sil %.2fs | Δspan %+.3fs"
                  % (f, rm["secs"], rm["onset"], rm["span"], rm["sil"],
                     sm["secs"], sm["onset"], sm["span"], sm["sil"],
                     sm["span"] - rm["span"]), flush=True)

    (out / "summary.json").write_text(
        json.dumps({"caption": CAPTION, "voice": args.voice, "port": args.port, "rows": rows},
                   ensure_ascii=False, indent=1), "utf8")

    print("\n| 句 | 档 | raw 有声 | raw 语速 | raw 尾静音 | shaped 有声 | shaped 语速 | shaped 尾静音 | 末50ms dB(raw/shaped) | Δspan |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %s | x%.2f | %.2fs | %.2f | %.2fs | %.2fs | %.2f | %.2fs | %.1f / %.1f | %+.3fs |"
              % (r["sentence"], r["tier"], r["raw_span"], r["raw_vcps"], r["raw_sil"],
                 r["shaped_span"], r["shaped_vcps"], r["shaped_sil"],
                 r["raw_rms50"], r["shaped_rms50"], r["span_delta"]))
    print("\n-> %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
