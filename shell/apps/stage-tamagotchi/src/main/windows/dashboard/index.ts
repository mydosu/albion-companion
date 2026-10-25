import type { Rectangle } from 'electron'

import type { I18n } from '../../libs/i18n'
import type { ServerChannel } from '../../services/airi/channel-server'
import type { NoticeWindowManager } from '../notice'
import type { SettingsWindowManager } from '../settings'

import { dirname, join, resolve } from 'node:path'
import { env } from 'node:process'
import { fileURLToPath } from 'node:url'

import { is } from '@electron-toolkit/utils'
import { defineInvokeHandler } from '@moeru/eventa'
import { createContext } from '@moeru/eventa/adapters/electron/main'
import { initScreenCaptureForWindow } from '@proj-airi/electron-screen-capture/main'
import { BrowserWindow, ipcMain } from 'electron'
import { isLinux } from 'std-env'

import icon from '../../../../resources/icon.png?asset'

import { electronStartDraggingWindow } from '../../../shared/eventa'
import { baseUrl, getElectronMainDirname, load, withHashRoute } from '../../libs/electron/location'
import { appConfig, getAppConfig, updateAppConfig } from '../main/app-config'
import { protectPrivilegedWindowNavigation } from '../shared'
import { setupDashboardWindowElectronInvokes } from './rpc/index.electron'

export async function setupDashboardWindow(params: {
  settingsWindow: SettingsWindowManager
  chatWindow: () => Promise<BrowserWindow>
  noticeWindow: NoticeWindowManager
  onWindowCreated?: (window: BrowserWindow) => void
  serverChannel: ServerChannel
  i18n: I18n
}) {
  // 跟主窗共用同一份 app 配置（windows + windowFlags），不要另建一个同 key 的 createConfig
  appConfig.setup()

  const windowConfig = getAppConfig().windows?.find(w => w.title === 'AIRI Dashboard' && w.tag === 'dashboard')

  const window = new BrowserWindow({
    title: 'AIRI Dashboard',
    width: windowConfig?.width ?? 1200.0,
    height: windowConfig?.height ?? 600.0,
    x: windowConfig?.x,
    y: windowConfig?.y,
    show: false,
    icon,
    webPreferences: {
      preload: join(dirname(fileURLToPath(import.meta.url)), '../preload/index.mjs'),
      sandbox: false,
    },
  })

  if (params.onWindowCreated) {
    params.onWindowCreated(window)
  }

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
    const existingConfigIndex = windows.findIndex(w => w.title === 'AIRI Dashboard' && w.tag === 'dashboard')
    const nextWindow = {
      title: 'AIRI Dashboard',
      tag: 'dashboard',
      x: newBounds.x,
      y: newBounds.y,
      width: newBounds.width,
      height: newBounds.height,
    }

    if (existingConfigIndex === -1) {
      windows.push(nextWindow)
    }
    else {
      windows[existingConfigIndex] = { ...windows[existingConfigIndex], ...nextWindow }
    }

    // 只改 windows，windowFlags 等其它字段原样带回去
    updateAppConfig({ ...config, windows })
  }

  window.on('resize', () => handleNewBounds(window.getBounds()))
  window.on('move', () => handleNewBounds(window.getBounds()))

  window.on('ready-to-show', () => window!.show())
  protectPrivilegedWindowNavigation(window)

  await setupDashboardWindowElectronInvokes({
    window,
    settingsWindow: params.settingsWindow,
    chatWindow: params.chatWindow,
    noticeWindow: params.noticeWindow,
    i18n: params.i18n,
    serverChannel: params.serverChannel,
  })

  await load(window, withHashRoute(baseUrl(resolve(getElectronMainDirname(), '..', 'renderer')), '/dashboard', {
    query: { 'synced-leader': 'false' },
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
