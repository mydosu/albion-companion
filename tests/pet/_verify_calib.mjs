// 校准验收：你圈的两个点（头部、端茶壶的小黄鸡）现在必须命中正确的区域
const [, , wsUrl] = process.argv
const ws = new WebSocket(wsUrl)
let id = 0
const pending = new Map()
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const msgId = ++id
    pending.set(msgId, { resolve, reject })
    ws.send(JSON.stringify({ id: msgId, method, params }))
  })
}
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data)
  if (m.id && pending.has(m.id)) {
    const { resolve, reject } = pending.get(m.id)
    pending.delete(m.id)
    m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result)
  }
})
const ev = e => send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true }).then(r => r?.result?.value)

// 指挥官圈的两个点，从截图换算成窗口内的 CSS 坐标（截图 1193×998，含约 6px 边框，dpr 1.25）
const REF = [
  ['头部', 421, 292, 'touch_head'],
  ['端茶壶的小黄鸡', 249, 510, 'ArtMesh92'],
]
// 顺带抽查别的区域：按校准后的位置，看看落在哪个区
const CHECK = [
  ['丝袜区（校准后）', 585 * 0.63 - 64.1 + 79 * 0.63 / 2, 801 * 0.4943 + 16.2 + 68 * 0.4943 / 2],
  ['闪光点（校准后）', 361 * 0.63 - 64.1 + 74, 512 * 0.4943 + 16.2 + 41],
]

ws.addEventListener('open', async () => {
  try {
    const has = await ev(`typeof window.__albionInteractions?.calibration`)
    if (has !== 'function') {
      console.log('模块还没热更新，先刷新页面')
      await ev(`location.reload(), 'r'`)
      ws.close()
      process.exit(3)
    }
    console.log('当前校准 =', await ev(`JSON.stringify(window.__albionInteractions.calibration())`))
    console.log('')
    let ok = true
    for (const [name, x, y, want] of REF) {
      const got = await ev(`JSON.stringify(window.__albionInteractions.hitsAt(${x}, ${y}))`)
      const g = JSON.parse(got)
      const pass = g.includes(want)
      ok = ok && pass
      console.log(`  ${pass ? 'PASS' : 'FAIL'}  ${name.padEnd(10)} 屏幕(${x},${y}) → ${got}   期望含 ${want}`)
    }
    for (const [name, x, y] of CHECK) {
      const got = await ev(`JSON.stringify(window.__albionInteractions.hitsAt(${Math.round(x)}, ${Math.round(y)}))`)
      console.log(`  参考  ${name.padEnd(14)} 屏幕(${Math.round(x)},${Math.round(y)}) → ${got}`)
    }
    console.log(`\n结论：${ok ? '两个参考点全中 ✓' : '还有没中的 ✗'}`)
  }
  catch (e) { console.error('ERROR:', e.message) }
  finally { ws.close() }
})
