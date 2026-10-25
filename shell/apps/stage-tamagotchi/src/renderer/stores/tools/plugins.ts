import type { ExecutableTool } from '@proj-airi/stage-ui/stores/ai/chat-llm/tools'

import { errorMessageFrom } from '@moeru/std'
import { useElectronEventaInvoke } from '@proj-airi/electron-vueuse'
import { useLlmToolsStore } from '@proj-airi/stage-ui/stores/ai/chat-llm/tools'
import { useLlmToolsetPromptsStore } from '@proj-airi/stage-ui/stores/ai/chat-llm/toolset-prompts'
import { rawTool } from '@xsai/tool'
import { defineStore } from 'pinia'

import { electronPluginInvokeTool, electronPluginListXsaiTools } from '../../../shared/eventa/plugin/tools'

export const useTamagotchiPluginToolsStore = defineStore('tamagotchi-plugin-tools', () => {
  const llmToolsStore = useLlmToolsStore()
  const llmToolsetPromptsStore = useLlmToolsetPromptsStore()
  const listPluginXsaiToolDefinitions = useElectronEventaInvoke(electronPluginListXsaiTools)
  const invokePluginTool = useElectronEventaInvoke(electronPluginInvokeTool)
  const toolIdPrefix = 'plugin:'
  // Tools and prompts share this key, so the chat bridge header can pair them.
  const toolsetId = 'plugin-tools'

  function registeredToolIds() {
    return llmToolsStore.tools
      .filter(tool => tool.id.startsWith(toolIdPrefix))
      .map(tool => tool.id)
  }

  async function refresh() {
    const abortController = new AbortController()
    const timeout = setTimeout(() => abortController.abort(new Error(`Timed out after ${5_000}ms`)), 5_000)

    const definitions = await listPluginXsaiToolDefinitions(undefined, { signal: abortController.signal })
      .catch((error) => {
        console.warn(`[plugin-tools] Failed to list plugin xsai tools: ${errorMessageFrom(error) ?? 'Unknown error'}`)
        return { prompts: [], tools: [] }
      })
      .finally(() => {
        clearTimeout(timeout)
      })

    llmToolsetPromptsStore.registerToolsetPrompts(
      toolsetId,
      definitions.prompts.map(definition => ({
        id: `${definition.ownerExtensionId}:${definition.id}`,
        title: definition.prompt.title,
        content: definition.prompt.content,
      })),
    )

    const tools = definitions.tools.map((definition): ExecutableTool => ({
      ...rawTool({
        name: definition.name,
        description: definition.description,
        parameters: definition.parameters,
        execute: async input => invokePluginTool({
          ownerExtensionId: definition.ownerExtensionId,
          name: definition.name,
          input,
        }),
      }),
      id: `${toolIdPrefix}${definition.ownerExtensionId}:${definition.name}`,
      toolset: toolsetId,
    }))

    // NOTICE:
    // Requests de-duplicate tools by model-facing name, and the last registration wins
    // (see `resolveLlmTools`). Two plugins that expose the same name therefore shadow
    // each other. Report it so the shadowed plugin is not a silent no-op.
    const owners = new Map<string, string[]>()
    for (const definition of definitions.tools)
      owners.set(definition.name, [...owners.get(definition.name) ?? [], definition.ownerExtensionId])
    for (const [name, ownerIds] of owners) {
      if (ownerIds.length > 1)
        console.warn(`[plugin-tools] Plugins ${ownerIds.join(', ')} all expose tool "${name}"; only ${ownerIds.at(-1)} is used.`)
    }

    llmToolsStore.removeToolsByIds(...registeredToolIds())
    llmToolsStore.addTools(...tools)
  }

  function dispose() {
    llmToolsStore.removeToolsByIds(...registeredToolIds())
    llmToolsetPromptsStore.clearToolsetPrompts(toolsetId)
  }

  return {
    dispose,
    refresh,
  }
}, {
  synced: {
    actions: ['dispose', 'refresh'],
    state: false,
  },
})
