#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Play an existing TTS WAV and capture the selected Windows speaker via WASAPI loopback.

Requires ``numpy`` and ``soundcard`` in the selected Python environment, plus
ffplay on PATH. Writes the captured WAV and a compact metrics JSON beneath
tests/output. This does not call a TTS service or the production brain.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import threading
import time
import wave

import numpy as np
import soundcard as sc


ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_WAV = ROOT / "tests/output/tts-leftovers/impl-20261020/validated-live/albion.wav"
DEFAULT_OUT = ROOT / "tests/output/tts-s4/wasapi"
RATE = 48_000


def summarize(samples: np.ndarray, rate: int) -> dict:
    """Return total RMS/peak and duration above a low, fixed activity threshold."""
    if samples.ndim == 1:
        samples = samples[:, None]
    if not len(samples):
        raise ValueError("loopback capture is empty")
    mono = np.sqrt(np.mean(np.square(samples.astype(np.float64)), axis=1))
    rms = float(np.sqrt(np.mean(np.square(mono))))
    peak = float(np.max(np.abs(samples)))
    window = max(1, int(rate * 0.02))
    active = 0
    for start in range(0, len(mono), window):
        block = mono[start:start + window]
        if len(block) and float(np.sqrt(np.mean(np.square(block)))) >= 0.001:
            active += len(block)
    return {
        "capture_seconds": round(len(samples) / rate, 3),
        "active_seconds": round(active / rate, 3),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "channels": int(samples.shape[1]),
        "frames": int(len(samples)),
        "sample_rate": rate,
    }


def write_wav(path: pathlib.Path, samples: np.ndarray, rate: int) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = np.where(pcm < 0, pcm * 32768, pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(int(pcm.shape[1]) if pcm.ndim > 1 else 1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wav", type=pathlib.Path, default=DEFAULT_WAV)
    ap.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--speaker", help="exact or partial Windows speaker name; default speaker otherwise")
    args = ap.parse_args()
    source = args.wav.resolve()
    out_dir = args.out_dir.resolve()
    if not source.is_file():
        raise SystemExit(f"TTS WAV not found: {source}")
    if "tests" not in out_dir.parts or "output" not in out_dir.parts:
        raise SystemExit("--out-dir must be beneath tests/output")
    player = shutil.which("ffplay")
    if not player:
        raise SystemExit("ffplay is required on PATH")
    with wave.open(str(source), "rb") as wav:
        source_seconds = wav.getnframes() / wav.getframerate()
        if wav.getnframes() == 0:
            raise SystemExit("source WAV has no frames")

    speakers = sc.all_speakers()
    if args.speaker:
        matches = [s for s in speakers if args.speaker.casefold() in s.name.casefold()]
        if len(matches) != 1:
            raise SystemExit(f"speaker selector matched {len(matches)} devices; choose a unique name")
        speaker = matches[0]
    else:
        speaker = sc.default_speaker()
    if speaker is None:
        raise SystemExit("Windows has no default playback device")
    loopback = sc.get_microphone(id=speaker.name, include_loopback=True)

    frames = int((source_seconds + 2.0) * RATE)
    captured: list[np.ndarray] = []
    errors: list[str] = []

    def record() -> None:
        try:
            with loopback.recorder(samplerate=RATE) as recorder:
                # Start playback after WASAPI recording has entered its loop.
                ready.set()
                captured.append(recorder.record(numframes=frames))
        except Exception as exc:  # surfaced by the main thread without dumping device internals
            errors.append(type(exc).__name__ + ": " + str(exc))
            ready.set()

    ready = threading.Event()
    thread = threading.Thread(target=record, name="wasapi-loopback", daemon=True)
    thread.start()
    if not ready.wait(10):
        raise SystemExit("WASAPI loopback recorder did not become ready in 10 seconds")
    started = time.monotonic()
    proc = subprocess.Popen([player, "-nodisp", "-autoexit", "-loglevel", "error", str(source)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    stderr = proc.communicate(timeout=source_seconds + 30)[1]
    thread.join(timeout=10)
    if proc.returncode:
        raise SystemExit(f"ffplay failed ({proc.returncode}): {stderr[-500:]}")
    if thread.is_alive():
        raise SystemExit("WASAPI loopback recorder did not finish")
    if errors:
        raise SystemExit("WASAPI loopback failed: " + errors[0])
    if len(captured) != 1:
        raise SystemExit("WASAPI loopback produced no single capture buffer")

    metrics = summarize(captured[0], RATE)
    metrics.update({
        "source": str(source),
        "source_seconds": round(source_seconds, 3),
        "speaker_selection": "default" if not args.speaker else "explicit_name_match",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "player_exit_code": proc.returncode,
        "non_silent": metrics["rms"] >= 0.001 and metrics["peak"] >= 0.01,
        "duration_reasonable": source_seconds * 0.5 <= metrics["active_seconds"] <= source_seconds * 1.8,
    })
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "loopback.wav"
    write_wav(wav_path, captured[0], RATE)
    metrics["capture_wav"] = str(wav_path)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["non_silent"] and metrics["duration_reasonable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
