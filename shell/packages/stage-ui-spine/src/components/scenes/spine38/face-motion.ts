/**
 * Spine 3.8 脸部微动与口型的纯逻辑（不依赖渲染器，便于单测）。
 *
 * 这个模型的表情是整张贴图：`1`..`11` 都是 0 秒的整脸图，脸图一上，`normal` 里的眨眼和
 * 视线就被盖住了（见 tests/output/spine-survey-20261026/SURVEY.md）。所以"让脸活起来"
 * 只有两条路，这里各给一个出口：
 *   - 说话时让嘴骨跟上音量（`mouth*`）：原画静止态是张开 14px 的嘴，不说话时压到闭嘴值；
 *   - 脸图在场时用极小的头部晃动和叠层明暗代替表情（`face*`），别让它完全静止。
 */

/**
 * 嘴骨 `zui.scaleX` 的三个实测锚点（骨架单位换算成屏幕像素，2026-10-26 在生产壳量过）：
 * 0.55 → 嘴高约 6px（看得出的闭嘴），1.0 → 约 14px（原画静止态），2.2 → 约 35px（满开）。
 */
export const MOUTH_CLOSED_SCALE = 0.55
export const MOUTH_OPEN_SCALE = 2.2

/** 起音用指数平滑（时间常数 30ms）：比字头快，听感上不会"嘴慢半拍"。 */
export const MOUTH_ATTACK_MS = 30
/** 释放改用线性：120ms 一定走到闭嘴，满足"说完/打断/静音后 ≤150ms 回闭嘴"的硬要求。 */
export const MOUTH_RELEASE_MS = 120

/** RMS 噪声门与斜率：0.02 是 TTS 静音段的实测底噪，乘 6 后约 0.19 满幅。 */
export const MOUTH_NOISE_GATE = 0.02
export const MOUTH_RMS_GAIN = 6

/** 超过这么久没有新电平就当静音：TTS 会话断开或 Stage 的循环停掉时，嘴也不会卡在张开。 */
export const MOUTH_LEVEL_TIMEOUT_MS = 250

/** 脸图在场时 `zui` 骨对整张脸有权重（作者实测缩嘴骨有 44.5 万像素变化），摆动收到 ±10%。 */
export const MOUTH_FACE_IMAGE_SWING = 0.1

/** 换脸淡入淡出：附件是硬切的，会"闪一下"，用槽 `1` 的不透明度过渡。 */
export const FACE_FADE_MS = 180

/** 脸图期间头部微晃：±0.8°，4.7s 一圈；比 nod 的 9 单位小一个量级，叠在一起也不抢戏。 */
export const FACE_SWAY_DEG = 0.8
export const FACE_SWAY_PERIOD_MS = 4700

/** 9/10/11 的叠层（槽 `1-2`）慢呼吸：0.72..1.0，3.3s 一圈。 */
export const FACE_OVERLAY_MIN_ALPHA = 0.72
export const FACE_OVERLAY_PERIOD_MS = 3300

/**
 * Stage 的口型循环这一帧要不要推电平。
 *
 * 作者语音（`playVoice`）也在自己的 rAF 里写同一个电平，两个循环同时写会让嘴在 0 和实测值
 * 之间来回跳。所以只在真出声时推；刚停下那一帧推一次 0 收口，之后就不再推，
 * 静音由 Model 侧的 250ms 看门狗兜底。
 */
export type MouthPush = 'level' | 'silence' | 'none'

/**
 * @example
 * mouthPushFor(true, false)  // => 'level'
 * mouthPushFor(false, true)  // => 'silence'（说话刚停的那一帧）
 * mouthPushFor(false, false) // => 'none'（交给看门狗，别再和作者语音抢写）
 */
export function mouthPushFor(speaking: boolean, wasSpeaking: boolean): MouthPush {
  if (speaking)
    return 'level'
  return wasSpeaking ? 'silence' : 'none'
}

/**
 * 音频 RMS → 口型电平（0..1）：过噪声门后线性放大，到 1 截顶。
 *
 * @example
 * mouthLevelFromRms(0.01) // => 0（底噪）
 * mouthLevelFromRms(0.12) // => 0.6
 */
export function mouthLevelFromRms(rms: number): number {
  if (!(rms > MOUTH_NOISE_GATE))
    return 0
  return Math.min(1, (rms - MOUTH_NOISE_GATE) * MOUTH_RMS_GAIN)
}

/**
 * 嘴骨 scaleX 的一帧推进：起音指数、释放线性，值不会被推过目标。
 *
 * @example
 * smoothMouth(2.2, MOUTH_CLOSED_SCALE, 120) // => 0.55（120ms 内一定闭嘴）
 */
export function smoothMouth(current: number, target: number, dtMs: number): number {
  const dt = Number.isFinite(dtMs) ? Math.max(0, dtMs) : 0
  if (target > current) {
    const k = 1 - Math.exp(-dt / MOUTH_ATTACK_MS)
    return Math.min(target, current + (target - current) * k)
  }
  const step = (MOUTH_OPEN_SCALE - MOUTH_CLOSED_SCALE) * dt / MOUTH_RELEASE_MS
  return Math.max(target, current - step)
}

/**
 * 电平 → 嘴骨 scaleX。脸图在场时只做 ±10% 的小幅呼吸：脸图的嘴是画上去的，大幅缩放会把
 * 腮和下巴一起拉变形。
 *
 * @example
 * mouthScaleFor(0, false) // => 0.55（闭嘴）
 * mouthScaleFor(1, false) // => 2.2（满开）
 * mouthScaleFor(1, true)  // => 1.1（脸图在场，只给一点起伏）
 */
export function mouthScaleFor(level: number, faceImage: boolean): number {
  const l = Math.min(1, Math.max(0, Number.isFinite(level) ? level : 0))
  if (faceImage)
    return 1 - MOUTH_FACE_IMAGE_SWING + 2 * MOUTH_FACE_IMAGE_SWING * l
  return MOUTH_CLOSED_SCALE + (MOUTH_OPEN_SCALE - MOUTH_CLOSED_SCALE) * l
}

/**
 * 换脸不透明度的一帧推进：线性，`FACE_FADE_MS` 内一定走到目标。
 *
 * @example
 * stepFaceAlpha(1, 0, 180) // => 0
 */
export function stepFaceAlpha(current: number, target: number, dtMs: number): number {
  const dt = Number.isFinite(dtMs) ? Math.max(0, dtMs) : 0
  const step = dt / FACE_FADE_MS
  return target > current ? Math.min(target, current + step) : Math.max(target, current - step)
}

/** 脸图在场时的头部微晃角度（度）。 */
export function faceSwayDeg(elapsedMs: number): number {
  const t = Number.isFinite(elapsedMs) ? elapsedMs : 0
  return FACE_SWAY_DEG * Math.sin(2 * Math.PI * t / FACE_SWAY_PERIOD_MS)
}

/** 9/10/11 叠层的慢呼吸不透明度（已含 1.0 上限，乘在淡入淡出上）。 */
export function faceOverlayAlpha(elapsedMs: number): number {
  const t = Number.isFinite(elapsedMs) ? elapsedMs : 0
  const wave = (Math.sin(2 * Math.PI * t / FACE_OVERLAY_PERIOD_MS) + 1) / 2
  return FACE_OVERLAY_MIN_ALPHA + (1 - FACE_OVERLAY_MIN_ALPHA) * wave
}
