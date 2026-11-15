/**
 * 晚到的中文字幕该不该上屏（2026-11-15）。
 *
 * 字幕条显示的是**这一轮的整段累计文本**（applyDisplaySpan 每次 replace 整段，caption.vue
 * 一直留到下一轮），所以判断口径是"这一轮还在屏幕上吗"，不是"这句是不是当前句"：
 *
 * - `pending`：这一轮还在，但这个单元还没落账（还没轮到它上屏）⇒ 先存着，span 来了直接用；
 * - `apply`：这一轮还在，而且这个单元已经落账 ⇒ 存下来并重新 render + replace（哪怕它是
 *   早几句的，整段累计里也要换成中文）；
 * - `drop`：这一轮已经不在屏幕上（新一轮开始 / 被打断清掉了账本）⇒ 丢掉，绝不糊到新轮上。
 */
export type CaptionAdmission = 'pending' | 'apply' | 'drop'

/**
 * @param turnVisible 这一轮还在屏幕上（它的账本还在，没被新一轮/打断清掉）
 * @param known 这个 unitId 已经落过账
 */
export function admitCaption(turnVisible: boolean, known: boolean): CaptionAdmission {
  if (!turnVisible)
    return 'drop'
  return known ? 'apply' : 'pending'
}
