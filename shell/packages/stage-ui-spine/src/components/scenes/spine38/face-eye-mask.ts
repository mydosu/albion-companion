/**
 * 脸图「擦眼」：把脸上画出来的眼睛从贴图里抠掉，让底下真正的眼槽（会眨、会看人）从洞里透出来。
 *
 * 静态数据（mask）由 `pet/tools/spine_face_eye_masks.py` 离线生成，放在模型素材目录旁边的
 * `albion-eye-masks/`（本地文件、不入库）。运行时只做一件事：在 mask 矩形内逐像素
 * `a = round(a*(255-m)/255)`。**不要用 canvas 的 filter / 合成模式**——2026-10-26 实测
 * `ctx.filter + destination-out` 会把整张 4096² 画布的 alpha 一起压掉，软腮红当场消失。
 *
 * 脸 9（漩涡眼）和 10（一睁一闭）是表情眼，不擦。
 */

/** 会抢眼睛的槽：擦过眼的脸，动画里这些槽的 AttachmentTimeline 要去掉，真眼才留得住。 */
export const EYE_SLOT_NAMES = [
  'youyan-1',
  'youyan-2',
  'youyan-3',
  'youyan-4',
  'youyan-5',
  'youyan-6',
  'youyan-7',
  'youyan-8',
  'youyan-9',
  'zuoyan-1',
  'zuoyan-2',
  'zuoyan-3',
  'zuoyan-4',
  'zuoyan-5',
  'zuoyan-6',
  'zuoyan-7',
  'zuoyan-8',
] as const

/** 有擦眼 mask 的脸（普通睁眼）。9、10 不在里面。 */
export const ERASABLE_FACES = ['1', '2', '3', '4', '5', '6', '7', '8', '11'] as const

export interface EyeMaskEntry {
  /** atlas 页文件名（一张页可能被多张脸共用）。 */
  page: string
  /** mask 在页面上的位置：[x, y, w, h]。 */
  rect: [number, number, number, number]
  /** 同目录下的 mask 文件名（灰度 = 擦除强度）。 */
  file: string
  maskPx?: number
}

export interface EyeMaskManifest {
  faces: Record<string, EyeMaskEntry>
}

/** 槽名是不是眼槽（供 timeline 过滤用）。 */
export function isEyeSlot(name: string | null | undefined): boolean {
  return !!name && (EYE_SLOT_NAMES as readonly string[]).includes(name)
}

/**
 * 一个像素被 mask 削过之后的 alpha。
 *
 * @example
 * maskedAlpha(255, 255) // => 0
 * maskedAlpha(255, 0) // => 255
 * maskedAlpha(128, 64) // => 96
 */
export function maskedAlpha(alpha: number, mask: number): number {
  if (!mask || !alpha)
    return alpha
  return Math.round(alpha * (255 - mask) / 255)
}

/**
 * 就地把 mask 应用到一块 RGBA 像素上，返回被改动的像素数。
 *
 * 两个数组长度必须一致（同一个矩形）；不做任何边界猜测，越界的 mask 视为 0。
 */
export function eraseMaskedPixels(rgba: Uint8ClampedArray | number[], mask: Uint8ClampedArray | number[], alphaOffset = 3): number {
  let changed = 0
  const n = Math.min(rgba.length, mask.length)
  for (let i = 0; i < n; i += 4) {
    const m = mask[i]
    if (!m)
      continue
    const a0 = rgba[i + alphaOffset]
    const a1 = maskedAlpha(a0, m)
    if (a1 !== a0) {
      rgba[i + alphaOffset] = a1
      changed++
    }
  }
  return changed
}

/** 丢掉满足 `isDrop` 的时间线，保持原顺序（纯函数，便于单测）。 */
export function stripSlotTimelines<T>(timelines: readonly T[], isDrop: (t: T) => boolean): T[] {
  return timelines.filter(t => !isDrop(t))
}

/** manifest 里可用的脸（按 ERASABLE_FACES 顺序）。 */
export function erasableFacesIn(manifest: EyeMaskManifest | null | undefined): string[] {
  if (!manifest?.faces)
    return []
  return ERASABLE_FACES.filter(f => !!manifest.faces[f])
}
