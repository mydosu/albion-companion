import type { Ref } from 'vue'

import { watch } from 'vue'

/** 等她说完的上限。超时就放行，宁可掐断她的话，也不吞掉用户的话。 */
export const ASSISTANT_TURN_WAIT_TIMEOUT_MS = 30_000

/**
 * 等她把上一轮说完再把新话交给聊天队列。
 *
 * 背景（阿尔比恩自加，2026-09-25）：Stage.vue 在新一轮开始时调用
 * playbackManager.stopAll('new-message')，会掐断上一轮正在播的语音；
 * 所以第二句要先等她说完，否则前半句就听不全。
 * 无声音、播放失败或超时都放行 —— 绝不能因此把用户的话卡住。
 */
export function waitForAssistantTurnToSettle(
  busy: Ref<boolean>,
  timeoutMs = ASSISTANT_TURN_WAIT_TIMEOUT_MS,
): Promise<void> {
  if (!busy.value)
    return Promise.resolve()

  return new Promise<void>((resolve) => {
    let timer: ReturnType<typeof setTimeout> | undefined

    const stopWatch = watch(busy, (value) => {
      if (value)
        return
      finish()
    })

    function finish() {
      clearTimeout(timer)
      stopWatch()
      resolve()
    }

    timer = setTimeout(() => {
      console.warn(`[Main Page] Assistant turn still busy after ${timeoutMs}ms; sending the new message anyway.`)
      finish()
    }, timeoutMs)
  })
}
