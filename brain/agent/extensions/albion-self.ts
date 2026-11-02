/**
 * albion-self —— 她自己安排自己：下次什么时候醒、醒来想做什么、心里惦记着哪些事（2026-10-14）。
 *
 *   next_wake  定下次醒来的时间（说话轮 speak / 她自己的一轮 life）、醒来想做的事、这次做了什么、
 *              多久主动开口一次 → state/self.json、state/life.json；bin/wake.py 读它。
 *   notebook   心事本：记下惦记的事、做完划掉 → state/notebook.json；每次醒来宿主会把没划掉的念给她。
 *
 * 两条计时各走各的（2026-11-02）：说话轮 10～360 分钟（原来的行为），生活轮 30～180 分钟；
 * 多久主动跟他说一次话（speak_every_hours，1～6 小时）仍由她定；
 * 宿主只守三条底线：夜里安静时段不醒、他刚说过话不打断、她主动说的话他连着不回就越等越久（见 bin/wake.py）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { personalStateDir } from "../lib/session-scope.ts";

const SPEAK_MIN_MINUTES = 10;
const SPEAK_MAX_MINUTES = 360;
const SPEAK_DEFAULT_MINUTES = 30;
// 她自己的时间：30 分钟～3 小时，没定过按 1 小时
const LIFE_MIN_MINUTES = 30;
const LIFE_MAX_MINUTES = 180;
const LIFE_DEFAULT_MINUTES = 60;
const LIFE_KEEP = 20;
// 多久主动开口一次：她自己定，1～6 小时（2026-10-15 指挥官嫌每小时都出声太勤，3 小时是那时定的默认）
const MIN_SPEAK_H = 1;
const MAX_SPEAK_H = 6;
const DEFAULT_SPEAK_H = 3;
const MAX_OPEN_NOTES = 30;

// 探针/验收会话写自己的临时目录，不碰生产心事本（2026-10-29：探针台词被记成「他说过」就是从这漏的）
const stateDir = () => personalStateDir();

function readJson<T>(name: string, fallback: T): T {
	const p = join(stateDir(), name);
	if (!existsSync(p)) return fallback;
	try {
		return JSON.parse(readFileSync(p, "utf-8")) as T;
	} catch {
		// 坏了不能当成空的再覆盖掉：先原样留一份，再从空的开始（haiku 复核 G）
		try {
			renameSync(p, `${p}.corrupt-${Date.now()}`);
		} catch {
			/* ignore */
		}
		return fallback;
	}
}

/** 先写临时文件再改名：宿主随时可能在读。 */
function writeJson(name: string, value: unknown): void {
	const dir = stateDir();
	if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
	const tmp = join(dir, `.${name}.tmp`);
	writeFileSync(tmp, JSON.stringify(value, null, 2), "utf-8");
	renameSync(tmp, join(dir, name));
}

const clock = (ms: number) => new Date(ms).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

type Note = { id: number; text: string; at: number; done_at?: number };
type LifeItem = { did: string; at: number };
type Kind = "speak" | "life";
type Lane = { wake_at?: number; intent?: string; set_at?: number };
type SelfPlan = { speak?: Lane; life?: Lane; speak_every_hours?: number } & Partial<Lane>;

/** self.json 里的一条计时：新格式 {"speak": {...}, "life": {...}}；旧格式顶层 wake_at/intent/set_at 当成 speak。 */
function laneOf(plan: SelfPlan | null | undefined, kind: Kind): Lane {
	const v = plan?.[kind];
	if (v && typeof v === "object") return { ...v };
	if (kind === "speak" && plan && ("wake_at" in plan || "intent" in plan || "set_at" in plan)) {
		return { wake_at: plan.wake_at, intent: plan.intent, set_at: plan.set_at };
	}
	return {};
}

export default function albionSelf(pi: ExtensionAPI) {
	pi.registerTool({
		name: "next_wake",
		label: "定下次醒来",
		description:
			"给自己定下次醒来的时间和醒来想做的事。kind=speak（默认）是下次跟他说话的时间，10～360 分钟后；" +
			"kind=life 是我自己的一轮（不看他、不跟他说话），30～180 分钟后。" +
			`也可以顺手定多久主动跟他说一次话（${MIN_SPEAK_H}～${MAX_SPEAK_H} 小时），` +
			"做完自己那一轮用 did 记一句这次做了什么（一句话，120 字以内）。" +
			"到点我会自己醒；不定的话说话轮默认半小时后、生活轮默认一小时后。" +
			"夜里他睡觉、他刚说过话、他连着没回我时，醒来会往后推——那是为他的作息，不是我能改的。",
		promptSnippet: "next_wake：定下次跟他说话 / 我自己的一轮（kind）、想做的事、这次做了什么（did）",
		parameters: Type.Object({
			kind: Type.Optional(
				Type.Union([Type.Literal("speak"), Type.Literal("life")], {
					description: "speak = 下次跟他说话的时间（默认）；life = 我自己的一轮（不看他、不跟他说话）",
				}),
			),
			minutes: Type.Number({
				description: `多少分钟后（speak ${SPEAK_MIN_MINUTES}～${SPEAK_MAX_MINUTES}，life ${LIFE_MIN_MINUTES}～${LIFE_MAX_MINUTES}）`,
			}),
			intent: Type.String({ description: "醒来想做什么，一句话（会在醒来时念给自己听）" }),
			did: Type.Optional(Type.String({ description: "这次自己做了什么，一句话（120 字以内）；记进生活记录" })),
			speak_every_hours: Type.Optional(Type.Number({ description: `多久主动开口一次（${MIN_SPEAK_H}～${MAX_SPEAK_H} 小时）；不填就照上次定的` })),
		}),
		async execute(_toolCallId, params) {
			// null / "" / 空白在 Number() 下是 0，不能当成"想 0 分钟后醒"（haiku 复核 r33-3）
			const num = (given: unknown) =>
				typeof given === "number" || (typeof given === "string" && given.trim() !== "") ? Number(given) : NaN;
			const kind: Kind = (params as any).kind === "life" ? "life" : "speak";
			const minM = kind === "life" ? LIFE_MIN_MINUTES : SPEAK_MIN_MINUTES;
			const maxM = kind === "life" ? LIFE_MAX_MINUTES : SPEAK_MAX_MINUTES;
			const fallbackM = kind === "life" ? LIFE_DEFAULT_MINUTES : SPEAK_DEFAULT_MINUTES;
			const raw = num(params.minutes);
			const valid = Number.isFinite(raw);
			const minutes = Math.min(maxM, Math.max(minM, valid ? Math.round(raw) : fallbackM));
			const intent = String(params.intent || "").trim().slice(0, 200);
			const wakeAt = Date.now() + minutes * 60_000;
			const plan = readJson<SelfPlan>("self.json", {});
			// 开口节奏不填就沿用上次的（每次 next_wake 都会重写 self.json，不能把她定过的节奏冲掉）
			const prevRaw = plan.speak_every_hours;
			const prev = typeof prevRaw === "number" ? prevRaw : NaN;
			const rawSpeak = num((params as any).speak_every_hours);
			const speak = Math.min(MAX_SPEAK_H, Math.max(MIN_SPEAK_H, Number.isFinite(rawSpeak) ? rawSpeak : Number.isFinite(prev) ? prev : DEFAULT_SPEAK_H));
			const nowS = Date.now() / 1000;
			const lane: Lane = { wake_at: wakeAt / 1000, intent, set_at: nowS };
			// 另一条计时原样留着：定说话轮不冲掉生活轮，反过来也一样
			const other = laneOf(plan, kind === "life" ? "speak" : "life");
			writeJson("self.json", {
				speak: kind === "speak" ? lane : other,
				life: kind === "life" ? lane : other,
				speak_every_hours: speak,
			});
			// 这次自己做了什么：只留最近 20 条，每条带时间
			const cleanDid = String((params as any).did || "").trim().slice(0, 120);
			if (cleanDid) {
				const life = readJson<LifeItem[]>("life.json", []);
				const items = (Array.isArray(life) ? life : []).filter((x) => x && typeof x === "object");
				items.push({ did: cleanDid, at: nowS });
				writeJson("life.json", items.slice(-LIFE_KEEP));
			}
			const note = !valid ? `（没给有效的分钟数，按默认 ${minutes} 分钟）` : minutes !== Math.round(raw) ? `（调成了 ${minutes} 分钟）` : "";
			const speakNote = Number.isFinite(rawSpeak) ? `；以后大约每 ${speak} 小时主动跟他说一次话` : "";
			const didNote = cleanDid ? `；这次做的记下了：${cleanDid}` : "";
			const when = clock(wakeAt);
			if (kind === "life") {
				return text(`好，${when} 左右回到我自己的时间${note}；这次想做的事：${intent || "（没写）"}${didNote}。`);
			}
			return text(`好，${when} 左右醒（下次跟他说话）${note}；醒来想做的事：${intent || "（没写）"}${speakNote}${didNote}。`);
		},
	});

	pi.registerTool({
		name: "notebook",
		label: "心事本",
		description:
			"我的心事本：记下惦记着、想以后做的事（add），做完或不需要了就划掉（done，给编号），或者翻一翻（list）。" +
			"每次自己醒来时，没划掉的会念给我听。只记自己真惦记的事，不记流水账。",
		promptSnippet: "notebook：心事本（add 记一件 / done 划掉 / list 翻看）",
		parameters: Type.Object({
			action: Type.Union([Type.Literal("add"), Type.Literal("done"), Type.Literal("list")]),
			text: Type.Optional(Type.String({ description: "add 时：要记的事，一句话" })),
			id: Type.Optional(Type.Number({ description: "done 时：要划掉的编号" })),
		}),
		async execute(_toolCallId, params) {
			const notes = readJson<Note[]>("notebook.json", []);
			const open = notes.filter((n) => !n.done_at);
			if (params.action === "add") {
				const t = String(params.text || "").trim().slice(0, 200);
				if (!t) return text("要记什么？text 是空的。");
				if (open.length >= MAX_OPEN_NOTES) return text(`心事本里已经有 ${open.length} 件没划掉的事了，先划掉几件再记。`);
				const id = notes.reduce((m, n) => Math.max(m, n.id), 0) + 1;
				notes.push({ id, text: t, at: Date.now() / 1000 });
				// 只裁已划掉的旧条目；还开着的一件都不丢（haiku 复核 C）
				const done = notes.filter((n) => n.done_at);
				const keepDone = new Set(done.slice(-100));
				writeJson("notebook.json", notes.filter((n) => !n.done_at || keepDone.has(n)));
				return text(`记下了：#${id} ${t}`);
			}
			if (params.action === "done") {
				const n = notes.find((x) => x.id === Number(params.id) && !x.done_at);
				if (!n) return text(`没有还开着的 #${params.id}。`);
				n.done_at = Date.now() / 1000;
				writeJson("notebook.json", notes);
				return text(`划掉了：#${n.id} ${n.text}`);
			}
			if (!open.length) return text("心事本里现在没有没划掉的事。");
			return text(open.map((n) => `#${n.id} ${n.text}（${clock(n.at * 1000)} 记的）`).join("\n"));
		},
	});
}
