/**
 * albion-chat —— 她在 Telegram / Discord 上说话（2026-11-02）。
 *
 * 只有走这个工具发出去的话，他才在手机上收得到：她那一轮的回复正文照旧只回给壳（桌面），
 * 不会自动转发到任何平台。平台侧收发全在宿主旁边的常驻桥 `bin/chat_bridge.py`（127.0.0.1:8660）。
 *
 * channel：telegram（他的 Telegram 私聊）、discord（他的 Discord 私信）、discord_group（Discord 群）。
 * voice=true 只对 Telegram 有效：桥去 9881 合成、ffmpeg 转 ogg/opus 发语音条；她的声音是日语，
 * 所以语音条会附上她写的中文原文当 caption（GSV 的中文声不带）。
 *
 * 桥的回话：`{ok:true,text}` 给她看一句结果；`{ok:false,error}` 抛出去（Pi 记成失败的工具结果）。
 * 主动找他（该渠道没有待回的消息）时桥会先过她自己的开口节奏，不行就把原因带回来。
 *
 * 协议同 `../shell-tools-bridge.ts`：阻塞 HTTP、Bearer = API_SERVER_KEY、非 2xx 抛错。
 * stdout 是 RPC 协议通道，诊断只写 stderr。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const PORT = process.env.ALBION_CHAT_PORT || "8660";
const KEY = process.env.API_SERVER_KEY || "";
const SESSION = process.env.ALBION_SESSION_ID || "albion-desktop";
const TIMEOUT_S = Number(process.env.ALBION_CHAT_SEND_TIMEOUT || "180");

type SendResult = { ok: boolean; text?: string; error?: string };

async function send(channel: string, text: string, voice: boolean): Promise<SendResult> {
	const headers: Record<string, string> = { "content-type": "application/json" };
	if (KEY) headers.authorization = `Bearer ${KEY}`;
	let res: Response;
	try {
		res = await fetch(`http://127.0.0.1:${PORT}/send`, {
			method: "POST",
			headers,
			body: JSON.stringify({ session: SESSION, channel, text, voice }),
			signal: AbortSignal.timeout(TIMEOUT_S * 1000),
		});
	} catch (e) {
		throw new Error(`平台桥没回话（127.0.0.1:${PORT}）：${String((e as Error)?.message || e).slice(0, 120)}`);
	}
	const data = (await res.json().catch(() => ({}))) as SendResult;
	if (!res.ok || data?.ok !== true) {
		throw new Error(data?.error || `平台桥 HTTP ${res.status}`);
	}
	return data;
}

export default function albionChat(pi: ExtensionAPI) {
	pi.registerTool({
		name: "send_message",
		label: "发消息",
		description:
			"给指挥官或 Discord 群发消息。只有用这个工具发出去的话，他才在手机上收得到。" +
			"channel：telegram（他的 Telegram 私聊）、discord（他的 Discord 私信）、discord_group（Discord 群，大家都看得到）。" +
			"voice=true 发语音条（仅 Telegram）：我的声音是日语，他听不懂日语，所以语音条会自动附上我写的中文原文。",
		promptSnippet: "send_message：把话发到他手机上（telegram/discord/discord_group；voice 只 Telegram）",
		parameters: Type.Object({
			channel: Type.Union(
				[Type.Literal("telegram"), Type.Literal("discord"), Type.Literal("discord_group")],
				{ description: "发到哪儿：他的 Telegram 私聊 / 他的 Discord 私信 / Discord 群" },
			),
			text: Type.String({ description: "要说的话（就是我要说给他或群里的话）" }),
			voice: Type.Optional(Type.Boolean({ description: "true = 发语音条（仅 Telegram；会附上这段中文当 caption）" })),
		}),
		async execute(_toolCallId, params) {
			const channel = String((params as any)?.channel || "");
			const text = String((params as any)?.text || "").trim();
			const voice = Boolean((params as any)?.voice);
			if (!text) throw new Error("text 是空的：先把要说的话写出来。");
			const out = await send(channel, text, voice);
			console.error(`[albion-chat] sent channel=${channel} voice=${voice} chars=${text.length}`);
			return {
				content: [{ type: "text" as const, text: out.text || "发了。" }],
				details: { channel, voice, chars: text.length },
			};
		},
	});
}
