import type { DisplayModelURL } from '../display-models'

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ALBION_STAGE_MODEL_ID } from '../../albion/albion-defaults'
import { DisplayModelFormat, useDisplayModelsStore } from '../display-models'
import { useSettingsStageModel } from './stage-model'

vi.mock('@proj-airi/stage-shared/composables', async () => {
  const { refManualReset } = await import('@vueuse/core')

  return {
    useLocalStorageManualReset: (_key: string, value: string) => refManualReset(value),
  }
})

vi.mock('@vueuse/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@vueuse/core')>()

  return {
    ...actual,
    useEventListener: vi.fn(),
  }
})

describe('settings stage model store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  // https://github.com/moeru-ai/airi/issues/1984
  it('issue #1984: falls back to the default preset when a custom stage model is missing', async () => {
    const fallbackModel: DisplayModelURL = {
      id: ALBION_STAGE_MODEL_ID,
      format: DisplayModelFormat.Spine38Zip,
      type: 'url',
      url: 'https://example.com/preset-spine38.zip',
      name: 'Preset Spine 3.8',
      importedAt: 1,
    }

    const displayModelsStore = useDisplayModelsStore()
    const getDisplayModelSpy = vi.spyOn(displayModelsStore, 'getDisplayModel').mockImplementation(async (id) => {
      if (id === 'display-model-missing')
        return undefined
      if (id === fallbackModel.id)
        return fallbackModel
      return undefined
    })

    const store = useSettingsStageModel()
    store.stageModelSelected = 'display-model-missing'

    await store.initializeStageModel()

    expect(store.stageModelSelected).toBe(fallbackModel.id)
    expect(store.stageModelSelectedDisplayModel).toEqual(fallbackModel)
    expect(store.stageModelSelectedUrl).toBe(fallbackModel.url)
    expect(store.stageModelRenderer).toBe('spine38')
    expect(getDisplayModelSpy).toHaveBeenCalledWith('display-model-missing')
    expect(getDisplayModelSpy).toHaveBeenCalledWith(fallbackModel.id)
  })

  // 重置必须回到阿尔比恩自己的默认皮肤（albion-defaults.ts），不能退回上游默认。
  it('resetState restores the Albion default stage model', async () => {
    const displayModelsStore = useDisplayModelsStore()
    vi.spyOn(displayModelsStore, 'getDisplayModel').mockResolvedValue(undefined)

    const store = useSettingsStageModel()
    store.stageModelSelected = 'display-model-custom'

    await store.resetState()

    expect(store.stageModelSelected).toBe(ALBION_STAGE_MODEL_ID)
  })

  it('routes Tachie archives to the Tachie renderer', async () => {
    const tachieModel: DisplayModelURL = {
      id: 'tachie-model',
      format: DisplayModelFormat.TachieZip,
      type: 'url',
      url: 'https://example.com/character.tachie.zip',
      name: 'Tachie character',
      importedAt: 1,
    }
    const displayModelsStore = useDisplayModelsStore()
    vi.spyOn(displayModelsStore, 'getDisplayModel').mockResolvedValue(tachieModel)

    const store = useSettingsStageModel()
    store.stageModelSelected = tachieModel.id

    await store.initializeStageModel()

    expect(store.stageModelSelectedDisplayModel).toEqual(tachieModel)
    expect(store.stageModelSelectedUrl).toBe(tachieModel.url)
    expect(store.stageModelRenderer).toBe('tachie')
  })
})
