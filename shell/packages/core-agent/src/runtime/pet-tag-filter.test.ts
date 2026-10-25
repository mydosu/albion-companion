import { describe, expect, it } from 'vitest'

import { createPetTagStreamFilter, stripPetTags } from './pet-tag-filter'

/** 按给定切片逐片喂入，模拟流式 token 到达，最后 flush 收尾。 */
function streamThrough(chunks: string[]): string {
  const filter = createPetTagStreamFilter()
  return chunks.map(chunk => filter.push(chunk)).join('') + filter.flush()
}

describe('stripPetTags', () => {
  it('削掉完整标记，大小写不敏感', () => {
    expect(stripPetTags('好[[read notebook]]的')).toBe('好的')
    expect(stripPetTags('……那，[[LOOK:his]] 我看一眼。')).toBe('……那， 我看一眼。')
    expect(stripPetTags('[[look:MINE]]正文')).toBe('正文')
  })

  it('标记内 80 字为上限：80 字削掉，81 字原样保留', () => {
    expect(stripPetTags(`[[${'a'.repeat(80)}]]`)).toBe('')
    expect(stripPetTags(`[[${'a'.repeat(81)}]]`)).toBe(`[[${'a'.repeat(81)}]]`)
  })

  it('不跨换行：换行之间的 [[ ]] 不算标记', () => {
    expect(stripPetTags('[[a\nb]]')).toBe('[[a\nb]]')
  })

  it('削完两头又拼成标记时继续削', () => {
    // ROOT CAUSE:
    //
    // 只 replace 一遍时，`[[y][[x]]]` 削掉 `[[x]]` 剩下 `[[y]]`，标记原样漏出。
    // 我们改成削到不再变化为止。
    expect(stripPetTags('[[y][[x]]]')).toBe('')
  })
})

describe('createPetTagStreamFilter', () => {
  it('标记被切成两片时，拼起来再削', () => {
    expect(streamThrough(['你好[[re', 'ad notebook]]呀'])).toBe('你好呀')
  })

  it('标记被切成四片时也能拼回来', () => {
    expect(streamThrough(['[[', 'LOOK：', 'cam', ']]好'])).toBe('好')
  })

  it('尾部半截标记先攒着，不交出去', () => {
    const filter = createPetTagStreamFilter()
    expect(filter.push('你好[[re')).toBe('你好')
    expect(filter.push('ad]]')).toBe('')
    expect(filter.flush()).toBe('')
  })

  it('流结束时没合上的半截当正文交出，不吞字', () => {
    expect(streamThrough(['说到这[[里'])).toBe('说到这[[里')
  })

  it('跨片时超过 80 字的标记不算标记，原样交出', () => {
    const long = `[[${'a'.repeat(81)}]]`
    expect(streamThrough([long.slice(0, 40), long.slice(40)])).toBe(long)
  })

  it('reset 丢掉上一轮的半截，不带进下一轮', () => {
    const filter = createPetTagStreamFilter()
    expect(filter.push('[[ab')).toBe('')
    filter.reset()
    expect(filter.push('cd]]')).toBe('cd]]')
    expect(filter.flush()).toBe('')
  })

  it('切在两个左括号之间、或差最后一个右括号时也不漏', () => {
    // ROOT CAUSE:
    //
    // 原来尾部只认 `[[...` 这种半截。片尾是单个 `[`（"a[" + "[x]]"）
    // 或差一个 `]`（"[[x]" + "]"）时，这一片被原样交出，下一片再交出剩下的，
    // 两片在下游（字幕、TTS、聊天）拼回完整的 `[[x]]`。
    //
    // 我们把这两种半截也算进尾部，先攒着。
    expect(streamThrough(['a[', '[x]]b'])).toBe('ab')
    expect(streamThrough(['[[x]', ']b'])).toBe('b')
    expect(streamThrough(['[[y]', '[[x]]', ']'])).toBe('')
  })

  it('单个 [ 是正文时，流结束照样交出', () => {
    expect(streamThrough(['数组 a[', '0] 是第一个'])).toBe('数组 a[0] 是第一个')
    expect(streamThrough(['结尾一个 ['])).toBe('结尾一个 [')
  })

  it('任意切成两片、三片，输出都和整段削除一致', () => {
    const samples = [
      '好[[read notebook]]的',
      '……那，[[LOOK:his]] 我看一眼。[[look:cam]]',
      'a[[b]]c[[d]]e',
      '[[y][[x]]]尾巴',
      '说到这[[里',
      '数组 a[0] 和 b[[1]',
      `[[${'a'.repeat(81)}]]`,
      '[[a\nb]]',
    ]
    for (const full of samples) {
      const expected = stripPetTags(full)
      for (let i = 0; i <= full.length; i++) {
        expect(streamThrough([full.slice(0, i), full.slice(i)])).toBe(expected)
        for (let j = i; j <= full.length; j++)
          expect(streamThrough([full.slice(0, i), full.slice(i, j), full.slice(j)])).toBe(expected)
      }
    }
  })
})
