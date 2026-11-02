/**
 * albion-play —— 她真能玩的文字冒险（2026-10-14）。
 *
 * 为什么要这件工具：叫她「去玩没玩完的游戏」时，她读了旧进度、往心事本里抄一段、
 * 说「又往前走了点」，其实一步都没走（实测）。Colossal Cave Adventure（/usr/games/adventure）
 * 是交互程序：每次单独跑都从头开始，自带的 suspend 存档又要等 45 分钟才能恢复，她自己很难接着玩。
 *
 * 做法：游戏进程常驻在这个扩展里（RPC 会话是长驻的），一轮里走几步就把几行命令喂进去、
 * 收这几步的回应交给她；每一步追加进 /root/家/游戏/adventure.moves。
 * 会话重启后进程没了，下次调用时按记录把走过的步子重放一遍再接着走（游戏里有随机的矮人，
 * 重放偶尔会和原来不完全一样，回应里会说明）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync } from "node:fs";
import { dirname } from "node:path";

const GAME = process.env.ALBION_ADVENTURE_BIN || "/usr/games/adventure";
// 2026-10-15 实测：管道接住时游戏用全缓冲，输出不出来（看起来像游戏不理人）。
// 用 stdbuf 关掉缓冲；没有 stdbuf 就照原来直接跑。
const STDBUF = "/usr/bin/stdbuf";
const MOVES = process.env.ALBION_ADVENTURE_MOVES || "/root/家/游戏/adventure.moves";
const IDLE_MS = 350; // 输出停下这么久就算这一步回完了
const STEP_MAX_MS = 5000;
const MAX_MOVES_PER_CALL = 12;
const MAX_REPLY_CHARS = 3500;

let proc: ChildProcessWithoutNullStreams | null = null;
let buf = "";

const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

/** 等输出安静下来，取走这段输出。 */
async function drain(): Promise<string> {
	const start = Date.now();
	let last = buf.length;
	let quietSince = Date.now();
	while (Date.now() - start < STEP_MAX_MS) {
		await new Promise((r) => setTimeout(r, 50));
		if (!proc) break;
		if (buf.length !== last) {
			last = buf.length;
			quietSince = Date.now();
		} else if (Date.now() - quietSince >= IDLE_MS && buf.length > 0) break;
	}
	const out = buf;
	buf = "";
	return out;
}

function loggedMoves(): string[] {
	try {
		return readFileSync(MOVES, "utf-8").split("\n").map((s) => s.trim()).filter(Boolean);
	} catch {
		return [];
	}
}

async function send(move: string): Promise<string> {
	if (!proc) return "";
	proc.stdin.write(move + "\n");
	return drain();
}

/** 进程不在就起一个；有走过的记录就按记录重放。返回给她看的开场说明。 */
async function ensureGame(): Promise<string> {
	if (proc) return "";
	buf = "";
	proc = existsSync(STDBUF)
		? spawn(STDBUF, ["-o0", "-e0", GAME], { stdio: ["pipe", "pipe", "pipe"] })
		: spawn(GAME, [], { stdio: ["pipe", "pipe", "pipe"] });
	proc.stdout.setEncoding("utf-8");
	proc.stderr.setEncoding("utf-8");
	proc.stdout.on("data", (d: string) => (buf += d));
	proc.stderr.on("data", (d: string) => (buf += d));
	proc.on("exit", () => (proc = null));
	let intro = await drain();
	// 开场会问「要不要说明」，不答就一直卡在这句上，第一步会被当成回答吃掉。
	if (/instructions/i.test(intro)) intro += await send("no");
	const old = loggedMoves();
	if (!old.length) return `（新开了一局。）\n${intro}`;
	let lastOut = "";
	for (const m of old) lastOut = await send(m);
	return `（游戏进程重开过，按记录重放了 ${old.length} 步，接着上次的地方；里面有随机的矮人，重放偶尔会和原来不完全一样。）\n上一步的情形：\n${lastOut.trim()}\n`;
}

export default function albionPlay(pi: ExtensionAPI) {
	pi.registerTool({
		name: "adventure",
		label: "玩文字冒险",
		description:
			"玩我在家里下的那款文字冒险 Colossal Cave Adventure（英文命令，如 look、in、take lamp、north、xyzzy）。" +
			"moves 里写这一回要走的几步（每行一步，一次最多 12 步），工具会真的把它们输进游戏、把游戏的回应交给我；" +
			"走过的步子自动记在 /root/家/游戏/adventure.moves，下次接着走。只照着工具返回的说我走到哪了，没走过的不说。",
		promptSnippet: "adventure：真的玩那款文字冒险，走几步、看游戏怎么回",
		parameters: Type.Object({
			moves: Type.String({ description: "这一回要输入的游戏命令，每行一条（英文），例如 \"look\\nin\\ntake lamp\"" }),
		}),
		async execute(_toolCallId, params) {
			const moves = String(params.moves || "")
				.split(/\n|；|;/)
				.map((s) => s.trim())
				.filter(Boolean)
				.slice(0, MAX_MOVES_PER_CALL);
			if (!existsSync(GAME)) return text(`这台电脑上找不到游戏程序（${GAME}），这次玩不了。`);
			let head = "";
			try {
				head = await ensureGame();
			} catch (e) {
				proc = null;
				return text(`游戏没起来：${String((e as Error)?.message || e).slice(0, 120)}`);
			}
			if (!moves.length) return text(`${head}（没给要走的步子；想看看周围就用 look。）`);
			if (!existsSync(dirname(MOVES))) mkdirSync(dirname(MOVES), { recursive: true });
			const parts: string[] = [];
			for (const m of moves) {
				if (!proc) break;
				const out = await send(m);
				appendFileSync(MOVES, m + "\n", "utf-8");
				parts.push(`> ${m}\n${out.trim()}`);
			}
			let tail = "";
			if (!proc) {
				// 这一局结束了（quit / 死了）：把记录挪开，下次重新开局
				const done = `${MOVES}.done-${Date.now()}`;
				try {
					renameSync(MOVES, done);
				} catch {
					/* ignore */
				}
				tail = `\n（这一局结束了，走过的记录存到了 ${done}；下次是新开的一局。）`;
			}
			const body = (head + parts.join("\n\n") + tail).slice(-MAX_REPLY_CHARS);
			// r39 实测：她拿到英文原文后照着想象编了房间、箱子、地图——原文后面写明只照原文说
			return text(`【文字冒险·这一回真走了 ${parts.length} 步】\n${body}\n（上面是游戏原文。告诉他时照原文翻成中文说，原文里没有的地方、东西、动作一个都不加。）`);
		},
	});
}
