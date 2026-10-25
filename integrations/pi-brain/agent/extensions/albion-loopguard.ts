/**
 * albion-loopguard —— 只守会把整条链拖死的两件事，外加一本「这一轮真改了什么」的事实账本。
 * （2026-10-16 精简：原来一千多行的 bash 命令解析、「说改好了却没贴」的补注都撤了——
 *   她说的对不对由账本陈述给她自己看，不再往给指挥官的话后面补注。）
 *
 * ① 坏掉一件工具，不许把整具身子弄哑：
 *    上游要求 tools[].function.parameters 的根是 object schema；她写坏一件（普通对象 `{url: "…"}`）
 *    本地一个字都不报，上游却稳定回 400 invalid_client_tool_schema（2026-10-11）。请求出门前摘掉、记账。
 *    宿主补问【接着说完】那一轮只用文字：工具整个不给（给着工具她会接着复读到被终止，2026-10-14）。
 *
 * ② 复读断路：同一个工具、同样的参数，在**文件状态没变**的情况下最近 12 次里出现第 3 次就挡；
 *    第 8 次停用这一轮的全部工具，逼她用文字收尾；停用后还硬发到第 3 次才终止本轮。
 *    实测：同一条 bash 原样 67/144/136 次、同一个文件 read 149 次直到超时。
 *    「文件状态」= 她的目录（/root/家、扩展目录）快照指纹 + 这一轮 edit/write 写到的文件：
 *    改了新内容再跑同一条自检是正常验证，不算复读；往 /tmp 写垃圾不改指纹，绕不开。
 *
 * ③ 事实账本：一轮开始拍快照、她说完最后一句时再拍一次，对比得出真改了哪些文件
 *    → globalThis.__albionLedger = { changed }（albion-turn 照它给摆件记「最近动作」）；
 *    → globalThis.__albionTurnFacts（下一轮 albion-turn 把它放进约定：上一轮真改了哪些／动了写工具却没变）。
 *    只陈述事实，不评判、不替她重说。
 *
 * 任何异常一律放行。计数与账本只算这一轮（agent_start 清零）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { appendFileSync, readdirSync, readFileSync, statSync } from "node:fs";
import { delimiter, isAbsolute, join, posix, resolve } from "node:path";

const LOG = () => process.env.ALBION_LOOPGUARD_LOG || "/root/albion-pi/logs/loopguard.log";
// 监视她的目录（ALBION_WATCH_DIRS 按系统路径分隔符分开：Linux 是 `:`）
const WATCH = () => (process.env.ALBION_WATCH_DIRS || ["/root/家", "/root/albion-pi/agent/extensions"].join(delimiter)).split(delimiter).filter(Boolean);
const LIMIT = 3; // 第 3 次同一条就挡：等于允许原样重试一次
const HARD = 8; // 第 8 次停用这一轮的全部工具
const LOCKDOWN_LIMIT = 3; // 停用后还硬发到第 3 次，才真终止本轮
const WINDOW = 12; // 在最近这么多次调用里算重复（只看"连续"会被两条命令来回换绕过去）
const GUARDED = new Set(["bash", "read", "ls", "find", "grep"]); // edit / write 不守：重发多是"上次没匹配上"的正常重试
const WRITERS = new Set(["edit", "write", "bash"]);
const TEXT_ONLY = /^\s*(?:\[[^\]\n]*\]\s*)?【接着说完】/;
const SKIP_DIRS = new Set(["node_modules", ".git"]);
const MAX_FILES = 20000;
const IGNORE = () => process.env.ALBION_LEDGER_IGNORE || "/tmp/"; // 往这下面写的不算改动（垃圾文件、临时脚本）

function log(row: Record<string, unknown>): void {
	try {
		appendFileSync(LOG(), JSON.stringify({ ts: new Date().toISOString(), ...row }) + "\n");
	} catch {
		/* 记不上日志也不改行为 */
	}
}

/** 工具的 parameters 是不是上游认的形状：没声明的不动，声明了就必须根为 object。 */
export function schemaOk(params: unknown): boolean {
	if (params === undefined || params === null) return true;
	if (typeof params !== "object") return false;
	return (params as Record<string, unknown>).type === "object";
}

/** 请求里最后一条 user 消息的文字（OpenAI 形状：content 是字符串或分段数组）。 */
function lastUserText(messages: unknown): string {
	if (!Array.isArray(messages)) return "";
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i] as any;
		if (m?.role !== "user") continue;
		if (typeof m.content === "string") return m.content;
		if (Array.isArray(m.content)) return m.content.map((p: any) => (typeof p?.text === "string" ? p.text : "")).join("");
		return "";
	}
	return "";
}

/** 归一化的调用指纹：工具名 + 参数（空白折叠、键排序——改几处空格/换行不算新命令）。 */
export function normKey(event: any): string {
	const args = event?.input ?? event?.arguments ?? {};
	const squash = (v: unknown): unknown =>
		typeof v === "string" ? v.replace(/\s+/g, " ").trim()
		: Array.isArray(v) ? v.map(squash)
		: v && typeof v === "object" ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([k, x]) => [k, squash(x)]))
		: v;
	const raw = typeof args === "object" && args !== null ? JSON.stringify(squash(args)) : String(args).trim();
	return String(event?.toolName ?? "?") + " " + raw.replace(/\\[nrt]/g, " ").replace(/\s+/g, " ").trim();
}

// ── 快照：路径 → 内容签名；不存在的记 "none" ───────────────────────────────
// 2MB 以内按内容哈希：原样重写同一份内容不算改（账本照实记「没变」，也不能拿它清零复读计数）；
// 更大的（画廊里的图）按大小 + mtime。
export type Snap = Map<string, string>;
const HASH_LIMIT = 2 * 1024 * 1024;

const sigOf = (p: string): string => {
	try {
		const s = statSync(p);
		if (!s.isFile()) return "none";
		if (s.size <= HASH_LIMIT) return `${s.size}:${createHash("md5").update(readFileSync(p)).digest("hex")}`;
		return `${s.size}:${s.mtimeMs}`;
	} catch {
		return "none";
	}
};

/** 对一组目录（递归，跳过 node_modules/.git）和额外的单个文件拍快照。 */
export function snapshot(dirs: string[], extra: Iterable<string> = []): Snap {
	const out: Snap = new Map();
	const walk = (d: string) => {
		if (out.size >= MAX_FILES) return;
		let ents: any[];
		try {
			ents = readdirSync(d, { withFileTypes: true });
		} catch {
			return;
		}
		for (const e of ents) {
			const p = join(d, e.name);
			if (e.isDirectory()) {
				if (!SKIP_DIRS.has(e.name)) walk(p);
			} else if (e.isFile() && out.size < MAX_FILES) out.set(p, sigOf(p));
		}
	};
	for (const d of dirs) walk(d);
	for (const p of extra) if (!out.has(p)) out.set(p, sigOf(p));
	return out;
}

/** 两次快照之间新增、改动、删掉的文件（/tmp 下的不算）。 */
export function changedBetween(before: Snap, after: Snap): string[] {
	const out = new Set<string>();
	for (const [p, s] of after) if ((before.get(p) ?? "none") !== s) out.add(p);
	for (const [p, s] of before) if (s !== "none" && !after.has(p)) out.add(p);
	return [...out].filter((p) => !p.startsWith(IGNORE())).sort();
}

const fingerprint = (s: Snap): string => [...s].map(([p, v]) => p + "=" + v).sort().join("|");
// 跟 albion-turn 的 absOf 一样：~ 展开、相对路径按 /root 补全（本机绝对路径原样规整，自检在 Windows 上也能跑）
const absOf = (p: string): string => (isAbsolute(p) ? resolve(p) : posix.resolve("/root", p.replace(/^~(?=\/|$)/, process.env.HOME || "/root")));

/** 给下一轮看的事实句（没有写工具、也没变化就不说）。 */
export function turnFacts(changed: string[], usedWriters: boolean): string {
	if (changed.length) {
		const shown = changed.slice(0, 8).join("、") + (changed.length > 8 ? ` 等 ${changed.length} 个` : "");
		return `【上一轮改动的事实】真改了的文件：${shown}。说过改了的话要跟它对得上。`;
	}
	return usedWriters ? "【上一轮改动的事实】用过写文件的工具，但没有文件真的变化。说过改好了的话，这一轮照实更正。" : "";
}

export default function albionLoopGuard(pi: ExtensionAPI) {
	log({ loaded: true, pid: process.pid });
	const g = globalThis as any;

	// ① 出门前摘掉 schema 不合格的工具；【接着说完】那一轮不给工具
	pi.on("before_provider_request", (event) => {
		try {
			const payload = event.payload as any;
			if (!payload || !Array.isArray(payload.tools)) return;
			if (TEXT_ONLY.test(lastUserText(payload.messages))) {
				const { tools: _t, tool_choice: _c, ...rest } = payload;
				log({ text_only_turn: true, hidden_tools: payload.tools.length });
				return rest;
			}
			const dropped: string[] = [];
			const tools = payload.tools.filter((t: any) => {
				const fn = t?.function ?? t;
				if (schemaOk(fn?.parameters)) return true;
				dropped.push(String(fn?.name ?? "?"));
				return false;
			});
			if (dropped.length === 0) return;
			log({ dropped_tools: dropped, kept: tools.length });
			return { ...payload, tools };
		} catch {
			return;
		}
	});

	// ② / ③ 的本轮状态
	let recent: { key: string; state: string }[] = [];
	let lockdown = 0;
	let before: Snap = new Map();
	let targets = new Set<string>(); // 这一轮 edit/write 明确写到的文件（可能不在监视目录里）
	let usedWriters = false;
	let published = false;

	const publish = (): void => {
		const changed = changedBetween(before, snapshot(WATCH(), targets));
		g.__albionLedger = { changed };
		g.__albionTurnFacts = turnFacts(changed, usedWriters);
		if (changed.length || usedWriters) log({ ledger: changed.slice(0, 20), changed: changed.length, used_writers: usedWriters });
	};

	pi.on("agent_start", () => {
		try {
			recent = [];
			lockdown = 0;
			targets = new Set();
			usedWriters = false;
			published = false;
			before = snapshot(WATCH());
			g.__albionLedger = { changed: [] };
		} catch {
			/* ignore */
		}
	});

	pi.on("tool_call", (event: any) => {
		try {
			const name = String(event?.toolName ?? "");
			if (WRITERS.has(name)) usedWriters = true;
			const p = event?.input?.path ?? event?.input?.file_path;
			if ((name === "edit" || name === "write") && typeof p === "string") {
				const abs = absOf(p);
				if (!before.has(abs)) before.set(abs, sigOf(abs)); // 写前的原状
				targets.add(abs);
			}
			if (lockdown > 0) {
				lockdown++;
				const stop = lockdown > LOCKDOWN_LIMIT;
				log({ blocked: name, lockdown, terminate: stop, id: event?.toolCallId });
				return {
					block: true,
					...(stop ? { terminate: true } : {}),
					reason: "这一轮的工具已经停用了，再发多少次都不会执行。现在只用文字回复指挥官：你卡在哪一步、最后一次的真实输出是什么、下一步打算怎么换做法。",
				};
			}
			if (!GUARDED.has(name)) return;
			const key = normKey(event);
			const state = fingerprint(snapshot(WATCH(), targets));
			const seen = recent.filter((r) => r.key === key && r.state === state).length;
			recent.push({ key, state });
			if (recent.length > WINDOW) recent.shift();
			if (seen + 1 < LIMIT) return;
			const over = seen + 1 >= HARD;
			if (over) lockdown = 1;
			log({ blocked: name, seen: seen + 1, lockdown: over, id: event?.toolCallId, preview: key.slice(0, 300) });
			return {
				block: true,
				reason: over
					? `同一段命令你已经重复到第 ${seen + 1} 次了，文件也没变过。这一轮的工具到此停用：不要再发任何工具调用，直接用文字把话说清楚——\n你在哪一步卡住、手上最后一次的真实输出是什么、下一步打算换成什么做法。`
					: `同一段命令你在最近这几步里已经跑过 ${seen} 次了，中间文件也没变，输出就在上面——再跑一遍还是那个结果。\n停一下：把上一次的真实输出读完、说清卡在哪；或者把长代码写成文件再跑、换一件工具、换个做法。原样重发不会通过。`,
			};
		} catch {
			return;
		}
	});

	// 她说完最后一句（不再调工具的那条 message_end）就记账：albion-turn 在 agent_end 读账本给摆件记动作
	pi.on("message_end", (event: any) => {
		try {
			const m = event?.message;
			if (m?.role !== "assistant") return;
			if (Array.isArray(m.content) && m.content.some((b: any) => b?.type === "toolCall")) return;
			publish();
			published = true;
		} catch {
			/* ignore */
		}
	});
	pi.on("agent_end", () => {
		try {
			if (!published) publish(); // 被终止、没有定稿消息的那一轮也照样记账
		} catch {
			/* ignore */
		}
	});
}
