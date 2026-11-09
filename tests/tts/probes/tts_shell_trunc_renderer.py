"""Shell-side half of the tail-truncation probe (task 14d05275).

Injects `trunc-bundle.js` (built from shell_trunc_harness.ts) into the *existing* Electron main
renderer over CDP, plays the three production Japanese sentences through 9885 with the same
pipeline + playback clock the shell uses, records the output graph, and records the selected
speaker with WASAPI loopback for the same stretch.

Read-only: no production file is edited, no service restarted, nothing sent into the chat, no
chat/profile data read. Evidence goes under tests/output only.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import threading
import time
import wave

import numpy as np
import soundcard as sc
import urllib.request
import websockets

RATE = 48_000
SENTENCES = [
    "えっ……わ、私のフィギュアも指揮官の机の上に置くのですか…？",
    "先輩方に囲まれるなんて、想像するだけでも少し恥ずかしいですね…",
    "でも、指揮官のすぐお傍に置いていただけるなら、本当はすごく嬉しいです…",
]
TEXT = "".join(SENTENCES)


def write_wav(path: pathlib.Path, samples: np.ndarray, rate: int) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = np.where(pcm < 0, pcm * 32768, pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(int(pcm.shape[1]) if pcm.ndim > 1 else 1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


class Loopback:
    """Records the default speaker for a fixed number of seconds on a background thread."""

    def __init__(self, seconds: float, speaker_name: str | None = None):
        self.seconds = seconds
        self.speaker_name = speaker_name
        self.ready = threading.Event()
        self.data: np.ndarray | None = None
        self.error: str | None = None
        self.t_start = 0.0
        self.t_end = 0.0
        self._thread = threading.Thread(target=self._run, name="wasapi-loopback", daemon=True)

    def _run(self) -> None:
        try:
            speakers = sc.all_speakers()
            if self.speaker_name:
                matches = [s for s in speakers if self.speaker_name.casefold() in s.name.casefold()]
                if len(matches) != 1:
                    raise RuntimeError(f"speaker selector matched {len(matches)} devices")
                speaker = matches[0]
            else:
                speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("no default playback device")
            loop = sc.get_microphone(id=speaker.name, include_loopback=True)
            with loop.recorder(samplerate=RATE) as recorder:
                self.ready.set()
                self.t_start = time.monotonic()
                self.data = recorder.record(numframes=int(self.seconds * RATE))
                self.t_end = time.monotonic()
        except Exception as exc:               # surfaced on the main thread, no device internals
            self.error = type(exc).__name__ + ": " + str(exc)
            self.ready.set()

    def start(self) -> None:
        self._thread.start()
        if not self.ready.wait(10):
            raise SystemExit("WASAPI loopback recorder did not become ready in 10s")

    def join(self) -> None:
        self._thread.join(timeout=self.seconds + 20)


async def main(args) -> int:
    out = pathlib.Path(args.out).resolve()
    assert "tests" not in out.parts or "output" in out.parts
    out.mkdir(parents=True, exist_ok=True)
    bundle = pathlib.Path(args.bundle).read_text("utf8")
    targets = json.load(urllib.request.urlopen(args.cdp + "/json/list", timeout=3))
    pages = [x for x in targets if x.get("type") == "page" and "synced-leader=true#/" in x.get("url", "")]
    assert len(pages) == 1, f"need exactly one main renderer, found {len(pages)}"

    loop = Loopback(args.record_seconds, args.speaker)
    loop.start()
    t0 = time.monotonic()
    expression = ("(async()=>{" + bundle + ";return await ShellTruncProbe.runTruncProbe("
                  + json.dumps(args.server) + "," + json.dumps(TEXT) + ");})()")
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps(dict(id=1, method="Runtime.evaluate", params=dict(
            expression=expression, awaitPromise=True, returnByValue=True, userGesture=True))))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), 180))
            if resp.get("id") == 1:
                if "exceptionDetails" in resp.get("result", {}):
                    print(json.dumps(resp["result"]["exceptionDetails"], ensure_ascii=False)[:2000])
                    return 2
                data = resp["result"]["result"]["value"]
                break
    elapsed = round(time.monotonic() - t0, 3)
    loop.join()

    raw = data.pop("outputGraphBase64", None)
    if raw:
        (out / "shell-graph.wav").write_bytes(base64.b64decode(raw, validate=True))
    data["elapsed_s"] = elapsed
    data["graph_wav"] = "shell-graph.wav"
    (out / "shell-renderer.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    metrics = dict(error=loop.error, record_seconds=args.record_seconds,
                   started_at=t0, captured_seconds=len(loop.data) / RATE if loop.data is not None else 0)
    if loop.data is not None:
        write_wav(out / "loopback.wav", loop.data, RATE)
        mono = loop.data if loop.data.ndim == 1 else loop.data.mean(axis=1)
        metrics.update(rms=float(np.sqrt(np.mean(mono ** 2))), peak=float(np.max(np.abs(mono))))
        metrics["loopback_wav"] = "loopback.wav"
    (out / "loopback.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({k: data.get(k) for k in ("elapsed_s", "contextState", "sampleRate", "stats", "spans", "errors")},
                     ensure_ascii=False)[:4000])
    print(json.dumps(metrics, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cdp", default="http://127.0.0.1:9335")
    p.add_argument("--server", default="http://127.0.0.1:9885")
    p.add_argument("--bundle", default="tests/output/tts-shell-trunc-20261109/trunc-bundle.js")
    p.add_argument("--out", default="tests/output/tts-shell-trunc-20261109")
    p.add_argument("--record-seconds", type=float, default=45.0)
    p.add_argument("--speaker", default=None)
    raise SystemExit(asyncio.run(main(p.parse_args())))
