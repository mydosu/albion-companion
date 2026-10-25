import type {
  PlaybackEndEvent,
  PlaybackInterruptEvent,
  PlaybackItem,
  PlaybackRejectEvent,
  PlaybackStartEvent,
} from '@proj-airi/pipelines-audio'

export interface PlaybackSpeakingStateManager<TAudio> {
  onStart: (listener: (event: PlaybackStartEvent<TAudio>) => void) => void
  onEnd: (listener: (event: PlaybackEndEvent<TAudio>) => void) => void
  onInterrupt: (listener: (event: PlaybackInterruptEvent<TAudio>) => void) => void
  onReject: (listener: (event: PlaybackRejectEvent<TAudio>) => void) => void
}

export interface PlaybackSpeakingStateHandlers<TAudio> {
  /**
   * 说话状态。**第二个参数是这个回调对应的 item**（`onStart`/`onEnd` 有，`interrupt`/`reject` 也带）——
   * 宿主必须**按 item** 判断它是不是流式轮，不能按"当前会话"：服务端 `onDone` 会先把会话清掉，
   * 而 M 可能还没启动排队的 item、或时钟里还有尾音（codex 复核指出）。
   */
  setSpeaking: (value: boolean, item?: PlaybackItem<TAudio>) => void
  onStart?: (event: PlaybackStartEvent<TAudio>) => void
  /** 正常播完（不是 interrupt / reject）。宿主用它上报真实播放进度。 */
  onEnd?: (event: PlaybackEndEvent<TAudio>) => void
}

/**
 * Binds assistant speaking state to every terminal playback outcome.
 *
 * Use when:
 * - UI state must show whether assistant audio is currently audible.
 * - Voice input should be suspended only while playback is actually active.
 *
 * Expects:
 * - Playback managers emit exactly one terminal event for each accepted item.
 *
 * Returns:
 * - Nothing; listeners are registered on the provided manager.
 */
export function bindSpeakingStateToPlaybackManager<TAudio>(
  manager: PlaybackSpeakingStateManager<TAudio>,
  handlers: PlaybackSpeakingStateHandlers<TAudio>,
) {
  manager.onStart((event) => {
    handlers.setSpeaking(true, event.item)
    handlers.onStart?.(event)
  })

  manager.onEnd((event) => {
    handlers.setSpeaking(false, event.item)
    handlers.onEnd?.(event)
  })

  manager.onInterrupt((event) => {
    handlers.setSpeaking(false, (event as { item?: PlaybackItem<TAudio> })?.item)
  })

  manager.onReject((event) => {
    handlers.setSpeaking(false, (event as { item?: PlaybackItem<TAudio> })?.item)
  })
}
