import type { AddressInfo } from 'node:net'

import { Buffer } from 'node:buffer'
import { createServer } from 'node:http'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WebSocketServer } from 'ws'

import { createStreamingTtsPipeline } from './streaming-pipeline'

vi.mock('../auth', () => ({
  getAuthToken: () => 'test-jwt',
}))
vi.mock('../server', () => ({
  SERVER_URL: 'http://placeholder',
}))

// NOTICE:
// vi.waitFor 的超时只是失败时的上限，成功会立即返回。
// 旧的 1.5s / 2s 上限在全量 stage-ui node 测试并发负载下偶发超时（单独跑 19/19 通过，
// 全量第一次跑 1 条 WAV 用例失败，重跑 857/857 通过；见 tests/output/shell-s7c/IMPL.md）。
// 放宽到 5s 只是给慢机器留余量，不改变任何断言。
// 删除条件：确认该文件在全量负载下稳定通过且不再需要余量时，可收回。
const WAIT_TIMEOUT_MS = 5000

interface MockServer {
  url: string
  receivedFrames: Array<{ kind: 'text' | 'binary', data: string | Buffer }>
  observedVoiceTypes: string[]
  /** Resolves when the server has observed a `start` frame from the client. */
  startObserved: Promise<void>
  stop: () => Promise<void>
}

async function startMockServer(handler: (ws: import('ws').WebSocket) => void): Promise<MockServer> {
  const receivedFrames: MockServer['receivedFrames'] = []
  const observedVoiceTypes: string[] = []
  const httpServer = createServer()
  const wss = new WebSocketServer({ server: httpServer })

  let resolveStartObserved!: () => void
  const startObserved = new Promise<void>((res) => {
    resolveStartObserved = res
  })

  wss.on('connection', (ws, req) => {
    const u = new URL(req.url!, 'http://localhost')
    const voiceType = u.searchParams.get('tts_voice_type')
    if (voiceType != null)
      observedVoiceTypes.push(voiceType)

    ws.on('message', (data, isBinary) => {
      const buf = Buffer.isBuffer(data) ? data : Buffer.from(data as ArrayBuffer)
      const decoded = isBinary ? buf : buf.toString('utf8')
      receivedFrames.push({ kind: isBinary ? 'binary' : 'text', data: isBinary ? buf : (decoded as string) })
      if (!isBinary) {
        try {
          const ev = JSON.parse(decoded as string) as { event?: string }
          if (ev.event === 'start')
            resolveStartObserved()
        }
        catch {}
      }
    })
    handler(ws)
  })

  await new Promise<void>(resolve => httpServer.listen(0, '127.0.0.1', resolve))
  const { port } = httpServer.address() as AddressInfo

  return {
    url: `http://127.0.0.1:${port}`,
    receivedFrames,
    observedVoiceTypes,
    startObserved,
    async stop() {
      wss.close()
      await new Promise<void>(r => httpServer.close(() => r()))
    },
  }
}

// jsdom-friendly stub AudioContext for `decodeAudioData`. The pipeline does
// not introspect the AudioBuffer beyond passing it to consumers, so any
// shape with the expected fields is fine.
function makeStubAudioContext(): BaseAudioContext {
  let counter = 0
  const ctx = {
    sampleRate: 24000,
    decodeAudioData: vi.fn(async (buf: ArrayBuffer) => {
      // Return a fake AudioBuffer-like object identifiable by index/byteLength.
      counter += 1
      return {
        duration: buf.byteLength / 24000,
        length: buf.byteLength,
        numberOfChannels: 1,
        sampleRate: 24000,
        __index: counter,
        __byteLength: buf.byteLength,
      } as unknown as AudioBuffer
    }),
  }
  return ctx as unknown as BaseAudioContext
}

// 真实有效的 PCM WAV 构造（按 RIFF/WAVE 最小结构拼）：守卫是**结构校验**，
// 测试材料也必须是真 WAV，否则测不出"合法音频不会被误拒"。
function u32le(n: number): Buffer {
  const b = Buffer.alloc(4)
  b.writeUInt32LE(n >>> 0, 0)
  return b
}

function buildWav(pcmData: Uint8Array): Buffer {
  const fmtBody = Buffer.alloc(16)
  fmtBody.writeUInt16LE(1, 0)
  fmtBody.writeUInt16LE(1, 2)
  fmtBody.writeUInt32LE(24000, 4)
  fmtBody.writeUInt32LE(48000, 8)
  fmtBody.writeUInt16LE(2, 12)
  fmtBody.writeUInt16LE(16, 14)
  const fmt = Buffer.concat([Buffer.from('fmt '), u32le(16), fmtBody])
  const pad = pcmData.length % 2 ? Buffer.alloc(1) : Buffer.alloc(0)
  const data = Buffer.concat([Buffer.from('data'), u32le(pcmData.length), Buffer.from(pcmData), pad])
  const body = Buffer.concat([Buffer.from('WAVE'), fmt, data])
  return Buffer.concat([Buffer.from('RIFF'), u32le(body.length), body])
}

function pcm(n: number, fill = 0): Uint8Array {
  return new Uint8Array(n).fill(fill)
}


describe('createStreamingTtsPipeline', () => {
  let server: MockServer | undefined

  beforeEach(() => {
    server = undefined
  })
  afterEach(async () => {
    await server?.stop()
  })

  it('forwards appendText / finish frames and chunks audio per sentence.end', async () => {
    const chunks = [Buffer.from([1, 2, 3, 4]), Buffer.from([5, 6, 7, 8]), Buffer.from([9, 10, 11, 12])]
    server = await startMockServer((ws) => {
      ws.on('message', async (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event === 'finish') {
          // First sentence: chunk1 + chunk2 → sentence.end
          ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: 'first one.' } }))
          ws.send(chunks[0], { binary: true })
          ws.send(chunks[1], { binary: true })
          ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: 'first one.' } }))
          // Second sentence: chunk3 → sentence.end
          ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: 'second sentence.' } }))
          ws.send(chunks[2], { binary: true })
          ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: 'second sentence.' } }))
          ws.send(JSON.stringify({ event: 'session.finished', payload: { usage: { text_words: 4 } } }))
        }
      })
    })

    const onSentence = vi.fn()
    const onError = vi.fn()
    const onDone = vi.fn()

    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'volcengine/seed-tts-1.0',
      voice: 'mock',
      ttsVoiceType: 'official_selected',
      audioContext: makeStubAudioContext(),
      onSentence,
      onError,
      onDone,
    })

    handle.appendText('hi ')
    handle.appendText('there')
    handle.finish()

    // Wait for done.
    // 超时不能当作"等到了"：改成等待完成断言；到时间没完成就明确失败。
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    await server.startObserved
    const textFrames = server.receivedFrames.filter(f => f.kind === 'text').map(f => JSON.parse(f.data as string))
    expect(textFrames.map(f => f.event)).toEqual(['start', 'text', 'text', 'finish'])
    expect(server.observedVoiceTypes).toEqual(['official_selected'])
    expect(textFrames[1]).toMatchObject({ event: 'text', text: 'hi ' })
    expect(textFrames[2]).toMatchObject({ event: 'text', text: 'there' })

    expect(onError).not.toHaveBeenCalled()
    // Two `sentence.end` events → two AudioBuffers.
    expect(onSentence).toHaveBeenCalledTimes(2)
    const calls = onSentence.mock.calls.map(([s]) => s as { index: number, text: string, audio: { __byteLength: number } })
    expect(calls[0]).toMatchObject({ index: 0, text: 'first one.' })
    expect(calls[0].audio.__byteLength).toBe(chunks[0].length + chunks[1].length)
    expect(calls[1]).toMatchObject({ index: 1, text: 'second sentence.' })
    expect(calls[1].audio.__byteLength).toBe(chunks[2].length)
  })

  it('buffers entire session when bufferEntireSession is true', async () => {
    const chunks = [Buffer.from([1, 2, 3, 4]), Buffer.from([5, 6, 7, 8])]
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event === 'finish') {
          // Two sentences with sentence.end events — but the pipeline should
          // IGNORE them in buffered mode (TTS 2.0 ships subtitles async).
          ws.send(chunks[0], { binary: true })
          ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: 'sentence 1' } }))
          ws.send(chunks[1], { binary: true })
          ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: 'sentence 2' } }))
          ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
        }
      })
    })

    const onSentence = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'volcengine/seed-tts-2.0',
      voice: 'mock',
      audioContext: makeStubAudioContext(),
      bufferEntireSession: true,
      onSentence,
    })

    handle.finish()

    await new Promise<void>(resolve => setTimeout(resolve, 800))

    expect(onSentence).toHaveBeenCalledTimes(1)
    const [sentence] = onSentence.mock.calls[0] as [{ index: number, audio: { __byteLength: number } }]
    expect(sentence.index).toBe(0)
    expect(sentence.audio.__byteLength).toBe(chunks[0].length + chunks[1].length)
  })

  it('surfaces upstream error event then closes', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event === 'start') {
          ws.send(JSON.stringify({ event: 'error', code: 'insufficient_flux', message: 'top up' }))
        }
      })
    })

    const onError = vi.fn()
    const onDone = vi.fn()
    createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'volcengine/seed-tts-1.0',
      voice: 'mock',
      audioContext: makeStubAudioContext(),
      onError,
      onDone,
    })

    // 超时不能当作"等到了"：改成等待完成断言；到时间没完成就明确失败。
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    expect(onError).toHaveBeenCalledTimes(1)
    expect((onError.mock.calls[0][0] as Error).message).toMatch(/insufficient_flux.*top up/)
  })

  it('surfaces close-before-finished as error', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event === 'start') {
          // Drop ws without sending session.finished.
          setTimeout(() => ws.close(1011, 'simulated_truncation'), 10)
        }
      })
    })

    const onError = vi.fn()
    const onDone = vi.fn()
    createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'volcengine/seed-tts-1.0',
      voice: 'mock',
      audioContext: makeStubAudioContext(),
      onError,
      onDone,
    })

    // 超时不能当作"等到了"：改成等待完成断言；到时间没完成就明确失败。
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    expect(onError).toHaveBeenCalledTimes(1)
    expect((onError.mock.calls[0][0] as Error).message).toMatch(/streaming_tts_closed/)
  })

  it('cancel() sends cancel frame and terminates', async () => {
    let cancelObserved = false
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event === 'cancel')
          cancelObserved = true
      })
    })

    const onDone = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'volcengine/seed-tts-1.0',
      voice: 'mock',
      audioContext: makeStubAudioContext(),
      onDone,
    })

    await server.startObserved
    handle.cancel()

    // ROOT CAUSE:
    //
    // This test was flaky on CI: asserting `cancelObserved` right after `onDone`
    // resolved raced the mock server's `message` event.
    // `cancel()` queues the cancel frame in the ws write buffer, then `terminate()`
    // defers `ws.close()` + `onDone()` by one macrotask (streaming-pipeline.ts) —
    // but the frame still has to cross a real loopback socket and be dispatched to
    // the server's `message` listener, which on a loaded runner can happen AFTER
    // the client-side `onDone` fired.
    //
    // We fixed this by polling for both observations instead of asserting
    // immediately after `onDone`.
    await vi.waitFor(() => {
      expect(cancelObserved).toBe(true)
      expect(onDone).toHaveBeenCalledTimes(1)
    }, { interval: 10, timeout: WAIT_TIMEOUT_MS })
  })

  it('display.span 给显示资格：无音频段 eligible=immediate 且不进 decode；有声段在 sentence.start 之前', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        // 无音频段（纯心声）：只有 display.span + unit.no_audio —— 一个 sentence.* / 二进制都没有
        ws.send(JSON.stringify({
          event: 'display.span',
          payload: { unit_id: 'u0', source_unit_id: 'u0', source_range: [0, 2], text: '……', eligible: 'immediate' },
        }))
        ws.send(JSON.stringify({
          event: 'unit.no_audio',
          payload: { unit_id: 'u0', source_range: [0, 2], text: '……', reason: 'moan_only' },
        }))
        // 有声段：display.span(eligible=audio) 紧接在 sentence.start 之前
        ws.send(JSON.stringify({
          event: 'display.span',
          payload: { unit_id: 'u1', source_unit_id: 'u1', source_range: [2, 8], text: '你好呀。', eligible: 'audio' },
        }))
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { unit_id: 'u1', text: '你好呀。', format: 'wav' } }))
        ws.send(buildWav(pcm(64)), { binary: true })
        ws.send(JSON.stringify({
          event: 'sentence.end',
          payload: { unit_id: 'u1', text: '你好呀。', frames: 1, samples: 4, sr: 24000 },
        }))
        ws.send(JSON.stringify({ event: 'session.finished', payload: { units: 2, audio: 1, no_audio: 1 } }))
      })
    })

    const onDisplay = vi.fn()
    const onSentence = vi.fn()
    const onError = vi.fn()
    const onDone = vi.fn()
    const audioContext = makeStubAudioContext()

    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      responseFormat: 'wav',
      audioContext,
      onDisplay,
      onSentence,
      onError,
      onDone,
    })

    handle.appendText('……你好呀。')
    handle.finish()

    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    expect(onDisplay.mock.calls.map(c => c[0])).toEqual([
      expect.objectContaining({ unitId: 'u0', sourceRange: [0, 2], eligible: 'immediate' }),
      expect.objectContaining({ unitId: 'u1', sourceRange: [2, 8], eligible: 'audio' }),
    ])
    // 关键：无音频段一个字节都没 decode（P:188 的零字节 return 只省帧，不省 decode）
    expect(onSentence).toHaveBeenCalledTimes(1)
    expect(audioContext.decodeAudioData).toHaveBeenCalledTimes(1)
    expect(onError).not.toHaveBeenCalled()
  })

  it('坏 display.span 一律丢弃：未知 eligible / NaN / 负数 / 倒挂区间都不给显示资格', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        const bad = [
          { unit_id: 'b0', source_unit_id: 'b0', source_range: [0, 4], text: 'x', eligible: 'AUDIO' },
          { unit_id: 'b1', source_unit_id: 'b1', source_range: [0, Number.NaN], text: 'x', eligible: 'audio' },
          { unit_id: 'b2', source_unit_id: 'b2', source_range: [-2, 4], text: 'x', eligible: 'audio' },
          { unit_id: 'b3', source_unit_id: 'b3', source_range: [9, 3], text: 'x', eligible: 'immediate' },
          { unit_id: 'b4', source_unit_id: 'b4', source_range: [1, 1], text: 'x', eligible: 'audio' },
          { unit_id: 'b5', source_unit_id: 'b5', source_range: '0,4', text: 'x', eligible: 'audio' },
          { unit_id: 'b6', source_unit_id: 'b6', source_range: [0.5, 4], text: 'x', eligible: 'audio' },
          { unit_id: '', source_unit_id: '', source_range: [0, 4], text: 'x', eligible: 'audio' },
          { unit_id: 'b8', source_unit_id: 'b8', source_range: [0, Number.POSITIVE_INFINITY], text: 'x', eligible: 'immediate' },
        ]
        for (const payload of bad)
          ws.send(JSON.stringify({ event: 'display.span', payload }))
        ws.send(JSON.stringify({
          event: 'display.span',
          payload: { unit_id: 'ok', source_unit_id: 'ok', source_range: [0, 4], text: '好', eligible: 'immediate' },
        }))
        ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
      })
    })

    const onDisplay = vi.fn()
    const onDone = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      audioContext: makeStubAudioContext(),
      onDisplay,
      onDone,
    })

    handle.appendText('hi')
    handle.finish()

    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    expect(onDisplay).toHaveBeenCalledTimes(1)
    expect(onDisplay.mock.calls[0][0]).toMatchObject({ unitId: 'ok', sourceRange: [0, 4], eligible: 'immediate' })
  })

  it('句级文本不残留：end 自带文本时也要出队，否则下一个缺文本的 end 会取到陈旧句子', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        // 第一句：start/end 都带文本（v2 的常态）；第二句：end **不带**文本（旧桥/异常路径）
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '第一句。' } }))
        ws.send(Buffer.from([1, 2, 3, 4]), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '第一句。' } }))
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '第二句。' } }))
        ws.send(Buffer.from([5, 6, 7, 8]), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: {} }))
        ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
      })
    })

    const onSentence = vi.fn()
    const onDone = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      audioContext: makeStubAudioContext(),
      onSentence,
      onDone,
    })

    handle.appendText('hi')
    handle.finish()

    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    expect(onSentence.mock.calls.map(c => c[0].text)).toEqual(['第一句。', '第二句。'])
  })

  it('feedback 走同一条 ws 上行：credit / ack 原样到达服务端（P6 限流通道）', async () => {
    server = await startMockServer(() => {})
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      responseFormat: 'wav',
      audioContext: makeStubAudioContext(),
    })

    await server.startObserved
    handle.feedback('playback.credit', { seconds: 15, units: 8 })
    handle.feedback('playback.ack', { seconds: 3.5 })
    handle.cancel() // 收工：把 ws 关掉，否则 afterEach 的 server.stop() 会等着这条连接

    const frames = () => server!.receivedFrames.filter(f => f.kind === 'text').map(f => JSON.parse(f.data as string))
    await vi.waitFor(() => expect(frames().filter(f => f.event === 'playback.ack')).toHaveLength(1),
      { interval: 10, timeout: WAIT_TIMEOUT_MS })
    expect(frames()[1]).toMatchObject({ event: 'playback.credit', seconds: 15, units: 8 })
    expect(frames()[2]).toMatchObject({ event: 'playback.ack', seconds: 3.5 })
  })

  it('截断的段：decode 失败只丢这一句，不播、不炸会话（P6 限流砍单元的壳侧兜底）', async () => {
    // P6 契约里"限流可能砍掉单元"的壳侧后果：S 侧砍在**子段边界**（一份 WAV 的边界）上，
    // 所以正常情况只是"少了一段"；但字节一旦残缺（网络截断/上游给错），壳必须只丢这一句。
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '半句被砍了。' } }))
        ws.send(Buffer.from([1, 2, 3]), { binary: true })   // 残缺容器：守卫在解码前就拦下
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '半句被砍了。' } }))
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '解码器不认。' } }))
        ws.send(buildWav(pcm(128)), { binary: true })      // 合法容器（顶层 8+172），解码器自己失败
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '解码器不认。' } }))
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '这句是好的。' } }))
        ws.send(buildWav(pcm(64)), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '这句是好的。' } }))
        ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
      })
    })

    const decode = vi.fn(async (buf: ArrayBuffer) => {
      if (buf.byteLength === 172)
        throw new Error('EncodingError: 不是完整的 WAV')
      return { duration: 1, length: buf.byteLength, numberOfChannels: 1, sampleRate: 24000 } as unknown as AudioBuffer
    })
    const onSentence = vi.fn()
    const onError = vi.fn()
    const onSegmentError = vi.fn()
    const onDone = vi.fn()

    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      responseFormat: 'wav',
      audioContext: { sampleRate: 24000, decodeAudioData: decode } as unknown as BaseAudioContext,
      onSentence,
      onError,
      onSegmentError,
      onDone,
    })

    handle.appendText('半句被砍了。这句是好的。')
    handle.finish()
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    // 坏段走**单段**回调（守卫拦容器异常 / 解码器自己失败都算单段）：`onError` 是会话级（Stage 会清 currentSession），不能混用
    expect(onSegmentError).toHaveBeenCalledTimes(2)   // 守卫拦一条 + 解码器失败一条
    expect(onError).not.toHaveBeenCalled()
    expect(onSentence.mock.calls.map(c => c[0].text)).toEqual(['这句是好的。'])  // 好段照常播
  })

  it('没给 onSegmentError 时，单段解码失败仍走 onError（回退，不许把错误吞掉）', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '坏段。' } }))
        ws.send(Buffer.from([7, 7, 7]), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '坏段。' } }))
        ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
      })
    })

    const onError = vi.fn()
    const onDone = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      responseFormat: 'wav',
      audioContext: {
        sampleRate: 24000,
        decodeAudioData: async () => { throw new Error('EncodingError: 坏段') },
      } as unknown as BaseAudioContext,
      onError,
      onDone,
    })

    handle.appendText('坏段。')
    handle.finish()
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })
    expect(onError).toHaveBeenCalledTimes(1)
  })

  // sol-medium 二审（须修 1）：身份队列必须"每个 start 占一个槽"，否则无身份的 start 不占位，
  // 后面有身份的 start 会**向前错挂** —— 真实管道下 A 被标成 B、B 反而丢身份，正好污染
  // 用来排查"4→2"的取证。这里用真实管道 + 内存 WS 复现同一形状。
  it('身份队列按 start 占位：A 无身份、B 有身份 ⇒ 不能把 B 挂到 A', async () => {
    server = await startMockServer((ws) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        const idB = { session_id: 'S', generation: 1, unit_id: 'B', sub_index: 0 }
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: 'A' } }))
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: 'B', ...idB } }))
        ws.send(buildWav(pcm(64)), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: {} }))
        ws.send(buildWav(pcm(64)), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: {} }))
        ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
      })
    })

    const onSentence = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      responseFormat: 'wav',
      audioContext: makeStubAudioContext(),
      onSentence,
      onError: vi.fn(),
      onDone: vi.fn(),
    })

    handle.appendText('A B')
    handle.finish()
    await vi.waitFor(() => expect(onSentence).toHaveBeenCalledTimes(2), { interval: 10, timeout: WAIT_TIMEOUT_MS })

    const [first, second] = onSentence.mock.calls.map(c => c[0])
    expect(first.text).toBe('A')
    expect(first.identity).toBeUndefined()                        // A 不许被标成 B
    expect(second.text).toBe('B')
    expect(second.identity).toEqual({ sessionId: 'S', generation: 1, unitId: 'B', subIndex: 0 })
  })

describe('句组 WAV 容器结构校验', () => {
  // 用**真实有效的 PCM WAV** 做材料（sol-medium 拒签点：只塞 RIFF 头的假材料证明不了
  // "合法 WAV 不会被误拒"）。按 RIFF/WAVE 最小结构拼：RIFF + 顶层长度 + WAVE + fmt + data。



  async function run(handler: (ws: import('ws').WebSocket) => void, opts: Record<string, unknown> = {}) {
    server = await startMockServer(handler)
    const onSentence = vi.fn()
    const onSegmentError = vi.fn()
    const onError = vi.fn()
    const onDone = vi.fn()
    const handle = createStreamingTtsPipeline({
      serverUrl: server.url,
      model: 'albion-stream/v2',
      voice: 'albion',
      responseFormat: 'wav',
      audioContext: makeStubAudioContext(),
      onSentence,
      onSegmentError,
      onError,
      onDone,
      ...opts,
    })
    handle.appendText('x')
    handle.finish()
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { interval: 10, timeout: WAIT_TIMEOUT_MS })
    return { onSentence, onSegmentError, onError }
  }

  function finishing(sendFrames: (ws: import('ws').WebSocket) => void) {
    return (ws: import('ws').WebSocket) => {
      ws.on('message', (data, isBinary) => {
        if (isBinary)
          return
        const ev = JSON.parse(data.toString()) as { event?: string }
        if (ev.event !== 'finish')
          return
        sendFrames(ws)
        ws.send(JSON.stringify({ event: 'session.finished', payload: {} }))
      })
    }
  }

  it('合法 WAV、PCM 里恰巧含 "RIFF" 字节 ⇒ 必须允许解码（数魔数就会误拒）', async () => {
    const pcmBytes = pcm(64, 3)
    pcmBytes.set(Buffer.from('RIFF'), 20)         // 数据区里恰好有 ASCII RIFF
    const { onSentence, onSegmentError } = await run(finishing((ws) => {
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '合法句。' } }))
      ws.send(buildWav(pcmBytes), { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '合法句。' } }))
    }))

    expect(onSegmentError).not.toHaveBeenCalled()
    expect(onSentence).toHaveBeenCalledTimes(1)
  })

  it('一份合法 WAV 拆成多个二进制帧 ⇒ 拼回后只解码一次', async () => {
    const wav = buildWav(pcm(200))
    const { onSentence, onSegmentError } = await run(finishing((ws) => {
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '分段句。' } }))
      ws.send(wav.subarray(0, 30), { binary: true })
      ws.send(wav.subarray(30, 120), { binary: true })
      ws.send(wav.subarray(120), { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '分段句。' } }))
    }))

    expect(onSegmentError).not.toHaveBeenCalled()
    expect(onSentence).toHaveBeenCalledTimes(1)
  })

  it('两个完整 WAV 挤进一个句组 ⇒ 坏组单段失败、好句照常解码且身份正确', async () => {
    const { onSentence, onSegmentError, onError } = await run(finishing((ws) => {
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '双份。' } }))
      ws.send(buildWav(pcm(64)), { binary: true })
      ws.send(buildWav(pcm(64)), { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '双份。' } }))
      ws.send(JSON.stringify({
        event: 'sentence.start',
        payload: { text: '好句。', session_id: 'S', generation: 1, unit_id: 'U', sub_index: 2 },
      }))
      ws.send(buildWav(pcm(64)), { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '好句。' } }))
    }))

    expect(onSegmentError).toHaveBeenCalledTimes(1)
    expect((onSegmentError.mock.calls[0][0] as Error).message).toMatch(/not a single valid WAV/)
    expect(onError).not.toHaveBeenCalled()              // 单段错误不许升级成会话级
    expect(onSentence).toHaveBeenCalledTimes(1)
    expect(onSentence.mock.calls[0][0].text).toBe('好句。')
    expect(onSentence.mock.calls[0][0].identity).toEqual({ sessionId: 'S', generation: 1, unitId: 'U', subIndex: 2 })
  })

  it('顶层长度不符 / 子块越界 / 截断 / 缺必要块 ⇒ 都明确失败', async () => {
    const good = buildWav(pcm(64))
    const wrongTop = Buffer.from(good)
    wrongTop.writeUInt32LE(good.length, 4)               // 顶层长度多报 8 字节
    const overrun = Buffer.concat([
      Buffer.from('RIFF'), u32le(44), Buffer.from('WAVE'),
      Buffer.from('fmt '), u32le(16), Buffer.alloc(16),   // fmt 合法（24 字节）
      Buffer.from('data'), u32le(9999), Buffer.alloc(8),  // data 自称 9999 却只有 8 字节
    ])                                                      // 顶层长度自洽（8+44=52），只该被子块边界抓到
    const noData = Buffer.concat([
      Buffer.from('RIFF'), u32le(28), Buffer.from('WAVE'),
      Buffer.from('fmt '), u32le(16), Buffer.alloc(16),   // 齐全的外壳，但没有 data
    ])
    const noFmt = Buffer.concat([
      Buffer.from('RIFF'), u32le(20), Buffer.from('WAVE'),
      Buffer.from('data'), u32le(8), Buffer.alloc(8),     // 有音频没有格式块
    ])
    const truncated = good.subarray(0, 8)                // 连 RIFF/WAVE 头都没读全

    const { onSentence, onSegmentError, onError } = await run(finishing((ws) => {
      for (const [i, blob] of [wrongTop, overrun, truncated, noData, noFmt].entries()) {
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: `坏${i}` } }))
        ws.send(blob, { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: `坏${i}` } }))
      }
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '好句。' } }))
      ws.send(good, { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '好句。' } }))
    }))

    expect(onSegmentError).toHaveBeenCalledTimes(5)
    // 五条坏组走的是**五个不同分支**：别让它们都撞在顶层长度上（子块用例必须真走到子块边界）
    const reasons = onSegmentError.mock.calls.map(c => (c[0] as Error).message).join(' | ')
    expect(reasons).toMatch(/top-level length mismatch/)
    expect(reasons).toMatch(/chunk overruns the buffer/)
    expect(reasons).toMatch(/shorter than a WAV header/)
    expect(reasons).toMatch(/missing required chunk\(s\): data/)
    expect(reasons).toMatch(/missing required chunk\(s\): fmt/)
    expect(onError).not.toHaveBeenCalled()
    expect(onSentence.mock.calls.map(c => c[0].text)).toEqual(['好句。'])
  })

  it('非 WAV 格式（默认 mp3 路径）⇒ 不套单 WAV 规则', async () => {
    const { onSentence, onSegmentError } = await run(finishing((ws) => {
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '非WAV。' } }))
      ws.send(buildWav(pcm(64)), { binary: true })
      ws.send(buildWav(pcm(64)), { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '非WAV。' } }))
    }), { responseFormat: 'mp3' })

    expect(onSegmentError).not.toHaveBeenCalled()
    expect(onSentence).toHaveBeenCalledTimes(1)
  })

  it('整会话缓冲（bufferEntireSession）⇒ 不套单 WAV 句组规则', async () => {
    // 兼容性接线：缓冲模式一个整包本来就不保证"一句一份 WAV"。
    // 只验"不被守卫拦截、解码器被调用一次"，**不主张拼接 WAV 能被真实解码**。
    const merged = Buffer.concat([buildWav(pcm(64)), buildWav(pcm(64))])
    const { onSentence, onSegmentError } = await run(finishing((ws) => {
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '整包。' } }))
      ws.send(merged, { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '整包。' } }))
    }), { bufferEntireSession: true })

    expect(onSegmentError).not.toHaveBeenCalled()
    expect(onSentence).toHaveBeenCalledTimes(1)
  })

  it('fmt 子块长度不足（0 / 15 字节）⇒ 明确失败；坏组不进解码器、好句照常', async () => {
    const withFmtSize = (size: number) => {
      const body = Buffer.concat([
        Buffer.from('WAVE'),
        Buffer.from('fmt '), u32le(size), Buffer.alloc(size), size % 2 ? Buffer.alloc(1) : Buffer.alloc(0),
        Buffer.from('data'), u32le(8), Buffer.alloc(8),
      ])
      return Buffer.concat([Buffer.from('RIFF'), u32le(body.length), body])
    }
    const stub = makeStubAudioContext()
    const decode = vi.fn(stub.decodeAudioData.bind(stub))
    const { onSentence, onSegmentError, onError } = await run(finishing((ws) => {
      [0, 15].forEach((size, i) => {
        ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: `短fmt${size}` } }))
        ws.send(withFmtSize(size), { binary: true })
        ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: `短fmt${size}` } }))
      })
      ws.send(JSON.stringify({ event: 'sentence.start', payload: { text: '好句。' } }))
      ws.send(buildWav(pcm(64)), { binary: true })
      ws.send(JSON.stringify({ event: 'sentence.end', payload: { text: '好句。' } }))
    }), { audioContext: { ...stub, decodeAudioData: decode } })

    const reasons = onSegmentError.mock.calls.map(c => (c[0] as Error).message)
    expect(reasons).toHaveLength(2)
    expect(reasons.every(r => /fmt chunk is too short/.test(r))).toBe(true)   // 只命中 fmt 长度分支
    expect(onError).not.toHaveBeenCalled()
    expect(decode).toHaveBeenCalledTimes(1)                                  // 坏组一个都没进解码器
    expect(onSentence.mock.calls.map(c => c[0].text)).toEqual(['好句。'])
  })
})


})
