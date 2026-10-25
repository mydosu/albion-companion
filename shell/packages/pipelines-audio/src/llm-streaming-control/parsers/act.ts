import type { LlmStreamingControlParser, LlmStreamingControlTokenAct } from '../types'

// `<|ACT {...}|>`（上游写法）和 `<|ACT:{...}|>`（阿尔比恩的脑一直这么写）都认
const actTokenPattern = /^<\|ACT(?:\s*:|\s)([\s\S]*)\|>$/

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Creates the parser for `<|ACT {...}|>` / `<|ACT:{...}|>` streaming-control tokens.
 *
 * Use when:
 * - Loading the built-in performance action control
 *
 * Expects:
 * - The token body is a JSON object literal
 *
 * Returns:
 * - Parsed action data with no side effects
 */
export function tokenAct(): LlmStreamingControlParser<LlmStreamingControlTokenAct> {
  return {
    name: 'ACT',
    match(special) {
      return actTokenPattern.test(special.trim())
    },
    parse(special) {
      const rawPayload = actTokenPattern.exec(special.trim())?.[1].trim() ?? ''

      let parsed: unknown
      try {
        parsed = JSON.parse(rawPayload)
      }
      catch {
        return undefined
      }

      if (!isPlainObject(parsed)) {
        return undefined
      }

      return {
        type: 'act',
        payload: parsed,
      }
    },
  }
}
