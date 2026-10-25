import type { CaptionChannelEvent } from '@proj-airi/stage-shared'

import { readonly, shallowRef } from 'vue'

export interface CaptionItem {
  /** Stable render key and timer owner for one broadcast caption event. */
  id: number
  /** Caption source, used for styling and explicit type-level clears. */
  type: CaptionChannelEvent['type']
  /** Text payload rendered by the overlay. */
  text: string
  /** 已经到期、正在淡出，还没从列表里移除；新文字进来会复位成 false。 */
  fading: boolean
}

export interface UseCaptionItemsOptions {
  /**
   * How long one caption should stay visible before it starts leaving.
   *
   * 可以给一个数，也可以按类型分别给（用户那行要比阿尔比恩那行早退场）。
   *
   * @default 5000
   */
  ttlMs?: number | Partial<Record<CaptionChannelEvent['type'], number>>
  /**
   * 到期后先淡出多久再从列表里移除，给 CSS 的 opacity 过渡留时间。
   *
   * @default 0（到期立即移除）
   */
  fadeMs?: number
}

const defaultCaptionItemsOptions = {
  ttlMs: 5_000,
  fadeMs: 0,
} satisfies Required<UseCaptionItemsOptions>

/**
 * Manages caption overlay items with per-event expiry.
 *
 * Use when:
 * - Broadcast caption updates should age out independently.
 * - Empty caption events should clear only the matching caption source.
 *
 * Expects:
 * - Callers pass plain caption broadcast events.
 * - Callers call `dispose()` when the owner outlives Vue component cleanup.
 *
 * Returns:
 * - Readonly caption items plus actions for adding events and clearing timers.
 */
export function useCaptionItems(options: UseCaptionItemsOptions = {}) {
  const { ttlMs, fadeMs } = { ...defaultCaptionItemsOptions, ...options }
  const items = shallowRef<CaptionItem[]>([])
  const expiryTimers = new Map<CaptionItem['id'], ReturnType<typeof setTimeout>>()
  const fadeTimers = new Map<CaptionItem['id'], ReturnType<typeof setTimeout>>()
  let nextId = 1

  function ttlFor(type: CaptionItem['type']) {
    return typeof ttlMs === 'number' ? ttlMs : (ttlMs[type] ?? defaultCaptionItemsOptions.ttlMs)
  }

  function clearTimer(id: CaptionItem['id']) {
    const timer = expiryTimers.get(id)
    if (timer) {
      clearTimeout(timer)
      expiryTimers.delete(id)
    }

    const fadeTimer = fadeTimers.get(id)
    if (fadeTimer) {
      clearTimeout(fadeTimer)
      fadeTimers.delete(id)
    }
  }

  function remove(id: CaptionItem['id']) {
    clearTimer(id)
    items.value = items.value.filter(item => item.id !== id)
  }

  /** 到期：有淡出时长就先标 fading（模板据此做 opacity 过渡），过渡结束再移除。 */
  function startFade(id: CaptionItem['id']) {
    expiryTimers.delete(id)
    if (fadeMs <= 0) {
      remove(id)
      return
    }

    items.value = items.value.map(item => item.id === id ? { ...item, fading: true } : item)
    fadeTimers.set(id, setTimeout(() => {
      fadeTimers.delete(id)
      remove(id)
    }, fadeMs))
  }

  function clearType(type: CaptionChannelEvent['type']) {
    const matchedItems = items.value.filter(item => item.type === type)
    for (const item of matchedItems) {
      clearTimer(item.id)
    }
    items.value = items.value.filter(item => item.type !== type)
  }

  function scheduleExpiry(item: CaptionItem) {
    // 每来一次新文字都重新计时：先取消旧的到期/淡出，再按这一类自己的 TTL 排下一次
    clearTimer(item.id)
    expiryTimers.set(item.id, setTimeout(() => {
      startFade(item.id)
    }, ttlFor(item.type)))
  }

  function replace(event: CaptionChannelEvent) {
    const matchedItems = items.value.filter(item => item.type === event.type)
    const currentItem = matchedItems.at(-1)
    if (!currentItem) {
      const item: CaptionItem = {
        id: nextId++,
        type: event.type,
        text: event.text,
        fading: false,
      }
      items.value = [...items.value, item]
      scheduleExpiry(item)
      return
    }

    for (const item of matchedItems)
      clearTimer(item.id)

    const replacement = { ...currentItem, text: event.text, fading: false }
    items.value = items.value
      .filter(item => item.type !== event.type || item.id === currentItem.id)
      .map(item => item.id === currentItem.id ? replacement : item)
    scheduleExpiry(replacement)
  }

  function add(event: CaptionChannelEvent) {
    if (!event.text.trim()) {
      clearType(event.type)
      return
    }

    if (event.operation === 'replace') {
      replace(event)
      return
    }

    const item: CaptionItem = {
      id: nextId++,
      type: event.type,
      text: event.text,
      fading: false,
    }
    items.value = [...items.value, item]
    scheduleExpiry(item)
  }

  function dispose() {
    for (const timer of expiryTimers.values()) {
      clearTimeout(timer)
    }
    expiryTimers.clear()
    for (const timer of fadeTimers.values()) {
      clearTimeout(timer)
    }
    fadeTimers.clear()
    items.value = []
  }

  return {
    items: readonly(items),
    add,
    clearType,
    dispose,
  }
}
