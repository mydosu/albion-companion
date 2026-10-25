import type { ChatHistoryItem } from '../../../../types/chat'

import en from '@proj-airi/i18n/locales/en'

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from 'vitest-browser-vue'
import { createI18n } from 'vue-i18n'

import ChatHistory from './history.vue'

import { getChatHistoryItemKey } from '../utils'

function createEnglishI18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: { en },
  })
}

const RESIZE_OBSERVER_LOOP_MESSAGE = 'ResizeObserver loop completed with undelivered notifications.'

let resizeObserverLoopErrors = 0

function countResizeObserverLoopError(event: Event) {
  if (event instanceof ErrorEvent && event.message === RESIZE_OBSERVER_LOOP_MESSAGE)
    resizeObserverLoopErrors++
}

function waitForFrames(count: number) {
  return new Promise<void>((resolve) => {
    const step = (remaining: number) => remaining === 0
      ? resolve()
      : requestAnimationFrame(() => step(remaining - 1))
    step(count)
  })
}

describe('chat history', () => {
  // NOTICE:
  // The package Vitest config hides this browser message because Virtua can report it for
  // long lists (see vitest.config.ts). This counter keeps the history's own observers
  // accountable. It is added through EventTarget.prototype because Vitest wraps
  // window.addEventListener, counts error listeners, and stops failing tests on real
  // uncaught errors while any counted listener exists.
  // Source: node_modules/@vitest/browser/dist/client/error-catcher.js.
  // Remove the prototype call when Vitest stops counting window error listeners.
  beforeAll(() => {
    EventTarget.prototype.addEventListener.call(window, 'error', countResizeObserverLoopError)
  })
  beforeEach(() => {
    resizeObserverLoopErrors = 0
  })
  afterAll(() => {
    EventTarget.prototype.removeEventListener.call(window, 'error', countResizeObserverLoopError)
  })

  // ROOT CAUSE:
  //
  // Virtua mounts history rows inside its own ResizeObserver callback. Each row's action
  // menu then observed the scroll viewport with a new observer, and the top fade recreated
  // one observer for the viewport plus every message container. Those new observations sit
  // above the depth that Chromium is still delivering, so it reported a loop error per mount.
  //
  // We fixed this by reading the viewport bounds on demand in useElementScroll and by giving
  // the top fade one stable viewport observer apart from the message containers.
  it.each(['desktop', 'mobile'] as const)('mounts a short %s history without ResizeObserver loop errors', async (variant) => {
    const screen = await render(ChatHistory, {
      props: {
        messages: [
          { id: 'user-1', role: 'user', content: 'Hello' },
          { id: 'assistant-1', role: 'assistant', content: 'Hi there', slices: [], tool_results: [] },
        ],
        variant,
        style: 'height: 240px; width: 320px;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await vi.waitFor(() => {
      expect(screen.container.querySelectorAll('.chat-message-item-visible')).toHaveLength(2)
    })
    await waitForFrames(3)

    expect(resizeObserverLoopErrors).toBe(0)
  })

  it.each([false, true])('hides stored ACT metadata without rewriting history (slices: %s)', async (withSlices) => {
    const content = '<|ACT:{"emotion":"happy"}|>Stored reply'
    const message: ChatHistoryItem = {
      role: 'assistant',
      content,
      slices: withSlices ? [{ type: 'text', text: content }] : [],
      tool_results: [],
    }
    const screen = await render(ChatHistory, {
      props: {
        messages: [message],
        // Production layouts bound the viewport. An unbounded one grows with Virtua's
        // first row measurement and resizes the viewport inside that delivery.
        style: 'height: 240px; width: 320px;',
      },
      global: { plugins: [createEnglishI18n()] },
    })
    await expect.element(screen.getByText('Stored reply', { exact: true })).toBeVisible()
    expect(screen.container.textContent).not.toContain('<|ACT')
    expect(message.content).toBe(content)
    if (withSlices)
      expect(message.slices[0]).toEqual({ type: 'text', text: content })
  })

  // ROOT CAUSE:
  //
  // The desktop chat needs the styled Reka viewport, but forcing its track to
  // stay mounted leaves an inert scrollbar visible when short content cannot scroll.
  // Reka's automatic visibility must own the track without changing the viewport.
  it('uses a Reka viewport without showing a scrollbar for a short desktop chat', async () => {
    const screen = await render(ChatHistory, {
      props: {
        messages: [{ id: 'user-1', role: 'user', content: 'Hello' }],
        style: 'height: 240px; width: 320px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await vi.waitFor(() => {
      expect(screen.container.querySelector('.chat-history-list')).not.toBeNull()
    })

    const history = screen.container.querySelector<HTMLElement>('.chat-history-list')
    expect(history).not.toBeNull()
    if (!history)
      throw new Error('Expected a chat history viewport.')

    const track = screen.container.querySelector<HTMLElement>('.scrollable-area-scrollbar--vertical')
    expect(history.matches('[data-reka-scroll-area-viewport]')).toBe(true)
    expect(track === null || track.dataset.state === 'hidden').toBe(true)
  })

  it('virtualizes long desktop history inside one Reka viewport', async () => {
    const messages: ChatHistoryItem[] = Array.from({ length: 100 }, (_, index) => ({
      id: `desktop-user-${index}`,
      role: 'user',
      content: `Desktop message ${index} `.repeat(index % 6 + 1),
      createdAt: index,
    }))

    const screen = await render(ChatHistory, {
      props: {
        messages,
        style: 'height: 240px; width: 320px;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await vi.waitFor(() => {
      const renderedMessages = screen.container.querySelectorAll('.chat-message-item')
      expect(renderedMessages.length).toBeGreaterThan(0)
      expect(renderedMessages.length).toBeLessThan(messages.length)
    })

    const history = screen.container.querySelector<HTMLElement>('.chat-history-list')
    expect(history).not.toBeNull()
    if (!history)
      throw new Error('Expected a desktop chat history viewport.')

    expect(history.matches('[data-reka-scroll-area-viewport]')).toBe(true)
    expect(screen.container.querySelectorAll('.chat-history-list')).toHaveLength(1)
    await vi.waitFor(() => {
      expect(screen.container.querySelector('.scrollable-area-scrollbar--vertical')).not.toBeNull()
      expect(history.scrollHeight).toBeGreaterThan(history.clientHeight)
      expect(screen.container.textContent).toContain('Desktop message 99')
    })

    history.scrollTop = 0
    history.dispatchEvent(new Event('scroll'))

    await vi.waitFor(() => {
      expect(screen.container.textContent).toContain('Desktop message 0')
    })
  })

  // ROOT CAUSE:
  //
  // Virtua keeps its internal content root at least as tall as the viewport, but
  // absolutely positioned messages still start at the top. A short mobile history
  // therefore leaves most of the chat area empty below a newly sent message.
  //
  // We fixed this by bottom-aligning short virtualized content while preserving
  // the existing overflow direction for longer history.
  it('bottom-aligns a newly sent mobile message and its streaming placeholder', async () => {
    const screen = await render(ChatHistory, {
      props: {
        messages: [{ id: 'user-1', role: 'user', content: 'hello' }],
        sending: true,
        streamingMessage: {
          id: 'assistant-1',
          role: 'assistant',
          content: '',
          slices: [],
          tool_results: [],
        },
        variant: 'mobile',
        style: 'height: 240px; width: 320px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await vi.waitFor(() => {
      expect(screen.container.querySelector('.chat-message-item-visible')).not.toBeNull()
    })

    const history = screen.container.querySelector<HTMLElement>('.chat-history-list')
    const messages = screen.container.querySelectorAll<HTMLElement>('.chat-message-item')
    expect(history).not.toBeNull()
    expect(messages).toHaveLength(2)
    if (!history || messages.length !== 2)
      throw new Error('Expected a sent message and its streaming placeholder.')

    const historyBottom = history.getBoundingClientRect().bottom
    expect(historyBottom - messages[1].getBoundingClientRect().bottom).toBeLessThanOrEqual(16)
    expect(historyBottom - messages[0].getBoundingClientRect().bottom).toBeLessThanOrEqual(64)
  })

  // ROOT CAUSE:
  //
  // Rendering every message keeps every backdrop-filter surface alive, even when
  // most of the history is outside the viewport. Long histories then cost more to
  // lay out and composite during fast mobile scrolling.
  //
  // We virtualize the history and mount only the viewport plus a small overscan area.
  it('virtualizes long histories and reveals messages inside the viewport', async () => {
    const messages: ChatHistoryItem[] = Array.from({ length: 100 }, (_, index) => ({
      id: `user-${index}`,
      role: 'user',
      content: `Message ${index} `.repeat(index % 6 + 1),
      createdAt: index,
    }))

    const screen = await render(ChatHistory, {
      props: {
        messages,
        variant: 'mobile',
        style: 'height: 240px; width: 320px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await vi.waitFor(() => {
      const renderedMessages = screen.container.querySelectorAll('.chat-message-item')
      expect(renderedMessages.length).toBeGreaterThan(0)
      expect(renderedMessages.length).toBeLessThan(messages.length)
      expect(screen.container.textContent).toContain('Message 99')
    })

    await vi.waitFor(() => {
      const visibleMessages = screen.container.querySelectorAll('.chat-message-item-visible')
      const hiddenMessages = screen.container.querySelectorAll('.chat-message-item:not(.chat-message-item-visible)')

      expect(visibleMessages.length).toBeGreaterThan(0)
      expect(hiddenMessages.length).toBeGreaterThan(0)
      expect(visibleMessages[0].classList.contains('opacity-100')).toBe(true)
      expect(visibleMessages[0].classList.contains('transition-opacity')).toBe(true)
      expect(hiddenMessages[0].classList.contains('opacity-0')).toBe(true)
    })

    const history = screen.container.querySelector<HTMLElement>('.chat-history-list')
    expect(history).not.toBeNull()
    if (!history)
      throw new Error('Expected a chat history viewport.')

    expect(history.matches('[data-reka-scroll-area-viewport]')).toBe(false)
    expect(screen.container.querySelectorAll('.chat-history-list')).toHaveLength(1)
    await vi.waitFor(() => {
      expect(history.scrollHeight).toBeGreaterThan(history.clientHeight)
    })
    expect(getComputedStyle(history).overflowY).toBe('auto')
    expect(screen.container.querySelector('.scrollable-area-scrollbar--vertical')).toBeNull()

    history.scrollTop = 0
    history.dispatchEvent(new Event('scroll'))

    await vi.waitFor(() => {
      expect(screen.container.textContent).toContain('Message 0')
    })
  })

  it('keeps a stable mask on each mobile message container', async () => {
    const screen = await render(ChatHistory, {
      props: {
        messages: [{ role: 'user', content: 'hello' }],
        variant: 'mobile',
        style: 'height: 240px; width: 320px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await vi.waitFor(() => {
      expect(screen.container.querySelector('.chat-message-item-container')).not.toBeNull()
    })

    const messageContainer = screen.container.querySelector<HTMLElement>('.chat-message-item-container')
    expect(messageContainer).not.toBeNull()
    if (!messageContainer)
      throw new Error('Expected a mobile chat message container.')

    expect(getComputedStyle(messageContainer).maskImage).not.toBe('none')
    await vi.waitFor(() => {
      expect(messageContainer.closest('.chat-message-item-visible')).not.toBeNull()
    })
  })

  // ROOT CAUSE:
  //
  // Cross-window synchronization can publish `sending` before it publishes the new stream.
  // The initial stream object has a timestamp but no message id, which rendered a short-lived bubble.
  //
  // We fixed this by rendering only a stream that has the stable id assigned to the assistant turn.
  it('does not render the initial empty stream while a synchronized send starts', async () => {
    const screen = await render(ChatHistory, {
      props: {
        messages: [],
        sending: true,
        streamingMessage: {
          role: 'assistant',
          content: '',
          slices: [],
          tool_results: [],
          createdAt: 1710000000000,
        },
        style: 'height: 240px; width: 320px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    expect(screen.container.querySelectorAll('.chat-message-item')).toHaveLength(0)
  })

  it('emits retry-message when the retry button is clicked for an error after a user message', async () => {
    const messages: ChatHistoryItem[] = [
      { role: 'user', content: 'hello' },
      { role: 'error', content: 'Remote sent 400 response' },
    ]

    const screen = await render(ChatHistory, {
      props: {
        messages,
        style: 'height: 480px; width: 480px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await screen.getByRole('button', { name: 'Retry' }).click()

    expect(screen.emitted('retryMessage')).toEqual([[
      {
        message: messages[1],
        index: 1,
        key: getChatHistoryItemKey(messages[1], 1),
      },
    ]])
  })

  it('does not render the retry button when the error is not preceded by a user message', async () => {
    const screen = await render(ChatHistory, {
      props: {
        messages: [
          { role: 'assistant', content: 'hello', slices: [], tool_results: [] },
          { role: 'error', content: 'Remote sent 400 response' },
        ],
        style: 'height: 480px; width: 480px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    expect(screen.container.textContent).not.toContain('Retry')
  })

  it('emits tool-call-rerun with message context when a tool call rerun button is clicked', async () => {
    const args = JSON.stringify({ location: 'Tokyo' })
    const assistantMessage: ChatHistoryItem = {
      role: 'assistant',
      content: '',
      slices: [
        {
          type: 'tool-call',
          toolCall: {
            toolCallId: 'call-weather',
            toolCallType: 'function',
            toolName: 'weather',
            args,
          },
        },
      ],
      tool_results: [],
      createdAt: 1710000000000,
    }
    const messages: ChatHistoryItem[] = [
      { role: 'user', content: 'weather in Tokyo' },
      assistantMessage,
    ]

    const screen = await render(ChatHistory, {
      props: {
        messages,
        style: 'height: 480px; width: 480px; overflow-y: auto;',
      },
      global: {
        plugins: [createEnglishI18n()],
      },
    })

    await screen.getByLabelText('Re-run tool call').click()

    expect(screen.emitted('toolCallRerun')).toEqual([[
      {
        message: assistantMessage,
        index: 1,
        key: getChatHistoryItemKey(assistantMessage, 1),
        toolCallId: 'call-weather',
        toolName: 'weather',
        args,
      },
    ]])
  })
})
