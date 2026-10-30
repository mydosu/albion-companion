# -*- coding: utf-8 -*-
"""男声漂移 / 句尾截断复现探针：对生产 :9889 用固定文本、固定条件渲 N 轮**随机 seed**，逐段量两样。

背景（2026-10-30 宣传片录制）：同一条回复里偶发几句基频掉到 121–148Hz（正常 296–380Hz），
另外有些句子的原始渲染停在响处（末 2ms 还有 -25～-28dBFS 就直接接数字零）。
渲染端原来不记 seed/caption，没法复盘；这个探针把每次的 seed 显式传进去并落盘。

复刻的是录制时那条回复的两段（中文原文按 27+7 字切开，units 1:2 / 1:3）：
  「快七点了，要是手头的事都忙得差不多了，今晚就好好歇歇，」
  「别再硬撑着啦。」
中文经生产 :9881 `/v2/prepare` 翻成日语（soft 切分带「、」收尾），合成条件用记录里的
neutral/weak 锚（`自然な声で、丁寧に`、cfg=3.0）。

只读：只 POST `/v2/render/open`、`/v2/render`、`/v2/render/close`，不重启、不改服务、不发聊天。

用法（用 tts/irodori/.venv 的 python）::

    python tests/tts/probes/tts_male_guard_probe.py --out tests/output/tts-malevoice-20261030

指标（每段一条）：
  f0_median_hz   整段自相关 F0 中位数（40ms 窗 / 10ms 步，无声段不计入；算法同
                 `tests/tts/probes/emotion_bench.f0_track`）
  tail30_db      原始 wav 末 30ms 峰值相对全段峰值（dB；> -30 = 截在响处，gap_shaper 会硬接零）
  dur_s          时长
  male           albion 且 f0_median_hz < 200（生产自检的判据）
  clip           tail30_db > -30（生产自检的判据）

输出：`raw/<轮>-<段>.wav`（整形前原始渲染）、`metrics.json`、`README.md`（汇总）。
"""
import argparse
import asyncio
import base64
import io
import json
import pathlib
import random
import statistics
import sys
import wave

import httpx
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "tts" / "probes"))

import emotion_bench as EB  # noqa: E402

BACKEND = "http://127.0.0.1:9889"
FAMILY = "neutral"
CAPTION = "自然な声で、丁寧に"          # 生产码本 v2 的 neutral/weak 锚（cfg=3.0）
CFG = 3.0
PLAN_VERSION = 6
CODEBOOK_VERSION = 2
VOICE = "albion"

# 录制时那条回复的两段（units 1:2 / 1:3）经生产 :9881 的译文；`、` 收尾是 soft 切分的延续语调。
DEFAULT_TEXTS = [
    "もうすぐ7時ですね、もし手元の執務が一段落ついたのでしたら、今夜はゆっくり休んでくださいね、",
    "指揮官、もう無理はしないでくださいね？",
]


def load_wav(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype={2: "<i2", 4: "<i4"}[sw]).astype("float32") / float(2 ** (8 * sw - 1))
    return sr, (a.reshape(-1, ch).mean(1) if ch > 1 else a)


def db(x):
    return round(float(20 * np.log10(max(float(x), 1e-12))), 1)


def tail30_db(a, sr):
    """末 30ms 峰值相对全段峰值（dB）；短于 30ms 或全零返回 None。"""
    n = max(1, int(0.03 * sr))
    peak = float(np.abs(a).max())
    if peak <= 0 or len(a) < n:
        return None
    return db(float(np.abs(a[-n:]).max()) / peak)


def measure(sr, a):
    f0 = EB.f0_track(a, sr)                     # 复用评测台的自相关 F0（无声段已跳过）
    f0_med = round(statistics.median(f0), 1) if len(f0) >= 5 else None
    tail = tail30_db(a, sr)
    return dict(sr=sr, dur_s=round(len(a) / sr, 3), f0_median_hz=f0_med,
                f0_frames=len(f0), tail30_db=tail,
                male=bool(f0_med is not None and f0_med < 200.0),
                clip=bool(tail is not None and tail > -30.0))


async def sse_lines(r):
    event = None
    async for line in r.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            yield event, json.loads(line[5:].strip())


async def render_unit(client, address, text, seed, unit_id, voice=VOICE):
    body = dict(address, plan_version=PLAN_VERSION, voice=voice, format="wav",
                units=[{"unit_id": unit_id, "sub_index": 0, "text": text, "family": FAMILY,
                        "caption": CAPTION, "cfg": CFG, "scene_id": 1, "seed": seed,
                        "codebook_version": CODEBOOK_VERSION}])
    blob = None
    async with client.stream("POST", BACKEND + "/v2/render", json=body, timeout=300) as r:
        r.raise_for_status()
        async for name, payload in sse_lines(r):
            if name == "audio_chunk":
                blob = base64.b64decode(payload["audio_base64"])
            elif name == "unit.failed":
                raise RuntimeError(payload.get("error"))
    if blob is None:
        raise RuntimeError("没有 audio_chunk")
    return blob


async def main_async(args):
    out = pathlib.Path(args.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    texts = args.text or DEFAULT_TEXTS
    rng = random.Random(args.rng_seed)          # 只决定这批 seed 的抽样序列，方便复现整批
    rows, failures = [], []
    async with httpx.AsyncClient() as client:
        for i in range(args.rounds):
            opened = (await client.post(BACKEND + "/v2/render/open",
                                        json={"voice": args.voice})).json()
            address = {"session_id": opened["session_id"], "generation": opened["generation"]}
            for j, text in enumerate(texts):
                seed = rng.randrange(0, 2 ** 31)
                try:
                    blob = await render_unit(client, address, text, seed, "u%d" % j, args.voice)
                except Exception as e:               # noqa: BLE001 - 一次失败不该毁掉整批
                    failures.append({"i": i, "seg": j, "seed": seed,
                                     "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
                    print("%02d-%d seed=%d FAILED %s" % (i, j, seed, e), flush=True)
                    continue
                path = out / "raw" / ("%02d-%d.wav" % (i, j))
                path.write_bytes(blob)
                sr, a = load_wav(blob)
                m = measure(sr, a)
                m.update(i=i, seg=j, seed=seed, raw=str(path.relative_to(out)).replace("\\", "/"),
                         text=text, caption=CAPTION, cfg=CFG, ship=args.voice)
                rows.append(m)
                print("%02d-%d seed=%-10d dur=%.2fs f0=%s tail30=%s dB  %s"
                      % (i, j, seed, m["dur_s"],
                         "—" if m["f0_median_hz"] is None else "%.1fHz" % m["f0_median_hz"],
                         "—" if m["tail30_db"] is None else "%.1f" % m["tail30_db"],
                         " ".join(k for k in ("male", "clip") if m[k])), flush=True)
            await client.post(BACKEND + "/v2/render/close", json=address)
    summary = {"rounds": args.rounds, "segments": len(texts), "rendered": len(rows),
               "failed": len(failures),
               "male": sum(1 for r in rows if r["male"]),
               "clip": sum(1 for r in rows if r["clip"])}
    for j in range(len(texts)):
        seg = [r for r in rows if r["seg"] == j]
        f0 = [r["f0_median_hz"] for r in seg if r["f0_median_hz"] is not None]
        tail = [r["tail30_db"] for r in seg if r["tail30_db"] is not None]
        summary["seg%d" % j] = dict(n=len(seg), male=sum(1 for r in seg if r["male"]),
                                    clip=sum(1 for r in seg if r["clip"]),
                                    f0_min=min(f0) if f0 else None,
                                    f0_max=max(f0) if f0 else None,
                                    f0_median=round(statistics.median(f0), 1) if f0 else None,
                                    tail_max=max(tail) if tail else None)
    payload = dict(backend=BACKEND, texts=texts, caption=CAPTION, cfg=CFG, ship=args.voice,
                   plan_version=PLAN_VERSION, codebook_version=CODEBOOK_VERSION,
                   rng_seed=args.rng_seed, summary=summary, rows=rows, failures=failures)
    (out / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 男声漂移 / 句尾截断复现（%d 轮 × %d 段 = %d 次随机 seed）"
             % (args.rounds, len(texts), len(rows)), "",
             "条件：`%s` / `%s` / cfg=%s / %s；每轮开一个 session，两段各用一个随机 seed。"
             % (args.voice, CAPTION, CFG, BACKEND), "",
             "- 男声（F0 中位 < 200Hz）：**%d/%d**" % (summary["male"], len(rows)),
             "- 截在响处（末 30ms > 峰值 -30dB）：**%d/%d**" % (summary["clip"], len(rows)),
             "- 失败：%d" % len(failures)]
    for j in range(len(texts)):
        s = summary["seg%d" % j]
        lines.append("- 段 %d：男声 %d/%d，截尾 %d/%d，F0 中位 %s–%s Hz（中位 %s），最响尾 %s dB"
                     % (j, s["male"], s["n"], s["clip"], s["n"], s["f0_min"], s["f0_max"],
                        s["f0_median"], s["tail_max"]))
    lines += ["", "| 轮 | 段 | seed | 时长 s | F0 中位 Hz | 末 30ms dB | 男声 | 截尾 |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %d | %d | %d | %.2f | %s | %s | %s | %s |" % (
            r["i"], r["seg"], r["seed"], r["dur_s"], r["f0_median_hz"], r["tail30_db"],
            "✔" if r["male"] else "", "✔" if r["clip"] else ""))
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--text", action="append", default=None,
                    help="单元文本，可重复（缺省 = 录制时那两段的日语译文）")
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--rng-seed", type=int, default=20261030)
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
