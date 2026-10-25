import { describe, expect, it, vi } from 'vitest'

import { useCaptionItems } from './useCaptionItems'

describe('useCaptionItems', () => {
  it('expires each caption event without cancelling earlier events of the same type', () => {
    vi.useFakeTimers()

    try {
      const captions = useCaptionItems({ ttlMs: 1000 })

      captions.add({ type: 'caption-speaker', text: 'first' })
      vi.advanceTimersByTime(500)
      captions.add({ type: 'caption-speaker', text: 'second' })

      expect(captions.items.value.map(item => item.text)).toEqual(['first', 'second'])

      vi.advanceTimersByTime(500)

      expect(captions.items.value.map(item => item.text)).toEqual(['second'])

      vi.advanceTimersByTime(500)

      expect(captions.items.value).toEqual([])
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('clears caption items of the matching type when an empty event arrives', () => {
    vi.useFakeTimers()

    try {
      const captions = useCaptionItems({ ttlMs: 1000 })

      captions.add({ type: 'caption-speaker', text: 'speaker' })
      captions.add({ type: 'caption-assistant', text: 'assistant' })
      captions.add({ type: 'caption-speaker', text: '' })

      expect(captions.items.value.map(item => item.text)).toEqual(['assistant'])

      vi.advanceTimersByTime(1000)

      expect(captions.items.value).toEqual([])
    }
    finally {
      vi.useRealTimers()
    }
  })

  // ROOT CAUSE:
  //
  // Streaming providers send a complete volatile sentence on each update.
  // The caption overlay appended every correction as a separate item.
  it('replaces volatile speaker captions without accumulating corrections', () => {
    vi.useFakeTimers()

    try {
      const captions = useCaptionItems({ ttlMs: 1000 })

      captions.add({ operation: 'replace', type: 'caption-speaker', text: '今天天气很号' })
      vi.advanceTimersByTime(500)
      captions.add({ operation: 'replace', type: 'caption-speaker', text: '今天天气很好' })

      expect(captions.items.value).toHaveLength(1)
      expect(captions.items.value[0]?.text).toBe('今天天气很好')

      vi.advanceTimersByTime(500)

      expect(captions.items.value).toHaveLength(1)

      vi.advanceTimersByTime(500)

      expect(captions.items.value).toEqual([])
    }
    finally {
      vi.useRealTimers()
    }
  })

  // ROOT CAUSE:
  //
  // 字幕窗原来给所有类型同一个 24 小时 TTL（指挥官 2026-09-17 定的「字幕不再自动消失」），
  // 用户自己那行于是永远留在屏幕上。现在按类型分开：用户那行 5 秒后淡出，阿尔比恩那行不动。
  it('fades the speaker caption out after its own ttl while the assistant one stays', () => {
    vi.useFakeTimers()

    try {
      const captions = useCaptionItems({
        ttlMs: { 'caption-speaker': 5000, 'caption-assistant': 24 * 60 * 60 * 1000 },
        fadeMs: 1000,
      })

      captions.add({ operation: 'replace', type: 'caption-speaker', text: '我说的话' })
      captions.add({ operation: 'replace', type: 'caption-assistant', text: '她说的话' })

      vi.advanceTimersByTime(4999)
      /** @example 4.999 秒时用户那行还在，也还没开始淡出。 */
      expect(captions.items.value.map(item => item.fading)).toEqual([false, false])

      vi.advanceTimersByTime(1)
      /** @example 到 5 秒整，用户那行进入淡出，但文字还在（给 CSS 过渡用）。 */
      expect(captions.items.value.map(item => item.text)).toEqual(['我说的话', '她说的话'])
      /** @example 淡出的是用户那行，阿尔比恩那行不受影响。 */
      expect(captions.items.value.map(item => item.fading)).toEqual([true, false])

      vi.advanceTimersByTime(999)
      /** @example 淡出 1 秒内文字仍然可见。 */
      expect(captions.items.value).toHaveLength(2)

      vi.advanceTimersByTime(1)
      /** @example 淡出结束才移除；阿尔比恩那行继续留着。 */
      expect(captions.items.value.map(item => item.text)).toEqual(['她说的话'])
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('restarts the speaker countdown when new text arrives mid-fade', () => {
    vi.useFakeTimers()

    try {
      const captions = useCaptionItems({ ttlMs: { 'caption-speaker': 5000 }, fadeMs: 1000 })

      captions.add({ operation: 'replace', type: 'caption-speaker', text: '第一段' })
      vi.advanceTimersByTime(4800)
      captions.add({ operation: 'replace', type: 'caption-speaker', text: '第一段第二段' })

      vi.advanceTimersByTime(200)
      /** @example 新文字把计时重新拉回 5 秒，这时候不该开始淡出。 */
      expect(captions.items.value[0]?.fading).toBe(false)
      expect(captions.items.value[0]?.text).toBe('第一段第二段')

      vi.advanceTimersByTime(4800)
      /** @example 从最后一次更新算起 5 秒，才开始淡出。 */
      expect(captions.items.value[0]?.fading).toBe(true)

      captions.add({ operation: 'replace', type: 'caption-speaker', text: '第一段第二段第三段' })
      vi.advanceTimersByTime(1000)
      /** @example 淡出途中来了新文字：复位，不删。 */
      expect(captions.items.value.map(item => [item.text, item.fading])).toEqual([['第一段第二段第三段', false]])
    }
    finally {
      vi.useRealTimers()
    }
  })
})
