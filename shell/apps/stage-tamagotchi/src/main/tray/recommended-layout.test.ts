import type { Rectangle } from 'electron'

import type { StageLayoutSnapshot } from '../../shared/eventa'

import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LIVE2D_ALBION_MODEL_ID, recommendedModelPairs, setupRecommendedLayouts, SPINE_ALBION_MODEL_ID } from './recommended-layout'

function snapshotOf(modelId: string | null, overrides: Partial<StageLayoutSnapshot> = {}): StageLayoutSnapshot {
  return {
    modelId,
    live2d: { scale: '1.4', position: '{"x":3,"y":40}' },
    spine: { scale: '0.8', x: '700.5', y: '1600' },
    ...overrides,
  }
}

describe('setupRecommendedLayouts', () => {
  let dir: string
  let bounds: Rectangle

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(100_000)
    dir = mkdtempSync(join(tmpdir(), 'albion-recommended-layout-'))
    bounds = { x: 10, y: 20, width: 900, height: 700 }
  })

  afterEach(() => {
    vi.useRealTimers()
    rmSync(dir, { recursive: true, force: true })
  })

  function setup(autosaveAfter = 0) {
    return setupRecommendedLayouts({ userDataDir: dir, getBounds: () => bounds, autosaveAfter })
  }

  const live2dFile = () => join(dir, 'recommended-view.json')
  const spineFile = () => join(dir, 'recommended-view-spine.json')
  const readJson = (file: string) => JSON.parse(readFileSync(file, 'utf8'))

  it('returns the built-in defaults until a layout is saved', () => {
    const layouts = setup()
    expect(layouts.read('live2d')).toEqual({ width: 964, height: 799, x: 1, y: 6, scale: '1.3', position: '{"x":-1,"y":46}' })
    expect(layouts.read('spine-b').scale).toBe('0.7026')
  })

  it('debounces autosave and writes once after changes stop', async () => {
    const layouts = setup()
    layouts.updateSnapshot(snapshotOf(LIVE2D_ALBION_MODEL_ID))

    for (let i = 0; i < 5; i++) {
      bounds = { ...bounds, x: bounds.x + 10 }
      layouts.scheduleAutosave()
      await vi.advanceTimersByTimeAsync(500)
    }
    expect(existsSync(live2dFile())).toBe(false)

    await vi.advanceTimersByTimeAsync(1500)
    expect(readJson(live2dFile())).toEqual({ width: 900, height: 700, x: 60, y: 20, scale: '1.4', position: '{"x":3,"y":40}' })
  })

  it('postpones autosave requests made during the startup guard', async () => {
    const layouts = setup(Date.now() + 15_000)
    layouts.updateSnapshot(snapshotOf(LIVE2D_ALBION_MODEL_ID))
    layouts.scheduleAutosave()

    await vi.advanceTimersByTimeAsync(14_000)
    expect(existsSync(live2dFile())).toBe(false)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(existsSync(live2dFile())).toBe(true)
  })

  it('saves the Spine layout from the raw x and y values', async () => {
    const layouts = setup()
    layouts.updateSnapshot(snapshotOf(SPINE_ALBION_MODEL_ID))
    layouts.scheduleAutosave()
    await vi.advanceTimersByTimeAsync(1500)

    expect(existsSync(live2dFile())).toBe(false)
    expect(readJson(spineFile())).toMatchObject({ scale: '0.8', position: '{"x":700.5,"y":1600}' })
    expect(recommendedModelPairs('spine', '0.8', '{"x":700.5,"y":1600}')).toEqual([
      ['settings/spine38/scale', '0.8'],
      ['settings/spine38/x', '700.5'],
      ['settings/spine38/y', '1600'],
    ])
  })

  it('does not save layouts for models other than the two Albion models', async () => {
    const layouts = setup()
    layouts.updateSnapshot(snapshotOf('preset-live2d-other'))
    layouts.scheduleAutosave()
    await vi.advanceTimersByTimeAsync(5000)

    expect(existsSync(live2dFile())).toBe(false)
    expect(existsSync(spineFile())).toBe(false)
  })

  it('keeps the saved size for sub-pixel drift of 3px or less, and saves a real resize', async () => {
    writeFileSync(live2dFile(), JSON.stringify({ width: 974, height: 1539, x: 10, y: 20, scale: '1.4', position: '{"x":3,"y":40}' }))
    const layouts = setup()
    layouts.updateSnapshot(snapshotOf(LIVE2D_ALBION_MODEL_ID))

    bounds = { x: 10, y: 20, width: 975, height: 1541 }
    layouts.scheduleAutosave()
    await vi.advanceTimersByTimeAsync(1500)
    expect(readJson(live2dFile())).toMatchObject({ width: 974, height: 1539 })

    bounds = { x: 10, y: 20, width: 978, height: 1539 }
    layouts.scheduleAutosave()
    await vi.advanceTimersByTimeAsync(1500)
    expect(readJson(live2dFile())).toMatchObject({ width: 978, height: 1539 })
  })

  it('skips the write when an autosave finds the same layout', async () => {
    const layouts = setup()
    layouts.updateSnapshot(snapshotOf(LIVE2D_ALBION_MODEL_ID))
    layouts.scheduleAutosave()
    await vi.advanceTimersByTimeAsync(1500)
    // Mark the file, so a second write would be visible.
    const marked = { ...readJson(live2dFile()), marker: true }
    writeFileSync(live2dFile(), JSON.stringify(marked))

    layouts.scheduleAutosave()
    await vi.advanceTimersByTimeAsync(1500)
    expect(readJson(live2dFile())).toEqual(marked)
  })

  it('reports the previous model id so the tray can detect a model change', () => {
    const layouts = setup()
    expect(layouts.updateSnapshot(snapshotOf(LIVE2D_ALBION_MODEL_ID))).toBeUndefined()
    expect(layouts.updateSnapshot(snapshotOf(SPINE_ALBION_MODEL_ID))).toBe(LIVE2D_ALBION_MODEL_ID)
    expect(layouts.currentModelId()).toBe(SPINE_ALBION_MODEL_ID)
  })

  it('saves on demand even when nothing changed, and returns the saved view', () => {
    const layouts = setup()
    layouts.updateSnapshot(snapshotOf(LIVE2D_ALBION_MODEL_ID))
    expect(layouts.save('live2d')).toMatchObject({ x: 10, y: 20 })
    expect(layouts.save('live2d')).toMatchObject({ x: 10, y: 20 })
  })
})
