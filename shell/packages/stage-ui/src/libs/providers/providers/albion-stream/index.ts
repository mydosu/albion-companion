import { z } from 'zod'

import { defineProvider } from '../registry'

/**
 * 阿尔比恩本机流式语音（2026-09-20 加）
 *
 * 走壳的 `bidirectional-ws` 传输：整个 LLM 意图开一条 WS，**原始 token 原样转发**，
 * 不做客户端切句 —— 由我们的本机服务（tts/stream/stream_server.py :9885）边收边切子句、
 * 边翻译边合成，第一句不必等整段回复写完。
 *
 * 服务地址不写在这里：运行期由 Stage.vue 的 buildStreamingSnapshot 从
 * albion-defaults 传下去（provider 自己带 serverUrl，所以不用改编译期常量 SERVER_URL）。
 */
const albionStreamConfigSchema = z.object({
  baseUrl: z.string().optional(),
  voice: z.string().optional(),
})

export const ALBION_STREAM_PROVIDER_ID = 'albion-stream-tts'
export const ALBION_STREAM_MODEL_ID = 'albion-stream'
const SHIPS = ['albion', 'taihou', 'shinano', 'brest', 'jinshi', 'denia']

export const providerAlbionStreamSpeech = defineProvider({
  id: ALBION_STREAM_PROVIDER_ID,
  order: -2,
  name: 'Albion 本机流式语音',
  nameLocalize: () => 'Albion 本机流式语音',
  description: '本机流式语音（unspeech v1 WebSocket → 现有 TTS 桥 :9881）。',
  descriptionLocalize: () => '本机流式语音（unspeech v1 WebSocket → 现有 TTS 桥 :9881）。',
  tasks: ['text-to-speech', 'tts'],
  icon: 'i-solar:volume-loud-bold-duotone',
  requiresCredentials: false,
  // 关键一行：声明成双向 WS ⇒ tts-session 走流式适配器
  capabilities: {
    speech: { transport: 'bidirectional-ws' },
  },
  createProviderConfig: () => albionStreamConfigSchema,
  createProvider() {
    return {
      // 流式路径不走这个 speech()；留着只为设置页预览不报错
      speech: () => ({ baseURL: 'http://127.0.0.1:9885/v1/', model: ALBION_STREAM_MODEL_ID }),
    }
  },
  validationRequiredWhen: () => false,
  extraMethods: {
    listModels: async () => [
      { id: ALBION_STREAM_MODEL_ID, name: '本机流式（Irodori 桥）', provider: ALBION_STREAM_PROVIDER_ID },
    ],
    listVoices: async () => SHIPS.map(id => ({
      id,
      name: id,
      provider: ALBION_STREAM_PROVIDER_ID,
      languages: [{ code: 'ja-JP', title: '日本語' }],
    })),
  },
})
