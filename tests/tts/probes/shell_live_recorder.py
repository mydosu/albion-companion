"""Passive live recorder for shell audio-truncation (task 95be04e0).

Attaches read-only prototype hooks to the *running* Electron renderer over CDP
(127.0.0.1:9335, main page `synced-leader=true#/`), records every
`AudioBufferSourceNode.start/stop/ended`, the `GainNode` fade calls, `AudioContext`
suspend/close, and tees whatever reaches `AudioContext.destination` into a WAV.
Nothing is dispatched into the chat and no production file is edited; the hooks are
restored and the tap disconnected on exit.

    python tests/tts/probes/shell_live_recorder.py --out tests/output/tts-shell-trunc-20261109/live

Stops when `--min-turns` intents have finished playing (+ `--post-turn-seconds` of tail),
or after `--max-wait` seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import time
import urllib.request
import wave

import numpy as np
import websockets

HOOK = r"""
(() => {
  if (window.__truncLive && window.__truncLive.installed) return 'already'
  const L = { installed: true, events: [], pcm: [], pcmStartCtx: null, sr: null, ctx: null, taps: [], t0: performance.now() }
  window.__truncLive = L
  const orig = {
    start: AudioBufferSourceNode.prototype.start,
    stop: AudioBufferSourceNode.prototype.stop,
    connect: AudioNode.prototype.connect,
    ramp: AudioParam.prototype.linearRampToValueAtTime,
    setv: AudioParam.prototype.setValueAtTime,
    cancel: AudioParam.prototype.cancelScheduledValues,
    suspend: AudioContext.prototype.suspend,
    close: AudioContext.prototype.close,
  }
  L.orig = orig
  const ids = new WeakMap()
  let seq = 0
  const idOf = (n) => { let i = ids.get(n); if (!i) { i = ++seq; ids.set(n, i) } return i }
  const rel = () => Math.round(performance.now() - L.t0)
  const taps = new WeakMap()
  const stackOf = () => (new Error('trace')).stack.split('\n').slice(2, 9).map(s => s.trim()).join(' <- ')

  function tapFor(ctx) {
    let sp = taps.get(ctx)
    if (sp) return sp
    sp = ctx.createScriptProcessor(4096, 1, 1)
    const mute = ctx.createGain()
    mute.gain.value = 0
    orig.connect.call(sp, mute)
    orig.connect.call(mute, ctx.destination)
    sp.onaudioprocess = (e) => {
      const ch = e.inputBuffer.getChannelData(0)
      if (L.pcmStartCtx == null) L.pcmStartCtx = e.playbackTime - ch.length / ctx.sampleRate
      if (L.sr == null) L.sr = ctx.sampleRate
      const pcm = new Int16Array(ch.length)
      for (let i = 0; i < ch.length; i++) {
        const v = Math.max(-1, Math.min(1, ch[i]))
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7FFF
      }
      if (L.pcm.length > 1500) L.pcm.shift()          // ~6.5 min of 4k blocks safety cap
      L.pcm.push(pcm)
    }
    taps.set(ctx, sp)
    L.taps.push({ ctx, sp, mute })
    if (!L.ctx) L.ctx = ctx
    L.events.push({ k: 'tap', t: rel(), ctx: ctx.currentTime, sr: ctx.sampleRate, state: ctx.state })
    return sp
  }

  AudioNode.prototype.connect = function (target, ...rest) {
    try {
      if (target && target instanceof AudioDestinationNode) {
        const sp = tapFor(this.context)
        orig.connect.call(this, sp)
      }
    } catch (err) { L.events.push({ k: 'tapError', t: rel(), msg: String(err) }) }
    return orig.connect.call(this, target, ...rest)
  }
  AudioBufferSourceNode.prototype.start = function (...a) {
    try {
      L.events.push({ k: 'start', t: rel(), id: idOf(this), ctx: this.context.currentTime, when: a[0] ?? 0,
        dur: this.buffer ? this.buffer.duration : null, frames: this.buffer ? this.buffer.length : null,
        rate: this.buffer ? this.buffer.sampleRate : null,
        unit: this.__albionUnitId ?? null, intent: this.__albionIntentId ?? null,
        ident: this.__albionIdentity ? JSON.parse(JSON.stringify(this.__albionIdentity)) : null })
      this.addEventListener('ended', () => {
        L.events.push({ k: 'ended', t: rel(), id: idOf(this), ctx: this.context.currentTime,
          unit: this.__albionUnitId ?? null, intent: this.__albionIntentId ?? null })
      })
    } catch (err) { L.events.push({ k: 'hookError', t: rel(), msg: String(err) }) }
    return orig.start.apply(this, a)
  }
  AudioBufferSourceNode.prototype.stop = function (...a) {
    try {
      L.events.push({ k: 'stop', t: rel(), id: idOf(this), ctx: this.context.currentTime, when: a[0] ?? null,
        unit: this.__albionUnitId ?? null, intent: this.__albionIntentId ?? null, state: this.context.state,
        stack: stackOf() })
    } catch (err) {}
    return orig.stop.apply(this, a)
  }
  AudioParam.prototype.linearRampToValueAtTime = function (v, t) {
    try { L.events.push({ k: 'ramp', t: rel(), ctx: L.ctx ? L.ctx.currentTime : null, value: v, at: t, pid: idOf(this) }) } catch (err) {}
    return orig.ramp.call(this, v, t)
  }
  AudioParam.prototype.setValueAtTime = function (v, t) {
    try { L.events.push({ k: 'setv', t: rel(), ctx: L.ctx ? L.ctx.currentTime : null, value: v, at: t, pid: idOf(this) }) } catch (err) {}
    return orig.setv.call(this, v, t)
  }
  AudioParam.prototype.cancelScheduledValues = function (t) {
    try { L.events.push({ k: 'cancelv', t: rel(), ctx: L.ctx ? L.ctx.currentTime : null, at: t, pid: idOf(this) }) } catch (err) {}
    return orig.cancel.call(this, t)
  }
  AudioContext.prototype.suspend = function (...a) {
    try { L.events.push({ k: 'suspend', t: rel(), ctx: this.currentTime, stack: stackOf() }) } catch (err) {}
    return orig.suspend.apply(this, a)
  }
  AudioContext.prototype.close = function (...a) {
    try { L.events.push({ k: 'close', t: rel(), ctx: this.currentTime, stack: stackOf() }) } catch (err) {}
    return orig.close.apply(this, a)
  }

  L.drain = () => {
    const events = L.events
    L.events = []
    const pcm = L.pcm
    L.pcm = []
    let total = 0
    for (const c of pcm) total += c.length
    const all = new Int16Array(total)
    let o = 0
    for (const c of pcm) { all.set(c, o); o += c.length }
    const bytes = new Uint8Array(all.buffer)
    let s = ''
    for (let i = 0; i < bytes.length; i += 8192) s += String.fromCharCode(...bytes.subarray(i, i + 8192))
    return { events, pcm: btoa(s), pcmStartCtx: L.pcmStartCtx, sr: L.sr,
             ctxNow: L.ctx ? L.ctx.currentTime : null, state: L.ctx ? L.ctx.state : null }
  }
  L.unhook = () => {
    AudioBufferSourceNode.prototype.start = orig.start
    AudioBufferSourceNode.prototype.stop = orig.stop
    AudioNode.prototype.connect = orig.connect
    AudioParam.prototype.linearRampToValueAtTime = orig.ramp
    AudioParam.prototype.setValueAtTime = orig.setv
    AudioParam.prototype.cancelScheduledValues = orig.cancel
    AudioContext.prototype.suspend = orig.suspend
    AudioContext.prototype.close = orig.close
    for (const { sp, mute } of L.taps) {
      try { sp.onaudioprocess = null } catch (e) {}
      try { sp.disconnect() } catch (e) {}
      try { mute.disconnect() } catch (e) {}
    }
    L.installed = false
    return 'unhooked'
  }
  return 'hooked'
})()
"""


async def evaluate(ws, expression, ident, timeout=60):
    await ws.send(json.dumps(dict(id=ident, method="Runtime.evaluate", params=dict(
        expression=expression, awaitPromise=False, returnByValue=True, userGesture=True))))
    while True:
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if resp.get("id") == ident:
            if "exceptionDetails" in resp.get("result", {}):
                return None, resp["result"]["exceptionDetails"].get("text") or "exception"
            return resp["result"]["result"].get("value"), None


def write_wav(path: pathlib.Path, raw: bytes, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(raw)


async def main(args) -> int:
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    targets = json.load(urllib.request.urlopen(args.cdp + "/json/list", timeout=3))
    pages = [x for x in targets if x.get("type") == "page" and "synced-leader=true#/" in x.get("url", "")]
    assert len(pages) == 1, f"need exactly one main renderer, found {len(pages)}"
    started = time.monotonic()
    deadline = started + args.max_wait
    pcm_path = out / "live.pcm"
    pcm_file = pcm_path.open("wb")
    events: list[dict] = []
    sr = None
    stop_at: float | None = None
    seq = 0
    turns: dict[str, dict] = {}
    last_complete: float | None = None
    log = out / "live-log.txt"

    def note(msg: str) -> None:
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        log.open("a", encoding="utf8").write(line + "\n")

    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        note("attach " + pages[0]["url"][-60:])
        seq += 1
        state, err = await evaluate(ws, HOOK, seq)
        note("hook: %s %s" % (state, err or ""))
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                seq += 1
                drained, err = await evaluate(ws, "window.__truncLive ? window.__truncLive.drain() : null", seq)
                if err or drained is None:
                    note("drain failed (%s), re-hooking" % (err or "no probe"))
                    seq += 1
                    await evaluate(ws, HOOK, seq)
                    continue
                if drained.get("sr"):
                    sr = drained["sr"]
                raw = base64.b64decode(drained.get("pcm") or "")
                if raw:
                    pcm_file.write(raw)
                for ev in drained.get("events") or []:
                    events.append(ev)
                    if ev["k"] in ("start", "ended", "stop") and ev.get("intent"):
                        turn = turns.setdefault(ev["intent"], {"spans": 0, "ended": 0, "stopped": 0, "last": 0.0})
                        turn["last"] = time.monotonic()
                        if ev["k"] == "start":
                            turn["spans"] += 1
                        elif ev["k"] == "ended":
                            turn["ended"] += 1
                        elif ev["k"] == "stop":
                            turn["stopped"] += 1
                now_ctx = drained.get("ctxNow")
                # a turn counts as finished only when every span ended AND nothing new showed up for a while
                done = [k for k, v in turns.items()
                        if v["spans"] and v["ended"] >= v["spans"] and time.monotonic() - v["last"] >= 8]
                if done and last_complete is None:
                    last_complete = time.monotonic()
                    note("first complete turn: %s (%d spans) — keeping the tap open"
                         % (done[0], turns[done[0]]["spans"]))
                if len(done) >= args.min_turns:
                    if stop_at is None:
                        stop_at = time.monotonic() + args.post_turn_seconds
                        note("%d complete turns — %.0fs of tail then stop" % (len(done), args.post_turn_seconds))
                if stop_at and time.monotonic() >= stop_at:
                    note("stop: %d complete turn(s), %d events, %d s elapsed"
                         % (len(done), len(events), round(time.monotonic() - started, 1)))
                    break
                if int(time.monotonic() - started) % 60 < 2:
                    note("waiting: %d events, %d spans, state=%s ctx=%s"
                         % (len(events), sum(t["spans"] for t in turns.values()),
                            drained.get("state"), round(now_ctx or 0, 2)))
        finally:
            seq += 1
            drained, err = await evaluate(ws, "window.__truncLive ? window.__truncLive.drain() : null", seq)
            if drained:
                raw = base64.b64decode(drained.get("pcm") or "")
                if raw:
                    pcm_file.write(raw)
                events.extend(drained.get("events") or [])
            seq += 1
            note("unhook: %s" % str(await evaluate(ws, "window.__truncLive ? window.__truncLive.unhook() : 'absent'", seq)))
            pcm_file.close()

    elapsed = round(time.monotonic() - started, 1)
    meta = dict(elapsed_s=elapsed, sample_rate=sr, pcm_bytes=pcm_path.stat().st_size,
                events=len(events), turns={k: v for k, v in turns.items()},
                complete_intents=[k for k, v in turns.items() if v["spans"] and v["ended"] >= v["spans"]],
                pcm_start_ctx=events[0].get("ctx") if events else None,
                note="passive capture in the live shell; nothing injected into the chat")
    if sr:
        write_wav(out / "live-destination.wav", pcm_path.read_bytes(), sr)
    (out / "live-events.json").write_text(json.dumps(dict(meta=meta, events=events), ensure_ascii=False, indent=1),
                                         encoding="utf8")
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cdp", default="http://127.0.0.1:9335")
    p.add_argument("--out", default="tests/output/tts-shell-trunc-20261109/live")
    p.add_argument("--max-wait", type=float, default=1500.0)
    p.add_argument("--min-turns", type=int, default=3)
    p.add_argument("--post-turn-seconds", type=float, default=10.0)
    raise SystemExit(asyncio.run(main(p.parse_args())))
