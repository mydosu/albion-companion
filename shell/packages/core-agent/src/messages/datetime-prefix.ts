function padDatePart(value: number): string {
  return value.toString().padStart(2, '0')
}

/**
 * The commander and Albion live on Beijing time (UTC+8, no daylight saving).
 *
 * NOTICE:
 * The desktop host's Windows clock has been set to US Central time since
 * 2026-10-13, so the host-local prefix told the model it was 01:33 at 14:33
 * Beijing time and she kept calling the afternoon "early morning".
 * The prefix is the model's time anchor, so it follows the people, not the host.
 */
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000

/**
 * Formats a timestamp as `[YYYY-MM-DD HH:MM] ` in Beijing time (UTC+8),
 * whatever the host timezone is.
 *
 * Use when:
 * - Annotating user messages so the model has a concrete time anchor on
 *   every turn — historic and current user turns use the same shape so that
 *   prefix-cache stays valid when a "current" turn becomes "historic" on
 *   the next send.
 * - Not used for assistant messages — that caused the model to mirror the
 *   prefix into its own output.
 *
 * Returns:
 * - String including a trailing space, e.g. `"[2026-04-25 18:47] "`.
 *
 * Before:
 * - createdAt = 1777114020000  (2026-04-25 10:47 UTC)
 *
 * After:
 * - "[2026-04-25 18:47] "
 */
export function formatTimePrefix(createdAt: number): string {
  // Shift into Beijing wall-clock time and read it with UTC getters so the host timezone never applies.
  const date = new Date(createdAt + BEIJING_OFFSET_MS)
  const year = date.getUTCFullYear()
  const month = padDatePart(date.getUTCMonth() + 1)
  const day = padDatePart(date.getUTCDate())
  const hour = padDatePart(date.getUTCHours())
  const minute = padDatePart(date.getUTCMinutes())

  return `[${year}-${month}-${day} ${hour}:${minute}] `
}
