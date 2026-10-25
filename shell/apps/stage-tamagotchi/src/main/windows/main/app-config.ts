import type { InferOutput } from 'valibot'

import { array, boolean, number, object, optional, string } from 'valibot'

import { createConfig } from '../../libs/electron/persistence'

/**
 * 主窗的主进程配置（`<userData>/app-config.json`）：窗口位置/尺寸 + 托盘「窗口与交互」三个开关。
 *
 * 位置/尺寸一直存这里（见 windows/main/index.ts 的 handleNewBounds）。三个开关以前只写在
 * 主窗渲染端的 localStorage 里，启动时靠「页面加载完后 executeJavaScript 读一次」拿回来：
 * 读早了/读失败只打一行 warn 就回落到默认的不勾 ⇒ 重启后勾选看起来丢了，窗口又置顶。
 * 现在开关和窗口位置存同一份文件，主进程在**建窗口之前**同步读到，勾选和层级都不再依赖渲染端
 * （渲染端 localStorage 只作为渲染端自己的读缓存保留，见 window-flags.ts）。
 */
const appConfigSchema = object({
  windows: optional(array(object({
    title: optional(string()),
    tag: string(),
    x: optional(number()),
    y: optional(number()),
    width: optional(number()),
    height: optional(number()),
  }))),
  windowFlags: optional(object({
    bottomLayer: optional(boolean()),
    lockDrag: optional(boolean()),
    lockWheelZoom: optional(boolean()),
  })),
})

export type AppConfig = InferOutput<typeof appConfigSchema>

/** 托盘「窗口与交互」三个开关在配置文件里的样子；`undefined` 字段 = 还没存过 */
export type WindowFlagsSnapshot = NonNullable<AppConfig['windowFlags']>

export const appConfig = createConfig('app', 'config.json', appConfigSchema, {
  default: { windows: [] },
  autoHeal: true,
})

export function getAppConfig(): AppConfig {
  return appConfig.get() ?? { windows: [] }
}

export function updateAppConfig(config: AppConfig): void {
  appConfig.update(config)
}

/**
 * 已存下来的窗口开关；整个 `windowFlags` 字段都没有 = 这个版本之前没存过
 * （需要从渲染端 localStorage 迁移一次，迁移后永远以配置文件为准）。
 */
export function readWindowFlags(): WindowFlagsSnapshot | undefined {
  return appConfig.get()?.windowFlags
}

/** 存盘时保留 `windows` 等其它字段，只替换 `windowFlags` */
export function saveWindowFlags(flags: Required<WindowFlagsSnapshot>): void {
  updateAppConfig({ ...getAppConfig(), windowFlags: { ...flags } })
}
