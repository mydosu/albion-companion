import { describe, expect, it } from 'vitest'

import {
  FACE_FADE_MS,
  FACE_OVERLAY_MIN_ALPHA,
  FACE_SWAY_DEG,
  FACE_SWAY_PERIOD_MS,
  faceFadeStep,
  faceOverlayAlpha,
  faceSwayDeg,
  IDLE_LOOP_SEC,
  IDLE_SWING_MAX_DEFER_MS,
  idleSwingDeferMs,
  MOUTH_ATTACK_MS,
  MOUTH_CLOSED_SCALE,
  MOUTH_FACE_IMAGE_CLOSED,
  MOUTH_FACE_IMAGE_OPEN,
  MOUTH_OPEN_SCALE,
  MOUTH_RELEASE_MS,
  mouthLevelFromRms,
  mouthPushFor,
  mouthScaleFor,
  mouthScaleForFace,
  rmsOfByteTimeDomain,
  smoothMouth,
  stepFaceAlpha,
} from './face-motion'

describe('rmsOfByteTimeDomain', () => {
  it('is 0 for silence or empty input, full-scale square wave is ~1, a half-scale wave is ~0.5', () => {
    expect(rmsOfByteTimeDomain(new Uint8Array([]))).toBe(0)
    expect(rmsOfByteTimeDomain(new Uint8Array(512).fill(128))).toBe(0)
    expect(rmsOfByteTimeDomain(new Uint8Array([0, 256 - 1, 0, 255]))).toBeCloseTo(0.9961, 3)
    expect(rmsOfByteTimeDomain(new Uint8Array([192, 64, 192, 64]))).toBeCloseTo(0.5, 6)
  })

  it('feeds mouthLevelFromRms: quiet noise stays below the gate, speech opens the mouth', () => {
    expect(mouthLevelFromRms(rmsOfByteTimeDomain(new Uint8Array([129, 127, 129, 127])))).toBe(0)
    expect(mouthLevelFromRms(rmsOfByteTimeDomain(new Uint8Array([160, 96, 160, 96])))).toBeGreaterThan(0.5)
  })
})

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
    expect(mouthScaleFor(0, true)).toBe(MOUTH_FACE_IMAGE_CLOSED)
    expect(mouthScaleFor(1, true)).toBe(MOUTH_FACE_IMAGE_OPEN)
  })

  // ROOT CAUSE: 触摸反应播作者 wav 时 pose 轨在播，旧写法 `posing ? 1 : …` 把嘴钉在 1.0，嘴不跟声音动。
  // 现在 pose 期间（脸图 alpha=1）和脸图说话走同一条规则：电平越大嘴越开，单调变化。
  it('moves with the audio level while a pose face is fully shown (no longer pinned)', () => {
    const at = (l: number) => mouthScaleForFace(l, 1)
    expect(at(0)).toBe(MOUTH_FACE_IMAGE_CLOSED)
    expect(at(1)).toBe(MOUTH_FACE_IMAGE_OPEN)
    expect(at(0.5)).toBeGreaterThan(at(0.2))
    expect(at(0.2)).toBeGreaterThan(at(0))
    expect(new Set([0, 0.25, 0.5, 0.75, 1].map(at)).size).toBe(5)
  })
})

describe('mouthScaleForFace', () => {
  it('is the bare mouth without a face and the painted-face mouth at full alpha', () => {
    expect(mouthScaleForFace(0.3, 0)).toBeCloseTo(mouthScaleFor(0.3, false), 9)
    expect(mouthScaleForFace(0.3, 1)).toBeCloseTo(mouthScaleFor(0.3, true), 9)
    expect(mouthScaleForFace(0, Number.NaN)).toBe(MOUTH_CLOSED_SCALE)
  })

  // ROOT CAUSE: 脸图一上，旧写法按「有没有脸附件」二选一，zui 从 0.55 在一两帧内跳到 0.9，撤脸时再跳回。
  // 现在跟着淡入淡出 alpha 插值：相邻帧变化 ≤ 0.05。
  it.each([['in', 0, 1], ['out', 1, 0]] as const)('fading %s moves zui by ≤0.05 per 60fps frame (through smoothMouth)', (_n, from, to) => {
    let alpha: number = from
    let zui = mouthScaleForFace(0, from)
    let maxStep = 0
    for (let i = 0; i < 40; i++) {
      alpha = stepFaceAlpha(alpha, to, 1000 / 60)
      const next = smoothMouth(zui, mouthScaleForFace(0, alpha), 1000 / 60)
      maxStep = Math.max(maxStep, Math.abs(next - zui))
      zui = next
    }
    expect(maxStep).toBeLessThanOrEqual(0.05)
    expect(zui).toBeCloseTo(mouthScaleFor(0, to === 1), 6)
  })
})

describe('idleSwingDeferMs', () => {
  // 窗口来自 2026-10-30 实测：normal 一圈里 3.5–4.5s 和 8.5–9.0s 是快速摆头
  it('defers a face change that lands inside a swing window until the window ends', () => {
    expect(idleSwingDeferMs(3.5)).toBe(1000)
    expect(idleSwingDeferMs(3.9)).toBeCloseTo(600, -1)
    expect(idleSwingDeferMs(8.6)).toBeCloseTo(400, -1)
    // 取模：第二圈同相位一样
    expect(idleSwingDeferMs(IDLE_LOOP_SEC + 3.9)).toBeCloseTo(600, -1)
    expect(idleSwingDeferMs(3.9)).toBeLessThanOrEqual(IDLE_SWING_MAX_DEFER_MS)
  })

  it('changes immediately outside the windows (end is exclusive) and on bad input', () => {
    for (const t of [0, 2.9, 3.49, 4.5, 6, 8.49, 9.0, 13])
      expect(idleSwingDeferMs(t)).toBe(0)
    expect(idleSwingDeferMs(Number.NaN)).toBe(0)
    expect(idleSwingDeferMs(4, 0)).toBe(0)
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

describe('mouthPushFor', () => {
  it('only makes the Stage loop push the level it measured while speaking', () => {
    expect(mouthPushFor(true, false)).toBe('level')
    expect(mouthPushFor(true, true)).toBe('level')
  })

  it('pushes one zero on the frame speech stops, then goes quiet', () => {
    // ROOT CAUSE: 之前不说话时每帧都推 0，作者语音（摸头/点身体）自己的 rAF 每帧推实测值，
    // 两个循环抢着写同一个电平 → 作者语音期间嘴在 0 和实测值之间跳。
    // 现在停下的那一帧推 0 收口，之后交给 Model 的 250ms 看门狗。
    expect(mouthPushFor(false, true)).toBe('silence')
    expect(mouthPushFor(false, false)).toBe('none')
  })
})

describe('faceFadeStep', () => {
  it('walks alpha toward the target and only reports completion after it hits 0', () => {
    const mid = faceFadeStep({ alpha: 1, target: 0, fadingOut: true }, FACE_FADE_MS / 2)
    expect(mid.alpha).toBeCloseTo(0.5, 6)
    expect(mid.completed).toBe(false)
    const end = faceFadeStep({ alpha: 0.5, target: 0, fadingOut: true }, FACE_FADE_MS)
    expect(end).toEqual({ alpha: 1, completed: true })
  })

  // ROOT CAUSE:
  // 换脸那一帧 alpha 被置 0（淡入还没开始），同一帧又 setFace('normal') → target 也是 0。
  // 旧写法把完成判断塞在 `if (alpha !== target)` 里，这个状态永远进不去：脸图隐形挂在 0 轨上、
  // 眼槽又是 None，结果整张脸没有眼睛。
  it('completes a fade-out that starts already at alpha === target === 0', () => {
    expect(faceFadeStep({ alpha: 0, target: 0, fadingOut: true }, 16)).toEqual({ alpha: 1, completed: true })
  })

  it('does nothing when not fading and already at the target', () => {
    expect(faceFadeStep({ alpha: 1, target: 1, fadingOut: false }, 16)).toEqual({ alpha: 1, completed: false })
    expect(faceFadeStep({ alpha: 0.3, target: 0.3, fadingOut: false }, 16)).toEqual({ alpha: 0.3, completed: false })
  })

  it('never reports completion while only fading in', () => {
    expect(faceFadeStep({ alpha: 0, target: 1, fadingOut: false }, FACE_FADE_MS)).toEqual({ alpha: 1, completed: false })
  })
})
