# -*- coding: utf-8 -*-
"""把「音频目录 + 台词表」整理成 HuggingFace `audiofolder` 数据集（喂 prepare_manifest.py 用）。

只用标准库（wave）。不装 numpy/soundfile：系统 Python 的 numpy 在部分机器上是坏的。
非 wav（mp3 等）自动走 PATH 里的 ffmpeg 转成 wav；没有 ffmpeg 就报错并列出文件名。

台词表 csv/json，列：file（文件名，相对音频目录，可省扩展名）, text；可选 start, end（秒，切片段用）。
  csv  : 带表头 file,text[,start,end]，UTF-8（带 BOM 也行）
  json : [{"file":..,"text":..}, ...] 或 {"文件名": "文本", ...}
同一个文件出现多行（各带 start/end）= 一段长音频切成多条。

输出（--out）：
  <out>/train/<id>.wav            16-bit 单声道，重采样到 --sr（默认 24000）
  <out>/train/metadata.csv        file_name,text    ← audiofolder 约定，load_dataset("audiofolder", data_dir=<out>) 即可读
  <out>/report.json               条数、总时长、被跳过的条目及原因

之后：
  python tts/irodori/prepare_manifest.py --dataset audiofolder --data-dir <out> --split train \
      --audio-column audio --text-column text --output-manifest ... --latent-dir ...
  （参数以 prepare_manifest.py --help 为准）

用法：
  python tools/voice_data/build_dataset.py --audio-dir 音频目录 --lines 台词表.csv --out 输出目录
  python tools/voice_data/build_dataset.py --selftest      # 合成小样本自检，只写 tests/output/
"""
import argparse
import csv
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array

ROOT = pathlib.Path(__file__).resolve().parents[2]


def read_lines(path):
    path = pathlib.Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data = [{"file": k, "text": v} for k, v in data.items()]
        return data
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_wav(src, tmp):
    """非 wav 先转；wav 原样返回。"""
    if src.suffix.lower() == ".wav":
        return src
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("非 wav 需要 ffmpeg（PATH 里没有）")
    dst = pathlib.Path(tmp) / (src.stem + ".wav")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-acodec", "pcm_s16le", str(dst)], check=True)
    return dst


def read_mono16(path):
    with wave.open(str(path), "rb") as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError("只支持 16-bit PCM wav（%d-bit）；先用 ffmpeg 转" % (sw * 8))
    s = array("h")
    s.frombytes(raw)
    if sys.byteorder == "big":
        s.byteswap()
    if ch > 1:
        s = array("h", (sum(s[i:i + ch]) // ch for i in range(0, len(s) - ch + 1, ch)))
    return s, sr


def resample(s, sr, to):
    """线性插值。训练数据用，够用；要更高质量请先用 ffmpeg 离线重采样。"""
    if sr == to or not s:
        return s
    n = int(len(s) * to / sr)
    out = array("h")
    step = sr / to
    for i in range(n):
        p = i * step
        j = int(p)
        f = p - j
        a = s[j]
        b = s[j + 1] if j + 1 < len(s) else a
        out.append(int(a + (b - a) * f))
    return out


def trim_silence(s, sr, thresh=300, pad=0.05):
    """去掉首尾低于 thresh（16-bit 幅值）的静音，各留 pad 秒。"""
    idx = [i for i, v in enumerate(s) if abs(v) >= thresh]
    if not idx:
        return s
    p = int(pad * sr)
    return s[max(0, idx[0] - p):idx[-1] + 1 + p]


def write_wav(path, s, sr):
    if sys.byteorder == "big":
        s = array("h", s)
        s.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(s.tobytes())


def build(audio_dir, lines, out, sr=24000, min_sec=0.5, max_sec=30.0, trim=True):
    audio_dir, out = pathlib.Path(audio_dir), pathlib.Path(out)
    tdir = out / "train"
    tdir.mkdir(parents=True, exist_ok=True)
    rows, skipped, total = [], [], 0.0
    cache = {}
    with tempfile.TemporaryDirectory() as tmp:
        for k, ln in enumerate(lines):
            name, text = str(ln.get("file") or "").strip(), str(ln.get("text") or "").strip()
            if not name or not text:
                skipped.append({"row": k, "file": name, "why": "缺 file 或 text"})
                continue
            src = audio_dir / name
            if not src.is_file():
                hits = [p for p in audio_dir.glob(name + ".*")]
                src = hits[0] if hits else src
            if not src.is_file():
                skipped.append({"row": k, "file": name, "why": "音频不存在"})
                continue
            try:
                if src not in cache:
                    cache[src] = read_mono16(to_wav(src, tmp))
                s, in_sr = cache[src]
                a = float(ln["start"]) if ln.get("start") not in (None, "") else 0.0
                b = float(ln["end"]) if ln.get("end") not in (None, "") else len(s) / in_sr
                s = s[int(a * in_sr):int(b * in_sr)]
                s = resample(s, in_sr, sr)
                if trim:
                    s = trim_silence(s, sr)
            except Exception as e:  # noqa: BLE001 - 坏文件记下来继续
                skipped.append({"row": k, "file": name, "why": "%s: %s" % (type(e).__name__, e)})
                continue
            dur = len(s) / sr
            if not (min_sec <= dur <= max_sec):
                skipped.append({"row": k, "file": name, "why": "时长 %.2fs 不在 [%.1f, %.1f]" % (dur, min_sec, max_sec)})
                continue
            rid = "%04d_%s" % (len(rows), pathlib.Path(name).stem)
            write_wav(tdir / (rid + ".wav"), s, sr)
            rows.append((rid + ".wav", text))
            total += dur
    with open(tdir / "metadata.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file_name", "text"])
        w.writerows(rows)
    rep = {"count": len(rows), "seconds": round(total, 2), "sr": sr, "skipped": skipped}
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def selftest():
    import math
    import random
    base = ROOT / "tests" / "output" / "voice_data_selftest"
    shutil.rmtree(base, ignore_errors=True)
    aud = base / "audio"
    aud.mkdir(parents=True)

    def tone(path, sr, sec, hz, ch=1, lead=0.3):
        s = array("h")
        for i in range(int(sr * sec)):
            v = int(8000 * math.sin(2 * math.pi * hz * i / sr)) if i > lead * sr and i < (sec - lead) * sr else 0
            s.extend([v] * ch)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(s.tobytes())

    random.seed(0)
    tone(aud / "a.wav", 44100, 2.0, 220)
    tone(aud / "b.wav", 16000, 1.5, 330, ch=2)
    tone(aud / "long.wav", 22050, 6.0, 440, lead=0.1)
    lines_path = base / "lines.csv"
    lines_path.write_text("file,text,start,end\na,こんにちは。,,\nb.wav,指揮官、おはようございます。,,\nlong,前半。,0,2.5\nlong,後半。,3,5.5\n"
                          "missing.wav,ない,,\nlong,空,,\n", encoding="utf-8-sig")
    lines = read_lines(lines_path)
    lines[-1]["text"] = ""   # 缺 text → 跳过
    rep = build(aud, lines, base / "ds", sr=24000)
    assert rep["count"] == 4, rep
    assert len(rep["skipped"]) == 2, rep
    meta = list(csv.DictReader(open(base / "ds" / "train" / "metadata.csv", encoding="utf-8", newline="")))
    assert [m["text"] for m in meta] == ["こんにちは。", "指揮官、おはようございます。", "前半。", "後半。"], meta
    for m in meta:
        with wave.open(str(base / "ds" / "train" / m["file_name"])) as w:
            assert w.getframerate() == 24000 and w.getnchannels() == 1
            assert 0.5 < w.getnframes() / 24000 < 2.6, w.getnframes()
    print("selftest ok:", {k: rep[k] for k in ("count", "seconds")}, "->", base / "ds")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio-dir")
    ap.add_argument("--lines")
    ap.add_argument("--out")
    ap.add_argument("--sr", type=int, default=24000)
    ap.add_argument("--min-sec", type=float, default=0.5)
    ap.add_argument("--max-sec", type=float, default=30.0)
    ap.add_argument("--no-trim", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.audio_dir and a.lines and a.out):
        ap.error("需要 --audio-dir --lines --out（或 --selftest）")
    rep = build(a.audio_dir, read_lines(a.lines), a.out, a.sr, a.min_sec, a.max_sec, not a.no_trim)
    print("条数 %d，总时长 %.1fs，跳过 %d（见 report.json）" % (rep["count"], rep["seconds"], len(rep["skipped"])))


if __name__ == "__main__":
    main()
