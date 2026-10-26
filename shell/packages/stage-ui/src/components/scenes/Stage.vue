<script setup lang="ts">
import type { Live2DLipSync, Live2DLipSyncOptions } from '@proj-airi/model-driver-lipsync'
import type { Profile } from '@proj-airi/model-driver-lipsync/shared/wlipsync'
import type { PlaybackItem } from '@proj-airi/pipelines-audio'
import type { CaptionChannelEvent } from '@proj-airi/stage-shared'
import type { VrmInteractionTarget } from '@proj-airi/stage-ui-three'
import type { SpeechProviderWithExtraOptions } from '@xsai-ext/providers/utils'
import type { UnElevenLabsOptions } from 'unspeech'

import type { EmotionPayload } from '../../constants/emotions'
import type { StreamingDisplaySpan } from '../../libs/speech/streaming-pipeline'
import type { StreamingPlayback } from '../../libs/speech/streaming-playback'
import type { SpeechTransport, StageTtsSession, StreamingSessionSnapshot } from '../../libs/speech/tts-session'

import { defineInvokeHandler } from '@moeru/eventa'
import { sleep } from '@moeru/std'
import { createLive2DLipSync } from '@proj-airi/model-driver-lipsync'
import { wlipsyncProfile } from '@proj-airi/model-driver-lipsync/shared/wlipsync'
import { createPlaybackManager, createSpeechPipeline, createTtsSegmentStream, normalizeActPayload } from '@proj-airi/pipelines-audio'
import { defaultLive2DMotionControlDynamics, Live2DScene, useLive2DMotionControl, useLive2dParams, useSettingsLive2d } from '@proj-airi/stage-ui-live2d'
import { MMDScene } from '@proj-airi/stage-ui-mmd'
import { mouthLevelFromRms, mouthPushFor, Spine38Scene, SpineScene } from '@proj-airi/stage-ui-spine'
import { TachieScene } from '@proj-airi/stage-ui-tachie'
import { ThreeScene } from '@proj-airi/stage-ui-three'
import { animations } from '@proj-airi/stage-ui-three/assets/vrm'
import { createQueue } from '@proj-airi/stream-kit'
import { Callout } from '@proj-airi/ui'
import { useBroadcastChannel } from '@vueuse/core'
// import { createTransformers } from '@xsai-transformers/embed'
// import embedWorkerURL from '@xsai-transformers/embed/worker?worker&url'
// import { embed } from '@xsai/embed'
import { storeToRefs } from 'pinia'
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'

import StageRenderError from './stage-render-error.vue'

import { bindAlbionStageEmotion, buildAlbionStreamingSnapshot, isAlbionActRenderer, isAlbionStreamTtsProvider } from '../../albion/albion-stage-bridge'
import { useDuckDb } from '../../composables/use-duck-db'
import { useIOTraceBridge } from '../../composables/use-io-trace-bridge'
import { initIOTracer } from '../../composables/use-io-tracer'
import { Emotion, EMOTION_EmotionMotionName_value, EMOTION_VRMExpressionName_value, EmotionThinkMotionName } from '../../constants/emotions'
import { live2dMotionMagicProfiles, useLive2DMotionMagic, useLive2DMotionMagicSettings } from '../../features/motions/live2d'
import { getDefaultStreamingModel, getDefinedProvider } from '../../libs/providers/providers'
import { OFFICIAL_SPEECH_PROVIDER_ID, OFFICIAL_SPEECH_STREAMING_PROVIDER_ID } from '../../libs/providers/providers/official'
import { createCaptionFallback } from '../../libs/speech/caption-fallback'
import { createDisplayLedger } from '../../libs/speech/display-ledger'
import { bindSpeakingStateToPlaybackManager } from '../../libs/speech/playback-speaking-state'
import { barrierArmValue, bindFadeBarrier, bindNaturalEndBarrier, createSpeechBarrier } from '../../libs/speech/speech-barrier'
import { createStreamClockRegistry } from '../../libs/speech/stream-clock-registry'
import { createStreamTurnTracker } from '../../libs/speech/stream-turn'
import { createStageTtsSession } from '../../libs/speech/tts-session'
import { getSpeechBusContext, speechOutputGetPlaybackState } from '../../services/speech/bus'
import { useLlmStreamingControlStore } from '../../stores/ai/chat-llm/streaming-control'
import { useAudioContext, useSpeakingStore } from '../../stores/audio'
import { useBackgroundStore } from '../../stores/background'
import { useChatStore } from '../../stores/chat'
import { useAiriCardStore } from '../../stores/modules'
import { useSpeechStore } from '../../stores/modules/speech'
import { useProviderConfigStore } from '../../stores/providers/config'
import { useProviderStore } from '../../stores/providers/provider'
import { useSettings } from '../../stores/settings'
import { useSpeechOutputControlStore } from '../../stores/speech-output-control'
import { useSpeechRuntimeStore } from '../../stores/speech-runtime'

const props = withDefaults(defineProps<{
  cursorPosition?: { x: number, y: number }
  enableOrbitControls?: boolean
  paused?: boolean
}>(), {
  enableOrbitControls: true,
  paused: false,
})

const componentState = defineModel<'pending' | 'loading' | 'mounted'>('state', { default: 'pending' })

const { getDb } = useDuckDb()
// const transformersProvider = createTransformers({ embedWorkerURL })

const vrmViewerRef = ref<InstanceType<typeof ThreeScene>>()
const live2dSceneRef = ref<InstanceType<typeof Live2DScene>>()
const spineSceneRef = ref<InstanceType<typeof SpineScene>>()
const spine38SceneRef = ref<InstanceType<typeof Spine38Scene>>()
const tachieSceneRef = ref<InstanceType<typeof TachieScene>>()
const mmdSceneRef = ref<InstanceType<typeof MMDScene>>()

const settingsStore = useSettings()
const {
  stageModelRenderer,
  stageViewControlsEnabled,
  stageModelSelectedUrl,
  stageModelSelected,
  themeColorsHue,
  themeColorsHueDynamic,

} = storeToRefs(settingsStore)
const {
  live2dMotionDriver,
  live2dShadowEnabled,
  live2dMaxFps,
  live2dRenderScale,
} = storeToRefs(useSettingsLive2d())
const live2dMotionControl = useLive2DMotionControl()
const { exclusiveOwnerId: live2dMotionControlOwnerId } = storeToRefs(live2dMotionControl)
const {
  forceViewTarget: live2dMagicForceViewTarget,
  profileId: live2dMagicProfileId,
  skipMouthOpen: live2dMagicSkipMouthOpen,
} = storeToRefs(useLive2DMotionMagicSettings())
const live2dMagicMotion = useLive2DMotionMagic({
  dataset: () => live2dMotionMagicProfiles[live2dMagicProfileId.value].dataset,
  forceViewTarget: live2dMagicForceViewTarget,
  skipMouthOpen: live2dMagicSkipMouthOpen,
  disabled: () => live2dMotionControlOwnerId.value !== null,
  publishPose: pose => live2dMotionControl.setPose('stage:live2d-motion-magic', pose, defaultLive2DMotionControlDynamics),
  releasePose: () => live2dMotionControl.release('stage:live2d-motion-magic'),
})
let live2dMagicActivationRequest = 0

watch(
  [stageModelRenderer, live2dMotionDriver, live2dMagicProfileId, () => props.paused, live2dMotionControlOwnerId],
  async ([renderer, driver, , paused, controlOwnerId]) => {
    const request = ++live2dMagicActivationRequest
    if (renderer !== 'live2d' || driver !== 'magic' || paused || controlOwnerId !== null) {
      live2dMagicMotion.stop()
      return
    }

    if (live2dMagicMotion.status.value === 'idle')
      await live2dMagicMotion.initialize()

    if (
      request !== live2dMagicActivationRequest
      || stageModelRenderer.value !== 'live2d'
      || live2dMotionDriver.value !== 'magic'
      || props.paused
      || live2dMotionControlOwnerId.value !== null
    ) {
      return
    }

    live2dMagicMotion.start()
  },
  { immediate: true },
)
const {
  spinePremultipliedAlpha,
  spineDefaultMixDuration,
  spineIdleAnimationEnabled,
  spineMaxFps,
  spineRenderScale,
} = storeToRefs(settingsStore)
const { mouthOpenSize, nowSpeaking, assistantTurnBusy } = storeToRefs(useSpeakingStore())
const disposePlaybackStateHandler = defineInvokeHandler(
  getSpeechBusContext(),
  speechOutputGetPlaybackState,
  () => ({ speaking: nowSpeaking.value }),
)
const { audioContext } = useAudioContext()
const currentAudioSource = ref<AudioBufferSourceNode>()
const speechOutputControlStore = useSpeechOutputControlStore()
const { latestStopRequest, speechMuted, pushToTalkActive } = storeToRefs(speechOutputControlStore)
const lastVrmInteractionAt = new Map<VrmInteractionTarget, number>()
const VRM_INTERACTION_COOLDOWN_MS = 450

function getVrmInteractionExpression(target: VrmInteractionTarget) {
  if (target === 'head')
    return 'happy'
  if (target === 'leftFoot' || target === 'rightFoot')
    return 'relaxed'
  return 'surprised'
}

function onVRMInteract(target: VrmInteractionTarget) {
  const now = Date.now()
  const lastTriggeredAt = lastVrmInteractionAt.get(target) ?? 0
  if (now - lastTriggeredAt < VRM_INTERACTION_COOLDOWN_MS)
    return
  lastVrmInteractionAt.set(target, now)
  vrmViewerRef.value?.setExpression(getVrmInteractionExpression(target), 1)
}

const { onBeforeMessageComposed, onBeforeSend, onTokenLiteral, onTokenSpecial, onStreamEnd, onAssistantResponseEnd } = useChatStore()
const chatHookCleanups: Array<() => void> = []
// WORKAROUND: clear previous handlers on unmount to avoid duplicate calls when this component remounts.
//             We keep per-hook disposers instead of wiping the global chat hooks to play nicely with
//             cross-window broadcast wiring.

const providersStore = useProviderStore()

const providerStore = useProviderConfigStore()
const live2dStore = useLive2dParams()
const showStage = ref(true)
const stageRenderError = shallowRef<Error>()
const viewUpdateCleanups: Array<() => void> = []

function handleStageRenderError(error: Error) {
  stageRenderError.value = error
}

async function retryStageRenderer() {
  stageRenderError.value = undefined
  showStage.value = false
  await nextTick()
  showStage.value = true
}

watch([stageModelRenderer, stageModelSelected, stageModelSelectedUrl], () => {
  stageRenderError.value = undefined
})

// Caption + Presentation broadcast channels
const { post: postCaption } = useBroadcastChannel<CaptionChannelEvent, CaptionChannelEvent>({ name: 'airi-caption-overlay' })
const assistantCaption = ref('')

type PresentEvent
  = | { type: 'assistant-reset' }
    | { type: 'assistant-append', text: string }
const { post: postPresent } = useBroadcastChannel<PresentEvent, PresentEvent>({ name: 'airi-chat-present' })

viewUpdateCleanups.push(live2dStore.onShouldUpdateView(async () => {
  showStage.value = false
  await settingsStore.updateStageModel()
  setTimeout(() => {
    showStage.value = true
  }, 100)
}))

const audioAnalyser = ref<AnalyserNode>()
const lipSyncStarted = ref(false)
const lipSyncLoopId = ref<number>()
const live2dLipSync = ref<Live2DLipSync>()
const live2dLipSyncOptions: Live2DLipSyncOptions = { mouthUpdateIntervalMs: 50, mouthLerpWindowMs: 50 }

function resetAssistantSpeechSurface(source: string, { clearCaption = true }: { clearCaption?: boolean } = {}) {
  nowSpeaking.value = false
  mouthOpenSize.value = 0
  assistantCaption.value = ''

  // 指挥官 2026-09-17：指挥官发消息时不要把上一条字幕擦掉 ——
  // 上一段话留到"下一句真的说出来"再被顶替（见 caption.vue 的淡出淡入）。
  if (!clearCaption)
    return

  try {
    postCaption({ type: 'caption-assistant', text: '', operation: 'replace' })
  }
  catch (error) {
    console.warn(`[Stage] Failed to post caption reset for ${source} (channel may be closed)`, { error })
  }

  try {
    postPresent({ type: 'assistant-reset' })
  }
  catch (error) {
    console.warn(`[Stage] Failed to post present reset for ${source} (channel may be closed)`, { error })
  }
}

const { activeCard } = storeToRefs(useAiriCardStore())
const speechStore = useSpeechStore()
const { ssmlEnabled, activeSpeechProvider, activeSpeechModel, activeSpeechVoice, pitch } = storeToRefs(speechStore)
const activeCardId = computed(() => activeCard.value?.name ?? 'default')
const speechRuntimeStore = useSpeechRuntimeStore()
const backgroundStore = useBackgroundStore()
const { activeBackgroundUrl } = storeToRefs(backgroundStore)

const { currentMotion } = storeToRefs(useLive2dParams())

const emotionsQueue = createQueue<EmotionPayload>({
  handlers: [
    async (ctx) => {
      if (stageModelRenderer.value === 'vrm') {
        // console.debug('VRM emotion anime: ', ctx.data)
        const value = EMOTION_VRMExpressionName_value[ctx.data.name]
        if (!value)
          return

        await vrmViewerRef.value!.setExpression(value, ctx.data.intensity)
      }
      else if (stageModelRenderer.value === 'live2d') {
        currentMotion.value = { group: EMOTION_EmotionMotionName_value[ctx.data.name] }
      }
      else if (stageModelRenderer.value === 'spine') {
        spineSceneRef.value?.setEmotion(ctx.data.name, ctx.data.intensity)
      }
      else if (stageModelRenderer.value === 'spine38') {
        spine38SceneRef.value?.setEmotion(ctx.data.name, ctx.data.intensity)
      }
      else if (stageModelRenderer.value === 'tachie') {
        tachieSceneRef.value?.setEmotion(ctx.data.name, ctx.data.intensity)
      }
      else if (stageModelRenderer.value === 'mmd') {
        mmdSceneRef.value?.setEmotion(ctx.data.name, ctx.data.intensity)
      }
    },
  ],
})

const streamingControl = useLlmStreamingControlStore()

/**
 * 本机 v2（P5）字幕账本，见 `libs/speech/display-ledger.ts`：
 * 文本来源是服务端 `display.span`（原文，含心声标点这类不朗读的字符），不是"播到的句子"；
 * 纯心声/纯符号段没有音频，旧路径会把它们整段漏掉，这里照样落账 —— 原文守恒。
 * 一旦有 span 进来，`driven` 置位，onStart 那条老路就停手（否则同一句会记两遍）。
 */
const displayLedger = createDisplayLedger()
// 没有可用语音会话（9885 挂了 / 静音 / 按键说话）时，靠它把整轮原文直接顶到字幕和展示通道。
const captionFallback = createCaptionFallback()

// 阿尔比恩桌宠的情绪入口（挂载原因见 albion-stage-bridge）。
// 只对"自带表情分层的渲染器"生效：Live2D 有自己的姿态表（albion-pet 的 POSES）。
// Spine 3.8 直接收整条 ACT（face / motion / hold），换脸和头部动作在场景里做。
bindAlbionStageEmotion((name, intensity = 1, act = {}) => {
  if (stageModelRenderer.value === 'spine38')
    return spine38SceneRef.value?.applyAct({ ...act, emotion: name }) ?? false
  if (!['spine', 'tachie'].includes(stageModelRenderer.value ?? ''))
    return false
  const payload = toStageEmotionPayload({ name, intensity })
  if (!payload)
    return false
  emotionsQueue.enqueue(payload)
  return true
})

function toStageEmotionPayload(payload: { name: string, intensity: number }): EmotionPayload | undefined {
  switch (payload.name) {
    case 'happy':
      return { name: Emotion.Happy, intensity: payload.intensity }
    case 'sad':
      return { name: Emotion.Sad, intensity: payload.intensity }
    case 'angry':
      return { name: Emotion.Angry, intensity: payload.intensity }
    case 'think':
      return { name: Emotion.Think, intensity: payload.intensity }
    case 'surprised':
      return { name: Emotion.Surprise, intensity: payload.intensity }
    case 'awkward':
      return { name: Emotion.Awkward, intensity: payload.intensity }
    case 'question':
      return { name: Emotion.Question, intensity: payload.intensity }
    case 'curious':
      return { name: Emotion.Curious, intensity: payload.intensity }
    case 'neutral':
      return { name: Emotion.Neutral, intensity: payload.intensity }
    default:
      return undefined
  }
}

chatHookCleanups.push(streamingControl.onSignal(async (signal) => {
  if (signal.type === 'act') {
    // 阿尔比恩的渲染器由 albion-pet 按 ACT 演（见 isAlbionActRenderer），这里再演就是两遍
    if (isAlbionActRenderer(stageModelRenderer.value))
      return
    const act = normalizeActPayload(signal.payload)
    if (act.motion && stageModelRenderer.value === 'live2d') {
      currentMotion.value = { group: act.motion }
      return
    }
    if (act.emotion) {
      const emotion = toStageEmotionPayload(act.emotion)
      if (!emotion)
        return

      // eslint-disable-next-line no-console
      console.debug('emotion detected', emotion)
      emotionsQueue.enqueue(emotion)
    }
    return
  }

  if (signal.type === 'delay') {
    // eslint-disable-next-line no-console
    console.debug('delay detected', signal.seconds)
    await sleep(signal.seconds * 1000)
  }
}))

// Play special token: plugin CALL, delay, or emotion.
async function playSpecialToken(
  special: string,
  options?: {
    turnId?: string
    intentId?: string
    streamId?: string
  },
) {
  await streamingControl.dispatchWith(special, {
    turnId: options?.turnId,
    intentId: options?.intentId,
    streamId: options?.streamId,
  })
}
const lipSyncNode = ref<AudioNode>()

/** 流式 item 认领标记：一个 turn 一个 item，音频在 `clock` 里按时间轴排（§4.3）。 */
interface StreamPlaybackItem { clock?: StreamingPlayback }

/**
 * "这一轮的流式 item 是否还在播" —— 只跟 M 事件里的 item 走，**不读 `currentSession`**：
 * 服务端 `onDone` 会先清掉会话，而 M 可能还没启动排队的 item、或时钟里还有尾音
 * （codex 复核指出：按会话判断会让 M 启动流式 item 时被当成老路径、并让空档永不闭嘴）。
 */
const streamTurn = createStreamTurnTracker<PlaybackItem<AudioBuffer>>()

/**
 * 跨轮屏障：新一轮首段不许早于「上一轮淡出终点 + 20ms」（模块见 `libs/speech/speech-barrier.ts`）。
 * 按 AudioContext 存 —— M 的 abort 会**立刻**放掉旧 item，光靠旧 clock 的 `drained` 拦不住下一轮。
 */
const speechBarrier = createSpeechBarrier()
/**
 * 还在跑的流式 clock（含**已从 M / `currentSession` 移除**、仍在淡出的旧轮）——
 * 静音/卸载要能遍历它把残留取消尾巴收掉，否则旧句尾巴会照原样播完（sol-medium §2）。
 */
const streamClocks = createStreamClockRegistry()
/** 取消理由别统一丢成 'aborted'：`new-message` 要让旧句淡出，静音/卸载/其它仍是立即停。 */
function abortReason(signal: AbortSignal): string {
  const reason = (signal as AbortSignal & { reason?: unknown }).reason
  return typeof reason === 'string' && reason ? reason : 'aborted'
}
/** 段节点接出去：主输出 + 分析器 + 口型图（老路径与流式路径共用一份连接）。
 *  `output` 是流式时钟插进来的**段增益节点** —— 三条支路都从它接，别留 source 直连的旁路。 */
function connectPlaybackSource(source: AudioBufferSourceNode, output: AudioNode = source) {
  if (!audioContext)
    return
  currentAudioSource.value = source
  output.connect(audioContext.destination)
  if (audioAnalyser.value)
    output.connect(audioAnalyser.value)
  if (lipSyncNode.value)
    output.connect(lipSyncNode.value)
}

async function playFunction(item: Parameters<Parameters<typeof createPlaybackManager<AudioBuffer>>[0]['play']>[0], signal: AbortSignal): Promise<void> {
  const clock = (item as StreamPlaybackItem).clock
  if (!audioContext || (!item.audio && !clock))
    return

  // Ensure audio context is resumed (browsers suspend it by default until user interaction)
  if (audioContext.state === 'suspended') {
    try {
      await audioContext.resume()
    }
    catch {
      return
    }
  }

  if (stageModelRenderer.value === 'live2d' && !lipSyncStarted.value) {
    // NOTICE: Playback can be triggered by non-chat speech intents, so initialize
    // the wLipSync graph here before connecting the AudioBufferSourceNode.
    setupAnalyser()
    await setupLipSync()
  }

  // 流式 turn（§4.3）：这个 item 只表示"这一轮的所有权"；段已经排在时钟里，
  // 开门之后由它起播，`play` 等"输入关闭 + 已收音频播完"，abort 立即停当前与未来的段。
  if (clock) {
    if (signal.aborted) {
      clock.stop(abortReason(signal))
      return
    }
    // 上下文恢复（设备切换/被浏览器挂起后回来）时由**持有这个 item 的人**叫醒时钟 ——
    // 会话本身可能已经被 `onDone` 清掉了（尾音还在播），那时 `currentSession?.wake?.()` 找不到它。
    const ctx = audioContext
    const onStateChange = () => {
      if (ctx.state === 'running')
        clock.wake()
      else
        clock.abortCancelledTails() // 挂起/关闭：取消淡出走不动了，尾巴立刻收掉
    }
    if (typeof ctx.addEventListener === 'function')
      ctx.addEventListener('statechange', onStateChange)
    const onAbort = () => clock.stop(abortReason(signal))
    signal.addEventListener('abort', onAbort, { once: true })
    streamClocks.add(clock) // 记在册上：静音/卸载够得到它（哪怕 M 已经放掉这个 item）
    try {
      // 首段要等上一轮淡出走完（屏障由 `onPlaybackStopped` 记下）；没人取消过就是 0。
      clock.arm(barrierArmValue(speechBarrier, ctx))
      await clock.drained
    }
    finally {
      signal.removeEventListener('abort', onAbort)
      if (typeof ctx.removeEventListener === 'function')
        ctx.removeEventListener('statechange', onStateChange)
      streamClocks.remove(clock)
    }
    return
  }

  const source = audioContext.createBufferSource()
  currentAudioSource.value = source
  source.buffer = item.audio!

  source.connect(audioContext.destination)
  if (audioAnalyser.value)
    source.connect(audioAnalyser.value)
  if (lipSyncNode.value)
    source.connect(lipSyncNode.value)

  return new Promise<void>((resolve) => {
    let settled = false
    const resolveOnce = () => {
      if (settled)
        return
      settled = true
      resolve()
    }

    const stopPlayback = () => {
      try {
        source.stop()
        source.disconnect()
      }
      catch {}
      if (currentAudioSource.value === source)
        currentAudioSource.value = undefined
      resolveOnce()
    }

    if (signal.aborted) {
      stopPlayback()
      return
    }

    signal.addEventListener('abort', stopPlayback, { once: true })
    source.onended = () => {
      signal.removeEventListener('abort', stopPlayback)
      stopPlayback()
    }

    try {
      source.start(0)
    }
    catch {
      stopPlayback()
    }
  })
}

const playbackManager = createPlaybackManager<AudioBuffer>({
  play: playFunction,
  maxVoices: 1,
  // 2（而不是 1）：同一个人连着两句时，第二句走到「全局 overflow」= queue，
  // 排在前一句后面播完；若留 1，会先撞 owner-overflow ⇒ 默认 steal-oldest
  // ⇒ 正在播的那句被直接掐掉（就是「截断」）。maxVoices 仍是 1 ⇒ 永不重叠。
  maxVoicesPerOwner: 2,
  overflowPolicy: 'queue',
  ownerOverflowPolicy: 'steal-oldest',
})

// ── 整轮播放门闩（阿尔比恩自加，2026-09-25）──
// 为什么不能用 nowSpeaking：它是「单段播放项」状态，两段 TTS 之间
// 的空隙会翻 false；且她还在生成时它本来就是 false。
// 实验：第二句进来会让 onBeforeMessageComposed 调 stopAll('new-message')
// 掐断上一轮正在播的语音。所以门闩要从生成开始就持有，
// 到 TTS 结束且最后一个播放项也结束才释放。
let outputTurnId: string | null = null
let ttsFinished = false
let outputGateTimer: ReturnType<typeof setTimeout> | undefined
/** 段与段之间若超过这么久没有下一段，就闭嘴（0.10s 的有意停顿保持张嘴，不抖）。 */
let segmentSilenceTimer: ReturnType<typeof setTimeout> | undefined
const SEGMENT_SILENCE_KEEP_MOUTH_MS = 200

function clearOutputGate() {
  if (outputGateTimer)
    clearTimeout(outputGateTimer)
  outputGateTimer = undefined
  outputTurnId = null
  ttsFinished = false
  assistantTurnBusy.value = false
}

function releaseOutputWhenDrained(turnId: string) {
  // playbackManager 先发 end，再同步启动队列中的下一项；
  // 到微任务时检查，才能避开两段之间短暂的 nowSpeaking=false。
  queueMicrotask(() => {
    if (outputTurnId === turnId && ttsFinished && !nowSpeaking.value && !streamTurn.pending)
      clearOutputGate()
  })
}

function markTtsFinished(turnId: string) {
  if (outputTurnId !== turnId)
    return
  ttsFinished = true
  releaseOutputWhenDrained(turnId)
}

function beginOutputTurn(turnId: string) {
  clearOutputGate()
  outputTurnId = turnId
  assistantTurnBusy.value = true
  outputGateTimer = setTimeout(() => {
    if (outputTurnId !== turnId)
      return
    console.warn('[Speech Pipeline] output gate timed out', { turnId })
    clearOutputGate()
  }, 180_000)
}

const outputGateCleanups = [
  playbackManager.onEnd(() => {
    if (outputTurnId)
      releaseOutputWhenDrained(outputTurnId)
  }),
  playbackManager.onInterrupt(() => {
    if (outputTurnId)
      releaseOutputWhenDrained(outputTurnId)
  }),
  playbackManager.onReject(() => {
    if (outputTurnId)
      releaseOutputWhenDrained(outputTurnId)
  }),
]

/**
 * Classifies chat auto-TTS voice usage before forwarding analytics to the server.
 */
function resolveStageVoiceType(): 'official_selected' | 'custom_configured' {
  return activeSpeechProvider.value === OFFICIAL_SPEECH_PROVIDER_ID || activeSpeechProvider.value === OFFICIAL_SPEECH_STREAMING_PROVIDER_ID ? 'official_selected' : 'custom_configured'
}

const speechPipeline = createSpeechPipeline<AudioBuffer>({
  // 一句一块：只在句号/问号/叹号处切。
  // 逗号不再切（minimum/maximum 拉满）、头两块也不再抢跑（boost=0）——
  // 每块就是一整句 ⇒ 合成耗时不再长于音频时长，句间空档与「短句抢着说」一起消失。
  segmenter: (tokens, meta) => createTtsSegmentStream(tokens, meta, {
    boost: 0,
    minimumWords: Number.MAX_SAFE_INTEGER,
    maximumWords: Number.MAX_SAFE_INTEGER,
  }),
  // 并发 2：下一句在前一句**播放**时就把翻译+合成做完 ⇒ 句间空档消失。
  // （空档的根源是「翻译 1.5~5s + 合成 2~3s」串在播放后面跑。）
  // 配合上面的 maxVoicesPerOwner: 2 —— 预合成好的音频只会排队，不会掐断正在播的那句。
  ttsMaxConcurrent: 1,
  tts: async (request, signal) => {
    if (signal.aborted)
      return null

    if (speechMuted.value)
      return null

    if (activeSpeechProvider.value === 'speech-noop')
      return null

    if (!activeSpeechProvider.value)
      return null

    // Streaming provider must NEVER reach this per-segment callback. The
    // streaming code path opens its own ws at `onBeforeMessageComposed`
    // and bypasses speech-pipeline entirely. If we got here while the
    // streaming provider is active, the open path failed (most often:
    // voice catalog hadn't finished loading when the user sent the
    // message). The old fallback would silently re-open a fresh ws per
    // segment — exactly the behavior the refactor is meant to delete.
    // Codex review MEDIUM #3: refuse loudly instead.
    if (resolveSpeechTransport(activeSpeechProvider.value) === 'bidirectional-ws') {
      console.warn('[Speech Pipeline] bidirectional-ws provider reached per-segment fallback', {
        reason: 'streaming session was not opened at intent start (voice unset?)',
        provider: activeSpeechProvider.value,
        segment: request.text?.slice(0, 40),
      })
      return null
    }

    const provider = await providersStore.getProviderInstance(activeSpeechProvider.value) as SpeechProviderWithExtraOptions<string, UnElevenLabsOptions>
    if (!provider) {
      console.error('Failed to initialize speech provider')
      return null
    }

    if (!request.text && !request.special)
      return null

    const providerConfig = providerStore.getProviderConfig(activeSpeechProvider.value)

    // For OpenAI Compatible providers, always use provider config for model and voice
    // since these are manually configured in provider settings
    let model = activeSpeechModel.value
    let voice = activeSpeechVoice.value

    if (activeSpeechProvider.value === 'openai-compatible-audio-speech') {
      // Always prefer provider config for OpenAI Compatible (user configured it there)
      if (providerConfig?.model) {
        model = providerConfig.model as string
      }
      else {
        // Fallback to default if not in provider config
        model = 'tts-1'
        console.warn('[Speech Pipeline] OpenAI Compatible: No model in provider config, using default', { providerConfig })
      }

      if (providerConfig?.voice) {
        voice = {
          id: providerConfig.voice as string,
          name: providerConfig.voice as string,
          description: providerConfig.voice as string,
          previewURL: '',
          languages: [{ code: 'en', title: 'English' }],
          provider: activeSpeechProvider.value,
          gender: 'neutral',
        }
      }
      else {
        // Fallback to default if not in provider config
        voice = {
          id: 'alloy',
          name: 'alloy',
          description: 'alloy',
          previewURL: '',
          languages: [{ code: 'en', title: 'English' }],
          provider: activeSpeechProvider.value,
          gender: 'neutral',
        }
        console.warn('[Speech Pipeline] OpenAI Compatible: No voice in provider config, using default', { providerConfig })
      }
    }

    if (!model || !voice)
      return null

    try {
      const speechRequest = speechStore.resolveSpeechInput({
        text: request.text,
        voice,
        providerConfig: {
          ...providerConfig,
          pitch: ssmlEnabled.value ? pitch.value : undefined,
        },
        forceSSML: ssmlEnabled.value,
        supportsSSML: speechStore.supportsSSML,
      })

      // Non-streaming providers only: synth via REST. Streaming provider
      // was already early-returned above; it owns its own ws path opened
      // in `onBeforeMessageComposed`.
      const res = await speechStore.speech(
        provider,
        model,
        speechRequest.input,
        voice.id,
        speechRequest.providerConfig,
        {
          trigger: 'auto',
          source: 'chat_auto_tts',
          voice_type: resolveStageVoiceType(),
        },
      )

      if (signal.aborted || !res || res.byteLength === 0)
        return null

      const audioBuffer = await audioContext.decodeAudioData(res)
      return audioBuffer
    }
    catch (err) {
      // Surface the error with context. Pipeline still drops the segment
      // (returning null) so the conversation keeps going, but operators see
      // the failure in devtools instead of silent truncation. Streaming
      // failures (truncated session, network drop, billing rejection) now
      // produce visible diagnostic lines — see codex review item #6.
      if (!signal.aborted) {
        console.error('[Speech Pipeline] tts() failed', {
          provider: activeSpeechProvider.value,
          model,
          voice: voice?.id,
          error: err,
        })
      }
      return null
    }
  },
  playback: playbackManager,
})

initIOTracer()
useIOTraceBridge(speechPipeline)
void speechRuntimeStore.registerHost(speechPipeline)

speechPipeline.on('onSpecial', (segment) => {
  if (segment.special) {
    void playSpecialToken(segment.special, {
      turnId: segment.turnId,
      intentId: segment.intentId,
      streamId: segment.streamId,
    })
  }
})

speechPipeline.on('onTurnEnd', (turnId) => {
  streamingControl.completeTurn(turnId)
  markTtsFinished(turnId)
})

speechPipeline.on('onTurnCancel', ({ turnId }) => {
  streamingControl.cancelTurn(turnId)
  markTtsFinished(turnId)
})

function resetSpeakingState() {
  nowSpeaking.value = false
  mouthOpenSize.value = 0
}

// 双方括号标记（她仿写的 [[read notebook]]、旧聊天记录里的 [[LOOK:his]]）在文本进来时就削掉了：
// 聊天编排器和 spark 反应各削一次（core-agent 的 runtime/pet-tag-filter）。这里收到的 literal、
// 切句器的 item.text、v2 `display.span` 的原文都从削过的文本来，Stage 不再自己削。
// 2026-10-16 起壳里已经没有 [[LOOK]] 这条路（看东西走脑里的 look 工具）。

bindSpeakingStateToPlaybackManager(playbackManager, {
  setSpeaking: (speaking, item) => {
    // 流式 turn 的**置真**交给段级事件（`onSegmentStart`）：M 的 `onStart` 在
    // `playFunction` **之前**发，那时还在 resume、初始化口型、等首段 50ms lead ——
    // 提前置真会让嘴先动、还会误触发停麦（codex 复核指出）。清零照常走这里。
    // 判断**按 M 事件里的 item**，不按 `currentSession`（服务端先 `onDone` 时它已清空）。
    if (speaking && streamTurn.isStreamItem(item))
      return
    if (!speaking) {
      streamTurn.reset()
      if (segmentSilenceTimer) {
        clearTimeout(segmentSilenceTimer)
        segmentSilenceTimer = undefined
      }
      resetSpeakingState()
    }
    else {
      nowSpeaking.value = true
    }
  },
  onStart: ({ item }) => {
    if (streamTurn.onItemStart(item))
      return
    // NOTICE: postCaption and postPresent may throw errors if the BroadcastChannel is closed
    // (e.g., when navigating away from the page). We wrap these in try-catch to prevent
    // breaking playback when the channel is unavailable.
    const itemText = item.text.trim()
    // 本机 v2（P5）下字幕由 `display.span` 账本驱动（原文守恒、含无音频段），
    // 这条"播到才追加"的老路会让同一句被记两遍，所以 v2 生效时让它靠边站。
    if (!displayLedger.driven) {
      assistantCaption.value += ` ${itemText}`
      try {
        // 字幕显示"整段话"，只让 TTS 切句器决定刷新时机（原来每切一段就只显示那一段，
        // 所以看着被切得七零八落；改成 replace + 累积文本 ⇒ 屏幕上是完整一句句长出来的）。
        postCaption({ type: 'caption-assistant', text: assistantCaption.value.trim(), operation: 'replace' })
      }
      catch {
        // BroadcastChannel may be closed - don't break playback
      }
    }
    if (!displayLedger.driven) {
      try {
        postPresent({ type: 'assistant-append', text: itemText })
      }
      catch {
        // BroadcastChannel may be closed - don't break playback
      }
    }
  },
  onEnd: ({ item }) => {
    // 这一轮的流式 item 结束（进度上报在会话适配器里，跟着段级事件走，不在这里）。
    streamTurn.onItemEnd(item)
  },
})

function startLipSyncLoop() {
  if (lipSyncLoopId.value)
    return

  const tick = () => {
    if (!nowSpeaking.value || !live2dLipSync.value) {
      mouthOpenSize.value = 0
    }
    else {
      mouthOpenSize.value = live2dLipSync.value.getMouthOpen()
    }
    lipSyncLoopId.value = requestAnimationFrame(tick)
  }

  lipSyncLoopId.value = requestAnimationFrame(tick)
}

function stopLipSyncLoop() {
  if (lipSyncLoopId.value) {
    cancelAnimationFrame(lipSyncLoopId.value)
    lipSyncLoopId.value = undefined
  }

  mouthOpenSize.value = 0
}

function resetLive2dLipSync() {
  stopLipSyncLoop()

  try {
    lipSyncNode.value?.disconnect()
  }
  catch {

  }

  lipSyncNode.value = undefined
  live2dLipSync.value = undefined
  lipSyncStarted.value = false
}

function syncLipSyncLoop() {
  if (stageModelRenderer.value === 'live2d' && !props.paused && lipSyncStarted.value) {
    startLipSyncLoop()
    return
  }

  stopLipSyncLoop()
}

/**
 * spine38 的口型：它没有 wlipsync，直接读 `audioAnalyser` —— 这个分析器一直挂在每段音频的
 * 输出上（`connectPlaybackSource`），以前没人读。电平只在这里算，平滑和骨缩放归 Model.vue。
 */
let spineMouthBuf = new Uint8Array(0)
let spineMouthLoopId: number | undefined
let spineMouthWasSpeaking = false

function startSpineMouthLoop() {
  if (spineMouthLoopId)
    return
  const tick = () => {
    // 作者语音（摸头/点身体时的 wav）也在自己的 rAF 里写同一个电平：这里只在真的说话时推，
    // 停下那一帧推一次 0 收口，之后不再推，静音交给 Model 的 250ms 看门狗。
    const push = mouthPushFor(nowSpeaking.value, spineMouthWasSpeaking)
    if (push !== 'none')
      spine38SceneRef.value?.setMouthLevel(push === 'level' ? readSpineMouthLevel() : 0)
    spineMouthWasSpeaking = nowSpeaking.value
    spineMouthLoopId = requestAnimationFrame(tick)
  }
  spineMouthLoopId = requestAnimationFrame(tick)
}

function stopSpineMouthLoop() {
  if (spineMouthLoopId) {
    cancelAnimationFrame(spineMouthLoopId)
    spineMouthLoopId = undefined
  }
  spineMouthWasSpeaking = false
  spine38SceneRef.value?.setMouthLevel(0)
}

function readSpineMouthLevel(): number {
  const analyser = audioAnalyser.value
  if (!analyser)
    return 0
  if (spineMouthBuf.length !== analyser.fftSize)
    spineMouthBuf = new Uint8Array(analyser.fftSize)
  const buf = spineMouthBuf
  analyser.getByteTimeDomainData(buf)
  let sum = 0
  for (const v of buf) {
    const d = (v - 128) / 128
    sum += d * d
  }
  return mouthLevelFromRms(Math.sqrt(sum / buf.length))
}

function syncSpineMouthLoop() {
  if (stageModelRenderer.value === 'spine38' && !props.paused)
    startSpineMouthLoop()
  else
    stopSpineMouthLoop()
}

async function setupLipSync() {
  if (stageModelRenderer.value !== 'live2d') {
    resetLive2dLipSync()
    return
  }

  if (lipSyncStarted.value)
    return

  try {
    const lipSync = await createLive2DLipSync(audioContext, wlipsyncProfile as Profile, live2dLipSyncOptions)
    live2dLipSync.value = lipSync
    lipSyncNode.value = lipSync.node
    await audioContext.resume()
    lipSyncStarted.value = true
    syncLipSyncLoop()
  }
  catch (error) {
    resetLive2dLipSync()
    console.error('Failed to setup Live2D lip sync', error)
  }
}

function setupAnalyser() {
  if (!audioAnalyser.value) {
    audioAnalyser.value = audioContext.createAnalyser()
  }
}

// One TTS session per LLM intent. The active provider determines which
// adapter `createStageTtsSession` returns: the segmenter-based adapter for
// every non-streaming provider, or the bidirectional WebSocket adapter
// for the official streaming provider. Stage.vue intentionally does NOT
// branch on provider id anywhere below — the factory is the single
// decision point. See `packages/stage-ui/src/libs/speech/tts-session.ts`.
let currentSession: StageTtsSession | null = null

function stopSpeechOutput(reason: string) {
  captionFallback.close()
  currentSession?.cancel(reason)
  currentSession = null
  speechPipeline.stopAll(reason)
  playbackManager.stopAll(reason)
  // 上面三个入口都**不持有**已经从 M / currentSession 移除的旧轮：它们的淡出尾巴得由册子收
  // （`abortCancelledTails()` 对正常播放的 clock 是 no-op）。sol-medium 复核 §2。
  streamClocks.sweepCancelledTails()
  clearOutputGate()
  resetAssistantSpeechSurface(reason)
}

/**
 * Resolves the official streaming TTS model for the current Stage session.
 */
function resolveStreamingSessionModel(): string | null {
  const activeModel = activeSpeechModel.value as string | undefined
  const sessionModel = activeModel?.includes('/') ? activeModel : getDefaultStreamingModel()
  if (!sessionModel?.includes('/'))
    return null
  return sessionModel
}

function buildStreamingSnapshot(turnId: string): StreamingSessionSnapshot | null {
  if (speechMuted.value)
    return null

  // 阿尔比恩的本机流式通道（会话参数见 albion-stage-bridge）
  if (isAlbionStreamTtsProvider(activeSpeechProvider.value)) {
    return buildAlbionStreamingSnapshot({
      voiceType: resolveStageVoiceType(),
      ownerId: activeCardId.value,
      onImmediateSpecial: special => playSpecialToken(special, { turnId }),
    })
  }

  // Snapshotted once per session, so a mid-session provider/voice swap
  // does not corrupt an in-flight session — the watcher below detects
  // changes and tears down explicitly. Returns `null` when streaming
  // can't be opened (no voice picked, no audioContext, no model);
  // `createStageTtsSession` falls back to the segmenter adapter in that
  // case, which is the right behaviour for the rest of the providers too.
  const voiceId = activeSpeechVoice.value?.id
  if (!voiceId)
    return null
  // Resolve the concrete streaming model id. The active speech model is only
  // valid here when it carries the `<backend>/<api_resource_id>` shape the ws
  // upstream expects — the HTTP TTS `auto` alias (and an empty selection after
  // a provider switch) must NOT reach the bridge, so fall back to the
  // server-curated default instead of a hardcoded id. Returns null (segmenter
  // fallback) when neither resolves, rather than guessing a resource id.
  const sessionModel = resolveStreamingSessionModel()
  if (!sessionModel)
    return null
  const apiResourceId = sessionModel.split('/', 2)[1]
  // TTS 2.0 / ICL 2.0 ship subtitles asynchronously relative to audio
  // (per the wire spec), so chunk-on-sentence-end would drop frames.
  // Buffer the entire session and decode at session.finished instead.
  const bufferEntireSession = apiResourceId.startsWith('seed-tts-2.0') || apiResourceId.startsWith('seed-icl-2.0')
  return {
    model: sessionModel,
    voice: voiceId,
    voiceType: resolveStageVoiceType(),
    bufferEntireSession,
    extraBody: {
      api_resource_id: apiResourceId,
      audio: { sample_rate: 24000, bit_rate: 64000 },
    },
    ownerId: activeCardId.value,
    onImmediateSpecial: special => playSpecialToken(special, { turnId }),
  }
}

function resolveSpeechTransport(providerId: string | null | undefined): SpeechTransport | undefined {
  if (!providerId)
    return undefined
  // Read straight from the unified ProviderDefinition registry — keeps the
  // factory transport-agnostic and lets a new provider opt into streaming
  // by setting `capabilities.speech.transport: 'bidirectional-ws'` in its
  // own `defineProvider` call (no Stage / factory edits needed).
  return getDefinedProvider(providerId)?.capabilities?.speech?.transport
}

function emitFallbackCaption(emission: { caption: string, present: string } | null) {
  if (!emission)
    return
  assistantCaption.value = emission.caption
  try {
    postCaption({ type: 'caption-assistant', text: emission.caption, operation: 'replace' })
  }
  catch {
    // BroadcastChannel may be closed - don't break playback
  }
  if (!emission.present)
    return
  try {
    postPresent({ type: 'assistant-append', text: emission.present })
  }
  catch {
    // BroadcastChannel may be closed - don't break playback
  }
}

/** 这一轮没有可用语音会话：账本推过的部分展示通道不重复补。 */
function failCaptionFallback(turnId: string) {
  emitFallbackCaption(captionFallback.fail(turnId, displayLedger.text()))
}

function applyDisplaySpan(span: StreamingDisplaySpan) {
  // 兜底已经用整轮原文接管字幕，会话残留的 span 不许再 replace 回更短的文本
  if (captionFallback.active)
    return
  const text = displayLedger.add(span)
  const fresh = displayLedger.takeNew()
  if (!text)
    return
  assistantCaption.value = text
  try {
    postCaption({ type: 'caption-assistant', text, operation: 'replace' })
  }
  catch {
    // BroadcastChannel may be closed - don't break playback
  }
  if (!fresh)
    return
  // 展示通道也要吃到无音频段/失败段的原文（复核第 3 条）：老的 postPresent 只在播放
  // onStart 里追加，凡是没音频的段（心声、合成失败）就永远进不了这条通道。
  try {
    postPresent({ type: 'assistant-append', text: fresh })
  }
  catch {
    // BroadcastChannel may be closed - don't break playback
  }
}

function openTtsSession(turnId: string): StageTtsSession {
  // A session must only clear the module-level `currentSession` if it IS that session. The previous
  // code cleared it whenever any `stream-` session completed, which is unsafe once sessions exist that
  // are not assigned to `currentSession` (e.g. one-off read-aloud sessions): one of those finishing
  // would null a still-active chat session and drop the rest of the reply. Capture the session and
  // compare identity; the `stream-` guard is preserved so segmenter sessions still don't self-clear.
  let session: StageTtsSession | null = null
  // 新一轮会话：字幕账本清零（本机 v2 的 span 只属于这一轮）
  displayLedger.clear()
  const clearIfActive = () => {
    if (session && currentSession === session && session.intentId.startsWith('stream-'))
      currentSession = null
  }
  session = createStageTtsSession<AudioBuffer>({
    transport: resolveSpeechTransport(activeSpeechProvider.value),
    streaming: () => buildStreamingSnapshot(turnId),
    audioContext,
    playbackManager,
    openIntent: opts => speechRuntimeStore.openIntent(opts),
    // §4.3：流式 turn 的段节点由时钟建，连接搬到这里（口型图必须先就绪 —— 段在
    // `play()` 开门之后才建）。暂停时"段开始"的通知会压后，恢复后由宿主叫 `wake()`。
    playbackConnect: connectPlaybackSource,
    // 取消（`new-message`）后旧句会淡出 100ms；把淡出终点写进屏障，下一轮首段不会压住它。
    onPlaybackStopped: bindFadeBarrier(speechBarrier, audioContext),
    // 旧轮**自然说完**时也要写屏障（此前只有取消才写）：否则新轮的音频一就绪就紧贴旧尾起播，
    // 听感就是"抢句"（sol-high 方案 §二.C；真机 16:13 的 100/180/100ms 接续）。
    onPlaybackNaturalEnd: bindNaturalEndBarrier(speechBarrier, audioContext),
    isPlayable: () => audioContext?.state === 'running',
    intentOptions: () => ({
      turnId,
      ownerId: activeCardId.value,
      priority: 'normal',
      behavior: 'queue',
    }),
    hooks: {
      onError: (err) => {
        console.error('[Speech Pipeline] streaming session error', {
          provider: activeSpeechProvider.value,
          model: activeSpeechModel.value,
          error: err,
        })
        // Drop the failed session so no further audio is queued, but let the
        // playback manager keep draining already-queued audio and emit its own
        // terminal events. Calling resetSpeakingState() here would force the
        // mouth shut while audio is still playing.
        clearIfActive()
        // 按 turnId 归属：旧一轮晚到的 onError 带旧 turnId，fail 里对不上就忽略
        failCaptionFallback(turnId)
      },
      /**
       * 单个音频段解码失败（P6 限流可能砍在子段边界，字节也可能残缺）：**只丢这一句**。
       * 绝不能在这里 `clearIfActive()` —— 那会让后续段不再接收、`playback.ack` 也不再上报，
       * S 侧拿不到进度，每个单元都会等到超时被砍，整轮直接哑掉（codex P6 复核指出）。
       */
      onSegmentError: (err) => {
        console.warn('[Speech Pipeline] segment dropped', { error: err })
      },
      /**
       * 段级播放事件（§4.3）：M 的 `onStart/onEnd` 是 **turn 级**（起播时一次、整轮只报一次），
       * 拿它当"在说话"会在段与段之间一直张着嘴。真正的说话状态接这里。
       */
      onSegmentStart: () => {
        if (segmentSilenceTimer) {
          clearTimeout(segmentSilenceTimer)
          segmentSilenceTimer = undefined
        }
        nowSpeaking.value = true
      },
      /**
       * 段**结束**不立刻闭嘴：相邻段之间只有 0.10s 有意停顿，段段闭嘴会抖。
       * 但也不能只等 `onUnderrun` —— 它要等**下一段到达重新排程**才触发，等不到下一段
       * 就永远不闭嘴（codex 复核指出）。所以这里起一个 200ms 静音定时器：
       * 下一段开始就取消，超时则闭嘴；整轮结束/取消在 `setSpeaking(false)` 里清理。
       */
      onSegmentEnd: () => {
        if (segmentSilenceTimer)
          clearTimeout(segmentSilenceTimer)
        segmentSilenceTimer = setTimeout(() => {
          segmentSilenceTimer = undefined
          // 归属校验：只有"这一轮的流式 item 还在播"才闭嘴 —— 旧一轮的晚到定时器
          // 不许影响新一轮（按会话判断在这里同样会失效：`onDone` 已经把它清了）。
          if (streamTurn.pending)
            resetSpeakingState()
        }, SEGMENT_SILENCE_KEEP_MOUTH_MS)
      },
      onDone: () => {
        clearIfActive()
        markTtsFinished(turnId)
      },
      onDisplay: applyDisplaySpan,
    },
  })
  // P6：开局就把可接收提前量告诉协调器（方案 §3.4 的上限：15 秒 / 8 段，先到为准）。
  // 只有流式适配器有 `feedback`，segmenter 路径是 undefined，`?.` 自然跳过。
  // 此刻 ws 可能还没 open —— 管道会把帧排队，握手后补发。
  session.feedback?.('playback.credit', { seconds: 15, units: 8 })
  return session
}

watch(latestStopRequest, (request) => {
  if (!request)
    return

  stopSpeechOutput(request.reason)
}, { flush: 'sync' })

watch(speechMuted, (muted) => {
  if (muted)
    stopSpeechOutput('muted')
}, { immediate: true })

chatHookCleanups.push(onBeforeMessageComposed(async (_message, context) => {
  const stopRequest = latestStopRequest.value
  beginOutputTurn(context.turnId)
  captionFallback.begin(context.turnId)
  playbackManager.stopAll('new-message')
  resetAssistantSpeechSurface('new-message', { clearCaption: false })

  currentSession?.cancel('new-message')
  currentSession = null

  if (speechMuted.value || pushToTalkActive.value) {
    failCaptionFallback(context.turnId)
    markTtsFinished(context.turnId)
    return
  }

  try {
    setupAnalyser()
    await setupLipSync()
    if (latestStopRequest.value !== stopRequest || pushToTalkActive.value) {
      failCaptionFallback(context.turnId)
      markTtsFinished(context.turnId)
      return
    }
    currentSession = openTtsSession(context.turnId)
  }
  catch (error) {
    failCaptionFallback(context.turnId)
    clearOutputGate()
    throw error
  }
}))

chatHookCleanups.push(onBeforeSend(async () => {
  currentMotion.value = { group: EmotionThinkMotionName }
}))

chatHookCleanups.push(onTokenLiteral(async (literal) => {
  // 注意：空串也要喂给会话 —— 编排器削掉标记后可能发来空串，之前"空就不喂"会把
  // 切句器的节奏打乱：该念的整句没被送出去，于是没语音、连字幕也不出。
  emitFallbackCaption(captionFallback.push(literal))
  currentSession?.appendText(literal)
}))

chatHookCleanups.push(onTokenSpecial(async (special, context) => {
  // Muting speech must not suppress non-audio signals such as emotion, motion,
  // delay, or plugin calls that normally travel through the TTS session.
  if (speechMuted.value) {
    await playSpecialToken(special, { turnId: context.turnId })
    return
  }

  currentSession?.appendSpecial(special)
}))

chatHookCleanups.push(onStreamEnd(async () => {
  currentSession?.finishInput()
}))

chatHookCleanups.push(onAssistantResponseEnd(async () => {
  currentSession?.end()
  // Streaming sessions null-out via the onDone hook; segmenter sessions
  // stay around until the next `onBeforeMessageComposed` cancels them
  // (the segmenter pipeline's IntentHandle.end is idempotent and
  // ResourceMessages still arrive after end() — clearing here would
  // race with the pipeline's own cleanup). Keep the ref pointing at
  // the just-ended session; it costs nothing and the next message
  // replaces it.
  // const res = await embed({
  //   ...transformersProvider.embed('Xenova/nomic-embed-text-v1'),
  //   input: message,
  // })

  // await db.value?.execute(`INSERT INTO memory_test (vec) VALUES (${JSON.stringify(res.embedding)});`)
}))

// Mid-session provider / voice / model swaps would otherwise keep feeding
// tokens to the OLD adapter (segmenter for the new provider, or stale ws
// for the streaming provider). Cancel the active session so the next LLM
// token after the swap falls through `currentSession?.` cleanly (silent
// drop is acceptable — we don't try to fork-replay text into a new
// adapter with potentially different voice/model).
watch(
  [activeSpeechProvider, () => activeSpeechVoice.value?.id, activeSpeechModel],
  ([provider, voiceId, model], [prevProvider, prevVoiceId, prevModel]) => {
    if (!currentSession)
      return
    if (provider === prevProvider && voiceId === prevVoiceId && model === prevModel)
      return
    console.warn('[Speech Pipeline] provider/voice/model changed mid-session, tearing down', {
      provider,
      prevProvider,
      voiceId,
      prevVoiceId,
      model,
      prevModel,
    })
    currentSession.cancel('provider-or-voice-changed')
    currentSession = null
  },
)

// Resume audio context on first user interaction (browser requirement)
let audioContextResumed = false
function resumeAudioContextOnInteraction() {
  if (audioContextResumed || !audioContext)
    return
  audioContextResumed = true
  audioContext.resume().then(() => currentSession?.wake?.()).catch(() => {
    // Ignore errors - audio context will be resumed when needed
  })
}

// Add event listeners for user interaction
if (typeof window !== 'undefined') {
  const events = ['click', 'touchstart', 'keydown']
  events.forEach((event) => {
    window.addEventListener(event, resumeAudioContextOnInteraction, { once: true, passive: true })
  })
}

onMounted(async () => {
  await getDb() // stub for future update
})

watch([stageModelRenderer, () => props.paused], ([renderer]) => {
  if (renderer === 'godot') {
    componentState.value = 'mounted'
  }

  syncSpineMouthLoop()
  if (renderer !== 'live2d') {
    resetLive2dLipSync()
    return
  }

  syncLipSyncLoop()
}, { immediate: true })

function canvasElement() {
  if (stageModelRenderer.value === 'live2d')
    return live2dSceneRef.value?.canvasElement()

  else if (stageModelRenderer.value === 'vrm')
    return vrmViewerRef.value?.canvasElement()

  else if (stageModelRenderer.value === 'spine')
    return spineSceneRef.value?.canvasElement()

  else if (stageModelRenderer.value === 'spine38')
    return spine38SceneRef.value?.canvasElement()

  else if (stageModelRenderer.value === 'tachie')
    return tachieSceneRef.value?.canvasElement()

  else if (stageModelRenderer.value === 'mmd')
    return mmdSceneRef.value?.canvasElement()
}

function readRenderTargetRegionAtClientPoint(clientX: number, clientY: number, radius: number) {
  if (stageModelRenderer.value !== 'vrm')
    return null

  return vrmViewerRef.value?.readRenderTargetRegionAtClientPoint?.(clientX, clientY, radius) ?? null
}

async function captureCharacterFrame() {
  if (stageModelRenderer.value === 'live2d')
    return live2dSceneRef.value?.captureFrame()
  if (stageModelRenderer.value === 'vrm')
    return vrmViewerRef.value?.captureFrame()
  if (stageModelRenderer.value === 'spine')
    return spineSceneRef.value?.captureFrame()

  if (stageModelRenderer.value === 'spine38')
    return spine38SceneRef.value?.captureFrame()
  if (stageModelRenderer.value === 'tachie')
    return tachieSceneRef.value?.captureFrame()
  if (stageModelRenderer.value === 'mmd')
    return mmdSceneRef.value?.captureFrame()
}

async function captureFrame() {
  const charBlob = await captureCharacterFrame()

  if (!activeBackgroundUrl.value || !charBlob)
    return charBlob

  try {
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('2d')
    if (!ctx)
      return charBlob

    // Load background image
    const bgImg = new Image()
    bgImg.crossOrigin = 'anonymous'
    bgImg.src = activeBackgroundUrl.value
    await new Promise((resolve, reject) => {
      bgImg.onload = resolve
      bgImg.onerror = reject
    })

    // Load character frame
    const charImg = await createImageBitmap(charBlob)

    // Match canvas size to the captured frame (respects DPI/Render Scale)
    canvas.width = charImg.width
    canvas.height = charImg.height

    // Draw background with "cover" logic
    const scale = Math.max(canvas.width / bgImg.width, canvas.height / bgImg.height)
    const w = bgImg.width * scale
    const h = bgImg.height * scale
    const x = (canvas.width - w) / 2
    const y = (canvas.height - h) / 2

    ctx.drawImage(bgImg, x, y, w, h)

    // Draw character on top
    ctx.drawImage(charImg, 0, 0)

    return new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'))
  }
  catch (error) {
    console.error('[Stage] Failed to composite photo with background:', error)
    return charBlob // Fallback to character-only
  }
}

onUnmounted(() => {
  disposePlaybackStateHandler()
  resetLive2dLipSync()
  stopSpineMouthLoop()
  chatHookCleanups.forEach(dispose => dispose?.())
  viewUpdateCleanups.forEach(dispose => dispose?.())
  // Tear down any in-flight TTS session (segmenter or streaming) and
  // drain playback. Without this, a still-open streaming ws keeps
  // feeding sentences into a playbackManager whose listeners still
  // mutate component refs (caption / nowSpeaking). Codex review: HIGH
  // #1 + MEDIUM #5.
  currentSession?.cancel('unmount')
  currentSession = null
  playbackManager.stopAll('unmount')
  streamClocks.sweepCancelledTails() // 卸载：同样要把已经不可达的旧轮淡出尾巴收掉
  clearOutputGate()
  outputGateCleanups.forEach(dispose => dispose())
})

defineExpose({
  canvasElement,
  captureFrame,
  readRenderTargetRegionAtClientPoint,
})
</script>

<template>
  <div relative h-full w-full>
    <!-- Scene Background Layer -->
    <div
      v-if="activeBackgroundUrl"
      :class="[
        'absolute left-0 top-0 z-0 h-full w-full',
        'transition-opacity duration-500',
      ]"
      :style="{
        backgroundImage: `url(${activeBackgroundUrl})`,
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        backgroundRepeat: 'no-repeat',
      }"
    />

    <div relative h-full w-full>
      <Live2DScene
        v-if="stageModelRenderer === 'live2d' && showStage"
        ref="live2dSceneRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100"
        h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :model-id="stageModelSelected"
        :cursor-position="cursorPosition"
        :mouth-open-size="mouthOpenSize"
        :now-speaking="nowSpeaking"
        :paused="paused"
        :theme-colors-hue="themeColorsHue"
        :theme-colors-hue-dynamic="themeColorsHueDynamic"
        :live2d-shadow-enabled="live2dShadowEnabled"
        :live2d-max-fps="live2dMaxFps"
        :live2d-render-scale="live2dRenderScale"
        @error="handleStageRenderError"
      />
      <ThreeScene
        v-if="stageModelRenderer === 'vrm' && showStage"
        ref="vrmViewerRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100" h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :cursor-position="cursorPosition"
        :idle-animation="animations.idleLoop.toString()"
        :paused="paused"
        :show-axes="stageViewControlsEnabled"
        :enable-orbit-controls="props.enableOrbitControls"
        :audio-context="audioContext"
        :current-audio-source="currentAudioSource"
        @error="console.error"
        @vrm-interact="onVRMInteract"
      />
      <SpineScene
        v-if="stageModelRenderer === 'spine' && showStage"
        ref="spineSceneRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100"
        h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :model-id="stageModelSelected"
        :paused="paused"
        :premultiplied-alpha="spinePremultipliedAlpha"
        :default-mix-duration="spineDefaultMixDuration"
        :idle-animation-enabled="spineIdleAnimationEnabled"
        :max-fps="spineMaxFps"
        :render-scale="spineRenderScale"
      />
      <Spine38Scene
        v-if="stageModelRenderer === 'spine38' && showStage"
        ref="spine38SceneRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100"
        h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :model-id="stageModelSelected"
        :paused="paused"
        :premultiplied-alpha="spinePremultipliedAlpha"
        :idle-animation-enabled="spineIdleAnimationEnabled"
        :speaking="assistantTurnBusy"
      />
      <TachieScene
        v-if="stageModelRenderer === 'tachie' && showStage"
        ref="tachieSceneRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100"
        h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :model-id="stageModelSelected"
        :paused="paused"
        :theme-colors-hue="themeColorsHue"
        :theme-colors-hue-dynamic="themeColorsHueDynamic"
        @error="console.error"
      />
      <MMDScene
        v-if="stageModelRenderer === 'mmd' && showStage"
        ref="mmdSceneRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100"
        h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :model-id="stageModelSelected"
        :paused="paused"
        :cursor-position="cursorPosition"
        :enable-orbit-controls="props.enableOrbitControls"
        :audio-context="audioContext"
        :current-audio-source="currentAudioSource"
        @error="console.error"
      />
      <div
        v-if="stageModelRenderer === 'godot'"
        :class="[
          'h-full w-full',
          'flex items-center justify-center',
          'px-4 py-6',
        ]"
      >
        <div
          :class="[
            'w-96 max-w-full',
            'min-h-32',
            'flex items-center justify-center',
          ]"
        >
          <Callout label="Godot Stage (Experimental)">
            <p>Godot Stage (experimental) is running...</p>
          </Callout>
        </div>
      </div>

      <StageRenderError
        v-if="stageRenderError"
        :error="stageRenderError"
        renderer="Live2D"
        :model-id="stageModelSelected"
        @retry="retryStageRenderer"
      />
    </div>
  </div>
</template>
