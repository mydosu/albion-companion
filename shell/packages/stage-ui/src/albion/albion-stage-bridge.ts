import type { StreamingSessionSnapshot } from '../libs/speech/tts-session'

import { ALBION_ACT_RE } from './albion-act'
import { ALBION_STREAM_TTS_BASE, ALBION_STREAM_TTS_MODEL, ALBION_STREAM_TTS_PROVIDER_ID, ALBION_TTS_MODEL } from './albion-defaults'

/**
 * Stage.vue 的流式 TTS 入口先问这里：只有阿尔比恩的本机 provider 走
 * {@link buildAlbionStreamingSnapshot}，其它 provider 的流式逻辑保持上游原样。
 */
export function isAlbionStreamTtsProvider(providerId: string | null | undefined): boolean {
  return providerId === ALBION_STREAM_TTS_PROVIDER_ID
}

/**
 * 本机流式服务（tts/stream/stream_server.py）的会话参数。
 * 调用方只提供 Stage 自己的状态（voiceType、ownerId、onImmediateSpecial），
 * 其余字段由本机服务决定，所以不再依赖 Stage.vue 里的 provider 配置。
 *
 * `token` 是占位值：本机服务不校验它，唯一作用是绕过壳的 `getAuthToken()` 门槛
 * （没登录 airi 时那个值是 null，整条流式会静默变哑巴）。
 * `serverUrl` 让 provider 自己带地址，不必为了一个本地服务去改 server.ts 的编译期常量 SERVER_URL。
 */
export function buildAlbionStreamingSnapshot(common: Pick<StreamingSessionSnapshot, 'voiceType' | 'ownerId' | 'onImmediateSpecial'>): StreamingSessionSnapshot {
  return {
    ...common,
    model: ALBION_STREAM_TTS_MODEL,
    voice: ALBION_TTS_MODEL,
    bufferEntireSession: false,
    // protocol_version 2 必须和本机服务一致，服务端不匹配版本会直接拒绝
    extraBody: { protocol_version: 2 },
    // Irodori 原样出 WAV，少一层编码，样本数可直接从帧头读
    responseFormat: 'wav',
    serverUrl: ALBION_STREAM_TTS_BASE,
    token: 'albion-local',
    // ACT 情绪先验（任务 #23）：舞台照演（onImmediateSpecial），同时把标记原样交给 9885 → 桥。
    // 脸和声音读同一份情绪。
    specialToText: albionActToText,
  }
}

/**
 * `<|ACT:{…}|>` 要不要转进 9885 的文字流（任务 #23）。
 *
 * 只挂在阿尔比恩本机 provider 的会话上（{@link buildAlbionStreamingSnapshot}），其它
 * provider 的 `appendSpecial` 还是原来的直通。返回**原串**、一个字符都不改：服务端把整段
 * `<|…|>` 当 `control` 块吃掉（不进 speech_text、不出字幕），桥从源文本里读 JSON。
 */
export function albionActToText(special: string): string | null {
  return ALBION_ACT_RE.test(special) ? special : null
}

/** ACT 里除 emotion 以外交给场景的表演字段（Spine 3.8 认 face / motion / hold，见 stage-act.ts）。 */
export interface AlbionStageAct {
  face?: string
  motion?: string
  hold?: unknown
}

/**
 * 这些渲染器的 ACT 由 albion-pet 一手处理（Live2D 姿态表与动作白名单、Spine 换脸与头部动作）。
 * Stage 上游的 onSignal 对它们不再动手，否则同一条 ACT 会演两遍。
 */
export function isAlbionActRenderer(renderer: string | null | undefined): boolean {
  return renderer === 'live2d' || renderer === 'spine' || renderer === 'spine38' || renderer === 'tachie'
}

/**
 * 把情绪入口挂到 `globalThis.__albionStageEmotion`，albion-pet.ts 通过它送情绪和 ACT 的其余字段。
 *
 * 原因：AIRI 上层会把 `<|ACT:{...}|>` 从 token 流里洗掉，所以桌宠里的
 * onTokenLiteral 收不到情绪，只能由 albion-pet 自己解析后走这个入口。
 * 返回 false 表示当前渲染器不接受（Live2D 有自己的姿态表，两边再走一遍会打架）。
 */
export function bindAlbionStageEmotion(play: (name: string, intensity: number, act?: AlbionStageAct) => boolean): void {
  ;(globalThis as unknown as Record<string, unknown>).__albionStageEmotion = play
}
