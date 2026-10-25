import en from '@proj-airi/i18n/locales/en'

import { useLive2dParams } from '@proj-airi/stage-ui-live2d/stores'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import { useSpeakingStore } from '../stores/audio'
import { useChatStore } from '../stores/chat'
import { useChatSessionStore } from '../stores/chat/session-store'
import { useSettingsStageModel } from '../stores/settings/stage-model'
import { useSpeechOutputControlStore } from '../stores/speech-output-control'
import { startAlbionPetInteractions } from './albion-pet'

let pinia: ReturnType<typeof createPinia>
let running: ReturnType<typeof startAlbionPetInteractions> | undefined
let app: ReturnType<typeof createApp>
const emotion = vi.fn()

beforeEach(async () => {
  localStorage.clear()
  localStorage.setItem('settings/albion/touch-voice', 'false')
  pinia = createPinia()
  setActivePinia(pinia)
  document.body.innerHTML = '<canvas></canvas>'
  ;(globalThis as any).__albionStageEmotion = emotion
  delete (globalThis as any).__albionModel
  emotion.mockClear()
  app = createApp({
    setup() {
      useChatSessionStore()
      useChatStore()
      return () => null
    },
  })
  const root = document.createElement('div')
  document.body.append(root)
  app.use(pinia).use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(root)
  await useChatSessionStore().initialize()
  vi.useFakeTimers()
})

afterEach(() => {
  running?.dispose()
  app?.unmount()
  disposePinia(pinia)
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
  delete (globalThis as any).__albionModel
  delete (globalThis as any).__albionStageEmotion
})

describe('s3 pet startup and ownership', () => {
  it.each(['speaking', 'busy', 'ptt', 'stop'])('s4 stops pack touch audio on %s and blocks overlap', async (mode) => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    ;(globalThis as any).__albionModel = {
      internalModel: { coreModel: { _parameterIds: [], _drawableIds: [] } },
      hitTest: () => ['touch_body'],
    }
    running = startAlbionPetInteractions()
    await running.ready
    const canvas = document.querySelector('canvas')!
    const tap = () => {
      canvas.dispatchEvent(new PointerEvent('pointerdown', { clientX: 100, clientY: 100 }))
      canvas.dispatchEvent(new PointerEvent('pointerup', { clientX: 100, clientY: 100 }))
    }
    tap()
    expect(play).toHaveBeenCalled()
    const plays = play.mock.calls.length
    const pauses = pause.mock.calls.length
    if (mode === 'speaking')
      useSpeakingStore().nowSpeaking = true
    else if (mode === 'busy')
      useSpeakingStore().assistantTurnBusy = true
    else if (mode === 'ptt')
      useSpeechOutputControlStore().pushToTalkActive = true
    else
      useSpeechOutputControlStore().requestStopSpeaking('push-to-talk')
    expect(pause).toHaveBeenCalledTimes(pauses + 1)
    await vi.advanceTimersByTimeAsync(4001)
    if (mode !== 'stop') {
      tap()
      expect(play).toHaveBeenCalledTimes(plays)
    }
  })

  it('s4 drops synthesized touch audio that arrives after an interruption', async () => {
    localStorage.setItem('settings/albion/touch-voice', 'true')
    let finish!: (response: Response) => void
    const response = new Promise<Response>((resolve) => {
      finish = resolve
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => (await response).clone())
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    running = startAlbionPetInteractions()
    ;(window as any).__albionPet.poke('head')
    useSpeechOutputControlStore().requestStopSpeaking('push-to-talk')
    finish(new Response(new Blob(['test-audio'])))
    await vi.advanceTimersByTimeAsync(1)
    expect(play).not.toHaveBeenCalled()
  })

  it('seeds history without performance or mutation, then performs a new turn once', async () => {
    const session = useChatSessionStore()
    const content = '<|ACT:{"emotion":"happy","motion":"touch_head"}|>旧消息'
    session.messages = [{ role: 'assistant', content, slices: [], tool_results: [] }]
    running = startAlbionPetInteractions()
    await vi.advanceTimersByTimeAsync(1200)
    expect(emotion).not.toHaveBeenCalled()
    expect(session.messages[0].content).toBe(content)
    session.messages.push({ role: 'assistant', content: '<|ACT:{"emotion":"sad"}|>新消息', slices: [], tool_results: [] })
    await vi.advanceTimersByTimeAsync(1200)
    expect(emotion).toHaveBeenCalledExactlyOnceWith('sad', 1, {})
    await vi.advanceTimersByTimeAsync(1200)
    expect(emotion).toHaveBeenCalledTimes(1)
    expect(session.messages[1].content).toContain('<|ACT:')
  })

  it('reads the ACT kept in categorization.speech and forwards face/hold instead of guessing from text', async () => {
    const session = useChatSessionStore()
    running = startAlbionPetInteractions()
    await vi.advanceTimersByTimeAsync(1200)
    session.messages.push({
      role: 'assistant',
      content: '有点难过',
      categorization: { speech: '<|ACT:{"emotion":"happy","face":"gentle","motion":"nod","hold":"keep"}|>有点难过', reasoning: '' },
      slices: [],
      tool_results: [],
    })
    await vi.advanceTimersByTimeAsync(1200)
    expect(emotion).toHaveBeenCalledExactlyOnceWith('happy', 1, { face: 'gentle', motion: 'nod', hold: 'keep' })
  })

  it('performs a streamed ACT once, then falls back to keyword guessing only for a turn without ACT', async () => {
    const session = useChatSessionStore()
    const chat = useChatStore()
    running = startAlbionPetInteractions()
    await vi.advanceTimersByTimeAsync(1200)
    const special = '<|ACT:{"emotion":"neutral","face":"pout"}|>'
    await chat.emitTokenSpecialHooks(special, {} as any)
    expect(emotion).toHaveBeenCalledExactlyOnceWith('neutral', 1, { face: 'pout' })
    session.messages.push({ role: 'assistant', content: '哼', categorization: { speech: `${special}哼`, reasoning: '' }, slices: [], tool_results: [] })
    await vi.advanceTimersByTimeAsync(1200)
    expect(emotion).toHaveBeenCalledTimes(1)
    session.messages.push({ role: 'assistant', content: '今天好开心', slices: [], tool_results: [] })
    await vi.advanceTimersByTimeAsync(1200)
    expect(emotion).toHaveBeenLastCalledWith('happy', 1, {})
    expect(emotion).toHaveBeenCalledTimes(2)
  })

  it('does no Live2D loop or layer work on Spine, and restores the Live2D wrapper on dispose', async () => {
    const update = vi.fn()
    const startMotion = vi.fn()
    const manager = { update, startMotion, stopAllMotions: vi.fn(), motionGroups: {}, state: {} }
    ;(globalThis as any).__albionModel = { internalModel: { motionManager: manager, coreModel: {} } }
    const stage = useSettingsStageModel()
    stage.stageModelRenderer = 'spine38'
    running = startAlbionPetInteractions()
    await running.ready
    await vi.advanceTimersByTimeAsync(2000)
    expect(startMotion).not.toHaveBeenCalled()
    expect(manager.update).toBe(update)
    stage.stageModelRenderer = 'live2d'
    await vi.advanceTimersByTimeAsync(2000)
    expect(startMotion).toHaveBeenCalled()
    expect(manager.update).not.toBe(update)
    running.dispose()
    expect(manager.update).toBe(update)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps the Live2D motion chain active and cancels pose and follow-up timers on dispose', async () => {
    const stage = useSettingsStageModel()
    stage.stageModelRenderer = 'live2d'
    const params = useLive2dParams()
    running = startAlbionPetInteractions()
    await Promise.resolve()
    expect((window as any).__albionPlay('Action1', 0)).toBe(true)
    await vi.advanceTimersByTimeAsync(2467)
    expect(params.currentMotion).toEqual({ group: 'Action1', index: 1 })
    ;(window as any).__albionPet.emotion('happy')
    running.dispose()
    expect(vi.getTimerCount()).toBe(0)
    const motion = params.currentMotion
    window.dispatchEvent(new Event('albion:motion-finish'))
    await vi.advanceTimersByTimeAsync(6000)
    expect(params.currentMotion).toBe(motion)
  })

  it('does not send a late doorbell response after disposal', async () => {
    let resolveBell!: (value: { due: boolean, marker: string }) => void
    const bell = new Promise<{ due: boolean, marker: string }>((resolve) => {
      resolveBell = resolve
    })
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: () => bell } as Response)
    const send = vi.spyOn(useChatStore(), 'send')
    running = startAlbionPetInteractions()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    const options = fetcher.mock.calls[0][1]!
    expect(options.signal?.aborted).toBe(false)
    running.dispose()
    expect(options.signal?.aborted).toBe(true)
    resolveBell({ due: true, marker: 'test-wake' })
    await vi.advanceTimersByTimeAsync(20_000)
    expect(send).not.toHaveBeenCalled()
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('replaces a previous start and cancels the pending model wait', async () => {
    const first = startAlbionPetInteractions()
    const count = vi.getTimerCount()
    running = startAlbionPetInteractions()
    first.dispose()
    expect(vi.getTimerCount()).toBe(count)
    running.dispose()
    await running.ready
    expect(vi.getTimerCount()).toBe(0)
    expect((window as any).__albionPet).toBeUndefined()
  })
})
