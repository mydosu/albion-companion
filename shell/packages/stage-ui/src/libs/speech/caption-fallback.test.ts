import { describe, expect, it } from 'vitest'

import { createCaptionFallback } from './caption-fallback'

describe('createCaptionFallback', () => {
  it('会话失败后立刻出整轮原文，之后的 literal 继续追加', () => {
    const fb = createCaptionFallback()
    fb.begin('t1')
    expect(fb.push('你好')).toBeNull()
    expect(fb.push('呀。')).toBeNull()

    expect(fb.fail('t1')).toEqual({ caption: '你好呀。', present: '你好呀。' })
    expect(fb.active).toBe(true)

    expect(fb.push('今天')).toEqual({ caption: '你好呀。今天', present: '今天' })
    expect(fb.push('')).toBeNull()
  })

  it('失败时一个字还没到：只置位，后到的 literal 直接出', () => {
    const fb = createCaptionFallback()
    fb.begin('t1')
    expect(fb.fail('t1')).toBeNull()
    expect(fb.push('嗯。')).toEqual({ caption: '嗯。', present: '嗯。' })
  })

  it('新一轮开始清零，旧轮晚到的失败不影响新一轮', () => {
    const fb = createCaptionFallback()
    fb.begin('t1')
    fb.push('旧的。')
    fb.fail('t1')

    fb.begin('t2')
    expect(fb.active).toBe(false)
    expect(fb.push('新的。')).toBeNull()
    expect(fb.fail('t1')).toBeNull()
    expect(fb.active).toBe(false)
    expect(fb.fail('t2')).toEqual({ caption: '新的。', present: '新的。' })
  })

  it('账本已推进展示通道的前缀不重复，字幕仍用整轮原文', () => {
    const fb = createCaptionFallback()
    fb.begin('t1')
    fb.push('第一句。')
    fb.push('第二句。')
    expect(fb.fail('t1', '第一句。')).toEqual({ caption: '第一句。第二句。', present: '第二句。' })
    expect(fb.push('第三句。')).toEqual({ caption: '第一句。第二句。第三句。', present: '第三句。' })
  })

  it('账本文本对不上原文前缀时整段补，不丢字', () => {
    const fb = createCaptionFallback()
    fb.begin('t1')
    fb.push('原文。')
    expect(fb.fail('t1', '改写过')).toEqual({ caption: '原文。', present: '原文。' })
  })

  it('重复失败只出一次；用户停止后不再出字，下一轮恢复', () => {
    const fb = createCaptionFallback()
    fb.begin('t1')
    fb.push('甲。')
    expect(fb.fail('t1')).not.toBeNull()
    expect(fb.fail('t1')).toBeNull()

    fb.close()
    expect(fb.push('乙。')).toBeNull()
    expect(fb.fail('t1')).toBeNull()
    expect(fb.active).toBe(false)

    fb.begin('t2')
    fb.push('丙。')
    expect(fb.fail('t2')).toEqual({ caption: '丙。', present: '丙。' })
  })
})
