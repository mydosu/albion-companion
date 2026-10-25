import { getAuthToken } from '../auth'
import { SERVER_URL } from '../server'

/**
 * One synthesized sentence emitted by the streaming pipeline. The pipeline
 * delivers these in arrival order; consumers schedule them into their
 * playback manager directly.
 */
export interface StreamingPipelineSentence {
  /** 0-based sentence index within the session. */
  index: number
  /** Sentence text from the upstream `sentence.*` payload, when available. */
  text: string
  /** Decoded audio. Same `AudioContext` is used for every sentence in the session. */
  audio: AudioBuffer
  /**
   * 服务端句组身份（v2 `sentence.*` 的 `session_id/generation/unit_id/sub_index`）。
   * 客户端此前只用本地 `index`，于是"服务端渲染了几个单元、客户端播了几段"对不上号，
   * 丢段/合并都无法归因（sol-high 方案 §三：身份要一路留到 clock，别被本地编号替换）。
   */
  identity?: StreamingSentenceIdentity
}

/** 服务端句组身份。`unitId` 是唯一必需项 —— 少了它就没法跟服务端账本对齐。 */
export interface StreamingSentenceIdentity {
  sessionId?: string
  generation?: number
  unitId: string
  subIndex?: number
}

export interface StreamingDisplaySpan {
  /** 单元 id（一个单元可能对应多句音频）。 */
  unitId: string
  /** 源账本单元 id（目前与 unitId 相同；分开是给 P6 的合并/拆分留位）。 */
  sourceUnitId: string
  /** 源文本下标区间 `[start, end)`，字幕账本按它对齐原文。 */
  sourceRange: [number, number]
  /** 该单元要显示的原文（含心声标点这类不可朗读字符）。 */
  text: string
  /**
   * 显示资格（方案 §4.2）：
   * - `audio`：首个音频帧已发出，可现在显示；
   * - `immediate`：无音频段（纯心声/纯符号），立刻可显示，且**不会有** sentence.* / 音频帧。
   */
  eligible: 'audio' | 'immediate'
}

export interface StreamingTtsPipelineEvents {
  /**
   * Fires once per synthesized sentence (TTS 1.0) or once per session
   * (TTS 2.0 / `bufferEntireSession: true`). Schedule the audio into your
   * playback manager from this callback.
   */
  onSentence?: (sentence: StreamingPipelineSentence) => void
  /**
   * Fires per v2 `display.span` — 字幕的显示资格由服务端给（方案 §4.2），
   * **不**依赖音频解码完成，所以无音频段也能按时显示原文。
   * Consumers own the ledger: 按 `sourceRange` 把 span 落进原文账本，别按句子序号对。
   */
  onDisplay?: (span: StreamingDisplaySpan) => void
  /**
   * Surfaced for any post-upgrade failure (server `error` event, ws close
   * without `session.finished`, decode failure). Consumers should treat the
   * session as terminated after this fires.
   */
  onError?: (err: Error) => void
  /**
   * 单个音频段解码失败（不是会话致命错误）。给了它，坏段就只走这条；没给才回退到
   * `onError`（保持既有调用方的行为不变）。
   */
  onSegmentError?: (err: Error) => void
  /**
   * Fires after the ws closes for any reason. Always paired with either
   * `onSentence` (success path) or `onError` (failure path) preceding it.
   */
  onDone?: () => void
}

export interface StreamingTtsPipelineOptions extends StreamingTtsPipelineEvents {
  /** Server URL override. Defaults to {@link SERVER_URL}. */
  serverUrl?: string
  /** Override the auth token (Bearer). Defaults to {@link getAuthToken}. */
  token?: string
  /** unspeech-routed model id, e.g. `volcengine/seed-tts-2.0`. */
  model: string
  /** Upstream voice / speaker id. */
  voice: string
  /** OpenAI-style format. Default `mp3`. `wav` 是本机 v2 默认（Irodori 原样出 WAV）。 */
  responseFormat?: 'mp3' | 'opus' | 'aac' | 'flac' | 'pcm' | 'wav'
  /** Backend-specific knobs forwarded as the `extra_body` of the `start` frame. */
  extraBody?: Record<string, unknown>
  /** Business trigger hint sent to server-side product analytics. */
  ttsTrigger?: 'auto' | 'manual'
  /** Low-cardinality source hint sent to server-side product analytics. */
  ttsSource?: 'chat_auto_tts' | 'manual_preview' | 'settings_test'
  /** Low-cardinality voice bucket sent to server-side product analytics. */
  ttsVoiceType?: 'official_default' | 'official_selected' | 'custom_configured' | 'voice_pack' | 'unknown'
  /**
   * Decoder context. The pipeline calls `decodeAudioData` on it for each
   * sentence (or once at session end in buffered mode). Reusing the page's
   * AudioContext is required so playback nodes that connect to its
   * destination see compatible sample rates.
   */
  audioContext: BaseAudioContext
  /**
   * When `true`, accumulate every binary chunk until `session.finished` and
   * emit ONE `onSentence`. Use for models where per-sentence audio boundaries
   * are not synchronously aligned with `sentence.end` events (Volcengine
   * Seed-TTS 2.0 ships subtitles asynchronously; chunking on `sentence.end`
   * would drop frames). Default `false` (chunk per sentence — correct for
   * Seed-TTS 1.0 / ICL 1.0 where `sentence.end` arrives in-band with audio).
   */
  bufferEntireSession?: boolean
}

export interface StreamingTtsPipelineHandle {
  /**
   * Forward a chunk of LLM-generated text to the in-flight TTS session.
   * The text is sent verbatim — no client-side segmentation. The upstream
   * model decides where to split sentences and how to pace prosody.
   *
   * Safe to call before the ws is open; frames are queued and flushed
   * after the handshake completes.
   */
  appendText: (text: string) => void
  /**
   * Signal end of the LLM text stream. The upstream emits any remaining
   * audio then `session.finished`; the pipeline closes the ws after.
   */
  finish: () => void
  /**
   * Abort the in-flight session. Sends `cancel` upstream (best-effort, no
   * ack wait per protocol v1) and closes the ws.
   */
  cancel: () => void
  /**
   * 上行一条 v2 反馈帧（`playback.credit` / `playback.ack`，方案 §4.1）。
   * 协调器拿"壳自己播到第几秒"做提前量限流 —— 它不拿"发出去多少"冒充"播了多少"。
   */
  feedback: (event: 'playback.credit' | 'playback.ack', payload?: Record<string, unknown>) => void
}

/**
 * Drives a single bidirectional streaming TTS session for one LLM intent.
 *
 * Use when:
 * - You have a streaming LLM output you want voiced without client-side
 *   sentence segmentation. The upstream model receives raw token chunks
 *   and decides where to split.
 *
 * Expects:
 * - Authenticated user (or `options.token`).
 * - `STREAMING_TTS_UPSTREAM` configKV configured server-side.
 *
 * Returns:
 * - A handle with `appendText` / `finish` / `cancel`. Side-effect: audio
 *   AudioBuffers are emitted via `options.onSentence` in arrival order.
 */
export function createStreamingTtsPipeline(options: StreamingTtsPipelineOptions): StreamingTtsPipelineHandle {
  const token = options.token ?? getAuthToken()
  if (!token) {
    const err = new Error('streaming-pipeline: not authenticated')
    queueMicrotask(() => {
      options.onError?.(err)
      options.onDone?.()
    })
    return noopHandle()
  }

  const wsUrl = toWebSocketUrl(options.serverUrl ?? SERVER_URL, '/api/v1/audio/speech/ws', token, {
    ttsTrigger: options.ttsTrigger ?? 'auto',
    ttsSource: options.ttsSource ?? 'chat_auto_tts',
    ttsVoiceType: options.ttsVoiceType ?? 'unknown',
  })
  const ws = new WebSocket(wsUrl)
  ws.binaryType = 'arraybuffer'

  let closed = false
  let sawSessionFinished = false
  /**
   * Queue for frames sent before the ws transitions to OPEN. Avoids
   * silently dropping early `appendText` calls (the caller doesn't know
   * the handshake hasn't completed yet).
   */
  const beforeOpenQueue: string[] = []
  /** Binary chunks accumulated since the last sentence flush. */
  let chunks: ArrayBuffer[] = []
  let chunkBytes = 0
  let sentenceIndex = 0
  /**
   * Promise chain for serialized `flushAccumulatedAsSentence` invocations.
   *
   * Each `handleControlFrame` runs via `void handleControlFrame(...)`, so
   * multiple control frames execute concurrently. Without serialization,
   * `session.finished`'s synchronous `chunkBytes === 0` check fires
   * immediately (the prior `sentence.end` already cleared the buffer
   * synchronously before its `await decodeAudioData`), terminating the
   * session before the last sentence's `decodeAudioData` resolves — its
   * `onSentence` then arrives after `terminated = true` in tts-session.ts
   * and gets dropped. Chaining all flushes through this single promise lets
   * `requestTerminate` await the tail before tearing down.
   */
  let pendingFlush: Promise<void> = Promise.resolve()
  let terminationRequested = false
  /**
   * FIFO of sentence texts seen via `sentence.start` events that haven't
   * been paired with a `sentence.end` yet. The protocol promises in-order
   * pairs, but a buggy upstream or re-ordered transport could send two
   * `sentence.start`s in a row; using a queue (instead of a single
   * `pendingSentenceText` variable) keeps each audio buffer labelled with
   * the right text instead of overwriting. Codex review MEDIUM #4.
   */
  const pendingSentenceTexts: string[] = []
  // 与文本队列**并行**消费，但占位纪律更严：**每个 `sentence.start` 都占一个槽**（允许空值），
  // **每个 `sentence.end` 消费一个**。只有每次都占位，start/end 的位置才对齐；否则无身份的 start
  // 不占位、后面有身份的 start 会向前错挂（A 被标成 B、B 反而丢身份），正好污染"服务端几个单元 /
  // 客户端几段"的取证（sol-medium 二审，已用真实管道复现）。
  const pendingSentenceIds: Array<StreamingSentenceIdentity | null> = []
  const bufferEntireSession = options.bufferEntireSession ?? false

  function safeSend(payload: string) {
    if (closed)
      return
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(payload)
      return
    }
    if (ws.readyState === WebSocket.CONNECTING) {
      beforeOpenQueue.push(payload)
    }
    // CLOSING/CLOSED — drop silently; caller will see onDone shortly.
  }

  async function flushAccumulatedAsSentence(
    sentenceChunks: ArrayBuffer[],
    sentenceChunkBytes: number,
    textOverride?: string,
    identity?: StreamingSentenceIdentity,
  ) {
    if (sentenceChunkBytes === 0)
      return
    const merged = new Uint8Array(sentenceChunkBytes)
    let offset = 0
    for (const c of sentenceChunks) {
      merged.set(new Uint8Array(c), offset)
      offset += c.byteLength
    }

    // Prefer the explicit override (the `sentence.end` payload's own text)
    // over the queued `sentence.start` text — `sentence.end` is the
    // authoritative pairing. The queue covers the case where `sentence.end`
    // arrives without a text field.
    const text = textOverride ?? pendingSentenceTexts.shift() ?? ''

    // 协议守卫：一组只接受**一份完整 WAV**。容器结构或长度不符时明确报单段错误，
    // 不把异常输入交给解码器自由发挥（解码器遇到拼接/截断字节的行为不可控）。
    // 只对 WAV 生效：默认格式是 mp3，整会话缓冲本来就是一个整包，都不套这条。
    if ((options.responseFormat ?? 'mp3') === 'wav' && !bufferEntireSession) {
      const problem = validateSingleWav(merged)
      if (problem) {
        const err = new Error(`sentence group is not a single valid WAV: ${problem}`)
        if (options.onSegmentError)
          options.onSegmentError(err)
        else
          options.onError?.(err)
        return
      }
    }

    try {
      // decodeAudioData needs a transferable ArrayBuffer; pass the buffer
      // backing `merged`. Clone to a fresh buffer so subsequent flushes do
      // not race on a buffer the decoder may detach.
      const audio = await options.audioContext.decodeAudioData(merged.buffer.slice(0))
      options.onSentence?.({ index: sentenceIndex++, text, audio, ...(identity ? { identity } : {}) })
    }
    catch (err) {
      // 单段解码失败：只丢这一句。宿主若把这类错误当会话级处理（清 currentSession），
      // 后续段的接收和 playback.ack 会一起停掉 —— 所以单独一条回调（codex P6 复核）。
      const err2 = err instanceof Error ? err : new Error(String(err))
      if (options.onSegmentError)
        options.onSegmentError(err2)
      else
        options.onError?.(err2)
    }
  }

  function enqueueFlush(textOverride?: string, identity?: StreamingSentenceIdentity): Promise<void> {
    const sentenceChunks = chunks
    const sentenceChunkBytes = chunkBytes
    chunks = []
    chunkBytes = 0

    // `.catch(() => {})` keeps a single decode failure from poisoning the
    // tail of the chain — failures already surface via `onError` inside
    // `flushAccumulatedAsSentence`.
    pendingFlush = pendingFlush.then(() => flushAccumulatedAsSentence(sentenceChunks, sentenceChunkBytes, textOverride, identity)).catch(() => {})
    return pendingFlush
  }

  async function requestTerminate(err: Error | null) {
    if (closed || terminationRequested)
      return
    terminationRequested = true
    // Wait for every queued flush (and its `onSentence` dispatch) to drain
    // before flipping `closed`. Without this await, late-resolving decodes
    // would land after `onDone` has already set `terminated = true` in the
    // consumer adapter and be dropped — that is the "last sentence missing"
    // symptom observed in the wild.
    await pendingFlush
    terminate(err)
  }

  ws.addEventListener('open', () => {
    const startFrame = {
      event: 'start',
      model: options.model,
      voice: options.voice,
      response_format: options.responseFormat ?? 'mp3',
      ...(options.extraBody ? { extra_body: options.extraBody } : {}),
    }
    ws.send(JSON.stringify(startFrame))
    for (const payload of beforeOpenQueue)
      ws.send(payload)
    beforeOpenQueue.length = 0
  })

  ws.addEventListener('message', (e) => {
    if (typeof e.data === 'string') {
      void handleControlFrame(e.data)
      return
    }
    // binary audio chunk
    if (e.data instanceof ArrayBuffer) {
      chunks.push(e.data)
      chunkBytes += e.data.byteLength
    }
  })

  async function handleControlFrame(raw: string) {
    let evt: { event?: string, payload?: Record<string, unknown>, text?: string, code?: string, message?: string }
    try {
      evt = JSON.parse(raw)
    }
    catch {
      return
    }

    switch (evt.event) {
      case 'sentence.start': {
        // Append to the queue. `sentence.end` consumes from the head, so
        // back-to-back `sentence.start`s (which shouldn't happen but
        // codex MEDIUM #4 noted the race) don't clobber each other.
        const text = readSentenceText(evt.payload)
        if (text != null)
          pendingSentenceTexts.push(text)
        // 身份队列：每次 start 都占位（解析不到身份就占空槽），保证与 end 一对一。
        pendingSentenceIds.push(readSentenceIdentity(evt.payload))
        break
      }
      case 'sentence.end': {
        if (bufferEntireSession)
          break
        // 先出队头再决定用谁的文本：`sentence.start` 入队的文本属于这一句，无论 end 自带没自带
        // 文本都要消费掉 —— 否则它在队列里越堆越多，下一个**缺文本**的 end 会取到陈旧的句子
        // （复核指出的旧边界：当前 v2 每个 end 都带文本，所以只在混合载荷下才暴露）。
        const own = readSentenceText(evt.payload)
        const queued = pendingSentenceTexts.shift()
        const text = own ?? queued ?? ''
        const queuedId = pendingSentenceIds.shift()
        const identity = readSentenceIdentity(evt.payload) ?? queuedId ?? undefined
        // Fire-and-forget into the serialized chain. We do NOT await here;
        // awaiting from the message handler does not block sibling handlers
        // (they run concurrently via `void handleControlFrame`), so an
        // await would only delay this handler's own return without
        // preventing the session.finished race. The chain itself is what
        // enforces ordering.
        void enqueueFlush(text, identity)
        break
      }
      case 'display.span': {
        // 方案 §4.2：显示资格走独立事件，和音频解码解耦。无音频段（eligible=immediate）
        // 只会有这条事件，不会跟任何 sentence.* / 二进制帧 —— 所以这里绝不能顺手 decode。
        const span = readDisplaySpan(evt.payload)
        if (span)
          options.onDisplay?.(span)
        break
      }
      case 'subtitle': {
        // TTS 2.0 emits subtitle events asynchronously (may arrive after
        // the next sentence's audio has already started). We surface the
        // text via the queue but do NOT flush audio here — buffered mode
        // flushes once at session.finished instead.
        const text = readSentenceText(evt.payload)
        if (text != null)
          pendingSentenceTexts.push(text)
        break
      }
      case 'session.finished': {
        sawSessionFinished = true
        void enqueueFlush()
        void requestTerminate(null)
        break
      }
      case 'error': {
        const code = evt.code ?? 'streaming_tts_error'
        const message = evt.message ?? code
        void requestTerminate(new Error(`${code}: ${message}`))
        break
      }
    }
  }

  ws.addEventListener('close', (ev) => {
    if (closed)
      return
    if (sawSessionFinished) {
      // Normal end after `session.finished`; the session.finished handler
      // already enqueued the tail flush and called requestTerminate. Just
      // make sure termination happens even if that path somehow didn't
      // (idempotent — requestTerminate guards against re-entry).
      void requestTerminate(null)
      return
    }
    // Closed before completion: surface as an error so callers don't
    // mistake truncated audio for a successful (short) sentence.
    const reason = ev.reason || `closed_${ev.code}`
    void requestTerminate(new Error(`streaming_tts_closed: ${reason}`))
  })

  ws.addEventListener('error', () => {
    // The `error` event carries no useful info per the WebSocket API; the
    // `close` event right after has the actual reason. Don't double-emit.
  })

  function terminate(err: Error | null) {
    if (closed)
      return
    closed = true

    const triggerCallbacks = () => {
      if (err != null)
        options.onError?.(err)
      options.onDone?.()
    }

    try {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        // NOTICE:
        // Deferring ws.close() and callbacks to the next event loop tick allows the WebSocket
        // to flush any pending outgoing messages (like 'cancel') before closing the connection.
        // packages/stage-ui/src/libs/speech/streaming-pipeline.ts
        // Can be removed if the WebSocket implementation natively flushes the write buffer before close.
        setTimeout(() => {
          try {
            ws.close()
          }
          catch {}
          triggerCallbacks()
        }, 0)
        return
      }
    }
    catch {}
    triggerCallbacks()
  }

  return {
    appendText(text: string) {
      if (text.length === 0)
        return
      // Pure-whitespace chunks (e.g. the " " between two LLM tokens) ARE
      // forwarded verbatim. Dropping them would corrupt the text the
      // upstream model sees ("hello" + " " + "world" → "helloworld").
      // The per-character billing cost is negligible compared to the
      // semantic risk; codex review LOW #7 noted the wasted units but
      // accepted the trade-off.
      safeSend(JSON.stringify({ event: 'text', text }))
    },
    finish() {
      safeSend(JSON.stringify({ event: 'finish' }))
    },
    cancel() {
      if (closed || terminationRequested)
        return
      safeSend(JSON.stringify({ event: 'cancel' }))
      // Route through `requestTerminate` so any in-flight `decodeAudioData`
      // can still resolve and emit `onSentence` before `onDone` flips the
      // consumer's `terminated` flag. tts-session.ts then runs
      // `stopByIntent` on the playback manager and drops whatever did
      // schedule, so this does NOT prolong playback — it just keeps the
      // termination semantics consistent across cancel / session.finished
      // / error / close paths (codex review).
      void requestTerminate(null)
    },
    feedback(event, payload) {
      // 只在 ws 活着的时候发；关掉之后 S 那边的会话已经结束，再报也没人收。
      safeSend(JSON.stringify({ event, ...payload }))
    },
  }
}

function noopHandle(): StreamingTtsPipelineHandle {
  return { appendText: () => {}, finish: () => {}, cancel: () => {}, feedback: () => {} }
}

function toWebSocketUrl(
  httpBase: string,
  path: string,
  token: string,
  analytics: {
    ttsTrigger: 'auto' | 'manual'
    ttsSource: 'chat_auto_tts' | 'manual_preview' | 'settings_test'
    ttsVoiceType: 'official_default' | 'official_selected' | 'custom_configured' | 'voice_pack' | 'unknown'
  },
): string {
  const u = new URL(path, httpBase)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  u.searchParams.set('token', token)
  u.searchParams.set('tts_trigger', analytics.ttsTrigger)
  u.searchParams.set('tts_source', analytics.ttsSource)
  u.searchParams.set('tts_voice_type', analytics.ttsVoiceType)
  return u.toString()
}

/**
 * Reads the sentence text from a `sentence.start` / `sentence.end` /
 * `subtitle` payload. Returns `null` when the payload doesn't carry one
 * (e.g. final upstream events with empty bodies).
 */
/**
 * 校验一份句组字节是不是**恰好一份完整 WAV** —— 结构校验，不是"数 RIFF 出现几次"。
 *
 * 为什么不用魔数计数：PCM 数据里恰巧出现 ASCII `RIFF` 就会被误判成"第二个容器"，
 * 于是**合法音频被误拒**（sol-medium 用真实 WAV 复现过）。按容器结构走一遍才分得清
 * "里面恰好有这几个字节" 和 "后面真的还跟着一段"。
 *
 * 判据（RIFF/WAVE 最小结构）：
 *   - 头 12 字节是 `RIFF` + 顶层长度 + `WAVE`，且 `8 + 顶层长度` 必须**正好**等于字节总长
 *     （多出来的字节 = 后面还跟着东西，比如第二份 WAV）；
 *   - 逐个子块（id 4 + size 4 + data，data 奇数长度补 1 字节对齐）走一遍，越界/截断即不合法。
 *
 * 返回 `null` = 合法；否则返回一句人话原因（进错误信息用）。
 */
function validateSingleWav(bytes: Uint8Array): string | null {
  if (bytes.length < 12)
    return `shorter than a WAV header (${bytes.length} bytes)`
  const tag = (o: number) => String.fromCharCode(bytes[o], bytes[o + 1], bytes[o + 2], bytes[o + 3])
  const u32 = (o: number) => (bytes[o] | (bytes[o + 1] << 8) | (bytes[o + 2] << 16) | (bytes[o + 3] << 24)) >>> 0
  if (tag(0) !== 'RIFF' || tag(8) !== 'WAVE')
    return 'not a RIFF/WAVE container'
  const declared = u32(4)
  if (8 + declared !== bytes.length)
    return `top-level length mismatch (8 + ${declared} != ${bytes.length})`
  let off = 12
  let sawFmt = false
  let sawData = false
  while (off + 8 <= bytes.length) {
    const id = tag(off)
    const size = u32(off + 4)
    const next = off + 8 + size + (size % 2)   // 子块数据按偶数对齐
    if (next > bytes.length)
      return `chunk overruns the buffer (${next} > ${bytes.length})`
    if (id === 'fmt ') {
      if (size < 16)                       // fmt 至少要能装下 PCM 的最小描述
        return `fmt chunk is too short (${size} < 16)`
      sawFmt = true
    }
    if (id === 'data')
      sawData = true
    off = next
  }
  if (off !== bytes.length)
    return `trailing bytes after the last chunk (${bytes.length - off})`
  // 光有外壳不算 WAV：必要块得在（`fmt ` 定格式、`data` 是音频本体）
  if (!sawFmt || !sawData)
    return `missing required chunk(s):${sawFmt ? '' : ' fmt'}${sawData ? '' : ' data'}`
  return null
}

function readSentenceText(payload: Record<string, unknown> | undefined): string | null {
  if (!payload || typeof payload !== 'object')
    return null
  const text = (payload as { text?: unknown }).text
  return typeof text === 'string' ? text : null
}

/**
 * Reads the v2 sentence-group identity. `unit_id` 缺了就当没有身份（返回 null）——
 * 宁可退回旧的本地编号，也不要造一个假身份去污染对账。
 */
function readSentenceIdentity(payload: Record<string, unknown> | undefined): StreamingSentenceIdentity | null {
  if (!payload || typeof payload !== 'object')
    return null
  const { unit_id: unitId, session_id: sessionId, generation, sub_index: subIndex } = payload as {
    unit_id?: unknown
    session_id?: unknown
    generation?: unknown
    sub_index?: unknown
  }
  if (typeof unitId !== 'string' || !unitId)
    return null
  return {
    unitId,
    ...(typeof sessionId === 'string' ? { sessionId } : {}),
    ...(typeof generation === 'number' ? { generation } : {}),
    ...(typeof subIndex === 'number' ? { subIndex } : {}),
  }
}

/**
 * Reads a v2 `display.span` payload (方案 §4.1/§4.2). Returns `null` for a
 * malformed span — a bad span must not crash the session, and it must not be
 * silently turned into a display credit either.
 */
function readDisplaySpan(payload: Record<string, unknown> | undefined): StreamingDisplaySpan | null {
  if (!payload || typeof payload !== 'object')
    return null
  const { text, source_range: range, unit_id: unitId, source_unit_id: sourceUnitId, eligible } = payload as {
    text?: unknown
    source_range?: unknown
    unit_id?: unknown
    source_unit_id?: unknown
    eligible?: unknown
  }
  if (typeof text !== 'string' || !Array.isArray(range) || range.length !== 2)
    return null
  // `eligible` 只认这两个字面量：**未知值一律丢弃**，不能默认放行 —— 否则一个坏 span
  // 会凭空拿到显示资格（复核指出：默认按 audio 放行与"坏 span 不获得资格"自相矛盾）。
  if (eligible !== 'audio' && eligible !== 'immediate')
    return null
  // 必需的 `unit_id` 不能退化成空串：账本靠它去重，空 id 会让重复投递算两遍。
  if (typeof unitId !== 'string' || unitId.length === 0)
    return null
  const [start, end] = range as [unknown, unknown]
  // 区间必须是真下标：非负**安全整数**、且非空（NaN / Infinity / 小数 / 负数 / 倒挂全部丢弃）。
  // 比复核口径（`end >= start`）再严一点：零宽 span 没有任何字符可显示，按坏数据处理。
  if (typeof start !== 'number' || typeof end !== 'number'
    || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end <= start) {
    return null
  }
  return {
    unitId,
    sourceUnitId: typeof sourceUnitId === 'string' && sourceUnitId.length > 0 ? sourceUnitId : unitId,
    sourceRange: [start, end],
    text,
    eligible,
  }
}
