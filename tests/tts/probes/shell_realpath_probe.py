"""壳真实流式路径的输出对照（句尾截断排查，2026-11-13）。

和 shell_loopback_probe.py 的区别：那边是在壳里**自己** decode + 排程；这里经 CDP 发聊天钩子
（emitBeforeMessageComposed / TokenLiteral / StreamEnd / AssistantResponseEnd），让 Stage.vue
走**生产那条**流式 TTS：9885 合成 → 壳解码 → 时钟排程 → connectPlaybackSource。
不经脑、不写聊天记录（这些钩子只有 Stage / albion-pet / 跨窗字幕转发在听）。

同时 WASAPI loopback 录默认输出，结束后把本轮新落到 logs/tts-recent/ 的 wav 逐段在录音里找位置，
比尾部缺失和段内断音（口径同 shell_loopback_analyze.py）。

用法：python shell_realpath_probe.py <outdir> [--cdp 9335] [--secs 60] [--text "..."]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_loopback_analyze import FR, SR, db, env  # noqa: E402
from shell_loopback_probe import NODE_CDP, Recorder  # noqa: E402

RECENT = Path(r"D:\AI\Wife\logs\tts-recent")
TEXT = "今日はとても静かな夜ですね。少しだけ、お話ししませんか。お茶を淹れておきますわ。"

TRIGGER = r"""
(async (text, turnId) => {
  const chat = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('chat');
  const message = { role: 'user', content: '', id: turnId, createdAt: Date.now() };
  const ctx = { turnId, message, contexts: {}, composedMessage: [] };
  await chat.emitBeforeMessageComposedHooks('', ctx);
  for (const ch of text.match(/[^。！？]+[。！？]?/g)) {
    await chat.emitTokenLiteralHooks(ch, ctx);
    await new Promise(r => setTimeout(r, 300));
  }
  await chat.emitStreamEndHooks(ctx);
  await chat.emitAssistantResponseEndHooks(text, ctx);
  return JSON.stringify({ ok: true, turnId });
})
"""


def cdp(js: str, ms: int = 30000) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    r = subprocess.run(["node", "-e", NODE_CDP, "9335", f.name, str(ms)], capture_output=True,
                       text=True, encoding="utf-8", timeout=ms / 1000 + 10)
    Path(f.name).unlink(missing_ok=True)
    if r.returncode != 0:
        raise SystemExit(f"CDP failed: {r.stderr[:600]}")
    return r.stdout


def mono(p: Path) -> np.ndarray:
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    assert sr == SR, (p, sr)
    return x.mean(axis=1)


def analyze(rec: np.ndarray, wavs: list[Path]) -> list[dict]:
    er = env(rec)
    out = []
    for p in wavs:
        s = mono(p)
        es = env(s)
        g = signal.correlate(er, es, mode="valid")
        norms = np.sqrt(np.convolve(er ** 2, np.ones(len(es)), mode="valid")) * np.linalg.norm(es) + 1e-12
        c = g / norms
        a = int(np.argmax(c))
        w = er[a:a + len(es)]
        odb, rdb = db(es), db(w)
        peak = float(odb.max())
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
        lvl = float(np.median(rdb[mask] - odb[mask])) if mask.any() else 0.0
        # 句尾 300ms（原音 voiced 结束前）的能量差，扣掉整体电平差：负得多 = 尾巴被压/被吃
        k0 = max(0, int(o_end / 0.01) - 30)
        k1 = int(o_end / 0.01)
        tail_db = float(np.mean(rdb[k0:k1] - odb[k0:k1]) - lvl) if k1 > k0 else 0.0
        out.append({
            "wav": p.name, "dur": round(len(s) / SR, 2), "rec_at_s": round(a * 0.01, 2),
            "corr": round(float(c[a]), 3), "peak_db": round(peak, 1),
            "orig_voiced_end_s": round(o_end, 2), "rec_voiced_end_s": round(r_end, 2),
            "tail_lost_ms": round((o_end - r_end) * 1000), "level_diff_db": round(lvl, 1),
            "tail300_rel_db": round(tail_db, 1), "holes": holes,
        })
    return out


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    secs = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 60
    text = sys.argv[sys.argv.index("--text") + 1] if "--text" in sys.argv else TEXT
    before = {p.name for p in RECENT.glob("*.wav")}
    turn = f"probe-tailcut-{int(time.time())}"

    rec = Recorder()
    print(f"[realpath] loopback={rec.mic.name!r} secs={secs} turn={turn}")
    rec.start()
    t0 = time.time()
    print(cdp(f"({TRIGGER})({json.dumps(text, ensure_ascii=False)}, {json.dumps(turn)})"))
    time.sleep(max(0.0, secs - (time.time() - t0)))
    audio = rec.stop()
    sf.write(str(out / "realpath.wav"), audio, SR, subtype="PCM_24")

    new = sorted((p for p in RECENT.glob("*.wav") if p.name not in before), key=lambda p: p.stat().st_mtime)
    segs = analyze(audio.mean(axis=1), new)
    meta = {"turn": turn, "text": text, "loopback": rec.mic.name, "recorded_s": round(len(audio) / SR, 2),
            "new_wavs": [p.name for p in new], "segments": segs}
    (out / "realpath.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in segs:
        print(f"  {s['wav'][-26:]:26s} dur={s['dur']:5.2f} at={s['rec_at_s']:6.2f} corr={s['corr']:.2f} "
              f"tail_lost={s['tail_lost_ms']:+5d}ms tail300={s['tail300_rel_db']:+5.1f}dB "
              f"lvl={s['level_diff_db']:+5.1f}dB holes={len(s['holes'])}/{sum(h['len_s'] for h in s['holes']):.2f}s")


if __name__ == "__main__":
    main()
