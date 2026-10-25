// @vitest-environment jsdom

import { afterEach, describe, expect, it } from 'vitest'
import { nextTick, watch } from 'vue'

import { useLocalStorageManualReset } from '.'

// Node 25 defines a stub `localStorage` global that shadows jsdom's implementation,
// so inject storage the same way the neighbouring versioned-storage test does.
class MemoryStorage {
  #items = new Map<string, string>()

  getItem(key: string) {
    return this.#items.get(key) ?? null
  }

  removeItem(key: string) {
    this.#items.delete(key)
  }

  setItem(key: string, value: string) {
    this.#items.set(key, value)
  }
}

const storage = new MemoryStorage()

afterEach(() => {
  storage.removeItem('cards')
})

describe('useLocalStorageManualReset', () => {
  // ROOT CAUSE:
  //
  // A synchronized store replaced a Map with a structured clone. Persistence
  // serialized that value, then the storage ref reflected another Map clone
  // into the store. Pinia saw the reflection as a new direct mutation and
  // published another domain snapshot.
  //
  // We fixed this by making `listenToStorageChanges: false` one-way: state is
  // still persisted, but the storage ref cannot write back into live state.
  it('does not reflect persisted values when storage listening is disabled', async () => {
    const state = useLocalStorageManualReset('cards', new Map<string, string>(), {
      listenToStorageChanges: false,
      storage,
    })
    let changes = 0
    const stop = watch(state, () => {
      changes += 1
    }, { flush: 'sync' })

    state.value = new Map([['card-1', 'ReLU']])
    await nextTick()

    expect(changes).toBe(1)
    expect(state.value).toEqual(new Map([['card-1', 'ReLU']]))
    stop()
  })
})
