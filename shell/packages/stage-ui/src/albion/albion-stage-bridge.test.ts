import { afterEach, describe, expect, it, vi } from 'vitest'

import { ALBION_STREAM_TTS_BASE, ALBION_STREAM_TTS_MODEL, ALBION_STREAM_TTS_PROVIDER_ID, ALBION_TTS_MODEL } from './albion-defaults'
import { bindAlbionStageEmotion, buildAlbionStreamingSnapshot, isAlbionActRenderer, isAlbionStreamTtsProvider } from './albion-stage-bridge'

type EmotionEntry = (name: string, intensity: number, act?: object) => boolean

describe('albion stage bridge', () => {
  afterEach(() => {
    delete (globalThis as unknown as Record<string, unknown>).__albionStageEmotion
  })

  it('claims only the albion stream provider', () => {
    expect(isAlbionStreamTtsProvider(ALBION_STREAM_TTS_PROVIDER_ID)).toBe(true)
    expect(isAlbionStreamTtsProvider('official-speech')).toBe(false)
    expect(isAlbionStreamTtsProvider(undefined)).toBe(false)
  })

  it('keeps the host-owned fields and fills in the local stream service fields', () => {
    const onImmediateSpecial = vi.fn()
    const snapshot = buildAlbionStreamingSnapshot({ voiceType: 'custom_configured', ownerId: 'default', onImmediateSpecial })

    expect(snapshot).toMatchObject({
      model: ALBION_STREAM_TTS_MODEL,
      voice: ALBION_TTS_MODEL,
      voiceType: 'custom_configured',
      ownerId: 'default',
      bufferEntireSession: false,
      extraBody: { protocol_version: 2 },
      responseFormat: 'wav',
      serverUrl: ALBION_STREAM_TTS_BASE,
      token: 'albion-local',
    })
    expect(snapshot.onImmediateSpecial).toBe(onImmediateSpecial)
  })

  it('publishes the emotion entry on globalThis for albion-pet to call', () => {
    const play = vi.fn(() => true)
    bindAlbionStageEmotion(play)

    const published = (globalThis as unknown as Record<string, EmotionEntry>).__albionStageEmotion
    expect(published('happy', 0.5)).toBe(true)
    expect(play).toHaveBeenCalledWith('happy', 0.5)
    expect(published('neutral', 1, { face: 'gentle', motion: 'nod', hold: 'keep' })).toBe(true)
    expect(play).toHaveBeenLastCalledWith('neutral', 1, { face: 'gentle', motion: 'nod', hold: 'keep' })
  })

  it('keeps the upstream ACT handler away from renderers albion-pet already drives', () => {
    for (const renderer of ['live2d', 'spine', 'spine38', 'tachie'])
      expect(isAlbionActRenderer(renderer)).toBe(true)
    for (const renderer of ['vrm', 'mmd', 'godot', '', undefined])
      expect(isAlbionActRenderer(renderer)).toBe(false)
  })
})
