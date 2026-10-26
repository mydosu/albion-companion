import { describe, expect, it } from 'vitest'

import {
  FACE_FADE_MS,
  FACE_OVERLAY_MIN_ALPHA,
  FACE_SWAY_DEG,
  FACE_SWAY_PERIOD_MS,
  faceOverlayAlpha,
  faceSwayDeg,
  MOUTH_ATTACK_MS,
  MOUTH_CLOSED_SCALE,
  MOUTH_OPEN_SCALE,
  MOUTH_RELEASE_MS,
  mouthLevelFromRms,
  mouthScaleFor,
  smoothMouth,
  stepFaceAlpha,
} from './face-motion'

describe('mouthLevelFromRms', () => {
  it('gates the noise floor and saturates at full scale', () => {
    expect(mouthLevelFromRms(0)).toBe(0)
    expect(mouthLevelFromRms(0.019)).toBe(0)
    expect(mouthLevelFromRms(Number.NaN)).toBe(0)
    expect(mouthLevelFromRms(0.12)).toBeCloseTo(0.6, 5)
    expect(mouthLevelFromRms(0.5)).toBe(1)
  })
})

describe('smoothMouth', () => {
  it('attacks fast: one time constant covers ~63% of the gap', () => {
    const oneTau = smoothMouth(MOUTH_CLOSED_SCALE, MOUTH_OPEN_SCALE, MOUTH_ATTACK_MS)
    expect(oneTau).toBeCloseTo(MOUTH_CLOSED_SCALE + (MOUTH_OPEN_SCALE - MOUTH_CLOSED_SCALE) * (1 - Math.exp(-1)), 6)
    // 112ms（7 帧 16ms）就基本张开了，不会比字头慢
    let v = MOUTH_CLOSED_SCALE
    for (let i = 0; i < 7; i++)
      v = smoothMouth(v, MOUTH_OPEN_SCALE, 16)
    expect(v).toBeGreaterThan(MOUTH_OPEN_SCALE - 0.05)
  })

  it('never overshoots the target on either side', () => {
    expect(smoothMouth(1, 2.2, 10_000)).toBe(2.2)
    expect(smoothMouth(1, 0.55, 10_000)).toBe(0.55)
    expect(smoothMouth(1, 1, 16)).toBe(1)
  })

  it('closes within the 150ms budget once speech stops', () => {
    // 任务要求：说完/打断/静音后 ≤150ms 回到闭嘴
    expect(smoothMouth(MOUTH_OPEN_SCALE, MOUTH_CLOSED_SCALE, MOUTH_RELEASE_MS)).toBeCloseTo(MOUTH_CLOSED_SCALE, 10)
    expect(smoothMouth(MOUTH_OPEN_SCALE, MOUTH_CLOSED_SCALE, 150)).toBe(MOUTH_CLOSED_SCALE)
    // 半程线性：60ms 走一半
    const half = (MOUTH_OPEN_SCALE + MOUTH_CLOSED_SCALE) / 2
    expect(smoothMouth(MOUTH_OPEN_SCALE, MOUTH_CLOSED_SCALE, MOUTH_RELEASE_MS / 2)).toBeCloseTo(half, 10)
  })

  it('treats a bad dt as no movement', () => {
    expect(smoothMouth(1.4, 2.2, Number.NaN)).toBe(1.4)
    expect(smoothMouth(1.4, 0.55, -5)).toBe(1.4)
  })
})

describe('mouthScaleFor', () => {
  it('closes the mouth at rest and opens it with level', () => {
    expect(mouthScaleFor(0, false)).toBe(MOUTH_CLOSED_SCALE)
    expect(mouthScaleFor(1, false)).toBe(MOUTH_OPEN_SCALE)
    expect(mouthScaleFor(0.5, false)).toBeCloseTo(1.375, 6)
    expect(mouthScaleFor(2, false)).toBe(MOUTH_OPEN_SCALE)
    expect(mouthScaleFor(-1, false)).toBe(MOUTH_CLOSED_SCALE)
  })

  it('keeps the painted face almost still (the face mesh is skinned to zui)', () => {
    expect(mouthScaleFor(0, true)).toBeCloseTo(0.9, 6)
    expect(mouthScaleFor(1, true)).toBeCloseTo(1.1, 6)
  })
})

describe('stepFaceAlpha', () => {
  it('finishes the fade in exactly FACE_FADE_MS and clamps', () => {
    expect(stepFaceAlpha(0, 1, FACE_FADE_MS)).toBe(1)
    expect(stepFaceAlpha(1, 0, FACE_FADE_MS)).toBe(0)
    expect(stepFaceAlpha(0, 1, FACE_FADE_MS / 2)).toBeCloseTo(0.5, 6)
    expect(stepFaceAlpha(0.2, 0, 10_000)).toBe(0)
    expect(stepFaceAlpha(0.2, 1, 10_000)).toBe(1)
    expect(stepFaceAlpha(0.2, 1, Number.NaN)).toBe(0.2)
  })
})

describe('face micro motion', () => {
  it('sways a fraction of a degree around rest with a fixed period', () => {
    expect(faceSwayDeg(0)).toBeCloseTo(0, 10)
    expect(faceSwayDeg(FACE_SWAY_PERIOD_MS / 4)).toBeCloseTo(FACE_SWAY_DEG, 6)
    expect(faceSwayDeg(FACE_SWAY_PERIOD_MS * 3 / 4)).toBeCloseTo(-FACE_SWAY_DEG, 6)
    expect(faceSwayDeg(FACE_SWAY_PERIOD_MS)).toBeCloseTo(0, 6)
    for (let t = 0; t < 20_000; t += 37)
      expect(Math.abs(faceSwayDeg(t))).toBeLessThanOrEqual(FACE_SWAY_DEG + 1e-9)
  })

  it('breathes the overlay between the floor and full opacity', () => {
    for (let t = 0; t < 20_000; t += 41) {
      const a = faceOverlayAlpha(t)
      expect(a).toBeGreaterThanOrEqual(FACE_OVERLAY_MIN_ALPHA - 1e-9)
      expect(a).toBeLessThanOrEqual(1)
    }
    expect(faceOverlayAlpha(825)).toBeCloseTo(1, 6)
  })
})
