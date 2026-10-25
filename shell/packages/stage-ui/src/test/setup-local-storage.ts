/**
 * Node 25 exposes a `localStorage` stub without Storage methods, and Vitest's
 * jsdom environment cannot replace a global that already exists. Node tests for
 * these stores only need in-memory persistence, so install a working Storage.
 */
const items = new Map<string, string>()

Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: {
    get length() {
      return items.size
    },
    clear: () => items.clear(),
    getItem: (key: string) => items.get(key) ?? null,
    key: (index: number) => [...items.keys()][index] ?? null,
    removeItem: (key: string) => items.delete(key),
    setItem: (key: string, value: string) => items.set(key, String(value)),
  } satisfies Storage,
})
