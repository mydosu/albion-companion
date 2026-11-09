"""Server-side half of the shell tail-truncation probe (task 14d05275).

Opens ONE v2 ws session against 9885 with the same params the shell uses, streams
the three production Japanese sentences, and records — per sentence group — every
binary frame (size + arrival time) versus the control frames, so we can tell
whether the bytes 9885 ships are already shorter than the shaped WAV on disk.

Read-only: no production code touched, no chat/profile access. Writes only under
tests/output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time
import wave

import websockets

SENTENCES = [
    "えっ……わ、私のフィギュアも指揮官の机の上に置くのですか…？",
    "先輩方に囲まれるなんて、想像するだけでも少し恥ずかしいですね…",
    "でも、指揮官のすぐお傍に置いていただけるなら、本当はすごく嬉しいです…",
]
TEXT = "".join(SENTENCES)


def wav_info(blob: bytes) -> dict:
    """Duration straight from the RIFF header (48k/mono/16-bit production shape)."""
    import io
    with wave.open(io.BytesIO(blob), "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
        return {
            "bytes": len(blob),
            "rate": rate,
            "channels": w.getnchannels(),
            "width": w.getsampwidth(),
            "frames": frames,
            "seconds": round(frames / rate, 4),
            "header_data_size": int.from_bytes(blob[40:44], "little") if len(blob) >= 44 else None,
            "riff_size": int.from_bytes(blob[4:8], "little") if len(blob) >= 8 else None,
        }


async def main(args) -> int:
    out = pathlib.Path(args.out).resolve()
    assert "tests" in out.parts and "output" in out.parts, "evidence must live under tests/output"
    out.mkdir(parents=True, exist_ok=True)
    groups: list[dict] = []
    control: list[dict] = []
    buf = b""
    cur: dict | None = None
    t0 = time.monotonic()

    async def send_text(ws):
        for i in range(0, len(TEXT), 5):
            await ws.send(json.dumps(dict(event="text", text=TEXT[i:i + 5]), ensure_ascii=False))
            await asyncio.sleep(0.08)
        await ws.send(json.dumps(dict(event="finish")))

    sender = None
    async with websockets.connect(args.ws, max_size=None) as ws:
        await ws.send(json.dumps(dict(
            event="start", model="irodori", voice="albion", response_format="wav",
            extra_body=dict(protocol_version=2, generation=1,
                            audio=dict(sample_rate=24000, bit_rate=64000)))))

        def since() -> float:
            return round(time.monotonic() - t0, 4)

        while True:
            raw = await asyncio.wait_for(ws.recv(), 180)
            if isinstance(raw, bytes):
                if cur is None:                       # binary outside a sentence group
                    groups.append(dict(unit_id=None, stray_bytes=len(raw)))
                else:
                    cur["frames"].append(dict(bytes=len(raw), at=since()))
                    buf += raw
                continue
            ev = json.loads(raw)
            name = ev.get("event")
            payload = ev.get("payload") or {}
            control.append(dict(event=name, at=since(),
                                unit_id=payload.get("unit_id"),
                                sub_index=payload.get("sub_index"),
                                text=payload.get("text")))
            if name == "session.started":
                await ws.send(json.dumps(dict(event="playback.credit", seconds=15, units=8)))
                sender = asyncio.create_task(send_text(ws))
            elif name == "sentence.start":
                assert not buf, "binary bytes leaked across sentence groups"
                cur = dict(unit_id=payload.get("unit_id"), sub_index=payload.get("sub_index"),
                           text=payload.get("text"), started_at=since(), frames=[])
            elif name == "sentence.end":
                info = wav_info(buf) if buf else dict(bytes=0, seconds=0.0)
                cur = cur or dict(unit_id=payload.get("unit_id"))
                cur.update(info)
                cur["ended_at"] = since()
                cur["payload"] = payload
                groups.append(cur)
                stem = "%02d_%s" % (len(groups), str(cur.get("unit_id", "x")).replace(":", "-"))
                if buf:
                    (out / (stem + ".wav")).write_bytes(buf)
                buf, cur = b"", None
            elif name == "session.finished":
                break
    if sender:
        sender.cancel()
    for g in groups:
        g.pop("payload", None)
    data = dict(text=TEXT, sentences=SENTENCES, groups=groups, control=control,
                ws=args.ws, note="bytes/durations as shipped by 9885 over the v2 ws")
    (out / "9885-ws.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    for g in groups:
        print(json.dumps({k: g.get(k) for k in ("unit_id", "bytes", "seconds", "frames", "started_at", "ended_at")},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ws", default="ws://127.0.0.1:9885/api/v1/audio/speech/ws")
    p.add_argument("--out", required=True)
    raise SystemExit(asyncio.run(main(p.parse_args())))
