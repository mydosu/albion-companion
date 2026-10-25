import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import { ASSISTANT_TURN_WAIT_TIMEOUT_MS, waitForAssistantTurnToSettle } from './wait-for-assistant-turn'

describe('waitForAssistantTurnToSettle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('resolves immediately when she is not busy', async () => {
    const busy = ref(false)
    const onSettled = vi.fn()

    await waitForAssistantTurnToSettle(busy).then(onSettled)

    expect(onSettled).toHaveBeenCalledOnce()
  })

  it('resolves as soon as the busy flag drops', async () => {
    const busy = ref(true)
    const onSettled = vi.fn()

    const pending = waitForAssistantTurnToSettle(busy).then(onSettled)
    await nextTick()
    expect(onSettled).not.toHaveBeenCalled()

    busy.value = false
    await pending

    expect(onSettled).toHaveBeenCalledOnce()
    expect(console.warn).not.toHaveBeenCalled()
  })

  it('lets the new message through after the timeout when she stays busy', async () => {
    const busy = ref(true)
    const onSettled = vi.fn()

    const pending = waitForAssistantTurnToSettle(busy).then(onSettled)
    await vi.advanceTimersByTimeAsync(ASSISTANT_TURN_WAIT_TIMEOUT_MS - 1)
    expect(onSettled).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    await pending

    expect(onSettled).toHaveBeenCalledOnce()
    expect(console.warn).toHaveBeenCalledOnce()
  })

  it('does not warn or leave a timer behind when the flag drops in time', async () => {
    const busy = ref(true)

    const pending = waitForAssistantTurnToSettle(busy)
    busy.value = false
    await pending

    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(ASSISTANT_TURN_WAIT_TIMEOUT_MS)
    expect(console.warn).not.toHaveBeenCalled()
  })

  it('uses the 30 second cap by default', () => {
    expect(ASSISTANT_TURN_WAIT_TIMEOUT_MS).toBe(30_000)
  })
})
