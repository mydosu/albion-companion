import type { createContext } from '@moeru/eventa/adapters/electron/main'
import type { ResizeDirection } from '@proj-airi/electron-eventa'
import type { BrowserWindow, BrowserWindowConstructorOptions } from 'electron'

import type { I18n } from '../../libs/i18n'
import type { ServerChannel } from '../../services/airi/channel-server'

import { spawn } from 'node:child_process'

import { isRendererUnavailable } from '@proj-airi/electron-vueuse/main'
import { shell } from 'electron'
import { isMacOS, isWindows } from 'std-env'

import { createServerChannelService } from '../../services/airi/channel-server'
import { createI18nService } from '../../services/airi/i18n'
import { createAppService, createPowerMonitorService, createScreenService, createSystemPreferencesService, createWindowService } from '../../services/electron'

export function toggleWindowShow(window?: BrowserWindow | null): void {
  if (!window) {
    return
  }
  if (isRendererUnavailable(window)) {
    return
  }

  if (window?.isMinimized()) {
    window?.restore()
  }

  window?.show()
  window?.focus()
}

export function transparentWindowConfig(): BrowserWindowConstructorOptions {
  return {
    frame: false,
    titleBarStyle: isMacOS ? 'hidden' : undefined,
    transparent: true,
    hasShadow: false,
  }
}

/**
 * Blocks renderer navigation while allowing safe links to open in the system browser.
 *
 * Use when:
 * - Creating an Electron window that receives the shared privileged preload
 *
 * Expects:
 * - The window loads only AIRI-controlled renderer content
 *
 * Returns:
 * - Nothing; installs navigation and popup guards on the window's web contents
 */
export function protectPrivilegedWindowNavigation(window: BrowserWindow): void {
  function openSafeExternalUrl(rawUrl: string): void {
    try {
      const url = new URL(rawUrl)
      if (url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:')
        void shell.openExternal(url.toString())
    }
    catch {
      // Ignore malformed navigation targets.
    }
  }

  window.webContents.setWindowOpenHandler((details) => {
    openSafeExternalUrl(details.url)
    return { action: 'deny' }
  })
  window.webContents.on('will-navigate', (event, navigationUrl) => {
    // Renderer-initiated reloads keep the exact current URL in both packaged
    // (`file:`) and Vite (`http:`) builds. Let those through without widening
    // navigation to other local files or development-server paths.
    if (navigationUrl === window.webContents.getURL())
      return

    event.preventDefault()
    openSafeExternalUrl(navigationUrl)
  })
}

export function blurryWindowConfig(): BrowserWindowConstructorOptions {
  return {
    vibrancy: 'hud',
    backgroundMaterial: 'acrylic',
  }
}

export function spotlightLikeWindowConfig(): BrowserWindowConstructorOptions {
  return {
    ...blurryWindowConfig(),
    titleBarStyle: isMacOS ? 'hidden' : undefined,
  }
}

/**
 * Sets the window always-on-top level according to the host platform.
 *
 * macOS and Windows support the screen-saver level and relative level layering,
 * while Linux (X11/Wayland) works reliably with standard always-on-top.
 */
export function setWindowAlwaysOnTop(
  window: Pick<BrowserWindow, 'setAlwaysOnTop'>,
  flag: boolean,
  relativeLevel = 1,
): void {
  if (!flag) {
    window.setAlwaysOnTop(false)
    return
  }

  if (isMacOS || isWindows) {
    window.setAlwaysOnTop(true, 'screen-saver', relativeLevel)
    return
  }

  window.setAlwaysOnTop(true)
}

/**
 * 真·沉到桌面最底层（Win32 HWND_BOTTOM = 1）。
 *
 * 为什么需要它（2026-09-23 实测）：只调 `setAlwaysOnTop(false)` 是「不置顶」≠「沉底」——
 * z 序一动不动（实测索引 52/434），一获焦就跑到最前面。Electron 没有沉底 API ⇒ 拿
 * `getNativeWindowHandle()` 的 HWND 交给一次性 powershell 调 user32 的 SetWindowPos。
 * `SWP_NOSIZE|NOMOVE|NOACTIVATE = 0x13`：不改尺寸、不动位置、不抢焦点。
 *
 * 用法：不要直接调，走 windows/main/window-flags 的 resinkIfBottomLayer——那里判底层开关并合并节流。
 * 开启"窗口置于桌面底层"时、主窗获焦/显示时、消息框等附属窗被激活时（Windows 会把附属窗的主人一起抬起来）都会请求。
 *
 * ponytail: 每次 sink 起一个 ~150ms 的进程；节流后连点也最多约 1 秒一次。
 * 真嫌重再上 FFI（koffi 直调 user32，属新依赖，要先比较方案、经指挥官选定），不必引原生模块。
 */
export function sinkWindowToDesktopBottom(window: Pick<BrowserWindow, 'getNativeWindowHandle'>): void {
  try {
    const hwnd = window.getNativeWindowHandle().readBigUInt64LE(0).toString()
    // 顺带打上 WS_EX_NOACTIVATE（0x08000000）：点击她不再激活/抬升她，但鼠标事件照收（能拖、能戳）。
    // 末位 0x33 = NOSIZE|NOMOVE|NOACTIVATE|FRAMECHANGED（改扩展样式后要 FRAMECHANGED 才生效）。
    const cmd = 'Add-Type -Namespace Albion -Name Sink -MemberDefinition \'[DllImport("user32.dll")] '
      + 'public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f); '
      + '[DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i); '
      + '[DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);\'; '
      + `$h = [IntPtr]${hwnd}; [void][Albion.Sink]::SetWindowLong($h, -20, ([Albion.Sink]::GetWindowLong($h, -20) -bor 0x08000000)); [void][Albion.Sink]::SetWindowPos($h, [IntPtr]1, 0, 0, 0, 0, 0x33)`
    spawn('powershell', ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', cmd], { stdio: 'ignore', windowsHide: true }).on('error', () => {})
  }
  catch (e) {
    console.warn('[window] 沉底失败（不影响其他功能）', e)
  }
}

/**
 * 关掉「窗口置于桌面底层」时，把 sink 打上的 WS_EX_NOACTIVATE（0x08000000）清掉。
 *
 * 为什么需要（2026-10-25）：沉底时为了让点她不抢焦点会加上这个扩展样式，它是 HWND 级别的，
 * 不会因为 `setAlwaysOnTop(true)` 自己消失 ⇒ 关掉开关后点她仍然不激活、也抬不起来。
 * 只在「本来在底层 → 改成不底层」时调一次，跟 sink 一样走一次性 powershell。
 *
 * 每个 extern 都要单独标 [DllImport]，少一个 Add-Type 就编译不过（stdio 是 ignore，看不出来）。
 * 改完扩展样式再 SetWindowPos 一次：0x37 = NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED，
 * FRAMECHANGED 才让样式改动生效；带 NOZORDER ⇒ hWndInsertAfter 被忽略，z 序不动。
 */
export function releaseWindowFromDesktopBottom(window: Pick<BrowserWindow, 'getNativeWindowHandle'>): void {
  try {
    const hwnd = window.getNativeWindowHandle().readBigUInt64LE(0).toString()
    const cmd = 'Add-Type -Namespace Albion -Name Release -MemberDefinition \'[DllImport("user32.dll")] '
      + 'public static extern int GetWindowLong(IntPtr h, int i); '
      + '[DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v); '
      + '[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);\'; '
      + `$h = [IntPtr]${hwnd}; [void][Albion.Release]::SetWindowLong($h, -20, ([Albion.Release]::GetWindowLong($h, -20) -band (-bnot 0x08000000))); [void][Albion.Release]::SetWindowPos($h, [IntPtr]0, 0, 0, 0, 0, 0x37)`
    spawn('powershell', ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', cmd], { stdio: 'ignore', windowsHide: true }).on('error', () => {})
  }
  catch (e) {
    console.warn('[window] 取消沉底失败（不影响其他功能）', e)
  }
}

export function resizeWindowByDelta(params: {
  window: BrowserWindow
  deltaX: number
  deltaY: number
  direction: ResizeDirection
  minWidth?: number
  minHeight?: number
}): void {
  const bounds = params.window.getBounds()
  const minWidth = params.minWidth ?? 100
  const minHeight = params.minHeight ?? 200

  let { x, y, width, height } = bounds

  if (params.direction.includes('e')) {
    width = Math.max(minWidth, width + params.deltaX)
  }
  if (params.direction.includes('w')) {
    const newWidth = Math.max(minWidth, width - params.deltaX)
    if (newWidth !== width) {
      x = x + (width - newWidth)
      width = newWidth
    }
  }

  if (params.direction.includes('s')) {
    height = Math.max(minHeight, height + params.deltaY)
  }
  if (params.direction.includes('n')) {
    const newHeight = Math.max(minHeight, height - params.deltaY)
    if (newHeight !== height) {
      y = y + (height - newHeight)
      height = newHeight
    }
  }

  params.window.setBounds({ x, y, width, height })
}

export async function setupBaseWindowElectronInvokes(params: {
  context: ReturnType<typeof createContext>['context']
  window: BrowserWindow
  serverChannel: ServerChannel
  i18n: I18n
}) {
  createScreenService({ context: params.context, window: params.window })
  createWindowService({ context: params.context, window: params.window })
  createAppService({ context: params.context, window: params.window })
  createPowerMonitorService({ context: params.context, window: params.window })
  createSystemPreferencesService({ context: params.context, window: params.window })

  await createI18nService({ context: params.context, window: params.window, i18n: params.i18n })

  createServerChannelService({ serverChannel: params.serverChannel })
}
