import type { Ref } from 'vue'

import { useIntervalFn, useWindowFocus } from '@vueuse/core'
import { nanoid } from 'nanoid'
import { onScopeDispose, watch } from 'vue'

import { useChatSessionStore } from '../stores/chat/session-store'
import { useAlbionDrafting } from './albion-drafting'

/** Lease each input separately; a closed renderer cannot hold the doorbell gate forever. */
export function useAlbionDraftActivity(draft: Ref<string>) {
  const drafting = useAlbionDrafting()
  const session = useChatSessionStore()
  const focused = useWindowFocus()
  const owner = nanoid()
  const update = () => drafting.setDraft(owner, session.activeSessionId, focused.value && !!draft.value.trim())
  watch([draft, focused, () => session.activeSessionId], update, { immediate: true, flush: 'sync' })
  useIntervalFn(update, 30_000)
  onScopeDispose(() => drafting.setDraft(owner, session.activeSessionId, false))
}
