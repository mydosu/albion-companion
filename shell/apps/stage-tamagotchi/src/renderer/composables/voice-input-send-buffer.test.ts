import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createVoiceInputSendBuffer, VOICE_INPUT_HOLD_MAX_MS, VOICE_INPUT_SEND_DEBOUNCE_MS } from './voice-input-send-buffer'

/**
 * @example
 * 用户说完一段后先等 2 秒，2 秒内的新识别结果接着拼，超时才整段发出去。
 */
describe('createVoiceInputSendBuffer', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('waits two seconds after the last fragment before sending one merged turn', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    /** @example 等待时长就是那个显眼的常量，没有第二套数。 */
    expect(VOICE_INPUT_SEND_DEBOUNCE_MS).toBe(2000)

    buffer.push('我今天想跟你说')
    await vi.advanceTimersByTimeAsync(1500)
    /** @example 1.5 秒时还什么都没发。 */
    expect(sent).toEqual([])

    buffer.push('一件挺重要的事')
    await vi.advanceTimersByTimeAsync(1500)
    /** @example 第二段把计时重新拉回 2 秒：从它算起才 1.5 秒，仍然不发。 */
    expect(sent).toEqual([])

    await vi.advanceTimersByTimeAsync(500)
    /** @example 2 秒没动静，两段拼成一句一起发。 */
    expect(sent).toEqual(['我今天想跟你说一件挺重要的事'])
  })

  it('sends separately when the pause exceeds the debounce window', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    buffer.push('第一句')
    await vi.advanceTimersByTimeAsync(2000)
    /** @example 超过 2 秒没动静，先发第一句。 */
    expect(sent).toEqual(['第一句'])

    buffer.push('第二句')
    await vi.advanceTimersByTimeAsync(2000)
    /** @example 后一句单独发，不被并进上一句。 */
    expect(sent).toEqual(['第一句', '第二句'])
  })

  it('flushes immediately when the buffer is disposed (push-to-talk release)', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    buffer.push('按住说话说的')
    /** @example 松开按键那一下走 dispose：不等 2 秒，立刻发。 */
    await buffer.dispose()

    expect(sent).toEqual(['按住说话说的'])

    await vi.advanceTimersByTimeAsync(2000)
    /** @example 已经发过的那句不会被定时器再发一次。 */
    expect(sent).toEqual(['按住说话说的'])
  })

  it('reports the accumulated text on every fragment', () => {
    const updates: string[] = []
    const buffer = createVoiceInputSendBuffer({ send: () => {}, onAccumulatedText: text => updates.push(text) })

    buffer.push('等发送的时候')
    buffer.push('字幕要显示整段')

    /** @example 字幕拿到的是整段，不是最后一小段。 */
    expect(updates).toEqual(['等发送的时候', '等发送的时候字幕要显示整段'])
  })

  // ROOT CAUSE:
  //
  // 2 秒是从「拿到识别结果」开始算的，而用户停顿后接着说，下一段要等他讲完（VAD 静音 1.2 秒）
  // 再等 whisper 转写回来 —— 加起来常常超过 2 秒，第一段照样先发出去。
  it('holds the countdown while the next segment is being spoken', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    buffer.push('第一段')
    await vi.advanceTimersByTimeAsync(1000)
    /** @example VAD 判断又有人开口：按住。 */
    buffer.hold()

    // 这一段说了 5 秒才出结果（远超 2 秒）
    await vi.advanceTimersByTimeAsync(5000)
    /** @example 按住的期间一个字都不能发。 */
    expect(sent).toEqual([])

    buffer.push('第二段')
    await vi.advanceTimersByTimeAsync(1999)
    /** @example 结果到了重新计 2 秒，还差 1 毫秒。 */
    expect(sent).toEqual([])

    await vi.advanceTimersByTimeAsync(1)
    /** @example 2 秒没动静，两段拼成一句一次发出。 */
    expect(sent).toEqual(['第一段第二段'])
  })

  it('resumes the countdown when the held segment ends without a result', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    buffer.push('第一段')
    await vi.advanceTimersByTimeAsync(500)
    buffer.hold()

    await vi.advanceTimersByTimeAsync(5000)
    /** @example 这一段是空的 / 报错 / 被闸门挡掉：恢复计时。 */
    buffer.resume()

    await vi.advanceTimersByTimeAsync(1999)
    /** @example 从恢复那一刻起算，还不到 2 秒。 */
    expect(sent).toEqual([])

    await vi.advanceTimersByTimeAsync(1)
    /** @example 恢复后 2 秒把攒着的那段发出去，不能一直挂着。 */
    expect(sent).toEqual(['第一段'])
  })

  it('sends anyway when the hold lasts longer than the safety cap', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    buffer.push('一直没等到结局的一段')
    buffer.hold()

    await vi.advanceTimersByTimeAsync(VOICE_INPUT_HOLD_MAX_MS - 1)
    /** @example 保险时间内还按着。 */
    expect(sent).toEqual([])

    await vi.advanceTimersByTimeAsync(1)
    /** @example 到 15 秒照常发，不能因为 VAD 不结束就把话卡死。 */
    expect(sent).toEqual(['一直没等到结局的一段'])
  })

  // ROOT CAUSE:
  //
  // 第一版让「缓冲为空时 hold 什么都不做」—— 最常见的场景正好漏掉：A 说完停顿 1.2 秒、
  // VAD 切出 A 开始转写，用户紧接着开口说 B，这时 onSegmentStart 触发的 hold 撞上「缓冲还是空的」
  // 就丢了；A 的结果一到就开始计 2 秒，B 还没说完 A 就被单独发了出去。
  it('keeps counting holds even when the buffer is still empty', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    // A 开口（此刻缓冲是空的，A 的结果还没回来）
    buffer.hold()
    // B 开口（缓冲仍然是空的）
    buffer.hold()

    // A 的结果这才到
    buffer.push('A')
    await vi.advanceTimersByTimeAsync(2000)
    /** @example B 还在说：A 说完 2 秒也不能先发出去。 */
    expect(sent).toEqual([])

    // B 的结果到
    buffer.push('B')
    await vi.advanceTimersByTimeAsync(1999)
    /** @example 从 B 的结果算起还差 1 毫秒。 */
    expect(sent).toEqual([])

    await vi.advanceTimersByTimeAsync(1)
    /** @example 2 秒没动静，A + B 一次发出。 */
    expect(sent).toEqual(['A B'])
  })

  it('keeps working after an empty-buffer hold ends without a result', async () => {
    const sent: string[] = []
    const buffer = createVoiceInputSendBuffer({
      send: (text) => {
        sent.push(text)
      },
    })

    buffer.hold()
    // 这一段是空的（或者报错 / 被闸门挡掉）：消费掉这次 hold
    buffer.resume()

    buffer.push('后面这句')
    await vi.advanceTimersByTimeAsync(1999)
    /** @example 空的 hold 已经消费掉，不会把后面这句一直按住。 */
    expect(sent).toEqual([])

    await vi.advanceTimersByTimeAsync(1)
    /** @example 正常按 2 秒发出去。 */
    expect(sent).toEqual(['后面这句'])
  })
})
