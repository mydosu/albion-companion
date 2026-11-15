import { describe, expect, it } from 'vitest'

import { admitCaption } from './caption-swap'

describe('admitCaption', () => {
  it('还没落账的单元先存着（先到中文 ⇒ 起播时直接中文）', () => {
    expect(admitCaption(false, false)).toBe('pending')
    // 防御：未知单元即使被当成"最近一个"，也仍然是 pending
    expect(admitCaption(false, true)).toBe('pending')
  })

  it('就是屏幕上这一句 ⇒ 立刻替换', () => {
    expect(admitCaption(true, true)).toBe('apply')
  })

  it('已经翻篇（落过账但后面又有单元）⇒ 丢掉', () => {
    expect(admitCaption(true, false)).toBe('drop')
  })
})
