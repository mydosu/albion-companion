import type { StreamingSentenceIdentity } from './streaming-pipeline'

/**
 * 流式 turn 的音频时钟（方案 §4.3）。
 *
 * 为什么需要它：`playback-manager` 只做所有权/排队/抢占，真正创建音频节点的是宿主的
 * `play()`。原来"一段一个 item + `source.start(0)`"，而 M 配 `maxVoices: 1`，
 * 任意时刻只有一个 `AudioBufferSourceNode` 在世 —— 段间全靠"上一段 ended → M 取下一段 →
 * 起播"这几次事件循环交接，于是有了可听见的空档。
 *
 * 这个模块只做一件事：把**一个 turn** 里陆续解码好的段排到 `AudioContext` 的时间轴上，
 * 让它们在 `nextStart` 处无缝接续。所有权/抢占仍归 M（一个 turn 一个 item，见宿主接线）。
 *
 * 口径（codex 裁决，不要改）：
 * - `start(max(now + lead, nextStart))`；`nextStart += duration + pauseAfter`。
 * - `lead` **只在时间轴没有起点时**加（首段 / 重同步之后），持续播放期间每段不再加。
 * - 只提前排 `maxAheadSpans` 段、`maxAheadSeconds` 秒内的节点；更远的留在这里的 `pending`。
 * - `pauseAfter` 是**真的停顿**（v2 的音频里没有插静音，由这里加），不是缺陷空档；
 *   所以 `underrun` 判定要把 `pauseAfter` 扣掉：空了更多才算供给/调度没跟上。
 * - `progress()` **不能**拿 `currentTime` 的墙钟差当"已播音频秒数"（它会跨过有意停顿和欠载
 *   空档）。逐段累计：播完的段整段计入，正在播的那段只计入"音频内已过的部分"。
 *   `units` 只在段**正常播完**时增加。
 */

export interface StreamingPlaybackSpan {
  /** 归属：`unit_id`（+ 子段号），只用于事件关联与去重记账。 */
  unitId: string
  subIndex?: number
  /**
   * 取证用：服务端身份**原样**保留（`session_id/generation/unit_id/sub_index`）。
   * 只进事件与节点，不参与排程、ACK、规划 —— 别靠拆 `unitId` 的冒号还原身份。
   */
  identity?: StreamingSentenceIdentity
  /** 取证用：本轮 intent。两个 intent 拿到同一个服务端 `unit_id` 时靠它区分。 */
  intentId?: string
  buffer: AudioBuffer
}

export interface StreamingPlaybackSegmentEvent {
  span: StreamingPlaybackSpan
  /** AudioContext 时间轴上的预定起点/终点（秒）。 */
  startAt: number
  endAt: number
  duration: number
}

export interface StreamingPlaybackStats {
  spansScheduled: number
  spansPlayed: number
  underruns: number
  underrunMs: number
  /** 时间轴重新起算的次数（首个段之后。取消/暂停恢复/设备切换都算）。 */
  resyncs: number
}

export interface StreamingPlaybackStopEvent {
  reason?: string
  fadeEnd: number | null
  /** A completed tail can exist before input closes or its ended callback runs. */
  completedEndAt?: number
}

export interface StreamingPlaybackOptions {
  audioContext: BaseAudioContext
  /**
   * 把段节点接到 destination / analyser / 口型图上。
   * `output` 是本模块插进来的**段增益节点**：三条支路都要从它接出去，
   * 别留 source 直连 destination 的旁路 —— 否则淡出控制不到那条声音（sol-high 方案 §三.2）。
   */
  connect?: (source: AudioBufferSourceNode, output: AudioNode) => void
  /**
   * 上下文现在能不能出声（宿主给 `() => audioContext.state === 'running'`）。默认 true。
   * 只用来**决定要不要报"这段开始了"** —— 暂停时墙钟照走、音频时钟不走，靠定时器报会提前点亮
   * 说话状态。暂停/设备切换后由宿主调 `wake()` 把待报的通知补上，这里不做短轮询。
   */
  isPlayable?: () => boolean
  /** 初始提前量，默认 0.05s。 */
  leadSeconds?: number
  /** 相邻可播段之间的停顿，默认 0.10s（旧 `_prepend_gap` 的听感，但由时间轴加）。 */
  pauseAfterSeconds?: number
  maxAheadSpans?: number
  maxAheadSeconds?: number
  /** 段**预定起点到点**时触发（不是排程那一刻）—— `nowSpeaking` / 口型用它。 */
  onSegmentStart?: (event: StreamingPlaybackSegmentEvent) => void
  onSegmentEnd?: (event: StreamingPlaybackSegmentEvent & { completed: boolean }) => void
  onUnderrun?: (ms: number, event: StreamingPlaybackSegmentEvent) => void
  /**
   * 该 turn 的音频全部播完（或已取消）—— 宿主的 `play()` 可以 resolve 了。
   * 取消时**要等淡出尾部真的走完**（见 `stop()`），不是调用即完成。
   */
  onDrained?: () => void
  /**
   * `stop()` 之后回报淡出终点（AudioContext 时间轴秒；没淡出就是 null）。
   * 宿主拿它设「下一轮不许早于此刻起播」的屏障 —— M 的 abort 会立刻放掉旧 item，
   * 光靠旧 clock 的 `drained` 拦不住新一轮（sol-high 方案 §三.2③）。
   */
  onStopped?: (info: StreamingPlaybackStopEvent) => void
  /**
   * 该 turn **自然播完**（不是被取消）时回报最后一段的终点（AudioContext 时间轴秒）。
   * 宿主拿它写「跨轮气口」屏障：旧轮正常说完 → 新轮首段至少再等 `naturalTurnGapSeconds`。
   * 之前只有取消路径会写屏障，自然说完时屏障是空的，新轮的音频一就绪就紧贴旧尾起播
   * （sol-high 方案 §二.A；真机 16:13 抓到 100/180/100ms 的接续）。
   */
  onNaturalEnd?: (endAt: number) => void
}

export interface StreamingPlayback {
  /** 段就绪（已解码）就交进来；第一次 push 之后宿主的 `play()` 才有事可做。 */
  push: (span: StreamingPlaybackSpan) => void
  /**
   * 宿主拿到所有权（`play()` 被调用）后开门：**在此之前只排队、不建节点、不起播** ——
   * 否则首个段会在 M 的所有权之外先出声（codex 复核同类问题）。
   */
  arm: (notBefore?: number) => void
  /**
   * 取消淡出期间上下文不走了（挂起 / 关闭）⇒ 把剩下的取消尾巴立刻收掉。
   * 宿主在 `statechange` 里发现非 running 时调它；正常播放时是 no-op。
   */
  abortCancelledTails: () => void
  /** 不会再有新段（服务端 `finish` / 会话收尾）。之后 `push()` 一律拒收。 */
  closeInput: () => void
  /**
   * 上下文恢复播放（或设备切换完成）后叫一声：补排待排的段，并补报还没报过的"段开始"。
   */
  wake: () => void
  /** 取消：停掉当前与所有未来的节点，清时间轴。 */
  stop: (reason?: string) => void
  /** 已播音频进度（秒 / 段），供 `playback.ack` 用。 */
  progress: () => { seconds: number, units: number }
  /** 时间轴当前的 `nextStart`（没起点时 null）—— 测试与诊断用。 */
  nextStart: () => number | null
  stats: () => StreamingPlaybackStats
  /** 该 turn 播完（或取消）后 resolve。 */
  drained: Promise<void>
}

/** 段结束时比 `endAt` 早多少算"被掐断"而不是"播完了"（event 循环抖动容忍）。 */
const COMPLETED_EPSILON = 0.02
/**
 * 主动打断时的淡出时长（秒）。**这是工程试验值，不是听测出来的最佳值** ——
 * 位于讨论过的 80~150ms 中间：短到不引入"等整段播完"的延迟，长到能消掉硬切/压尾。
 * 单变量验收（耳朵）之后再调；不为它加配置项（sol-high 方案 §三.1）。
 */
const FADE_SECONDS = 0.1
/** 只有这个中断理由走淡出（`Stage.vue` 的 `onBeforeMessageComposed` → `stopAll('new-message')`）。 */
const FADE_REASON = 'new-message'

export function createStreamingPlayback(options: StreamingPlaybackOptions): StreamingPlayback {
  const ctx = options.audioContext
  const lead = options.leadSeconds ?? 0.05
  const pauseAfter = options.pauseAfterSeconds ?? 0.10
  const maxAheadSpans = Math.max(1, options.maxAheadSpans ?? 2)
  const maxAheadSeconds = Math.max(0, options.maxAheadSeconds ?? 2)

  interface Scheduled {
    span: StreamingPlaybackSpan
    source: AudioBufferSourceNode
    /** 段增益：正常恒为 1；主动打断时 100ms 线性降到 0（sol-high 方案 §三.1）。 */
    gain: GainNode
    startAt: number
    endAt: number
    duration: number
    timer: ReturnType<typeof setTimeout> | null
    /** 段开始事件报过没有（暂停期间不报，恢复后由 `wake()` 补）。 */
    startNotified: boolean
    /** source/gain 是否已断开（幂等清理用，别重复 disconnect）。 */
    released: boolean
  }

  let nextStart: number | null = null
  let lastEndWithPause: number | null = null
  /** 本轮第一段不许早于这个时刻（AudioContext 时间轴秒）—— 跨轮屏障，取消/淡出后由宿主给。 */
  let notBefore = 0
  const pending: StreamingPlaybackSpan[] = []
  const scheduled: Scheduled[] = []
  let inputClosed = false
  let stopped = false
  let armed = false
  let wakeup: ReturnType<typeof setTimeout> | null = null
  let resolveDrained: (() => void) | null = null
  const drained = new Promise<void>((resolve) => {
    resolveDrained = resolve
  })
  let playedSeconds = 0
  let playedUnits = 0
  let resyncs = 0
  /** 最后一段**正常播完**的终点（取 `entry.endAt`，不取回调时刻的 `currentTime`）。 */
  let lastCompletedEndAt: number | null = null
  const stats: StreamingPlaybackStats = {
    spansScheduled: 0,
    spansPlayed: 0,
    underruns: 0,
    underrunMs: 0,
    resyncs: 0,
  }

  function maybeDrained() {
    const idle = scheduled.length === 0 && pending.length === 0
    // 取消（`stopped`）也**必须等节点清干净**才收尾 —— 淡出中的段还挂在 `scheduled` 上，
    // 那一刻放槽就等于让下一轮压着旧句尾巴开口（sol-high 方案 §三.2③）。
    if (!resolveDrained || !(stopped ? idle : (inputClosed && idle)))
      return
    // 自然轮末：**先写跨轮气口，再结算 `drained`**。顺序反了，M 会先放掉旧 item、
    // 启动下一轮，而那时屏障还是空的 ⇒ 新轮紧贴旧尾起播（sol-high 方案 §二.A）。
    // 取消路径（`stopped`）不走这里，它由 `onStopped({fadeEnd})` 写淡出屏障。
    if (!stopped && lastCompletedEndAt != null) {
      const endAt = lastCompletedEndAt
      lastCompletedEndAt = null            // 只回报一次
      options.onNaturalEnd?.(endAt)
    }
    const done = resolveDrained
    resolveDrained = null
    done()
    options.onDrained?.()
  }

  function schedule(span: StreamingPlaybackSpan, startAt: number) {
    const duration = span.buffer.duration
    const source = ctx.createBufferSource()
    source.buffer = span.buffer
    // 每个段一个增益节点，正常恒为 1 —— 只有主动打断才动它（淡出）。
    const gain = ctx.createGain()
    gain.gain.value = 1
    source.connect(gain)
    options.connect?.(source, gain)

    // 调试用（只写不读，不影响播放）：把服务端句组身份挂在节点上，CDP 探针才能把
    // 「服务端单元 ↔ 客户端播放段」对上号 —— 否则只能靠本地编号猜（sol-high §三）。
    try {
      // 取证元数据（只写不读，不参与排程）：探针按这些字段把播放段跟服务端账本逐条对上。
      const forensics = source as unknown as {
        __albionUnitId?: string
        __albionIdentity?: StreamingSentenceIdentity
        __albionIntentId?: string
      }
      forensics.__albionUnitId = span.unitId
      forensics.__albionIdentity = span.identity
      forensics.__albionIntentId = span.intentId
    }
    catch {}
    const entry: Scheduled = {
      span,
      source,
      gain,
      startAt,
      endAt: startAt + duration,
      duration,
      timer: null,
      startNotified: false,
      released: false,
    }
    scheduled.push(entry)
    stats.spansScheduled += 1

    armStartNotice(entry)
    source.onended = () => onEnded(entry)
    try {
      source.start(startAt)
    }
    catch {
      // 起播失败（设备没了之类）：按"没播"收尾，别把进度算进去。
      // 走 `onEnded`（它现在是唯一出口：这类段只走到"没播完"分支，进度 +0、事件照旧）
      // —— 顺带把 source/gain 断掉（sol-medium §2 要的清理）。
      source.onended = null
      onEnded(entry)
      return
    }
    nextStart = entry.endAt + pauseAfter
    lastEndWithPause = nextStart
  }

  /**
   * "这段开始了"的通知。定时器只是**唤醒信号**：到点后必须核过音频时钟（`currentTime` 已经
   * 走到预定起点）且上下文确实能出声，才报 —— 上下文暂停时墙钟照走、音频时钟不走，
   * 只靠定时器会把说话状态提前点亮。核不过就**不报也不轮询**，等宿主 `wake()`。
   */
  function armStartNotice(entry: Scheduled) {
    if (entry.startNotified || stopped || entry.timer != null)
      return
    const delayMs = Math.max(0, (entry.startAt - ctx.currentTime) * 1000)
    entry.timer = setTimeout(() => {
      entry.timer = null
      if (stopped || entry.startNotified)
        return
      if (ctx.currentTime + 1e-6 < entry.startAt || !playable())
        return                                   // 等 `wake()`：这里不做短轮询
      entry.startNotified = true
      options.onSegmentStart?.({
        span: entry.span,
        startAt: entry.startAt,
        endAt: entry.endAt,
        duration: entry.duration,
      })
    }, delayMs)
  }

  function playable() {
    return options.isPlayable?.() ?? true
  }

  function onEnded(entry: Scheduled) {
    if (entry.timer != null) {
      clearTimeout(entry.timer)
      entry.timer = null
    }
    const index = scheduled.indexOf(entry)
    if (index >= 0)
      scheduled.splice(index, 1)
    releaseNodes(entry)          // 正常播完/提前 ended 都要把 source+gain 断掉（sol-medium §2）

    // 事件循环卡顿会让"段开始"的通知还没发出去，音频就已经播完了（浏览器先给 `onended`）。
    // 那就**先补一次 start、再报 end** —— 否则消费者只看到 end 没看到 start，
    // 说话状态/口型会整段错过（codex 复核留下的边界）。取消时（`stopped`）不补。
    if (!stopped && !entry.startNotified && ctx.currentTime + 1e-6 >= entry.startAt) {
      entry.startNotified = true
      options.onSegmentStart?.({
        span: entry.span,
        startAt: entry.startAt,
        endAt: entry.endAt,
        duration: entry.duration,
      })
    }

    // 正常播完 = 没被取消、且真的走到了预定终点（`stop()` 会让 `currentTime` 停在半路）。
    const completed = !stopped && ctx.currentTime >= entry.endAt - COMPLETED_EPSILON
    if (completed) {
      playedSeconds += entry.duration
      playedUnits += 1
      stats.spansPlayed += 1
      // 自然播完的终点：气口屏障要的是"这一段排到哪儿结束"，不是回调什么时候跑起来
      // （事件循环抖一下，回调可能晚几十毫秒，拿 `currentTime` 会凭空多算一段气口）。
      lastCompletedEndAt = Math.max(lastCompletedEndAt ?? 0, entry.endAt)
    }
    else {
      // 被掐断/提前 ended：**已经听到的那部分要算进进度**（`progress()` 必须单调），
      // 但 `units` 只认正常播完的段。
      playedSeconds += Math.max(0, Math.min(entry.duration, ctx.currentTime - entry.startAt))
    }
    options.onSegmentEnd?.({
      span: entry.span,
      startAt: entry.startAt,
      endAt: entry.endAt,
      duration: entry.duration,
      completed,
    })
    pump()
    maybeDrained()
  }

  function pump() {
    if (stopped || !armed)
      return
    while (pending.length && scheduled.length < maxAheadSpans) {
      const span = pending[0]
      const now = ctx.currentTime
      // 时间轴没起点（首段 / 上段之后停太久 / 取消后重建）⇒ 从"现在 + lead"起算。
      // 有了起点就纯用 `nextStart` —— 持续播放期间不再每段加 lead。
      const fresh = nextStart == null || nextStart < now
      // 首个段不许早于跨轮屏障（上一轮淡出终点 + 余量）；持续播放期间仍是纯 `nextStart`。
      const startAt = fresh ? Math.max(now + lead, notBefore) : nextStart!
      if (startAt - now > maxAheadSeconds) {
        // 窗口外：留在 pending 等前面播掉。但**最后一段**可能等不到任何事件（没有下一段到达、
        // 也没有段结束）⇒ 会永远排不出去、这一轮永远 drained 不了。所以设个兜底闹钟，
        // 等它进入窗口时自己回来排。
        armWakeup((startAt - maxAheadSeconds - now) * 1000)
        break
      }

      pending.shift()
      if (fresh) {
        if (lastEndWithPause != null) {
          resyncs += 1
          stats.resyncs += 1
          noteGap(startAt, span)                  // 时间轴断了：这里同样是"供给/调度没跟上"
        }
        nextStart = null
      }
      else {
        noteGap(startAt, span)
      }
      schedule(span, startAt)
    }
    if (!pending.length && wakeup != null) {
      clearTimeout(wakeup)
      wakeup = null
    }
  }

  /** 时间轴断口记账：**扣掉有意停顿**，剩下的才算供给/调度造成的空档。 */
  function noteGap(startAt: number, span: StreamingPlaybackSpan) {
    if (lastEndWithPause == null || startAt <= lastEndWithPause)
      return
    const duration = span.buffer.duration
    const gapMs = (startAt - lastEndWithPause) * 1000
    stats.underruns += 1
    stats.underrunMs += gapMs
    options.onUnderrun?.(gapMs, { span, startAt, endAt: startAt + duration, duration })
  }

  function armWakeup(delayMs: number) {
    if (wakeup != null)
      clearTimeout(wakeup)
    wakeup = setTimeout(() => {
      wakeup = null
      pump()
    }, Math.max(0, delayMs) + 1)
  }

  /** 断开一个段的 source/gain。**幂等**：正常结束、取消结束、起播失败、强制清理都走这里。 */
  function releaseNodes(entry: Scheduled) {
    if (entry.released)
      return
    entry.released = true
    try {
      entry.source.disconnect()
    }
    catch {}
    try {
      entry.gain.disconnect()
    }
    catch {}
  }

  /** 立即清掉一个段（未来段、或上下文没法淡出时）：不算播完，也不报段事件。 */
  function hardStop(entry: Scheduled) {
    try {
      entry.source.stop()
    }
    catch {}
    releaseNodes(entry)
  }

  /**
   * 被取消的段淡出走完 → **只做资源清理**，不进正常完成路径：
   * 不报 `onSegmentEnd`（旧轮不补"整段已播完"的 ACK）、不记 `spansPlayed`、不动口型/说话状态。
   */
  function onCancelledEnded(entry: Scheduled) {
    releaseNodes(entry)
    const index = scheduled.indexOf(entry)
    if (index >= 0)
      scheduled.splice(index, 1)
    maybeDrained()                                  // 最后一个淡出段走完 = 这一轮真的收尾了
  }

  /**
   * 取消淡出期间上下文不走了（挂起 / 设备切换 / 关闭）：淡出**没法再推进**，
   * 留着就是"恢复后旧句尾巴还会冒出来 + `drained` 永远挂着 + 节点/闭包不收"
   * （sol-medium 复核 §1）。这里把**已取消**的尾巴立刻收掉。
   *
   * 只在 `stopped` 之后动手：正常播放时不许拿墙钟抢在排程淡出前面 disconnect。
   * 幂等；不报段事件、不 ACK、不动字幕/口型。
   */
  function sweepCancelledTails() {
    if (!stopped || !scheduled.length)
      return
    for (const entry of [...scheduled]) {
      if (entry.timer != null) {
        clearTimeout(entry.timer)
        entry.timer = null
      }
      entry.source.onended = null
      hardStop(entry)
      const index = scheduled.indexOf(entry)
      if (index >= 0)
        scheduled.splice(index, 1)
    }
    maybeDrained()
  }

  /**
   * 取消：停掉当前与所有未来的节点。
   *
   * 主动打断（`new-message`）**不是硬切**：正在出声的那段走 `FADE_SECONDS` 线性淡出
   * （`source.stop(fadeEnd)` 排程停止），未来段立即取消（不许在这段时间里冒出来），
   * 而且**淡出期间不能 disconnect**（否则淡出会被硬切掉）。
   *
   * 幂等但**分两种**：同一条取消链的重复调用（含再次 `new-message`）不延长淡出、不补硬停；
   * 静音 / 卸载 / 强制释放（别的理由）**允许立刻收走残留尾巴**（`sweepCancelledTails`）。
   * `drained` 要等淡出尾部真的走完（或被上面两种清理收掉）才 resolve。
   */
  function stop(reason?: string) {
    if (stopped) {
      if (reason !== FADE_REASON && reason !== undefined)
        sweepCancelledTails()
      return
    }
    stopped = true
    if (wakeup != null) {
      clearTimeout(wakeup)
      wakeup = null
    }
    const now = ctx.currentTime
    // 只有"新一轮开始"这种**主动打断**才淡出；静音、卸载、所有权取消、意图取消都还是立即停
    // （理由由 `stopAll(reason)` → `controller.abort(reason)` → 信号 reason 一路透传上来）。
    const canFade = reason === FADE_REASON && ctx.state === 'running'
    const removed: Scheduled[] = []
    let fadeEnd: number | null = null
    let completedEndAt = lastCompletedEndAt
    for (const entry of [...scheduled]) {
      if (entry.timer != null) {
        clearTimeout(entry.timer)
        entry.timer = null
      }
      // 还没出声的（未来段）、已经播完的、或上下文没法前进的 ⇒ 立即清掉
      const playing = canFade && now >= entry.startAt && now < entry.endAt
      if (!playing) {
        // Audio can finish before its ended callback reaches the main thread.
        if (now >= entry.endAt)
          completedEndAt = Math.max(completedEndAt ?? 0, entry.endAt)
        entry.source.onended = null
        hardStop(entry)
        removed.push(entry)
        continue
      }
      const end = Math.min(now + FADE_SECONDS, entry.endAt)
      fadeEnd = Math.max(fadeEnd ?? 0, end)
      try {
        entry.gain.gain.cancelScheduledValues(now)
        entry.gain.gain.setValueAtTime(1, now)
        entry.gain.gain.linearRampToValueAtTime(0, end)
        entry.source.onended = () => onCancelledEnded(entry)
        entry.source.stop(end)                      // 排程停止：淡出走完才真停
      }
      catch {
        entry.source.onended = null
        hardStop(entry)
        removed.push(entry)
      }
    }
    for (const entry of removed) {
      const index = scheduled.indexOf(entry)
      if (index >= 0)
        scheduled.splice(index, 1)
    }
    pending.length = 0
    nextStart = null
    lastEndWithPause = null
    options.onStopped?.({
      reason,
      fadeEnd,
      ...(reason === FADE_REASON && fadeEnd == null && completedEndAt != null ? { completedEndAt } : {}),
    })
    maybeDrained()                                  // 还有淡出中的段 ⇒ 等它们 ended 再收尾
  }

  function progress() {
    // 见文件头口径：墙钟差会跨过停顿与空档，只能逐段按"音频内已过的部分"累计。
    // `units` 只在段正常播完时 +1（在 `onEnded` 里），这里不估算，避免把半段算成一段。
    let seconds = playedSeconds
    const now = ctx.currentTime
    for (const entry of scheduled) {
      if (now > entry.startAt)
        seconds += Math.min(entry.duration, now - entry.startAt)
    }
    return { seconds, units: playedUnits }
  }

  return {
    push(span) {
      // 输入已经关闭（尤其纯无音频 turn：`drained` 早就 resolve、宿主的 M item 也放掉了），
      // 迟到音频**拒收** —— 否则它会在宿主所有权之外凭空出声（codex 复核第 1 条）。
      if (stopped || inputClosed)
        return
      pending.push(span)
      pump()
    },
    closeInput() {
      inputClosed = true
      pump()
      maybeDrained()
    },
    arm(notBeforeSeconds = 0) {
      if (stopped || armed)
        return
      notBefore = Math.max(0, notBeforeSeconds)
      armed = true
      pump()
    },
    wake() {
      if (stopped) {
        // 取消淡出期间被挂起过 ⇒ 恢复时把残留尾巴收掉（正常播放时是 no-op）
        sweepCancelledTails()
        return
      }
      pump()
      for (const entry of scheduled)
        armStartNotice(entry)
    },
    abortCancelledTails: sweepCancelledTails,
    stop,
    progress,
    nextStart: () => nextStart,
    stats: () => ({ ...stats }),
    drained,
  }
}
