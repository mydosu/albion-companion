/**
 * 流式轮的"这一轮还在播"状态（方案 §4.3 接头片）。
 *
 * 为什么不能按 `currentSession` 判断：服务端 `onDone` 会先把会话清掉，而播放管理器可能
 * **还没启动**排队的 item（首段更晚到），或者时钟里还有尾音。用会话判断就会出现
 * "M 启动流式 item 时被当成老路径"（嘴提前动、门闩提前放行）和"会话已清 ⇒ 永远不闭嘴"。
 *
 * 所以状态**只跟着 M 的事件里的 item 走**：`onItemStart` / `onItemEnd` / `reset`。
 * 宿主在段级回调里用 `pending` 判断"这一轮的流式 item 是否仍然有效"，防止上一轮的
 * 晚到定时器影响新一轮。
 */
export interface StreamTurnTracker<TItem> {
  /** 这个 M item 是不是流式轮的 item（带动钟的占位 item）。`setSpeaking` 用它。 */
  isStreamItem: (item: TItem | undefined) => boolean
  /** M 的 `onStart`：返回 true = 流式轮，宿主跳过老路径（置真/字幕）。 */
  onItemStart: (item: TItem | undefined) => boolean
  /** M 的 `onEnd`：返回 true = 流式轮结束。 */
  onItemEnd: (item: TItem | undefined) => boolean
  /** 打断 / 拒绝 / 取消：没有 item 上下文时的兜底清理。 */
  reset: () => void
  /** 这一轮的流式 item 还没结束。 */
  readonly pending: boolean
}

export function createStreamTurnTracker<TItem>(): StreamTurnTracker<TItem> {
  let pending = false
  const isStreamItem = (item: TItem | undefined) =>
    !!(item as { clock?: unknown } | undefined)?.clock
  return {
    isStreamItem,
    onItemStart(item) {
      if (!isStreamItem(item))
        return false
      pending = true
      return true
    },
    onItemEnd(item) {
      if (!isStreamItem(item))
        return false
      pending = false
      return true
    },
    reset() {
      pending = false
    },
    get pending() {
      return pending
    },
  }
}
