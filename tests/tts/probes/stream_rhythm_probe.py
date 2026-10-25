# -*- coding: utf-8 -*-
"""经 :9885（或备用口 :19885）的 ws 走一段文本，逐单元落 wav，量节奏，并按壳的播放规则模拟拼接。

只读：会调用真实的翻译桥和后端（花少量翻译额度），不改任何服务。

用法（用 tts/irodori/.venv 的 python）::

    python tests/tts/probes/stream_rhythm_probe.py --out tests/output/xxx --tag A \
        --text "指挥官，今天辛苦了。实验做完了吗？……做完的话，就早点休息吧。"
    python tests/tts/probes/stream_rhythm_probe.py --ws ws://127.0.0.1:19885/api/v1/audio/speech/ws ...

输出（都在 --out 下，必须位于 tests/output/ 内）：
  <tag>-NN.wav        网关发来的每个音频段（壳 `sentence.start`→`sentence.end` 之间的字节）
  <tag>-probe.json    每段的时序/静音/末 200ms 能量，以及模拟播放的起止与可听间隔
  <tag>-sim.wav       按壳 streaming-playback 的规则摆好的整段播放（含欠载造成的空档），可直接听

壳的规则（shell/packages/stage-ui/src/libs/speech/streaming-playback.ts）：
  首段 start = 到达时刻 + 0.05s；之后 start = max(到达时刻, 上段结束 + 0.10s)。
"""
import argparse
import asyncio
import io
import json
import pathlib
import time
import wave

import numpy as np
import websockets

LEAD, PAUSE = 0.05, 0.10
DEFAULT_WS = "ws://127.0.0.1:9885/api/v1/audio/speech/ws"


def wav_arr(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype={2: "<i2", 4: "<i4"}[sw]).astype("float32") / float(2 ** (8 * sw - 1))
    return sr, (a.reshape(-1, ch).mean(1) if ch > 1 else a)


def db(x):
    return 20 * np.log10(max(float(x), 1e-9))


def analyze(sr, a):
    """时长、首/尾静音（阈值 = 峰值 -40dB 与 -55dBFS 取大）、末 200ms 电平。末 200ms 在静音电平 = 不是在发声中被切断。"""
    peak = float(np.abs(a).max()) or 1e-9
    thr = max(peak * 10 ** (-40 / 20), 10 ** (-55 / 20))
    act = np.where(np.abs(a) > thr)[0]
    if len(act) == 0:
        return dict(dur=len(a) / sr, lead=len(a) / sr, tail=0.0, voiced_end=0.0, tail200_db=db(0), peak_db=db(peak))
    n200 = int(0.2 * sr)
    rms = float(np.sqrt((a[-n200:] ** 2).mean()))
    return dict(dur=round(len(a) / sr, 3), lead=round(act[0] / sr, 3),
                tail=round((len(a) - 1 - act[-1]) / sr, 3), voiced_end=round((act[-1] + 1) / sr, 3),
                tail200_db=round(db(rms), 1), peak_db=round(db(peak), 1))


def simulate(segs):
    """按壳的时间轴给每段算 play_start/play_end、欠载、相邻段的可听间隔（有声结束→下一段有声开始）。"""
    nxt = None
    for s in segs:
        arrive = s["end_at"]
        start = arrive + LEAD if (nxt is None or nxt < arrive) else nxt
        s["underrun_before"] = None if nxt is None else round(start - nxt, 3)
        s["play_start"], s["play_end"] = round(start, 3), round(start + s["dur"], 3)
        nxt = start + s["dur"] + PAUSE
    for a, b in zip(segs, segs[1:]):
        b["audible_gap_s"] = round((b["play_start"] + b["lead"]) - (a["play_start"] + a["voiced_end"]), 3)


def write_sim(path, segs, blobs):
    sr = segs[0]["sr"]
    total = int((segs[-1]["play_end"] + 0.5) * sr)
    out = np.zeros(total, dtype="float32")
    for s, blob in zip(segs, blobs):
        _, a = wav_arr(blob)
        i = int(s["play_start"] * sr)
        out[i:i + len(a)] += a[:max(0, total - i)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())


async def run(ws_url, text, chunk=3, chunk_gap=0.06):
    t0 = time.monotonic()
    segs, blobs, events, buf, first_audio, cur = [], [], [], b"", None, None
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps(dict(event="start", voice="albion", response_format="wav",
                                      extra_body=dict(protocol_version=2, generation=1))))

        async def feed():
            for i in range(0, len(text), chunk):      # 像 LLM token 流
                await ws.send(json.dumps(dict(event="text", text=text[i:i + chunk]), ensure_ascii=False))
                await asyncio.sleep(chunk_gap)
            await ws.send(json.dumps(dict(event="finish")))
            events.append(("text_done", round(time.monotonic() - t0, 3)))

        sent_s = 0.0
        while True:
            raw = await asyncio.wait_for(ws.recv(), 180)
            now = time.monotonic() - t0
            if isinstance(raw, bytes):
                buf += raw
                first_audio = first_audio or now
                continue
            ev = json.loads(raw)
            name, p = ev.get("event"), ev.get("payload") or {}
            if name == "session.started":
                await ws.send(json.dumps(dict(event="playback.credit", seconds=15, units=8)))
                asyncio.create_task(feed())
            elif name == "sentence.start":
                cur = dict(unit=p.get("unit_id"), start_at=round(now, 3))
            elif name == "sentence.end":
                sr, a = wav_arr(buf)
                cur = dict(cur or {}, end_at=round(now, 3), sr=sr, **analyze(sr, a))
                segs.append(cur); blobs.append(buf); buf = b""
                sent_s += cur["dur"]
                await ws.send(json.dumps(dict(event="playback.ack", generation=1, unit_id=p.get("unit_id"),
                                              seconds=round(sent_s, 3), units=len(segs))))
            elif name in ("unit.no_audio", "unit.failed", "error"):
                events.append((name, round(now, 3), p.get("unit_id"), p.get("stage") or p.get("reason")))
            elif name == "session.finished":
                events.append(("finished", round(now, 3), ev.get("status")))
                break
    simulate(segs)
    return dict(first_audio_s=round(first_audio, 3) if first_audio else None, segs=segs, events=events), blobs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws", default=DEFAULT_WS)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--text", required=True)
    args = ap.parse_args()
    out = pathlib.Path(args.out).resolve()
    assert "tests" in out.parts and "output" in out.parts, "输出必须在 tests/output/ 下"
    out.mkdir(parents=True, exist_ok=True)
    res, blobs = asyncio.run(run(args.ws, args.text))
    for i, b in enumerate(blobs):
        (out / ("%s-%02d.wav" % (args.tag, i))).write_bytes(b)
    if res["segs"]:
        write_sim(out / (args.tag + "-sim.wav"), res["segs"], blobs)
    res["text"] = args.text
    (out / (args.tag + "-probe.json")).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf8")
    for s in res["segs"]:
        print("%s %s arrive=%.2fs dur=%.2f lead=%.2f tail=%.2f t200=%sdB gap=%s underrun=%s" % (
            args.tag, s["unit"][-3:], s["end_at"], s["dur"], s["lead"], s["tail"], s["tail200_db"],
            s.get("audible_gap_s"), s.get("underrun_before")))
    print("first_audio_s", res["first_audio_s"], "events", [e for e in res["events"] if e[0] != "text_done"])


if __name__ == "__main__":
    main()
