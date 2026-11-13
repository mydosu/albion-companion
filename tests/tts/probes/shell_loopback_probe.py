"""壳输出对照探针（一次性排查用，未提交）。

两条路径各播同一批 tts-recent wav，同时用 WASAPI loopback 录默认输出设备：
  baseline —— Python(soundcard) 直接播到默认扬声器
  shell    —— 经 CDP 在壳渲染进程里 decodeAudioData + AudioContext 排程播放

用法：
  python shell_loopback_probe.py baseline <outdir>
  python shell_loopback_probe.py shell <outdir> [--cdp 9335]   # 渲染进程里新建 AudioContext
  python shell_loopback_probe.py live <outdir> [--cdp 9335]    # 壳正在用的那个 AudioContext
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundcard as sc
import soundfile as sf

SR = 48000
GAP = 0.10
WAVS = [
    r"D:\AI\Wife\logs\tts-recent\sa9483fc3291f_sa9483fc3291f_1_0_0.wav",
    r"D:\AI\Wife\logs\tts-recent\sa9483fc3291f_sa9483fc3291f_1_1_0.wav",
    r"D:\AI\Wife\logs\tts-recent\sa9483fc3291f_sa9483fc3291f_1_2_1.wav",
    r"D:\AI\Wife\logs\tts-recent\s30ce7859f920_s30ce7859f920_1_2_1.wav",
]
HTTP_PORT = 8899


def load(p: str) -> np.ndarray:
    x, sr = sf.read(p, dtype="float32", always_2d=True)
    if sr != SR:
        raise SystemExit(f"{p}: expect {SR}Hz, got {sr}")
    mono = x.mean(axis=1, keepdims=True)
    return np.repeat(mono, 2, axis=1)


class Recorder:
    def __init__(self) -> None:
        self.speaker = sc.default_speaker()
        self.mic = sc.get_microphone(id=str(self.speaker.name), include_loopback=True)
        self.chunks: list[np.ndarray] = []
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    def _run(self) -> None:
        with self.mic.recorder(samplerate=SR, channels=2, blocksize=1024) as rec:
            while not self.stop_flag.is_set():
                self.chunks.append(rec.record(numframes=1024))

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        time.sleep(0.4)

    def stop(self) -> np.ndarray:
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=3)
        return np.concatenate(self.chunks, axis=0) if self.chunks else np.zeros((0, 2), np.float32)


def serve_wavs(port: int) -> ThreadingHTTPServer:
    root = str(Path(WAVS[0]).parent)

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


JS = r"""
(async (urls, gap, live) => {
  // live=true：用壳里那个长期存活的 AudioContext（pinia 'audio-context'，Stage.vue 播 TTS 用的就是它）
  const ctx = live
    ? document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('audio-context').audioContext
    : new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' });
  await ctx.resume();
  const outs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audiooutput');
  const want = live ? null : outs.find(d => /Realtek/i.test(d.label));
  let sink = null;
  if (want && typeof ctx.setSinkId === 'function') {
    try { await ctx.setSinkId(want.deviceId); sink = want.label; } catch (e) { sink = 'setSinkId failed: ' + e.message; }
  }
  const bufs = [];
  for (const u of urls) {
    const r = await fetch(u, { cache: 'no-store' });
    if (!r.ok) throw new Error('fetch ' + u + ' ' + r.status);
    bufs.push(await ctx.decodeAudioData(await r.arrayBuffer()));
  }
  const t0 = ctx.currentTime + 0.35;
  const ct0 = ctx.currentTime;
  let t = t0;
  const spans = [];
  for (const b of bufs) {
    const src = ctx.createBufferSource();
    const g = ctx.createGain();
    g.gain.value = 1;
    src.buffer = b;
    src.connect(g).connect(ctx.destination);
    src.start(t);
    spans.push({ dur: b.duration, sr: b.sampleRate, start: t });
    t += b.duration + gap;
  }
  const wait = (t - t0 + 0.9) * 1000;
  await new Promise(res => setTimeout(res, wait));
  const info = {
    state: ctx.state,
    sampleRate: ctx.sampleRate,
    baseLatency: ctx.baseLatency,
    outputLatency: ctx.outputLatency,
    sinkId: (typeof ctx.sinkId === 'string' ? ctx.sinkId : null),
    sinkLabel: sink,
    ctxTimeAtSchedule: ct0,
    ctxTimeAfter: ctx.currentTime,
    outputs: outs.map(d => d.label),
    live,
    spans,
    total: t - t0,
  };
  return JSON.stringify(info);
})
"""


NODE_CDP = r"""
const [port, file, ms] = process.argv.slice(1);
const js = require('fs').readFileSync(file, 'utf8');
(async () => {
  const ts = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = ts.find(t => t.type === 'page' && t.url.includes('synced-leader=true'));
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: js, awaitPromise: true, returnByValue: true } }));
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id !== 1) return;
    const r = m.result; if (r.exceptionDetails) { console.error(JSON.stringify(r.exceptionDetails).slice(0, 600)); process.exit(2) }
    process.stdout.write(r.result.value); process.exit(0) };
  setTimeout(() => { console.error('CDP timeout'); process.exit(3) }, Number(ms));
})();
"""


def cdp_eval(port: int, js: str, await_ms: int) -> dict:
    """CDP 走 node（22+ 自带 WebSocket），免得 python 侧再装 websocket-client。"""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
    r = subprocess.run(["node", "-e", NODE_CDP, str(port), f.name, str(await_ms)],
                       capture_output=True, text=True, encoding="utf-8", timeout=await_ms / 1000 + 10)
    Path(f.name).unlink(missing_ok=True)
    if r.returncode != 0:
        raise SystemExit(f"CDP failed: {r.stderr[:600]}")
    return json.loads(r.stdout)


def main() -> None:
    mode = sys.argv[1]
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    cdp_port = 9335
    if "--cdp" in sys.argv:
        cdp_port = int(sys.argv[sys.argv.index("--cdp") + 1])

    data = [load(p) for p in WAVS]
    total = sum(len(d) for d in data) / SR + GAP * (len(data) - 1)

    rec = Recorder()
    print(f"[{mode}] speaker={rec.speaker.name!r} loopback={rec.mic.name!r} total={total:.2f}s")
    rec.start()
    t_start = time.time()

    if mode == "baseline":
        for i, d in enumerate(data):
            rec.speaker.play(d, samplerate=SR, blocksize=2048)
            if i != len(data) - 1:
                time.sleep(GAP)
        info = {"mode": "baseline", "player": "python-soundcard"}
    else:
        serve_wavs(HTTP_PORT)
        urls = [f"http://127.0.0.1:{HTTP_PORT}/{Path(p).name}" for p in WAVS]
        live = mode == "live"
        info = cdp_eval(cdp_port, f"({JS})({json.dumps(urls)}, {GAP}, {str(live).lower()})", int((total + 8) * 1000))
        info["mode"] = mode

    time.sleep(0.8)
    audio = rec.stop()
    elapsed = time.time() - t_start
    sf.write(str(out / f"{mode}.wav"), audio, SR, subtype="PCM_24")
    meta = {
        "mode": mode,
        "speaker": rec.speaker.name,
        "loopback": rec.mic.name,
        "wavs": [Path(p).name for p in WAVS],
        "gap_s": GAP,
        "planned_s": total,
        "recorded_s": len(audio) / SR,
        "wall_s": elapsed,
        "info": info,
    }
    (out / f"{mode}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

