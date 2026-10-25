import type { StreamingPlaybackSpan } from './streaming-playback'

import { describe, expect, it } from 'vitest'

import { createStreamClockRegistry } from './stream-clock-registry'
import { createStreamingPlayback } from './streaming-playback'

/**
 * 宿主入口测试（sol-medium 复核 §2）：旧轮被 `new-message` 取消、M **已经放掉**它的 item、
 * `currentSession` 也换新之后，静音/卸载仍然要把旧轮的淡出尾巴立刻收掉。
 *
 * 关键就是要「不可达」这一步 —— 只调旧 clock 自己的 `stop('muted')` 测不到这个阻塞。
 * 这里用的是**生产函数** `createStreamClockRegistry`（Stage 的 `stopSpeechOutput`/`onUnmounted`
 * 调的就是它的 `sweepCancelledTails`）。
 */
function makeFakeContext() {
  const sources: { starts: number[], stops: number, stopArgs: (number | undefined)[], disconnects: number }[] = []
  const gains: { disconnects: number }[] = []
  const clock = {
    currentTime: 0,
    state: 'running' as AudioContextState,
    sampleRate: 24000,
    createBufferSource() {
      const node = {
        buffer: null as AudioBuffer | null,
        starts: [] as number[],
        stops: 0,
        stopArgs: [] as (number | undefined)[],
        disconnects: 0,
        connect: () => {},
        disconnect: () => { node.disconnects += 1 },
        start: (when: number) => { node.starts.push(when) },
        stop: (when?: number) => { node.stops += 1; node.stopArgs.push(when) },
        onended: null as (() => void) | null,
      }
      sources.push(node)
      return node
    },
    createGain() {
      const gain = {
        gain: {
          value: 1,
          cancelScheduledValues: () => {},
          setValueAtTime: () => {},
          linearRampToValueAtTime: () => {},
        },
        connect: () => {},
        disconnect: () => { gain.disconnects += 1 },
        disconnects: 0,
      }
      gains.push(gain)
      return gain
    },
  }
  return { ctx: clock as unknown as BaseAudioContext, clock, sources, gains }
}

function span(unitId: string, seconds: number): StreamingPlaybackSpan {
  return { unitId, buffer: { duration: seconds } as AudioBuffer }
}

describe('createStreamClockRegistry（宿主静音/卸载入口）', () => {
  it('旧轮已被 M 移除、会话已换新 ⇒ 静音仍能立刻收掉旧轮的取消尾巴，且不打扰新轮', async () => {
    const { ctx, clock, sources, gains } = makeFakeContext()
    const registry = createStreamClockRegistry()

    // 旧轮：playFunction 接管 ⇒ 登记
    const old = createStreamingPlayback({ audioContext: ctx })
    registry.add(old)
    old.push(span('u1', 2.0))
    old.arm(0)
    clock.currentTime = 1.0
    old.stop('new-message')                       // 淡出排到 1.10
    expect(sources[0].stopArgs).toEqual([1.1])

    // 此刻：M 的 abort 已经把它放掉、currentSession 换新 —— 宿主只剩这本册子
    const fresh = createStreamingPlayback({ audioContext: ctx })
    registry.add(fresh)
    fresh.push(span('u2', 1.0))
    fresh.arm(0)
    expect(sources[1].starts[0]).toBeCloseTo(1.05, 6)

    clock.currentTime = 1.02
    registry.sweepCancelledTails()                // = `stopSpeechOutput('muted')` 里那一步

    expect(sources[0].stopArgs).toEqual([1.1, undefined])   // 旧尾巴立即收掉（第二下无参 stop）
    expect(sources[0].disconnects).toBe(1)
    expect(gains[0].disconnects).toBe(1)
    expect(sources[1].stops).toBe(0)                        // 新轮在正常播，不许被动
    await expect(old.drained).resolves.toBeUndefined()

    registry.remove(old)
    expect(registry.size()).toBe(1)
  })

  it('没有取消尾巴时（正常播放/已收尾）sweep 是 no-op', async () => {
    const { ctx, sources } = makeFakeContext()
    const registry = createStreamClockRegistry()
    const playback = createStreamingPlayback({ audioContext: ctx })
    registry.add(playback)
    playback.push(span('u1', 1.0))
    playback.arm(0)

    registry.sweepCancelledTails()
    expect(sources[0].stops).toBe(0)              // 正常播放的段不许被静音路径提前停掉
  })
})
