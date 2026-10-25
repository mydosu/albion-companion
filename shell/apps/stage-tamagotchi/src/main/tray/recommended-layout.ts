import type { Rectangle } from 'electron'

import type { StageLayoutSnapshot } from '../../shared/eventa'

import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/** Live2D 阿尔比恩（命中区校准就是按它的推荐尺寸来的） */
export const LIVE2D_ALBION_MODEL_ID = 'preset-live2d-albion'
/** Spine 3.8 阿尔比恩（晨光）：它自己的一套推荐布局，跟 Live2D 那套分开存（指挥官 2026-09-17） */
export const SPINE_ALBION_MODEL_ID = 'preset-spine38-albion'

/** live2d / spine 各自对应一个模型；spine-b 是晨光的手动预设 B，只读不自动保存 */
export type RecommendedKind = 'live2d' | 'spine' | 'spine-b'
export interface RecommendedView { width: number, height: number, x: number, y: number, scale: string, position: string }

/** 推荐布局的默认值（指挥官 2026-09-17 定稿那一刻）；之后由自动保存覆盖，托盘不提供手动「存为」项 */
const RECOMMENDED_DEFAULT: RecommendedView = {
  width: 964,
  height: 799,
  x: 1,
  y: 6,
  scale: '1.3',
  position: '{"x":-1,"y":46}',
}
const RECOMMENDED_SPINE_DEFAULT: RecommendedView = {
  width: 963,
  height: 803,
  x: -2,
  y: 2,
  scale: '0.7026',
  position: '{"x":788.5999755859375,"y":1635.8000793457031}',
}

/** 只认阿尔比恩这两个模型；别的模型不套、不存推荐布局，免得把推荐布局覆盖成无关的值 */
export function recommendedKindOfModel(modelId: string | null | undefined): 'live2d' | 'spine' | undefined {
  if (modelId === LIVE2D_ALBION_MODEL_ID)
    return 'live2d'
  if (modelId === SPINE_ALBION_MODEL_ID)
    return 'spine'
  return undefined
}

/** 模型视图要写哪几个 localStorage 键：live2d 用 scale+position，spine（晨光）用 scale+x/y */
export function recommendedModelPairs(kind: RecommendedKind, scale: string, position: string): Array<[string, string]> {
  if (kind !== 'live2d') {
    let p: { x?: number, y?: number } = {}
    try {
      p = JSON.parse(position || '{}')
    }
    catch {}
    return [
      ['settings/spine38/scale', String(scale)],
      ['settings/spine38/x', String(p.x ?? 0)],
      ['settings/spine38/y', String(p.y ?? 0)],
    ]
  }
  return [['settings/live2d/scale', String(scale)], ['settings/live2d/position', String(position)]]
}

/**
 * 推荐布局的读写和自动保存。
 *
 * 状态：
 * - 推荐布局文件（持久化）：userData 下，live2d = recommended-view.json，晨光 = recommended-view-spine.json，
 *   预设 B = recommended-view-spine-b.json；没存过就用内置默认。
 * - 舞台布局快照（运行时）：主窗渲染端经 electronStageLayoutChanged 推来的最新一份，存档时取模型缩放/位置。
 * - 上次存的值（缓存）：给亚像素过滤和"没变就不写"用。
 *
 * 自动保存（2026-09-23 起）：一键关闭.bat 是 taskkill /F（before-quit 不跑），所以布局一变就静默存盘。
 * 以前靠 2 秒轮询兜底；现在由主窗 moved/resized 和渲染端布局事件调 scheduleAutosave，停下 autosaveDelayMs 后存一次。
 * 防抖窗口内被硬杀会丢最后这一次挪动（和以前轮询的损失相当）。
 */
export function setupRecommendedLayouts(options: {
  userDataDir: string
  getBounds: () => Rectangle
  /** 这个时刻（epoch ms）之前不自动保存：启动时窗口/模型还在被摆位，存了会把过渡态写进预设 */
  autosaveAfter: number
  /** @default 1500 */
  autosaveDelayMs?: number
}) {
  const autosaveDelayMs = options.autosaveDelayMs ?? 1500
  const lastLayoutSig = new Map<RecommendedKind, string>()
  const lastLayoutView = new Map<RecommendedKind, RecommendedView>()
  let snapshot: StageLayoutSnapshot | undefined
  let autosaveTimer: ReturnType<typeof setTimeout> | undefined

  const fileOf = (kind: RecommendedKind) =>
    join(options.userDataDir, `recommended-view${kind === 'live2d' ? '' : `-${kind}`}.json`)
  const defaultOf = (kind: RecommendedKind) =>
    (kind === 'live2d' ? RECOMMENDED_DEFAULT : RECOMMENDED_SPINE_DEFAULT)

  function read(kind: RecommendedKind = 'live2d'): RecommendedView {
    try {
      const raw = JSON.parse(readFileSync(fileOf(kind), 'utf8')) as Partial<RecommendedView>
      return { ...defaultOf(kind), ...raw }
    }
    catch {
      return { ...defaultOf(kind) }
    }
  }

  /**
   * 把"此刻的布局"存为推荐：窗口大小/位置取窗口实际值，模型缩放/位置取最新快照。
   * silent=true（自动保存）时值没变就不写。返回写入的值；没写（没变或写盘失败）返回 undefined。
   */
  function save(kind: RecommendedKind, opts: { silent?: boolean } = {}): RecommendedView | undefined {
    const b = options.getBounds()
    const dft = defaultOf(kind)
    let scale = dft.scale
    let position = dft.position
    // 快照里缺的键沿用默认值（和以前 localStorage 读不到时一样）
    if (kind !== 'live2d') {
      if (snapshot?.spine.scale)
        scale = snapshot.spine.scale
      position = JSON.stringify({ x: Number(snapshot?.spine.x ?? 0), y: Number(snapshot?.spine.y ?? 0) })
    }
    else {
      if (snapshot?.live2d.scale)
        scale = snapshot.live2d.scale
      if (snapshot?.live2d.position)
        position = snapshot.live2d.position
    }
    // 亚像素抖动过滤：这台机器上 applyWindowSize(setBounds) 之后 getBounds() 的回读会比设定值大 1~2px
    // （机制未完全定论——DPI 取整模型已用算术否掉：125% 下整数都是不动点）。不过滤的话，自动保存会把这
    // 点差值写进预设 ⇒ 每次启动窗口被“卷大”一点（实测 974×1539 → 975×1541）。
    // 只压 ≤3px 的抖动：位置没变、尺寸差 ≤3px ⇒ 沿用上一次存的值；真拖动（≥4px）照存。
    const view: RecommendedView = { width: b.width, height: b.height, x: b.x, y: b.y, scale, position }
    const prev = lastLayoutView.get(kind) ?? read(kind)
    if (opts.silent && view.x === prev.x && view.y === prev.y
      && Math.abs(view.width - prev.width) <= 3 && Math.abs(view.height - prev.height) <= 3) {
      view.width = prev.width
      view.height = prev.height
    }
    const sig = JSON.stringify(view)
    if (opts.silent && lastLayoutSig.get(kind) === sig)
      return undefined
    try {
      writeFileSync(fileOf(kind), JSON.stringify(view, null, 2), 'utf8')
    }
    catch (e) {
      console.warn('[tray] 写推荐布局失败', e)
      return undefined
    }
    lastLayoutSig.set(kind, sig)
    lastLayoutView.set(kind, view)
    return view
  }

  function autosave() {
    autosaveTimer = undefined
    const kind = recommendedKindOfModel(snapshot?.modelId)
    if (kind)
      save(kind, { silent: true })
  }

  /** 布局可能变了：重新计时，停下 autosaveDelayMs 后存一次；启动保护期内的请求顺延到保护期结束 */
  function scheduleAutosave() {
    clearTimeout(autosaveTimer)
    autosaveTimer = setTimeout(autosave, Math.max(autosaveDelayMs, options.autosaveAfter - Date.now()))
  }

  /** 收下渲染端推来的快照；返回上一份快照的模型 id（没收到过为 undefined），给调用方判断模型是否切换 */
  function updateSnapshot(next: StageLayoutSnapshot): string | null | undefined {
    const previousModelId = snapshot?.modelId
    snapshot = next
    return previousModelId
  }

  return {
    read,
    save,
    scheduleAutosave,
    updateSnapshot,
    /** 最新快照里的模型 id；还没收到快照时为 undefined */
    currentModelId: () => snapshot?.modelId,
    dispose: () => {
      clearTimeout(autosaveTimer)
      autosaveTimer = undefined
    },
  }
}
