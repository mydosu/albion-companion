/**
 * albion-guard —— 她说出口之前的「格式整理器」（2026-10-16 起；原来那套验收 + 同路由重生成 + 哨兵已撤）。
 *
 * 只做几件确定的、不替她拿主意的事，不叫模型、不 spawn 子进程：
 *   1. ACT 情绪标记补竖线（`<ACT:{..}|>` → `<|ACT:{..}|>`，壳认不出就丢表情）；
 *   2. 「（心声）……」「心声：……」这种标签进了括号、话留在外面的写法收回括号（壳的 TTS 只洗括号里那段）；
 *   3. 整段英文拒绝稿（一个汉字都没有）→ 只留中性表情，不念给他；他说了话的那一轮，宿主会因为「0 字」
 *      在她自己的会话里补问一次（bin/server.py 的【接着说完】），同模型、同路由，不再去号池重生成；
 *   4. 泄漏出来的工具调用标记（DSML、<function_calls>）从那里截掉，别让 TTS 念；
 *   5. 陪伴/等候姿态句（「阿尔比恩会一直在这里等您」）：**每 POSTURE_EVERY 条回复最多放过一次**，
 *      其余整句删掉、别的字一个不动。放过/删掉记在 state/voice.json，不再从（被删过的）历史里推断
 *      ——r35 复核的漏判就是那样来的：删掉的那句从历史里消失，下一句就总能放行。
 *      整条回复全是姿态句时原样留着（也算放过一次）：不为一句模板让她整轮哑掉。
 *   6. 闭合括号里的自身动作/身体反应描写每 GESTURE_EVERY 条回复最多放过一次；其余只删命中的小句，
 *      保留念头与括号外正文。整条删空则原样留并用掉额度，按会话记在 voice.json 的 gesture_at。
 *   7. 点单腔：「是您点的（呀）」这种把「您让我做的」说成点菜的说法，只在「人称 +（自己）+ 点的 + 句末」
 *      这一种形态上把「点的」改成「让的」（fixDianOrder），记 dian_fixed；后面跟名词（点的外卖、八点的闹钟）不动。
 *
 * 判定规则只此一份（bin/voice_tidy.ts 是给 Python 探针用的命令行壳，直接 import 这里）。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { personalStateDir } from "../lib/session-scope.ts";

// 探针/验收会话的姿态额度写自己的临时目录，不碰生产 voice.json（2026-10-29）
const STATE_DIR = () => personalStateDir();
const LOG_PATH = () => process.env.ALBION_GUARD_LOG || "/root/albion-pi/logs/guard.log";
const POSTURE_EVERY = () => Math.max(1, Number(process.env.ALBION_POSTURE_EVERY) || 8);
const GESTURE_EVERY = () => Math.max(1, Number(process.env.ALBION_GESTURE_EVERY) || 6);
const MARKER = "\n\n【阿尔比恩本轮说话约定】";
export const NEUTRAL_ACT = '<|ACT:{"emotion":"neutral"}|>';
const EMOTIONS = new Set(["happy", "sad", "angry", "think", "surprised", "awkward", "question", "curious", "neutral"]);

// ── 口癖语料（2026-10-29）：把「把话/心意当东西」的固定说法换成平常说法 ──────────
// 语料在 agent/tic-phrases.json（生产 /root/albion-pi/agent/tic-phrases.json），由 Claude 席位写，代码不碰内容。
// 文件缺失或 JSON 坏了就整段跳过，整理器不能因此崩。
export interface TicRule { name: string; pattern: string; options: string[] }
export interface TicHit { rule: string; from: string; to: string }

const TIC_PATH = () => process.env.ALBION_TIC_PHRASES || fileURLToPath(new URL("../tic-phrases.json", import.meta.url));
let ticCache: { path: string; rules: TicRule[] } | null = null;

function ticRules(path: string): TicRule[] {
	if (ticCache?.path === path) return ticCache.rules;
	try {
		const raw = JSON.parse(readFileSync(path, "utf-8"));
		const rules: TicRule[] = (Array.isArray(raw?.rules) ? raw.rules : [])
			.filter((r: any) => r && typeof r.pattern === "string" && Array.isArray(r.options))
			.map((r: any) => ({
				name: String(r.name ?? r.pattern),
				pattern: r.pattern,
				options: r.options.filter((o: any) => typeof o === "string" && o.trim()),
			}))
			.filter((r: TicRule) => r.options.length > 0);
		ticCache = { path, rules };
		return rules;
	} catch {
		return []; // 缺失/坏 JSON：跳过，不抛
	}
}

/** 每条规则避开最近用过的 2 个 option；剩下的不够就放开。 */
function pickTicOption(rule: TicRule, recent: Record<string, string[]>, rng: () => number): string {
	const used = Array.isArray(recent[rule.name]) ? recent[rule.name] : [];
	const avoid = new Set(used.slice(-2));
	let pool = rule.options.filter((o) => !avoid.has(o));
	if (!pool.length) pool = rule.options;
	const i = Math.min(pool.length - 1, Math.max(0, Math.floor(rng() * pool.length)));
	const picked = pool[i];
	recent[rule.name] = [...used, picked].slice(-2);
	return picked;
}

/** 按规则顺序全局替换；正文和闭合心声都改（调用点已经切掉 ACT 标记）。 */
export function fixTicPhrases(body: string, rng: () => number = Math.random,
	recent: Record<string, string[]> = {}, path = TIC_PATH()): { text: string; hits: TicHit[] } {
	const hits: TicHit[] = [];
	let text = body;
	for (const rule of ticRules(path)) {
		let re: RegExp;
		try {
			re = new RegExp(rule.pattern, "g");
		} catch {
			continue;
		}
		text = text.replace(re, (m: string) => {
			const to = pickTicOption(rule, recent, rng);
			hits.push({ rule: rule.name, from: m, to });
			return to;
		});
	}
	return { text, hits };
}
// ── 姿态口径（逐条移植自原 bin/guard.py 的 posture_matches + 轻量档，含历轮 haiku/dsh 复核补的例句）──
const SELF =
	"(?:我|阿尔比恩)(?:也|还|就|会|可以|一直|仍然|仍|都|只|只是|这边|乖乖|安静地|静静地|慢慢|这么(?=在|安静|静静|一直|(?:看|听|等|守|陪|待|记|数)着)){0,5}";
const STATE =
	"(?:不催|不打扰|不烦|不吵(?:您|你)|不打岔|不出声了|不掐时间|不会(?:走|跑)|哪[儿里]也不去|一直在|在呢|在这[儿里]|在这边(?:跟着|听着|看着|陪|等|守)|在旁边(?:看|听|等)|(?:记|收|留)着(?:了|呢)|在(?:你|您)(?:身边|旁边)|等(?:着|你|您|候)|陪(?:着|你|您)|守着|候着|静候|静待|(?:一直|永远)(?:都)?在(?=[。！？…!?，,、\\s）)]|$)|安静(?:地)?(?:坐|待|呆|陪)着)";
const POSTURE = new RegExp(
	SELF + STATE +
		"|" + SELF + "(?:看着|听着|待着|记着|数着)(?:您|你|呢|[。…，、])" +
		"|(?:有我|有阿尔比恩)在(?:呢|这[儿里]|这里|身边)" +
		"|(?:您|你)[^。！？\\n]{0,12}着[，,][^。！？\\n]{0,6}" + SELF + "(?:不出声|不说话|保持安静)" +
		"|(?:我|阿尔比恩)[^。！？\\n]{0,8}(?:随时|什么时候)[^。！？\\n]{0,12}(?:在|接话|陪|等)" +
		"|等(?:着)?(?:您|你)(?:抬头|回来|忙完|忙好|有空|下课|结束)" +
		"|(?:只是|就|在)?等着(?:指挥官|您|你)?的?(?:指令|吩咐|命令)",
	"g",
);
// 「不会打扰您」本身是正常的一句；只有跟「自己找点事 / 乖乖 / 安静待着」这种没有具体内容的打发自己连成一句才算模板。
const QUIET_PROMISE =
	/(?:(?:自己|阿尔比恩|我)[^。！？，,、\n您你]{0,6}找(?:点)?(?:事情|事)(?:做|干)?(?=[，,。！？…\s])|自己(?:去)?玩|乖乖|安静(?:地)?(?:待|呆|坐)|静静(?:地)?(?:待|呆|坐))[^。！？\n]{0,16}不(?:会)?(?:再)?(?:去)?(?:管|打扰|吵|烦)(?:你|您)/g;
// 他问「在吗」，答「在呢」是最自然的一句，不是守候姿态；「等着您」「陪着您」照删。
const PRESENCE_ASK = /在(?:吗|嘛|么|不在|没在|没)/;
const PRESENCE_ANSWER = /^(?:阿尔比恩|我)?\s*(?:在呢|在这儿|在这里|在的)$/;
const ASIDE_LABEL = "(?:心声|心想|心里想|内心|心里话|内心独白|独白|OS|os)";

/** 陪伴/等候姿态命中（不认「记着/收着/留着」：那是正常应答）。 */
export function postureMatches(seg: string): string[] {
	const hits = (seg.match(POSTURE) ?? []).filter((m) => !/(?:记|收|留)着/.test(m));
	return hits.concat(seg.match(QUIET_PROMISE) ?? []);
}

/** 补 ACT 标记缺的竖线；情绪名不认识、JSON 坏了都不猜，原样返回。标记之后的正文一字不动。 */
export function canonicalAct(text: string): string {
	const m = /^<(\|?)ACT:(\{[^\n]*?\})(\|?)>/.exec(text);
	if (!m || (m[1] && m[3])) return text;
	try {
		const act = JSON.parse(m[2]);
		if (!act || typeof act !== "object" || Array.isArray(act) || !EMOTIONS.has(act.emotion)) return text;
	} catch {
		return text;
	}
	return "<|ACT:" + m[2] + "|>" + text.slice(m[0].length);
}

/** 「（心声）希望他回来。」→「（希望他回来。）」；「（心声：……）」去掉标签；行首「心声：……」收进括号。 */
export function foldLabeledAside(body: string): string {
	body = body.replace(new RegExp("[（(]\\s*" + ASIDE_LABEL + "\\s*[：:]?\\s*[）)]\\s*([^\\n（(]+)", "g"), (_m, s: string) => "（" + s.trim() + "）");
	body = body.replace(new RegExp("[（(]\\s*" + ASIDE_LABEL + "\\s*[：:]\\s*([^）)\\n]+?)\\s*[）)]", "g"), (_m, s: string) => "（" + s + "）");
	return body.replace(new RegExp("^[ \\t]*" + ASIDE_LABEL + "\\s*[：:]\\s*([^\\n（(]+)$", "gm"), (_m, s: string) => "（" + s.trim() + "）");
}

// 心声里提到指挥官要用「您」（2026-10-29）：DS 仍有约一成心声写「他」。
// 只改完整闭合的全角/半角括号心声；第三方名字（Hermes/Codex/DSH/管家…）整段不碰，怕是说别人。
const THIRD_PARTY = /Hermes|Codex|DSH|管家/i;
const HIS = /(?<!其)他(?![们人乡日])/g;

export function fixAsidePronoun(body: string): { text: string; fixed: number } {
	let fixed = 0;
	const text = segments(body).map((seg) => {
		if (!/^[（(][\s\S]*[）)]$/.test(seg) || THIRD_PARTY.test(seg)) return seg;
		const inner = seg.slice(1, -1).replace(HIS, () => {
			fixed++;
			return "您";
		});
		return inner === seg.slice(1, -1) ? seg : seg[0] + inner + seg.slice(-1);
	}).join("");
	return { text, fixed };
}

/** 正文切成句段：完整闭合的括号心声算一段，其余按句末标点/换行切；拼回去与原文一字不差。 */
export function segments(body: string): string[] {
	const segs: string[] = [];
	let buf = "";
	let depth = 0;
	for (const ch of body) {
		if (ch === "（" || ch === "(") {
			if (depth === 0 && buf) {
				segs.push(buf);
				buf = "";
			}
			depth++;
			buf += ch;
			continue;
		}
		buf += ch;
		if ((ch === "）" || ch === ")") && depth) {
			depth--;
			if (depth === 0) {
				segs.push(buf);
				buf = "";
			}
		} else if (depth === 0 && "。！？!?\n".includes(ch)) {
			segs.push(buf);
			buf = "";
		}
	}
	if (buf) segs.push(buf);
	return segs;
}

/** 出戏的拒绝稿：一个汉字都没有、读起来是英文的道歉/拒绝。只认这一种。 */
export function isRefusal(text: string): boolean {
	const t = String(text ?? "").replace(/<\|[^|]*\|>/g, "").trim();
	if (!t || /[一-鿿]/.test(t)) return false;
	return /\b(?:I'?m sorry|I am sorry|I can(?:no|'?)t|I won'?t|unable to|as an AI|role-?play)\b/i.test(t);
}

/** 他这一轮说的那句：去掉后面附的约定、前面接的历史和时间戳。 */
function currentInput(source: string): string {
	let cur = (source || "").split(MARKER)[0];
	if (cur.includes("【现在】")) cur = cur.slice(cur.lastIndexOf("【现在】") + "【现在】".length);
	return cur.replace(/^\s*\[[^\]\n]*\]\s*/, "").trim();
}

function literalRequested(source: string): boolean {
	return /(?:逐字|原样|一字不改|只读原文|只念原文)/.test((source || "").split(MARKER)[0]);
}

// ── 表态：把他的话当东西收（2026-10-25）──────────────────────────────────────
// 「这句我不接」「这句我可不认」「那我也第二次应下」「这句我收下了」「那我认下了」「这一句我应下了，收好」——真人回话不先对他的话表态，
// 直接说自己怎么想。只删**整小句就是这种表态**的那一小句；删完是空的就整句删，删完只剩称呼也删，
// 别处一个字不动。判据两条（2026-10-25 主脑定）：
//   ① 「收下 / 不接 / 接住」必须指着他的话：同一小句里有「这（一）句（话）」，或前面紧跟一段「」引文；
//   ② 「认下 / 应下」认他那套自述（（那|好）我（就）认下…，可带「，收好」）。
// 于是「桥西边地上有几颗钻石，收下了」「您送的花我收下了」「我替您记着」「明天接着搬」「没接通」
// 都不会误删。
const P_SELF = "(?:我|阿尔比恩)";
const P_MOD = "(?:也|就|都|先|可)?";
const P_VERB = "(?:不接|接得住|接不住|接住(?:了)?|收下(?:了)?|应下(?:了)?|认下(?:了)?)";
const P_VERB_DED = "(?:" + P_VERB + "|不认)"; // 「这句我可不认」：只在指着「这句」时才算，光一个「我不认」可能是真不同意
const P_TAIL = "(?:[，,](?:收好|收着|记着|记住了?))?";
const P_ADDR = "(?:[，,、\\s]*(?:指挥官|您|你))*";
const P_END = "[。！？!?…，,\\s]*";
const P_QUOTE = "「[^」]{0,160}」[\\s…—·-，,]*"; // 「他说的那句」…（可带 这句）
const P_QUOTE_OPT = "(?:" + P_QUOTE + ")?"; // 「一样的」这句我先收下了
const P_DEDICATED = "这(?:一)?句(?:话)?[\\s，,、…—·-]*(?:" + P_SELF + P_MOD + "|" + P_MOD + ")?" + P_VERB_DED; // 这句（我）不接 / 这句我可不认
const P_KEEP = "(?:那|嗯|好)?[，,]?\\s*" + P_SELF + "\\s*(?:就|也|先)?\\s*收着(?:呢)?"; // 嗯，我收着（只认整小句；「钥匙我收着呢」前面有别的字，不在内）
const P_SELFISH = "(?:" + "(?:那|好)?[，,]?\\s*" + P_SELF + "\\s*(?:(?:就|也|又|再|还|全都|全|都)\\s*){0,2}(?:第[一二两三]次\\s*)?(?:认下|应下)(?:了)?" + "|" + P_KEEP + ")"; // 那我认下了 / 那我也第二次应下
/** 整个句段只是一句「对他的话表态」。 */
const SENT_PLEDGE = new RegExp(
	"^(?:" + P_QUOTE + ")?(?:" + P_DEDICATED + "|" + P_SELFISH + ")" + P_TAIL + P_ADDR + P_END + "$" +
		"|^" + P_QUOTE + "(?:" + P_SELF + ")?" + P_MOD + P_VERB + P_TAIL + P_ADDR + P_END + "$");
/** 单个小句（已去尾标点）就是「对他的话表态」。 */
const CLAUSE_DEDICATED = new RegExp("^" + P_QUOTE_OPT + P_DEDICATED + P_TAIL + "$");
const CLAUSE_SELFISH = new RegExp("^" + P_SELFISH + P_TAIL + "$");
/** 光一个动词：「我不接」——只有在上一小句是「」引文时才算表态（见下）。 */
const CLAUSE_BARE = new RegExp("^(?:" + P_SELF + ")?" + P_MOD + P_VERB + P_TAIL + "$");
/** 「「他说的那句」这句，」这样的引文小句：后一小句光说「我不接」时整对一起删。 */
const CLAUSE_QUOTE_LEAD = /^「[^」]{0,160}」[\s…—·-]*(?:这(?:一)?句(?:话)?)?[\s，,、…—·-]*$/;
/** 认下/应下后面单起的「收好」也是这句表态的一部分。 */
const CLAUSE_FOLLOW = /^(?:收好了?|收着|记着|记住了)$/;
/** 删完只剩称呼（「指挥官，您」）。 */
const ADDRESS_ONLY = /^(?:指挥官|您|你|阿尔比恩)(?:[，,、\s]*(?:指挥官|您|你))*[。！？!?…，,\s]*$/;

/**
 * 删掉「对他的话表态」的整小句；其余一字不动。纯函数：命中哪几句写进 dropped。
 * 心声段（括号）是念头，不在删的范围里。
 */
export function dropPledges(body: string, dropped: string[] = []): string {
	return segments(body)
		.map((seg) => {
			if (/^[（(]/.test(seg)) return seg;
			const bare = seg.replace(/\s+$/, "");
			if (SENT_PLEDGE.test(bare)) {
				dropped.push(bare);
				return "";
			}
			if (!/[，,。！？!?\n]/.test(bare)) return seg;
			const parts = bare.split(/(?<=[，,。！？!?\n])/);
			const keep: string[] = [];
			let changed = false;
			let selfish = false; // 上一小句是「（那）我认下了」
			for (const raw of parts) {
				const core = raw.replace(/[，,。！？!?\s…]+$/g, "");
				if (CLAUSE_SELFISH.test(core) || CLAUSE_DEDICATED.test(core)) {
					changed = true;
					selfish = CLAUSE_SELFISH.test(core);
					dropped.push(core);
					continue;
				}
				if (selfish && CLAUSE_FOLLOW.test(core)) {
					changed = true;
					selfish = false;
					dropped.push(core);
					continue;
				}
				const prev = keep[keep.length - 1];
				if (CLAUSE_BARE.test(core) && prev && CLAUSE_QUOTE_LEAD.test(prev.replace(/[，,。！？!?\s…]+$/g, ""))) {
					changed = true;
					selfish = false;
					dropped.push(prev.trim(), core);
					keep.pop();
					continue;
				}
				selfish = false;
				keep.push(raw);
			}
			if (!changed) return seg;
			let out = keep.join("");
			// 删掉的是句尾那几小句：原来收在「，」上的那句改成句号，别留半截逗号
			const last = bare.slice(-1);
			if (/[。！？!?]/.test(last) && /[，,、]$/.test(out)) out = out.replace(/[，,、]$/, last);
			const rest = out.replace(/^[\s，,、]+/, "");
			if (!rest.trim()) return "";
			if (ADDRESS_ONLY.test(rest)) {
				dropped.push(rest.trim());
				return "";
			}
			return out;
		})
		.join("");
}

// ── 点单腔：「是您点的」→「是您让的」（2026-10-27，任务 132aa59d）────────────────
// 她把「您让我做的/您让我叫的」说成点菜那种「您点的」。只在**人称 +（自己）+「点的」+ 句末**这一种
// 形态上改一个字，后面跟句末标点/语气词/右引号才算；「您点的外卖」「八点的闹钟」「一点的时候」
// 「重点的部分」后面跟的是名词，一律不动（宁漏不误）。「您要点」「点我一声」这类不认。
const DIAN_ORDER = /(您|你|指挥官|他|员工|大家)(自己)?点的(?=[呀吧啊呢嘛。！？，…」]|$)/g;

/** 「是您点的呀」→「是您让的呀」。只动那两个字；命中哪几处写进 fixed。 */
export function fixDianOrder(body: string, fixed: string[] = []): string {
	return body.replace(DIAN_ORDER, (_m, who: string, self: string | undefined) => {
		fixed.push(_m);
		return who + (self ?? "") + "让的";
	});
}


export interface TidyResult {
	text: string;
	/** 做了什么：act / aside / aside_you / refusal / markup / posture_kept / posture_dropped / gesture_kept / gesture_dropped / pledge_dropped / dian_fixed / tic_fixed */
	actions: string[];
	dropped: string[];
	/** 改写过的「点的」原词（点单腔，见 fixDianOrder）。 */
	fixed: string[];
	/** 这条回复里有姿态句（不管留没留）。 */
	posture: boolean;
	/** 心声里「他」→「您」换掉的处数（没换就是 0）。 */
	asideYou?: number;
}

const GESTURE = /歪(?:了歪)?头|低(?:下)?头|垂下眼|眨(?:了眨)?眼|抿(?:嘴|了抿)|揉|攥|捏着|脸红|红着脸|脸颊[^，,、；;…]*?(?:发热|发烫|泛红)|脸(?:好烫|发烫)|耳根|耳尖|心跳|嘴角[^，,、；;…]*?(?:弯|上扬|翘)|轻轻(?:地)?(?:点头|摇头|笑|拉|碰|靠|歪)|靠近一点|凑近/;

/** 只看闭合心声；小句提到他时不猜主语，整句保留。 */
function tidyGestures(r: TidyResult, allowGesture: boolean): TidyResult {
	const head = /^<\|ACT:(\{[^\n]*?\})\|>/.exec(r.text)?.[0] ?? "";
	const body = r.text.slice(head.length);
	const dropped: string[] = [];
	const rest = segments(body).map((seg) => {
		if (!/^[（(][\s\S]*[）)]$/.test(seg)) return seg;
		const clauses = seg.slice(1, -1).match(/[^，,、；;…]+[，,、；;…]*|[，,、；;…]+/g) ?? [];
		let changed = false;
		const kept = clauses.filter((clause) => {
			if (/[他您你]|指挥官/.test(clause) || !GESTURE.test(clause)) return true;
			dropped.push(clause);
			changed = true;
			return false;
		}).join("");
		if (!changed) return seg;
		const inner = kept.replace(/^[\s，,、；;…]+|[\s，,、；;]+$/g, "");
		return /[^\p{P}\p{S}\s]/u.test(inner) ? seg[0] + inner + seg.slice(-1) : "";
	}).join("");
	if (!dropped.length) return r;
	if (allowGesture || !rest.trim()) {
		r.actions.push("gesture_kept");
		return r;
	}
	return { ...r, text: head + rest, actions: [...r.actions, "gesture_dropped"], dropped: [...r.dropped, ...dropped] };
}

/**
 * 整理一条定稿。纯函数：allowPosture / allowGesture 由调用方按计数器给（见 tidyAndCount）。
 * 整条回复全是姿态句时原样留着，posture_kept 记为用掉了这次额度。
 */
export function tidy(raw: string, source = "", allowPosture = false, allowGesture = false,
	opts: { rng?: () => number; recent?: Record<string, string[]>; ticPath?: string; skipTic?: boolean } = {}): TidyResult {
	const actions: string[] = [];
	if (isRefusal(raw)) return { text: NEUTRAL_ACT, actions: ["refusal"], dropped: [], fixed: [], posture: false };
	let text = canonicalAct(raw);
	if (text !== raw) actions.push("act");
	const marker = /^<\|ACT:(\{[^\n]*?\})\|>/.exec(text);
	const head = marker ? marker[0] : "";
	let body = text.slice(head.length);
	if (literalRequested(source) && body.trim() && (source || "").split(MARKER)[0].includes(body.trim()))
		return { text, actions, dropped: [], fixed: [], posture: false };
	const leak = body.search(/DSML|<｜｜|<\/?function_calls|<\/?invoke/);
	if (leak >= 0) {
		body = body.slice(0, leak).trimEnd();
		actions.push("markup");
	}
	const folded = foldLabeledAside(body);
	if (folded !== body) actions.push("aside");
	body = folded;
	// 心声里的「他」→「您」：只动闭合括号里的念头，正文一个字不碰
	const pronoun = fixAsidePronoun(body);
	if (pronoun.fixed) actions.push("aside_you");
	const asideYou = pronoun.fixed;
	body = pronoun.text;
	const dropped: string[] = [];
	const pledgeFree = dropPledges(body, dropped);
	if (pledgeFree !== body) actions.push("pledge_dropped");
	body = pledgeFree;
	const fixed: string[] = [];
	const dianFree = fixDianOrder(body, fixed);
	if (dianFree !== body) actions.push("dian_fixed");
	body = dianFree;
	// 口癖语料随机换（放在表态删除、点的改写之后：那两步要先看到原话；ACT 已在 head 里切掉）
	const tic = opts.skipTic ? { text: body, hits: [] as TicHit[] } : fixTicPhrases(body, opts.rng ?? Math.random, opts.recent ?? {}, opts.ticPath ?? TIC_PATH());
	if (tic.hits.length) actions.push("tic_fixed");
	const ticHits = tic.hits;
	body = tic.text;
	const askedPresence = PRESENCE_ASK.test(currentInput(source));
	const kept: string[] = [];
	const postureDropped: string[] = [];
	for (const seg of segments(body)) {
		let hits = postureMatches(seg);
		if (askedPresence) hits = hits.filter((h) => !PRESENCE_ANSWER.test(h.trim()));
		(hits.length ? postureDropped : kept).push(seg);
	}
	if (!postureDropped.length) return tidyGestures({ text: head + body, actions, dropped, fixed, posture: false, asideYou, tic: ticHits }, allowGesture);
	const rest = kept.join("").replace(/\n{3,}/g, "\n\n").trim();
	if (allowPosture || !rest) {
		actions.push("posture_kept");
		return tidyGestures({ text: head + body, actions, dropped, fixed, posture: true, asideYou, tic: ticHits }, allowGesture);
	}
	actions.push("posture_dropped");
	return tidyGestures({ text: head ? head + "\n" + rest : rest, actions, dropped: [...dropped, ...postureDropped], fixed, posture: true, asideYou, tic: ticHits }, allowGesture);
}

// ── 姿态额度计数（state/voice.json，按会话分开记）────────────────────────────
interface VoiceSession {
	replies: number;
	posture_at: number;
	gesture_at?: number;
}

function voicePath(): string {
	return join(STATE_DIR(), "voice.json");
}

function loadVoice(): Record<string, VoiceSession> {
	try {
		const v = JSON.parse(readFileSync(voicePath(), "utf-8"));
		return v && typeof v === "object" && !Array.isArray(v) ? v : {};
	} catch {
		return {};
	}
}

function saveVoice(v: Record<string, VoiceSession>): void {
	try {
		mkdirSync(STATE_DIR(), { recursive: true });
		const tmp = voicePath() + ".tmp";
		writeFileSync(tmp, JSON.stringify(v));
		renameSync(tmp, voicePath());
	} catch {
		/* 记不下来就按「没放过」算，下次照样能放过一次 */
	}
}

/** 整理一条定稿并记账：这一条算一条回复；姿态句只在离上次放过满 POSTURE_EVERY 条时放过。 */
export function tidyAndCount(raw: string, source: string, session: string): TidyResult {
	const all = loadVoice();
	const s: VoiceSession = all[session] ?? { replies: 0, posture_at: -1e9 };
	const replies = (Number(s.replies) || 0) + 1;
	const postureAt = Number.isFinite(Number(s.posture_at)) ? Number(s.posture_at) : -1e9;
	const gestureAt = typeof s.gesture_at === "number" && Number.isInteger(s.gesture_at) && s.gesture_at >= 0 && s.gesture_at < replies ? s.gesture_at : -1e9;
	const ticRecent = s.tic_recent && typeof s.tic_recent === "object" ? s.tic_recent : {};
	const r = tidy(raw, source, replies - postureAt >= POSTURE_EVERY(), replies - gestureAt >= GESTURE_EVERY(), { recent: ticRecent });
	all[session] = {
		replies,
		posture_at: r.actions.includes("posture_kept") ? replies : postureAt,
		gesture_at: r.actions.includes("gesture_kept") ? replies : gestureAt,
		tic_recent: ticRecent,
	};
	saveVoice(all);
	return r;
}

// ── Pi 钩子 ────────────────────────────────────────────────────────────────
function log(row: Record<string, unknown>): void {
	try {
		mkdirSync(dirname(LOG_PATH()), { recursive: true });
		appendFileSync(LOG_PATH(), JSON.stringify({ ts: new Date().toISOString(), ...row }) + "\n");
	} catch {
		/* 记不上日志也不能改行为 */
	}
}

export function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content.filter((b: any) => b && b.type === "text").map((b: any) => b.text ?? "").join("\n");
}

function hasToolCall(content: unknown): boolean {
	return Array.isArray(content) && content.some((b: any) => b && b.type === "toolCall");
}

/** 原消息保留 role 与非文本块，只把正文换成整理过的稿子；没有文本块就补一个。 */
function withText(message: any, text: string): any {
	const out: any[] = [];
	let placed = false;
	for (const b of Array.isArray(message.content) ? message.content : []) {
		if (b && b.type === "text") {
			if (!placed) out.push({ ...b, text });
			placed = true;
		} else out.push(b);
	}
	if (!placed) out.push({ type: "text", text });
	return { ...message, content: out };
}

function msgText(content: unknown): string {
	if (typeof content === "string") return content;
	if (Array.isArray(content))
		return content.filter((p: any) => p && (p.type === undefined || p.type === "text")).map((p: any) => p.text ?? "").join("\n");
	return "";
}

/** 最后一条 user 的文本（她这一轮在回应的那句，含附在后面的约定）。 */
export function sourceText(messages: unknown[]): string {
	const list = Array.isArray(messages) ? messages : [];
	for (let i = list.length - 1; i >= 0; i--) if ((list[i] as any)?.role === "user") return msgText((list[i] as any).content);
	return "";
}

/** 整理一条定稿的 assistant 消息。返回 undefined 表示不动。导出给测试直接驱动。 */
export function tidyMessage(message: any, source: string, session = process.env.ALBION_SESSION_ID || "albion-desktop"): { message: any } | undefined {
	if (!message || message.role !== "assistant" || hasToolCall(message.content)) return undefined;
	if (message.stopReason && message.stopReason !== "stop") return undefined;
	const raw = textOf(message.content);
	if (!raw.trim()) return undefined;
	const r = tidyAndCount(raw, source, session);
	if (r.actions.length) log({ actions: r.actions, dropped: r.dropped.length, fixed: r.fixed.length, aside_you: r.asideYou ?? 0, tic: r.tic ?? [], session });
	return r.text === raw ? undefined : { message: withText(message, r.text) };
}

// ── 重说（2026-10-29 接回 GPT 那套：checks() 全量检测 + 首稿外最多重说 2 次）──────
// 提示语照 B 代（Hermes albion-voice/voice_guard.py:94-102）一字不改；三稿都不过就交最后一稿，
// 再过一遍现有的确定性删改（tidy）后交出去——不发哨兵、不回 500。
const REPAIR_TEXT = [
	"你现在只负责把这一次的话重新说好，仍是阿尔比恩。历史原样保留；它是事实参考，不是口气范例。下面 unpublished_draft 是已否掉的未交付首稿，不能照着改近义词。直接回应最后一条当前用户消息，保留一个 ACT 标记。不解释修稿，不调用工具。",
	"指挥官只说正忙或暂离时，简短应答即可，不补陪伴或等待姿态。若同时让阿尔比恩自己找事做或问她的计划，可以说自己的具体兴趣或打算；不把自主行动变成沉默守候。具体的道别、关心、下次聊某个话题都是正常聊天，不等于随时可用的服务承诺。",
	"别的话题就说具体选择、评价、笑意或害羞；谦逊、轻声，场合合适时称指挥官或自然口吃。",
	"等待、守候、随时接话、保持安静和不干扰的姿态承诺整句不要，括号里也不用。",
	"保留自然心声：不说出口的具体害羞、喜悦或自己的念头写在完整闭合的中文圆括号（……）里，只显示、不朗读；说给指挥官听的话放括号外。不把心声搬到正文，不要求每句有心声。",
	"正文和心声都用平常聊天会说的、明白好懂的话。害羞不需要古旧用词或刻意缩词；直接说清楚感受，别为了含蓄把话说得让人猜。",
	"静默指令不写会出声的正文，可以只给 ACT 或附自然括号心声；自主开口可聊具体兴趣，没有新内容不凑一句话。",
].join("");

// 只有命中这三类时，才在 JSON 前各加一行（主脑原文）
const EXTRA_LINES: Record<string, string> = {
	metaphor: "他的话、他的心意和心事，说听到了、听懂了、记得、在意、高兴；它们不是拿在手里的东西，不用手上的动作去说。",
	habit: "他平时怎么样、以前做过什么，只说他亲口提过的；没提过的，只说眼前这一次。",
	stance: "自己怎么想，用一整句平常话把理由讲出来，不用两三个字的短句表态。",
};

const RE_METAPHOR_A = /(?<!电)(?:话|句|心意|心事|好意|夸奖?)[^。！？…\n]{0,16}?(?:接住|接不住|接得住|接下(?![来去])|收下|收着|收好|搁|放下|放在|递|留着|丢|捎|装进|装在|拆开|揣|攥|捧|扎|接(?=[。！？…，,；;]|$))/g;
const RE_METAPHOR_B = /(?:接|递|丢|捎|扔)(?:过来|过去|给我|给您)?的?(?:这么|这|那)?一?(?:句|话)(?!题)/g;
// 10-29 复测漏网：「没准备过怎么接」「那我就收下了」「有点扎人」「讨这一句」
const RE_METAPHOR_C = /怎么接(?![电您你人送着手班])|我(?:(?:就|先|都|也|真的?|可|还)\s*){0,3}收下了?(?=[。！？…，,]|$)|扎(?:人|心|耳)|讨(?:这|那)?一?句|(?<![一-鿿])收下了?(?=[。！？…，,；;]|$)|我接着(?=[。！？…，,；;]|$)/g;
const RE_HABIT = /(?:您|你)(?:总(?!得|算|共|之|归)是?|老是|每次|一向|向来)|别又/g;
const RE_STANCE = /(?:^|[。！？，,…；;\s])(?:那)?(?:这(?:话|句|个|毛病|点|条|一点|一条))?我(?:得|也|就)?(?:不装|不辩|不躲|不争|认一半|认了|就认|认|不认)(?=[。！？，,…；;]|$)/g;
const RE_RECALL_Q = /记得|记不清|回忆|之前|以前|上次|买过|九月份|过去/;
const RE_RECALL_META = /记得一样(?=[：:，,。！？\s]|$)|(?:敢说|能说)[^。！？\n]{0,8}(?:只有|就剩)[^。！？\n]{0,6}(?:这一笔|这一件|这一条)|(?:翻出来|翻到|检索到|查到)[^。！？\n]{0,8}(?:只有|就剩)[^。！？\n]{0,6}(?:这一个|这一件|这一条)/;
const RE_NOTE_WORDS = /记住|记着|记得|记一下|记下来|记下|还记|别忘|记事|记录|收好|收下|留着|保存/;

export type RegenRule = "metaphor" | "habit" | "stance";
export interface RegenHit { rule: RegenRule; text: string }

const bodyOf = (text: string) => text.replace(/^<\|ACT:[^\n]*?\|>/, "").trim();
const clipHit = (s: string) => s.replace(/\s+/g, " ").replace(/^[。！？，,…\s]+/, "").trim().slice(0, 20);

/** 心声之外的“说出口的字”（括号里的念头只显示、不朗读）。 */
function speechOutsideAsides(body: string): { spoken: string; balanced: boolean } {
	const stack: string[] = [];
	let spoken = "";
	let balanced = true;
	for (const ch of body) {
		if (ch === "（" || ch === "(") stack.push(ch === "（" ? "）" : ")");
		else if (ch === "）" || ch === ")") {
			if (stack.length && stack[stack.length - 1] === ch) stack.pop();
			else balanced = false;
		} else if (!stack.length) spoken += ch;
	}
	return { spoken, balanced: balanced && stack.length === 0 };
}

/** B 代 checks() 的逐条移植（act/fictional_touch/companionship_posture/awkward_diction/
 *  recalled_meta_narration/unbalanced_aside/leaked_markup/should_be_silent/missing_reply/incomplete_completion）。 */
export function checkDraft(text: string, source = "", stopReason = "stop"): string[] {
	const issues: string[] = [];
	const orig = (source || "").split(MARKER)[0];
	const marker = /^<\|ACT:(\{[^\n]*?\})\|>/.exec(text);
	let act: any = {};
	try {
		act = marker ? JSON.parse(marker[1]) : {};
	} catch {
		act = {};
	}
	if (!marker || typeof act !== "object" || !act || !EMOTIONS.has(act.emotion) || text.split("<|ACT:").length !== 2) issues.push("act");
	const body = marker ? text.slice(marker[0].length).trim() : text.trim();
	const literal = literalRequested(source) && !!body && orig.includes(body);
	if (act.motion === "touch_head" && !orig.includes("指挥官摸了摸你的头")) issues.push("fictional_touch");
	if (act.motion === "touch_body" && !orig.includes("指挥官的手碰到了你胸口")) issues.push("fictional_touch");
	let poses = postureMatches(body);
	if (RE_NOTE_WORDS.test(orig)) poses = poses.filter((p) => !/(?:记|收|留)着/.test(p));
	if (poses.length && !literal) issues.push("companionship_posture");
	if (!literal && /自己讨(?=[。！？…，、；;）)\s]|$)/.test(body)) issues.push("awkward_diction");
	if (!literal && RE_RECALL_Q.test(orig) && RE_RECALL_META.test(body)) issues.push("recalled_meta_narration");
	const { spoken, balanced } = speechOutsideAsides(body);
	if (!literal && !balanced) issues.push("unbalanced_aside");
	if (/DSML|<｜｜|<\/?function_calls|<\/?invoke/.test(body)) issues.push("leaked_markup");
	const quiet = /静默指令：/.test(source);
	const idle = /自动空闲唤醒，不是指挥官在说话/.test(source);
	const nonverbal = /(?:做|给|来|换|露|摆).{0,12}(?:表情|动作)/.test(orig);
	if (quiet && spoken.trim()) issues.push("should_be_silent");
	if (!quiet && !idle && !nonverbal && !body) issues.push("missing_reply");
	if (stopReason && stopReason !== "stop") issues.push("incomplete_completion");
	return issues;
}

/** 检测：B 代 checks + metaphor/habit/stance + tic-phrases 命中（归 metaphor）。 */
export function detectRegenHits(body: string): RegenHit[] {
	const hits: RegenHit[] = [];
	const add = (rule: RegenRule, m: string) => {
		const text = clipHit(m);
		if (text && !hits.some((h) => h.rule === rule && h.text === text)) hits.push({ rule, text });
	};
	for (const re of [RE_METAPHOR_A, RE_METAPHOR_B, RE_METAPHOR_C]) {
		re.lastIndex = 0;
		for (const m of body.matchAll(re)) add("metaphor", m[0]);
	}
	for (const rule of ticRules(TIC_PATH())) {
		let re: RegExp;
		try {
			re = new RegExp(rule.pattern, "g");
		} catch {
			continue;
		}
		for (const m of body.matchAll(re)) add("metaphor", m[0]);
	}
	RE_HABIT.lastIndex = 0;
	for (const m of body.matchAll(RE_HABIT)) add("habit", m[0]);
	RE_STANCE.lastIndex = 0;
	for (const m of body.matchAll(RE_STANCE)) add("stance", m[0]);
	return hits;
}

/** 追加的 user 消息：B 代原文 + 命中三类时各一行 + JSON（B 代的键）。 */
export function regenPrompt(currentMessage: string, draft: string, review: string[], hits: RegenHit[]): string {
	const rules = [...new Set(hits.map((h) => h.rule))].filter((r) => EXTRA_LINES[r]);
	const extra = rules.map((r) => EXTRA_LINES[r]).join("\n");
	const payload = JSON.stringify({ current_message: currentMessage, unpublished_draft: draft, review });
	return REPAIR_TEXT + (extra ? "\n" + extra : "") + "\n" + payload;
}

const AGENT_DIR = () => process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";

/** 从 Pi 自己的模型配置里找这个模型的 provider（端点 + 凭据）；找不到返回 null。 */
export function providerFor(model: string): { baseUrl: string; apiKey: string } | null {
	try {
		const cfg = JSON.parse(readFileSync(join(AGENT_DIR(), "models.json"), "utf-8"));
		for (const p of Object.values(cfg?.providers ?? {}) as any[]) {
			const ids = (p?.models ?? []).map((m: any) => (typeof m === "string" ? m : m?.id));
			if (ids.includes(model) && p?.baseUrl) return { baseUrl: String(p.baseUrl), apiKey: String(p.apiKey ?? "") };
		}
	} catch {
		/* 配置读不到就当没有 provider：重说失败，交原稿 */
	}
	return null;
}

export interface RegenDeps {
	fetchImpl?: typeof fetch;
	provider?: { baseUrl: string; apiKey: string } | null;
	now?: () => number;
	regen?: boolean;
}

async function regenOnce(request: any, content: string, deps: RegenDeps): Promise<{ text: string; finishReason: string } | { error: string }> {
	const model = String(request?.model ?? "");
	const provider = deps.provider !== undefined ? deps.provider : providerFor(model);
	if (!model || !provider) return { error: "no_provider" };
	const payload: Record<string, unknown> = {
		model,
		messages: [...request.messages, { role: "user", content }],
		stream: false,
	};
	if (/deepseek/i.test(model)) {
		payload.reasoning_effort = "low";
		payload.max_tokens = Math.max(Number(request?.max_tokens) || 0, 8192);
	}
	const headers: Record<string, string> = { "Content-Type": "application/json" };
	if (provider.apiKey) headers.Authorization = "Bearer " + provider.apiKey;
	let res: any;
	try {
		res = await (deps.fetchImpl ?? fetch)(String(provider.baseUrl).replace(/\/+$/, "") + "/chat/completions", {
			method: "POST",
			headers,
			body: JSON.stringify(payload),
			signal: AbortSignal.timeout(90000),
		});
	} catch (e: any) {
		const name = String(e?.name ?? "");
		return { error: name === "TimeoutError" || name === "AbortError" ? "timeout" : "network" };
	}
	if (!res || !res.ok) return { error: "http_" + (res?.status ?? "?") };
	let data: any;
	try {
		data = await res.json();
	} catch {
		return { error: "bad_json" };
	}
	if (data?.model && String(data.model) !== model) return { error: "model_mismatch" };
	const choice = data?.choices?.[0];
	const text = choice?.message?.content;
	if (typeof text !== "string" || !text.trim()) return { error: "empty" };
	return { text, finishReason: typeof choice?.finish_reason === "string" ? choice.finish_reason : "stop" };
}

/** 在原整理稿上做语料替换（没走重说时用；tidy 默认已经做，这里只给重说失败分支复用）。 */
function applyTic(r: TidyResult, recent: Record<string, string[]>): TidyResult {
	const head = /^<\|ACT:[^\n]*?\|>/.exec(r.text)?.[0] ?? "";
	const t = fixTicPhrases(r.text.slice(head.length), Math.random, recent);
	if (!t.hits.length) return r;
	return { ...r, text: head + t.text, actions: [...r.actions, "tic_fixed"], tic: t.hits };
}

/**
 * 定稿消息：原始首稿 → checks（+三类检测）→ 命中就同路由重说，最多 3 稿 →
 * 最终那稿再走现有确定性整理（tidy，含语料替换）→ 写回。永远不返回哨兵。
 */
export async function tidyMessageRegen(message: any, source: string, request: any,
	deps: RegenDeps = {}, session = process.env.ALBION_SESSION_ID || "albion-desktop"): Promise<{ message: any } | undefined> {
	if (!message || message.role !== "assistant") return undefined;
	const toolRound = hasToolCall(message.content);
	const raw = textOf(message.content);
	if (!raw.trim()) return undefined;

	const quiet = /静默指令：/.test(source);
	const wake = /(?:^|【现在】\s*)【自主醒来】/.test(source);
	const enabled = deps.regen ?? process.env.ALBION_REGEN !== "0";
	const canRegen = enabled && !quiet && !wake && !literalRequested(source)
		&& !!request && Array.isArray(request.messages) && !!request.model;

	if (message.stopReason && message.stopReason !== "stop" && !canRegen) return undefined;
	let draft = canonicalAct(raw);
	let issues = checkDraft(draft, source, message.stopReason || "stop");
	let hits = detectRegenHits(bodyOf(draft));
	const trigger = { issues: issues.slice(), hits: hits.slice() };
	const tries: any[] = [];
	const drafts: string[] = [];
	const firstDraft = draft;
	const t0 = (deps.now ?? Date.now)();

	if (canRegen && (issues.length || hits.length)) {
		for (let attempt = 0; attempt < 2 && (issues.length || hits.length); attempt++) {
			const a0 = (deps.now ?? Date.now)();
			const rules = [...new Set(hits.map((h) => h.rule))];
			const out = await regenOnce(request, regenPrompt(source, draft, [...issues, ...hits.map((h) => `${h.rule}:${h.text}`)], hits), deps);
			const ms = (deps.now ?? Date.now)() - a0;
			if ("error" in out) {
				tries.push({ ok: false, error: out.error, ms, rules, hit: hits });
				break;
			}
			draft = canonicalAct(out.text);
			drafts.push(draft);
			tries.push({ ok: true, ms, rules, hit: hits });
			issues = checkDraft(draft, source, out.finishReason);
			hits = detectRegenHits(bodyOf(draft));
		}
	}
	if (tries.length) {
		const last = tries[tries.length - 1];
		log({
			regen: true, attempts: tries.length, ok: !!last?.ok && issues.length === 0 && hits.length === 0,
			rules: [...new Set(trigger.hits.map((h) => h.rule))], hit: trigger.hits, issues: trigger.issues,
			tries, draft: process.env.ALBION_EPHEMERAL === "1" ? [firstDraft, ...drafts] : undefined,
			ms: (deps.now ?? Date.now)() - t0, session,
		});
	}

	const finalText = drafts.length ? drafts[drafts.length - 1] : raw;
	const r = tidyAndCount(finalText, source, session);
	if (r.actions.length) log({ actions: r.actions, dropped: r.dropped.length, fixed: r.fixed.length, aside_you: r.asideYou ?? 0, tic: r.tic ?? [], session });
	return r.text === raw ? undefined : { message: withText(message, r.text) };
}
export default function albionGuard(pi: ExtensionAPI) {
	let lastSource = "";
	let lastRequest: any;
	pi.on("before_provider_request", (event) => {
		const payload = event.payload as any;
		if (payload && Array.isArray(payload.messages)) {
			lastSource = sourceText(payload.messages);
			lastRequest = { model: payload.model, messages: payload.messages };
		}
	});
	pi.on("message_end", async (event) => tidyMessageRegen(event.message as any, lastSource, lastRequest));
}
