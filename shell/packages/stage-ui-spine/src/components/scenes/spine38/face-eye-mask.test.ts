import { describe, expect, it } from 'vitest'

import {
  ERASABLE_FACES,
  erasableFacesIn,
  eraseMaskedPixels,
  EYE_SLOT_NAMES,
  isEyeSlot,
  maskedAlpha,
  stripSlotTimelines,
} from './face-eye-mask'

describe('maskedAlpha', () => {
  it('scales alpha by the mask and leaves untouched pixels alone', () => {
    expect(maskedAlpha(255, 255)).toBe(0)
    expect(maskedAlpha(255, 0)).toBe(255)
    expect(maskedAlpha(128, 64)).toBe(96)
    expect(maskedAlpha(0, 200)).toBe(0)
  })
})

describe('eraseMaskedPixels', () => {
  it('only rewrites pixels where the mask is non-zero', () => {
    const rgba = new Uint8ClampedArray([10, 20, 30, 255, 40, 50, 60, 200, 70, 80, 90, 100])
    const mask = new Uint8ClampedArray([0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0])
    const changed = eraseMaskedPixels(rgba, mask)
    expect(changed).toBe(1)
    expect(Array.from(rgba)).toEqual([10, 20, 30, 255, 0, 0, 0, 0, 70, 80, 90, 100])
  })

  // ROOT CAUSE:
  //
  // The atlas is premultiplied. Scaling only alpha left RGB > A on half-erased pixels,
  // and PMA blending drew them as a bright ring around the eyes.
  //
  // We fixed this by scaling RGB by the same factor as A, so RGB <= A still holds.
  it('keeps premultiplied pixels valid on a partial erase', () => {
    const rgba = new Uint8Array([200, 100, 50, 200])
    eraseMaskedPixels(rgba, [128, 0, 0, 0])
    expect(Array.from(rgba)).toEqual([100, 50, 25, 100])
    expect(Math.max(rgba[0], rgba[1], rgba[2])).toBeLessThanOrEqual(rgba[3])
  })

  it('treats a short mask as zeros past its end', () => {
    const rgba = new Uint8ClampedArray([1, 2, 3, 255, 4, 5, 6, 255])
    const mask = new Uint8ClampedArray([255])
    expect(eraseMaskedPixels(rgba, mask)).toBe(1)
    expect(rgba[3]).toBe(0)
    expect(rgba[7]).toBe(255)
  })
})

describe('stripSlotTimelines', () => {
  const timeline = (slotIndex: number | null, kind: string) => ({ slotIndex, kind })

  it('drops the matching eye attachment timelines and keeps everything else in order', () => {
    const list = [timeline(87, 'AttachmentTimeline'), timeline(69, 'AttachmentTimeline'), timeline(69, 'DeformTimeline'), timeline(null, 'DrawOrderTimeline')]
    const out = stripSlotTimelines(list, t => t.slotIndex === 69 && t.kind === 'AttachmentTimeline')
    expect(out.map(t => `${t.slotIndex}:${t.kind}`)).toEqual(['87:AttachmentTimeline', '69:DeformTimeline', 'null:DrawOrderTimeline'])
  })

  it('returns a new array (caller assigns it back)', () => {
    const list = [timeline(1, 'AttachmentTimeline')]
    expect(stripSlotTimelines(list, () => false)).not.toBe(list)
  })
})

describe('isEyeSlot / erasableFacesIn', () => {
  it('knows the eye slots and not the face or blush slots', () => {
    expect(isEyeSlot('youyan-1')).toBe(true)
    expect(isEyeSlot('zuoyan-8')).toBe(true)
    expect(isEyeSlot('1')).toBe(false)
    expect(isEyeSlot('lianhong1')).toBe(false)
    expect(isEyeSlot(null)).toBe(false)
    expect(EYE_SLOT_NAMES).toHaveLength(17)
  })

  it('keeps only the faces the generator produced, in canonical order', () => {
    const manifest = { faces: { 11: { page: 'p', rect: [0, 0, 1, 1] as [number, number, number, number], file: 'mask-11.png' }, 9: { page: 'p', rect: [0, 0, 1, 1] as [number, number, number, number], file: 'mask-9.png' }, 5: { page: 'p', rect: [0, 0, 1, 1] as [number, number, number, number], file: 'mask-5.png' } } }
    expect(erasableFacesIn(manifest)).toEqual(['5', '11'])
    expect(erasableFacesIn(null)).toEqual([])
    expect(ERASABLE_FACES).not.toContain('9')
    expect(ERASABLE_FACES).not.toContain('10')
  })
})
