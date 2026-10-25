import type { StreamingPlaybackStopEvent } from './streaming-playback'

/**
 * 跨轮「新一轮首段不许早于此刻起播」屏障（单位：AudioContext 时间轴秒）。
 *
 * 为什么要**跨轮**、并且按 AudioContext 存：M 的 abort 会**立刻**放掉旧 item（不等淡出结束），
 * 所以旧 clock 的 `drained` 拦不住下一轮；屏障不属于某一个 clock，得挂在上下文上。
 * 只挡下一轮的**第一段起点**（`arm(notBefore)`），轮内 `nextStart` 的节奏不受影响。
 *
 * 抽成独立小模块是为了能直接测宿主接线（sol-medium 复核 §4：手写 `arm(fadeEnd + 0.02)`
 * 只证明 clock 收屏障，证明不了 Stage 那条链）。
 */
const GAP_SECONDS = 0.02

/**
 * 旧轮**自然播完**（不是被打断）之后，新一轮首段至少等这个时长（秒）。
 *
 * 为什么单独有一条：淡出屏障只在**取消**路径被写（`onStopped({fadeEnd})`），
 * 旧轮正常说完时屏障里一条记录都没有 ⇒ 新轮的音频一就绪就紧贴着上一段起播。
 * 真机 2026-09-30 16:13 抓到 100 / 180 / 100ms 的接续，听感就是"抢句"（sol-high 方案 §二）。
 *
 * 取值：0.40 是**工程试验初值**，目前**没有任何听测证据**支持某个具体数值；
 * 指挥官的耳朵定稿后可对比 300 / 400。回退值 0 = 恢复本次改动前的行为。
 */
const NATURAL_TURN_GAP_SECONDS = 0.4

export function createSpeechBarrier(
  gapSeconds = GAP_SECONDS,
  naturalTurnGapSeconds = NATURAL_TURN_GAP_SECONDS,
) {
  const fadeMap = new WeakMap<BaseAudioContext, number>()
  const naturalMap = new WeakMap<BaseAudioContext, number>()
  const keepMax = (m: WeakMap<BaseAudioContext, number>, ctx: BaseAudioContext, v: number) => {
    m.set(ctx, Math.max(m.get(ctx) ?? 0, v))
  }
  return {
    /** 记下旧轮淡出终点。`null`/没有淡出 = 什么都不做；取 max ⇒ 只前进不后退。 */
    noteFadeEnd(ctx: BaseAudioContext, fadeEnd: number | null | undefined) {
      if (fadeEnd == null)
        return
      keepMax(fadeMap, ctx, fadeEnd + gapSeconds)
    },
    /**
     * 记下旧轮**自然播完**的最后一段终点（`onNaturalEnd` 的入参，AudioContext 时间轴秒）。
     * 与取消路径分开记：两条路互不覆盖，`notBefore` 取两者较大值。
     */
    noteNaturalEnd(ctx: BaseAudioContext, endAt: number | null | undefined) {
      if (endAt == null)
        return
      keepMax(naturalMap, ctx, endAt + naturalTurnGapSeconds)
    },
    /** 新一轮首段不许早于这个时刻；这一轮之前没人说过话就是 0（= 不挡）。 */
    notBefore(ctx: BaseAudioContext) {
      return Math.max(fadeMap.get(ctx) ?? 0, naturalMap.get(ctx) ?? 0)
    },
  }
}

export type SpeechBarrier = ReturnType<typeof createSpeechBarrier>

/**
 * 宿主侧的**生产接线**（sol-medium 复核 §3）：Stage 用这两个函数，测试也直接调它们 ——
 * 免得测试自己拼 `fadeEnd + 0.02`、而真正的宿主接线没人守。
 */

/** 「时钟的取消回调 → 写屏障」：给 `createStreamingTtsSession({ onPlaybackStopped })` 用。 */
export function bindFadeBarrier(barrier: SpeechBarrier, ctx: BaseAudioContext | undefined) {
  return ({ reason, fadeEnd, completedEndAt }: StreamingPlaybackStopEvent) => {
    if (!ctx)
      return
    barrier.noteFadeEnd(ctx, fadeEnd)
    // Cancelling between segments must preserve the gap after a completed tail.
    if (reason === 'new-message' && fadeEnd == null)
      barrier.noteNaturalEnd(ctx, completedEndAt)
  }
}

/** 「时钟的自然轮末回调 → 写屏障」：给 `onPlaybackNaturalEnd` 用（旧轮正常说完那条路）。 */
export function bindNaturalEndBarrier(barrier: SpeechBarrier, ctx: BaseAudioContext | undefined) {
  return (endAt: number | null) => {
    if (!ctx)
      return
    barrier.noteNaturalEnd(ctx, endAt)
  }
}

/** 「新一轮 arm 时读屏障」：给 `clock.arm(...)` 用。 */
export function barrierArmValue(barrier: SpeechBarrier, ctx: BaseAudioContext | undefined) {
  return ctx ? barrier.notBefore(ctx) : 0
}
