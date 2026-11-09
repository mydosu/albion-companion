// Read-only instrumentation harness for task 14d05275 (shell-side tail truncation).
// Runs the repository pipeline + playback clock inside the *existing* Electron renderer,
// with prototype-level hooks (WebSocket / decodeAudioData / AudioBufferSourceNode) so we can
// see exactly where seconds go missing. No production code is modified; nothing is dispatched
// into the chat, and no chat/profile data is read.
import { createStreamingTtsPipeline } from '../../../shell/packages/stage-ui/src/libs/speech/streaming-pipeline'
import { createStreamingPlayback } from '../../../shell/packages/stage-ui/src/libs/speech/streaming-playback'

export async function runTruncProbe(serverUrl: string, text: string) {
  const t0 = performance.now()
  const rec = {
    decoded: [] as Record<string, unknown>[],
    groups: [] as Record<string, unknown>[],
    spans: [] as Record<string, unknown>[],
    nodes: [] as Record<string, unknown>[],
    stops: [] as Record<string, unknown>[],
    events: [] as Record<string, unknown>[],
    errors: [] as string[],
    contextState: '',
    sampleRate: 0,
    stats: {} as Record<string, unknown>,
    progress: {} as Record<string, unknown>,
    nextStart: null as number | null,
  }
  const now = () => Math.round((performance.now() - t0) * 1000) / 1000

  // ── wire-level: count binary bytes per sentence group ────────────────────────────────
  const OrigWS = window.WebSocket
  const groupCounts: { bytes: number, at: number }[] = []
  let liveCount: { bytes: number, at: number } | null = null
  // @ts-expect-error probe subclass
  window.WebSocket = class extends OrigWS {
    constructor(...args: any[]) {
      super(...args)
      this.addEventListener('message', (e: MessageEvent) => {
        if (typeof e.data === 'string') {
          try {
            const evt = JSON.parse(e.data)
            if (evt.event === 'sentence.start')
              liveCount = { bytes: 0, at: now() }
            else if (evt.event === 'sentence.end' && liveCount)
              groupCounts.push(liveCount)
          }
          catch {}
          return
        }
        const size = (e.data as ArrayBuffer).byteLength
        if (liveCount)
          liveCount.bytes += size
      })
    }
  }

  // ── decode: how many bytes went in, how many frames came out ─────────────────────────
  const decodeProto = (window.AudioContext as any).prototype
  const origDecode = decodeProto.decodeAudioData
  const decodedRaw: { bytes: number, at: number }[] = []
  decodeProto.decodeAudioData = function (bytes: ArrayBuffer, ok?: any, bad?: any) {
    const entry = { bytes: bytes.byteLength, at: now() }
    decodedRaw.push(entry)
    return origDecode.call(this, bytes, (buf: AudioBuffer) => {
      Object.assign(entry, {
        seconds: buf.duration, frames: buf.length, rate: buf.sampleRate, channels: buf.numberOfChannels,
        ok: true,
      })
      if (ok) ok(buf)
    }, (err: any) => {
      Object.assign(entry, { ok: false })
      if (bad) bad(err)
    })
  }

  // ── node-level: start/stop/ended, tagged with the span id via `connect()` ────────────
  const srcProto = (window.AudioBufferSourceNode as any).prototype
  const origStart = srcProto.start
  const origStop = srcProto.stop
  const tagOf = new WeakMap<object, string>()
  const nodeIds = new WeakMap<object, number>()
  let nodeSeq = 0
  function idOf(node: any) {
    let id = nodeIds.get(node)
    if (id == null) { id = ++nodeSeq; nodeIds.set(node, id) }
    return id
  }
  srcProto.start = function (...args: any[]) {
    const ctx = this.context
    const node = {
      id: idOf(this), span: tagOf.get(this) ?? null, at: now(), ctxAt: ctx?.currentTime ?? null,
      when: args[0] ?? 0, duration: this.buffer?.duration ?? null, frames: this.buffer?.length ?? null,
      rate: this.buffer?.sampleRate ?? null, endedAt: null as number | null, ctxEndedAt: null as number | null,
    }
    rec.nodes.push(node)
    this.addEventListener('ended', () => {
      node.endedAt = now()
      node.ctxEndedAt = ctx?.currentTime ?? null
    })
    rec.events.push({ event: 'start', at: node.at, id: node.id, span: node.span, when: node.when, duration: node.duration })
    return origStart.apply(this, args)
  }
  srcProto.stop = function (...args: any[]) {
    const stack = (new Error('probe-stop')).stack?.split('\n').slice(1, 6).join(' | ') ?? ''
    rec.stops.push({ at: now(), id: idOf(this), span: tagOf.get(this) ?? null, when: args[0] ?? null,
      ctxAt: this.context?.currentTime ?? null, stack })
    return origStop.apply(this, args)
  }

  // ── run the pipeline exactly like tts-session.ts does ───────────────────────────────
  const ctx = new AudioContext()
  await ctx.resume()
  rec.contextState = ctx.state
  rec.sampleRate = ctx.sampleRate

  // Output-graph tap (float capture of what the clock actually emits, mono).
  const tap = ctx.createScriptProcessor(4096, 1, 1)
  const chunks: Float32Array[] = []
  tap.onaudioprocess = (e) => {
    const ch = e.inputBuffer.getChannelData(0)
    chunks.push(new Float32Array(ch))
  }
  const mute = ctx.createGain()
  mute.gain.value = 0
  tap.connect(mute)
  mute.connect(ctx.destination)

  const clock = createStreamingPlayback({
    audioContext: ctx,
    connect: (source, output) => {
      const span = (source as any).__albionUnitId as string | undefined
      if (span)
        tagOf.set(source, span)
      output.connect(ctx.destination)
      output.connect(tap)
    },
    isPlayable: () => ctx.state === 'running',
    onSegmentStart: e => rec.events.push({ event: 'segment-start', at: now(), ctxAt: ctx.currentTime,
      span: e.span.unitId, startAt: e.startAt, endAt: e.endAt, duration: e.duration }),
    onSegmentEnd: e => rec.events.push({ event: 'segment-end', at: now(), ctxAt: ctx.currentTime,
      span: e.span.unitId, startAt: e.startAt, endAt: e.endAt, duration: e.duration, completed: e.completed }),
    onUnderrun: (ms, e) => rec.events.push({ event: 'underrun', at: now(), ms,
      span: e.span.unitId, startAt: e.startAt, endAt: e.endAt }),
    onStopped: info => rec.events.push({ event: 'clock-stopped', at: now(), reason: info.reason,
      fadeEnd: info.fadeEnd, completedEndAt: info.completedEndAt ?? null }),
    onNaturalEnd: endAt => rec.events.push({ event: 'natural-end', at: now(), endAt }),
  })

  let doneResolve: () => void = () => {}
  const done = new Promise<void>((resolve) => { doneResolve = resolve })
  let pipeline: ReturnType<typeof createStreamingTtsPipeline>
  pipeline = createStreamingTtsPipeline({
    serverUrl, token: 'local-probe', model: 'irodori', voice: 'albion', responseFormat: 'wav',
    extraBody: { protocol_version: 2, generation: 1, audio: { sample_rate: 24000, bit_rate: 64000 } },
    audioContext: ctx,
    onSentence: (sentence) => {
      const unitId = sentence.identity
        ? (sentence.identity.subIndex == null ? sentence.identity.unitId : `${sentence.identity.unitId}:${sentence.identity.subIndex}`)
        : `probe-${sentence.index}`
      rec.spans.push({ unitId, index: sentence.index, bytesDuration: null, bufferDuration: sentence.audio.duration,
        frames: sentence.audio.length, rate: sentence.audio.sampleRate, channels: sentence.audio.numberOfChannels,
        at: now() })
      clock.push({ unitId, identity: sentence.identity, buffer: sentence.audio })
    },
    onDisplay: () => {},
    onError: e => rec.errors.push('session:' + e.name + ':' + e.message),
    onSegmentError: e => rec.errors.push('segment:' + e.name + ':' + e.message),
    onDone: () => { clock.closeInput(); doneResolve() },
  })
  clock.arm()
  pipeline.feedback('playback.credit', { seconds: 15, units: 8 })
  const ackTimer = setInterval(() => {
    pipeline.feedback('playback.ack', clock.progress() as any)
  }, 100)
  const timeout = setTimeout(() => {
    rec.errors.push('timeout')
    pipeline.cancel()
    clock.stop('probe-timeout')
  }, 120000)
  try {
    for (let i = 0; i < text.length; i += 5) {
      pipeline.appendText(text.slice(i, i + 5))
      await new Promise(resolve => setTimeout(resolve, 80))
    }
    pipeline.finish()
    await done
    await clock.drained
    // let the last node's `ended` land before reading the recording
    await new Promise(resolve => setTimeout(resolve, 300))
  }
  finally {
    clearInterval(ackTimer)
    clearTimeout(timeout)
    rec.stats = clock.stats() as any
    rec.progress = clock.progress() as any
    rec.nextStart = clock.nextStart()
    rec.contextState = ctx.state
    try { pipeline.cancel() } catch {}
    try { clock.stop('probe-cleanup') } catch {}
    tap.onaudioprocess = null
    try { tap.disconnect() } catch {}
    try { mute.disconnect() } catch {}
    window.WebSocket = OrigWS
  }

  rec.decoded = decodedRaw
  rec.groups = groupCounts
  // Correlate wire groups → decoded buffers → spans by arrival order (both are FIFO).
  for (let i = 0; i < rec.spans.length; i++) {
    const span = rec.spans[i] as any
    const g = groupCounts[i]
    const d = decodedRaw[i] as any
    if (g) {
      span.wireBytes = g.bytes
      span.wireHeaderSeconds = null
    }
    if (d) {
      span.decodeBytes = d.bytes
      span.decodeSeconds = d.seconds ?? null
      span.decodeOk = d.ok ?? null
      // WAV PCM16 mono @48k → seconds implied by the byte count (44-byte header)
      span.bytesImpliedSeconds = +(((d.bytes - 44) / 2) / 48000).toFixed(4)
    }
  }

  // float capture → int16 mono WAV (base64)
  let total = 0
  for (const c of chunks) total += c.length
  const pcm = new Int16Array(total)
  let off = 0
  for (const c of chunks) {
    for (let i = 0; i < c.length; i++) {
      const v = Math.max(-1, Math.min(1, c[i]))
      pcm[off++] = v < 0 ? v * 0x8000 : v * 0x7FFF
    }
  }
  const rate = ctx.sampleRate
  const header = new ArrayBuffer(44)
  const dv = new DataView(header)
  const tag = (o: number, s: string) => { for (let i = 0; i < 4; i++) dv.setUint8(o + i, s.charCodeAt(i)) }
  tag(0, 'RIFF'); dv.setUint32(4, 36 + pcm.byteLength, true); tag(8, 'WAVE'); tag(12, 'fmt ')
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true)
  dv.setUint32(24, rate, true); dv.setUint32(28, rate * 2, true); dv.setUint16(32, 2, true)
  dv.setUint16(34, 16, true); tag(36, 'data'); dv.setUint32(40, pcm.byteLength, true)
  const out = new Uint8Array(44 + pcm.byteLength)
  out.set(new Uint8Array(header), 0)
  out.set(new Uint8Array(pcm.buffer), 44)
  let binary = ''
  for (let i = 0; i < out.length; i += 8192)
    binary += String.fromCharCode(...out.subarray(i, i + 8192))
  await ctx.close()
  return {
    ...rec, outputGraphBase64: btoa(binary), outputGraphBytes: out.length, outputGraphRate: rate,
    text, source: 'repository streaming-pipeline + streaming-playback inside the live Electron renderer',
  }
}
