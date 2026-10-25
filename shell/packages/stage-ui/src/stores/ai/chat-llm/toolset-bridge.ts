import type { ToolDefinition } from './tools'
import type { LlmToolsetPromptContribution } from './toolset-prompts'

/**
 * Request header that names the toolset-contributed tools in a chat request.
 *
 * The Albion Pi brain (`integrations/pi-brain/bin/shell_tools.py`) reads it.
 * The brain bridges only the tools named here, not every tool in `body.tools`.
 * When the brain calls one of them, it returns OpenAI `tool_calls`. xsai runs
 * the tool in this renderer and sends a `role: tool` result back.
 * Other providers ignore the header.
 */
export const TOOLSET_BRIDGE_HEADER = 'X-Albion-Toolset'

/**
 * The brain ignores larger headers. Its Python `http.server` also rejects a
 * header line over 64 KiB, which would fail the whole request.
 */
const MAX_HEADER_CHARS = 48 * 1024

interface ToolsetBridgeManifest {
  v: 1
  tools: string[]
  prompts: LlmToolsetPromptContribution[]
}

function encodeBase64Url(text: string) {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  for (const byte of bytes)
    binary += String.fromCharCode(byte)

  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
}

/**
 * Builds the toolset bridge header for one chat request.
 *
 * Only active tools that declare a toolset are listed. Prompts come from the
 * toolset prompt provider that has the same key. If the manifest is too large,
 * the prompts are dropped first. If it is still too large, no header is sent.
 *
 * @example
 * toolsetBridgeHeaders(
 *   [{ id: 'plugin:chess:play_chess', type: 'function', function: { name: 'play_chess', parameters: {} }, toolset: 'plugin-tools' }],
 *   { 'plugin-tools': [{ id: 'chess:prompt', content: 'Use SAN moves.' }] },
 * )
 * // => { 'X-Albion-Toolset': '<base64url of {"v":1,"tools":["play_chess"],"prompts":[...]}>' }
 */
export function toolsetBridgeHeaders(
  tools: ToolDefinition[],
  promptsByProvider: Record<string, LlmToolsetPromptContribution[]>,
): Record<string, string> {
  const names = new Set<string>()
  const toolsets = new Set<string>()
  for (const tool of tools) {
    if (!tool.toolset || tool.defaultActive === false)
      continue

    names.add(tool.function.name)
    toolsets.add(tool.toolset)
  }

  if (names.size === 0)
    return {}

  const prompts = [...toolsets]
    .flatMap(toolset => promptsByProvider[toolset] ?? [])
    .filter(prompt => prompt.content.trim().length > 0)
  const manifest: ToolsetBridgeManifest = { v: 1, tools: [...names], prompts }

  let encoded = encodeBase64Url(JSON.stringify(manifest))
  if (encoded.length > MAX_HEADER_CHARS && prompts.length > 0) {
    console.warn('[toolset-bridge] Toolset prompts are too large for the bridge header. Sending tool names only.')
    encoded = encodeBase64Url(JSON.stringify({ ...manifest, prompts: [] }))
  }
  if (encoded.length > MAX_HEADER_CHARS) {
    console.warn('[toolset-bridge] Too many bridged tools for the bridge header. The header is not sent.')
    return {}
  }

  return { [TOOLSET_BRIDGE_HEADER]: encoded }
}
