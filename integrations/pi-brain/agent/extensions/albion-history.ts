/**
 * albion-history —— 翻我们以前的聊天记录（2026-10-14）。
 *
 * Hermes 时期她能搜旧会话；Pi 这边只有 OpenViking 的语义回忆（抓大意，抓不准原话和时间）。
 * 这件工具按关键词直接在活动库（state/state.db，摆件也读它）里找他和她说过的原话，带北京时间。
 * 只读；宿主塞给她的内部提示（醒来提示、主动开口旁白、补催）不算聊天，过滤掉。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync } from "node:fs";

const DB = () => process.env.ALBION_STATE_DB || "/root/albion-pi/state/state.db";
const SESSION = () => process.env.ALBION_SESSION_ID || "albion-desktop";
const MAX_HITS = 8;
const INTERNAL = /【自主醒来】|【定下次醒来】|【想起来去做】|【接着说完】|【真去做】|（阿尔比恩自己醒来）|（阿尔比恩想起来|（阿尔比恩接着|（宿主提醒|^（这是我自己|^（你要的【|这是一次自主开口/;
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

const clock = (s: number) => new Date(s * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });

export default function albionHistory(pi: ExtensionAPI) {
	pi.registerTool({
		name: "chat_search",
		label: "翻聊天记录",
		description:
			"按关键词翻我和指挥官以前聊过的原话（带时间）。keywords 写一两个词，空格隔开表示都要有；" +
			"days 可选，只看最近几天。想确认他以前说过什么、我们哪天聊过某件事时用，照查到的原话说，查不到就说没找到。",
		promptSnippet: "chat_search：按关键词翻以前的聊天原话",
		parameters: Type.Object({
			keywords: Type.String({ description: "关键词，空格隔开，如「概率论 作业」" }),
			days: Type.Optional(Type.Number({ description: "只看最近几天（可选）" })),
		}),
		async execute(_toolCallId, params) {
			const words = String(params.keywords ?? "").split(/\s+/).map((w) => w.trim()).filter(Boolean).slice(0, 5);
			if (!words.length) return text("要给关键词。");
			if (!existsSync(DB())) return text("找不到聊天记录库，这次翻不了。");
			let rows: { role: string; content: string; timestamp: number }[] = [];
			try {
				const { DatabaseSync } = await import("node:sqlite");
				const db = new DatabaseSync(DB(), { readOnly: true });
				const days = Number(params.days);
				const since = Number.isFinite(days) && days > 0 ? Date.now() / 1000 - days * 86400 : 0;
				// 任一词命中就取出来，再按命中词数排序：她常一次塞好几个词（其中还有猜的），
				// 全部都要才算会一条都搜不到，她就拿回忆去拼答案（r42 实测）
				const where = words.map(() => "content LIKE ?").join(" OR ");
				rows = db
					.prepare(`SELECT role, content, timestamp FROM messages WHERE session_id = ? AND role IN ('user','assistant') AND timestamp >= ? AND (${where}) ORDER BY id DESC LIMIT 300`)
					.all(SESSION(), since, ...words.map((w) => `%${w}%`)) as any;
				db.close();
			} catch (e) {
				return text(`翻记录出错了：${String((e as Error)?.message || e).slice(0, 120)}`);
			}
			const score = (c: string) => words.filter((w) => c.includes(w)).length;
			const hits = rows
				.filter((r) => !INTERNAL.test(String(r.content ?? "").replace(/^\[[^\]\n]*\]\s*/, "")))
				.map((r, i) => ({ r, s: score(String(r.content)), i }))
				.sort((a, b) => b.s - a.s || a.i - b.i) // 命中词多的在前，同分新的在前
				.slice(0, MAX_HITS)
				.map((x) => x.r);
			if (!hits.length)
				return text(`没找到含「${words.join("」「")}」任何一个词的聊天记录。照实说没找到，别拿印象拼答案；可以换个更短的词再找。`);
			const lines = hits.map((r) => {
				const who = r.role === "user" ? "指挥官" : "我";
				const body = String(r.content).replace(/<\|[^|]*\|>/g, "").replace(/^\[[^\]\n]*\]\s*/, "").replace(/\s+/g, " ").trim();
				return `· ${clock(r.timestamp)} ${who}：${body.slice(0, 160)}`;
			});
			return text(`【聊天记录·按「${words.join(" ")}」找到 ${hits.length} 条，命中词多的在前】\n${lines.join("\n")}\n（只照这些原话说；这里没有的，别拿印象补。）`);
		},
	});
}
