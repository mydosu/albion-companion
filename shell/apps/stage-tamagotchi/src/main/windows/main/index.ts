import type { Rectangle } from 'electron'

import type { I18n } from '../../libs/i18n'
import type { ServerChannel } from '../../services/airi/channel-server'
import type { GodotStageManager } from '../../services/airi/godot-stage'
import type { McpStdioManager } from '../../services/airi/mcp-servers'
import type { AutoUpdater } from '../../services/electron/auto-updater'
import type { GlobalShortcutService } from '../../services/electron/global-shortcut'
import type { EditorWindowManager } from '../editor'
import type { NoticeWindowManager } from '../notice'
import type { OnboardingWindowManager } from '../onboarding'
import type { SettingsWindowManager } from '../settings'
import type { WidgetsWindowManager } from '../widgets'

import { dirname, join, resolve } from 'node:path'
import { env } from 'node:process'
import { fileURLToPath } from 'node:url'

import { is } from '@electron-toolkit/utils'
import { defineInvokeHandler } from '@moeru/eventa'
import { createContext } from '@moeru/eventa/adapters/electron/main'
import { initScreenCaptureForWindow } from '@proj-airi/electron-screen-capture/main'
import { BrowserWindow, ipcMain } from 'electron'
import { isLinux, isMacOS } from 'std-env'

import icon from '../../../../resources/icon.png?asset'

import { electronStartDraggingWindow } from '../../../shared/eventa'
import { onAppBeforeQuit } from '../../libs/bootkit/lifecycle'
import { baseUrl, getElectronMainDirname, load, withHashRoute } from '../../libs/electron/location'
import { protectPrivilegedWindowNavigation, setWindowAlwaysOnTop, transparentWindowConfig } from '../shared'
import { sinkWindowToDesktopBottom } from '../shared/window'
import { appConfig, getAppConfig, readWindowFlags, updateAppConfig } from './app-config'
import { setupMainWindowElectronInvokes } from './rpc/index.electron'

export async function setupMainWindow(params: {
  editorWindow: EditorWindowManager
  settingsWindow: SettingsWindowManager
  chatWindow: () => Promise<BrowserWindow>
  widgetsManager: WidgetsWindowManager
  noticeWindow: NoticeWindowManager
  autoUpdater: AutoUpdater
  onWindowCreated?: (window: BrowserWindow) => void
  serverChannel: ServerChannel
  godotStageManager: GodotStageManager
  mcpStdioManager: McpStdioManager
  i18n: I18n
  onboardingWindowManager: OnboardingWindowManager
  globalShortcut: GlobalShortcutService
}) {
  appConfig.setup()

  const mainWindowConfig = getAppConfig().windows?.find(w => w.title === 'AIRI' && w.tag === 'main')
  // 托盘「窗口置于桌面底层」等开关和窗口位置存同一份配置：建窗口之前就同步读出来，
  // 这样勾选状态和窗口层级从第一帧起就是对的，不再等页面加载完再读渲染端 localStorage。
  const bottomLayerAtStartup = readWindowFlags()?.bottomLayer ?? false

  const window = new BrowserWindow({
    title: 'AIRI',
    width: mainWindowConfig?.width ?? 450.0,
    height: mainWindowConfig?.height ?? 600.0,
    x: mainWindowConfig?.x,
    y: mainWindowConfig?.y,
    show: false,
    // 指挥官 2026-09-29：只留托盘图标（任务栏不出现按钮）。
    // 原来按钮在不在取决于「窗口置于桌面底层」的 WS_EX_NOACTIVATE 有没有被打上（sink 是事件驱动的）
    // ⇒ 启动时 show 早于 2 秒轮询读回开关，那一次不 sink ⇒ 有时启动后任务栏有按钮、点她一下才消失。
    // 同 chatbox / spotlight 的做法，直接不参与任务栏。
    skipTaskbar: true,
    icon,
    webPreferences: {
      preload: join(dirname(fileURLToPath(import.meta.url)), '../preload/index.mjs'),
      sandbox: false,
    },
    // Thanks to [@HeartArmy](https://github.com/HeartArmy) for the tip implementation.
    //
    // https://github.com/electron/electron/issues/10078#issuecomment-3410164802
    // https://stackoverflow.com/questions/39835282/set-browserwindow-always-on-top-even-other-app-is-in-fullscreen-electron-mac
    type: isMacOS ? 'panel' : undefined,
    ...transparentWindowConfig(),
  })

  if (params.onWindowCreated) {
    params.onWindowCreated(window)
  }

  let allowClose = false
  onAppBeforeQuit(() => {
    allowClose = true
  })

  // NOTICE: in development mode, open devtools by default
  if (is.dev || env.MAIN_APP_DEBUG || env.APP_DEBUG) {
    try {
      window.webContents.openDevTools({ mode: 'detach' })
    }
    catch (err) {
      console.error('failed to open devtools:', err)
    }
  }

  function handleNewBounds(newBounds: Rectangle) {
    const config = getAppConfig()
    const windows = Array.isArray(config.windows) ? config.windows : []
    const existingConfigIndex = windows.findIndex(w => w.title === 'AIRI' && w.tag === 'main')
    const nextMainWindow = {
      title: 'AIRI',
      tag: 'main',
      x: newBounds.x,
      y: newBounds.y,
      width: newBounds.width,
      height: newBounds.height,
    }

    if (existingConfigIndex === -1) {
      windows.push(nextMainWindow)
    }
    else {
      windows[existingConfigIndex] = { ...windows[existingConfigIndex], ...nextMainWindow }
    }

    // 只改 windows，windowFlags 等其它字段原样带回去
    updateAppConfig({ ...config, windows })
  }

  window.on('resize', () => handleNewBounds(window.getBounds()))
  window.on('move', () => handleNewBounds(window.getBounds()))
  window.on('close', (event) => {
    if (allowClose) {
      return
    }

    event.preventDefault()
    window.hide()
  })

  // Thanks to [@HeartArmy](https://github.com/HeartArmy) for the tip implementation.
  //
  // https://github.com/electron/electron/issues/10078#issuecomment-3410164802
  // https://stackoverflow.com/questions/39835282/set-browserwindow-always-on-top-even-other-app-is-in-fullscreen-electron-mac
  window.setVisibleOnAllWorkspaces(true)
  if (isMacOS) {
    window.setFullScreenable(false)
    window.setWindowButtonVisibility(false)
  }
  // 上次勾了「窗口置于桌面底层」⇒ 建窗口就先不置顶，show 之后立刻沉底（见 ready-to-show），
  // 不用等页面加载完读渲染端；没勾（默认）保持原来的置顶。
  setWindowAlwaysOnTop(window, !bottomLayerAtStartup)

  window.on('ready-to-show', () => {
    window!.show()
    // show 会把主窗抬到最前；勾了底层就在这里压回去。之后焦点/显示时的沉底由 window-flags 节流维持。
    if (bottomLayerAtStartup)
      sinkWindowToDesktopBottom(window!)
  })
  protectPrivilegedWindowNavigation(window)

  await setupMainWindowElectronInvokes({
    window,
    editorWindow: params.editorWindow,
    settingsWindow: params.settingsWindow,
    chatWindow: params.chatWindow,
    widgetsManager: params.widgetsManager,
    noticeWindow: params.noticeWindow,
    autoUpdater: params.autoUpdater,
    serverChannel: params.serverChannel,
    godotStageManager: params.godotStageManager,
    mcpStdioManager: params.mcpStdioManager,
    i18n: params.i18n,
    onboardingWindowManager: params.onboardingWindowManager,
    globalShortcut: params.globalShortcut,
  })

  await load(window, withHashRoute(baseUrl(resolve(getElectronMainDirname(), '..', 'renderer')), '/', {
    query: { 'synced-leader': 'true' },
  }))

  /**
   * This is a know issue (or expected behavior maybe) to Electron.
   * We don't use this approach on Linux because it's not working.
   *
   * Discussion: https://github.com/electron/electron/issues/37789
   * Workaround: https://github.com/noobfromph/electron-click-drag-plugin
   */
  if (!isLinux) {
    const { default: clickDragPlugin } = await import('electron-click-drag-plugin')

    function handleStartDraggingWindow() {
      try {
        const windowId = window.getNativeWindowHandle()
        clickDragPlugin.startDrag(windowId)
      }
      catch (error) {
        console.error(error)
      }
    }

    // TODO: once we refactored eventa to support window-namespaced contexts,
    // we can remove the setMaxListeners call below since eventa will be able to dispatch and
    // manage events within eventa's context system.
    ipcMain.setMaxListeners(0)

    const { context } = createContext(ipcMain, window)
    const cleanUpWindowDraggingInvokeHandler = defineInvokeHandler(context, electronStartDraggingWindow, handleStartDraggingWindow)

    window.on('closed', () => {
      cleanUpWindowDraggingInvokeHandler()
    })
  }

  initScreenCaptureForWindow(window)

  return window
}
