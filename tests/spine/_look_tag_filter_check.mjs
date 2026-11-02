
const RE = /\[\[\s*LOOK\s*[:：]\s*(?:his|mine|cam)\s*\]\]/gi
const CAP = /\[\[\s*LOOK\s*[:：]\s*(?:his|mine|cam)\s*\]\]/gi
function makeFilter() {
  let buf = ''
  return (literal) => {
    const text = buf + literal
    buf = ''
    const tail = text.match(/\[\[[^\]]*$/)
    let safe = text
    if (tail) { safe = text.slice(0, text.length - tail[0].length); buf = tail[0] }
    return safe.replace(RE, '')
  }
}
const cases = [
  { n: '半角冒号', p: ['……那，[[LOOK:his]] 我看一眼。'], w: '……那， 我看一眼。' },
  { n: '全角冒号', p: ['[[LOOK：his]]好'], w: '好' },
  { n: '切成四片', p: ['[[', 'LOOK：', 'cam', ']]好'], w: '好' },
  { n: '字幕层兜底', p: ['（旁白）[[LOOK: mine]]正文'], w: '（旁白）正文' },
]
let bad = 0
for (const c of cases) {
  const f = makeFilter()
  const got = c.p.map(f).join('').replace(CAP, '')
  const ok = got === c.w
  if (!ok) bad++
  console.log(`${ok ? 'OK  ' : 'BAD '}${c.n}: ${JSON.stringify(got)}${ok ? '' : ' 期望 ' + JSON.stringify(c.w)}`)
}
if (bad) process.exit(1)
console.log('全部通过')
