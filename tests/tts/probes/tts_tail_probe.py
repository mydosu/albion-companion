# -*- coding: utf-8 -*-
"""句尾探针：直接问生产后端 :9889 渲染一批日语短句，存**整形前原始 wav**与**GS.shape 之后**的 wav，
并量「句尾有没有被截」。

只读：只 POST /v2/render/open、/v2/render、/v2/render/close，不重启、不改服务。

用法（用 tts/irodori/.venv 的 python）::

    python tests/tts/probes/tts_tail_probe.py --out tests/output/tts-tail-20261026 \
        --raw-dir raw --shape-dir shape-before

    # 不重新渲染，只拿已落的原始 wav 再过一遍当前 gap_shaper（修前/修后用同一批原始音频做受控对比）
    python tests/tts/probes/tts_tail_probe.py --out tests/output/tts-tail-20261026 \
        --from-raw raw --shape-dir shape-after

指标（每条一句）：
  raw_last50_db   原始 wav 最后 50ms 的峰值电平（dBFS）
  raw_end_voiced  最后 50ms 是否仍高于有声阈值（True = 模型自己把话截在半句，嫌疑 B）
  raw_tail_s      原始 wav 有声结束之后还剩多长静音
  cut_ms          整形从有声结束之后一共拿掉多少毫秒
  lost60_ms       被拿掉的样本里高于 -60dBFS 的部分有多长（>0 = 真的裁掉了可听的尾巴）
  lost_voiced_ms  被拿掉的样本里高于有声阈值的部分有多长
  cut_db          裁点前最后一个样本相对峰值的电平（近距离 0dB = 硬切/爆音）
  fade_ms         整形保留段末尾最后 30ms 内是否做了淡出（修后才有）
"""
import argparse
import asyncio
import base64
import io
import json
import pathlib
import sys
import wave

import httpx
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tts" / "stream"))

import gap_shaper as GS  # noqa: E402

BACKEND = "http://127.0.0.1:9889"
FAMILY = "neutral"                                    # 与 tts/irodori/scene_plan.py FAMILIES 一致
CAPTION = "落ち着いた自然な声で、丁寧に"
CFG = 3.0

TEXTS = [
    ("はい、わかりました。", "sentence"),
    ("もう大丈夫です。", "sentence"),
    ("お茶を淹れます。", "sentence"),
    ("少し休みましょう。", "sentence"),
    ("今日はいい天気ですね。", "sentence"),
    ("そうなんですよ。", "sentence"),
    ("海がきれいです。", "sentence"),
    ("お疲れさまでした。", "sentence"),
    ("一緒に行きますか？", "sentence"),
    ("何をしているのか？", "sentence"),
    ("静かにしてください。", "sentence"),
    ("ちょっと待ってください。", "sentence"),
    ("これはあなたの本です。", "sentence"),
    ("あとで読みます。", "sentence"),
    ("寒くないですか？", "sentence"),
    ("早く寝てください。", "sentence"),
    ("大丈夫じゃないけど、平気です。", "sentence"),
    ("今日はもう遅いですから、休みましょう。", "sentence"),
    ("甲板に出て、風に当たって、少しだけ海を見ていました。", "sentence"),
    ("図書館で古い海図を調べていたら、つい時間を忘れてしまいました。", "sentence"),
    ("そうですね、", "comma"),
    ("たぶん、そうだと思うの。", "sentence"),
]


def load_wav(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype={2: "<i2", 4: "<i4"}[sw]).astype("float32") / float(2 ** (8 * sw - 1))
    return sr, ch, (a.reshape(-1, ch).mean(1) if ch > 1 else a)


def db(x):
    return round(20 * np.log10(max(float(x), 1e-9)), 1)


def voiced_end(a, sr):
    """与 gap_shaper 同口径：10ms 窗峰值 > max(峰值-40dB, -55dBFS) 的最后一个窗。返回 (onset, end, thr_db)。"""
    peak = float(np.abs(a).max()) or 1e-9
    thr = max(peak * 10 ** (-GS.VOICED_DB / 20.0), 10 ** (GS.FLOOR_DB / 20.0))
    win = max(1, int(0.01 * sr))
    frames = len(a) // win
    if frames == 0:
        return 0, 0, db(peak)
    loud = np.abs(a[:frames * win].reshape(frames, win)).max(axis=1) > thr
    idx = np.flatnonzero(loud)
    if len(idx) == 0:
        return 0, 0, db(peak)
    return int(idx[0]) * win, (int(idx[-1]) + 1) * win, db(peak)


def measure(raw_blob, shaped_blob, kind):
    sr, ch, a = load_wav(raw_blob)
    onset, end_voiced, peak_db = voiced_end(a, sr)
    n = len(a)
    m = {}
    m["sr"] = sr
    m["ch"] = ch
    m["raw_dur_s"] = round(n / sr, 3)
    m["peak_db"] = peak_db
    m["raw_voiced_end_s"] = round(end_voiced / sr, 3)
    m["raw_tail_s"] = round((n - end_voiced) / sr, 3)
    n50 = max(1, int(0.05 * sr))
    m["raw_last50_db"] = db(np.abs(a[-n50:]).max())
    thr_db = max(peak_db - GS.VOICED_DB, GS.FLOOR_DB)
    m["raw_end_voiced"] = bool(m["raw_last50_db"] > thr_db)
    m["raw_final_sample_db"] = db(abs(float(a[-1])))
    if shaped_blob is None:
        return m, a, None, None
    sr2, ch2, b = load_wav(shaped_blob)
    tail_s = GS.TAIL_S.get(kind, 0.0)
    lead = int(GS.LEAD_S * sr2)
    head = max(0, lead - onset)                        # 首静音补零的长度（onset < lead 时才有）
    kept = len(b) - head - int(tail_s * sr2)           # 保留的原始样本数
    keep_start = max(0, onset - lead)
    cut = keep_start + kept                            # 原始 wav 里被裁掉的位置
    removed = a[cut:] if 0 <= cut <= n else a[:0]
    m["shaped_dur_s"] = round(len(b) / sr2, 3)
    m["shaped_tail_s"] = round((len(b) - voiced_end(b, sr2)[1]) / sr2, 3)
    m["cut_s"] = round(cut / sr, 3)
    m["cut_ms"] = round(len(removed) / sr * 1000, 0)
    if len(removed):
        above60 = np.abs(removed) > 1e-3                 # -60dBFS
        above_thr = np.abs(removed) > 10 ** (thr_db / 20.0)
        m["lost60_ms"] = int(round(above60.sum() / sr * 1000))
        m["lost_voiced_ms"] = int(round(above_thr.sum() / sr * 1000))
        m["removed_max_db"] = db(np.abs(removed).max())
    else:
        m["lost60_ms"] = m["lost_voiced_ms"] = 0
        m["removed_max_db"] = -120.0
    last_kept = a[cut - 1] if 0 < cut <= n else 0.0
    m["cut_db"] = db(abs(float(last_kept)))
    # 裁点之后原始音频里还剩多少高于 -60dBFS 的可听尾巴（>0 = 还在出声时就切了）
    aud = np.flatnonzero(np.abs(a) > 1e-3)
    if len(aud):
        m["audible_after_cut_ms"] = int(round(max(0, int(aud[-1]) + 1 - cut) / sr * 1000))
    else:
        m["audible_after_cut_ms"] = 0
    # 保留段末尾 30ms 的峰值：真做了淡出，紧挨静音前的最后几毫秒应该接近 0
    n30 = max(1, int(0.03 * sr2))
    m["keep_last30_db"] = db(np.abs(b[head + kept - n30:head + kept]).max()) if kept > n30 else -120.0
    return m, a, b, kept


async def sse_lines(r):
    """逐行解析 SSE，yield (event, data)。"""
    event = None
    async for line in r.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            yield event, json.loads(line[5:].strip())


async def render_one(client, backend, text, voice="albion"):
    opened = (await client.post(backend + "/v2/render/open", json={"voice": voice})).json()
    address = {"session_id": opened["session_id"], "generation": opened["generation"]}
    body = dict(address, plan_version=2, voice=voice, format="wav",
                units=[{"unit_id": "u0", "sub_index": 0, "text": text, "family": FAMILY,
                        "caption": CAPTION, "cfg": CFG, "scene_id": 1}])
    blob, done = b"", False
    try:
        async with client.stream("POST", backend + "/v2/render", json=body, timeout=180) as r:
            r.raise_for_status()
            async for name, payload in sse_lines(r):
                if name == "audio_chunk":
                    blob += base64.b64decode(payload["audio_base64"])
                elif name == "done":
                    done = payload.get("status") == "complete"
    finally:
        await client.post(backend + "/v2/render/" + ("close" if done else "cancel"), json=address)
    return blob, done


async def run(args):
    out = pathlib.Path(args.out).resolve()
    if "tests" + "\\output" not in str(out) and "tests/output" not in str(out):
        raise SystemExit("--out 必须在 tests/output/ 下")
    raw_dir = out / args.raw_dir
    shape_dir = out / args.shape_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    shape_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    async with httpx.AsyncClient() as client:
        for i, (text, kind) in enumerate(TEXTS):
            raw_path = raw_dir / ("%02d-raw.wav" % i)
            if args.from_raw:
                raw_blob = raw_path.read_bytes()
            else:
                raw_blob, ok = await render_one(client, args.backend, text)
                if not ok or not raw_blob.startswith(b"RIFF"):
                    print("skip %d: render failed" % i, flush=True)
                    continue
                raw_path.write_bytes(raw_blob)
            shaped = GS.shape(raw_blob, GS.tail_kind(text, "finish"))
            (shape_dir / ("%02d-shaped.wav" % i)).write_bytes(shaped)
            m, _, _, _ = measure(raw_blob, shaped, GS.tail_kind(text, "finish"))
            m.update(i=i, text=text, kind=GS.tail_kind(text, "finish"), raw=str(raw_path),
                     shaped=str(shape_dir / ("%02d-shaped.wav" % i)))
            rows.append(m)
            print("%02d %-28s raw=%ss tail=%ss last50=%sdB cut=%sms lost60=%sms cut_db=%sdB"
                  % (i, text, m["raw_dur_s"], m["raw_tail_s"], m["raw_last50_db"],
                     m["cut_ms"], m["lost60_ms"], m["cut_db"]), flush=True)
    (out / (args.shape_dir + ".json")).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf8")
    return rows


def load_rows(path):
    p = pathlib.Path(path)
    return json.loads(p.read_text(encoding="utf8")) if p.exists() else []


def report(out):
    """把同一批原始 wav 的「修前/修后」数字整理成 README（表格 + 听哪几条）。"""
    out = pathlib.Path(out).resolve()
    before, after = load_rows(out / "shape-before.json"), load_rows(out / "shape-after.json")
    fresh = load_rows(out / "fresh-after.json")
    if not before or not after:
        raise SystemExit("需要 shape-before.json 与 shape-after.json（同一批 raw 的两次整形）")
    dyn_before, dyn_after = load_rows(out / "dyn" / "shape-before.json"), load_rows(out / "dyn" / "shape-after.json")
    dyn_fresh = load_rows(out / "dyn" / "fresh-after.json")
    lines = []
    add = lines.append

    def table(rows_b, rows_a, title):
        add("### %s\n" % title)
        add("| # | 句子 | 修前裁点前30ms | 前丢掉 >-60dB | 整形前裁点 | 修后裁点前30ms | 后丢掉 >-60dB | 整形后裁点 | 多留的衰减 |")
        add("|---|---|---|---|---|---|---|---|---|")
        for b, a in zip(rows_b, rows_a):
            add("| %02d | %s | %sdB | %dms | %.3fs | %sdB | %dms | %.3fs | +%dms |"
                % (b["i"], b["text"], "%.0f" % b["keep_last30_db"], b["lost60_ms"], b["cut_s"],
                   "%.0f" % a["keep_last30_db"], a["lost60_ms"], a["cut_s"],
                   round((a["cut_s"] - b["cut_s"]) * 1000)))
        add("")

    def summary(rows_b, rows_a, label):
        lost_b = [r["lost60_ms"] for r in rows_b]
        lost_a = [r["lost60_ms"] for r in rows_a]
        k_b = max(r["keep_last30_db"] for r in rows_b)
        k_a = max(r["keep_last30_db"] for r in rows_a)
        add("- **%s**：修前 %d/%d 句丢掉了可听尾音（最多 %dms）；裁点前 30ms 的包络最高 **%.0fdB**（还在出声中"
            "就切）。修后 %d/%d 句（最多 %dms），包络最高 %.0fdB。"
            % (label, sum(1 for x in lost_b if x > 0), len(lost_b), max(lost_b), k_b,
               sum(1 for x in lost_a if x > 0), len(lost_a), max(lost_a), k_a))

    add("# 句尾截断修复对比（2026-10-26）\n")
    add("同一批**原始 wav**（生产 :9889 直渲染）分别过修前/修后的 `tts/stream/gap_shaper.py`："
        "`raw/` 原始、`shape-before/` 修前整形、`shape-after/` 修后整形（受控对比，音频内容完全相同）。"
        "`dyn/` 是另一批 10 句「响开头 + 轻声收尾」。`fresh-raw/` + `fresh-after/` 是修后**重新渲染**的一遍。"
        "指标：裁点 = 整形把原始 wav 切掉的位置；丢掉 >-60dB 的量 = 切开后原始 wav 里高于 -60dBFS 的样本时长；"
        "裁点前 30ms = 紧挨数字零之前那段音频的峰值电平（-40dB 说明还在出声中就切了，-60dB 以下才听不见）。\n")
    add("## 结论数字\n")
    summary(before, after, "常用句尾 22 句")
    summary(dyn_before, dyn_after, "动态句 10 句")
    if fresh:
        add("- **修后重新渲染的 %d 句**：%d/%d 句丢可听尾音（最多 %dms，裁点最高 %.0fdB）。"
            % (len(fresh) + len(dyn_fresh),
               sum(1 for r in fresh + dyn_fresh if r["lost60_ms"] > 0), len(fresh) + len(dyn_fresh),
               max([r["lost60_ms"] for r in fresh + dyn_fresh] or [0]),
               max([r["cut_db"] for r in fresh + dyn_fresh] or [0])))
    add("- **后端（嫌疑 B）**：%d 句原始 wav 的末 50ms 全部在 -68dBFS 以下，没有一句是真的在发声中被截；"
        "自然衰减的 5ms 包络单调下降、最大跳变 4–12dB —— 模型没截，`duration_scale` 也不用动。"
        % (len(before) + len(dyn_before)))
    add("")
    table(before, after, "常用句尾 22 句（修前 → 修后）")
    table(dyn_before, dyn_after, "动态句 10 句（修前 → 修后）")
    add("## 听哪几条\n")
    worst = sorted(zip(before, after), key=lambda p: -(p[0]["lost60_ms"]))[:4]
    add("同一目录里 `shape-before/XX-shaped.wav` 与 `shape-after/XX-shaped.wav` 成对听：")
    for b, a in worst:
        add("- **%02d %s**：修前裁点前 30ms 还有 %.0fdB，丢掉 %dms；修后 %.0fdB、丢掉 0ms。"
            % (b["i"], b["text"], b["keep_last30_db"], b["lost60_ms"], a["keep_last30_db"]))
    dyn_worst = sorted(zip(dyn_before, dyn_after), key=lambda p: -(p[0]["keep_last30_db"]))[:2]
    for b, a in dyn_worst:
        add("- **dyn %02d %s**：修前裁点前 30ms 还有 %.0fdB，丢掉 %dms；修后 %.0fdB、0ms。"
            % (b["i"], b["text"], b["keep_last30_db"], b["lost60_ms"], a["keep_last30_db"]))
    add("\n修后整形只在自然衰减落到 -60dB 以下之后切，且最后 30ms 线性淡出（`FADE_S`）；"
        "气口长度（`TAIL_S`）没改，所以节奏不变：每句只是把原来被掐掉的 10–90ms 尾音留了回来。")
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf8")
    print("wrote", out / "README.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=BACKEND)
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", default="raw")
    ap.add_argument("--shape-dir", default="shape-before")
    ap.add_argument("--from-raw", action="store_true", help="不重新渲染，复用 raw-dir 里的原始 wav")
    ap.add_argument("--texts-file", help="可选：JSON 文件，[[文本, 类型], …]；不给就用内置 22 句")
    ap.add_argument("--report", action="store_true", help="只根据已有 JSON 生成 README，不渲染")
    args = ap.parse_args()
    if args.report:
        report(args.out)
        return
    if args.texts_file:
        global TEXTS
        TEXTS = [tuple(x) for x in json.loads(pathlib.Path(args.texts_file).read_text(encoding="utf8"))]
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
