import type { LocaleDetector } from '@intlify/core'
import type { BrowserWindow, Rectangle } from 'electron'

import type { I18n } from '../libs/i18n'
import type { ServerChannel } from '../services/airi/channel-server'
import type { setupBeatSync } from '../windows/beat-sync'
import type { setupCaptionWindowManager } from '../windows/caption'
import type { ChatboxWindowManager } from '../windows/chatbox'
import type { WindowFlags } from '../windows/main/window-flags'
import type { SettingsWindowManager } from '../windows/settings'
import type { WidgetsWindowManager } from '../windows/widgets'
import type { RecommendedKind } from './recommended-layout'

import { env } from 'node:process'

import { is } from '@electron-toolkit/utils'
import { createContext } from '@moeru/eventa/adapters/electron/main'
import { isRendererUnavailable } from '@proj-airi/electron-vueuse/main'
import { effect } from 'alien-signals'
import { app, ipcMain, Menu, nativeImage, screen, shell, Tray } from 'electron'
import { debounce, once } from 'es-toolkit'
import { isMacOS } from 'std-env'

import icon from '../../../resources/icon.png?asset'
import macOSTrayIcon from '../../../resources/tray-icon-macos.png?asset'

import { electronStageLayoutChanged, electronStageLayoutRequest } from '../../shared/eventa'
import { findDominantDisplayArea } from '../../shared/utils/electron/display'
import { onAppBeforeQuit } from '../libs/bootkit/lifecycle'
import { sidecarConfigPath, sidecarStatus, startAlbionSidecars, writeSidecarConfig } from '../services/airi/albion-sidecars'
import { setupInlayWindow } from '../windows/inlay'
import { Animator } from '../windows/shared/animator'
import { computeResizedBoundsAnchoredToDominantDisplay } from '../windows/shared/display'
import { toggleWindowShow } from '../windows/shared/window'
import { recommendedKindOfModel, recommendedModelPairs, setupRecommendedLayouts } from './recommended-layout'

/** 语音服务（sidecar）最近一次探测到的状态，菜单构建时同步读取 */
let sidecarUp: Record<number, boolean> = {}

/** 顺高 / 半高 这两个选项沿用原来的竖版比例（450:600），不要跟着推荐尺寸变大 */
const ASPECT_RATIO = 450 / 600

function applyWindowSize(window: BrowserWindow, width: number, height: number, x?: number, y?: number): void {
  if (isRendererUnavailable(window)) {
    return
  }

  window.setResizable(true)

  const bounds = x !== undefined && y !== undefined
    ? {
        x: Math.round(x),
        y: Math.round(y),
        width: Math.round(width),
        height: Math.round(height),
      }
    : computeResizedBoundsAnchoredToDominantDisplay({
        currentBounds: window.getBounds(),
        targetSize: { width, height },
        displays: screen.getAllDisplays(),
      })

  window.setBounds(bounds)
  window.show()
}

function resolveAlignedWindowBounds(
  window: BrowserWindow,
  workArea: Rectangle,
  position: 'center' | 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right',
): Rectangle {
  const { width: windowWidth, height: windowHeight } = window.getBounds()
  const { x: areaX, y: areaY, width: areaWidth, height: areaHeight } = workArea

  let x = areaX
  let y = areaY

  switch (position) {
    case 'center':
      x = areaX + Math.floor((areaWidth - windowWidth) / 2)
      y = areaY + Math.floor((areaHeight - windowHeight) / 2)
      break
    case 'top-left':
      break
    case 'top-right':
      x = areaX + areaWidth - windowWidth
      break
    case 'bottom-left':
      y = areaY + areaHeight - windowHeight
      break
    case 'bottom-right':
      x = areaX + areaWidth - windowWidth
      y = areaY + areaHeight - windowHeight
      break
  }

  return { x, y, width: windowWidth, height: windowHeight }
}

function isSizeMatch(window: BrowserWindow, targetWidth: number, targetHeight: number): boolean {
  const { width, height } = window.getBounds()
  return Math.abs(width - Math.round(targetWidth)) <= 2 && Math.abs(height - Math.round(targetHeight)) <= 2
}

function isPositionMatch(window: BrowserWindow, targetX: number, targetY: number): boolean {
  const { x, y } = window.getBounds()
  return Math.abs(x - targetX) <= 5 && Math.abs(y - targetY) <= 5
}

export function setupTray(params: {
  mainWindow: BrowserWindow
  windowFlags: WindowFlags
  settingsWindow: SettingsWindowManager
  captionWindow: ReturnType<typeof setupCaptionWindowManager>
  chatboxWindow: ChatboxWindowManager
  widgetsWindow: WidgetsWindowManager
  beatSyncBgWindow: Awaited<ReturnType<typeof setupBeatSync>>
  aboutWindow: () => Promise<BrowserWindow>
  serverChannel: ServerChannel
  i18n: I18n
}): void {
  once(() => {
    const mainWindowAnimator = new Animator(params.mainWindow)

    function animateMainWindowTo(workArea: Rectangle, position: Parameters<typeof resolveAlignedWindowBounds>[2]) {
      const bounds = resolveAlignedWindowBounds(params.mainWindow, workArea, position)
      mainWindowAnimator.windowBoundsAnimateTo(bounds)
      params.mainWindow.show()
    }

    function applyMainWindowSize(width: number, height: number, x?: number, y?: number) {
      mainWindowAnimator.stop()
      applyWindowSize(params.mainWindow, width, height, x, y)
    }

    function applyRecommendedModelView(scale: string, position: string, kind: RecommendedKind) {
      const pairs = recommendedModelPairs(kind, scale, position)
      params.mainWindow.webContents
        .executeJavaScript(`(() => {
          try {
            const pairs = ${JSON.stringify(pairs)}
            for (const [k, v] of pairs)
              localStorage.setItem(k, v)
            // vueuse 的 useLocalStorage 监听 storage 事件，同窗口写入不会自动触发 → 手工派发一次
            for (const [k, v] of pairs)
              window.dispatchEvent(new StorageEvent('storage', { key: k, newValue: v, storageArea: localStorage }))
          }
          catch {}
        })()`)
        .catch(() => {})
    }

    // 启动头 15 秒不自动保存：那时窗口/模型还在被 applyRecommendedView 摆位，存了会把过渡态写进预设
    const layouts = setupRecommendedLayouts({
      userDataDir: app.getPath('userData'),
      getBounds: () => params.mainWindow.getBounds(),
      autosaveAfter: Date.now() + 15000,
    })

    /** 手动保存（退出前）：存完弹气泡、重建菜单 */
    function saveCurrentAsRecommended(kind: RecommendedKind) {
      const view = layouts.save(kind)
      if (!view)
        return
      const kindLabel = kind === 'spine' ? '（晨光）' : kind === 'spine-b' ? '（晨光·预设B）' : ''
      const text = `${kindLabel}${view.width}×${view.height} @ (${view.x}, ${view.y})　模型 ${view.scale} / ${view.position}`
      console.info('[tray] 已存为推荐：', text)
      try {
        // eslint-disable-next-line ts/no-use-before-define -- 定义在后面，但这段只在退出前执行
        appTray.displayBalloon({ title: kind === 'live2d' ? '推荐布局已保存' : `布局${kind === 'spine' ? '（晨光）' : '（晨光·预设B）'}已保存`, content: text })
      }
      catch {}
      // eslint-disable-next-line ts/no-use-before-define -- 同上，此刻菜单早已构建完成
      rebuildContextMenu()
    }

    /** 推荐：窗口大小 + 屏幕位置 + 模型缩放/位置 一起恢复（按模型种类取各自那套） */
    function applyRecommendedView(withPosition = false, kind: RecommendedKind = 'live2d') {
      const rec = layouts.read(kind)
      if (withPosition)
        applyMainWindowSize(rec.width, rec.height, rec.x, rec.y)
      else
        applyMainWindowSize(rec.width, rec.height)
      applyRecommendedModelView(rec.scale, rec.position, kind)
    }

    // 主窗渲染端推来的舞台布局（模型 id + 缩放/位置）。以前是 2 秒轮询 executeJavaScript 去读。
    // 入站 IPC 不分窗口，这里只认主窗自己发的（同 widgets 服务的 isFromWindow）。
    const { context: stageLayoutContext, dispose: disposeStageLayoutContext } = createContext(ipcMain, params.mainWindow)
    stageLayoutContext.on(electronStageLayoutChanged, (event, options) => {
      if (!event.body || options?.raw?.ipcMainEvent?.sender.id !== params.mainWindow.webContents.id)
        return
      const previousModelId = layouts.updateSnapshot(event.body)
      const modelId = event.body.modelId
      // 同一模型（含渲染端重载后重发的同一份）⇒ 只是缩放/位置变了，排一次自动保存
      if (previousModelId !== undefined && previousModelId === modelId) {
        layouts.scheduleAutosave()
        return
      }
      // 指挥官 2026-09-17：阿尔比恩模型一开启（启动时第一次收到、或切换过来）就自动套用它那套推荐布局。
      // 交互命中区是按推荐尺寸校准的，窗口大小一变就会偏，所以模型切到它时把窗口拉回推荐尺寸。
      // 套用引起的挪动/缩放会再触发自动保存，存下来的就是套用后的布局。
      const kind = recommendedKindOfModel(modelId)
      if (kind === 'live2d') {
        console.info('[tray] 检测到 Live2D 模型已开启，自动套用推荐布局')
        applyRecommendedView(true, 'live2d')
      }
      else if (kind === 'spine') {
        console.info('[tray] 检测到 Spine（晨光）模型已开启，自动套用推荐布局（晨光）')
        applyRecommendedView(true, 'spine')
      }
    })
    // 渲染端可能在这里开始监听之前就发过第一份快照 ⇒ 现在和以后每次页面加载完都请它补发一次
    const requestStageLayout = () => {
      if (!isRendererUnavailable(params.mainWindow))
        stageLayoutContext.emit(electronStageLayoutRequest, undefined)
    }
    params.mainWindow.webContents.on('did-finish-load', requestStageLayout)
    requestStageLayout()

    // 窗口挪动/缩放（拖动、托盘对齐、ResizeHandler）都会走到这里；防抖后静默存盘
    const scheduleLayoutAutosave = () => layouts.scheduleAutosave()
    params.mainWindow.on('move', scheduleLayoutAutosave)
    params.mainWindow.on('resize', scheduleLayoutAutosave)

    const trayImage = nativeImage.createFromPath(isMacOS ? macOSTrayIcon : icon).resize({ width: 16 })
    trayImage.setTemplateImage(isMacOS)

    const appTray = new Tray(trayImage)

    const rebuildContextMenu = debounce((): void => {
      if (isRendererUnavailable(params.mainWindow)) {
        return
      }

      const mainWindowBounds = params.mainWindow.getBounds()
      const rec = layouts.read('live2d')
      const recSpine = layouts.read('spine')
      const currentDisplay = findDominantDisplayArea(mainWindowBounds, screen.getAllDisplays()) ?? screen.getDisplayMatching(mainWindowBounds)
      const { x: areaX, y: areaY, width: areaWidth, height: areaHeight } = currentDisplay.workArea
      const { width: windowWidth, height: windowHeight } = mainWindowBounds

      const fullHeightTarget = areaHeight
      const fullWidthTarget = Math.floor(areaHeight * ASPECT_RATIO)
      const halfHeightTarget = Math.floor(areaHeight / 2)
      const halfWidthTarget = Math.floor(halfHeightTarget * ASPECT_RATIO)

      const contextMenu = Menu.buildFromTemplate([
        { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.show'), click: () => toggleWindowShow(params.mainWindow) },
        {
          label: '消息框',
          click: () => {
            void params.chatboxWindow.toggleVisibility().then(() => rebuildContextMenu())
          },
        },
        { label: '套用预设 B（晨光）', click: () => applyRecommendedView(true, 'spine-b') },
        { type: 'separator' },
        {
          label: '布局',
          submenu: [
            {
              label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.adjust_sizes'),
              submenu: [
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.recommended_size'),
                  type: 'checkbox',
                  checked: isSizeMatch(params.mainWindow, rec.width, rec.height),
                  click: () => applyRecommendedView(false, 'live2d'),
                },
                {
                  label: '推荐（晨光）',
                  type: 'checkbox',
                  checked: isSizeMatch(params.mainWindow, recSpine.width, recSpine.height),
                  click: () => applyRecommendedView(false, 'spine'),
                },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.full_height'),
                  type: 'checkbox',
                  checked: isSizeMatch(params.mainWindow, fullWidthTarget, fullHeightTarget),
                  click: () => applyMainWindowSize(fullWidthTarget, fullHeightTarget),
                },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.half_height'),
                  type: 'checkbox',
                  checked: isSizeMatch(params.mainWindow, halfWidthTarget, halfHeightTarget),
                  click: () => applyMainWindowSize(halfWidthTarget, halfHeightTarget),
                },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.full_screen'),
                  type: 'checkbox',
                  checked: isSizeMatch(params.mainWindow, areaWidth, areaHeight),
                  click: () => applyMainWindowSize(areaWidth, areaHeight, areaX, areaY),
                },
              ],
            },
            {
              label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.align_to'),
              submenu: [
                {
                  label: '推荐（Live2D）',
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, rec.x, rec.y),
                  click: () => applyRecommendedView(true, 'live2d'),
                },
                {
                  label: '推荐（晨光）',
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, recSpine.x, recSpine.y),
                  click: () => applyRecommendedView(true, 'spine'),
                },
                { type: 'separator' },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.center'),
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, areaX + Math.floor((areaWidth - windowWidth) / 2), areaY + Math.floor((areaHeight - windowHeight) / 2)),
                  click: () => animateMainWindowTo(currentDisplay.workArea, 'center'),
                },
                { type: 'separator' },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.top_left'),
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, areaX, areaY),
                  click: () => animateMainWindowTo(currentDisplay.workArea, 'top-left'),
                },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.top_right'),
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, areaX + areaWidth - windowWidth, areaY),
                  click: () => animateMainWindowTo(currentDisplay.workArea, 'top-right'),
                },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.bottom_left'),
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, areaX, areaY + areaHeight - windowHeight),
                  click: () => animateMainWindowTo(currentDisplay.workArea, 'bottom-left'),
                },
                {
                  label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.bottom_right'),
                  type: 'checkbox',
                  checked: isPositionMatch(params.mainWindow, areaX + areaWidth - windowWidth, areaY + areaHeight - windowHeight),
                  click: () => animateMainWindowTo(currentDisplay.workArea, 'bottom-right'),
                },
              ],
            },
          ],
        },
        { type: 'separator' },
        { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.settings'), click: () => void params.settingsWindow.openWindow('/settings') },
        {
          type: 'submenu',
          label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.caption_overlay'),
          submenu: Menu.buildFromTemplate([
            {
              label: params.i18n.t(params.captionWindow.isVisible()
                ? 'tamagotchi.electron.tray.menu.labels.label.close_caption'
                : 'tamagotchi.electron.tray.menu.labels.label.open_caption'),
              click: () => {
                void params.captionWindow.toggleVisibility().then(() => rebuildContextMenu())
              },
            },
            { type: 'checkbox', label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.follow_window'), checked: params.captionWindow.getIsFollowingWindow(), click: async menuItem => await params.captionWindow.setFollowWindow(Boolean(menuItem.checked)) },
            { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.reset_position'), click: async () => await params.captionWindow.resetToSide() },
          ]),
        },
        {
          label: '更多功能',
          submenu: [
            { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.open_inlay'), click: () => setupInlayWindow({ i18n: params.i18n, serverChannel: params.serverChannel }) },
            { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.open_widgets'), click: () => params.widgetsWindow.getWindow().then(window => toggleWindowShow(window)) },
            { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.about'), click: () => params.aboutWindow().then(window => toggleWindowShow(window)) },
          ],
        },
        {
          // fork 新增：窗口层级 + 拖动锁（2026-09-22）
          label: '窗口与交互',
          submenu: [
            {
              label: '窗口置于桌面底层（不压其他程序）',
              type: 'checkbox',
              checked: params.windowFlags.get('bottomLayer'),
              // windowFlags.set 会通知 onChange ⇒ 菜单在那里重建
              click: menuItem => params.windowFlags.set('bottomLayer', Boolean(menuItem.checked)),
            },
            {
              label: '禁止拖动模型',
              type: 'checkbox',
              checked: params.windowFlags.get('lockDrag'),
              click: menuItem => params.windowFlags.set('lockDrag', Boolean(menuItem.checked)),
            },
            {
              label: '禁止滚轮缩放',
              type: 'checkbox',
              checked: params.windowFlags.get('lockWheelZoom'),
              click: menuItem => params.windowFlags.set('lockWheelZoom', Boolean(menuItem.checked)),
            },
          ],
        },
        { type: 'separator' },
        {
          label: '语音服务',
          submenu: [
            { label: `Irodori 后端 :9889  ${sidecarUp[9889] ? '就绪' : '未就绪'}`, enabled: false },
            { label: `TTS 桥 :9881  ${sidecarUp[9881] ? '就绪' : '未就绪'}`, enabled: false },
            { label: `耳·STT :9882  ${sidecarUp[9882] ? '就绪' : '未就绪'}`, enabled: false },
            { type: 'separator' },
            { label: '重新拉起语音服务', click: () => { void startAlbionSidecars() } },
            {
              label: '打开 sidecar 配置（模型目录）',
              click: () => {
                writeSidecarConfig({})
                void shell.openPath(sidecarConfigPath())
              },
            },
          ],
        },
        ...is.dev || env.MAIN_APP_DEBUG || env.APP_DEBUG
          ? [
              { type: 'header', label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.devtools') },
              { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.troubleshoot_beatsync'), click: () => params.beatSyncBgWindow.webContents.openDevTools({ mode: 'detach' }) },
              { type: 'separator' },
            ] as const
          : [],
        { label: params.i18n.t('tamagotchi.electron.tray.menu.labels.label.quit'), click: () => app.quit() },
      ])

      appTray.setContextMenu(contextMenu)
    }, 50)

    params.mainWindow.on('resize', rebuildContextMenu)
    params.mainWindow.on('move', rebuildContextMenu)
    const visibilityChangeUnListener = params.captionWindow.onVisibilityChanged(rebuildContextMenu)
    // 开关读入（页面加载完）或托盘切换后，勾选状态要跟着变
    const stopWindowFlagsListener = params.windowFlags.onChange(rebuildContextMenu)

    // 语音服务状态：每 5 秒探一次端口，状态变了重建菜单（托盘菜单是静态的，只能重建）
    const refreshSidecar = async (): Promise<void> => {
      const next: Record<number, boolean> = {}
      for (const s of await sidecarStatus())
        next[s.port] = s.up
      const changed = JSON.stringify(next) !== JSON.stringify(sidecarUp)
      sidecarUp = next
      if (changed)
        rebuildContextMenu()
    }
    const sidecarTimer = setInterval(() => void refreshSidecar(), 5000)
    void refreshSidecar()

    rebuildContextMenu()

    // 指挥官 2026-09-17：壳一启动就把字幕窗打开，不用再手动点托盘。
    // 之后想关还是从托盘那一项关（关掉后这次运行不会再自动弹回来）。
    if (!params.captionWindow.isVisible())
      params.captionWindow.toggleVisibility()

    // 指挥官 2026-09-17：消息框（我们自己写的那条小对话条）也一起开。
    void params.chatboxWindow.isVisible().then((visible) => {
      if (!visible)
        void params.chatboxWindow.toggleVisibility()
    })

    const stopLocaleEffect = effect(() => {
      const locale = params.i18n.locale as (() => string | LocaleDetector<any[]> | undefined)
      locale()
      rebuildContextMenu()
    })

    onAppBeforeQuit(async () => {
      // 指挥官 2026-09-22：关壳前把此刻布局自动存成「推荐」。
      // 下次启动收到第一份布局快照时会 applyRecommendedView ⇒ 模型与画布位置就能回到关闭前。
      // 模型 id 和缩放/位置取渲染端最近推来的快照；只认阿尔比恩这两个模型（见 recommendedKindOfModel）。
      try {
        const kind = recommendedKindOfModel(layouts.currentModelId())
        if (kind)
          saveCurrentAsRecommended(kind)
      }
      catch (e) {
        console.warn('[tray] 退出前保存布局失败（不影响退出）', e)
      }

      // Stop every menu rebuild source before canceling its pending trailing call.
      // The tray must remain alive until no callback can reach it.
      params.mainWindow.off('resize', rebuildContextMenu)
      params.mainWindow.off('move', rebuildContextMenu)
      params.mainWindow.off('resize', scheduleLayoutAutosave)
      params.mainWindow.off('move', scheduleLayoutAutosave)
      params.mainWindow.webContents.off('did-finish-load', requestStageLayout)
      disposeStageLayoutContext()
      layouts.dispose()

      visibilityChangeUnListener()
      stopWindowFlagsListener()
      stopLocaleEffect()

      clearInterval(sidecarTimer)
      rebuildContextMenu.cancel()
      mainWindowAnimator.stop()

      appTray.destroy()
    })

    appTray.setToolTip('Project AIRI')
    appTray.addListener('click', () => toggleWindowShow(params.mainWindow))

    // On macOS, there's a special double-click event
    if (isMacOS) {
      appTray.addListener('double-click', () => toggleWindowShow(params.mainWindow))
    }
  })()
}
