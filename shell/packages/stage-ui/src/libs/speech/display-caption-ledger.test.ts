/**
 * 中文字幕替换（2026-11-15）：字幕条换中文，原文账本（聊天/展示通道）不变。
 *
 * 服务端不再等字幕：display.span 先给日语原文，display.caption 晚到再用 unitId 换这一句。
 * 这里锁两件事：render() 能逐单元替换成中文；takeNew()（展示通道的增量）永远还是原文。
 */
import { describe, expect, it } from 'vitest'

import { createDisplayLedger } from './display-ledger'

function span(unitId: string, start: number, text: string) {
  return { unitId, sourceRange: [start, start + text.length] as [number, number], text, eligible: 'audio' as const }
}

describe('displayLedger.render', () => {
  it('按 unitId 替换成中文，其余单元保持原文', () => {
    const ledger = createDisplayLedger()
    ledger.add(span('u1', 0, 'はぁ……'))
    ledger.add(span('u2', 4, 'あっ……'))
    ledger.takeNew()

    expect(ledger.render(unitId => (unitId === 'u1' ? '哈啊……' : undefined)))
      .toBe('哈啊……あっ……')
    expect(ledger.text()).toBe('はぁ……あっ……') // 原文账本本身没被改
  })

  it('展示通道的增量（takeNew）永远是模型原话，不会被中文顶掉', () => {
    const ledger = createDisplayLedger()
    const zh = new Map([['u1', '哈啊……']])
    ledger.add(span('u1', 0, 'はぁ……'))
    // 先到中文：字幕替换成中文
    expect(ledger.render(unitId => zh.get(unitId))).toBe('哈啊……')
    // 聊天那条走 takeNew：拿到的是日语原文
    expect(ledger.takeNew()).toBe('はぁ……')

    ledger.add(span('u2', 4, 'あっ……'))
    expect(ledger.render(unitId => zh.get(unitId))).toBe('哈啊……あっ……')
    expect(ledger.takeNew()).toBe('あっ……') // 只补新长出来的原文
  })

  it('早几句的中文晚到：整段累计里换进去，展示通道不受影响', () => {
    const ledger = createDisplayLedger()
    ledger.add(span('u1', 0, 'はぁ……'))
    ledger.add(span('u2', 4, 'あっ……'))
    ledger.takeNew() // 展示通道已经拿到这两句的原文

    // 第一句的中文现在才回来：字幕条是整轮累计，照样要换
    const zh = new Map([['u1', '哈啊……']])
    expect(ledger.render(unitId => zh.get(unitId))).toBe('哈啊……あっ……')
    expect(ledger.takeNew()).toBe('') // 原文没长：展示通道不重复补，也不被中文顶掉
  })

  it('has() / lastUnitId 支持"先到 / 当前 / 翻篇"的判断', () => {
    const ledger = createDisplayLedger()
    expect(ledger.lastUnitId).toBe('')
    ledger.add(span('u1', 0, 'はぁ……'))
    expect(ledger.has('u1')).toBe(true)
    expect(ledger.lastUnitId).toBe('u1')
    ledger.add(span('u2', 4, 'あっ……'))
    expect(ledger.lastUnitId).toBe('u2')
    expect(ledger.has('u1')).toBe(true) // 落过账，但已不是当前句
    expect(ledger.has('u3')).toBe(false)
  })
})
