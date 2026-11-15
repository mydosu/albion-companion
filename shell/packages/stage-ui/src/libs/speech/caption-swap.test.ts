import { describe, expect, it } from 'vitest'

import { admitCaption } from './caption-swap'

describe('admitCaption', () => {
  it('这一轮还在、单元已落账 ⇒ 换进整段累计字幕（早几句的也换）', () => {
    expect(admitCaption(true, true)).toBe('apply')
  })

  it('这一轮还在、单元还没落账 ⇒ 先存着，span 来了直接用', () => {
    expect(admitCaption(true, false)).toBe('pending')
  })

  it('这一轮已经不在屏幕上（新一轮 / 被打断）⇒ 丢掉', () => {
    expect(admitCaption(false, true)).toBe('drop')
    expect(admitCaption(false, false)).toBe('drop')
  })
})
