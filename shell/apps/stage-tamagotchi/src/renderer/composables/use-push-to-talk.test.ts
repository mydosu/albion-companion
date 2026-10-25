import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { usePushToTalk } from './use-push-to-talk'

afterEach(() => vi.useRealTimers())

describe('s4 push to talk', () => {
  it.each([false, true])('preserves the saved mic switch (%s) through press and release', async (saved) => {
    vi.useFakeTimers()
    const savedEnabled = ref(saved)
    const interrupt = vi.fn()
    const ptt = usePushToTalk(savedEnabled, interrupt)
    ptt.press()
    ptt.press()
    expect(interrupt).toHaveBeenCalledTimes(1)
    expect(ptt.enabled.value).toBe(true)
    expect(savedEnabled.value).toBe(saved)
    ptt.release()
    await vi.advanceTimersByTimeAsync(299)
    expect(ptt.enabled.value).toBe(true)
    await vi.advanceTimersByTimeAsync(1)
    expect(ptt.active.value).toBe(false)
    expect(ptt.enabled.value).toBe(saved)
    expect(savedEnabled.value).toBe(saved)
  })

  it('cancels the release tail when pressed again', async () => {
    vi.useFakeTimers()
    const ptt = usePushToTalk(ref(false), vi.fn())
    ptt.press()
    ptt.release()
    await vi.advanceTimersByTimeAsync(200)
    ptt.press()
    await vi.advanceTimersByTimeAsync(300)
    expect(ptt.active.value).toBe(true)
    ptt.dispose()
  })

  it('clears runtime state and timers on disposal, including a held key', () => {
    vi.useFakeTimers()
    const saved = ref(false)
    const ptt = usePushToTalk(saved, vi.fn())
    ptt.press()
    ptt.release()
    ptt.dispose()
    expect(ptt.enabled.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    ptt.press()
    ptt.dispose()
    expect(ptt.held.value).toBe(false)
    expect(saved.value).toBe(false)
    expect(usePushToTalk(saved, vi.fn()).enabled.value).toBe(false)
  })
})
