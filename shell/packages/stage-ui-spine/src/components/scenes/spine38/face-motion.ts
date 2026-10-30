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

/**
 * 脸图在场时 `zui.scaleX` 的闭嘴 / 满开值。脸图的嘴是画上去的，`zui` 对整张脸有权重，所以范围比无脸时小得多。
 * 2026-10-30 在生产壳上对脸 2/5/8/9 逐档截图（tests/output/promo-face-20261030/mouth/）：
 * ±10% 肉眼几乎看不出在动，0.6–1.4 嘴形变化清楚、腮和下巴没有被拉变形。
 * 闭嘴值留 0.9（接近原画，没人说话时脸不变形），满开 1.4。
 */
export const MOUTH_FACE_IMAGE_CLOSED = 0.9
export const MOUTH_FACE_IMAGE_OPEN = 1.4

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
 * AnalyserNode.getByteTimeDomainData 的一帧样本（0..255，128 为零点）→ RMS（0..1）。
 * 作者 wav 的口型和 TTS 共用 {@link mouthLevelFromRms} 之后的那一段。
 *
 * @example
 * rmsOfByteTimeDomain(new Uint8Array([128, 128])) // => 0
 * rmsOfByteTimeDomain(new Uint8Array([255, 1]))   // => 0.9921875
 */
export function rmsOfByteTimeDomain(samples: ArrayLike<number>): number {
  if (!samples.length)
    return 0
  let sum = 0
  for (let i = 0; i < samples.length; i++) {
    const d = (samples[i] - 128) / 128
    sum += d * d
  }
  return Math.sqrt(sum / samples.length)
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
 * 电平 → 嘴骨 scaleX。脸图在场时只在 0.9..1.4 之间动（见 MOUTH_FACE_IMAGE_*）。
 * 触摸说话和 TTS 说话共用这条规则。
 *
 * @example
 * mouthScaleFor(0, false) // => 0.55（闭嘴）
 * mouthScaleFor(1, false) // => 2.2（满开）
 * mouthScaleFor(1, true)  // => 1.4（脸图在场，满开）
 */
export function mouthScaleFor(level: number, faceImage: boolean): number {
  const l = Math.min(1, Math.max(0, Number.isFinite(level) ? level : 0))
  if (faceImage)
    return MOUTH_FACE_IMAGE_CLOSED + (MOUTH_FACE_IMAGE_OPEN - MOUTH_FACE_IMAGE_CLOSED) * l
  return MOUTH_CLOSED_SCALE + (MOUTH_OPEN_SCALE - MOUTH_CLOSED_SCALE) * l
}

/**
 * 嘴骨 scaleX，随脸图不透明度在「无脸」和「脸图」两套结果之间插值。
 * 脸图一上 / 一撤，两套结果差 0.35（闭嘴 0.55 ↔ 脸图 0.9），直接切会在一两帧里跳变（2026-10-30 实测），
 * 所以跟着 `FACE_FADE_MS` 的 alpha 走，180ms 内线性过渡。`faceAlpha` 在没有脸图时传 0。
 *
 * @example
 * mouthScaleForFace(0, 0)   // => 0.55
 * mouthScaleForFace(0, 0.5) // => 0.725
 * mouthScaleForFace(0, 1)   // => 0.9
 */
export function mouthScaleForFace(level: number, faceAlpha: number): number {
  const a = Math.min(1, Math.max(0, Number.isFinite(faceAlpha) ? faceAlpha : 0))
  const bare = mouthScaleFor(level, false)
  return bare + (mouthScaleFor(level, true) - bare) * a
}

/**
 * 作者 `normal` 待机的一圈时长（秒）和其中两段快速摆头的相位窗口 [起, 止]。
 * 来源：2026-10-30 生产壳 30fps 逐帧采样 `lian` 骨（tests/output/promo-face-20261030/live/phase.py），
 * 快摆出现在约 3.5–4.5s 和 8.5–9.0s。换脸的淡入若同时落在这两段，叠上去就是「抽一下」。
 */
export const IDLE_LOOP_SEC = 13.367
export const IDLE_SWING_WINDOWS: ReadonlyArray<readonly [number, number]> = [[3.5, 4.5], [8.5, 9.0]]
/** 最多推迟这么久：窗口最长 1s，超过就不值得让 ACT 的脸晚出。 */
export const IDLE_SWING_MAX_DEFER_MS = 1000

/**
 * 现在（待机播放头 `trackTime` 秒）换脸要不要等：落在快摆窗口里返回要等的毫秒数（≤1000），否则 0。
 *
 * @example
 * idleSwingDeferMs(3.9) // => 600（等到 4.5s）
 * idleSwingDeferMs(2.0) // => 0
 */
export function idleSwingDeferMs(trackTime: number, loopSec = IDLE_LOOP_SEC): number {
  if (!Number.isFinite(trackTime) || !(loopSec > 0))
    return 0
  const phase = ((trackTime % loopSec) + loopSec) % loopSec
  for (const [from, to] of IDLE_SWING_WINDOWS) {
    if (phase >= from && phase < to)
      return Math.min(IDLE_SWING_MAX_DEFER_MS, Math.ceil((to - phase) * 1000))
  }
  return 0
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

export interface FaceFadeState {
  alpha: number
  target: number
  /** 正在淡出：这张脸还挂在 0 轨上，等 alpha 到 0 才真的切回 normal。 */
  fadingOut: boolean
}

export interface FaceFadeStep {
  alpha: number
  /** 淡出已经走完：调用方要把 `表情` 归零、syncIdle，并把 alpha/target 复位成 1。 */
  completed: boolean
}

/**
 * 换脸不透明度的状态机（一帧）。完成判断**不能**放在「alpha 还没到 target」的分支里。
 *
 * 为什么：换脸那一帧 alpha 可能刚被置 0（淡入还没开始），同一帧又 setFace('normal') → target 也是 0，
 * `alpha !== target` 不成立 → 完成分支永远进不去，脸图会隐形挂在 0 轨上、眼槽又是 None，等于没有眼睛。
 * 这条是**读代码推断**出的死锁路径，尚未在真机上撞到（2026-10-26）。
 *
 * 所以这里先推进 alpha，再独立判断「fadingOut 且 alpha === 0」。
 *
 * @example
 * faceFadeStep({ alpha: 1, target: 0, fadingOut: true }, 90)  // => { alpha: 0.5, completed: false }
 * faceFadeStep({ alpha: 1, target: 0, fadingOut: true }, 200) // => { alpha: 1, completed: true }（一步到 0 并收尾）
 * faceFadeStep({ alpha: 0, target: 0, fadingOut: true }, 16)  // => { alpha: 1, completed: true }
 */
export function faceFadeStep(state: FaceFadeState, dtMs: number): FaceFadeStep {
  const alpha = state.alpha === state.target ? state.alpha : stepFaceAlpha(state.alpha, state.target, dtMs)
  if (state.fadingOut && alpha === 0)
    return { alpha: 1, completed: true }
  return { alpha, completed: false }
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
