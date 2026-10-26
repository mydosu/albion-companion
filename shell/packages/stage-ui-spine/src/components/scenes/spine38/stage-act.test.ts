import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { asHeadMotion, FACE_ANIMATION, FaceHoldTimer, HEAD_MOTIONS, headMotionOffset, holdDurationMs, parseHold, resolveFace } from './stage-act'

describe('resolveFace', () => {
  it('prefers a known face over emotion, and maps every semantic name including 4-7', () => {
    expect(resolveFace({ emotion: 'happy', face: 'pout' })).toBe('11')
    expect(resolveFace({ face: ' Wry ' })).toBe('4')
    expect(resolveFace({ face: 'bashful' })).toBe('5')
    expect(resolveFace({ face: 'fluster' })).toBe('6')
    expect(resolveFace({ face: 'gentle' })).toBe('7')
    // 12 个名字里 shy 和 bashful 都指向脸 5（都是「害羞」的普通眼脸），所以实际只有 11 张脸；
    // 脸 9（漩涡眼）没有表情映射了，只留作手动/调试。
    expect(new Set(Object.values(FACE_ANIMATION)).size).toBe(11)
    expect(resolveFace({ face: 'shy' })).toBe('5')
    expect(Object.values(FACE_ANIMATION)).not.toContain('9')
  })

  it('falls back to the emotion default when face is missing or not a semantic name', () => {
    expect(resolveFace({ emotion: 'happy', face: '8' })).toBe('8')
    expect(resolveFace({ emotion: 'sad' })).toBe('11')
    expect(resolveFace({ emotion: 'neutral' })).toBe('normal')
    expect(resolveFace({ emotion: 'nope' })).toBeUndefined()
    expect(resolveFace({})).toBeUndefined()
  })
})

describe('head motions', () => {
  it('accepts only nod/tilt/shake', () => {
    expect(asHeadMotion('Nod')).toBe('nod')
    expect(asHeadMotion('touch_head')).toBeUndefined()
    expect(asHeadMotion(1)).toBeUndefined()
  })

  it.each(HEAD_MOTIONS)('%s stays small, starts and ends at rest, then finishes', (kind) => {
    const start = headMotionOffset(kind, 0)!
    expect(Math.abs(start.rot) + Math.abs(start.down) + Math.abs(start.right)).toBeLessThan(1e-9)
    let finished = -1
    for (let t = 0; t < 5; t += 0.01) {
      const o = headMotionOffset(kind, t)
      if (!o) {
        finished = t
        break
      }
      expect(Math.abs(o.rot)).toBeLessThanOrEqual(6)
      expect(Math.abs(o.down)).toBeLessThanOrEqual(9)
      expect(Math.abs(o.right)).toBeLessThanOrEqual(6)
    }
    expect(finished).toBeGreaterThan(0)
    const last = headMotionOffset(kind, finished - 0.01)!
    expect(Math.abs(last.rot)).toBeLessThan(0.6)
    expect(headMotionOffset(kind, -1)).toBeUndefined()
    expect(headMotionOffset(kind, Number.NaN)).toBeUndefined()
  })
})

describe('parseHold', () => {
  it('reads keep, seconds (clamped 1..60) and defaults to speech', () => {
    expect(parseHold('keep')).toEqual({ kind: 'keep' })
    expect(parseHold('8')).toEqual({ kind: 'seconds', seconds: 8 })
    expect(parseHold(0.2)).toEqual({ kind: 'seconds', seconds: 1 })
    expect(parseHold(999)).toEqual({ kind: 'seconds', seconds: 60 })
    expect(parseHold(undefined)).toEqual({ kind: 'speech' })
    expect(parseHold('soon')).toEqual({ kind: 'speech' })
    expect(parseHold(-3)).toEqual({ kind: 'speech' })
  })
})

describe('faceHoldTimer', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('speech hold waits for the turn to stop speaking, then a short tail', () => {
    const release = vi.fn()
    const hold = new FaceHoldTimer(release)
    hold.start({ kind: 'speech' }, true)
    // 说话中的上限是 6s（脸图期间眼睛不动，宁短勿长），所以别越过它
    vi.advanceTimersByTime(5_000)
    expect(release).not.toHaveBeenCalled()
    hold.setSpeaking(false)
    vi.advanceTimersByTime(1499)
    expect(release).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(release).toHaveBeenCalledTimes(1)
    hold.setSpeaking(true)
    hold.setSpeaking(false)
    vi.advanceTimersByTime(10_000)
    expect(release).toHaveBeenCalledTimes(1)
  })

  it('speech hold without any speech releases after 6s; speaking caps the face at 6s too', () => {
    const release = vi.fn()
    const hold = new FaceHoldTimer(release)
    hold.start({ kind: 'speech' }, false)
    vi.advanceTimersByTime(6000)
    expect(release).toHaveBeenCalledTimes(1)
    hold.start({ kind: 'speech' }, true)
    vi.advanceTimersByTime(5999)
    expect(release).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(release).toHaveBeenCalledTimes(2)
  })

  it('seconds and keep ignore speech; keep is capped at 2 minutes; cancel stops everything', () => {
    const release = vi.fn()
    const hold = new FaceHoldTimer(release)
    hold.start({ kind: 'seconds', seconds: 8 }, true)
    hold.setSpeaking(false)
    vi.advanceTimersByTime(7999)
    expect(release).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(release).toHaveBeenCalledTimes(1)
    hold.start({ kind: 'keep' }, false)
    hold.setSpeaking(true)
    hold.setSpeaking(false)
    vi.advanceTimersByTime(119_999)
    expect(release).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(release).toHaveBeenCalledTimes(2)
    hold.start({ kind: 'keep' }, false)
    hold.cancel()
    vi.advanceTimersByTime(200_000)
    expect(release).toHaveBeenCalledTimes(2)
  })
})

describe('holdDurationMs', () => {
  it('caps a default speech face at 6s and hands out the tail once speech stops', () => {
    expect(holdDurationMs({ kind: 'speech' }, true, true)).toBe(6000)
    expect(holdDurationMs({ kind: 'speech' }, true, false)).toBe(6000)
    expect(holdDurationMs({ kind: 'speech' }, false, true)).toBe(1500)
    expect(holdDurationMs({ kind: 'speech' }, false, false)).toBe(6000)
  })

  it('honours an explicit seconds value and keep', () => {
    expect(holdDurationMs({ kind: 'seconds', seconds: 8 }, true, true)).toBe(8000)
    expect(holdDurationMs({ kind: 'keep' }, false, false)).toBe(120_000)
  })
})
