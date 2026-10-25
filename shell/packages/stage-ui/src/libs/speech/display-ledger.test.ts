import { describe, expect, it } from 'vitest'

import { createDisplayLedger } from './display-ledger'

/** 按 code point 切原文 —— `source_range` 的口径就是它（复核要求）。 */
function cpSlice(source: string, range: [number, number]): string {
  return [...source].slice(range[0], range[1]).join('')
}

describe('createDisplayLedger', () => {
  it('只显示连续前缀：后段先到就先存着，缺口补上再一起长出来', () => {
    const source = '……你好呀。今天天气真不错呢。'
    // code point 下标（共 15 个）：0,1='……' 2..12='你好呀。今天天气真不错' 13,14='呢。'
    const led = createDisplayLedger()
    const span = (unitId: string, range: [number, number], eligible: 'audio' | 'immediate' = 'audio') =>
      ({ unitId, sourceRange: range, text: cpSlice(source, range), eligible })

    // 后段先到（前段还在合成）→ 一个字都不该显示出来
    expect(led.add(span('u2', [2, 13]))).toBe('')
    // 前段到齐 ⇒ 连续前缀补齐，两段一起长出来
    expect(led.add(span('u1', [0, 2], 'immediate'))).toBe('……你好呀。今天天气真不错')
    // 末段到 ⇒ 整句守恒
    expect(led.add(span('u3', [13, 15], 'immediate'))).toBe(source)

    expect(led.text()).toBe(source)
    expect(led.driven).toBe(true)
    expect(led.size).toBe(3)
  })

  it('emoji 不会错位：range 是 code point 下标，JS length 会多算', () => {
    const source = '你😊呀'
    // 事实核对：Python 里 3 个字符，JS 里 length 是 4（😊 是代理对）
    expect(source.length).toBe(4)
    expect([...source].length).toBe(3)

    const led = createDisplayLedger()
    led.add({ unitId: 'a', sourceRange: [0, 2], text: cpSlice(source, [0, 2]), eligible: 'audio' }) // '你😊'
    led.add({ unitId: 'b', sourceRange: [2, 3], text: cpSlice(source, [2, 3]), eligible: 'audio' }) // '呀'
    expect(led.text()).toBe(source)
  })

  it('takeNew 只给增量：连续前缀每长一截才吐一次（postPresent 用）', () => {
    const led = createDisplayLedger()
    led.add({ unitId: 'u1', sourceRange: [0, 2], text: '你好', eligible: 'audio' })
    expect(led.takeNew()).toBe('你好')
    expect(led.takeNew()).toBe('')
    led.add({ unitId: 'u2', sourceRange: [2, 5], text: '，世界', eligible: 'immediate' })
    expect(led.takeNew()).toBe('，世界')
    // 还没接上前缀的段不算增量（它一个字都没显示出来）
    led.add({ unitId: 'u4', sourceRange: [9, 11], text: '尾', eligible: 'audio' })
    expect(led.takeNew()).toBe('')
    led.add({ unitId: 'u3', sourceRange: [5, 9], text: '真不错呢', eligible: 'audio' })
    expect(led.takeNew()).toBe('真不错呢尾')
  })

  it('同一 unit_id 只落一次；unit_id 为空一律不收', () => {
    const led = createDisplayLedger()
    led.add({ unitId: 'u1', sourceRange: [0, 2], text: '你好', eligible: 'audio' })
    expect(led.add({ unitId: 'u1', sourceRange: [0, 2], text: '你好', eligible: 'audio' })).toBe('你好')
    expect(led.add({ unitId: '', sourceRange: [2, 4], text: '世界', eligible: 'audio' })).toBe('你好')
    expect(led.size).toBe(1)
    expect(led.driven).toBe(true)
  })

  it('clear 后回到未驱动状态（新一轮会话）', () => {
    const led = createDisplayLedger()
    led.add({ unitId: 'u1', sourceRange: [0, 2], text: '你好', eligible: 'audio' })
    led.clear()
    expect(led.text()).toBe('')
    expect(led.takeNew()).toBe('')
    expect(led.driven).toBe(false)
    expect(led.size).toBe(0)
  })
})
