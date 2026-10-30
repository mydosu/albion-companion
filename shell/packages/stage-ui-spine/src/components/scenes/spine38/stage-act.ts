/**
 * 脑在 ACT 标记里能指挥的 Spine 3.8 表演：脸（face）、保持多久（hold）、头部小动作（motion）。
 *
 * 这个模型没有手势动画：骨架里只有 `normal`（全身待机）和 '1'..'11'（0 秒的整张脸替换）。
 * 所以"动作"= 换哪张脸 + 头骨 `lian` 上的程序化点头/歪头/摇头。脑只看到语义名，
 * 动画名留在壳里，换模型时只改这里的表。
 */

/** ACT 里和 Spine 有关的字段（albion-pet 解析后经 Stage 交给场景）。 */
export interface SpineStageAct {
  emotion?: string
  face?: string
  motion?: string
  hold?: unknown
}

/**
 * 脸的语义名 → 换脸动画名（名字在脑的提示词里用，不改）。描述按 2026-10-30 生产壳逐张截图
 * （tests/output/promo-face-20261030/live/faces-all.png）校准，名字和实际画面不一定相符；
 * 括号里是作者在 spec 里拿这张脸配过的台词（含义以台词为准）：
 *   calm(normal) 平静待机
 *   nervous(1) 慌张，颤线+汗（作者配：牛奶打翻了…指挥官，您醒了？）
 *   smile(2) 眼睛睁大、嘴微张，愉快地开口说话（作者配：谢谢您／出去走走吧／任务处理完了？刚好…）。不是张嘴笑，也不是惊讶
 *   worried(3) 眉下压、小 o 嘴+汗，歉疚难过（作者配：试牛奶温度时手忙脚乱…）
 *   wry(4) 冒汗苦笑（作者没配台词）
 *   bashful(5)、shy(5) 含羞红脸小嘴（作者配：昨晚休息得怎么样？）
 *   fluster(6) 着急咧嘴（作者配：面包烤焦了，抱歉）
 *   gentle(7) 温和抿笑（作者没配台词）
 *   happy(8) 红脸眯眼笑（作者配：一起准备早餐／来新邮件了）
 *   dizzy(10) 手指抵唇、单眼眯、头边卷线的疑问小吃惊（作者配：这么早就有新的任务了？）。不是发晕
 *   pout(11) 皱眉鼓气（作者没配台词）
 *   9 漩涡眼（作者配：早安吻／誓约），没有语义名，见下面 shy 的注释
 */
export const FACE_ANIMATION: Readonly<Record<string, string>> = Object.freeze({
  calm: 'normal',
  nervous: '1',
  smile: '2',
  worried: '3',
  wry: '4',
  bashful: '5',
  fluster: '6',
  gentle: '7',
  happy: '8',
  // 害羞用带脸红的普通眼脸（脸 5）：脸 9 是漩涡眼，脸上的眼睛被画死了，眨不了也看不了人。
  // 9 现在没有情绪映射，只留作手动/调试用。
  shy: '5',
  dizzy: '10',
  pout: '11',
})

/**
 * AIRI 的 9 种情绪 → 默认脸（她没写 face 时用）。2026-10-30 对着截图和作者台词重排：
 * 难过用 3（歉疚）；闹小脾气用 11（皱眉鼓气）；惊讶和疑问都用 10（小吃惊的「嗯？」）；
 * 思考用 7（平和抿嘴）；好奇用 2（睁大眼、愉快开口）。
 */
const FACE_OF_EMOTION: Readonly<Record<string, string>> = Object.freeze({
  neutral: 'normal',
  happy: '8',
  sad: '3',
  angry: '11',
  surprised: '10',
  awkward: '5',
  question: '10',
  think: '7',
  curious: '2',
})

/**
 * 这一条 ACT 要换的脸（动画名）。face 认得就用 face；不认得或没写才看 emotion；都没有返回 undefined（脸不动）。
 *
 * @example
 * resolveFace({ emotion: 'happy', face: 'pout' }) // => '11'
 * resolveFace({ emotion: 'happy', face: '8' })    // => '8'（壳不认脸号，退回 emotion）
 */
export function resolveFace(act: SpineStageAct): string | undefined {
  const face = typeof act.face === 'string' ? FACE_ANIMATION[act.face.trim().toLowerCase()] : undefined
  if (face)
    return face
  return typeof act.emotion === 'string' ? FACE_OF_EMOTION[act.emotion.trim().toLowerCase()] : undefined
}

/** 头部小动作：只认这三个；Live2D 的组名（touch_head 等）在 Spine 下一律忽略。 */
export const HEAD_MOTIONS = ['nod', 'tilt', 'shake'] as const
export type HeadMotion = typeof HEAD_MOTIONS[number]

export function asHeadMotion(value: unknown): HeadMotion | undefined {
  const v = typeof value === 'string' ? value.trim().toLowerCase() : ''
  return (HEAD_MOTIONS as readonly string[]).includes(v) ? v as HeadMotion : undefined
}

/**
 * 头骨在第 t 秒相对原位的偏移；播完返回 undefined（调用方把偏移归零 = 回原位）。
 * rot 是度（正 = 逆时针），down / right 是骨架世界单位（≈0.7 CSS px）。幅度刻意小：
 * 鼠标跟随最多 ±3°，这里最多 6°，叠在待机动画之上不抢戏。
 *
 * @example
 * headMotionOffset('tilt', 1) // => { rot: 6, down: 0, right: 0 }
 * headMotionOffset('tilt', 9) // => undefined
 */
export function headMotionOffset(kind: HeadMotion, t: number): { rot: number, down: number, right: number } | undefined {
  if (!(t >= 0))
    return undefined
  if (kind === 'nod') {
    // 两下点头：第一下 9 单位、第二下 5 单位，各 0.45s，sin² 起落没有拐点
    const beat = 0.45
    if (t >= beat * 2)
      return undefined
    const amp = t < beat ? 9 : 5
    const s = Math.sin(Math.PI * (t % beat) / beat)
    return { rot: -1.5 * s * s, down: amp * s * s, right: 0 }
  }
  if (kind === 'tilt') {
    // 歪头：0.35s 歪过去，停 1.2s，0.45s 回来
    const into = 0.35
    const hold = 1.2
    const out = 0.45
    if (t >= into + hold + out)
      return undefined
    const ease = (x: number) => x * x * (3 - 2 * x)
    const k = t < into ? ease(t / into) : t < into + hold ? 1 : ease(1 - (t - into - hold) / out)
    return { rot: 6 * k, down: 0, right: 0 }
  }
  // 摇头：左右三下、逐渐收住，1.2s
  const dur = 1.2
  if (t >= dur)
    return undefined
  const s = Math.sin(2 * Math.PI * t / 0.4) * (1 - t / dur)
  return { rot: 1.5 * s, down: 0, right: 6 * s }
}

/** hold 的三种写法：默认跟着这段说话走；`keep` 留到下一个 ACT（有上限）；数字是秒。 */
export type FaceHold = { kind: 'speech' } | { kind: 'keep' } | { kind: 'seconds', seconds: number }

/**
 * @example
 * parseHold('keep') // => { kind: 'keep' }
 * parseHold('8')    // => { kind: 'seconds', seconds: 8 }
 * parseHold(999)    // => { kind: 'seconds', seconds: 60 }
 * parseHold(undefined) // => { kind: 'speech' }
 */
export function parseHold(value: unknown): FaceHold {
  if (typeof value === 'string' && value.trim().toLowerCase() === 'keep')
    return { kind: 'keep' }
  const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : Number.NaN
  if (Number.isFinite(n) && n > 0)
    return { kind: 'seconds', seconds: Math.min(60, Math.max(1, n)) }
  return { kind: 'speech' }
}

/** 说话结束后脸再留一会儿，免得最后一个字刚落脸就变。 */
const SPEECH_TAIL_MS = 1500
/** 这一轮没出声（静音、只给 ACT 的静默回合）时脸留多久。 */
const NO_SPEECH_MS = 6000
/**
 * 默认 hold 的上限：脸图在场时眨眼和视线都被盖住（眼睛是画进图里的），
 * 所以一张脸最多留 6s，到点先回 `normal` 让眼睛活过来，后面的句子用 emotion 再换。
 */
const SPEECH_MAX_MS = 6_000
/** keep 的安全上限：下一个 ACT 会先替换它，万一一直没有新回复，2 分钟后也回到 normal。 */
const KEEP_MAX_MS = 120_000

/**
 * 这张脸接下来留多久（毫秒）。`seconds` 与 `keep` 听调用方的；默认 `speech` 的规则：
 * 正在出声 → 最多 `SPEECH_MAX_MS`（脸图期间眼睛不会动，宁短勿长）；说完 → 补 `SPEECH_TAIL_MS`；
 * 直到超时都没出过声 → `NO_SPEECH_MS`。
 *
 * @example
 * holdDurationMs({ kind: 'speech' }, true, true)   // => 6000（说话中，6s 后先收脸）
 * holdDurationMs({ kind: 'speech' }, false, true)  // => 1500（说完的尾巴）
 * holdDurationMs({ kind: 'seconds', seconds: 8 }, true, true) // => 8000
 */
export function holdDurationMs(hold: FaceHold, speaking: boolean, heardSpeech: boolean): number {
  if (hold.kind === 'seconds')
    return hold.seconds * 1000
  if (hold.kind === 'keep')
    return KEEP_MAX_MS
  if (speaking)
    return SPEECH_MAX_MS
  return heardSpeech ? SPEECH_TAIL_MS : NO_SPEECH_MS
}

/**
 * 脸什么时候退回 normal。
 *
 * 状态：当前 hold、这一轮是否正在出声（Stage 的 assistantTurnBusy，整轮门闩而不是单段）、
 * 是否已经出过声。`start` 换新的一张脸时重置；`cancel` 在场景卸载、换脸到 normal 时调用。
 * 到点只调一次 `release`。
 */
export class FaceHoldTimer {
  private timer: ReturnType<typeof setTimeout> | undefined
  private hold: FaceHold | undefined
  private speaking = false
  private heardSpeech = false
  private readonly release: () => void

  constructor(release: () => void) {
    this.release = release
  }

  start(hold: FaceHold, speaking: boolean) {
    this.hold = hold
    this.speaking = speaking
    this.heardSpeech = speaking
    this.arm()
  }

  setSpeaking(speaking: boolean) {
    this.speaking = speaking
    if (speaking)
      this.heardSpeech = true
    if (this.hold?.kind === 'speech')
      this.arm()
  }

  cancel() {
    clearTimeout(this.timer)
    this.timer = undefined
    this.hold = undefined
  }

  private arm() {
    clearTimeout(this.timer)
    const hold = this.hold
    if (!hold)
      return
    const ms = holdDurationMs(hold, this.speaking, this.heardSpeech)
    this.timer = setTimeout(() => {
      this.timer = undefined
      this.hold = undefined
      this.release()
    }, ms)
  }
}
