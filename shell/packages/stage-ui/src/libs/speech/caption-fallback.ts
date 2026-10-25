/**
 * 语音服务挂了的字幕兜底。
 *
 * 为什么要有：字幕只有两条来源——9885 下发的 `display.span` 账本、以及"播到才追加"的老路，
 * 两条都要先有语音会话。握手失败 / 开会话抛错 / 静音 / 按键说话时没有会话，整轮既没声也没字。
 * 这里按轮攒编排器给的原文（`onTokenLiteral` 的 literal，标记已削干净），会话不可用时
 * 一次性把整轮原文交出去，之后到来的 literal 直接增量交出去。
 *
 * 归属按 `turnId` 判：旧一轮晚到的 `onError` 带着旧 turnId，`begin()` 已经换了新轮，
 * 所以不会误伤新一轮；用户主动停止后 `close()` 掉，后面的 literal 也不许借兜底复活。
 */

export interface CaptionFallbackEmission {
  /** 字幕窗口的完整文本（replace 语义）。 */
  caption: string
  /** 展示通道要 append 的增量；可能为空串，调用方自己跳过。 */
  present: string
}

export interface CaptionFallback {
  /** 新一轮开始：攒的原文、兜底状态全部清零。 */
  begin: (turnId: string) => void
  /** 收一段 literal（不管兜底有没有开都要攒）；兜底已开才返回要出的字。 */
  push: (literal: string) => CaptionFallbackEmission | null
  /**
   * 这一轮没有可用语音会话。`presented` 是账本已经推进展示通道的前缀，
   * 展示通道只补它之后的部分；字幕永远用整轮原文 replace。
   * 不是当前轮 / 已关 / 已开 / 还没有原文 ⇒ 返回 null（没有原文时只置位，等 literal 来）。
   */
  fail: (turnId: string, presented?: string) => CaptionFallbackEmission | null
  /** 用户主动停止：这一轮不再出字，直到下一次 `begin()`。 */
  close: () => void
  readonly active: boolean
}

export function createCaptionFallback(): CaptionFallback {
  let currentTurnId: string | undefined
  let raw = ''
  let active = false
  let closed = false

  return {
    begin(turnId) {
      currentTurnId = turnId
      raw = ''
      active = false
      closed = false
    },
    push(literal) {
      if (closed)
        return null
      raw += literal
      if (!active || !literal)
        return null
      return { caption: raw.trim(), present: literal }
    },
    fail(turnId, presented = '') {
      if (closed || active || turnId !== currentTurnId)
        return null
      active = true
      if (!raw.trim())
        return null
      // 账本的文本理应是原文前缀；对不上（服务端改写过）就宁可整段补一遍，也不丢字。
      const present = presented && raw.startsWith(presented) ? raw.slice(presented.length) : raw
      return { caption: raw.trim(), present }
    },
    close() {
      closed = true
      active = false
      raw = ''
    },
    get active() {
      return active
    },
  }
}
