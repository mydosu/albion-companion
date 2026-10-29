/**
 * albion-todo —— 做一件长一点的事时给自己列的步骤（2026-10-14）。
 *
 * 和 notebook（心事本：惦记着、以后想做的事）分开：todo 是"手上这件事拆成几步、做完一步勾一步"，
 * Hermes 时期她有这个；长活儿没有清单，做到一半就容易忘了还剩什么、或者没做完就说做完了。
 * 存在 state/todo.json，每次醒来、每轮都能翻。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { personalStatePath } from "../lib/session-scope.ts";

type Item = { id: number; text: string; done: boolean };
// 探针/验收会话写自己的临时目录，不碰生产 todo（2026-10-29）
const file = () => personalStatePath("todo.json");
const MAX_ITEMS = 30;
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

function load(): { title: string; items: Item[] } {
	try {
		const v = JSON.parse(readFileSync(file(), "utf-8"));
		if (v && Array.isArray(v.items)) return { title: String(v.title ?? ""), items: v.items };
	} catch {
		/* 没有或坏了都从空开始 */
	}
	return { title: "", items: [] };
}

function save(v: { title: string; items: Item[] }): void {
	const f = file();
	const dir = f.slice(0, f.lastIndexOf("/"));
	if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
	writeFileSync(`${f}.tmp`, JSON.stringify(v, null, 2), "utf-8");
	renameSync(`${f}.tmp`, f);
}

function show(v: { title: string; items: Item[] }): string {
	if (!v.items.length) return "清单是空的。";
	const left = v.items.filter((i) => !i.done).length;
	return `【${v.title || "手上的事"}】还剩 ${left} / ${v.items.length} 步\n` + v.items.map((i) => `${i.done ? "☑" : "☐"} #${i.id} ${i.text}`).join("\n");
}

export default function albionTodo(pi: ExtensionAPI) {
	pi.registerTool({
		name: "todo",
		label: "待办清单",
		description:
			"做一件要好几步的事时给自己列清单：plan 写下这件事和几个步骤（会替换旧清单）；add 追加一步；done 勾掉一步（id）；list 看还剩什么。" +
			"清单没勾完，就不说这件事做完了。",
		promptSnippet: "todo：给手上的长活儿列步骤、做完勾掉",
		parameters: Type.Object({
			action: Type.Union([Type.Literal("plan"), Type.Literal("add"), Type.Literal("done"), Type.Literal("list")]),
			title: Type.Optional(Type.String({ description: "plan 时：这件事叫什么" })),
			steps: Type.Optional(Type.String({ description: "plan / add 时：步骤，每行一步" })),
			id: Type.Optional(Type.Number({ description: "done 时：勾掉第几步" })),
		}),
		async execute(_toolCallId, params) {
			const v = load();
			const lines = String(params.steps ?? "").split("\n").map((s) => s.replace(/^\s*[-*\d.、)]+\s*/, "").trim()).filter(Boolean);
			if (params.action === "plan") {
				if (!lines.length) return text("plan 要写上步骤（steps，每行一步）。");
				const nv = { title: String(params.title ?? "").trim().slice(0, 60), items: lines.slice(0, MAX_ITEMS).map((t, i) => ({ id: i + 1, text: t.slice(0, 120), done: false })) };
				save(nv);
				return text(show(nv));
			}
			if (params.action === "add") {
				if (!lines.length) return text("add 要写上步骤（steps）。");
				let next = v.items.reduce((m, i) => Math.max(m, i.id), 0);
				for (const t of lines) if (v.items.length < MAX_ITEMS) v.items.push({ id: ++next, text: t.slice(0, 120), done: false });
				save(v);
				return text(show(v));
			}
			if (params.action === "done") {
				const it = v.items.find((i) => i.id === Number(params.id));
				if (!it) return text(`清单里没有 #${params.id}。`);
				it.done = true;
				save(v);
				return text(show(v));
			}
			return text(show(v));
		},
	});
}
