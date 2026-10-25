import { describe, expect, it } from 'vitest'

import { formatTimePrefix } from './datetime-prefix'

/** Unix ms for a Beijing (UTC+8) wall-clock time; the prefix is always Beijing time. */
function beijing(year: number, monthIndex: number, day: number, hour: number, minute: number, second = 0): number {
  return Date.UTC(year, monthIndex, day, hour - 8, minute, second)
}

describe('formatTimePrefix', () => {
  it('wraps `[YYYY-MM-DD HH:MM]` with trailing space', () => {
    expect(formatTimePrefix(beijing(2026, 3, 25, 18, 47))).toBe('[2026-04-25 18:47] ')
  })

  it('zero-pads month, day, hour, minute', () => {
    expect(formatTimePrefix(beijing(2026, 0, 5, 3, 7))).toBe('[2026-01-05 03:07] ')
  })

  it('produces stable output for the same input (cache-friendly)', () => {
    const ts = beijing(2026, 3, 25, 18, 47)
    expect(formatTimePrefix(ts)).toBe(formatTimePrefix(ts))
  })

  it('produces different output across day boundaries (lets the model see day changes)', () => {
    const day1 = beijing(2026, 3, 25, 12, 0)
    const day2 = beijing(2026, 3, 26, 12, 0)
    expect(formatTimePrefix(day1)).not.toBe(formatTimePrefix(day2))
    expect(formatTimePrefix(day1)).toContain('2026-04-25')
    expect(formatTimePrefix(day2)).toContain('2026-04-26')
  })

  it('shares the same prefix across timestamps in the same minute (KV-cache stable)', () => {
    expect(formatTimePrefix(beijing(2026, 3, 25, 18, 47, 12))).toBe(formatTimePrefix(beijing(2026, 3, 25, 18, 47, 58)))
  })
})
