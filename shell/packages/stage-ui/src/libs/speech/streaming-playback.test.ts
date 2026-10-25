import type { StreamingPlaybackSpan } from './streaming-playback'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createStreamingPlayback } from './streaming-playback'

/**
 * 假 `AudioContext`：`currentTime` 由测试手动推，`createBufferSource()` 返回的节点只记录
 * `start(when)` / `stop()`，`onended` 由测试手动触发（= 段播完那一刻）。
 * 这样时间轴上的每个预定时刻都能逐位断言。
 */
interface FakeSource {
  buffer: AudioBuffer | null
  starts: number[]
  stops: number
  stopArgs: (number | undefined)[]
  connects: number
  disconnects: number
  connect: (node?: unknown) => void
  disconnect: () => void
  start: (when: number) => void
  stop: (when?: number) => void
  onended: (() => void) | null
}

/** 假增益参数：记下淡出用的是"降到 0 + 带绝对时刻"这一族调用，而不是只有个数字。 */
interface FakeParam {
  value: number
  ramps: { value: number, at: number }[]
  cancels: number
  sets: { value: number, at: number }[]
  cancelScheduledValues: (at: number) => void
  setValueAtTime: (value: number, at: number) => void
  linearRampToValueAtTime: (value: number, at: number) => void
}

interface FakeGain {
  gain: FakeParam
  connect: (node?: unknown) => void
  disconnect: () => void
  disconnects: number
}

function makeFakeParam(): FakeParam {
  return {
    value: 1,
    ramps: [],
    cancels: 0,
    sets: [],
    cancelScheduledValues(at: number) { this.cancels += 1; void at },
    setValueAtTime(value: number, at: number) { this.sets.push({ value, at }) },
    linearRampToValueAtTime(value: number, at: number) { this.ramps.push({ value, at }) },
  }
}

function makeFakeContext() {
  const sources: FakeSource[] = []
  const gains: FakeGain[] = []
  /** 测试开关：让 `start()` 抛错（起播失败路径）。 */
  const state = { failStart: false }
  const raw = {
    currentTime: 0,
    sampleRate: 24000,
    state: 'running' as AudioContextState,
    createBufferSource(): FakeSource {
      const source: FakeSource = {
        buffer: null,
        starts: [],
        stops: 0,
        stopArgs: [],
        connects: 0,
        disconnects: 0,
        connect: () => { source.connects += 1 },
        disconnect: () => { source.disconnects += 1 },
        start: (when: number) => {
          if (state.failStart)
            throw new Error('start failed')
          source.starts.push(when)
        },
        stop: (when?: number) => { source.stops += 1; source.stopArgs.push(when) },
        onended: null,
      }
      sources.push(source)
      return source
    },
    createGain(): FakeGain {
      const gain: FakeGain = {
        gain: makeFakeParam(),
        connect: () => {},
        disconnect: () => { gain.disconnects += 1 },
        disconnects: 0,
      }
      gains.push(gain)
      return gain
    },
  }
  // `BaseAudioContext.currentTime` 是只读的，测试要推时钟 ⇒ 另外留一个可写句柄给测试用。
  return { ctx: raw as unknown as BaseAudioContext, clock: raw, sources, gains, state }
}

function span(unitId: string, seconds: number): StreamingPlaybackSpan {
  return { unitId, buffer: { duration: seconds } as AudioBuffer }
}

/** 段播完：把时钟推到预定终点，再触发 `onended`（浏览器就是这个顺序）。 */
function finish(source: FakeSource, clock: { currentTime: number }, endAt: number) {
  clock.currentTime = endAt
  source.onended?.()
}

function createArmed(options: Parameters<typeof createStreamingPlayback>[0]) {
  const playback = createStreamingPlayback(options)
  playback.arm()                                   // 等价于宿主 play() 拿到所有权那一下
  return playback
}

describe('createStreamingPlayback（§4.3 音频时钟）', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('arm 之前只排队、不建节点（不许在宿主所有权之外先出声）', () => {
    const { ctx, sources } = makeFakeContext()
    const playback = createStreamingPlayback({ audioContext: ctx })

    playback.push(span('u1', 1.0))
    vi.advanceTimersByTime(5000)
    expect(sources).toHaveLength(0)
    expect(playback.nextStart()).toBeNull()

    playback.arm()
    expect(sources).toHaveLength(1)
    expect(sources[0].starts[0]).toBeCloseTo(0.05, 6)
  })

  it('首段按 now + lead 起播，后续段接在前段终点 + pauseAfter 上（不重复加 lead）', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 1.0))
    expect(sources[0].starts[0]).toBeCloseTo(0.05, 6)              // lead = 50ms
    playback.push(span('u2', 1.0))
    expect(sources[1].starts[0]).toBeCloseTo(1.15, 6)              // 0.05 + 1.0 + 0.10

    playback.push(span('u3', 1.0))
    expect(sources).toHaveLength(2)                     // 默认窗口 2 段：第三段先留在 pending
    finish(sources[0], clock, 1.05)
    expect(sources[2].starts[0]).toBeCloseTo(2.25, 6)
  })

  it('窗口只排 2 段：第三段等前段播完才排，起点仍在时间轴上连成一线', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx, maxAheadSpans: 2 })

    playback.push(span('u1', 1.0))
    playback.push(span('u2', 1.0))
    playback.push(span('u3', 1.0))
    expect(sources).toHaveLength(2)                                // 第三段还在 pending
    expect(playback.nextStart()).toBeCloseTo(2.25, 6)

    finish(sources[0], clock, 1.05)
    expect(sources).toHaveLength(3)
    expect(sources[2].starts[0]).toBeCloseTo(2.25, 6)              // 起点不因排队而漂
  })

  it('窗口还有秒数上限；窗口外不排，但会自动回来排（不会卡到永远没有 drained）', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx, maxAheadSpans: 2, maxAheadSeconds: 2 })

    playback.push(span('u1', 2.5))
    playback.push(span('u2', 0.5))
    // 第二段预定在 2.65s（0.05 + 2.5 + 0.10），距 now=0 超过 2s 窗口 ⇒ 先留在 pending
    expect(sources).toHaveLength(1)
    expect(playback.nextStart()).toBeCloseTo(2.65, 6)

    // 时间轴自己往前走：时钟推到 1.0s（距 2.65 只差 1.65s ≤ 2s）后兜底闹钟回来排
    clock.currentTime = 1.0
    vi.advanceTimersByTime(700)
    expect(sources).toHaveLength(2)
    expect(sources[1].starts[0]).toBeCloseTo(2.65, 6)
  })

  it('时间轴断口按 underrun 记账，且**扣掉有意停顿**；起点重新加一次 lead', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const underruns: number[] = []
    const playback = createArmed({
      audioContext: ctx,
      onUnderrun: ms => underruns.push(ms),
    })

    playback.push(span('u1', 1.0))
    expect(playback.nextStart()).toBeCloseTo(1.15, 6)              // 0.05 + 1.0 + 0.10

    // 下一段直到 2.0s 才就绪：听得到的空档 = 1.15（时间轴过期点）→ 2.05（新起点）= 0.90s
    // （0.10s 有意停顿已经在时间轴里，不重复计）
    clock.currentTime = 2.0
    playback.push(span('u2', 1.0))
    expect(underruns).toHaveLength(1)
    expect(underruns[0]).toBeCloseTo(900, 3)
    expect(sources[1].starts[0]).toBeCloseTo(2.05, 6)              // 重新起算 ⇒ 再加一次 lead
    expect(playback.stats().underruns).toBe(1)
    expect(playback.stats().resyncs).toBe(1)
  })

  it('段级事件：起点到点才报 start；正常播完才报 completed 并计入进度', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const events: string[] = []
    const playback = createArmed({
      audioContext: ctx,
      onSegmentStart: e => events.push(`start:${e.span.unitId}`),
      onSegmentEnd: e => events.push(`end:${e.span.unitId}:${e.completed}`),
    })

    playback.push(span('u1', 1.0))
    expect(events).toEqual([])                                     // 还没到点，不许提前报"在说"
    clock.currentTime = 0.05                                       // 音频时钟走到预定起点
    vi.advanceTimersByTime(50)
    expect(events).toEqual(['start:u1'])

    finish(sources[0], clock, 1.05)
    expect(events).toEqual(['start:u1', 'end:u1:true'])
    expect(playback.progress()).toEqual({ seconds: 1.0, units: 1 })
  })

  it('progress 逐段累计：正在播的段只算"音频内已过的部分"，units 只在播完时 +1', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 1.0))
    clock.currentTime = 0.45                                          // 起播 0.05 ⇒ 音频内过了 0.40s
    expect(playback.progress().seconds).toBeCloseTo(0.4, 6)
    expect(playback.progress().units).toBe(0)

    finish(sources[0], clock, 1.05)
    playback.push(span('u2', 1.0))
    clock.currentTime = 1.35                                          // 第二段起播 1.15 ⇒ 过了 0.20s
    expect(playback.progress().seconds).toBeCloseTo(1.2, 6)
    expect(playback.progress().units).toBe(1)
  })

  it('stop（非主动理由）会停掉当前与所有未来节点、清时间轴，并让 drained 收尾（不把半段算成已播）', async () => {
    const { ctx, clock, sources } = makeFakeContext()
    const onDrained = vi.fn()
    const playback = createArmed({ audioContext: ctx, onDrained })

    playback.push(span('u1', 1.0))
    playback.push(span('u2', 1.0))
    clock.currentTime = 0.5
    playback.stop('stop-all')

    expect(sources[0].stops).toBe(1)
    expect(sources[1].stops).toBe(1)
    expect(playback.nextStart()).toBeNull()
    await expect(playback.drained).resolves.toBeUndefined()
    expect(onDrained).toHaveBeenCalledTimes(1)
    expect(playback.progress()).toEqual({ seconds: 0, units: 0 })  // 半段不算

    // stop 之后迟到的 onended 不许再报 completed / 推进时间轴
    sources[0].onended?.()
    expect(playback.progress()).toEqual({ seconds: 0, units: 0 })
  })

  it('closeInput + 段播完 ⇒ drained 收尾；纯无音频 turn 也能立刻收尾（不占播放槽）', async () => {
    const { ctx, clock, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 1.0))
    playback.closeInput()
    let done = false
    void playback.drained.then(() => { done = true })
    await Promise.resolve()
    expect(done).toBe(false)                                       // 还在播，别急着放槽

    finish(sources[0], clock, 1.05)
    await playback.drained
    expect(playback.stats().spansPlayed).toBe(1)

    const empty = createArmed({ audioContext: ctx })
    empty.closeInput()
    await expect(empty.drained).resolves.toBeUndefined()
  })

  it('closeInput 之后拒收迟到的段（drained 已经放掉宿主的所有权，不许还出声）', async () => {
    const { ctx, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.closeInput()
    await playback.drained
    playback.push(span('late', 1.0))
    vi.advanceTimersByTime(5000)

    expect(sources).toHaveLength(0)                                // 一个节点都不该建
    expect(playback.nextStart()).toBeNull()
    expect(playback.progress()).toEqual({ seconds: 0, units: 0 })
  })

  it('上下文暂停时不报"段开始"（定时器只是唤醒信号），wake() 后再补报', () => {
    const { ctx, clock, sources } = makeFakeContext()
    let playable = false
    const starts: string[] = []
    const playback = createArmed({
      audioContext: ctx,
      isPlayable: () => playable,
      onSegmentStart: e => starts.push(e.span.unitId),
    })

    playback.push(span('u1', 1.0))
    vi.advanceTimersByTime(50)                                     // 墙钟到点了
    expect(starts).toEqual([])                                     // 但音频时钟没走 ⇒ 不报
    expect(sources[0].starts[0]).toBeCloseTo(0.05, 6)              // 节点照样排好了

    // 恢复：音频时钟走到起点，宿主 wake()
    playable = true
    clock.currentTime = 0.05
    playback.wake()
    vi.advanceTimersByTime(1)
    expect(starts).toEqual(['u1'])
  })

  it('提前 onended：已听到的那部分算进进度（单调），但 units 只认正常播完的段', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 1.0))
    clock.currentTime = 0.4
    expect(playback.progress().seconds).toBeCloseTo(0.35, 6)       // 起播 0.05 ⇒ 音频内过了 0.35s

    sources[0].onended?.()                                         // 提前结束（没到 1.05s）
    expect(playback.progress().seconds).toBeCloseTo(0.35, 6)       // 不回退
    expect(playback.progress().units).toBe(0)
    expect(playback.stats().spansPlayed).toBe(0)
  })

  it('事件循环卡顿导致"先 ended 后 start"：先补 start 再报 end（不许只有 end）', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const events: string[] = []
    const playback = createArmed({
      audioContext: ctx,
      onSegmentStart: e => events.push(`start:${e.span.unitId}`),
      onSegmentEnd: e => events.push(`end:${e.span.unitId}:${e.completed}`),
    })

    playback.push(span('u1', 1.0))
    // 故意不让定时器跑：直接把音频时钟推到终点，模拟"音频播完了，start 通知还堵在事件循环里"
    clock.currentTime = 1.05
    sources[0].onended?.()

    expect(events).toEqual(['start:u1', 'end:u1:true'])
  })
})

/**
 * 主动打断（`new-message`）的淡出与跨轮屏障 —— sol-high 方案 §三/§四 的离线接线断言。
 * 注意：这里的"没 disconnect"是**淡出没被硬切**的判据（淡出期间断开就等于切掉渐降）。
 */
describe('主动打断淡出（new-message）', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('播中取消：gain 100ms 内线性降到 0、stop 带绝对时刻、淡出期间不 disconnect', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const stopped: { reason?: string, fadeEnd: number | null }[] = []
    const ends: string[] = []
    const playback = createArmed({
      audioContext: ctx,
      onStopped: info => stopped.push(info),
      onSegmentEnd: e => ends.push(`${e.span.unitId}:${e.completed}`),
    })

    playback.push(span('u1', 2.0))                       // 起播 0.05，终点 2.05
    expect(gains).toHaveLength(1)                        // 每段一个增益节点
    expect(gains[0].gain.value).toBe(1)                  // 正常恒为 1

    clock.currentTime = 1.0                              // 正在播
    playback.stop('new-message')

    const gain = gains[0]
    expect(gain.gain.cancels).toBe(1)
    expect(gain.gain.sets).toEqual([{ value: 1, at: 1.0 }])
    expect(gain.gain.ramps).toHaveLength(1)
    expect(gain.gain.ramps[0].value).toBe(0)              // 降到 0
    expect(gain.gain.ramps[0].at).toBeCloseTo(1.1, 6)     // ……在 100ms 内
    expect(sources[0].stopArgs[0]).toBeCloseTo(1.1, 6)    // 排程停止：带时刻，不是立即
    expect(sources[0].disconnects).toBe(0)                // 淡出期间不许断开
    expect(gain.disconnects).toBe(0)
    expect(stopped).toHaveLength(1)
    expect(stopped[0].reason).toBe('new-message')
    expect(stopped[0].fadeEnd).toBeCloseTo(1.1, 6)

    let done = false
    void playback.drained.then(() => { done = true })
    await Promise.resolve()
    expect(done).toBe(false)                              // 淡出还在走 ⇒ 不放槽

    clock.currentTime = 1.1
    sources[0].onended?.()                                // 淡出尾真的走完
    expect(gain.disconnects).toBe(1)
    expect(sources[0].disconnects).toBe(1)
    expect(ends).toEqual([])                              // 被取消的段不进正常完成路径（不补 ACK）
    expect(playback.progress()).toEqual({ seconds: 0, units: 0 })
    await expect(playback.drained).resolves.toBeUndefined()
  })

  it('未来段立即取消（不许在这 100ms 里冒出来），且它们不淡出', () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 2.0))                        // 播中
    clock.currentTime = 1.0
    playback.push(span('u2', 1.0))                        // 未来段（起点 2.15）
    playback.stop('new-message')

    expect(sources[1].stopArgs[0]).toBeUndefined()         // 立即停：无参
    expect(sources[1].disconnects).toBe(1)
    expect(gains[1].disconnects).toBe(1)
    expect(gains[1].gain.ramps).toHaveLength(0)
    expect(sources[0].stopArgs[0]).toBeCloseTo(1.1, 6)     // 只有正在出声的那段淡出
  })

  it('重复取消幂等：不改淡出终点、不补第二次硬停、只报一次', () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const stopped: unknown[] = []
    const playback = createArmed({ audioContext: ctx, onStopped: info => stopped.push(info) })

    playback.push(span('u1', 2.0))
    clock.currentTime = 1.0
    playback.stop('new-message')
    clock.currentTime = 1.04                               // 淡出还没走完
    playback.stop('new-message')

    expect(sources[0].stops).toBe(1)
    expect(sources[0].stopArgs).toEqual([1.1])
    expect(gains[0].gain.ramps).toHaveLength(1)
    expect(stopped).toHaveLength(1)
  })

  it('非主动理由仍是立即停：不给它们加这 100ms', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const stopped: { fadeEnd: number | null }[] = []
    const playback = createArmed({ audioContext: ctx, onStopped: info => stopped.push(info) })

    playback.push(span('u1', 2.0))
    clock.currentTime = 1.0
    playback.push(span('u2', 1.0))
    playback.stop('stop-all')

    expect(sources[0].stopArgs[0]).toBeUndefined()
    expect(sources[1].stopArgs[0]).toBeUndefined()
    expect(gains[0].gain.ramps).toHaveLength(0)
    expect(stopped[0].fadeEnd).toBeNull()
    await expect(playback.drained).resolves.toBeUndefined()   // 不用等淡出
  })

  it('上下文暂停时没有可淡出的东西 ⇒ 立刻清理，不等一个不会前进的时钟', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 2.0))
    clock.currentTime = 1.0
    clock.state = 'suspended'
    playback.stop('new-message')

    expect(gains[0].gain.ramps).toHaveLength(0)
    expect(sources[0].stopArgs[0]).toBeUndefined()
    await expect(playback.drained).resolves.toBeUndefined()
  })

  it('跨轮屏障：下一轮首段不早于上一轮淡出终点 + 20ms', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const stopped: { fadeEnd: number | null }[] = []
    const first = createArmed({ audioContext: ctx, onStopped: info => stopped.push(info) })

    first.push(span('u1', 2.0))
    clock.currentTime = 1.0
    first.stop('new-message')                              // 淡出终点 1.1
    const fadeEnd = stopped[0].fadeEnd!

    const second = createStreamingPlayback({ audioContext: ctx })
    second.push(span('u2', 1.0))
    second.arm(fadeEnd + 0.020)                            // 宿主用 onStopped 记下的屏障
    // now + lead = 1.05 比屏障（1.12）早 ⇒ 被屏障挡住，新句不压旧句尾巴
    expect(sources[1].starts[0]).toBeCloseTo(1.12, 6)
  })
})

/** sol-medium 复核 §1/§2 的清理边界：上下文挂起后的取消尾巴、静音强制清理、节点生命周期。 */
describe('取消尾巴与节点生命周期', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('先在 running 开始淡出、再挂起 ⇒ 尾巴被强制收掉、drained 收尾、不进正常完成路径', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const ends: string[] = []
    const playback = createArmed({
      audioContext: ctx,
      onSegmentEnd: e => ends.push(`${e.span.unitId}:${e.completed}`),
    })

    playback.push(span('u1', 2.0))
    clock.currentTime = 1.0
    playback.stop('new-message')                           // 淡出排到 1.10
    expect(sources[0].stopArgs).toEqual([1.1])
    expect(sources[0].disconnects).toBe(0)                 // 淡出期间不许断

    clock.state = 'suspended'                              // 淡出走不动了
    playback.abortCancelledTails()

    expect(sources[0].stopArgs).toEqual([1.1, undefined])  // 补一次立即停
    expect(sources[0].disconnects).toBe(1)
    expect(gains[0].disconnects).toBe(1)
    expect(ends).toEqual([])                               // 不报段结束
    await expect(playback.drained).resolves.toBeUndefined()

    sources[0].onended?.()                                 // 迟到的 ended 幂等，不许再动
    expect(sources[0].disconnects).toBe(1)
    expect(gains[0].disconnects).toBe(1)
  })

  it('淡出期间要求静音/卸载 ⇒ 允许立刻收走残留尾巴（第二次调用不再纯早退）', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 2.0))
    clock.currentTime = 1.0
    playback.stop('new-message')
    clock.currentTime = 1.05
    playback.stop('muted')                                 // 强制清理可以升级

    expect(sources[0].stopArgs[0]).toBeCloseTo(1.1, 6)     // 不改原来的淡出终点
    expect(sources[0].stops).toBe(2)                        // 但补了一次立即停
    expect(gains[0].disconnects).toBe(1)
    await expect(playback.drained).resolves.toBeUndefined()
  })

  it('同一条取消链的重复 new-message 仍是纯 no-op（不延长、不补硬停）', () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 2.0))
    clock.currentTime = 1.0
    playback.stop('new-message')
    clock.currentTime = 1.04
    playback.stop('new-message')

    expect(sources[0].stops).toBe(1)
    expect(sources[0].stopArgs).toEqual([1.1])
    expect(gains[0].gain.ramps).toHaveLength(1)
  })

  it('正常播完 ⇒ source 与 gain 都断开', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    playback.push(span('u1', 1.0))
    playback.closeInput()
    finish(sources[0], clock, 1.05)
    await playback.drained

    expect(sources[0].disconnects).toBe(1)
    expect(gains[0].disconnects).toBe(1)
  })

  it('起播失败 ⇒ 一样放掉 source/gain，进度 +0，后面的段照常排', async () => {
    const { ctx, clock, sources, gains, state } = makeFakeContext()
    const playback = createArmed({ audioContext: ctx })

    state.failStart = true
    playback.push(span('u1', 1.0))                         // 起播就抛
    expect(sources[0].disconnects).toBe(1)
    expect(gains[0].disconnects).toBe(1)
    expect(playback.progress()).toEqual({ seconds: 0, units: 0 })

    state.failStart = false
    playback.push(span('u2', 1.0))                         // 时间轴没被卡住
    expect(sources[1].starts[0]).toBeCloseTo(0.05, 6)
    finish(sources[1], clock, 1.05)
    expect(playback.progress()).toEqual({ seconds: 1, units: 1 })
  })
})

/**
 * 自然轮末回报（sol-high 方案 §二.A）：旧轮**正常说完**也要把最后一段的终点交出来，
 * 否则跨轮屏障是空的，新轮音频一就绪就紧贴旧尾起播（真机 16:13 的 100/180/100ms 接续）。
 */
describe('自然轮末回报（onNaturalEnd）', () => {
  it('回报的是最后一段的 endAt，不是回调执行时的 currentTime', async () => {
    const { ctx, clock, sources } = makeFakeContext()
    const seen: number[] = []
    const playback = createStreamingPlayback({ audioContext: ctx, onNaturalEnd: e => seen.push(e) })

    playback.push(span('u1', 1.0))
    playback.arm()                                        // startAt 0.05 ⇒ endAt 1.05
    finish(sources[0], clock, 1.05)                       // 自然播完
    clock.currentTime = 9.0                               // 事件循环迟到的极端：时钟已经走远
    playback.closeInput()
    await playback.drained

    expect(seen).toEqual([1.05])                          // 记排程终点，不记 9.0
  })

  it('先回报自然终点、再结算 drained（顺序不能反），且只报一次', async () => {
    const { ctx, clock, sources } = makeFakeContext()
    const order: string[] = []
    const playback = createStreamingPlayback({
      audioContext: ctx,
      onNaturalEnd: e => order.push(`natural:${e.toFixed(2)}`),
      onDrained: () => order.push('drained'),
    })

    playback.push(span('u1', 0.5))
    playback.arm()
    finish(sources[0], clock, 0.55)
    playback.closeInput()
    await playback.drained

    expect(order).toEqual(['natural:0.55', 'drained'])
    playback.closeInput()                                 // 再叫一次不许重复回报
    expect(order.filter(x => x.startsWith('natural'))).toHaveLength(1)
  })

  it('取消（被打断）不走自然终点 —— 那条路仍走 onStopped(fadeEnd)', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const natural: number[] = []
    const stopped: unknown[] = []
    const playback = createStreamingPlayback({
      audioContext: ctx,
      onNaturalEnd: e => natural.push(e),
      onStopped: info => stopped.push(info),
    })

    playback.push(span('u1', 2.0))
    playback.arm()
    clock.currentTime = 1.0
    playback.stop('new-message')                          // 淡出终点 1.10
    finish(sources[0], clock, 1.1)

    expect(natural).toEqual([])
    expect(stopped).toEqual([{ reason: 'new-message', fadeEnd: 1.1 }])
  })

  it('纯无音频 turn：不写虚构终点', async () => {
    const { ctx } = makeFakeContext()
    const seen: number[] = []
    const playback = createStreamingPlayback({ audioContext: ctx, onNaturalEnd: e => seen.push(e) })

    playback.arm()
    playback.closeInput()
    await playback.drained

    expect(seen).toEqual([])
  })
})

describe('节点取证元数据', () => {
  it('节点挂 __albionUnitId / __albionIdentity / __albionIntentId，与 span 一致（只写不读）', () => {
    const { ctx, sources } = makeFakeContext()
    const playback = createStreamingPlayback({ audioContext: ctx })
    const identity = { sessionId: 'S', generation: 1, unitId: 'U', subIndex: 2 }

    playback.push({
      unitId: 'U:2',
      subIndex: 2,
      identity,
      intentId: 'intent-7',
      buffer: { duration: 1 } as AudioBuffer,
    })
    playback.arm()

    const node = sources[0] as unknown as Record<string, unknown>
    expect(node.__albionUnitId).toBe('U:2')
    expect(node.__albionIdentity).toEqual(identity)
    expect(node.__albionIntentId).toBe('intent-7')
  })

  it('本地编号的段（服务端没给身份）不带取证身份，别造假账', () => {
    const { ctx, sources } = makeFakeContext()
    const playback = createStreamingPlayback({ audioContext: ctx })

    playback.push(span('local-0', 1.0))
    playback.arm()

    const node = sources[0] as unknown as Record<string, unknown>
    expect(node.__albionUnitId).toBe('local-0')
    expect(node.__albionIdentity).toBeUndefined()
    expect(node.__albionIntentId).toBeUndefined()
  })
})
