import { selfTest } from 'file:///D:/AI/Wife/shell/packages/stage-ui-spine/src/components/scenes/spine38/author-engine.ts'
const errs = selfTest()
if (errs.length) { console.log('✗ 自检失败:', errs); process.exit(1) }
console.log('✓ author-engine 自检全过')
