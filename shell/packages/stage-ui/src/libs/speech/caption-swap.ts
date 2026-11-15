/**
 * 晚到的中文字幕该不该上屏（2026-11-15）。
 *
 * 服务端先发日语原文的 display.span，中文（display.caption）什么时候回来说不准：
 * 亲密段一句 10~13s，经常是这句已经播完才回来。屏幕状态由壳决定，所以这条判断放在这里：
 *
 * - `pending`：这个单元还没落账（还没轮到它上屏）⇒ 先存着，span 来时直接显示中文；
 * - `apply`：就是屏幕上这一句 ⇒ 立刻把这一句的字幕换成中文；
 * - `drop`：落过账、但后面又有单元落账（已经翻篇）⇒ 丢掉，绝不糊到新句上。
 */
export type CaptionAdmission = 'pending' | 'apply' | 'drop'

/**
 * @param known 这个 unitId 已经落过账（账本里有它）
 * @param current 它是账本里最近落账的那个单元（= 屏幕上这一句）
 */
export function admitCaption(known: boolean, current: boolean): CaptionAdmission {
  if (!known)
    return 'pending'
  return current ? 'apply' : 'drop'
}
