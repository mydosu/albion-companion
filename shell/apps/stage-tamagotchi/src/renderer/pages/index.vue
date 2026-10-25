<script setup lang="ts">
import type { CaptionChannelEvent, HearingInputChannelEvent } from '@proj-airi/stage-shared'
import type { ModelSettingsRuntimeSnapshot } from '@proj-airi/stage-ui/components/scenarios/settings/model-settings/runtime'

import type { ModelSettingsRuntimeChannelEvent } from '../../shared/model-settings-runtime'

import { errorMessageFrom, tryCatch } from '@moeru/std'
import { electron } from '@proj-airi/electron-eventa'
import {
  getElectronEventaContext,
  useElectronEventaInvoke,
  useElectronMouseAroundWindowBorder,
  useElectronMouseInElement,
  useElectronMouseInWindow,
  useElectronRelativeMouse,
} from '@proj-airi/electron-vueuse'
import { hearingInputChannelName } from '@proj-airi/stage-shared'
import { parseAccelerator } from '@proj-airi/stage-shared/global-shortcut'
import { useModelStore, useThreeSceneIsTransparentAtPoint } from '@proj-airi/stage-ui-three'
import { HoloCoupon } from '@proj-airi/stage-ui/components'
import {
  createEmptyModelSettingsRuntimeSnapshot,
  resolveComponentStateToRuntimePhase,
} from '@proj-airi/stage-ui/components/scenarios/settings/model-settings/runtime'
import { WidgetStage } from '@proj-airi/stage-ui/components/scenes'
import { useVoiceInputSession } from '@proj-airi/stage-ui/composables'
import { useCanvasPixelIsTransparentAtPoint } from '@proj-airi/stage-ui/composables/canvas-alpha'
import { useSpeakingStore } from '@proj-airi/stage-ui/stores/audio'
import { useChatStore } from '@proj-airi/stage-ui/stores/chat'
import { useChatSessionStore } from '@proj-airi/stage-ui/stores/chat/session-store'
import { useHearingSpeechInputPipeline, useHearingStore } from '@proj-airi/stage-ui/stores/modules/hearing'
import { useOnboardingStore } from '@proj-airi/stage-ui/stores/onboarding'
import { useSettings, useSettingsAudioDevice } from '@proj-airi/stage-ui/stores/settings'
import { useSpeechOutputControlStore } from '@proj-airi/stage-ui/stores/speech-output-control'
import { refDebounced, useBroadcastChannel } from '@vueuse/core'
import { storeToRefs } from 'pinia'
import { computed, onMounted, onUnmounted, ref, shallowRef, toRef, watch } from 'vue'
import { toast } from 'vue-sonner'

import ControlsIslandRoot from '../components/stage-islands/controls-island/controls-island-root.vue'
import ControlsIsland from '../components/stage-islands/controls-island/index.vue'
import ResourceStatusIsland from '../components/stage-islands/resource-status-island/index.vue'

import { electronOpenOnboarding, electronShortcutRegister, electronShortcutTriggered, electronShortcutUnregister } from '../../shared/eventa'
import { modelSettingsRuntimeSnapshotChannelName } from '../../shared/model-settings-runtime'
import { usePushToTalk } from '../composables/use-push-to-talk'
import { createVoiceInputSendBuffer, joinTranscriptFragments } from '../composables/voice-input-send-buffer'
import { waitForAssistantTurnToSettle } from '../composables/wait-for-assistant-turn'
import { useControlsIslandStore } from '../stores/controls-island'
import { useStageWindowLifecycleStore } from '../stores/stage-window-lifecycle'
import { resolveFadeOnHoverInteraction } from '../utils/fade-on-hover'
import { shouldSampleStageTransparency } from '../utils/stage-three-transparency'
import { createVoiceInputInteractionLifecycle } from '../utils/voice-input-lifecycle'
import {
  assistantSpeechCooldownDeadline,
  DEFAULT_ASSISTANT_SPEECH_INPUT_COOLDOWN_MS,
  shouldSuppressVoiceInput,
} from '../utils/voice-input-suppression'

const controlsIslandRef = ref<InstanceType<typeof ControlsIsland>>()
const controlsIslandInteractionActive = shallowRef(false)
const controlsIslandElement = toRef(() => controlsIslandRef.value?.element)
const widgetStageRef = ref<InstanceType<typeof WidgetStage>>()
const stageCanvas = toRef(() => widgetStageRef.value?.canvasElement())
const componentStateStage = ref<'pending' | 'loading' | 'mounted'>('pending')
const stageMounted = computed(() => componentStateStage.value === 'mounted')
const isLoading = computed(() => !stageMounted.value)

const isIgnoringMouseEvents = ref(false)
const shouldFadeOnCursorWithin = ref(false)

const onboardingStore = useOnboardingStore()
const openOnboarding = useElectronEventaInvoke(electronOpenOnboarding)

const { isOutside: isOutsideWindow } = useElectronMouseInWindow()
const { isOutside } = useElectronMouseInElement(controlsIslandElement)
const isOutsideFor250Ms = refDebounced(isOutside, 250)
const { x: relativeMouseX, y: relativeMouseY } = useElectronRelativeMouse()
// NOTICE: In real-world use cases of Fade on Hover feature, the cursor may move around the edge of the
// model rapidly, causing flickering effects when checking pixel transparency strictly.
// Here we use render-target pixel sampling to keep detection aligned with the actual render output.
const isTransparentByPixels = useCanvasPixelIsTransparentAtPoint(
  stageCanvas,
  relativeMouseX,
  relativeMouseY,
  { regionRadius: 25 },
)
const isTransparentByThree = useThreeSceneIsTransparentAtPoint(
  widgetStageRef,
  relativeMouseX,
  relativeMouseY,
  { regionRadius: 25 },
)
const isTransparentByPixelsExact = useCanvasPixelIsTransparentAtPoint(
  stageCanvas,
  relativeMouseX,
  relativeMouseY,
)
const isTransparentByThreeExact = useThreeSceneIsTransparentAtPoint(
  widgetStageRef,
  relativeMouseX,
  relativeMouseY,
)

const settingsStore = useSettings()
const { stageModelRenderer, stageModelSelectedUrl } = storeToRefs(settingsStore)
const modelStore = useModelStore()
const { sceneMutationLocked, scenePhase } = storeToRefs(modelStore)
const { stagePaused } = storeToRefs(useStageWindowLifecycleStore())
const { fadeOnHoverEnabled } = storeToRefs(useControlsIslandStore())
const modelSettingsRuntimeOwnerInstanceId = `tamagotchi-main-stage:${Math.random().toString(36).slice(2, 10)}`
const { data: modelSettingsRuntimeChannelEvent, post: postModelSettingsRuntimeChannelEvent } = useBroadcastChannel<ModelSettingsRuntimeChannelEvent, ModelSettingsRuntimeChannelEvent>({ name: modelSettingsRuntimeSnapshotChannelName })
const shouldUseThreeTransparencyHitTest = computed(() => shouldSampleStageTransparency({
  componentState: componentStateStage.value,
  fadeOnHoverEnabled: fadeOnHoverEnabled.value,
  stageModelRenderer: stageModelRenderer.value,
  stagePaused: stagePaused.value,
}))
const isTransparent = computed(() => {
  if (stagePaused.value || componentStateStage.value !== 'mounted' || !fadeOnHoverEnabled.value)
    return true

  if (stageModelRenderer.value === 'vrm')
    return shouldUseThreeTransparencyHitTest.value ? isTransparentByThree.value : true

  if (stageModelRenderer.value === 'live2d' || stageModelRenderer.value === 'tachie')
    return isTransparentByPixels.value

  return true
})
const isTransparentForMouseEvents = computed(() => {
  if (stagePaused.value || componentStateStage.value !== 'mounted' || !fadeOnHoverEnabled.value)
    return true

  if (stageModelRenderer.value === 'vrm')
    return shouldUseThreeTransparencyHitTest.value ? isTransparentByThreeExact.value : true

  if (stageModelRenderer.value === 'live2d' || stageModelRenderer.value === 'tachie')
    return isTransparentByPixelsExact.value

  return true
})

const { isNearAnyBorder: isAroundWindowBorder } = useElectronMouseAroundWindowBorder({ threshold: 10 })
const isAroundWindowBorderFor250Ms = refDebounced(isAroundWindowBorder, 250)

const setIgnoreMouseEvents = useElectronEventaInvoke(electron.window.setIgnoreMouseEvents)

const hearingDialogOpen = computed(() => controlsIslandRef.value?.hearingDialogOpen ?? false)

const modelSettingsRuntimeSnapshot = computed<ModelSettingsRuntimeSnapshot>(() => {
  const hasModel = !!stageModelSelectedUrl.value

  if (stageModelRenderer.value === 'live2d') {
    const phase = resolveComponentStateToRuntimePhase(componentStateStage.value, { hasModel })

    return createEmptyModelSettingsRuntimeSnapshot({
      ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
      renderer: 'live2d',
      phase,
      controlsLocked: hasModel ? phase !== 'mounted' : false,
      previewAvailable: hasModel,
      canCapturePreview: false,
      updatedAt: Date.now(),
    })
  }

  if (stageModelRenderer.value === 'vrm') {
    return createEmptyModelSettingsRuntimeSnapshot({
      ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
      renderer: 'vrm',
      phase: hasModel ? scenePhase.value : 'no-model',
      controlsLocked: hasModel
        ? (!stageMounted.value || sceneMutationLocked.value)
        : false,
      previewAvailable: hasModel,
      canCapturePreview: false,
      updatedAt: Date.now(),
    })
  }

  if (stageModelRenderer.value === 'spine') {
    const phase = resolveComponentStateToRuntimePhase(componentStateStage.value, { hasModel })

    return createEmptyModelSettingsRuntimeSnapshot({
      ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
      renderer: 'spine',
      phase,
      controlsLocked: hasModel ? phase !== 'mounted' : false,
      previewAvailable: hasModel,
      canCapturePreview: false,
      updatedAt: Date.now(),
    })
  }

  if (stageModelRenderer.value === 'tachie') {
    const phase = resolveComponentStateToRuntimePhase(componentStateStage.value, { hasModel })

    return createEmptyModelSettingsRuntimeSnapshot({
      ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
      renderer: 'tachie',
      phase,
      controlsLocked: hasModel ? phase !== 'mounted' : false,
      previewAvailable: hasModel,
      canCapturePreview: false,
      updatedAt: Date.now(),
    })
  }

  if (stageModelRenderer.value === 'mmd') {
    const phase = resolveComponentStateToRuntimePhase(componentStateStage.value, { hasModel })

    return createEmptyModelSettingsRuntimeSnapshot({
      ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
      renderer: 'mmd',
      phase,
      controlsLocked: hasModel ? phase !== 'mounted' : false,
      previewAvailable: hasModel,
      canCapturePreview: false,
      updatedAt: Date.now(),
    })
  }

  if (stageModelRenderer.value === 'godot') {
    return createEmptyModelSettingsRuntimeSnapshot({
      ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
      renderer: 'godot',
      phase: hasModel ? 'mounted' : 'no-model',
      controlsLocked: false,
      previewAvailable: false,
      canCapturePreview: false,
      updatedAt: Date.now(),
    })
  }

  return createEmptyModelSettingsRuntimeSnapshot({
    ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
    updatedAt: Date.now(),
  })
})

/**
 * Keeps the rendered fade state and Electron click-through state synchronized.
 *
 * Triggering workflow:
 *
 * {@link watch}
 *   -> `fade-on-hover reactive state change`
 *     -> {@link handleFadeOnHoverInteractionChange}
 *
 * Upstream:
 * - {@link isOutsideFor250Ms} and {@link isAroundWindowBorderFor250Ms}
 * - {@link isOutsideWindow}, {@link isTransparent}, and {@link isTransparentForMouseEvents}
 * - {@link hearingDialogOpen}, {@link fadeOnHoverEnabled}, and {@link stagePaused}
 *
 * Downstream:
 * - {@link resolveFadeOnHoverInteraction}
 * - {@link setIgnoreMouseEvents}
 */
function handleFadeOnHoverInteractionChange() {
  if (stagePaused.value) {
    isIgnoringMouseEvents.value = false
    shouldFadeOnCursorWithin.value = false
    setIgnoreMouseEvents([false, { forward: true }])
    return
  }

  if (hearingDialogOpen.value) {
    // Hearing dialog/drawer is open; keep window interactive
    isIgnoringMouseEvents.value = false
    shouldFadeOnCursorWithin.value = false
    setIgnoreMouseEvents([false, { forward: true }])
    return
  }

  const insideControls = !isOutsideFor250Ms.value
  const nearBorder = isAroundWindowBorderFor250Ms.value

  if (insideControls || nearBorder) {
    // Inside interactive controls or near resize border: do NOT ignore events
    isIgnoringMouseEvents.value = false
    shouldFadeOnCursorWithin.value = false
    setIgnoreMouseEvents([false, { forward: true }])
  }
  else {
    const interaction = resolveFadeOnHoverInteraction({
      cursorInsideWindow: !isOutsideWindow.value,
      enabled: fadeOnHoverEnabled.value,
      transparentForFade: isTransparent.value,
      transparentForPointer: isTransparentForMouseEvents.value,
    })

    isIgnoringMouseEvents.value = interaction.ignoreMouseEvents
    shouldFadeOnCursorWithin.value = interaction.fadeStage
    setIgnoreMouseEvents([interaction.ignoreMouseEvents, { forward: true }])
  }
}

watch(
  [isOutsideFor250Ms, isAroundWindowBorderFor250Ms, isOutsideWindow, isTransparent, isTransparentForMouseEvents, hearingDialogOpen, fadeOnHoverEnabled, stagePaused],
  handleFadeOnHoverInteractionChange,
  { immediate: true },
)

// Emit runtime snapshot on change and on request from settings panel
/**
 * Sends model-settings runtime events without letting closed HMR channels break the stage.
 */
function postModelSettingsRuntimeEvent(event: ModelSettingsRuntimeChannelEvent) {
  const { error } = tryCatch(() => postModelSettingsRuntimeChannelEvent(event))
  if (error)
    console.warn('[Main Page] Failed to post model settings runtime event:', error)
}

watch(modelSettingsRuntimeSnapshot, (snapshot) => {
  postModelSettingsRuntimeEvent({ type: 'snapshot', snapshot })
}, { immediate: true })

watch(modelSettingsRuntimeChannelEvent, (event) => {
  if (event?.type !== 'request-current')
    return

  postModelSettingsRuntimeEvent({ type: 'snapshot', snapshot: modelSettingsRuntimeSnapshot.value })
})

const settingsAudioDeviceStore = useSettingsAudioDevice()
const { stream, enabled: savedAudioEnabled } = storeToRefs(settingsAudioDeviceStore)
const { askPermission, startStream, stopStream } = settingsAudioDeviceStore
const { nowSpeaking, assistantTurnBusy } = storeToRefs(useSpeakingStore())
const speechOutputControl = useSpeechOutputControlStore()
const pushToTalk = usePushToTalk(savedAudioEnabled, interruptForPushToTalk)
const enabled = pushToTalk.enabled
watch(pushToTalk.active, active => speechOutputControl.pushToTalkActive = active, { flush: 'sync' })
const hearingStore = useHearingStore()
const { activeTranscriptionModel, activeTranscriptionProvider } = storeToRefs(hearingStore)
const hearingPipeline = useHearingSpeechInputPipeline()
const { removeStreamingTranscriptionConsumer, transcribeForMediaStream, stopStreamingTranscription } = hearingPipeline
const { error: transcriptionError, supportsStreamInput } = storeToRefs(hearingPipeline)
const transcriptionConsumerId = 'stage-tamagotchi:voice-input'
const chatStore = useChatStore()
const chatSession = useChatSessionStore()
const streamingTranscriptionUnavailable = ref(false)
const shouldUseStreamInput = computed(() => supportsStreamInput.value && !!stream.value && !streamingTranscriptionUnavailable.value)
/** 语音转写 → 等待播放 → 交给聊天，这一段的串行链（阿尔比恩自加，2026-09-25）。 */
let voiceSendTail: Promise<void> = Promise.resolve()

const voiceTranscriptBuffer = createVoiceInputSendBuffer({
  // 等待期间（2 秒）字幕显示已经拼好的整段，而不是最后一小段
  onAccumulatedText: text => postSpeakerCaption(text, 'replace'),
  send(text) {
    // 阿尔比恩（自加，2026-09-25）：这里不 await（避免 stopAudioInteractionConsumers
    // 被一整轮播放拖住、麦克风恢复被卡），但**必须自己串行化** ——
    // 每句都可能要等播放，谁先等完谁先入聊天队列；聊天侧的 FIFO 救不了入队前的乱序。
    // 目标会话也在这一刻固定：等播放期间用户可能切会话。
    const targetSessionId = chatSession.activeSessionId
    voiceSendTail = voiceSendTail
      .then(() => sendVoiceInputTextToChat(text, targetSessionId))
      .catch(error => reportVoiceInputFailure('send to chat', error))
  },
})

const assistantSpeechSuppressedUntil = shallowRef(0)
const assistantSpeechResumeTimer = shallowRef<ReturnType<typeof setTimeout>>()
let voiceInputGeneration = 0
let drainingRecording = false

/** Controls transcript cleanup while voice input stops. */
interface StopAudioInteractionOptions {
  /** Flushes pending transcript text to chat before stop completes. */
  flushTranscript?: boolean
  flushActiveRecording?: boolean
}

const voiceInputInteractionLifecycle = createVoiceInputInteractionLifecycle<StopAudioInteractionOptions>({
  start: startAudioInteractionConsumers,
  stop: stopAudioInteractionConsumers,
})

function interruptForPushToTalk() {
  clearAssistantSpeechResumeTimer()
  assistantSpeechSuppressedUntil.value = 0
  speechOutputControl.requestStopSpeaking('push-to-talk')
  void voiceInputInteractionLifecycle.start().catch(error => reportVoiceInputFailure('start push-to-talk', error))
}

// Caption overlay broadcast channel
const { post: postCaption } = useBroadcastChannel<CaptionChannelEvent, CaptionChannelEvent>({ name: 'airi-caption-overlay' })
const { post: postHearingInput } = useBroadcastChannel<HearingInputChannelEvent, HearingInputChannelEvent>({ name: hearingInputChannelName })
const hearingInputClearTimers = new Map<ReturnType<typeof setTimeout>, string>()
let hearingInputSequence = 0
let activeHearingInputSourceId: string | undefined

function currentHearingInputSourceId() {
  activeHearingInputSourceId ??= `stage-tamagotchi:${++hearingInputSequence}`
  return activeHearingInputSourceId
}

function postHearingInputEvent(event: HearingInputChannelEvent) {
  const { error } = tryCatch(() => postHearingInput(event))
  if (error)
    console.warn('[Main Page] Failed to post Hearing input text:', error)
}

function replaceHearingInput(text: string) {
  postHearingInputEvent({
    operation: 'replace',
    sourceId: currentHearingInputSourceId(),
    text,
  })
}

function clearHearingInput(sourceId = activeHearingInputSourceId) {
  if (!sourceId)
    return

  postHearingInputEvent({ operation: 'clear', sourceId })
  if (sourceId === activeHearingInputSourceId)
    activeHearingInputSourceId = undefined
}

function scheduleHearingInputClear(sourceId: string) {
  const timer = setTimeout(() => {
    hearingInputClearTimers.delete(timer)
    clearHearingInput(sourceId)
  }, 250)
  hearingInputClearTimers.set(timer, sourceId)
}

/**
 * Reports a voice input pipeline failure to both the console and visible app UI.
 */
function reportVoiceInputFailure(action: string, error: unknown) {
  const reason = errorMessageFrom(error)
  const message = reason
    ? `Voice input failed to ${action}: ${reason}`
    : `Voice input failed to ${action}.`
  console.error(`[Main Page] ${message}`, error)
  toast.error(message)
}

/**
 * Checks whether current voice input should be ignored to avoid assistant self-transcription.
 */
function isVoiceInputSuppressed(now = Date.now()) {
  if (pushToTalk.active.value || drainingRecording)
    return false
  return shouldSuppressVoiceInput({
    assistantSpeaking: nowSpeaking.value,
    suppressedUntil: assistantSpeechSuppressedUntil.value,
  }, now)
}

/**
 * Captures whether a queued VAD segment can still leave the app for ASR.
 */
function inspectVoiceInputProviderRequestGate(generation: unknown) {
  const current = generation === voiceInputGeneration
  const audioEnabled = enabled.value || drainingRecording
  const suppressed = isVoiceInputSuppressed()
  let reason: string | undefined
  if (!current)
    reason = 'Skipped stale voice input segment'
  else if (!audioEnabled)
    reason = 'Skipped voice input segment because audio input is disabled'
  else if (suppressed)
    reason = 'Skipped voice input segment while assistant speech is active or cooling down'

  return {
    generation,
    activeGeneration: voiceInputGeneration,
    current,
    enabled: audioEnabled,
    suppressed,
    reason,
    skip: !current || !audioEnabled || suppressed,
  }
}

/**
 * Captures whether live microphone audio can still leave the app for streaming ASR.
 */
function inspectVoiceInputStreamingRequestGate() {
  const audioEnabled = enabled.value
  const suppressed = isVoiceInputSuppressed()

  return {
    enabled: audioEnabled,
    suppressed,
    skip: !audioEnabled || suppressed,
  }
}

/**
 * Clears the pending assistant-speech resume timer.
 */
function clearAssistantSpeechResumeTimer() {
  if (!assistantSpeechResumeTimer.value)
    return

  clearTimeout(assistantSpeechResumeTimer.value)
  assistantSpeechResumeTimer.value = undefined
}

/**
 * Restarts voice input after assistant playback tail audio should be gone.
 */
function scheduleAssistantSpeechResume() {
  clearAssistantSpeechResumeTimer()

  if (!enabled.value)
    return

  const remainingCooldownMs = Math.max(
    0,
    assistantSpeechSuppressedUntil.value
      ? assistantSpeechSuppressedUntil.value - Date.now()
      : DEFAULT_ASSISTANT_SPEECH_INPUT_COOLDOWN_MS,
  )
  const cooldownMs = nowSpeaking.value
    ? DEFAULT_ASSISTANT_SPEECH_INPUT_COOLDOWN_MS
    : remainingCooldownMs

  assistantSpeechResumeTimer.value = setTimeout(() => {
    assistantSpeechResumeTimer.value = undefined
    if (!enabled.value || isVoiceInputSuppressed())
      return

    void voiceInputInteractionLifecycle.start().catch(error => reportVoiceInputFailure('resume listening', error))
  }, cooldownMs)
}

/**
 * Ensures the microphone stream has a live audio track before binding recorder or VAD.
 */
async function ensureLiveAudioInputStream() {
  if (!enabled.value)
    return false

  if (stream.value?.getAudioTracks().some(track => track.readyState === 'live'))
    return true

  stopStream()

  if (!enabled.value)
    return false

  await askPermission()

  if (!enabled.value)
    return false

  await startStream()

  if (!enabled.value) {
    stopStream()
    return false
  }

  if (stream.value?.getAudioTracks().some(track => track.readyState === 'live'))
    return true

  throw new Error('Microphone stream did not provide a live audio track')
}

/**
 * Sends voice captions as best-effort overlay updates without interrupting chat ingestion.
 */
function postSpeakerCaption(text: string, operation: NonNullable<CaptionChannelEvent['operation']> = 'append') {
  const { error } = tryCatch(() => postCaption({ operation, type: 'caption-speaker', text }))
  if (error)
    console.warn('[Main Page] Failed to post voice input caption:', error)
}

/**
 * Sends buffered voice input text to the active chat session.
 */
async function sendVoiceInputTextToChat(text: string, targetSessionId = chatSession.activeSessionId) {
  try {
    await waitForAssistantTurnToSettle(assistantTurnBusy)
    await chatStore.send({
      sessionId: targetSessionId,
      text,
    })
  }
  catch (err) {
    reportVoiceInputFailure('send to chat', err)
  }
}

/** 流式路径：这一句已经开口（有 partial 进来），发送计时按住了没有。 */
let streamingSendHeld = false

/** 流式 partial 说明她还在说：按住发送计时，等这一句的 final 再重新计时。 */
function holdStreamingSend() {
  if (streamingSendHeld)
    return

  streamingSendHeld = true
  voiceTranscriptBuffer.hold()
}

/** 流式这一句有结局了（final / 整段结束）：松开按住的计时。 */
function releaseStreamingSend() {
  if (!streamingSendHeld)
    return

  streamingSendHeld = false
  voiceTranscriptBuffer.resume()
}

/** Sends completed streaming-ASR sentences to captions and chat. */
function handleStreamingSentenceEnd(delta: string) {
  if (isVoiceInputSuppressed())
    return

  const finalText = delta
  if (!finalText || !finalText.trim())
    return

  const sourceId = currentHearingInputSourceId()
  replaceHearingInput(finalText)
  scheduleHearingInputClear(sourceId)
  activeHearingInputSourceId = undefined
  // 这一句的 final 到了：先松开 partial 按住的计时，再把它并进待发文字（push 会重新计 2 秒）
  releaseStreamingSend()
  voiceTranscriptBuffer.push(finalText)
}

/** Replaces the caption with the provider's current volatile transcript. */
function handleStreamingTranscriptionUpdate(text: string) {
  if (isVoiceInputSuppressed())
    return

  replaceHearingInput(text)
  holdStreamingSend()
  // 字幕显示「已经攒着待发的整段 + 这一句的 partial」，别把前面的盖掉
  postSpeakerCaption(joinTranscriptFragments(voiceTranscriptBuffer.pendingText, text.trim()), 'replace')
}

/** Publishes the provider's final streaming-ASR text to the caption overlay. */
function handleStreamingSpeechEnd(text: string) {
  if (isVoiceInputSuppressed())
    return

  releaseStreamingSend()

  const finalText = text.trim()
  // 逐句结果已经攒起来了：字幕显示攒着的那整段；一句都没攒到时用 provider 的整段兜底
  if (voiceTranscriptBuffer.pendingText) {
    postSpeakerCaption(voiceTranscriptBuffer.pendingText, 'replace')
    return
  }

  if (finalText)
    voiceTranscriptBuffer.push(finalText)
}

/** Reads the listening generation attached to recorder-backed transcription metadata. */
function getVoiceInputGeneration(metadata?: Record<string, unknown>) {
  return typeof metadata?.generation === 'number' ? metadata.generation : undefined
}

const voiceInputSession = useVoiceInputSession(stream, {
  shouldUseStreamInput,
  onLog(level, event, message, details) {
    const output = `[Voice Input] ${event}: ${message}`
    if (level === 'error') {
      console.error(output, details ?? {})
      return
    }
    if (level === 'warn') {
      console.warn(output, details ?? {})
      return
    }
    console.info(output, details ?? {})
  },
  canStartSegment: () => enabled.value && !isVoiceInputSuppressed(),
  inspectBeforeTranscription: ({ metadata }) => inspectVoiceInputProviderRequestGate(getVoiceInputGeneration(metadata)),
  inspectAfterTranscription: ({ metadata }) => inspectVoiceInputProviderRequestGate(getVoiceInputGeneration(metadata)),
  onRecordingReady: () => ({ generation: voiceInputGeneration }),
  onSegmentStart: () => {
    // VAD 判断有人开始说话了：先按住发送计时，等这一段的结局（有结果 / 空 / 报错 / 被挡）
    voiceTranscriptBuffer.hold()
  },
  onRecordingSkipped: () => {
    // 这一段没东西可转写（空录音 / 闸门挡掉）：别一直按着，恢复计时
    voiceTranscriptBuffer.resume()
  },
  onTranscriptionResult: ({ text }) => {
    toast(`Voice input transcribed: ${text}`)
    // 字幕和发送都走同一个防抖缓冲：2 秒内还有新识别结果就接着拼（字幕显示整段）；
    // push 同时消费掉这一段按住的那次 hold，重新计 2 秒
    voiceTranscriptBuffer.push(text)
  },
  onTranscriptionEmpty: () => {
    // 不是人说话的声音（咳嗽、碰撞、电视）也会被拾到 → 静默忽略，不弹提示
    // （指挥官点名要去掉 "voice input failed to transcribe speech" 这个报警）
    voiceTranscriptBuffer.resume()
    console.info('[Voice Input] empty transcription, ignored silently')
  },
  onTranscriptionError: ({ error }) => {
    // 同一类的噪音/极短音频失败也不弹窗，仅留日志便于排障；按住的那次也要松开
    voiceTranscriptBuffer.resume()
    console.error('[Voice Input] transcription failed, suppressed (no toast)', error)
  },
})

/** Starts the active streaming or recorder-backed voice-input consumers. */
async function startAudioInteractionConsumers() {
  if (isVoiceInputSuppressed()) {
    scheduleAssistantSpeechResume()
    return
  }

  if (!await ensureLiveAudioInputStream())
    return

  if (shouldUseStreamInput.value) {
    const currentStream = stream.value
    if (!currentStream)
      throw new Error('Microphone stream is unavailable for streaming transcription')

    const requestGate = inspectVoiceInputStreamingRequestGate()
    if (requestGate.skip)
      return

    await transcribeForMediaStream(currentStream, {
      consumerId: transcriptionConsumerId,
      onSentenceEnd: handleStreamingSentenceEnd,
      onSpeechEnd: handleStreamingSpeechEnd,
      onTranscriptionUpdate: handleStreamingTranscriptionUpdate,
    })

    if (inspectVoiceInputStreamingRequestGate().skip) {
      await stopStreamingTranscription(true)
      return
    }

    if (transcriptionError.value) {
      streamingTranscriptionUnavailable.value = true
      await stopStreamingTranscription(true)
      console.warn('[Main Page] Streaming transcription unavailable; using recorder-backed fallback:', transcriptionError.value)
    }
  }

  if (!shouldUseStreamInput.value)
    await voiceInputSession.startAutoSegmentation()
}

/**
 * Stops active microphone consumers before the stage binds to another audio stream.
 */
async function stopAudioInteractionConsumers(options: StopAudioInteractionOptions = {}) {
  const flushTranscript = options.flushTranscript ?? true
  const flushActiveRecording = options.flushActiveRecording ?? false

  clearAssistantSpeechResumeTimer()
  clearHearingInput()
  drainingRecording = flushActiveRecording
  if (!flushActiveRecording)
    voiceInputGeneration += 1
  try {
    await Promise.all([
      stopStreamingTranscription(!flushActiveRecording),
      voiceInputSession.stop({ flushActiveRecording }),
    ])
    if (flushTranscript)
      await voiceTranscriptBuffer.dispose()
    else
      voiceTranscriptBuffer.clear()
    // 停麦/换设置：流式那次「还说着一半」的记号也一起清掉
    streamingSendHeld = false
  }
  finally {
    if (flushActiveRecording)
      voiceInputGeneration += 1
    drainingRecording = false
    if (!enabled.value)
      stopStream()
  }
}

watch(enabled, async (val) => {
  try {
    if (val) {
      await askPermission()
      await voiceInputInteractionLifecycle.start()
    }
    else {
      await voiceInputInteractionLifecycle.stop({ flushActiveRecording: true })
    }
  }
  catch (error) {
    reportVoiceInputFailure(val ? 'start listening' : 'stop listening', error)
    if (val)
      savedAudioEnabled.value = false
    pushToTalk.dispose()
  }
}, { immediate: true })

watch([activeTranscriptionProvider, activeTranscriptionModel, supportsStreamInput], async () => {
  streamingTranscriptionUnavailable.value = false
  if (!enabled.value)
    return

  try {
    // 阿尔比恩（自加，2026-09-25）：这里保持 false —— 换转写设置是「重来一遍」，
    // 不是「用户说完一句」，不该把缓冲里的半句当完整话发出去。
    // （真正会丢话的是「她开始说话」那处，已改成 true。）
    await voiceInputInteractionLifecycle.stop({ flushTranscript: false })
    await voiceInputInteractionLifecycle.start()
  }
  catch (error) {
    reportVoiceInputFailure('restart after transcription settings changed', error)
    savedAudioEnabled.value = false
    pushToTalk.dispose()
  }
})

watch(nowSpeaking, async (speaking) => {
  if (pushToTalk.active.value || drainingRecording)
    return
  if (speaking) {
    clearAssistantSpeechResumeTimer()
    try {
      await voiceInputInteractionLifecycle.stop({ flushTranscript: true })
    }
    catch (error) {
      reportVoiceInputFailure('pause while assistant is speaking', error)
    }
    return
  }

  assistantSpeechSuppressedUntil.value = assistantSpeechCooldownDeadline()
  scheduleAssistantSpeechResume()
})

// ── 麦克风全局快捷键（阿尔比恩自加，2026-09-25）──
// 按住 V 开麦、松开 V 关麦。
// receiveKeyUps: true 会走 uiohook 驱动（按下/松开都收），所以注册必须在渲染端（主进程 registerMainShortcut 只走 electron，没有 up）。
// 按住 Alt 再按 V 才开麦（Alt 组合不产生文字 ⇒ 不会喂给输入法/文本框）；单按 V 不管用。
const MIC_SHORTCUT_ID = 'albion-mic-ptt'
const registerMicShortcut = useElectronEventaInvoke(electronShortcutRegister)
const unregisterMicShortcut = useElectronEventaInvoke(electronShortcutUnregister)
let disposeMicShortcutListener: (() => void) | undefined
// PTT state and its release timer belong to this mounted page.
let micShortcutMounted = false
// 注册/注销进同一条队列：避免“await 期间卸载⇒ 卸载后才注册成功”的残留绑定
let micShortcutChain: Promise<unknown> = Promise.resolve()
function queueMicShortcut(task: () => Promise<unknown>) {
  micShortcutChain = micShortcutChain.then(task, task)
  return micShortcutChain
}

onMounted(() => {
  micShortcutMounted = true
  disposeMicShortcutListener = getElectronEventaContext().on(electronShortcutTriggered, (event) => {
    const payload = event?.body
    if (payload?.id !== MIC_SHORTCUT_ID)
      return
    if (payload.phase === 'down') {
      pushToTalk.press()
    }
    else if (payload.phase === 'up') {
      pushToTalk.release()
    }
  })
  void queueMicShortcut(async () => {
    try {
      // 先注销再注册：上一次页面卸载时可能有晚到的绑定残留（重挂载/HMR）
      await unregisterMicShortcut({ id: MIC_SHORTCUT_ID })
      const result = await registerMicShortcut({
        id: MIC_SHORTCUT_ID,
        accelerator: parseAccelerator('Alt+KeyV'),
        scope: 'global',
        receiveKeyUps: true,
      })
      if (!micShortcutMounted) {
        await unregisterMicShortcut({ id: MIC_SHORTCUT_ID })
        return
      }
      if (!result?.ok)
        console.warn('[albion-pet] 麦克风按住说话快捷键注册失败', result)
    }
    catch (error) {
      console.warn('[albion-pet] 麦克快捷键注册异常', error)
    }
  })
})

onMounted(() => {
  if (onboardingStore.needsOnboarding) {
    openOnboarding()
  }
})

onUnmounted(() => {
  micShortcutMounted = false
  disposeMicShortcutListener?.()
  disposeMicShortcutListener = undefined
  pushToTalk.dispose()
  void queueMicShortcut(() => unregisterMicShortcut({ id: MIC_SHORTCUT_ID })).catch(() => {})
  removeStreamingTranscriptionConsumer(transcriptionConsumerId)
  for (const [timer, sourceId] of hearingInputClearTimers) {
    clearTimeout(timer)
    clearHearingInput(sourceId)
  }
  hearingInputClearTimers.clear()
  clearHearingInput()
  postModelSettingsRuntimeEvent({
    type: 'owner-gone',
    ownerInstanceId: modelSettingsRuntimeOwnerInstanceId,
  })
  clearAssistantSpeechResumeTimer()
  void voiceInputInteractionLifecycle.stop().catch(error => reportVoiceInputFailure('stop listening', error))
})

watch(stream, async (currentStream) => {
  if (!enabled.value || !currentStream || voiceInputInteractionLifecycle.isStarting() || voiceInputInteractionLifecycle.isStopping() || isVoiceInputSuppressed())
    return

  // NOTICE: The controls-island mic toggle and device changes can replace the underlying MediaStream
  // without reloading the page. When that happens, VAD may successfully restart against the new stream,
  // but any existing transcription transport is still bound to the old one. Always allow the page to
  // restart voice input for a newly available stream unless another lifecycle operation is underway.
  try {
    await voiceInputInteractionLifecycle.stop()
    await voiceInputInteractionLifecycle.start()
  }
  catch (error) {
    reportVoiceInputFailure('restart after microphone changed', error)
    savedAudioEnabled.value = false
    pushToTalk.dispose()
  }
})

// Assistant caption is broadcast from Stage.vue via the same channel

const cursorPosition = computed(() => ({
  x: relativeMouseX.value,
  y: relativeMouseY.value,
}))
</script>

<template>
  <div
    max-h="[100vh]"
    max-w="[100vw]"
    flex="~ col"
    relative z-2 h-full overflow-hidden rounded-xl
    transition="opacity duration-500 ease-in-out"
  >
    <!-- Stage is always in DOM so TresCanvas can measure dimensions -->
    <div
      :class="[
        'relative h-full w-full items-end gap-2',
        'transition-opacity duration-250 ease-in-out',
      ]"
    >
      <div
        :class="[
          shouldFadeOnCursorWithin ? 'op-0' : 'op-100',
          'absolute',
          'top-0 left-0 w-full h-full',
          'overflow-hidden',
          'rounded-2xl',
          'transition-opacity duration-250 ease-in-out',
        ]"
      >
        <ResourceStatusIsland />
        <WidgetStage
          ref="widgetStageRef"
          v-model:state="componentStateStage"
          h-full w-full
          flex-1
          :cursor-position="cursorPosition"
          :paused="stagePaused"
        />
        <HoloCoupon />
        <ControlsIslandRoot :frozen="controlsIslandInteractionActive">
          <ControlsIsland
            ref="controlsIslandRef"
            @interaction-change="controlsIslandInteractionActive = $event"
          />
        </ControlsIslandRoot>
      </div>
    </div>
    <!-- Loading overlay sits on top, does not hide the stage -->
    <div v-show="isLoading" class="absolute left-0 top-0 z-99 h-full w-full flex cursor-grab items-center justify-center overflow-hidden">
      <div
        :class="[
          'absolute h-24 w-full overflow-hidden rounded-xl',
          'flex items-center justify-center',
          'bg-white/80 dark:bg-neutral-950/80',
          'backdrop-blur-md',
        ]"
      >
        <div
          :class="[
            'drag-region',
            'absolute left-0 top-0',
            'h-full w-full flex items-center justify-center',
            'text-1.5rem text-primary-600 dark:text-primary-400 font-normal',
            'select-none',
            'animate-flash animate-duration-5s animate-count-infinite',
          ]"
        >
          Loading...
        </div>
      </div>
    </div>
  </div>
  <Transition
    enter-active-class="transition-opacity duration-250"
    enter-from-class="opacity-0"
    enter-to-class="opacity-100"
    leave-active-class="transition-opacity duration-250"
    leave-from-class="opacity-100"
    leave-to-class="opacity-0"
  >
    <div
      v-if="false"
      class="absolute left-0 top-0 z-99 h-full w-full flex cursor-grab items-center justify-center overflow-hidden drag-region"
    >
      <div
        class="absolute h-32 w-full flex items-center justify-center overflow-hidden rounded-xl"
        bg="white/80 dark:neutral-950/80" backdrop-blur="md"
      >
        <div class="wall absolute top-0 h-8" />
        <div
          :class="[
            'absolute left-0 top-0 h-full w-full',
            'flex items-center justify-center',
            'animate-flash animate-duration-5s animate-count-infinite',
            'select-none text-1.5rem text-primary-400 font-normal drag-region',
          ]"
        >
          DRAG HERE TO MOVE
        </div>
        <div class="wall absolute bottom-0 h-8 drag-region" />
      </div>
    </div>
  </Transition>
  <Transition
    enter-active-class="transition-opacity duration-250 ease-in-out"
    enter-from-class="opacity-50"
    enter-to-class="opacity-100"
    leave-active-class="transition-opacity duration-250 ease-in-out"
    leave-from-class="opacity-100"
    leave-to-class="opacity-50"
  >
    <div v-if="isAroundWindowBorderFor250Ms && !isLoading" class="pointer-events-none absolute left-0 top-0 z-999 h-full w-full">
      <div
        :class="[
          'b-primary/50',
          'h-full w-full animate-flash animate-duration-3s animate-count-infinite b-4 rounded-2xl',
        ]"
      />
    </div>
  </Transition>
</template>

<style scoped>
@keyframes wall-move {
  0% {
    transform: translateX(calc(var(--wall-width) * -2));
  }
  100% {
    transform: translateX(calc(var(--wall-width) * 1));
  }
}

.wall {
  --at-apply: text-primary-300;

  --wall-width: 8px;
  animation: wall-move 1s linear infinite;
  background-image: repeating-linear-gradient(
    45deg,
    currentColor,
    currentColor var(--wall-width),
    #ff00 var(--wall-width),
    #ff00 calc(var(--wall-width) * 2)
  );
  width: calc(100% + 4 * var(--wall-width));
}
</style>

<route lang="yaml">
meta:
  layout: stage
</route>
