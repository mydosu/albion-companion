import type { ChatHistoryItem } from '../../../types/chat'

import { stripAlbionAct } from '../../../albion/albion-act'

function isTextPart(part: unknown): part is { type: 'text', text?: string } {
  return typeof part === 'object'
    && part !== null
    && 'type' in part
    && part.type === 'text'
    && 'text' in part
}

function getTextFromContentParts(parts: unknown[]): string {
  return parts.reduce<string[]>((texts, part) => {
    if (!isTextPart(part))
      return texts

    const text = part.text?.trim()
    if (text)
      texts.push(text)

    return texts
  }, []).join('\n\n')
}

export function getChatHistoryItemCopyText(message: ChatHistoryItem): string {
  if (message.role === 'error')
    return message.content

  if (message.role === 'assistant') {
    if (message.slices?.length) {
      const text = message.slices
        .filter(slice => slice.type === 'text')
        .map(slice => stripAlbionAct(slice.text))
        .filter(Boolean)
        .join('\n\n')

      if (text)
        return text
    }

    if (typeof message.content === 'string')
      return stripAlbionAct(message.content)

    if (Array.isArray(message.content)) {
      const text = stripAlbionAct(getTextFromContentParts(message.content))

      if (text)
        return text

      return message.content.map(entry => JSON.stringify(entry)).join('\n')
    }

    return ''
  }

  if (typeof message.content === 'string')
    return message.content

  if (Array.isArray(message.content)) {
    const text = getTextFromContentParts(message.content)

    if (text)
      return text

    return message.content.map(entry => JSON.stringify(entry)).join('\n')
  }

  return ''
}

export function getChatHistoryItemKey(message: ChatHistoryItem | undefined, index: number): string | number {
  if (!message)
    return index

  if (message.id)
    return message.id

  if (message.createdAt != null)
    return `${message.role}:${message.createdAt}:${index}`

  return `${message.role}:${index}`
}
