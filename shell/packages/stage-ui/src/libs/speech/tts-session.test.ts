import type { IntentOptions, PlaybackItem } from '@proj-airi/pipelines-audio'

import type { PlaybackManagerSubset, StreamingSessionSnapshot } from './tts-session'

import { describe, expect, it, vi } from 'vitest'

import { createStageTtsSession, createStreamingTtsSession } from './tts-session'

// Lightweight IntentHandle stub. We do not import the real one from
// `@proj-airi/pipelines-audio` because the segmenter adapter only needs a
// fixed subset, and constructing a full IntentHandle would drag in the
// segmenter pipeline.
function makeIntentStub(overrides: Partial<{ intentId: string }> = {}) {
  return {
    intentId: overrides.intentId ?? 'segmenter-intent-1',
    streamId: 'stream-id',
    priority: 0,
    writeLiteral: vi.fn<(text: string) => void>(),
    writeSpecial: vi.fn<(special: string) => void>(),
    writeFlush: vi.fn<() => void>(),
    end: vi.fn<() => void>(),
    cancel: vi.fn<(reason?: string) => void>(),
    // `IntentHandle` also carries a `stream: ReadableStream<TextToken>`
    // field; the adapter never touches it, so we stub it as never.
    stream: undefined as never,
  }
}

function makePlaybackManagerStub<TAudio = AudioBuffer>(): PlaybackManagerSubset<TAudio> & {
  scheduled: Array<PlaybackItem<TAudio>>
  cancellations: Array<{ intentId: string, reason: string }>
} {
  const scheduled: Array<PlaybackItem<TAudio>> = []
  const cancellations: Array<{ intentId: string, reason: string }> = []
  return {
    schedule: vi.fn((item: PlaybackItem<TAudio>) => {
      scheduled.push(item)
    }),
    stopByIntent: vi.fn((intentId: string, reason: string) => {
      cancellations.push({ intentId, reason })
    }),
    scheduled,
    cancellations,
  }
}

function makeStreamingSnapshot(overrides: Partial<StreamingSessionSnapshot> = {}): StreamingSessionSnapshot {
  return {
    model: 'volcengine/seed-tts-2.0',
    voice: 'mock-voice',
    voiceType: 'official_selected',
    bufferEntireSession: false,
    extraBody: { api_resource_id: 'seed-tts-2.0' },
    ownerId: 'card-1',
    onImmediateSpecial: vi.fn(),
    ...overrides,
  }
}

// Stub for the streaming pipeline factory. Captures the callbacks so the
// test can drive `onSentence` / `onError` / `onDone` directly. Tracks
// `appendText` / `finish` / `cancel` invocations.
function makePipelineStub() {
  const calls: { appendText: string[], finish: number, cancel: number } = {
    appendText: [],
    finish: 0,
    cancel: 0,
  }
  let captured: any
  const factory = vi.fn((options: any) => {
    captured = options
    return {
      appendText: (text: string) => {
        calls.appendText.push(text)
      },
      finish: () => {
        calls.finish += 1
      },
      cancel: () => {
        calls.cancel += 1
      },
    }
  })
  return {
    factory,
    calls,
    get options() { return captured },
  }
}

const dummyAudioContext = { sampleRate: 24000 } as unknown as BaseAudioContext

/** 流式用例用的假上下文：`currentTime` + `createBufferSource` + `createGain`（时钟要建段增益）。 */
function makeStreamAudioContext() {
  return {
    currentTime: 0,
    sampleRate: 24000,
    state: 'running' as AudioContextState,
    createBufferSource: () => ({
      buffer: null,
      connect: vi.fn(),
      disconnect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
      onended: null,
    }),
    createGain: () => ({
      connect: vi.fn(),
      disconnect: vi.fn(),
      gain: {
        value: 1,
        cancelScheduledValues: vi.fn(),
        setValueAtTime: vi.fn(),
        linearRampToValueAtTime: vi.fn(),
      },
    }),
  } as unknown as BaseAudioContext
}

describe('createStageTtsSession (factory)', () => {
  it('returns the segmenter adapter when transport is "rest"', () => {
    const intent = makeIntentStub({ intentId: 'segmenter-1' })
    const playback = makePlaybackManagerStub()
    const session = createStageTtsSession({
      transport: 'rest',
      streaming: () => null,
      audioContext: dummyAudioContext,
      playbackManager: playback,
      openIntent: vi.fn(() => intent),
      intentOptions: () => ({ ownerId: 'card-1', priority: 'normal', behavior: 'queue' } as IntentOptions),
    })

    session.appendText('hello')
    session.appendSpecial('emotion:happy')
    session.finishInput()
    session.end()
    session.cancel('done')

    expect(session.intentId).toBe('segmenter-1')
    expect(intent.writeLiteral).toHaveBeenCalledWith('hello')
    expect(intent.writeSpecial).toHaveBeenCalledWith('emotion:happy')
    expect(intent.writeFlush).toHaveBeenCalled()
    expect(intent.end).toHaveBeenCalled()
    expect(intent.cancel).toHaveBeenCalledWith('done')
    expect(playback.scheduled).toHaveLength(0)
  })

  it('returns the segmenter adapter when transport is undefined (REST default)', () => {
    const intent = makeIntentStub({ intentId: 'segmenter-default' })
    const session = createStageTtsSession({
      transport: undefined,
      streaming: () => makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      openIntent: vi.fn(() => intent),
      intentOptions: () => ({ ownerId: 'card-1', priority: 'normal', behavior: 'queue' } as IntentOptions),
    })

    expect(session.intentId).toBe('segmenter-default')
  })

  it('falls back to segmenter when bidirectional-ws snapshot is missing', () => {
    const intent = makeIntentStub({ intentId: 'segmenter-fallback' })
    const playback = makePlaybackManagerStub()
    const session = createStageTtsSession({
      transport: 'bidirectional-ws',
      streaming: () => null, // No snapshot → fallback.
      audioContext: dummyAudioContext,
      playbackManager: playback,
      openIntent: vi.fn(() => intent),
      intentOptions: () => ({ ownerId: 'card-1', priority: 'normal', behavior: 'queue' } as IntentOptions),
    })

    expect(session.intentId).toBe('segmenter-fallback')
    session.appendText('hi')
    expect(intent.writeLiteral).toHaveBeenCalledWith('hi')
  })

  it('falls back to segmenter when audioContext is undefined', () => {
    const intent = makeIntentStub({ intentId: 'segmenter-no-ctx' })
    const session = createStageTtsSession({
      transport: 'bidirectional-ws',
      streaming: () => makeStreamingSnapshot(),
      audioContext: undefined,
      playbackManager: makePlaybackManagerStub(),
      openIntent: vi.fn(() => intent),
      intentOptions: () => ({ ownerId: 'card-1', priority: 'normal', behavior: 'queue' } as IntentOptions),
    })

    expect(session.intentId).toBe('segmenter-no-ctx')
  })

  it('falls back to segmenter when snapshot.voice is empty', () => {
    const intent = makeIntentStub({ intentId: 'segmenter-no-voice' })
    const session = createStageTtsSession({
      transport: 'bidirectional-ws',
      streaming: () => makeStreamingSnapshot({ voice: '' }),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      openIntent: vi.fn(() => intent),
      intentOptions: () => ({ ownerId: 'card-1', priority: 'normal', behavior: 'queue' } as IntentOptions),
    })

    expect(session.intentId).toBe('segmenter-no-voice')
  })
})

describe('createStreamingTtsSession (adapter)', () => {
  it('一个 turn 只提交一个 M item，段交给时钟（不再一段一个 item）', () => {
    const playback = makePlaybackManagerStub()
    const pipe = makePipelineStub()
    const snap = makeStreamingSnapshot()
    const ctx = makeStreamAudioContext()

    createStreamingTtsSession({
      intentId: 'stream-abc',
      snapshot: snap,
      audioContext: ctx,
      playbackManager: playback,
      pipelineFactory: pipe.factory as any,
    })

    expect(pipe.options.ttsVoiceType).toBe('official_selected')

    // Simulate the pipeline emitting two sentences.
    const audio0 = { duration: 1 } as unknown as AudioBuffer
    const audio1 = { duration: 2 } as unknown as AudioBuffer
    pipe.options.onSentence({ index: 0, text: 'first', audio: audio0 })

    expect(playback.scheduled).toHaveLength(1)
    expect(playback.scheduled[0]).toMatchObject({
      id: 'stream-abc-stream',
      streamId: 'stream-abc',
      intentId: 'stream-abc',
      segmentId: 'stream-abc-stream',
      sequence: 0,
      ownerId: 'card-1',
      kind: 'stream',
    })
    expect(playback.scheduled[0].audio).toBeUndefined()     // 不拿空音频冒充 AudioBuffer

    const clock = (playback.scheduled[0] as any).clock
    expect(clock.stats().spansScheduled).toBe(0)            // 宿主还没开门：只排队、不建节点

    pipe.options.onSentence({ index: 1, text: 'second', audio: audio1 })
    expect(playback.scheduled).toHaveLength(1)              // 第二个段不再提交 item

    clock.arm()                                            // = 宿主 play() 拿到所有权
    expect(clock.stats().spansScheduled).toBe(2)            // 两段都进了同一条时间轴
    expect(clock.progress()).toEqual({ seconds: 0, units: 0 })
  })

  it('forwards appendText / finishInput to the pipeline', () => {
    const pipe = makePipelineStub()
    const session = createStreamingTtsSession({
      intentId: 'stream-x',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      pipelineFactory: pipe.factory as any,
    })

    session.appendText('hello')
    session.appendText('world')
    session.finishInput()

    expect(pipe.calls.appendText).toEqual(['hello', 'world'])
    expect(pipe.calls.finish).toBe(1)
  })

  it('appendSpecial fires the host immediate-special callback', () => {
    const pipe = makePipelineStub()
    const onSpecial = vi.fn()
    const session = createStreamingTtsSession({
      intentId: 'stream-special',
      snapshot: makeStreamingSnapshot({ onImmediateSpecial: onSpecial }),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      pipelineFactory: pipe.factory as any,
    })

    session.appendSpecial('emotion:angry')
    session.appendSpecial('delay:500')

    expect(onSpecial).toHaveBeenCalledTimes(2)
    expect(onSpecial).toHaveBeenNthCalledWith(1, 'emotion:angry')
    expect(onSpecial).toHaveBeenNthCalledWith(2, 'delay:500')
    // 没有 `specialToText`（其它 provider）→ 一个字节也不往文字流里加
    expect(pipe.calls.appendText).toEqual([])
  })

  it('specialToText 转发的标记：先交给舞台，再原样进文字流（阿尔比恩 ACT 先验）', () => {
    const pipe = makePipelineStub()
    const onSpecial = vi.fn()
    const act = '<|ACT:{"emotion":"sad"}|>'
    const session = createStreamingTtsSession({
      intentId: 'stream-act',
      snapshot: makeStreamingSnapshot({
        onImmediateSpecial: onSpecial,
        // 只放行 ACT，别的（emotion:/delay:）照旧只给舞台
        specialToText: special => special === act ? special : null,
      }),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      pipelineFactory: pipe.factory as any,
    })

    session.appendText('你好')
    session.appendSpecial(act)
    session.appendSpecial('delay:500')
    session.appendText('呀')

    expect(onSpecial).toHaveBeenCalledTimes(2)
    // 顺序：ACT 原串夹在两段文字中间（9885 靠位置算 source_prefix）
    expect(pipe.calls.appendText).toEqual(['你好', act, '呀'])
  })

  it('cancel sends pipeline cancel AND stops playback by intent', () => {
    const playback = makePlaybackManagerStub()
    const pipe = makePipelineStub()
    const session = createStreamingTtsSession({
      intentId: 'stream-cancel',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: playback,
      pipelineFactory: pipe.factory as any,
    })

    session.cancel('user-aborted')

    expect(pipe.calls.cancel).toBe(1)
    expect(playback.cancellations).toEqual([
      { intentId: 'stream-cancel', reason: 'user-aborted' },
    ])
  })

  it('cancel after pipeline terminated still drains playback', () => {
    const playback = makePlaybackManagerStub()
    const pipe = makePipelineStub()
    const session = createStreamingTtsSession({
      intentId: 'stream-late',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: playback,
      pipelineFactory: pipe.factory as any,
    })

    // Pipeline naturally completes first.
    pipe.options.onDone()
    // Then host cancels — pipeline.cancel should NOT be re-called, but
    // any straggler playback items must still be drained.
    session.cancel('post-done-cancel')

    expect(pipe.calls.cancel).toBe(0)
    expect(playback.cancellations).toEqual([
      { intentId: 'stream-late', reason: 'post-done-cancel' },
    ])
  })

  it('onSentence is dropped after pipeline terminated', () => {
    const playback = makePlaybackManagerStub()
    const pipe = makePipelineStub()
    createStreamingTtsSession({
      intentId: 'stream-after-done',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: playback,
      pipelineFactory: pipe.factory as any,
    })

    // Mark terminated, then a straggler sentence arrives.
    pipe.options.onDone()
    pipe.options.onSentence({ index: 0, text: 'too late', audio: {} as AudioBuffer })

    expect(playback.scheduled).toHaveLength(0)
  })

  it('hooks.onError fires on pipeline error', () => {
    const onError = vi.fn()
    const onDone = vi.fn()
    const pipe = makePipelineStub()
    createStreamingTtsSession({
      intentId: 'stream-err',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      hooks: { onError, onDone },
      pipelineFactory: pipe.factory as any,
    })

    const err = new Error('boom')
    pipe.options.onError(err)
    pipe.options.onDone()

    expect(onError).toHaveBeenCalledWith(err)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('单段错误的回退：没给 onSegmentError 时仍走 hooks.onError，给了则只走单段那条', () => {
    // 适配器必须**原样透传** `hooks.onSegmentError`。包一层就等于"永远提供了单段回调"，
    // 管道会一直走单段分支 —— 没给这个回调的调用方从此收不到任何错误（codex 复核抓到的回归）。
    const onError = vi.fn()
    const bare = makePipelineStub()
    createStreamingTtsSession({
      intentId: 'stream-seg-bare',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      hooks: { onError },
      pipelineFactory: bare.factory as any,
    })
    expect(bare.options.onSegmentError).toBeUndefined()

    const onSegmentError = vi.fn()
    const withSeg = makePipelineStub()
    createStreamingTtsSession({
      intentId: 'stream-seg-given',
      snapshot: makeStreamingSnapshot(),
      audioContext: dummyAudioContext,
      playbackManager: makePlaybackManagerStub(),
      hooks: { onError, onSegmentError },
      pipelineFactory: withSeg.factory as any,
    })

    const err = new Error('bad segment')
    withSeg.options.onSegmentError!(err)
    expect(onSegmentError).toHaveBeenCalledWith(err)
    expect(onError).not.toHaveBeenCalled()      // 给了单段回调就不该再惊动会话级那条
  })

  // sol-high 方案 §三：服务端句组身份（`session_id/generation/unit_id/sub_index`）要一路
  // 留到 clock，别被本地 index 替换 —— 否则"服务端渲染了几个单元 / 客户端播了几段"
  // 永远对不上号，丢段与合并都无法归因。（sol-medium 二审要求四个字段原样到手，不是只留个名字。）
  interface CapturedSpan {
    unitId: string
    subIndex?: number
    identity?: unknown
    intentId?: string
  }

  function makeSpanRecorder() {
    const spans: CapturedSpan[] = []
    return { spans, hooks: { onSegmentStart: (e: { span: CapturedSpan }) => spans.push(e.span) } }
  }

  /** 建一个会话、送一段、把时钟推过 lead，返回这一段的 span。 */
  function runOneSegment(intentId: string, sentence: Record<string, unknown>) {
    const pipe = makePipelineStub()
    const playback = makePlaybackManagerStub()
    const ctx = makeStreamAudioContext()
    const rec = makeSpanRecorder()

    createStreamingTtsSession({
      intentId,
      snapshot: makeStreamingSnapshot(),
      audioContext: ctx,
      playbackManager: playback,
      hooks: rec.hooks,
      pipelineFactory: pipe.factory as any,
    })

    const audio = { duration: 1 } as unknown as AudioBuffer
    pipe.options.onSentence({ index: 0, text: 'x', audio, ...sentence })
    const clock = (playback.scheduled[0] as any).clock
    clock.arm()
    ;(ctx as unknown as { currentTime: number }).currentTime = 0.06     // 跨过 startAt=0.05
    return { rec, clock }
  }

  it('身份透传：四个服务端字段原样进 span，段名用 unit_id[:sub_index]，不被本地 index 替换', () => {
    vi.useFakeTimers()
    try {
      const { rec, clock } = runOneSegment('stream-id', {
        identity: { unitId: 'sid:1:2', subIndex: 3, sessionId: 'sid', generation: 1 },
      })
      vi.advanceTimersByTime(200)

      expect(clock.stats().spansScheduled).toBe(1)
      expect(rec.spans).toHaveLength(1)
      expect(rec.spans[0].unitId).toBe('sid:1:2:3')          // 不是 'stream-id-0'
      expect(rec.spans[0].subIndex).toBe(3)
      expect(rec.spans[0].identity).toEqual({ unitId: 'sid:1:2', subIndex: 3, sessionId: 'sid', generation: 1 })
      expect(rec.spans[0].intentId).toBe('stream-id')
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('服务端没给身份时退回本地编号（行为不变，且不写身份）', () => {
    vi.useFakeTimers()
    try {
      const { rec } = runOneSegment('stream-noid', {})
      vi.advanceTimersByTime(200)

      expect(rec.spans[0].unitId).toBe('stream-noid-0')
      expect(rec.spans[0].identity).toBeUndefined()
      expect(rec.spans[0].intentId).toBe('stream-noid')
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('两个 intent 收到同一个服务端 unit_id 时，靠 intentId 区分（不靠拆冒号）', () => {
    vi.useFakeTimers()
    try {
      const shared = { sessionId: 'S', generation: 1, unitId: 'U' }
      const a = runOneSegment('intent-a', { identity: shared })
      const b = runOneSegment('intent-b', { identity: shared })
      vi.advanceTimersByTime(200)

      expect([a.rec.spans[0].unitId, b.rec.spans[0].unitId]).toEqual(['U', 'U'])   // 同名允许
      expect([a.rec.spans[0].intentId, b.rec.spans[0].intentId]).toEqual(['intent-a', 'intent-b'])
      expect(a.rec.spans[0].identity).toEqual(shared)
      expect(b.rec.spans[0].identity).toEqual(shared)
    }
    finally {
      vi.useRealTimers()
    }
  })
})
