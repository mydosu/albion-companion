// 她的"点单"双方括号标记（仿写的 `[[read notebook]]`、旧的 `[[LOOK:his]]`）不进聊天、字幕，也不被念出来。
// 标记内最多 80 字，且不跨换行；超过的不算标记，原样保留。
//
// 只削一层：文本进入系统的地方（聊天编排器的 literal、spark 反应的 literal）各过一次。
// 聊天气泡、TTS、字幕（切句器 onStart 和 v2 `display.span` 账本）都从这份已削过的文本派生，下游不再各削一遍。
const PET_TAG_RE = /\[\[[^\]\n]{0,80}\]\]/g
// 尾部可能还没写完的标记：单个 `[`、`[[re`、差一个括号的 `[[re]`。流式过滤要先攒着等下一片来拼。
const PET_TAG_OPEN_TAIL_RE = /(?:\[\[[^\]\n]{0,80}\]?|\[)$/

/**
 * 削掉文本里已经完整的标记，削到不再变化为止。
 *
 * 只削一遍不够：`[[y][[x]]]` 削掉中间的 `[[x]]` 后，两头又拼成 `[[y]]`。
 *
 * @example
 * stripPetTags('好[[read notebook]]的')
 * // => '好的'
 */
export function stripPetTags(text: string): string {
  let previous: string
  let current = text
  do {
    previous = current
    current = previous.replace(PET_TAG_RE, '')
  } while (current !== previous)
  return current
}

/**
 * 流式版：一片一片喂入，返回此刻可以安全交出去的文本。
 *
 * 保证：所有 `push` 的返回值加上 `flush` 的返回值拼起来，里面没有完整标记 ——
 * 每次交出去的文本自身已削干净，结尾也不会停在"可能是标记开头"的位置，所以下一片拼不出跨片的标记。
 *
 * - `push` 返回值里的完整标记已削掉；尾部半截标记留在内部，等下一片决定。
 * - `flush` 在流结束时调用，把没合上的半截交出 —— 它是她真说的字，不是标记，不吞字。
 * - `reset` 在新一轮开始时调用，丢掉上一轮的半截，避免带进下一轮。
 */
export function createPetTagStreamFilter() {
  let pending = ''

  return {
    push(literal: string): string {
      const text = pending + literal
      const openTail = text.match(PET_TAG_OPEN_TAIL_RE)?.[0] ?? ''
      const safe = stripPetTags(text.slice(0, text.length - openTail.length))
      // 削完以后结尾可能又露出半截（`[[y]` + `[[x]]` 削成 `[[y]`），这段也要压回去等下一片。
      const exposedTail = safe.match(PET_TAG_OPEN_TAIL_RE)?.[0] ?? ''
      pending = exposedTail + openTail
      return safe.slice(0, safe.length - exposedTail.length)
    },
    flush(): string {
      const rest = stripPetTags(pending)
      pending = ''
      return rest
    },
    reset() {
      pending = ''
    },
  }
}
