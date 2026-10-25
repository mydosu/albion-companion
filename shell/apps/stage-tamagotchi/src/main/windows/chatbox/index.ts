import type { I18n } from '../../libs/i18n'
import type { ServerChannel } from '../../services/airi/channel-server'
import type { McpStdioManager } from '../../services/airi/mcp-servers'
import type { WindowFlags } from '../main/window-flags'
import type { WidgetsWindowManager } from '../widgets'

import { join, resolve } from 'node:path'

import { BrowserWindow, screen } from 'electron'
import { debounce } from 'es-toolkit'
import { number, object, optional } from 'valibot'

import icon from '../../../../resources/icon.png?asset'

import { baseUrl, getElectronMainDirname, load, withHashRoute } from '../../libs/electron/location'
import { createConfig } from '../../libs/electron/persistence'
import { createReusableWindow } from '../../libs/electron/window-manager'
import { setupChatWindowElectronInvokes } from '../chat/rpc/index.electron'
import { protectPrivilegedWindowNavigation, setWindowAlwaysOnTop, transparentWindowConfig } from '../shared/window'

/**
 * 消息框（我们 fork 新增，2026-09-15）：一条半透明的小横条，独立窗口、可拖可缩放。
 * 和字幕窗彻底分开——字幕保持 AIRI 原样，这里只负责打字跟她说话。
 *
 * 发送走和自带聊天窗同一条路（chatStore.send → 窗口级 RPC），所以复用了
 * setupChatWindowElectronInvokes：独立窗口不是 leader，必须有这套 RPC 才能发出去。
 *
 * 位置和大小记在 windows-chatbox/config.json（拖动/缩放停下来 0.4 秒后写盘）。
 */
const CHATBOX_WIDTH = 460
const CHATBOX_HEIGHT = 64
const MIN_WIDTH = 280
const MIN_HEIGHT = 52

const chatboxConfigSchema = object({
  bounds: optional(object({
    x: number(),
    y: number(),
    width: number(),
    height: number(),
  })),
})

interface Bounds { x: number, y: number, width: number, height: number }

/** 保证窗口不会跑到屏幕外（换显示器/改分辨率后也能开出来） */
function clampToDisplay(bounds: Bounds): Bounds {
  const { workArea } = screen.getDisplayMatching(bounds)
  const width = Math.min(Math.max(bounds.width, MIN_WIDTH), workArea.width)
  const height = Math.min(Math.max(bounds.height, MIN_HEIGHT), workArea.height)
  return {
    x: Math.min(Math.max(bounds.x, workArea.x), workArea.x + workArea.width - width),
    y: Math.min(Math.max(bounds.y, workArea.y), workArea.y + workArea.height - height),
    width,
    height,
  }
}

/** 第一次打开：主屏工作区底部居中，别贴边也别压任务栏 */
function defaultBounds(): Bounds {
  try {
    const { workArea } = screen.getPrimaryDisplay()
    return {
      x: Math.round(workArea.x + (workArea.width - CHATBOX_WIDTH) / 2),
      y: Math.round(workArea.y + workArea.height - CHATBOX_HEIGHT - 80),
      width: CHATBOX_WIDTH,
      height: CHATBOX_HEIGHT,
    }
  }
  catch {
    return { x: 40, y: 40, width: CHATBOX_WIDTH, height: CHATBOX_HEIGHT }
  }
}

export function setupChatboxWindowManager(params: {
  mainWindow: BrowserWindow
  windowFlags: WindowFlags
  widgetsManager: WidgetsWindowManager
  serverChannel: ServerChannel
  mcpStdioManager: McpStdioManager
  i18n: I18n
}) {
  const {
    setup: setupConfig,
    get: getConfig,
    update: updateConfig,
  } = createConfig('windows-chatbox', 'config.json', chatboxConfigSchema, {
    default: {},
    autoHeal: true,
  })
  setupConfig()

  const reusable = createReusableWindow(async () => {
    const window = new BrowserWindow({
      title: 'Chatbox',
      width: CHATBOX_WIDTH,
      height: CHATBOX_HEIGHT,
      minWidth: MIN_WIDTH,
      minHeight: MIN_HEIGHT,
      show: false,
      resizable: true,
      movable: true,
      skipTaskbar: true,
      icon,
      webPreferences: {
        preload: join(getElectronMainDirname(), '../preload/index.mjs'),
        sandbox: false,
      },
      ...transparentWindowConfig(),
    })

    protectPrivilegedWindowNavigation(window)
    setWindowAlwaysOnTop(window, false)
    // 同上：子窗口 —— 压着模型，但不压着别的应用。
    window.setParentWindow(params.mainWindow)

    // 附属窗被激活时，Windows 会把主人（整扇主窗，含模型）一起抬到最前 ⇒ 你点消息框 = 模型跟着上顶层。
    // 「窗口置于桌面底层」开着时，消息框一获焦就把主窗压回桌面最底；关着（要她置顶）时不动。
    // 开关值读主进程内存，沉底由 windowFlags 合并节流（连点消息框不会每次都起 powershell）。
    window.on('focus', params.windowFlags.resinkIfBottomLayer)

    // 上次的位置（有就用，没有就默认）
    const saved = getConfig()?.bounds
    window.setBounds(saved ? clampToDisplay(saved) : defaultBounds())

    // 拖/缩放停下来再写盘，别一边拖一边刷
    const persistBounds = debounce(() => {
      try {
        const b = window.getBounds()
        updateConfig({ bounds: { x: b.x, y: b.y, width: b.width, height: b.height } })
      }
      catch {}
    }, 400)
    window.on('move', persistBounds)
    window.on('resize', persistBounds)

    window.on('ready-to-show', () => window.show())

    await setupChatWindowElectronInvokes({
      window,
      widgetsManager: params.widgetsManager,
      serverChannel: params.serverChannel,
      mcpStdioManager: params.mcpStdioManager,
      i18n: params.i18n,
    })

    await load(window, withHashRoute(baseUrl(resolve(getElectronMainDirname(), '..', 'renderer')), '/chatbox', {
      query: {
        'stage-runtime': 'minimal',
        'synced-leader': 'false',
      },
    }))

    return window
  })

  async function isVisible() {
    const window = await reusable.getWindow()
    return window.isVisible()
  }

  async function toggleVisibility() {
    const window = await reusable.getWindow()
    if (window.isVisible()) {
      window.hide()
    }
    else {
      if (window.isMinimized())
        window.restore()
      window.show()
      window.focus()
    }
    return window.isVisible()
  }

  return {
    getWindow: reusable.getWindow,
    isVisible,
    toggleVisibility,
  }
}

export type ChatboxWindowManager = ReturnType<typeof setupChatboxWindowManager>
