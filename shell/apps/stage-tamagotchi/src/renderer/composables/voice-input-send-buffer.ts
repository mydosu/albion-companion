/**
 * 用户说完一段之后，等多久才把整段发给她（指挥官 2026-10-25）。
 *
 * 为什么：原来壳收到一段识别结果只等 500ms 就发，用户只是换口气（停一下）就被当成说完，
 * 一段话被拆成两三次发送。现在 2 秒内又来了新的语音/识别结果就接着拼、重新计时，
 * 2 秒内没动静才把拼好的整段一次发出去。
 *
 * 光有 2 秒还不够：这个 2 秒是从「拿到识别结果」算起的，而用户停顿后接着说，下一段要
 * 等他说完（VAD 静音 1.2 秒）再等 whisper 转写回来，加起来往往超过 2 秒 —— 第一段照样先发。
 * 所以还有 `hold()`/`resume()`：VAD 一判断有人开口就先按住不发，等这一段的结局
 * （结果 / 空 / 报错 / 被闸门挡掉 / 停麦）再恢复计时。
 *
 * 按键说话（push-to-talk）松开按键照旧立刻发：停止麦克风时会 dispose()，直接冲掉这个等待。
 */
export const VOICE_INPUT_SEND_DEBOUNCE_MS = 2_000

/**
 * 按住发送的最长时间（保险）：VAD 一直不结束、或者哪个结局回调没来，也不能把话一直卡着。
 * 超过就照常发出去。
 */
export const VOICE_INPUT_HOLD_MAX_MS = 15_000

/** 一段话攒到这个长度就不再等，直接发（沿用原来的 90，避免一口气说太久才发）。 */
const MAX_BUFFERED_TEXT_LENGTH = 90

const CJK_BOUNDARY_RE = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]$/u
const CJK_START_RE = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u

/** 两段识别结果之间的空格规则：中文/日文/韩文之间不加空格，其余按词加空格。 */
export function joinTranscriptFragments(previous: string, next: string) {
  if (!previous)
    return next

  if (CJK_BOUNDARY_RE.test(previous) && CJK_START_RE.test(next))
    return `${previous}${next}`

  return `${previous} ${next}`
}

export interface VoiceInputSendBufferOptions {
  /** 真正的发送动作：拼好的一整段只调一次。 */
  send: (text: string) => Promise<void> | void
  /** 每次拼接后回调，用于让字幕显示已经累积的整段文字。 */
  onAccumulatedText?: (text: string) => void
}

export interface VoiceInputSendBuffer {
  /** 收到一段最终识别文字：拼到待发的一整段里，并重新开始计 2 秒。 */
  push: (text: string) => void
  /**
   * 暂停发送计时（有人开始说话了）。缓冲里没有待发文字时也要记下（A 还在转写、B 已经开口）。
   *
   * 每一段的「开口」对应一个结局：`push`（有结果）或 `resume`（空 / 报错 / 被闸门挡掉 / 停麦），
   * 各消费一次；叠起来就是「上一段还在转写、下一段已经开口」。
   */
  hold: () => void
  /** 这一段结束了（空结果 / 报错 / 被闸门挡掉 / 停麦）：恢复计时。 */
  resume: () => void
  /** 立刻把待发的整段发出去（按键说话松开、麦克风要关时用）。 */
  flushNow: () => Promise<void>
  /** 丢掉待发文字，不发。 */
  clear: () => void
  /** 立刻发掉待发文字（dispose 语义，和 flushNow 等价，留给 stop 流程用）。 */
  dispose: () => Promise<void>
  /** 已经攒着、还没发出去的整段文字（流式那一句的 partial 要接在它后面显示）。 */
  readonly pendingText: string
}

/**
 * 语音输入这一层的发送防抖。
 *
 * 为什么自己写一个而不是复用 `@proj-airi/pipelines-audio` 的 `createTranscriptBuffer`：
 * 那个包在壳里是按 `dist/index.mjs` 吃进去的（`electron.vite.config.ts` 没有把它 alias 到 src），
 * 改它的源码得先单独重build 才有效果，而 `dist` 不进 Git —— 一改就变成「本机能跑、换机器失效」。
 * 所以这段逻辑放在壳里（本来就要显示「已经累积的整段」，也就是同一个字符串）。
 */
export function createVoiceInputSendBuffer(options: VoiceInputSendBufferOptions): VoiceInputSendBuffer {
  let pendingText = ''
  let flushTimer: ReturnType<typeof setTimeout> | undefined
  let holdCount = 0
  let holdCapTimer: ReturnType<typeof setTimeout> | undefined

  function clearFlushTimer() {
    if (!flushTimer)
      return

    clearTimeout(flushTimer)
    flushTimer = undefined
  }

  function clearHoldCapTimer() {
    if (!holdCapTimer)
      return

    clearTimeout(holdCapTimer)
    holdCapTimer = undefined
  }

  function flushNow() {
    clearFlushTimer()
    clearHoldCapTimer()
    holdCount = 0

    const text = pendingText.trim()
    pendingText = ''
    if (!text)
      return Promise.resolve()

    return Promise.resolve(options.send(text))
  }

  function scheduleFlush() {
    clearFlushTimer()

    // 按住的这段时间不发：等 resume（或者 15 秒保险）再说
    if (holdCount > 0)
      return

    flushTimer = setTimeout(() => {
      flushTimer = undefined
      void flushNow()
    }, VOICE_INPUT_SEND_DEBOUNCE_MS)
  }

  function hold() {
    // 缓冲还是空的时候也要记下这一次：最常见的就是「A 还在转写、用户已经开口说 B」——
    // 这时候 A 的结果没回来、缓冲是空的，如果不记，A 的 push 一到就开始计 2 秒，
    // B 还没说完 A 就先发出去了。
    if (holdCount === 0) {
      holdCapTimer = setTimeout(() => {
        holdCapTimer = undefined
        holdCount = 0
        void flushNow()
      }, VOICE_INPUT_HOLD_MAX_MS)
    }

    holdCount += 1
    clearFlushTimer()
  }

  function resume() {
    if (holdCount === 0)
      return

    holdCount -= 1
    if (holdCount > 0)
      return

    clearHoldCapTimer()
    scheduleFlush()
  }

  function push(text: string) {
    const trimmed = text.trim()
    if (!trimmed)
      return

    pendingText = joinTranscriptFragments(pendingText, trimmed)
    // 先让字幕看到「已经拼好的整段」，再决定是立刻发还是继续等
    options.onAccumulatedText?.(pendingText)

    // 这条结果就是刚才按住的那一段：消费掉一个 hold
    if (holdCount > 0) {
      holdCount -= 1
      if (holdCount === 0)
        clearHoldCapTimer()
    }

    if (pendingText.length >= MAX_BUFFERED_TEXT_LENGTH) {
      void flushNow()
      return
    }

    scheduleFlush()
  }

  function clear() {
    clearFlushTimer()
    clearHoldCapTimer()
    holdCount = 0
    pendingText = ''
  }

  return {
    push,
    hold,
    resume,
    flushNow,
    clear,
    dispose: flushNow,
    get pendingText() {
      return pendingText
    },
  }
}
