import type { Ref } from 'vue'

import { computed, ref } from 'vue'

/** Keeps the release tail in memory so PTT never changes the saved mic switch. */
export function usePushToTalk(savedEnabled: Ref<boolean>, interrupt: () => void) {
  const active = ref(false)
  const held = ref(false)
  const enabled = computed(() => savedEnabled.value || active.value)
  let releaseTimer: ReturnType<typeof setTimeout> | undefined

  function clearReleaseTimer() {
    clearTimeout(releaseTimer)
    releaseTimer = undefined
  }

  function press() {
    if (held.value)
      return
    clearReleaseTimer()
    held.value = true
    active.value = true
    interrupt()
  }

  function release() {
    if (!held.value)
      return
    held.value = false
    releaseTimer = setTimeout(() => {
      releaseTimer = undefined
      active.value = false
    }, 300)
  }

  function dispose() {
    clearReleaseTimer()
    held.value = false
    active.value = false
  }

  return { active, held, enabled, press, release, dispose }
}
