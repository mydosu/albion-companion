/** Resources belong to one start, including work that is still awaiting a model. */
export function createAlbionInteractionOwner() {
  const controller = new AbortController()
  const cleanups = new Set<() => void>()
  const { signal } = controller

  function onDispose(cleanup: () => void) {
    if (signal.aborted)
      cleanup()
    else
      cleanups.add(cleanup)
  }

  function timeout(callback: () => void, ms: number) {
    if (signal.aborted)
      return () => {}
    const id = setTimeout(() => {
      cleanups.delete(cancel)
      callback()
    }, ms)
    function cancel() {
      clearTimeout(id)
      cleanups.delete(cancel)
    }
    onDispose(cancel)
    return cancel
  }

  function interval(callback: () => void, ms: number) {
    if (!signal.aborted) {
      const id = setInterval(callback, ms)
      onDispose(() => clearInterval(id))
    }
  }

  function frame(callback: FrameRequestCallback) {
    if (signal.aborted)
      return
    const id = requestAnimationFrame((time) => {
      cleanups.delete(cancel)
      callback(time)
    })
    function cancel() {
      cancelAnimationFrame(id)
    }
    onDispose(cancel)
  }

  function delay(ms: number): Promise<boolean> {
    return new Promise((resolve) => {
      const cancelled = () => resolve(false)
      onDispose(cancelled)
      timeout(() => {
        cleanups.delete(cancelled)
        resolve(true)
      }, ms)
    })
  }

  function dispose() {
    if (signal.aborted)
      return
    controller.abort()
    for (const cleanup of cleanups)
      cleanup()
    cleanups.clear()
  }

  return { signal, onDispose, timeout, interval, frame, delay, dispose }
}
