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
 * 表情包（2026-11-02）：素材在 `D:\AI\stickers`（不进 Git），`index.json` 每项有
 * id / file / character / meaning / emotions / text_on_image。`find_sticker` 按心情挑几张候选，
 * 她再把编号交给 `send_message` 的 `sticker`，由桥按平台上传（TG 走 sendSticker/sendAnimation，
 * Discord 走附件上传）。
 *
 * 桥的回话：`{ok:true,text}` 给她看一句结果；`{ok:false,error}` 抛出去（Pi 记成失败的工具结果）。
 * 主动找他（该渠道没有待回的消息）时桥会先过她自己的开口节奏，不行就把原因带回来。
 *
 * 协议同 `../shell-tools-bridge.ts`：阻塞 HTTP、Bearer = API_SERVER_KEY、非 2xx 抛错。
 * stdout 是 RPC 协议通道，诊断只写 stderr。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import fs from "node:fs";
import path from "node:path";

const PORT = process.env.ALBION_CHAT_PORT || "8660";
const KEY = process.env.API_SERVER_KEY || "";
const SESSION = process.env.ALBION_SESSION_ID || "albion-desktop";
const TIMEOUT_S = Number(process.env.ALBION_CHAT_SEND_TIMEOUT || "180");
const STICKERS_DIR = process.env.ALBION_STICKERS_DIR || "/mnt/d/AI/stickers";

type SendResult = { ok: boolean; text?: string; error?: string };

type Sticker = {
	id?: string;
	file?: Record<string, string> | string;
	character?: string;
	meaning?: string;
	emotions?: string[];
	text_on_image?: string;
};

let stickerCache: { mtimeMs: number; items: Sticker[] } | null = null;

/** 读 `index.json`（按 mtime 缓存）：读不到就返回空表，不抛。 */
function loadStickers(): Sticker[] {
	try {
		const p = path.join(STICKERS_DIR, "index.json");
		const stat = fs.statSync(p);
		if (stickerCache && stickerCache.mtimeMs === stat.mtimeMs) return stickerCache.items;
		const raw = JSON.parse(fs.readFileSync(p, "utf8"));
		const items = (Array.isArray(raw) ? raw : []).filter(
			(it): it is Sticker => !!it && typeof it === "object" && !!String((it as Sticker).id || "").trim(),
		);
		stickerCache = { mtimeMs: stat.mtimeMs, items };
		return items;
	} catch {
		return [];
	}
}

/** 她自己的图（albion）、通用（generic）、其他舰娘（azur-other:*）才可选；可畏的图不给。 */
export function stickerAllowed(item: Sticker): boolean {
	const character = String(item?.character || "");
	return character === "albion" || character === "generic" || character.startsWith("azur-other");
}

/** 关键词打分：emotions 精确命中 3 分、包含 2 分；meaning / text_on_image 子串 1 分。 */
export function scoreSticker(mood: string, item: Sticker): number {
	const terms = String(mood || "")
		.toLowerCase()
		.split(/[\s,，、。/;；]+/)
		.map((t) => t.trim())
		.filter(Boolean);
	if (!terms.length) return 0;
	const emotions = (item?.emotions || []).map((e) => String(e).toLowerCase());
	const haystack = `${String(item?.meaning || "").toLowerCase()} ${String(item?.text_on_image || "").toLowerCase()}`;
	let score = 0;
	for (const term of terms) {
		if (emotions.some((e) => e === term)) score += 3;
		else if (emotions.some((e) => e.includes(term) || term.includes(e))) score += 2;
		if (haystack.includes(term)) score += 1;
	}
	return score;
}

/** 挑前 limit 张：同分时 albion 优先；一张都没命中就随机给 albion 的几张。 */
export function pickStickers(mood: string, limit = 6, items: Sticker[] = loadStickers()): {
	items: Sticker[];
	random: boolean;
} {
	const pool = items.filter(stickerAllowed);
	const scored = pool
		.map((item) => ({
			item,
			score: scoreSticker(mood, item),
			own: String(item.character || "") === "albion" ? 1 : 0,
		}))
		.filter((row) => row.score > 0)
		.sort((a, b) => b.score - a.score || b.own - a.own);
	if (scored.length) return { items: scored.slice(0, limit).map((row) => row.item), random: false };
	const own = pool.filter((item) => String(item.character || "") === "albion");
	const shuffled = own.slice();
	for (let i = shuffled.length - 1; i > 0; i -= 1) {
		const j = Math.floor(Math.random() * (i + 1));
		[shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
	}
	return { items: shuffled.slice(0, limit), random: true };
}

function stickerLine(item: Sticker): string {
	const meaning = String(item?.meaning || "").replace(/\s+/g, " ").trim() || "（没有写意思）";
	return `${String(item.id).trim()}：${meaning.slice(0, 80)}`;
}

function hasSticker(id: string): boolean {
	return loadStickers().some((item) => String(item.id || "").trim() === id);
}

async function send(channel: string, text: string, voice: boolean, sticker: string): Promise<SendResult> {
	const headers: Record<string, string> = { "content-type": "application/json" };
	if (KEY) headers.authorization = `Bearer ${KEY}`;
	let res: Response;
	try {
		res = await fetch(`http://127.0.0.1:${PORT}/send`, {
			method: "POST",
			headers,
			body: JSON.stringify({ session: SESSION, channel, text, voice, sticker }),
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
		name: "find_sticker",
		label: "找表情包",
		description:
			"按心情找表情包：mood 写想表达的意思或情绪（比如「害羞」「开心」「晚安」）。会列出几张候选，每张一个编号和它的意思；挑一张意思对得上的，用 send_message 的 sticker 发出去。我自己的图排在前面。想多看几张、自己慢慢挑的时候，mood 写「全部」，会列出所有能用的图和它们的意思。",
		promptSnippet: "find_sticker：按心情找表情包，配合 send_message 的 sticker 发",
		parameters: Type.Object({
			mood: Type.String({ description: "想表达的意思或情绪（比如「害羞」「开心」「晚安」）" }),
		}),
		async execute(_toolCallId, params) {
			const mood = String((params as any)?.mood || "").trim();
			const all = mood === "全部";
			const { items, random } = all
				? { items: loadStickers().filter(stickerAllowed), random: false }
				: pickStickers(mood);
			if (!items.length) {
				return {
					content: [
						{
							type: "text" as const,
							text: "素材库里没有可用的表情包（读不到 index.json，或者没有我能用的图）。",
						},
					],
					details: { count: 0, random: false },
				};
			}
			const head = all
				? `能用的表情包一共 ${items.length} 张：`
				: random
				? `没有正好对得上「${mood}」的，随机挑了几张：`
				: `按「${mood}」挑了几张：`;
			const text = [head, ...items.map(stickerLine)].join("\n");
			return { content: [{ type: "text" as const, text }], details: { count: items.length, random } };
		},
	});

	pi.registerTool({
		name: "send_message",
		label: "发消息",
		description:
			"给指挥官或 Discord 群发消息。只有用这个工具发出去的话，他才在手机上收得到。" +
			"channel：telegram（他的 Telegram 私聊）、discord（他的 Discord 私信）、discord_group（Discord 群，大家都看得到）。" +
			"voice=true 发语音条（仅 Telegram）：我的声音是日语，他听不懂日语，所以语音条会自动附上我写的中文原文。" +
			"在 Telegram 和 Discord 上，偶尔可以用表情包代替或补充一句话；不必每条都发，意思对得上才发。" +
			"要不要分成几条发、分几条，由我自己看着办：想分开发，就连着调用几次 send_message，每次一条，像平时连着发几条消息那样。",
		promptSnippet: "send_message：把话发到他手机上（telegram/discord/discord_group；voice 只 Telegram）",
		parameters: Type.Object({
			channel: Type.Union(
				[Type.Literal("telegram"), Type.Literal("discord"), Type.Literal("discord_group")],
				{ description: "发到哪儿：他的 Telegram 私聊 / 他的 Discord 私信 / Discord 群" },
			),
			text: Type.String({ description: "要说的话（就是我要说给他或群里的话）" }),
			voice: Type.Optional(Type.Boolean({ description: "true = 发语音条（仅 Telegram；会附上这段中文当 caption）" })),
			sticker: Type.Optional(
				Type.String({
					description:
						"表情包编号（find_sticker 查到的）。可以只发表情包，也可以和 text 一起发，表情包跟在文字后面。",
				}),
			),
		}),
		async execute(_toolCallId, params) {
			const channel = String((params as any)?.channel || "");
			const text = String((params as any)?.text || "").trim();
			const voice = Boolean((params as any)?.voice);
			const sticker = String((params as any)?.sticker || "").trim();
			if (!text && !sticker) {
				throw new Error("text 是空的：先把要说的话写出来（或者先用 find_sticker 挑一张表情包）。");
			}
			if (sticker && !hasSticker(sticker)) {
				return {
					content: [{ type: "text" as const, text: "没有这个表情包编号，先用 find_sticker 找一张" }],
					details: { channel, sticker, sent: false },
				};
			}
			const out = await send(channel, text, voice, sticker);
			console.error(
				`[albion-chat] sent channel=${channel} voice=${voice} sticker=${sticker || "-"} chars=${text.length}`,
			);
			return {
				content: [{ type: "text" as const, text: out.text || "发了。" }],
				details: { channel, voice, chars: text.length, sticker: sticker || undefined },
			};
		},
	});
}
