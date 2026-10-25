import { describe, expect, it } from 'vitest'

import { createStreamTurnTracker } from './stream-turn'

type Item = { id: string, clock?: object }

function item(id: string, stream = false): Item {
  return { id, clock: stream ? { fake: 'clock' } : undefined }
}

describe('stream-turn tracker', () => {
  it('按 item 识别流式轮，普通 item 不受影响', () => {
    const turn = createStreamTurnTracker<Item>()
    expect(turn.onItemStart(item('legacy'))).toBe(false)
    expect(turn.pending).toBe(false)
    expect(turn.isStreamItem(item('legacy'))).toBe(false)

    expect(turn.onItemStart(item('stream-1', true))).toBe(true)
    expect(turn.pending).toBe(true)
    expect(turn.isStreamItem(item('stream-1', true))).toBe(true)
  })

  it('onDone 先于 M onStart（会话已清）时仍认得出流式轮', () => {
    // 顺序：服务端 `session.finished` → 宿主清 `currentSession` → M 才启动排队的 item。
    // 追踪器不读任何会话状态，所以照样成立；按 `currentSession` 判断就会误判成老路径。
    const turn = createStreamTurnTracker<Item>()
    expect(turn.onItemStart(item('stream-1', true))).toBe(true)
    expect(turn.pending).toBe(true)
  })

  it('onDone 清会话后、尾音还在播：pending 仍为真（200ms 空档定时器不许被打断）', () => {
    const turn = createStreamTurnTracker<Item>()
    turn.onItemStart(item('stream-1', true))
    // `onDone` 只清会话；追踪器不由它重置 —— 这是本片要钉住的顺序。
    expect(turn.pending).toBe(true)
  })

  it('item 正常结束 / 打断（reset）后 pending 归零', () => {
    const turn = createStreamTurnTracker<Item>()
    turn.onItemStart(item('stream-1', true))
    expect(turn.onItemEnd(item('stream-1', true))).toBe(true)
    expect(turn.pending).toBe(false)

    turn.onItemStart(item('stream-2', true))
    turn.reset()                                              // 打断 / 拒绝：没有 item 上下文
    expect(turn.pending).toBe(false)
  })
})
