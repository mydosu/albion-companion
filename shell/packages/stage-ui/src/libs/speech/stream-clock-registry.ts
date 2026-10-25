import type { StreamingPlayback } from './streaming-playback'

/**
 * 宿主侧的「还在跑的流式 clock」集合（sol-medium 复核 §2 的阻塞点）。
 *
 * 为什么需要它：`new-message` 打断后，M **立刻**把旧 item 放掉、`currentSession` 也换成新轮，
 * 旧 clock 只剩 `playFunction` 的闭包还引用着 —— 它还在跑 100ms 淡出。
 * 这时静音 / 卸载走的是 `stopSpeechOutput()`：它只碰 `currentSession`、
 * `speechPipeline`、`playbackManager`，**都够不到那份旧 clock** ⇒ 旧句尾巴照原样播完。
 *
 * 所以宿主自己持有一份：接管 clock 时登记、`drained` 时移除，静音/卸载时遍历收尾巴。
 * `abortCancelledTails()` 对没取消过的 clock 是 no-op，只对残留的取消尾巴动手。
 */
export function createStreamClockRegistry() {
  const clocks = new Set<StreamingPlayback>()
  return {
    /** `playFunction` 接管这个 clock（拿到所有权）时登记。 */
    add(clock: StreamingPlayback) {
      clocks.add(clock)
    },
    /** 该 clock `drained`（`finally`）时移除。 */
    remove(clock: StreamingPlayback) {
      clocks.delete(clock)
    },
    /** 静音 / 卸载 / 强制输出清理：把残留的取消尾巴立刻收掉（正常播放的 clock 不受影响）。 */
    sweepCancelledTails() {
      for (const clock of clocks)
        clock.abortCancelledTails()
    },
    /** 诊断/测试用。 */
    size() {
      return clocks.size
    },
  }
}

export type StreamClockRegistry = ReturnType<typeof createStreamClockRegistry>
