// Real renderer instrumentation harness. Uses repository pipeline/clock/ledger; never reads chat/profile.
import { createStreamingTtsPipeline } from '../../../shell/packages/stage-ui/src/libs/speech/streaming-pipeline'
import { createStreamingPlayback } from '../../../shell/packages/stage-ui/src/libs/speech/streaming-playback'
import { createDisplayLedger } from '../../../shell/packages/stage-ui/src/libs/speech/display-ledger'

export async function runCase(serverUrl: string, text: string, cancel = false) {
  const ctx = new AudioContext()
  await ctx.resume()
  // Capture only this test's graph, never the microphone or other applications' output.
  // This is output-graph evidence, explicitly not WASAPI/device loopback.
  const capture = ctx.createMediaStreamDestination()
  const recorder = new MediaRecorder(capture.stream)
  const recorded: Blob[] = []
  recorder.ondataavailable = e => { if (e.data.size) recorded.push(e.data) }
  recorder.start()
  const ledger = createDisplayLedger()
  const events: Record<string, unknown>[] = []
  const errors: string[] = []
  const spans: Record<string, unknown>[] = []
  let cancelled = false
  let pipeline: ReturnType<typeof createStreamingTtsPipeline>
  const clock = createStreamingPlayback({
    audioContext: ctx,
    connect: (_source, output) => { output.connect(ctx.destination); output.connect(capture) },
    isPlayable: () => ctx.state === 'running',
    onSegmentStart: e => {
      events.push({event: 'started', at: ctx.currentTime, unit: e.span.unitId, startAt: e.startAt, endAt: e.endAt})
      if (cancel && !cancelled) {
        cancelled = true
        setTimeout(() => {
          pipeline.cancel()
          clock.stop('new-message')
        }, 250)
      }
    },
    onSegmentEnd: e => events.push({event: 'ended', at: ctx.currentTime, unit: e.span.unitId, completed: e.completed}),
  })
  const decoded: number[] = []
  const acks: Record<string, unknown>[] = []
  let doneResolve: () => void
  const done = new Promise<void>(resolve => { doneResolve = resolve })
  pipeline = createStreamingTtsPipeline({
    serverUrl, token: 'local-probe', model: 'irodori', voice: 'albion', responseFormat: 'wav',
    extraBody: {protocol_version: 2, generation: 1}, audioContext: ctx,
    onSentence: sentence => {
      if (cancelled) {
        errors.push('audio_enqueued_after_cancel')
        return
      }
      decoded.push(sentence.audio.duration)
      clock.push({unitId: sentence.identity?.unitId ?? String(sentence.index),
        identity: sentence.identity, buffer: sentence.audio})
    },
    onDisplay: span => { spans.push({...span}); ledger.add(span) },
    onError: error => errors.push(error.name),
    onSegmentError: error => errors.push(error.name),
    onDone: () => { clock.closeInput(); doneResolve() },
  })
  clock.arm()
  pipeline.feedback('playback.credit', {seconds: 15, units: 8})
  const timer = setInterval(() => {
    const progress = clock.progress()
    acks.push({at: ctx.currentTime, ...progress})
    pipeline.feedback('playback.ack', progress)
  }, 100)
  const timeout = setTimeout(() => { errors.push('timeout'); pipeline.cancel(); clock.stop('timeout') }, 180000)
  try {
    for (let i = 0; i < text.length && !cancelled; i += 5) {
      pipeline.appendText(text.slice(i, i + 5))
      await new Promise(resolve => setTimeout(resolve, 80))
    }
    if (!cancelled) pipeline.finish()
    await done
    await clock.drained
    const stopped = new Promise<void>(resolve => { recorder.onstop = () => resolve() })
    recorder.stop()
    await stopped
    const blob = new Blob(recorded, {type: recorder.mimeType})
    const bytes = new Uint8Array(await blob.arrayBuffer())
    let binary = ''
    for (let i = 0; i < bytes.length; i += 8192)
      binary += String.fromCharCode(...bytes.subarray(i, i + 8192))
    return {contextState: ctx.state, sampleRate: ctx.sampleRate, decoded, events, acks,
      spans, display: ledger.text(), stats: clock.stats(), progress: clock.progress(), errors,
      outputGraphBase64: btoa(binary), outputGraphBytes: bytes.length, outputGraphMime: recorder.mimeType,
      cancelled, source: text, scope: 'repository pipeline/clock/ledger inside existing Electron renderer; separate from chat UI'}
  }
  finally {
    clearInterval(timer)
    clearTimeout(timeout)
    pipeline.cancel()
    clock.stop('cleanup')
    if (recorder.state !== 'inactive') recorder.stop()
    for (const track of capture.stream.getTracks()) track.stop()
    await ctx.close()
  }
}
