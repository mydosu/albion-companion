"""Opt-in real translation/GPU/WS probe. Prerequisites and evidence limits: ../README.md."""
import argparse
import asyncio
import base64
import io
import json
import math
from pathlib import Path
import struct
import time
import uuid
import wave

import httpx
import websockets

JA = "こんにちは。今日はいい天気ですね。"
TRANSLATIONS = ["请把桌上的两本书放回书架，然后关灯。", "不要开窗，请先关门。"]
CASES = {"ja": JA, "aside": "（心想：别让他担心。）我已经回来了。",
         "silent": "（心想：明天再说。）", "long": (JA + "はい、わかりました。") * 5,
         "cancel": JA * 8}


def wav_info(blob):
    # A sentence may contain multiple complete RIFF files. Never concatenate their headers as PCM.
    pos, seconds, parts, frames = 0, 0., [], 0
    while pos < len(blob):
        assert blob[pos:pos+4] == b"RIFF" and blob[pos+8:pos+12] == b"WAVE", "invalid WAV"
        end = pos + 8 + struct.unpack_from("<I", blob, pos+4)[0]
        assert end <= len(blob), "truncated RIFF"
        with wave.open(io.BytesIO(blob[pos:end])) as w:
            assert w.getsampwidth() == 2
            params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            pcm = w.readframes(w.getnframes())
            samples = struct.unpack("<%dh" % (len(pcm)//2), pcm)
            rms = math.sqrt(sum(x*x for x in samples)/max(1, len(samples)))
            assert len(pcm) == w.getnframes()*params[0]*params[1] and rms > 1, "empty/silent audio"
            seconds += w.getnframes()/params[2]
            frames += w.getnframes()
            parts.append(dict(params=params, frames=w.getnframes(), rms=round(rms, 2)))
        pos = end
    assert parts
    return dict(seconds=round(seconds, 4), frames=frames, parts=parts, bytes=len(blob))


async def sse(response):
    name, data = "message", []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield name, json.loads("\n".join(data))
            name, data = "message", []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    assert not data, "unterminated SSE event"


async def probe(args):
    out = Path(args.out).resolve()
    assert "tests" in out.parts and "output" in out.parts, "evidence must be under tests/output"
    out.mkdir(parents=True, exist_ok=True)
    result = dict(requested=0, passed=0, failed=0, skipped=0, cases=[],
                  translation_semantics="REQUIRES_REVIEW", human_listening="UNVERIFIED",
                  actual_shell_playback="UNVERIFIED", translation_backend="UNKNOWN")
    async with httpx.AsyncClient(timeout=150) as client:
        try:
            for base, path in ((args.bridge, "/v2/health"), (args.backend, "/health"),
                               (args.ws.replace("ws://", "http://").split("/api/")[0], "/health")):
                ready = await client.get(base+path, timeout=3)
                ready.raise_for_status()
                assert ready.json().get("ok"), "service not ready"
        except Exception as e:
            result["preflight_error_type"] = type(e).__name__
            (out/"summary.json").write_text(json.dumps(result, indent=2), encoding="utf8")
            return 1
        async def post(base, path, body):
            r = await client.post(base + path, json=body)
            r.raise_for_status()
            return r.json()

        async def run(name, fn):
            result["requested"] += 1
            start = time.monotonic()
            rec = dict(name=name)
            try:
                rec.update(await fn())
                rec["passed"] = True
                result["passed"] += 1
            except Exception as e:
                # Never dump HTTP headers, response bodies or environment.
                rec.update(passed=False, error_type=type(e).__name__,
                           status_code=getattr(getattr(e, "response", None), "status_code", None))
                if isinstance(e, AssertionError):
                    rec["assertion"] = str(e)
                result["failed"] += 1
            rec["elapsed_s"] = round(time.monotonic()-start, 3)
            result["cases"].append(rec)
            (out/"summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            print(json.dumps(rec, ensure_ascii=False), flush=True)

        for i, text in enumerate(TRANSLATIONS):
            async def translation(text=text, i=i):
                data = await post(args.bridge, "/v2/prepare", dict(session_id="probe-"+uuid.uuid4().hex,
                    generation=1, unit_id="u", voice="albion", source_range=[0, len(text)],
                    speech_text=text, plan_version=4, emotion_mode="v3"))
                (out/("translation-%d.json" % i)).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
                assert data.get("status") == "ready" and data.get("ja_segments")
                return dict(source=text, segments=data["ja_segments"])
            await run("translation-%d" % i, translation)

        for voice in filter(None, args.voices.split(",")):
            async def audio(voice=voice):
                if args.legacy:
                    r = await client.post(args.backend+"/tts", json=dict(text=JA, voice=voice))
                    r.raise_for_status()
                    blob = r.content
                    path = out/(voice+".wav")
                    path.write_bytes(blob)
                    assert r.headers.get("x-irodori-ship") == voice, "voice header mismatch: " + str(r.headers.get("x-irodori-ship"))
                else:
                    plan = await post(args.backend, "/v2/plan", dict(plan_version=4, emotion_policy="cascade",
                        units=[dict(unit_id="u", text=JA, sub_index=0)]))
                    unit = dict(plan["units"][0], text=JA, unit_id="u", seed=12345)
                    opened = await post(args.backend, "/v2/render/open", dict(voice=voice))
                    assert opened["render_lifecycle_version"] == 2
                    address = dict(session_id=opened["session_id"], generation=opened["generation"])
                    completed, blobs, events = False, [], []
                    try:
                        async with client.stream("POST", args.backend+"/v2/render", json=dict(address,
                                voice=voice, plan_version=4, format="wav", units=[unit])) as r:
                            r.raise_for_status()
                            async for name, payload in sse(r):
                                events.append(name)
                                assert name not in ("unit.failed", "render.cancelled")
                                if name == "audio_chunk":
                                    part = base64.b64decode(payload["audio_base64"], validate=True)
                                    wav_info(part)
                                    blobs.append(part)
                                if name == "done":
                                    assert payload["status"] == "complete" and payload["failed"] == 0
                                    completed = True
                        assert completed and blobs
                        assert len(blobs) == 1, "single planned unit must produce one complete WAV"
                        blob = blobs[0]
                    finally:
                        await post(args.backend, "/v2/render/"+("close" if completed else "cancel"), address)
                path = out/(voice+".wav")
                path.write_bytes(blob)
                return dict(file=str(path), **wav_info(blob))
            await run("audio-"+voice, audio)

        if not args.legacy:
            for case in args.cases.split(","):
                if case:
                    await run("ws-"+case, lambda case=case: ws_case(args.ws, case, out))
            if "cancel" in args.cases.split(","):
                await run("ws-after-cancel", lambda: ws_case(args.ws, "ja", out, suffix="after-cancel"))
            await asyncio.sleep(.5)
            r = await client.get(args.backend+"/v2/render/stats")
            stats = r.json()
            (out/"resources.json").write_text(json.dumps(stats, indent=2), encoding="utf8")
            await run("resources", lambda: check_resources(stats))
    return result["failed"]


async def check_resources(stats):
    assert not stats["running"] and stats["queued"] == 0
    for key in ("active_sessions", "retired_entries", "watchers", "inflight_units", "done_units", "dropped_units"):
        assert stats[key] == 0, key
    return dict(resources=stats)


async def ws_case(url, case, out, suffix=None):
    text = CASES[case]
    t0, play_end, audio_seconds, played = time.monotonic(), time.monotonic(), 0., 0
    events, chunks, ack_tasks = [], [], []
    session_id, pending_display = None, set()
    buf, cancel_sent, first_audio = b"", False, None
    stem = suffix or case
    async with websockets.connect(url, max_size=None) as ws:
        async def send_text():
            for i in range(0, len(text), 5):
                await ws.send(json.dumps(dict(event="text", text=text[i:i+5]), ensure_ascii=False))
                await asyncio.sleep(.08)
            await ws.send(json.dumps(dict(event="finish")))
        async def ack(at, uid, seconds, units):
            await asyncio.sleep(max(0, at-time.monotonic()))
            await ws.send(json.dumps(dict(event="playback.ack", generation=1,
                                         unit_id=uid, seconds=seconds, units=units)))
        await ws.send(json.dumps(dict(event="start", voice="albion", response_format="wav",
                                     extra_body=dict(protocol_version=2, generation=1))))
        sender = None
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), 180)
                if isinstance(raw, bytes):
                    buf += raw
                    first_audio = first_audio or time.monotonic()-t0
                    continue
                ev = json.loads(raw)
                events.append(ev)
                name, p = ev.get("event"), ev.get("payload") or {}
                for address in (ev, p):
                    if "generation" in address:
                        assert address["generation"] == 1, "old generation"
                    if "session_id" in address and session_id is not None:
                        assert address["session_id"] == session_id, "wrong session"
                if name == "session.started":
                    session_id = ev["session_id"]
                    await ws.send(json.dumps(dict(event="playback.credit", seconds=15, units=8)))
                    sender = asyncio.create_task(send_text())
                elif name == "sentence.start":
                    assert not buf
                    assert p.get("generation", 1) == 1
                    pending_display.discard(p["unit_id"])
                elif name == "display.span":
                    start, end = p["source_range"]
                    assert 0 <= start < end <= len(text) and p["text"] == text[start:end], "display source range"
                    if p.get("eligible") == "audio":
                        pending_display.add(p["unit_id"])
                    else:
                        assert p.get("eligible") == "immediate"
                elif name == "sentence.end":
                    info = wav_info(buf)
                    (out/(stem+"-%02d.wav" % len(chunks))).write_bytes(buf)
                    chunks.append(info)
                    buf = b""
                    if case == "cancel" and not cancel_sent:
                        await ws.send(json.dumps(dict(event="cancel")))
                        cancel_sent = True
                        # Protocol v2 has no cancel ACK frame. Confirm the actual server state,
                        # rather than requiring a session.finished event it never promised.
                        async with httpx.AsyncClient(timeout=5) as control:
                            for _ in range(50):
                                health = (await control.get(url.replace("ws://", "http://").split("/api/")[0]+"/health")).json()
                                if any(s["session_id"] == session_id and s["state"] == "CANCELLED"
                                       for s in health["sessions"]):
                                    break
                                await asyncio.sleep(.02)
                            else:
                                raise AssertionError("cancel not accepted")
                        if sender:
                            sender.cancel()
                        try:
                            while True:
                                late = await asyncio.wait_for(ws.recv(), .5)
                                assert not isinstance(late, bytes), "old audio after cancel accepted"
                                assert json.loads(late).get("event") not in ("sentence.start", "sentence.end"), "late old sentence"
                        except (asyncio.TimeoutError, websockets.ConnectionClosed):
                            pass
                        break
                    elif not cancel_sent:
                        play_end = max(play_end, time.monotonic())+info["seconds"]
                        audio_seconds += info["seconds"]
                        played += 1
                        ack_tasks.append(asyncio.create_task(ack(play_end, p.get("unit_id"), audio_seconds, played)))
                elif name == "session.finished":
                    if case == "cancel":
                        assert cancel_sent and ev.get("status") == "cancelled"
                        # The server has accepted cancellation. No new audio/old sentence may follow.
                        try:
                            while True:
                                late = await asyncio.wait_for(ws.recv(), .5)
                                assert not isinstance(late, bytes), "old audio after cancel accepted"
                                assert json.loads(late).get("event") not in ("sentence.start", "sentence.end"), "late old sentence"
                        except (asyncio.TimeoutError, websockets.ConnectionClosed):
                            pass
                    else:
                        assert ev.get("status") == "complete" and ev.get("failed") == 0
                        assert bool(chunks) == (case != "silent")
                        if case == "silent":
                            assert ev.get("no_audio", 0) > 0
                        if case == "long":
                            assert audio_seconds > 15 and len(chunks) >= 10
                    break
                elif name in ("unit.failed", "error"):
                    raise AssertionError("WS stage failure")
            if case != "cancel":
                await asyncio.gather(*ack_tasks)
                spans = [e["payload"] for e in events if e.get("event") == "display.span"]
                assert spans
                assert not pending_display, "audio display without corresponding sentence"
                assert "".join(p["text"] for p in spans) == text, "display tail missing or duplicated"
                (out/(stem+"-display.json")).write_text(json.dumps(spans, ensure_ascii=False, indent=2), encoding="utf8")
        finally:
            for task in [sender, *ack_tasks]:
                if task:
                    task.cancel()
            await asyncio.gather(*[t for t in [sender, *ack_tasks] if t], return_exceptions=True)
            (out/(stem+"-events.json")).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf8")
    return dict(audio_seconds=round(audio_seconds, 3), chunks=chunks, first_audio_s=first_audio,
                cancel_sent=cancel_sent, simulated_playback=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", default="http://127.0.0.1:9881")
    parser.add_argument("--backend", default="http://127.0.0.1:9889")
    parser.add_argument("--ws", default="ws://127.0.0.1:9885/api/v1/audio/speech/ws")
    parser.add_argument("--voices", default="albion,taihou,shinano,brest,jinshi")
    parser.add_argument("--cases", default="ja,aside,silent,long,cancel")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--out", required=True)
    raise SystemExit(bool(asyncio.run(probe(parser.parse_args()))))
