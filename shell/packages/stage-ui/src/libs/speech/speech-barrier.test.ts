import type { StreamingPlaybackSpan } from './streaming-playback'

import { describe, expect, it } from 'vitest'

import { barrierArmValue, bindFadeBarrier, bindNaturalEndBarrier, createSpeechBarrier } from './speech-barrier'
import { createStreamingPlayback } from './streaming-playback'

/**
 * 跨轮屏障的**宿主接线**测试（sol-medium 复核 §3）。
 *
 * 手写 `arm(fadeEnd + 0.02)` 只证明「clock 收屏障」，证明不了 Stage 那条链。
 * 这里连宿主那两步也用**生产函数**：`bindFadeBarrier`（`onPlaybackStopped` 用）与
 * `barrierArmValue`（`clock.arm(...)` 用）—— Stage 调的就是这两个，改坏了这里先红。
 */
function makeFakeContext() {
  const sources: { starts: number[], onended: (() => void) | null }[] = []
  const clock = {
    currentTime: 0,
    state: 'running' as AudioContextState,
    sampleRate: 24000,
    createBufferSource() {
      const node = {
        buffer: null as AudioBuffer | null,
        starts: [] as number[],
        connect: () => {},
        disconnect: () => {},
        start: (when: number) => { node.starts.push(when) },
        stop: () => {},
        onended: null as (() => void) | null,
      }
      sources.push(node)
      return node
    },
    createGain() {
      return {
        gain: {
          value: 1,
          cancelScheduledValues: () => {},
          setValueAtTime: () => {},
          linearRampToValueAtTime: () => {},
        },
        connect: () => {},
        disconnect: () => {},
      }
    },
  }
  return { ctx: clock as unknown as BaseAudioContext, clock, sources }
}

function span(unitId: string, seconds: number): StreamingPlaybackSpan {
  return { unitId, buffer: { duration: seconds } as AudioBuffer }
}

describe('createSpeechBarrier', () => {
  it('记淡出终点 + 20ms；取 max 只前进不后退；没有淡出就不动', () => {
    const { ctx } = makeFakeContext()
    const barrier = createSpeechBarrier()

    expect(barrier.notBefore(ctx)).toBe(0)          // 没人取消过 = 不挡
    barrier.noteFadeEnd(ctx, null)
    barrier.noteFadeEnd(ctx, undefined)
    expect(barrier.notBefore(ctx)).toBe(0)

    barrier.noteFadeEnd(ctx, 1.10)
    expect(barrier.notBefore(ctx)).toBeCloseTo(1.12, 6)

    barrier.noteFadeEnd(ctx, 0.5)                   // 更早的淡出不许把屏障拉回去
    expect(barrier.notBefore(ctx)).toBeCloseTo(1.12, 6)
  })
})

describe('跨轮屏障接线（宿主 → 屏障 → 新轮）', () => {
  it('旧轮真实淡出喂进屏障 ⇒ 新轮首段 = 淡出终点 + 20ms（不等旧轮 drained 也挡得住）', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const barrier = createSpeechBarrier()

    const first = createStreamingPlayback({
      audioContext: ctx,
      onStopped: bindFadeBarrier(barrier, ctx),   // ← 宿主那一步（生产函数）
    })
    first.push(span('u1', 2.0))
    first.arm()
    clock.currentTime = 1.0
    first.stop('new-message')                       // 淡出终点 1.10 ⇒ 屏障 1.12

    // **不等** `first.drained`：M 的 abort 就是这么立刻放掉旧 item 的（这正是屏障要跨轮的原因）
    const second = createStreamingPlayback({ audioContext: ctx })
    second.push(span('u2', 1.0))
    second.arm(barrierArmValue(barrier, ctx))       // ← 宿主那一步（生产函数）
    expect(sources[1].starts[0]).toBeCloseTo(1.12, 6)

    // 对照：不接屏障（宿主漏传）时是 `now + lead` = 1.05 —— 早 70ms，旧句尾巴就会被压住。
    const third = createStreamingPlayback({ audioContext: ctx })
    third.push(span('u3', 1.0))
    third.arm(barrierArmValue(createSpeechBarrier(), ctx))   // 空屏障 = 没取消过
    expect(sources[2].starts[0]).toBeCloseTo(1.05, 6)
  })

  it('没有主动打断（静音等）⇒ 屏障不动，新轮不额外等', () => {
    const { ctx, clock } = makeFakeContext()
    const barrier = createSpeechBarrier()

    const first = createStreamingPlayback({
      audioContext: ctx,
      onStopped: bindFadeBarrier(barrier, ctx),
    })
    first.push(span('u1', 2.0))
    first.arm()
    clock.currentTime = 1.0
    first.stop('stop-all')                          // 不淡出 ⇒ fadeEnd = null

    expect(barrierArmValue(barrier, ctx)).toBe(0)
  })
})

describe('自然轮末气口（旧轮说完 → 新轮首段，生产接线）', () => {
  it('keeps the completed tail gap when a new turn cancels an input-open idle clock', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const barrier = createSpeechBarrier()
    const first = createStreamingPlayback({
      audioContext: ctx,
      onStopped: bindFadeBarrier(barrier, ctx),
      onNaturalEnd: bindNaturalEndBarrier(barrier, ctx),
    })
    first.push(span('u1', 2))
    first.arm()
    clock.currentTime = 2.05
    sources[0].onended?.()
    // Input is still open while synthesis or the final WS event is pending.
    first.stop('new-message')
    const second = createStreamingPlayback({ audioContext: ctx })
    second.push(span('u2', 1))
    second.arm(barrierArmValue(barrier, ctx))
    expect(sources[1].starts[0]).toBeCloseTo(2.45, 6)
  })

  it('keeps the completed tail gap when its ended callback has not run yet', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const barrier = createSpeechBarrier()
    const first = createStreamingPlayback({
      audioContext: ctx,
      onStopped: bindFadeBarrier(barrier, ctx),
    })
    first.push(span('u1', 2))
    first.arm()
    clock.currentTime = 2.05
    first.stop('new-message')
    const second = createStreamingPlayback({ audioContext: ctx })
    second.push(span('u2', 1))
    second.arm(barrierArmValue(barrier, ctx))
    expect(sources[1].starts[0]).toBeCloseTo(2.45, 6)
  })

  it('旧轮自然播完 ⇒ 新轮首段 = 最后一段终点 + 0.40s（不是 now+lead）', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const barrier = createSpeechBarrier()

    const first = createStreamingPlayback({
      audioContext: ctx,
      onNaturalEnd: bindNaturalEndBarrier(barrier, ctx),   // ← 宿主那一步（生产函数）
    })
    first.push(span('u1', 2.0))
    first.arm()                                          // startAt = 0.05 ⇒ endAt = 2.05
    clock.currentTime = 2.05
    sources[0].onended?.()                                // 自然播完
    first.closeInput()                                    // 只在这一刻才结算 drained/回报终点

    expect(barrierArmValue(barrier, ctx)).toBeCloseTo(2.45, 6)

    const second = createStreamingPlayback({ audioContext: ctx })
    second.push(span('u2', 1.0))
    second.arm(barrierArmValue(barrier, ctx))
    expect(sources[1].starts[0]).toBeCloseTo(2.45, 6)
  })

  it('反事实：漏接自然屏障（不传 onNaturalEnd）⇒ 屏障不动，新轮紧贴 now+lead 起播', () => {
    const { ctx, clock, sources } = makeFakeContext()
    const barrier = createSpeechBarrier()

    const first = createStreamingPlayback({ audioContext: ctx })   // ← 故意漏接线
    first.push(span('u1', 2.0))
    first.arm()
    clock.currentTime = 2.05
    sources[0].onended?.()
    first.closeInput()

    expect(barrierArmValue(barrier, ctx)).toBe(0)
    const second = createStreamingPlayback({ audioContext: ctx })
    second.push(span('u2', 1.0))
    second.arm(barrierArmValue(barrier, ctx))
    expect(sources[1].starts[0]).toBeCloseTo(2.1, 6)     // now + lead，比 2.45 早 350ms
  })

  it('自然与淡出两条路分开记账、取较大值；回退值 0 = 旧行为', () => {
    const { ctx } = makeFakeContext()
    const b = createSpeechBarrier()
    b.noteFadeEnd(ctx, 5.0)          // ⇒ 5.02
    b.noteNaturalEnd(ctx, 4.0)       // ⇒ 4.40
    expect(b.notBefore(ctx)).toBeCloseTo(5.02, 6)

    const fallback = createSpeechBarrier(0.02, 0)        // 回退：只改自然气口，不动淡出
    fallback.noteNaturalEnd(ctx, 4.0)
    expect(fallback.notBefore(ctx)).toBeCloseTo(4.0, 6)
    fallback.noteFadeEnd(ctx, 6.0)
    expect(fallback.notBefore(ctx)).toBeCloseTo(6.02, 6)  // 淡出那条路仍在
  })
})
