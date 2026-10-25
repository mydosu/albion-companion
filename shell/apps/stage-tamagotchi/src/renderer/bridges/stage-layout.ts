import type { StageLayoutSnapshot } from '../../shared/eventa'

import { getElectronEventaContext } from '@proj-airi/electron-vueuse'
import { useStorage, watchDebounced } from '@vueuse/core'
import { watch } from 'vue'

import { electronStageLayoutChanged, electronStageLayoutRequest } from '../../shared/eventa'

/**
 * Model scale and position change on every pointer move while the user drags
 * or zooms. Main debounces the save again, so this only limits IPC traffic.
 */
const VIEW_CHANGE_DEBOUNCE_MS = 300

/**
 * Pushes the main Stage layout to the main process.
 *
 * Use when:
 * - Initializing the main Stage window (route `/`) only; other windows share the
 *   same localStorage, and main ignores snapshots from other senders anyway.
 *
 * The tray applies the recommended layout when the Albion model changes and
 * saves the layout after changes. This replaces its 2-second
 * `executeJavaScript` polling.
 *
 * The refs only observe storage. They use `writeDefaults: false` and a `null`
 * default, so they never write and keep the raw stored strings. vueuse
 * dispatches a `storage` event for same-document writes, and the browser
 * dispatches one for writes from other windows, so both paths update the refs.
 *
 * A model change is sent at once, so the tray can apply the layout without
 * delay. Scale and position changes are debounced. Main can also send
 * {@link electronStageLayoutRequest} to get one snapshot now; it does that after
 * each page load, because the first snapshot can arrive before main listens.
 */
export function initializeStageLayoutBridge() {
  const context = getElectronEventaContext()
  const observe = (key: string) => useStorage<string | null>(key, null, localStorage, { writeDefaults: false })

  const modelId = observe('settings/stage/model')
  const live2dScale = observe('settings/live2d/scale')
  const live2dPosition = observe('settings/live2d/position')
  const spineScale = observe('settings/spine38/scale')
  const spineX = observe('settings/spine38/x')
  const spineY = observe('settings/spine38/y')

  function emitSnapshot() {
    const snapshot: StageLayoutSnapshot = {
      modelId: modelId.value,
      live2d: { scale: live2dScale.value, position: live2dPosition.value },
      spine: { scale: spineScale.value, x: spineX.value, y: spineY.value },
    }
    context.emit(electronStageLayoutChanged, snapshot)
  }

  watch(modelId, emitSnapshot)
  watchDebounced([live2dScale, live2dPosition, spineScale, spineX, spineY], emitSnapshot, { debounce: VIEW_CHANGE_DEBOUNCE_MS })
  context.on(electronStageLayoutRequest, emitSnapshot)
  emitSnapshot()
}
