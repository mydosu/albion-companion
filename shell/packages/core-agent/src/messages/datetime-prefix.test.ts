import { describe, expect, it } from 'vitest'

import { formatTimePrefix } from './datetime-prefix'

/** Unix ms for a Beijing (UTC+8) wall-clock time, independent of the host timezone. */
function beijing(year: number, monthIndex: number, day: number, hour: number, minute: number, second = 0): number {
  return Date.UTC(year, monthIndex, day, hour - 8, minute, second)
}

/**
 * @example
 * formatTimePrefix(beijing(2026, 3, 25, 18, 47))
 */
describe('formatTimePrefix', () => {
  /**
   * @example
   * Timestamp prefixes use `[YYYY-MM-DD HH:MM] ` with a trailing space.
   */
  it('wraps `[YYYY-MM-DD HH:MM]` with trailing space', () => {
    expect(formatTimePrefix(beijing(2026, 3, 25, 18, 47))).toBe('[2026-04-25 18:47] ')
  })

  /**
   * @example
   * Single-digit date parts are zero-padded.
   */
  it('zero-pads month, day, hour, and minute', () => {
    expect(formatTimePrefix(beijing(2026, 0, 5, 3, 7))).toBe('[2026-01-05 03:07] ')
  })

  /**
   * @example
   * Same-minute timestamps share the same prompt prefix.
   */
  it('shares the same prefix across timestamps in the same minute', () => {
    expect(formatTimePrefix(beijing(2026, 3, 25, 18, 47, 12))).toBe(formatTimePrefix(beijing(2026, 3, 25, 18, 47, 58)))
  })

  // ROOT CAUSE:
  //
  // The desktop host runs on US Central time, and the prefix used host-local getters.
  // 14:33 in Beijing (06:33 UTC) became `[2026-10-17 01:33]`, so the model called the afternoon "early morning".
  //
  // We fixed this by always formatting in Beijing time (UTC+8).
  it('uses Beijing time even across the UTC day boundary', () => {
    expect(formatTimePrefix(Date.UTC(2026, 9, 17, 6, 33))).toBe('[2026-10-17 14:33] ')
    expect(formatTimePrefix(Date.UTC(2026, 9, 17, 17, 5))).toBe('[2026-10-18 01:05] ')
  })
})
