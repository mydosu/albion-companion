import type { BrowserWindow } from 'electron'

import { EventEmitter } from 'node:events'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupWindowFlags } from './window-flags'

interface StoredFlags { bottomLayer?: boolean, lockDrag?: boolean, lockWheelZoom?: boolean }

const mocks = vi.hoisted(() => ({
  setWindowAlwaysOnTop: vi.fn(),
  sinkWindowToDesktopBottom: vi.fn(),
  releaseWindowFromDesktopBottom: vi.fn(),
  /** 假 app-config.json 里的 windowFlags；undefined = 这个版本之前没存过 */
  storedFlags: { value: undefined as StoredFlags | undefined },
  savedFlags: [] as StoredFlags[],
}))

vi.mock('@proj-airi/electron-vueuse/main', () => ({
  isRendererUnavailable: (window: { isDestroyed: () => boolean }) => window.isDestroyed(),
}))

vi.mock('../shared/window', () => ({
  setWindowAlwaysOnTop: mocks.setWindowAlwaysOnTop,
  sinkWindowToDesktopBottom: mocks.sinkWindowToDesktopBottom,
  releaseWindowFromDesktopBottom: mocks.releaseWindowFromDesktopBottom,
}))

vi.mock('./app-config', () => ({
  readWindowFlags: () => mocks.storedFlags.value,
  saveWindowFlags: (flags: Required<StoredFlags>) => {
    mocks.savedFlags.push({ ...flags })
    mocks.storedFlags.value = { ...flags }
  },
}))

function createMainWindow(options: { renderer?: StoredFlags, loading?: boolean, url?: string, failReads?: number, hangReads?: number } = {}) {
  const window = new EventEmitter() as EventEmitter & { webContents: EventEmitter & Record<string, unknown>, isDestroyed: () => boolean }
  const webContents = new EventEmitter() as EventEmitter & Record<string, unknown>
  const renderer: StoredFlags = options.renderer ?? {}
  const writes: string[] = []
  let url = options.url ?? 'file:///app/out/renderer/index.html?synced-leader=true#/'
  let failReads = options.failReads ?? 0
  let hangReads = options.hangReads ?? 0
  let reads = 0
  const executeJavaScript = vi.fn(async (code: string) => {
    if (code.includes('localStorage.setItem')) {
      writes.push(code)
      return undefined
    }
    reads++
    // 模拟 executeJavaScript 既不 resolve 也不 reject（真机上撞到的就是这种）
    if (hangReads > 0) {
      hangReads--
      return new Promise<never>(() => {})
    }
    // 模拟页面还没就绪：executeJavaScript 直接抛
    if (failReads > 0) {
      failReads--
      throw new Error('Script failed to execute')
    }
    return { ...renderer }
  })
  Object.assign(webContents, {
    executeJavaScript,
    isLoading: () => options.loading ?? false,
    getURL: () => url,
  })
  window.webContents = webContents
  window.isDestroyed = () => false
  return {
    window: window as unknown as BrowserWindow,
    emitter: window,
    executeJavaScript,
    writes,
    readCount: () => reads,
    setUrl: (next: string) => {
      url = next
    },
  }
}

describe('setupWindowFlags', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.setWindowAlwaysOnTop.mockClear()
    mocks.sinkWindowToDesktopBottom.mockClear()
    mocks.releaseWindowFromDesktopBottom.mockClear()
    mocks.storedFlags.value = undefined
    mocks.savedFlags.length = 0
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('restores stored flags at startup without waiting for the renderer and applies the bottom layer', async () => {
    mocks.storedFlags.value = { bottomLayer: true, lockDrag: false, lockWheelZoom: true }
    const main = createMainWindow()
    const flags = setupWindowFlags(main.window)

    // 同步就有值：托盘菜单建出来时勾选状态已经是对的
    expect(flags.get('bottomLayer')).toBe(true)
    expect(flags.get('lockDrag')).toBe(false)
    expect(flags.get('lockWheelZoom')).toBe(true)
    expect(mocks.setWindowAlwaysOnTop).toHaveBeenCalledWith(main.window, false)

    await vi.advanceTimersByTimeAsync(60)
    expect(mocks.sinkWindowToDesktopBottom).toHaveBeenCalledTimes(1)

    // 配置里已经有真值 ⇒ 不回读渲染端，只把真值写回去当缓存
    expect(main.readCount()).toBe(0)
    expect(main.writes.some(code => code.includes('"settings/albion/windows-bottom"')))
      .toBe(true)

    // No polling: time passing does not read the renderer again.
    await vi.advanceTimersByTimeAsync(10_000)
    expect(main.readCount()).toBe(0)
  })

  it('migrates the flags from renderer localStorage once when the config has none yet', async () => {
    const main = createMainWindow({ renderer: { bottomLayer: true, lockDrag: true } })
    const flags = setupWindowFlags(main.window)

    // 配置里没有 ⇒ 迁移前先按默认不勾，等页面读回来
    expect(flags.get('bottomLayer')).toBe(false)

    await vi.advanceTimersByTimeAsync(0)
    expect(main.readCount()).toBe(1)
    expect(flags.get('bottomLayer')).toBe(true)
    expect(flags.get('lockDrag')).toBe(true)
    expect(mocks.storedFlags.value).toEqual({ bottomLayer: true, lockDrag: true, lockWheelZoom: false })
    expect(mocks.setWindowAlwaysOnTop).toHaveBeenLastCalledWith(main.window, false)

    // 迁移过就不再读渲染端（哪怕页面重载、localStorage 被清空）
    main.window.webContents.emit('did-finish-load')
    await vi.advanceTimersByTimeAsync(0)
    expect(main.readCount()).toBe(1)
    expect(flags.get('bottomLayer')).toBe(true)
    expect(mocks.savedFlags).toHaveLength(1)
  })

  it('retries the migration when the renderer is not ready yet (真机 2026-10-25 栽的就是这个)', async () => {
    const main = createMainWindow({ renderer: { bottomLayer: true }, failReads: 2 })
    const flags = setupWindowFlags(main.window)

    await vi.advanceTimersByTimeAsync(0)
    expect(main.readCount()).toBe(1)
    expect(flags.get('bottomLayer')).toBe(false)

    await vi.advanceTimersByTimeAsync(600) // 第 2 次还是失败
    expect(main.readCount()).toBe(2)
    expect(mocks.storedFlags.value).toBeUndefined()

    await vi.advanceTimersByTimeAsync(600) // 第 3 次读到了
    expect(main.readCount()).toBe(3)
    expect(flags.get('bottomLayer')).toBe(true)
    expect(mocks.storedFlags.value?.bottomLayer).toBe(true)
    expect(mocks.savedFlags).toHaveLength(1)
  })

  it('does not get stuck when executeJavaScript never settles', async () => {
    const main = createMainWindow({ renderer: { bottomLayer: true }, hangReads: 1 })
    const flags = setupWindowFlags(main.window)

    await vi.advanceTimersByTimeAsync(0)
    expect(main.readCount()).toBe(1)
    expect(flags.get('bottomLayer')).toBe(false)

    await vi.advanceTimersByTimeAsync(3100) // 读超时 ⇒ 当失败，排重试
    await vi.advanceTimersByTimeAsync(600) // 第二次读成功
    expect(main.readCount()).toBe(2)
    expect(flags.get('bottomLayer')).toBe(true)
    expect(mocks.storedFlags.value?.bottomLayer).toBe(true)
  })

  it('waits until the main page is loaded before migrating', async () => {
    const main = createMainWindow({ renderer: { bottomLayer: true }, url: 'about:blank' })
    setupWindowFlags(main.window)

    await vi.advanceTimersByTimeAsync(0)
    expect(main.readCount()).toBe(0)
    expect(mocks.savedFlags).toHaveLength(0)

    main.setUrl('file:///app/out/renderer/index.html?synced-leader=true#/')
    await vi.advanceTimersByTimeAsync(600)
    expect(main.readCount()).toBe(1)
    expect(mocks.storedFlags.value?.bottomLayer).toBe(true)
  })

  it('persists tray clicks into the config so the next start restores them', async () => {
    mocks.storedFlags.value = { bottomLayer: false, lockDrag: false, lockWheelZoom: false }
    const main = createMainWindow()
    const flags = setupWindowFlags(main.window)
    await vi.advanceTimersByTimeAsync(0)

    const onChange = vi.fn()
    flags.onChange(onChange)

    flags.set('bottomLayer', true)
    expect(flags.get('bottomLayer')).toBe(true)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(mocks.storedFlags.value?.bottomLayer).toBe(true)
    expect(main.writes[main.writes.length - 1]).toContain('"settings/albion/windows-bottom"')
    expect(main.writes[main.writes.length - 1]).toContain('"1"')
    expect(main.writes[main.writes.length - 1]).toContain('new StorageEvent(\'storage\'')

    flags.set('bottomLayer', false)
    expect(mocks.storedFlags.value?.bottomLayer).toBe(false)
    expect(main.writes[main.writes.length - 1]).toContain('"settings/albion/windows-bottom", ""')
  })

  it('keeps the tray value when the tray changes a flag while the startup migration is in flight', async () => {
    const main = createMainWindow({ renderer: { bottomLayer: true, lockWheelZoom: true } })
    const flags = setupWindowFlags(main.window)
    flags.set('bottomLayer', false)
    await vi.advanceTimersByTimeAsync(0)

    expect(flags.get('bottomLayer')).toBe(false)
    // 托盘没碰过的开关照常迁移进来
    expect(flags.get('lockWheelZoom')).toBe(true)
  })

  it('coalesces focus and show into one sink and throttles repeated focus to about once per second', async () => {
    mocks.storedFlags.value = { bottomLayer: true, lockDrag: false, lockWheelZoom: false }
    const main = createMainWindow()
    setupWindowFlags(main.window)
    await vi.advanceTimersByTimeAsync(2000)
    mocks.sinkWindowToDesktopBottom.mockClear()

    main.emitter.emit('focus')
    main.emitter.emit('show')
    await vi.advanceTimersByTimeAsync(60)
    expect(mocks.sinkWindowToDesktopBottom).toHaveBeenCalledTimes(1)

    // Ten clicks on the chatbox within one second.
    for (let i = 0; i < 10; i++) {
      main.emitter.emit('focus')
      await vi.advanceTimersByTimeAsync(90)
    }
    // The trailing request still runs, so the window is pushed down after the last focus.
    await vi.advanceTimersByTimeAsync(1100)
    expect(mocks.sinkWindowToDesktopBottom).toHaveBeenCalledTimes(2)
  })

  // ROOT CAUSE:
  //
  // The controls island calls setAlwaysOnTop(true) on mount from its own renderer setting,
  // after the main process already sank the window. The bottom layer was lost until the tray toggled it again.
  //
  // We fixed this by undoing any always-on-top change while the bottom layer flag is on.
  it('undoes a later always-on-top request while the bottom layer is on', async () => {
    mocks.storedFlags.value = { bottomLayer: true, lockDrag: false, lockWheelZoom: false }
    const main = createMainWindow()
    const flags = setupWindowFlags(main.window)
    await vi.advanceTimersByTimeAsync(2000)
    mocks.setWindowAlwaysOnTop.mockClear()
    mocks.sinkWindowToDesktopBottom.mockClear()

    main.emitter.emit('always-on-top-changed', {}, true)
    expect(mocks.setWindowAlwaysOnTop).toHaveBeenCalledWith(main.window, false)
    await vi.advanceTimersByTimeAsync(60)
    expect(mocks.sinkWindowToDesktopBottom).toHaveBeenCalledTimes(1)

    // 开关关着时，置顶请求照常生效
    flags.set('bottomLayer', false)
    mocks.setWindowAlwaysOnTop.mockClear()
    main.emitter.emit('always-on-top-changed', {}, true)
    expect(mocks.setWindowAlwaysOnTop).not.toHaveBeenCalled()
  })

  it('does not sink when the bottom layer flag is off, and drops a pending sink after it is turned off', async () => {
    mocks.storedFlags.value = { bottomLayer: false, lockDrag: false, lockWheelZoom: false }
    const main = createMainWindow()
    const flags = setupWindowFlags(main.window)
    await vi.advanceTimersByTimeAsync(0)

    main.emitter.emit('focus')
    flags.resinkIfBottomLayer()
    await vi.advanceTimersByTimeAsync(2000)
    expect(mocks.sinkWindowToDesktopBottom).not.toHaveBeenCalled()

    flags.set('bottomLayer', true)
    flags.set('bottomLayer', false)
    await vi.advanceTimersByTimeAsync(2000)
    expect(mocks.sinkWindowToDesktopBottom).not.toHaveBeenCalled()
    expect(mocks.setWindowAlwaysOnTop).toHaveBeenLastCalledWith(main.window, true)
    // 沉底时打上的 WS_EX_NOACTIVATE 要清掉，否则关掉开关后点她还是不激活
    expect(mocks.releaseWindowFromDesktopBottom).toHaveBeenCalledTimes(1)
    expect(mocks.releaseWindowFromDesktopBottom).toHaveBeenCalledWith(main.window)
  })
})
