import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * Tracks whether the commander is typing in any window, so Albion's wake doorbell
 * (`albion-pet.ts`) does not ring in the middle of a draft.
 *
 * Each input reports its own lease through `setDraft(owner, ...)`; a lease older than
 * 90 seconds no longer counts, so a renderer that closed without cleanup cannot block
 * the doorbell forever. The store is synced so the main window sees drafts typed in
 * the chatbox and spotlight windows.
 */
export const useAlbionDrafting = defineStore('albion-drafting', () => {
  const drafts = ref<Record<string, { sessionId: string, at: number }>>({})

  function setDraft(owner: string, sessionId: string, active: boolean) {
    if (!active) {
      delete drafts.value[owner]
      return
    }
    drafts.value[owner] = { sessionId, at: Date.now() }
  }

  function drafting(sessionId: string, now = Date.now()) {
    return Object.values(drafts.value).some(draft => draft.sessionId === sessionId && now - draft.at < 90_000)
  }

  return { drafts, setDraft, drafting }
}, { synced: { actions: ['setDraft'], state: true } })
