import type { IntentHandle, IntentOptions, PlaybackItem } from '@proj-airi/pipelines-audio'

import type { StreamingDisplaySpan, StreamingTtsPipelineOptions } from './streaming-pipeline'
import type { StreamingPlaybackSegmentEvent, StreamingPlaybackStopEvent } from './streaming-playback'

import { createStreamingPlayback } from './streaming-playback'
import { createStreamingTtsPipeline } from './streaming-pipeline'

/**
 * Stage-level TTS session abstraction.
 *
 * Both the segmenter-based path (every non-streaming provider — feeds tokens
 * through `pipelines-audio`'s `IntentHandle` → segmenter → per-segment
 * `tts()` callback) and the bidirectional WebSocket path (the official
 * streaming provider — forwards raw tokens upstream and lets the model do
 * its own sentence splitting) implement this surface.
 *
 * Stage.vue holds exactly one `StageTtsSession` at any moment and forwards
 * every chat-orchestrator hook into it without branching on provider id.
 * The decision of which adapter to construct lives once in
 * {@link createStageTtsSession}.
 */
export interface StageTtsSession {
  /** Stable id for this session. Used by playback to scope cancellation. */
  readonly intentId: string
  /**
   * 流式路径专用（§4.3）：音频上下文恢复/设备切换完成后叫一声，
   * 让时钟补排待排的段并补报压后的"段开始"通知。segmenter 路径不给。
   */
  wake?: () => void
  /** Forward an LLM token. Adapter decides whether it's segmented or raw. */
  appendText: (text: string) => void
  /**
   * Forward a special token (emotion / delay marker). Segmenter adapter
   * queues it in lockstep with audio; streaming adapter fires it
   * immediately (no segmenter queue to ride; see
   * {@link createStageTtsSession} comments).
   */
  appendSpecial: (special: string) => void
  /** Signal end of LLM token stream. Caller still owes an `end()`. */
  finishInput: () => void
  /** Tear down on normal completion. */
  end: () => void
  /** Tear down on abort / new message / unmount. */
  cancel: (reason?: string) => void
  /**
   * v2 反馈上行走廊（P6）：`playback.credit` = 可接收提前量、`playback.ack` = 已播到第几秒。
   * 只有流式适配器有，segmenter 路径不给 —— 宿主一律 `?.` 调用。
   */
  feedback?: (event: 'playback.credit' | 'playback.ack', payload?: Record<string, unknown>) => void
}

/**
 * Minimal `IntentHandle` shape the segmenter adapter needs. Quoting the
 * full `IntentHandle` would drag in extra fields (`stream`, `priority`,
 * etc.) that are not part of the session protocol; this typed subset keeps
 * the adapter honest about what it actually depends on.
 */
type IntentHandleSubset = Pick<IntentHandle, 'intentId' | 'writeLiteral' | 'writeSpecial' | 'writeFlush' | 'end' | 'cancel'>

/**
 * Direct adapter from a `pipelines-audio` `IntentHandle` to
 * {@link StageTtsSession}. Pure passthrough — the segmenter pipeline already
 * owns segmentation, special-token queueing, and playback scheduling.
 */
function fromIntent(intent: IntentHandleSubset): StageTtsSession {
  return {
    intentId: intent.intentId,
    appendText: intent.writeLiteral,
    appendSpecial: intent.writeSpecial,
    finishInput: intent.writeFlush,
    end: intent.end,
    cancel: intent.cancel,
  }
}

/**
 * Per-session knobs the streaming adapter consumes. Snapshotted once at
 * session-open time so a mid-session provider/voice swap does not corrupt
 * an in-flight session — the hot-swap watcher in Stage.vue is responsible
 * for cancelling and re-opening.
 */
export interface StreamingSessionSnapshot {
  model: string
  voice: string
  /**
   * 本机流式服务地址（如 http://127.0.0.1:9885）。给了就走它，不给走编译期
   * 常量 SERVER_URL（airi 云）。2026-09-20 加：让 provider 自己带地址，
   * 不必为了一个本地服务去改 server.ts。
   */
  serverUrl?: string
  /** 对应 serverUrl 的令牌；本机服务不校验，随便给个非空值即可。 */
  token?: string
  voiceType: 'official_default' | 'official_selected' | 'custom_configured' | 'voice_pack' | 'unknown'
  bufferEntireSession: boolean
  extraBody: Record<string, unknown>
  /**
   * 音频返回格式。本机 v2（albion-stream）用 `wav`：Irodori 原样出 WAV，
   * 不额外编码，壳也能直接从帧头拿到真实样本数。省略时沿用管道的 `mp3` 默认。
   */
  responseFormat?: 'mp3' | 'opus' | 'aac' | 'flac' | 'pcm' | 'wav'
  /**
   * `ownerId` to stamp on each `PlaybackItem`. Mirrors the value the
   * segmenter-based intent uses (`activeCardId`) so playback manager
   * owner-quota policies treat both paths identically.
   */
  ownerId?: string
  /**
   * Called when the host wants a special token (emotion / delay marker)
   * dispatched. Streaming has no in-band queue to ride; this callback
   * fires immediately on `appendSpecial`. Without it the streaming adapter
   * would silently drop emotion/delay tokens. The host wires this to
   * something like `playSpecialToken` (Stage.vue's existing helper).
   */
  onImmediateSpecial: (special: string) => void
}

/**
 * Minimal `PlaybackManager` shape the streaming adapter writes to. Same
 * `intentId` is passed to `stopByIntent` on cancel and used as
 * `streamId`/`intentId` on every scheduled item, so cancellation reliably
 * stops every audio buffer this session emitted.
 */
export interface PlaybackManagerSubset<TAudio> {
  schedule: (item: PlaybackItem<TAudio>) => void
  stopByIntent: (intentId: string, reason: string) => void
}

/**
 * Internal helpers the streaming adapter calls out to. Lets the adapter
 * react to terminal events (error / done) by clearing whatever state the
 * host is holding — Stage.vue keeps a `currentSession` ref and needs to
 * null it when the underlying ws terminates on its own.
 */
export interface StreamingSessionHooks {
  /** Called once when the ws terminates with an error. */
  onError?: (err: Error) => void
  /**
   * 单个音频段解码失败（不是会话致命错误）：宿主只该丢这一句，**不该清会话** ——
   * 清了会连带停掉后续段的接收与 `playback.ack` 上报，整轮变哑。
   */
  onSegmentError?: (err: Error) => void
  /** Called once when the ws terminates (success or error follows). */
  onDone?: () => void
  /**
   * 每个 v2 `display.span` 回调一次（方案 §4.2）：字幕的显示资格由服务端给，
   * 和音频解码**无关**。宿主据此维护原文账本 —— 无音频段（`eligible: 'immediate'`）
   * 只有这条事件，不会有任何句子/音频帧。
   */
  onDisplay?: (span: StreamingDisplaySpan) => void
  /**
   * 段级播放事件（方案 §4.3）：接 `nowSpeaking`、口型和 `playback.ack`。
   * **不要**再拿 M 的 `onStart/onEnd` 当"在说话" —— 那是 turn 级（起播时整轮一次、只报一次），
   * 一个流式 turn 内会说完很多段。
   */
  onSegmentStart?: (event: StreamingPlaybackSegmentEvent) => void
  onSegmentEnd?: (event: StreamingPlaybackSegmentEvent & { completed: boolean }) => void
  onUnderrun?: (ms: number, event: StreamingPlaybackSegmentEvent) => void
}

export interface CreateStreamingSessionOptions<TAudio = AudioBuffer> {
  intentId: string
  snapshot: StreamingSessionSnapshot
  audioContext: BaseAudioContext
  playbackManager: PlaybackManagerSubset<TAudio>
  hooks?: StreamingSessionHooks
  /**
   * 把时钟排出的节点接到 destination / analyser / 口型图上（宿主原有连接）。
   * `output` 是时钟插进来的**段增益节点** —— 三条支路都从它接，别留 source 直连的旁路（淡出才管得住）。
   */
  playbackConnect?: (source: AudioBufferSourceNode, output: AudioNode) => void
  /** 取消淡出的终点回报（宿主据此设"下一轮不许早于此刻起播"的屏障）。 */
  onPlaybackStopped?: (info: StreamingPlaybackStopEvent) => void
  /**
   * 本轮**自然播完**（不是被打断）时回报最后一段的终点（AudioContext 时间轴秒）。
   * 宿主据此写"跨轮气口"屏障 —— 取消路径仍走 `onPlaybackStopped`，两条路分开记账
   * （sol-high 方案 §二.C：自然说完此前根本不写屏障，新轮紧贴旧尾起播）。
   */
  onPlaybackNaturalEnd?: (endAt: number) => void
  /** 上下文能不能出声（宿主给 `() => audioContext.state === 'running'`）。 */
  isPlayable?: () => boolean
  /**
   * Optional override for the underlying pipeline factory. Tests inject a
   * stub here; production wires the real `createStreamingTtsPipeline`.
   *
   * @default {@link createStreamingTtsPipeline}
   */
  pipelineFactory?: (options: StreamingTtsPipelineOptions) => ReturnType<typeof createStreamingTtsPipeline>
}

/**
 * Streaming adapter: opens ONE ws session for the whole intent and
 * schedules sentences into the playback manager as they arrive. Cancel
 * tells the pipeline to send `cancel` upstream AND drains any already-queued
 * playback items that belong to this intent.
 *
 * Use when:
 * - The active provider has the streaming surface enabled and a voice picked.
 *
 * Expects:
 * - `audioContext` is the same context the playback manager's `play`
 *   callback will use to attach the buffer source. Mismatched contexts will
 *   throw on decode.
 *
 * Returns:
 * - A {@link StageTtsSession} whose `appendSpecial` is intentionally
 *   immediate (no segmenter to align with); see {@link createStageTtsSession}.
 */
export function createStreamingTtsSession<TAudio = AudioBuffer>(
  options: CreateStreamingSessionOptions<TAudio>,
): StageTtsSession {
  const { intentId, snapshot, audioContext, playbackManager, hooks } = options
  const pipelineFactory = options.pipelineFactory ?? createStreamingTtsPipeline

  let sequence = 0
  let terminated = false
  let streamItemSubmitted = false

  // 一个流式 turn 的音频时钟（§4.3）：turn 内所有段都交给它按时间轴预排 ——
  // 这样相邻段之间只有那 0.10s 有意停顿，没有"等上一段 ended 再起下一段"的交接空档。
  const clock = createStreamingPlayback({
    audioContext: audioContext as AudioContext,
    connect: options.playbackConnect,
    isPlayable: options.isPlayable,
    onStopped: options.onPlaybackStopped,
    onNaturalEnd: options.onPlaybackNaturalEnd,
    onSegmentStart: event => hooks?.onSegmentStart?.(event),
    onSegmentEnd: (event) => {
      // P6 的 `playback.ack`：**段级**上报（一个 turn 现在只有一个 M item，只在 turn 结束时报的话
      // S 侧头 15 秒拿不到进度，直接触顶限流把整轮砍停）。口径直接取时钟：逐段按音频内进度累计，
      // `units` 只认正常播完的段（时钟是每会话一个，天然不会串轮）。
      const { seconds, units } = clock.progress()
      handle.feedback?.('playback.ack', { seconds: Number(seconds.toFixed(3)), units })
      hooks?.onSegmentEnd?.(event)
    },
    onUnderrun: (ms, event) => hooks?.onUnderrun?.(ms, event),
  })

  /**
   * 一个 turn 只提交**一个** M item：M 继续管所有权、排队和抢占（`maxVoices`、`stopByIntent`），
   * turn 内部的段交给时钟。`kind`/`clock` 是给宿主 `play()` 认领的（`PlaybackItem` 上没有这两个字段，
   * 但 M 只当它是普通占位 item，不碰 `audio`）。
   */
  function submitStreamItem() {
    if (streamItemSubmitted)
      return
    streamItemSubmitted = true
    playbackManager.schedule({
      id: `${intentId}-stream`,
      streamId: intentId,
      intentId,
      segmentId: `${intentId}-stream`,
      sequence: sequence++,
      ownerId: snapshot.ownerId,
      priority: 0,
      text: '',
      special: null,
      audio: undefined,
      kind: 'stream',
      clock,
      createdAt: Date.now(),
    } as unknown as PlaybackItem<TAudio>)
  }

  const handle = pipelineFactory({
    model: snapshot.model,
    voice: snapshot.voice,
    serverUrl: snapshot.serverUrl,
    token: snapshot.token,
    ttsVoiceType: snapshot.voiceType,
    audioContext,
    bufferEntireSession: snapshot.bufferEntireSession,
    responseFormat: snapshot.responseFormat,
    extraBody: snapshot.extraBody,
    onDisplay: (span) => {
      if (terminated)
        return
      hooks?.onDisplay?.(span)
    },
    onSentence: ({ index, text, audio, identity }) => {
      if (terminated)
        return
      void text
      // 段就绪 → 交给时钟排队；**首个段才提交** M item（纯无音频 turn 不占槽）。
      // `unitId` 用**服务端的句组身份**（`unit_id[:sub_index]`），不用本地 index ——
      // 否则"服务端渲染了几个单元 / 客户端播了几段"永远对不上号，丢段/合并无法归因
      // （sol-high 方案 §三）。服务端没给身份时退回旧的本地编号，行为不变。
      const unitId = identity
        ? (identity.subIndex == null ? identity.unitId : `${identity.unitId}:${identity.subIndex}`)
        : `${intentId}-${index}`
      // 身份四字段原样带进 clock（`sessionId/generation` 不能在这里丢），另带 intent 供跨轮区分。
      clock.push({
        unitId,
        subIndex: identity?.subIndex,
        identity: identity ? { ...identity } : undefined,
        intentId,
        buffer: audio,
      })
      submitStreamItem()
    },
    onError: (err) => {
      hooks?.onError?.(err)
    },
    // 直接透传（**不要**包一层）：包了就等于"永远提供了单段回调"，
    // 管道会一直走单段分支，没给这个回调的调用方再也收不到 `onError`（codex 复核）。
    onSegmentError: hooks?.onSegmentError,
    onDone: () => {
      terminated = true
      clock.closeInput()                             // 不会再有新段；已收的段播完才算这个 turn 结束
      hooks?.onDone?.()
    },
  })

  function cancel(reason?: string) {
    if (terminated) {
      // Pipeline already closed itself; still drain any playback items it
      // managed to queue before terminating.
      clock.stop(reason ?? 'session-already-terminated')
      playbackManager.stopByIntent(intentId, reason ?? 'session-already-terminated')
      return
    }
    terminated = true
    handle.cancel()
    clock.stop(reason ?? 'session-cancelled')
    playbackManager.stopByIntent(intentId, reason ?? 'session-cancelled')
  }

  return {
    intentId,
    appendText: handle.appendText,
    wake: clock.wake,
    // Streaming has no in-band queue to align audio with; fire the host's
    // immediate-special callback so emotion / delay tokens still reach the
    // queues. The perceptual mis-alignment vs audio is minor — codex
    // review and the segmenter parity comment in Stage.vue prior to the
    // refactor both accept it.
    appendSpecial: snapshot.onImmediateSpecial,
    finishInput: handle.finish,
    feedback: handle.feedback,
    end: () => {
      // `finish()` already drove session.finished → onDone; nothing extra
      // to do here. Kept as a method for protocol symmetry with the
      // segmenter adapter.
    },
    cancel,
  }
}

/**
 * Speech transport flavours the host knows how to drive. Mirrors the
 * `capabilities.speech.transport` enum on `ProviderDefinition`. Anything
 * the factory does not recognise is treated as `'rest'`.
 */
export type SpeechTransport = 'rest' | 'bidirectional-ws'

/**
 * Build context required by {@link createStageTtsSession}. Kept as a
 * single object so Stage.vue can construct it once per intent without
 * threading a dozen positional args.
 */
export interface StageTtsSessionContext<TAudio = AudioBuffer> {
  /**
   * Transport flavour of the active provider, read by the host from
   * `ProviderDefinition.capabilities.speech.transport`. `'rest'` (or any
   * other value) routes through the segmenter; `'bidirectional-ws'`
   * routes through the streaming WebSocket adapter.
   */
  transport: SpeechTransport | undefined
  /**
   * Snapshot of the streaming provider's settings. Used only when
   * `transport === 'bidirectional-ws'` and the snapshot is non-null with
   * a voice picked. Returning `null` is a graceful "not ready yet" signal
   * and falls back to the segmenter path.
   */
  streaming?: () => StreamingSessionSnapshot | null
  /** Host audio context. Required for the streaming path. */
  audioContext: BaseAudioContext | undefined
  /** Playback manager Stage uses for scheduling. */
  playbackManager: PlaybackManagerSubset<TAudio>
  /**
   * Factory for a fresh `IntentHandle` (segmenter path). Stage wires this
   * to `speechRuntimeStore.openIntent`.
   */
  openIntent: (options: IntentOptions) => IntentHandleSubset
  /**
   * Default intent options for the segmenter path. Stage.vue's existing
   * call passed `{ownerId, priority:'normal', behavior:'queue'}`; we keep
   * the same defaults here.
   */
  intentOptions: () => IntentOptions
  /** Lifecycle hooks shared by both paths. */
  hooks?: StreamingSessionHooks
  /**
   * 流式路径（§4.3）专用：把时钟排出的音频节点接到目的地 / 分析器 / 口型图上。
   * 段节点在 `play()` 开门（`clock.arm()`）之后才建，所以宿主可以先把它自己的
   * 口型图准备好再返回。
   */
  playbackConnect?: (source: AudioBufferSourceNode, output: AudioNode) => void
  /** 取消淡出的终点回报（宿主据此设"下一轮不许早于此刻起播"的屏障）。 */
  onPlaybackStopped?: (info: StreamingPlaybackStopEvent) => void
  /** 本轮**自然播完**的最后一段终点回报（写"跨轮气口"屏障用；取消不写这里）。 */
  onPlaybackNaturalEnd?: (endAt: number) => void
  /** 上下文能不能出声；暂停时"段开始"的通知会压后，恢复后由 `wake()` 补（见 `streaming-playback.ts`）。 */
  isPlayable?: () => boolean
}

/**
 * One decision point: streaming path or segmenter path? Returns a fully
 * wired {@link StageTtsSession} so Stage.vue's chat-orchestrator hooks
 * never branch on provider id again.
 *
 * Use when:
 * - `onBeforeMessageComposed` fires and Stage needs a fresh session for
 *   the next LLM intent.
 *
 * Expects:
 * - Caller has already cancelled / cleared any previous session ref.
 * - When `transport === 'bidirectional-ws'`, the snapshot's `voice` is
 *   a real voice id and `audioContext` is set; otherwise the factory
 *   silently falls back to the segmenter path (codex review MEDIUM #3
 *   noted this fallback should not silently re-enter the legacy
 *   per-segment path inside `tts()` — the segmenter adapter routes
 *   through the normal segmenter+tts callback, which is the intended
 *   behaviour for every REST provider).
 *
 * Returns:
 * - A `StageTtsSession`. Stage.vue stores it in a single `currentSession`
 *   ref and calls `appendText` / `appendSpecial` / `finishInput` / `end`
 *   / `cancel` on it from the hooks.
 */
export function createStageTtsSession<TAudio = AudioBuffer>(
  ctx: StageTtsSessionContext<TAudio>,
): StageTtsSession {
  const wantsStreaming = ctx.transport === 'bidirectional-ws'
  const snapshot = wantsStreaming ? ctx.streaming?.() ?? null : null
  const canStream = wantsStreaming
    && snapshot != null
    && snapshot.voice.length > 0
    && ctx.audioContext != null

  if (!canStream) {
    // Segmenter path: open the existing IntentHandle and adapt 1:1.
    return fromIntent(ctx.openIntent(ctx.intentOptions()))
  }

  const intentId = createStreamingIntentId()
  return createStreamingTtsSession<TAudio>({
    intentId,
    snapshot: snapshot!,
    audioContext: ctx.audioContext!,
    playbackManager: ctx.playbackManager,
    hooks: ctx.hooks,
    playbackConnect: ctx.playbackConnect,
    onPlaybackStopped: ctx.onPlaybackStopped,
    onPlaybackNaturalEnd: ctx.onPlaybackNaturalEnd,
    isPlayable: ctx.isPlayable,
  })
}

function createStreamingIntentId(): string {
  return `stream-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}
