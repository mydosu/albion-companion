import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { startAlbionInteractions } from './albion-interactions'

const hooks = { onTap: vi.fn(), playWav: vi.fn() }
let running: ReturnType<typeof startAlbionInteractions> | undefined
let pinia: ReturnType<typeof createPinia>

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  localStorage.clear()
  document.body.innerHTML = '<canvas width="964" height="804"></canvas>'
  ;(globalThis as any).__albionModel = {
    internalModel: { coreModel: { _parameterIds: [], _drawableIds: [] } },
    hitTest: () => [],
  }
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame'] })
  vi.stubEnv('DEV', false)
})

afterEach(() => {
  running?.dispose()
  disposePinia(pinia)
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
  document.body.innerHTML = ''
  delete (globalThis as any).__albionModel
})

describe('s3 interaction ownership and debug keys', () => {
  it('does not register production debug keys by default', async () => {
    running = startAlbionInteractions(hooks)
    await running.ready
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'h' }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'o' }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', altKey: true }))
    expect(localStorage.getItem('settings/albion/calibration')).toBeNull()
    expect(document.body.children.length).toBe(4)
  })

  it.each(['override', 'dev'])('enables debug keys with %s and retains Escape', async (mode) => {
    if (mode === 'override')
      localStorage.setItem('settings/albion/debug-keys', '1')
    else
      vi.stubEnv('DEV', true)
    running = startAlbionInteractions(hooks)
    await running.ready
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'h' }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'o' }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', altKey: true }))
    expect(localStorage.getItem('settings/albion/calibration')).not.toBeNull()
    expect(document.body.children.length).toBe(6)
    const api = (globalThis as any).__albionInteractions
    ;(globalThis as any).__albionLockedParams.test = 1
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(api.locked()).toEqual({})
    running.dispose()
    expect(vi.getTimerCount()).toBe(0)
    expect(document.body.children.length).toBe(1)
    localStorage.removeItem('settings/albion/calibration')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', altKey: true }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'o' }))
    expect(localStorage.getItem('settings/albion/calibration')).toBeNull()
    expect(document.body.children.length).toBe(1)
  })

  it('replaces a previous start without duplicate listeners, timers or nodes', async () => {
    const first = startAlbionInteractions(hooks)
    await first.ready
    const count = vi.getTimerCount()
    running = startAlbionInteractions(hooks)
    await running.ready
    first.dispose()
    expect(vi.getTimerCount()).toBe(count)
    expect(document.body.children.length).toBe(4)
    const canvas = document.querySelector('canvas')!
    canvas.dispatchEvent(new PointerEvent('pointerdown', { clientX: 900, clientY: 700 }))
    canvas.dispatchEvent(new PointerEvent('pointerup', { clientX: 900, clientY: 700 }))
    expect((globalThis as any).__albionInteractions.trace()).toHaveLength(2)
    running.dispose()
    expect(vi.getTimerCount()).toBe(0)
    expect((globalThis as any).__albionInteractions).toBeUndefined()
  })

  it.each(['canvas', 'model'])('can dispose during the asynchronous wait for %s', async (missing) => {
    if (missing === 'canvas')
      document.body.innerHTML = ''
    else
      delete (globalThis as any).__albionModel
    running = startAlbionInteractions(hooks)
    await Promise.resolve()
    running.dispose()
    await running.ready
    expect(vi.getTimerCount()).toBe(0)
    expect((globalThis as any).__albionInteractions).toBeUndefined()
  })
})
