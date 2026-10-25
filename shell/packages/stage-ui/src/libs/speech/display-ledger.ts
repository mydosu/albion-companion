/**
 * v2 字幕账本（P5 / 方案 §4.2）。
 *
 * 为什么不是"播到哪句就显示哪句"：纯心声、纯符号的段没有音频，靠播放事件驱动会整段漏掉，
 * 屏幕上就少字了。这里以服务端 `display.span` 为准 —— 可说段在首帧时给、无音频段立刻给、
 * 失败段由服务端补一次资格，于是**原文守恒**。
 *
 * 两条硬口径：
 * 1. **只显示连续前缀**（方案 §4.2）。span 乱序到达时（后段先到、前段还在合成），先把后段
 *    存着，等缺口补上再一起长出来 —— 否则屏幕会先蹦出后面的字，前段补上来时顺序就乱了。
 * 2. `sourceRange` 是 **Unicode code point 下标**，不是 JS `string.length`（UTF-16 code unit）。
 *    同一份原文 `'你😊呀'` 在 Python 里是 3 个字符、在 JS 里 `length` 是 4 —— 拿 JS 长度去切
 *    或去比对，emoji 之后整段字幕就错位了。所以本模块**不按 range 切原文**，只按 range 排序、
 *    用服务端给的 `text` 原文累积；需要按 range 比对时一律走 `[...str]`（code point 数组）。
 */

/** 与 `StreamingDisplaySpan` 同形；本模块刻意不 import 它，便于单独测。 */
export interface DisplayLedgerSpan {
  unitId: string
  sourceRange: [number, number]
  text: string
  eligible: 'audio' | 'immediate'
}

export interface DisplayLedger {
  /** 落一个 span；返回当前可显示的连续前缀文本。 */
  add: (span: DisplayLedgerSpan) => string
  /** 当前可显示的**连续前缀**文本。 */
  text: () => string
  /** 自上次 `takeNew()` 以来新长出来的那部分（给 `postPresent` 这类增量通道用）。 */
  takeNew: () => string
  /** 落过 span 没有？有 ⇒ 旧的字幕路径（播到才追加）就该停手，否则同一句记两遍。 */
  readonly driven: boolean
  /** 已落账的 span 数（去重后，含还没接上前缀的）。 */
  readonly size: number
  clear: () => void
}

export function createDisplayLedger(): DisplayLedger {
  const spans: DisplayLedgerSpan[] = []
  const seen = new Set<string>()
  let driven = false
  let shown = ''

  /** 从原文第 0 个字符开始一段接一段取，遇到缺口就停（= 连续前缀）。 */
  function text(): string {
    // 锚在 0：源文本的单元从 0 铺满整段，所以"缺口"就是还没拿到资格的段 ——
    // 拿第一个到手的 span 当锚会让后段先蹦出来（方案 §4.2 明确不允许）。
    let cursor = 0
    let out = ''
    for (const span of spans) {
      if (span.sourceRange[0] !== cursor)
        break
      out += span.text
      cursor = span.sourceRange[1]
    }
    return out
  }

  return {
    add(span) {
      // 坏 span 由管道层（`readDisplaySpan`）拦掉；这里只兜 `unit_id` 退化 + 重复投递。
      if (!span.unitId || seen.has(span.unitId))
        return text()
      seen.add(span.unitId)
      driven = true
      spans.push(span)
      spans.sort((a, b) => a.sourceRange[0] - b.sourceRange[0])
      return text()
    },
    text,
    takeNew() {
      const now = text()
      // 前缀只增不减 ⇒ 直接按 code unit 切尾巴：同一串字符，前缀边界在两种计数下一致。
      const fresh = now.startsWith(shown) ? now.slice(shown.length) : now
      shown = now
      return fresh
    },
    get driven() {
      return driven
    },
    get size() {
      return spans.length
    },
    clear() {
      spans.length = 0
      seen.clear()
      shown = ''
      driven = false
    },
  }
}
