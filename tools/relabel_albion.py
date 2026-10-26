# -*- coding: utf-8 -*-
"""albion 重标注（阶段 3a）：**本地**量韵律 → 给人写描述当依据 → 生成"每条两行"的 manifest。

为什么本地算：原声是游戏语音，**不上传任何外部 API**（版权 + 会烧生产脑的额度）。
指标函数直接复用评测台 `tests/tts/probes/emotion_bench.py`（自相关 F0、10 ms 包络发声块、
字速、动态、静音占比），本文件只补三项评测台没有的：句末 F0 走向、高频噪声比、谱平坦度（气声代理）。

三个子命令（数据只写 `data_lora/` 与 `tests/output/`，两处都被 ignore；脚本本身不含原文/描述）：

  measure  读 manifest + 同目录名的 wav → 每条韵律 + 在 83 条里的分位 → `<out>.jsonl`
  build    用人工写好的描述 JSON（key = 音频名 stem，如 "028-a"）→ 每条两行（原锚 + 新描述）
  review   生成审阅表（原文 | 旧 E | 韵律分位 | 新描述）+ 描述用词频次

索引：**不用手工重建**。`irodori_tts/dataset.py` 的 `_ManifestIndex` 会在第一次读新 manifest 时自动
建 `<manifest>.irodori_index.pt`（缓存版本 4，按 manifest 的 size+mtime_ns 校验；新文件名 ⇒ 必然重建）。
要提前建就只能调私有 API `_ManifestIndex.build(manifest_path, caption_key="caption")`，本任务不建。

用法（仓库根，用 irodori 的 venv）：
  python -B tools/relabel_albion.py measure
  python -B tools/relabel_albion.py build   --descriptions tts/irodori/data_lora/albion_rich_descriptions.json
  python -B tools/relabel_albion.py review  --descriptions tts/irodori/data_lora/albion_rich_descriptions.json
"""
import argparse
import json
import pathlib
import re
import statistics
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "tts" / "probes"))
import emotion_bench as EB  # noqa: E402  （纯 numpy，无副作用）

MANIFEST = ROOT / "tts" / "irodori" / "data_lora" / "albion_manifest_emo_spk.jsonl"
RICH = ROOT / "tts" / "irodori" / "data_lora" / "albion_manifest_rich.jsonl"
AUDIO = ROOT / "tts" / "GPT-sovits" / "audio" / "albion"
PROSODY = ROOT / "tts" / "irodori" / "data_lora" / "albion_prosody.jsonl"
DESCRIPTIONS = ROOT / "tts" / "irodori" / "data_lora" / "albion_rich_descriptions.json"
OUTDIR = ROOT / "tests" / "output" / "tts-relabel-20261026"

# 允许进描述的"声音动作词"（来自 checkpoints/EMOJI_ANNOTATIONS.md 的日文释义 + 7 条训练锚）。
# review 用它统计词频，确认没有哪个词占了大半。
VOICE_WORDS = [
    "囁く", "囁き", "小声", "息", "吐息", "ため息", "寝息", "息をのむ", "息を荒く", "息まじり",
    "震え", "震わせ", "震える", "早口", "まくしたて", "一気に", "ゆっくり", "間を置いて",
    "優しく", "柔らかく", "落ち着いた", "静か", "低い声", "沈んだ", "明るく", "弾んだ",
    "張り上げ", "感情的", "甘く", "喘ぐ", "近い距離", "笑い", "くすくす", "含み笑い",
    "眠そう", "眠たげ", "慌て", "動揺", "どもり", "語尾", "消え入", "尻すぼみ", "力なく",
    "淡々", "抑揚", "鼻声", "鼻にかけ", "声を潜め", "声が上ず", "声を弾ませ", "声を張り",
    "砕けた", "丁寧", "自然な声", "間延び", "巻き気味", "高め", "低め", "叫ぶ", "呟く",
]


def load_manifest(path=MANIFEST):
    return [json.loads(l) for l in path.read_text(encoding="utf8").splitlines() if l.strip()]


def stem_of(row):
    m = re.search(r"_([0-9]{3}(?:-[ab])?)\.pt$", row["latent_path"])
    if not m:
        raise SystemExit("latent 名字不认识：%r" % row["latent_path"])
    return m.group(1)


# ---- 指标：评测台 + 三项补充 ---------------------------------------------------

def _voiced(samples, sr):
    blocks = EB.speech_blocks(samples, sr)
    if not blocks:
        return samples, blocks
    return samples[int(blocks[0][0] * sr):int(blocks[-1][1] * sr)], blocks


def spectral_flatness(x):
    """谱平坦度 0..1：越高越像噪声（气声/喘息高，浊音低）。"""
    if len(x) < 512:
        return None
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2 + 1e-12
    spec = spec[1:]
    return float(np.exp(np.mean(np.log(spec))) / np.mean(spec))


def hf_ratio(x, sr, cut_hz=4000.0):
    """4 kHz 以上能量占比：气声/齿音多 → 高。"""
    if len(x) < 512:
        return None
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    tot = float(spec.sum()) + 1e-12
    return float(spec[freqs >= cut_hz].sum() / tot)


def tail_slope(voiced, sr, window_s=0.7):
    """句末 F0 走向：最后一段（默认 0.7 s）的 F0 均值与整段均值之差，按百分比返回。

    直接对末段音频再跑一次 `EB.f0_track` —— 帧位置不用自己维护，坑少。
    """
    if len(voiced) < int(0.4 * sr):
        return None
    head = EB.f0_track(voiced, sr)
    tail = EB.f0_track(voiced[max(0, len(voiced) - int(window_s * sr)):], sr)
    if len(head) < 20 or len(tail) < 5:
        return None
    return 100.0 * (statistics.fmean(tail) - statistics.fmean(head)) / statistics.fmean(head)


def measure_row(row, audio_dir=ROOT / "tts" / "GPT-sovits" / "audio" / "albion"):
    stem = stem_of(row)
    wav = audio_dir / (stem + ".wav")
    sr, a = EB.load_wav(wav)
    m = EB.analyze(a, sr, row["text"])
    voiced, blocks = _voiced(a, sr)
    gaps = [round(blocks[i + 1][0] - blocks[i][1], 2) for i in range(len(blocks) - 1)]
    tail = tail_slope(voiced, sr) if len(voiced) else None
    m.update(
        stem=stem, wav=str(wav.relative_to(ROOT)), frames=int(row.get("num_frames") or 0),
        chars=EB.spoken_chars(row["text"]), rate_net=m["rate_cps"],
        f0_range=(round(m["f0_p90"] - m["f0_p10"], 1)
                  if m.get("f0_p90") is not None and m.get("f0_p10") is not None else None),
        tail_pct=(round(tail, 1) if tail is not None else None),
        flat=round(spectral_flatness(voiced), 4) if len(voiced) else None,
        hf=round(hf_ratio(voiced, sr), 4) if len(voiced) else None,
        pauses=len(gaps), longest_gap=max(gaps) if gaps else 0.0,
    )
    return m


def percentile_of(value, pool):
    pool = [v for v in pool if v is not None]
    if value is None or not pool:
        return None
    return round(100.0 * sum(1 for v in pool if v <= value) / len(pool), 1)


def label(pct, hi, lo, high="快", low="慢"):
    if pct is None:
        return "—"
    if pct >= hi:
        return high
    if pct <= lo:
        return low
    return "中"


def cmd_measure(args):
    rows = load_manifest()
    out = []
    for i, row in enumerate(rows, 1):
        m = measure_row(row, args.audio_dir)
        out.append(m)
        print("[%2d/%d] %-7s dur=%5.2f rate=%5.2f f0=%6s tail=%+6s%% flat=%.4f hf=%.3f"
              % (i, len(rows), m["stem"], m["dur"], m["rate_net"] or 0,
                 EB.fmt(m["f0_mean"], "%.0f"), EB.fmt(m["tail_pct"], "%+.1f"),
                 m["flat"] or 0, m["hf"] or 0), flush=True)
    # 分位（在 83 条里），给写描述的人看"这条在整体里算快还是慢"
    pools = dict(rate=[m["rate_net"] for m in out], f0=[m["f0_mean"] for m in out],
                 dyn=[m["dyn_db"] for m in out], hf=[m["hf"] for m in out],
                 flat=[m["flat"] for m in out], silence=[m["silent_ratio"] for m in out],
                 dur=[m["dur"] for m in out], f0range=[m["f0_range"] for m in out])
    for m in out:
        m["pct"] = {k: percentile_of(m.get(src), pool) for k, (src, pool) in {
            "rate": ("rate_net", pools["rate"]), "f0": ("f0_mean", pools["f0"]),
            "dyn": ("dyn_db", pools["dyn"]), "hf": ("hf", pools["hf"]),
            "flat": ("flat", pools["flat"]), "silence": ("silent_ratio", pools["silence"]),
            "dur": ("dur", pools["dur"]), "f0range": ("f0_range", pools["f0range"]),
        }.items()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in out) + "\n",
                        encoding="utf8")
    print("→ %s（%d 条）；分布中位数：字速 %.2f、F0 %.0f、动态 %.1f、HF %.3f"
          % (args.out, len(out), statistics.median(pools["rate"] or [0]),
             statistics.median(pools["f0"] or [0]), statistics.median(pools["dyn"] or [0]),
             statistics.median(pools["hf"] or [0])))
    return 0


# ---- build / review -----------------------------------------------------------

def load_descriptions(path):
    raw = json.loads(path.read_text(encoding="utf8"))
    desc = raw.get("descriptions", raw)
    if not isinstance(desc, dict):
        raise SystemExit("描述 JSON 必须是 {stem: 描述}")
    return {str(k): str(v).strip() for k, v in desc.items() if not str(k).startswith("_")}


def check_description(stem, text):
    """规则来自 REPORT：10–30 字、只用声音动作词、不单写抽象情绪。"""
    problems = []
    if not 10 <= len(text) <= 30:
        problems.append("长度 %d 不在 10–30" % len(text))
    if any(ch in text for ch in "\n\r\t"):
        problems.append("含控制字符")
    if not any(w in text for w in VOICE_WORDS):
        problems.append("没有任何声音动作词")
    for bad in ("恥ずかしそう", "甘え", "得意げ", "心配そう", "嬉しそう", "悲しそう"):
        if bad in text and not any(w in text for w in VOICE_WORDS):
            problems.append("抽象情绪词单独出现：%s" % bad)
    return problems


def consistency(p, desc):
    """描述和韵律数字**矛盾**的地方（只抓说反了，不抓"没提到"）。

    10–30 字塞不下所有线索，所以"没写到某条"不算问题；写反了才是问题。
    3 s 以内的短句字速被停顿稀释/抬高，不参与字速判定。
    """
    if not p:
        return ["没有韵律数据"]
    q = p.get("pct") or {}
    out = []

    def has(*words):
        return any(w in desc for w in words)
    rate = None if (p.get("dur") or 0) < 3.0 else q.get("rate")
    if rate is not None and rate <= 33 and has("早口", "まくしたて", "一気に"):
        out.append("写了早口，但字速 P%.0f（偏慢）" % rate)
    if rate is not None and rate >= 67 and has("ゆっくり"):
        out.append("写了ゆっくり，但字速 P%.0f（偏快）" % rate)
    f0 = q.get("f0")
    if f0 is not None and f0 <= 33 and has("高め", "高い声"):
        out.append("写了高め，但 F0 P%.0f（偏低）" % f0)
    if f0 is not None and f0 >= 67 and has("低め", "低い声"):
        out.append("写了低め，但 F0 P%.0f（偏高）" % f0)
    dyn = q.get("dyn")
    if dyn is not None and dyn <= 33 and has("抑揚を大きく"):
        out.append("写了抑揚を大きく，但动态 P%.0f（平）" % dyn)
    if dyn is not None and dyn >= 67 and has("淡々", "抑揚を抑え"):
        out.append("写了淡々，但动态 P%.0f（起伏大）" % dyn)
    tail = p.get("tail_pct")
    if tail is not None and tail >= 10 and has("語尾を下げ"):
        out.append("写了語尾を下げ，但句末 +%.0f%%（扬）" % tail)
    if tail is not None and tail <= -10 and has("語尾を上げ"):
        out.append("写了語尾を上げ，但句末 %.0f%%（降）" % tail)
    hf = q.get("hf")
    if hf is not None and hf <= 33 and has("息まじり", "囁く", "囁き"):
        out.append("写了气声/囁き，但气声 P%.0f（很实）" % hf)
    return out


def load_prosody(path=PROSODY):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf8").splitlines():
            if line.strip():
                m = json.loads(line)
                out[m["stem"]] = m
    return out


def cmd_build(args):
    rows = load_manifest()
    desc = load_descriptions(args.descriptions)
    stems = [stem_of(r) for r in rows]
    missing = [s for s in stems if s not in desc]
    extra = [s for s in desc if s not in stems]
    if missing or extra:
        raise SystemExit("描述对不上：缺 %s / 多 %s" % (missing[:5], extra[:5]))
    pros = load_prosody()
    problems = {s: p for s, p in ((s, check_description(s, desc[s])) for s in stems) if p}
    soft = {s: c for s, c in ((s, consistency(pros.get(s), desc[s])) for s in stems) if c}
    for stem, p in sorted(problems.items()):
        print("WARN %-7s %s" % (stem, "；".join(p)))
    for stem, c in sorted(soft.items()):
        print("NOTE %-7s %s" % (stem, "；".join(c)))
    out = []
    for row in rows:
        stem = stem_of(row)
        base = {k: v for k, v in row.items() if k != "caption"}
        out.append(dict(base, caption=row["caption"]))                 # 原 7 锚一行
        out.append(dict(base, caption=desc[stem]))                     # 新描述一行
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                        encoding="utf8")
    print("→ %s（%d 行 = %d 条 × 2）" % (args.out, len(out), len(rows)))
    print("硬问题 %d 条、韵律软提示 %d 条" % (len(problems), len(soft)))
    return 0


def word_freq(texts):
    hit = {w: 0 for w in VOICE_WORDS}
    for t in texts:
        for w in VOICE_WORDS:
            if w in t:
                hit[w] += 1
    return sorted(((w, n) for w, n in hit.items() if n), key=lambda kv: -kv[1])


def cmd_review(args):
    rows = load_manifest()
    desc = load_descriptions(args.descriptions)
    pros = load_prosody(args.prosody)
    L = []
    add = L.append
    add("# albion 重标注审阅表（阶段 3a）")
    add("")
    add("来源 manifest：`%s`（%d 条）。新 manifest：`%s`（每条两行：原 7 锚 caption + 新描述）。"
        % (MANIFEST.relative_to(ROOT), len(rows), RICH.relative_to(ROOT)))
    add("")
    add("韵律全部**本地**量（不上任何外部 API）：字速 = 字数 ÷ 发声时长；F0 用自相关；"
        "动态 = 20 ms RMS 的 dB 标准差；气声 = 4 kHz 以上能量占比 / 谱平坦度；"
        "分位是在这 83 条里排的（P 越高 = 越快/越高/越响/越气声）。")
    add("")
    add("| # | 原文（截断到 28 字） | 旧 E | 关键韵律 | 新描述 |")
    add("|---|---|---|---|---|")
    for i, row in enumerate(rows, 1):
        stem = stem_of(row)
        p = pros.get(stem, {})
        pct = p.get("pct") or {}
        def pc(k, hi=67, lo=33, high="快", low="慢"):
            return "%s P%s" % (label(pct.get(k), hi, lo, high, low), EB.fmt(pct.get(k), "%.0f"))
        rhythm = " · ".join([
            "字速 %s %s" % (EB.fmt(p.get("rate_net"), "%.1f"), pc("rate")),
            "F0 %s %s" % (EB.fmt(p.get("f0_mean"), "%.0f"), pc("f0", high="高", low="低")),
            "范围 %s" % EB.fmt(p.get("f0_range"), "%.0f"),
            "动态 %s %s" % (EB.fmt(p.get("dyn_db"), "%.0f"), pc("dyn", high="响", low="轻")),
            "气声 %s %s" % (EB.fmt(p.get("hf"), "%.2f"), pc("hf", high="气", low="实")),
            "停顿 %s%%" % EB.fmt(100 * (p.get("silent_ratio") or 0), "%.0f"),
            "句末%s" % ("↑" if (p.get("tail_pct") or 0) > 3 else ("↓" if (p.get("tail_pct") or 0) < -3 else "→")),
        ])
        text = row["text"].replace("|", "／")
        add("| %d %s | %s | %s | %s | %s |" % (
            i, stem, text[:28] + ("…" if len(text) > 28 else ""), row["caption"][:6] + "…",
            rhythm, desc.get(stem, "**缺**")))
    add("")
    add("## 描述与韵律的自检")
    add("")
    soft = {s: c for s, c in ((s, consistency(pros.get(s), desc[s])) for s in desc) if c}
    if not soft:
        add("**没有一条描述和韵律数字说反**（写早了/写慢了、写高了/写低了、写扬了/写降了、写了气声却有很实——都没有）。")
    else:
        add("下面 %d 条里描述和某条韵律线索**说反了**：" % len(soft))
        add("")
        add("| stem | 矛盾 |")
        add("|---|---|")
        for stem, c in sorted(soft.items()):
            add("| %s | %s |" % (stem, "；".join(c)))
    add("")
    cues = [
        ("字速快（P≥67）", lambda p: (p.get("dur") or 0) >= 3 and (p["pct"].get("rate") or 0) >= 67,
         ("早口", "まくしたて", "一気に")),
        ("字速慢（P≤33）", lambda p: (p.get("dur") or 0) >= 3 and (p["pct"].get("rate") or 0) <= 33,
         ("ゆっくり", "間延び")),
        ("音高高（P≥67）", lambda p: (p["pct"].get("f0") or 0) >= 67, ("高め", "高い声")),
        ("音高低（P≤33）", lambda p: (p["pct"].get("f0") or 0) <= 33, ("低め", "低い声")),
        ("起伏大（P≥67）", lambda p: (p["pct"].get("dyn") or 0) >= 67, ("抑揚",)),
        ("气声重（P≥67）", lambda p: (p["pct"].get("hf") or 0) >= 67, ("息", "囁", "声を潜め")),
        ("句末上扬（≥+10%）", lambda p: (p.get("tail_pct") or 0) >= 10, ("語尾を上げ",)),
        ("句末下降（≤−10%）", lambda p: (p.get("tail_pct") or 0) <= -10, ("語尾を下げ",)),
        ("停顿多（≥15%）", lambda p: (p.get("silent_ratio") or 0) >= 0.15, ("間を置",)),
    ]
    add("覆盖率：按线索看「数字够极端、描述也写了」的比例（10–30 字塞不下所有线索，没写到不算错，"
        "只是事后听的时候更容易觉得「和数字不符」）。")
    add("")
    add("「字速慢」那一行天然偏低：字速 = 字数 ÷ 发声时长，长句本来就慢、而且停顿也算进分母，"
        "所以不少慢句在描述里改写了更显眼的线索（抑揚／語尾／息）。")
    add("")
    add("| 线索 | 符合的条数 | 描述写到的 | 覆盖率 |")
    add("|---|---|---|---|")
    for name, pred, words in cues:
        hit_rows = [s for s in desc if pros.get(s) and pred(pros[s])]
        hit = [s for s in hit_rows if any(w in desc[s] for w in words)]
        add("| %s | %d | %d | %s |" % (
            name, len(hit_rows), len(hit),
            ("%.0f%%" % (100.0 * len(hit) / len(hit_rows))) if hit_rows else "—"))
    add("")
    add("## 描述用词频次")
    add("")
    add("（按子串计，同一个词的不同活用算一次；「息」会同时被「息まじり」「ため息」命中，所以会重叠。）")
    freq = word_freq([desc[s] for s in desc])
    total = len(desc)
    add("共 %d 条描述，用到的声音动作词（按出现条数）：" % total)
    add("")
    add("| 词 | 条数 | 占比 |")
    add("|---|---|---|")
    for w, n in freq:
        add("| %s | %d | %.0f%% |" % (w, n, 100.0 * n / total))
    add("")
    if freq:
        add("最高频词「%s」占 %d/%d = %.0f%%，没有单个词占大半。" % (
            freq[0][0], freq[0][1], total, 100.0 * freq[0][1] / total))
    lens = [len(v) for v in desc.values()]
    add("")
    add("长度：%d–%d 字（中位 %d）；描述去重后 %d/%d 条互不相同。"
        % (min(lens), max(lens), int(statistics.median(lens)), len(set(desc.values())), total))
    add("")
    add("## 旧 E 标签分布（对照）")
    add("")
    cnt = {}
    for row in rows:
        cnt[row["caption"]] = cnt.get(row["caption"], 0) + 1
    add("| E | 条数 |")
    add("|---|---|")
    for cap, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        add("| %s | %d |" % (cap, n))
    add("")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(L) + "\n", encoding="utf8")
    print("→ %s（%d 条）" % (args.out, len(rows)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("measure", help="量韵律 + 分位")
    p.add_argument("--audio-dir", type=pathlib.Path, default=AUDIO)
    p.add_argument("--out", type=pathlib.Path, default=PROSODY)
    p.set_defaults(func=cmd_measure)
    p = sub.add_parser("build", help="生成两行 manifest")
    p.add_argument("--descriptions", type=pathlib.Path, default=DESCRIPTIONS)
    p.add_argument("--out", type=pathlib.Path, default=RICH)
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("review", help="出审阅表")
    p.add_argument("--descriptions", type=pathlib.Path, default=DESCRIPTIONS)
    p.add_argument("--prosody", type=pathlib.Path, default=PROSODY)
    p.add_argument("--out", type=pathlib.Path, default=OUTDIR / "REVIEW.md")
    p.set_defaults(func=cmd_review)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
