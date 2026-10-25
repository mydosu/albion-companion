import type { ToolDefinition } from './tools'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { TOOLSET_BRIDGE_HEADER, toolsetBridgeHeaders } from './toolset-bridge'

function decodeManifest(headers: Record<string, string>) {
  const encoded = headers[TOOLSET_BRIDGE_HEADER].replaceAll('-', '+').replaceAll('_', '/')
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded), char => char.charCodeAt(0))))
}

function definition(name: string, extra: Partial<ToolDefinition> = {}): ToolDefinition {
  return {
    id: `plugin:owner:${name}`,
    type: 'function',
    function: { name, parameters: { type: 'object', properties: {} } },
    ...extra,
  }
}

describe('toolsetBridgeHeaders', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('sends no header when no active tool declares a toolset', () => {
    expect(toolsetBridgeHeaders([definition('builtIn_debugRandomNumber')], {})).toEqual({})
    expect(toolsetBridgeHeaders([definition('play_chess', { toolset: 'plugin-tools', defaultActive: false })], {})).toEqual({})
  })

  it('lists each toolset tool name once and pairs it with that toolset prompts only', () => {
    const headers = toolsetBridgeHeaders([
      definition('play_chess', { toolset: 'plugin-tools' }),
      definition('play_chess', { id: 'plugin:other:play_chess', toolset: 'plugin-tools' }),
      definition('web_search'),
    ], {
      'plugin-tools': [
        { id: 'chess:prompt', title: 'Chess', content: 'Use SAN moves.' },
        { id: 'empty:prompt', content: '   ' },
      ],
      'web-search': [{ id: 'search', content: 'Search guidance.' }],
    })

    expect(decodeManifest(headers)).toEqual({
      v: 1,
      tools: ['play_chess'],
      prompts: [{ id: 'chess:prompt', title: 'Chess', content: 'Use SAN moves.' }],
    })
  })

  it('keeps non-ASCII prompts intact through the header encoding', () => {
    const headers = toolsetBridgeHeaders([definition('play_chess', { toolset: 'plugin-tools' })], {
      'plugin-tools': [{ id: 'p', content: '新开局不要传 fen。🙂' }],
    })

    expect(headers[TOOLSET_BRIDGE_HEADER]).toMatch(/^[\w-]+$/)
    expect(decodeManifest(headers).prompts[0].content).toBe('新开局不要传 fen。🙂')
  })

  it('drops prompts first, then the whole header, when the manifest is too large', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const tools = [definition('play_chess', { toolset: 'plugin-tools' })]
    const promptOnly = toolsetBridgeHeaders(tools, { 'plugin-tools': [{ id: 'huge', content: 'x'.repeat(60_000) }] })

    expect(decodeManifest(promptOnly)).toEqual({ v: 1, tools: ['play_chess'], prompts: [] })

    const manyTools = Array.from({ length: 3_000 }, (_, index) => definition(`plugin_tool_with_a_long_name_${index}`, { toolset: 'plugin-tools' }))

    expect(toolsetBridgeHeaders(manyTools, {})).toEqual({})
    expect(warn).toHaveBeenCalledTimes(2)
  })
})
