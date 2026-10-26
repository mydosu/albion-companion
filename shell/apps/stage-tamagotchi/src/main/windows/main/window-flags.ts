import type { BrowserWindow } from 'electron'

import { isRendererUnavailable } from '@proj-airi/electron-vueuse/main'

import { releaseWindowFromDesktopBottom, setWindowAlwaysOnTop, sinkWindowToDesktopBottom } from '../shared/window'
import { readWindowFlags, saveWindowFlags } from './app-config'

/**
 * 托盘「窗口与交互」的三个开关（指挥官 2026-09-22）：
 *   bottomLayer   = settings/albion/windows-bottom  '1' ⇒ 主窗不置顶，并压到桌面最底层
 *   lockDrag      = settings/albion/lock-drag       '1' ⇒ 禁止拖动模型（albion-interactions.ts 读）
 *   lockWheelZoom = settings/albion/lock-wheel-zoom '1' ⇒ 禁止滚轮缩放（spine38/Model.vue 读）
 *
 * 真值存在主进程配置文件里（`app-config.json` 的 `windowFlags`，跟窗口位置同一份，见 app-config.ts），
 * 启动时同步读回来 ⇒ 勾选状态和窗口层级不依赖渲染端什么时候加载完。
 * 渲染端的同名字 localStorage 项只是给渲染端自己读的缓存：老版本只存在那里，这里迁移一次；
 * 之后每次页面加载都把主进程的真值写回去（打包版是 file://、dev 是 http://localhost:5173，两个 origin 不共享）。
 * 只有托盘会写这些开关，渲染端只读。
 */
const FLAG_KEYS = {
  bottomLayer: 'settings/albion/windows-bottom',
  lockDrag: 'settings/albion/lock-drag',
  lockWheelZoom: 'settings/albion/lock-wheel-zoom',
} as const

export type WindowFlagName = keyof typeof FLAG_KEYS

const FLAG_NAMES = Object.keys(FLAG_KEYS) as WindowFlagName[]

/**
 * 沉底节流：每次沉底要起一个 ~150ms 的 powershell（见 sinkWindowToDesktopBottom）。
 * 请求先攒 COALESCE_MS（主窗 focus 和 show 常常前后脚到，合成一次），
 * 两次真正执行之间至少隔 MIN_INTERVAL_MS；间隔内来的请求并进那一次待执行的，不丢——
 * 最后一次获焦之后一定还会再压一次，所以连点消息框，主窗最多在上层停约 1 秒。
 */
const SINK_COALESCE_MS = 50
const SINK_MIN_INTERVAL_MS = 1000

/**
 * 迁移重试（2026-10-25 真机撞到）：只靠「建完窗口立刻读一次 + did-finish-load 再读」
 * 会在页面还没就绪时读失败，而 did-finish-load 那时早已过去 ⇒ 永远不再试，勾选就丢了。
 * 迁移只在「配置里还没有开关」时跑一次，重试几次的代价可以忽略。
 */
const MIGRATION_RETRY_MS = 500
const MIGRATION_MAX_RETRIES = 20
/**
 * executeJavaScript 在页面还没稳定时可能既不 resolve 也不 reject（真机上就是这种：
 * 没有报错、也没有迁移，因为 await 一直挂着，重试都没机会排上）⇒ 读超时就当失败。
 */
const RENDERER_READ_TIMEOUT_MS = 3000

export function setupWindowFlags(mainWindow: BrowserWindow) {
  const stored = readWindowFlags()
  const flags: Record<WindowFlagName, boolean> = {
    bottomLayer: stored?.bottomLayer ?? false,
    lockDrag: stored?.lockDrag ?? false,
    lockWheelZoom: stored?.lockWheelZoom ?? false,
  }
  const listeners = new Set<() => void>()
  // 迁移读回来途中被托盘改过的开关：旧值不覆盖它们，以托盘为准
  const setDuringMigration = new Set<WindowFlagName>()
  // 配置里已经有这三个开关 ⇒ 不必再读渲染端；老版本只存 localStorage，迁移一次
  let migrationDone = stored !== undefined
  let syncing = false
  let appliedBottomLayer: boolean | null = null

  let migrationRetries = 0
  let migrationRetryTimer: ReturnType<typeof setTimeout> | undefined
  let sinkTimer: ReturnType<typeof setTimeout> | undefined

  let lastSinkAt = Number.NEGATIVE_INFINITY

  function runSink() {
    sinkTimer = undefined
    // 等待期间开关可能被关掉、窗口可能已销毁 ⇒ 执行时再判一次
    if (!flags.bottomLayer || mainWindow.isDestroyed())
      return
    lastSinkAt = Date.now()
    sinkWindowToDesktopBottom(mainWindow)
  }

  /** 底层开关开着时把主窗压回桌面最底；关着时什么都不做。可以随便多调，会被合并节流。 */
  function resinkIfBottomLayer() {
    if (!flags.bottomLayer || sinkTimer)
      return
    const wait = Math.max(SINK_COALESCE_MS, lastSinkAt + SINK_MIN_INTERVAL_MS - Date.now())
    sinkTimer = setTimeout(runSink, wait)
  }

  function applyBottomLayer() {
    if (appliedBottomLayer === flags.bottomLayer)
      return
    const wasBottomLayer = appliedBottomLayer
    appliedBottomLayer = flags.bottomLayer
    // 关掉置顶 = 主窗落到普通层级；字幕/消息框是它的子窗口 ⇒ 仍在模型之上、其他应用之下
    setWindowAlwaysOnTop(mainWindow, !flags.bottomLayer)
    if (flags.bottomLayer) {
      // 光"取消置顶"z 序不动（实测索引 52/434，等于没沉）⇒ 再真沉一次底
      resinkIfBottomLayer()
    }
    else if (wasBottomLayer) {
      // 沉底时打上的 WS_EX_NOACTIVATE 是 HWND 级别的，不会自己消失 ⇒ 关开关时清掉，点她才能再激活
      releaseWindowFromDesktopBottom(mainWindow)
    }
    console.info(`[window-flags] 窗口层级：${flags.bottomLayer ? '桌面底层（不压其他程序）' : '置顶'}`)
  }

  function notify() {
    for (const listener of listeners)
      listener()
  }

  function persist() {
    saveWindowFlags({
      bottomLayer: flags.bottomLayer,
      lockDrag: flags.lockDrag,
      lockWheelZoom: flags.lockWheelZoom,
    })
  }

  function readRendererFlags() {
    // 页面还不是我们那个 index.html（还没开始加载 / 正在换页）时读 localStorage 没有意义
    if (isRendererUnavailable(mainWindow) || !mainWindow.webContents.getURL().includes('index.html'))
      return Promise.resolve(null)
    const read = mainWindow.webContents.executeJavaScript(`(() => {
      const keys = ${JSON.stringify(FLAG_KEYS)}
      const out = {}
      for (const [name, key] of Object.entries(keys))
        out[name] = localStorage.getItem(key) === '1'
      return out
    })()`) as Promise<Partial<Record<WindowFlagName, boolean>> | null>
    // 卡住的读当失败处理，交给 scheduleMigrationRetry
    return new Promise<Partial<Record<WindowFlagName, boolean>> | null>((resolve) => {
      const timer = setTimeout(resolve, RENDERER_READ_TIMEOUT_MS, null)
      const settle = (value: Partial<Record<WindowFlagName, boolean>> | null) => {
        clearTimeout(timer)
        resolve(value)
      }
      read.then(settle, () => settle(null))
    })
  }

  /**
   * 往渲染端写一个开关值。必须「写 localStorage + 手工派发 storage 事件」两步：
   * vueuse 的 useLocalStorage 只监听 storage 事件，同窗口写入不会自动触发。
   */
  function writeRenderer(name: WindowFlagName) {
    if (isRendererUnavailable(mainWindow))
      return
    const k = JSON.stringify(FLAG_KEYS[name])
    const v = JSON.stringify(flags[name] ? '1' : '')
    void mainWindow.webContents
      .executeJavaScript(`(() => {
        try {
          localStorage.setItem(${k}, ${v})
          window.dispatchEvent(new StorageEvent('storage', { key: ${k}, newValue: ${v}, storageArea: localStorage }))
        }
        catch {}
      })()`)
      .catch(() => {})
  }

  /** 迁移没读成（页面还没就绪 / 渲染进程刚崩）⇒ 隔一会儿再试，别把这次启动的勾选丢在那儿 */
  function scheduleMigrationRetry() {
    if (migrationDone || migrationRetryTimer !== undefined || mainWindow.isDestroyed())
      return
    if (migrationRetries >= MIGRATION_MAX_RETRIES) {
      console.warn('[window-flags] 放弃迁移：渲染端 localStorage 一直没读到，下次启动再试')
      return
    }
    migrationRetries++
    console.info(`[window-flags] 还没读到渲染端开关，${MIGRATION_RETRY_MS}ms 后重试（${migrationRetries}/${MIGRATION_MAX_RETRIES}）`)
    migrationRetryTimer = setTimeout(() => {
      migrationRetryTimer = undefined
      void syncRenderer()
    }, MIGRATION_RETRY_MS)
  }

  /**
   * 跟渲染端对一次账：配置文件里还没有开关就迁移一次，然后把主进程真值写回渲染端缓存。
   * 页面在重载、渲染进程刚崩过时 executeJavaScript 会失败 ⇒ 不标迁移完成，交给 scheduleMigrationRetry。
   */
  async function syncRenderer() {
    if (syncing || isRendererUnavailable(mainWindow))
      return
    syncing = true
    try {
      if (!migrationDone) {
        const storedFlags = await readRendererFlags()
        if (!storedFlags) {
          scheduleMigrationRetry()
          return
        }
        for (const name of FLAG_NAMES) {
          if (storedFlags[name] && !setDuringMigration.has(name))
            flags[name] = true
        }
        migrationDone = true
        persist()
        applyBottomLayer()
        notify()
        console.info('[window-flags] 已把渲染端 localStorage 里的开关迁移进 app-config.json')
      }
      for (const name of FLAG_NAMES)
        writeRenderer(name)
    }
    catch (error) {
      console.warn('[window-flags] 读取开关失败（稍后重试）', error)
      scheduleMigrationRetry()
    }
    finally {
      syncing = false
    }
  }

  function set(name: WindowFlagName, value: boolean) {
    setDuringMigration.add(name)
    flags[name] = value
    persist()
    writeRenderer(name)
    if (name === 'bottomLayer')
      applyBottomLayer()
    notify()
  }

  /** 订阅开关变化（迁移完成、托盘切换）；返回取消订阅函数 */
  function onChange(listener: () => void) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  // 配置里读到的就是真值 ⇒ 建窗口后立刻定层级，不用等页面加载
  applyBottomLayer()

  const onDidFinishLoad = () => void syncRenderer()
  mainWindow.webContents.on('did-finish-load', onDidFinishLoad)
  // 主窗通常在这之前就加载完了（setupMainWindow 会等 load），所以不等事件、直接对一次账；
  // 页面还没就绪时读不成，scheduleMigrationRetry 会补试（2026-10-25 真机就栽在这里）。
  void syncRenderer()

  // 她一旦拿到焦点就会被抬到最前面 ⇒ 底层开关开着时，获焦/显示后压回最底
  mainWindow.on('focus', resinkIfBottomLayer)
  mainWindow.on('show', resinkIfBottomLayer)
  // 控制岛挂载时会按渲染端自己的「置顶」设置调一次 setAlwaysOnTop(true)，把启动时的沉底盖掉
  // （2026-10-26 真机：重启后不沉底，托盘取消再勾才好）⇒ 底层开关开着时，谁置顶都撤回再沉底
  mainWindow.on('always-on-top-changed', (_event, isAlwaysOnTop) => {
    if (!flags.bottomLayer || !isAlwaysOnTop)
      return
    setWindowAlwaysOnTop(mainWindow, false)
    resinkIfBottomLayer()
  })

  // 生命周期跟主窗走：主窗关掉后不再沉底，待执行的那次也取消
  mainWindow.once('closed', () => {
    clearTimeout(sinkTimer)
    sinkTimer = undefined
    clearTimeout(migrationRetryTimer)
    migrationRetryTimer = undefined
    listeners.clear()
  })

  return {
    get: (name: WindowFlagName) => flags[name],
    set,
    onChange,
    resinkIfBottomLayer,
  }
}

export type WindowFlags = ReturnType<typeof setupWindowFlags>
