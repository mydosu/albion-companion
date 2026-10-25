// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick } from 'vue'

import { electronStageLayoutChanged, electronStageLayoutRequest } from '../../shared/eventa'
import { initializeStageLayoutBridge as startBridge } from './stage-layout'

const eventa = vi.hoisted(() => ({
  emit: vi.fn(),
  handlers: new Map<object, () => void>(),
}))

vi.mock('@proj-airi/electron-vueuse', () => ({
  getElectronEventaContext: () => ({
    emit: eventa.emit,
    on: (event: object, handler: () => void) => {
      eventa.handlers.set(event, handler)
    },
  }),
}))

/** Writes like the tray does: set the item, then dispatch a same-document storage event. */
function writeStorage(key: string, value: string) {
  localStorage.setItem(key, value)
  window.dispatchEvent(new StorageEvent('storage', { key, newValue: value, storageArea: localStorage }))
}

function lastSnapshot() {
  const call = eventa.emit.mock.calls.at(-1)
  expect(call?.[0]).toBe(electronStageLayoutChanged)
  return call?.[1]
}

describe('initializeStageLayoutBridge', () => {
  // Each test starts its own bridge; stop its storage listeners so they do not leak into the next test.
  let scope = effectScope()
  const initializeStageLayoutBridge = () => scope.run(startBridge)

  beforeEach(() => {
    scope = effectScope()
    vi.useFakeTimers()
    localStorage.clear()
    eventa.emit.mockClear()
    eventa.handlers.clear()
  })

  afterEach(() => {
    scope.stop()
    vi.useRealTimers()
  })

  it('sends the raw stored values once at startup and does not write missing keys', () => {
    localStorage.setItem('settings/stage/model', 'preset-spine38-albion')
    localStorage.setItem('settings/spine38/x', '788.5999755859375')

    initializeStageLayoutBridge()

    expect(eventa.emit).toHaveBeenCalledTimes(1)
    expect(lastSnapshot()).toEqual({
      modelId: 'preset-spine38-albion',
      live2d: { scale: null, position: null },
      spine: { scale: null, x: '788.5999755859375', y: null },
    })
    expect(localStorage.getItem('settings/live2d/scale')).toBeNull()
    expect(localStorage.length).toBe(2)
  })

  it('sends a model change at once', async () => {
    initializeStageLayoutBridge()
    eventa.emit.mockClear()

    writeStorage('settings/stage/model', 'preset-live2d-albion')
    await nextTick()

    expect(eventa.emit).toHaveBeenCalledTimes(1)
    expect(lastSnapshot().modelId).toBe('preset-live2d-albion')
  })

  it('debounces scale and position changes into one snapshot', async () => {
    initializeStageLayoutBridge()
    eventa.emit.mockClear()

    for (let i = 1; i <= 5; i++) {
      writeStorage('settings/spine38/scale', `0.${i}`)
      await vi.advanceTimersByTimeAsync(50)
    }
    expect(eventa.emit).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(300)
    expect(eventa.emit).toHaveBeenCalledTimes(1)
    expect(lastSnapshot().spine.scale).toBe('0.5')
  })

  it('sends one snapshot when main asks for it', () => {
    initializeStageLayoutBridge()
    eventa.emit.mockClear()

    eventa.handlers.get(electronStageLayoutRequest)?.()

    expect(eventa.emit).toHaveBeenCalledTimes(1)
    expect(lastSnapshot().modelId).toBeNull()
  })
})
