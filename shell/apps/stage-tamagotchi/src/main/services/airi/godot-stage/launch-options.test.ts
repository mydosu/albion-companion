import { describe, expect, it } from 'vitest'

import { resolveGodotStageLaunchOptions } from './launch-options'

describe('godot stage launch options', () => {
  it('keeps the default windowed launch', () => {
    expect(resolveGodotStageLaunchOptions({}).engineArgs).toEqual([])
    expect(resolveGodotStageLaunchOptions({}).headless).toBe(false)
    expect(resolveGodotStageLaunchOptions({ AIRI_GODOT_STAGE_HEADLESS: '0' }).headless).toBe(false)
  })

  it.each(['1', 'on', 'true', 'yes', ' TRUE '])('enables headless with %s', (flag) => {
    expect(resolveGodotStageLaunchOptions({ AIRI_GODOT_STAGE_HEADLESS: flag }).engineArgs).toEqual(['--headless'])
  })

  it('combines headless and remote debugging as engine flags', () => {
    const options = resolveGodotStageLaunchOptions({ AIRI_GODOT_STAGE_HEADLESS: '1', GODOT_STAGE_REMOTE_DEBUG: '1' })
    expect(options.engineArgs).toEqual(['--headless', '--remote-debug', 'tcp://127.0.0.1:6007'])
    expect(options.headless).toBe(true)
    expect(resolveGodotStageLaunchOptions({ GODOT_STAGE_REMOTE_DEBUG: '1', GODOT_STAGE_REMOTE_DEBUG_URI: 'tcp://127.0.0.1:6010' }).remoteDebugUri).toBe('tcp://127.0.0.1:6010')
  })
})
