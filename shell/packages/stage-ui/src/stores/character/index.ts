import type { IntentHandle } from '@proj-airi/pipelines-audio'

import { createPetTagStreamFilter } from '@proj-airi/core-agent'
import { nanoid } from 'nanoid'
import { defineStore, storeToRefs } from 'pinia'
import { computed, reactive, ref } from 'vue'

import { useLlmmarkerParser } from '../../composables/llm-marker-parser'
import { useAiriCardStore } from '../modules'
import { useSpeechRuntimeStore } from '../speech-runtime'

export * from './notebook'
export * from './orchestrator'

export interface CharacterSparkNotifyReaction {
  id: string
  message: string
  createdAt: number
  sourceEventId?: string
  metadata?: Record<string, unknown>
}

interface StreamingReactionState {
  reaction: CharacterSparkNotifyReaction
  intent: IntentHandle
  parser: ReturnType<ParserFactory>
  petTagFilter: ReturnType<typeof createPetTagStreamFilter>
}

const MAX_REACTIONS = 200
type ParserFactory = typeof useLlmmarkerParser
let parserFactory: ParserFactory = useLlmmarkerParser

export function setCharacterLlmMarkerParserFactoryForTest(factory: ParserFactory | null) {
  parserFactory = factory ?? useLlmmarkerParser
}

export const useCharacterStore = defineStore('character', () => {
  const { activeCard, systemPrompt } = storeToRefs(useAiriCardStore())

  const name = computed(() => activeCard.value?.name ?? '')
  const ownerId = computed(() => activeCard.value?.name ?? 'default')

  const reactions = ref<CharacterSparkNotifyReaction[]>([])
  const streamingReactions = ref<Map<string, StreamingReactionState>>(new Map())
  const speechRuntimeStore = useSpeechRuntimeStore()

  // 这里的文本不经过聊天编排器，直接进切句器（TTS + 字幕），所以 `[[ ]]` 标记在这里削一次。
  // 记录下来的 reaction.message 保留原文：它只给 devtools 和编排器的上下文看，不上字幕、不朗读。
  async function emitTextOutput(text: string) {
    const intent = speechRuntimeStore.openIntent({
      ownerId: ownerId.value,
      priority: 'normal',
      behavior: 'queue',
    })
    const petTagFilter = createPetTagStreamFilter()

    const parser = parserFactory({
      onLiteral: async (literal) => {
        const speech = petTagFilter.push(literal)
        if (speech)
          intent.writeLiteral(speech)
      },
      onSpecial: async (special) => {
        if (special)
          intent.writeSpecial(special)
      },
    })

    await parser.consume(text)
    await parser.end()

    const rest = petTagFilter.flush()
    if (rest)
      intent.writeLiteral(rest)
    intent.writeFlush()
    intent.end()
  }

  function onSparkNotifyReactionStreamEvent(sparkEventId: string, chunk: string, options?: { metadata?: Record<string, unknown> }) {
    if (!streamingReactions.value.has(sparkEventId)) {
      const newReaction = reactive({
        id: nanoid(),
        message: '',
        createdAt: Date.now(),
        sourceEventId: sparkEventId,
        metadata: options?.metadata,
      }) satisfies CharacterSparkNotifyReaction

      const intent = speechRuntimeStore.openIntent({
        turnId: `spark:${sparkEventId}`,
        intentId: `spark:${sparkEventId}`,
        ownerId: ownerId.value,
        priority: 'high',
        behavior: 'interrupt',
      })

      const petTagFilter = createPetTagStreamFilter()
      const parser = parserFactory({
        onLiteral: async (literal) => {
          const speech = petTagFilter.push(literal)
          if (speech)
            intent.writeLiteral(speech)
        },
        onSpecial: async (special) => {
          if (special)
            intent.writeSpecial(special)
        },
      })

      streamingReactions.value.set(sparkEventId, { reaction: newReaction, intent, parser, petTagFilter })
    }

    const state = streamingReactions.value.get(sparkEventId)!
    state.reaction.message += chunk
    void state.parser.consume(chunk)
  }

  function onSparkNotifyReactionStreamEnd(sparkEventId: string, fullText: string, options?: { metadata?: Record<string, unknown> }) {
    const state = streamingReactions.value.get(sparkEventId)
    if (!state)
      return

    state.reaction.message = fullText
    recordSparkNotifyReaction(sparkEventId, fullText, { metadata: options?.metadata })

    void state.parser.end().then(() => {
      const rest = state.petTagFilter.flush()
      if (rest)
        state.intent.writeLiteral(rest)
      state.intent.writeFlush()
      state.intent.end()
      streamingReactions.value.delete(sparkEventId)
    })
  }

  function recordSparkNotifyReaction(sparkEventId: string, message: string, options?: { metadata?: Record<string, unknown> }) {
    const newReaction = {
      id: nanoid(),
      message,
      createdAt: Date.now(),
      sourceEventId: sparkEventId,
      metadata: options?.metadata,
    } satisfies CharacterSparkNotifyReaction

    reactions.value.push(newReaction)

    if (reactions.value.length > MAX_REACTIONS) {
      reactions.value.splice(0, reactions.value.length - MAX_REACTIONS)
    }
  }

  function clearReactions() {
    reactions.value = []
  }

  return {
    name,
    reactions,
    systemPrompt,

    recordSparkNotifyReaction,
    onSparkNotifyReactionStreamEvent,
    onSparkNotifyReactionStreamEnd,
    clearReactions,

    emitTextOutput,
  }
})
