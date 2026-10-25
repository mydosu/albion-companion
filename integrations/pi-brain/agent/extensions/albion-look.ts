/**
 * albion-look —— 她自己的眼睛：想看就看，不用等壳替她取（2026-10-14）。
 *
 * 三路：
 *   his  = 指挥官日常那台电脑的屏幕 —— 原图交给她自己看（图片工具结果，主模型直接看）
 *   mine = 她自己所在那台电脑的屏幕 —— 同上
 *          （主模型不收图时——比如 DS 文本模型——这两路原图交号池 grok-4.7 转述，她拿文字描述；
 *           grok 不成再退到本地视觉模型）
 *   cam  = 这台电脑的摄像头·真实世界 —— 照片只交给本地视觉模型（局域网里的 Qwen3VL），
 *          她拿到的是文字描述；照片本身不进主模型、不出局域网（指挥官定）。
 *
 * 配置在 agent/.env（不进仓库），每次调用现读，改了不用重启：
 *   ALBION_EYES_MINE_URL / ALBION_EYES_MINE_TOKEN   本机采集端（WSL mirrored 网络下走 127.0.0.1）
 *   ALBION_EYES_HIS_URL  / ALBION_EYES_HIS_TOKEN    主用电脑的采集端（可写多个、逗号分隔：局域网在前、Tailscale 在后）
 *   ALBION_EYES_VLM                                 本地视觉模型（OpenAI 兼容）
 *
 * 硬底线（她改不了）：同一路 60 秒内不重复取；摄像头照片永远只走本地视觉模型。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync } from "node:fs";
import { connect } from "node:net";
import { join } from "node:path";
import { requestCapture } from "../eyes-http.ts";

type Target = "his" | "mine" | "cam";

const LABEL: Record<Target, string> = {
	his: "指挥官日常那台电脑的屏幕",
	mine: "我自己所在那台电脑的屏幕",
	cam: "这台电脑的摄像头·真实世界",
};
const SAME_TARGET_GAP_MS = 60_000;
// 取图（含传完）上限。主用机走 Tailscale 中继时一张整屏要 35–60 秒（2026-10-15 实测，直连时 1–3 秒），30 秒会稳定超时；中继再慢就只能修网络
const SHOT_TIMEOUT_MS = 60_000;
// grok 转述截图的上限：超了就退到本地视觉模型（总耗时仍有上界）
const GROK_TIMEOUT_MS = 75_000;
const VLM_MODEL = "/models/Qwen3VL-4B-Instruct-Q4_K_M.gguf";
const CAM_PROMPT = [
	"这是电脑摄像头拍下的一张真实世界的照片（不是屏幕截图）。用中文说：",
	"1) 画面里有几个人？他们在做什么（看着屏幕/看手机/离开座位/别的）？",
	"2) 他们的状态（在不在忙、神情如何、离得近不近）？周围的光线、时间感（白天/夜里/开着灯）？",
	"3) 尽量具体。只说你真看到的，看不清就说看不清。",
	"4) 不要输出敏感信息：证件、单据、密码之类，遇到就跳过，只写\"这里有一处敏感信息，已跳过\"。",
].join("");
// 主模型不收图（如 DS 文本模型）时，屏幕截图交 grok / 本地视觉模型转述用的提问（2026-10-15：换 DS 后截图被 pi 整张丢掉，她等于瞎了）
const SCREEN_PROMPT = [
	"这是一张电脑屏幕截图。用中文说：",
	"1) 开着哪些窗口/程序，最前面那个是什么？",
	"2) 正在看或在做的内容：标题、正文大意、能读出的关键文字（照原文摘几句）。",
	"3) 只说你真看到的，看不清就说看不清。",
	"4) 不要输出敏感信息：密码、密钥、证件号、聊天里的私人号码之类，遇到就跳过，只写\"这里有一处敏感信息，已跳过\"。",
].join("");

const lastAt: Partial<Record<Target, number>> = {};
const looking = new Set<Target>(); // 正在取的那一路：并发的第二次如实说"正在看"（haiku 复核 r34-P2）
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

function jpegComplete(b: Buffer): boolean {
	let end = b.length;
	while (end > 2 && b[end - 1] === 0x00) end--; // 有的编码器会在末尾补零
	return b[end - 2] === 0xff && b[end - 1] === 0xd9;
}

/** 按文件头认图片格式（JPEG / PNG / WebP / GIF）；认不出返回空串。 */
function imageType(b: Buffer): string {
	if (b.length < 12) return "";
	// JPEG 还要有结尾标记 FFD9：头好尾断的截图交上去照样会让整轮 400（haiku 复核 r33-7）
	if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return jpegComplete(b) ? "image/jpeg" : "";
	if (b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
	if (b.subarray(0, 4).toString("latin1") === "RIFF" && b.subarray(8, 12).toString("latin1") === "WEBP") return "image/webp";
	if (b.subarray(0, 4).toString("latin1") === "GIF8") return "image/gif";
	return "";
}

/** 现读 agent/.env 里的 ALBION_EYES_*；.env 里有的以它为准，进程环境只补 .env 没写的键。
 *  2026-10-15：原来进程环境优先，可宿主启动时把整份 .env 灌进了环境 ⇒ 之后改 .env（换地址、换 token）
 *  她一直用开机那份旧值，「改了不用重启」并不成立。测试要换配置就指 PI_CODING_AGENT_DIR 到一份测试 .env。 */
function eyesConfig(): Record<string, string> {
	const out: Record<string, string> = {};
	const dir = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
	try {
		for (const raw of readFileSync(join(dir, ".env"), "utf-8").split("\n")) {
			const m = /^\s*(ALBION_EYES_[A-Z_]+)\s*=\s*(.*?)\s*$/.exec(raw.replace(/\r$/, ""));
			if (m) out[m[1]] = m[2].replace(/^(["'])(.*)\1$/, "$2");
		}
	} catch {
		/* 没有 .env 就只看进程环境 */
	}
	for (const [k, v] of Object.entries(process.env)) if (k.startsWith("ALBION_EYES_") && v && !(k in out)) out[k] = v;
	return out;
}

/** 多个采集端地址里挑第一个连得上的（TCP 3 秒）。照 MWB 的做法：同一局域网就直连，
 *  不走 Tailscale 中继（中继时一张整屏 35–60 秒，局域网 1–3 秒，2026-10-15 实测）。都没连上就用最后一个（Tailscale，哪儿都通；走中继时建连可能超过 3 秒）。 */
async function pickUrl(raw: string): Promise<string> {
	const urls = raw.split(",").map((u) => u.trim()).filter(Boolean);
	if (urls.length <= 1) return urls[0] || "";
	for (const u of urls) {
		let host = "";
		let port = 80;
		try {
			const p = new URL(u);
			host = p.hostname;
			port = Number(p.port) || (p.protocol === "https:" ? 443 : 80);
		} catch {
			continue;
		}
		const ok = await new Promise<boolean>((resolve) => {
			const sock = connect({ host, port, timeout: 3000 });
			const done = (v: boolean) => {
				sock.destroy();
				resolve(v);
			};
			sock.once("connect", () => done(true));
			sock.once("timeout", () => done(false));
			sock.once("error", () => done(false));
		});
		if (ok) return u;
	}
	return urls[urls.length - 1];
}

function source(target: Target, cfg: Record<string, string>): { url: string; token: string; path: string } {
	if (target === "his") return { url: cfg.ALBION_EYES_HIS_URL || "", token: cfg.ALBION_EYES_HIS_TOKEN || "", path: "/shot" };
	return { url: cfg.ALBION_EYES_MINE_URL || "", token: cfg.ALBION_EYES_MINE_TOKEN || "", path: target === "cam" ? "/cam" : "/shot" };
}

const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

/** 截图原图交号池的 grok-4.7 转述（同 look-image.ts 的取号方式；摄像头照片永远不走这里）。 */
async function describeByGrok(imageUrl: string): Promise<{ desc: string } | { error: string }> {
	const dir = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
	let base = "http://127.0.0.1:8000/v1";
	let key = "";
	try {
		const p = JSON.parse(readFileSync(join(dir, "models.json"), "utf-8"))?.providers?.grokpool || {};
		base = String(p.baseUrl || base).replace(/\/+$/, "");
		key = String(p.apiKey || "");
	} catch {
		/* 读不到就用默认地址，号池那边会报 401 */
	}
	try {
		const r = await fetch(`${base}/chat/completions`, {
			method: "POST",
			headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
			body: JSON.stringify({
				model: "grok-4.7",
				max_tokens: 900,
				messages: [{ role: "user", content: [{ type: "text", text: SCREEN_PROMPT }, { type: "image_url", image_url: { url: imageUrl } }] }],
			}),
			signal: AbortSignal.timeout(GROK_TIMEOUT_MS),
		});
		const body = await r.text();
		if (!r.ok) return { error: `号池返回 ${r.status}` };
		const desc = String(JSON.parse(body)?.choices?.[0]?.message?.content ?? "").trim();
		return desc ? { desc } : { error: "没给描述" };
	} catch (e) {
		return { error: String((e as Error)?.message || e).slice(0, 80) };
	}
}

export default function albionLook(pi: ExtensionAPI) {
	pi.registerTool({
		name: "look",
		label: "看一眼",
		description:
			"自己看一眼：his = 指挥官那台电脑的屏幕，mine = 我自己所在那台电脑的屏幕（我能看图时拿截图原图；看不了图时由 grok 看过原图写成文字给我），" +
			"cam = 这台电脑的摄像头·真实世界的他（照片只交给家里的本地视觉模型，我拿到的是它写下的文字描述）。" +
			"想看就调用，不用等谁提醒；同一路一分钟内只取一次。看到的照实说，描述里没写的不算看到。",
		promptSnippet: "look：自己看一眼（his/mine 屏幕，cam 摄像头·真实世界）",
		parameters: Type.Object({
			target: Type.Union([Type.Literal("his"), Type.Literal("mine"), Type.Literal("cam")], {
				description: "his = 指挥官那台屏幕；mine = 我所在那台屏幕；cam = 摄像头·真实世界",
			}),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const target = String(params.target || "") as Target;
			if (!(target in LABEL)) return text("target 只能是 his、mine 或 cam。");
			if (looking.has(target)) return text(`【${LABEL[target]}】另一次正在看这一路，等它的结果，别重复取。`);
			const since = Date.now() - (lastAt[target] ?? 0);
			if (since < SAME_TARGET_GAP_MS)
				return text(`【${LABEL[target]}】${Math.ceil(since / 1000)} 秒前刚看过，一分钟内不重复取；就着上次看到的说。`);
			const cfg = eyesConfig();
			const src = source(target, cfg);
			const { token, path } = src;
			const url = await pickUrl(src.url);
			if (!url || !token) return text(`【${LABEL[target]}】这一路还没配好（缺采集端地址或令牌），这次看不到。照实说没看到，别猜。`);
			// 先占住这一路的窗口（同一轮并行调两次也只取一次），没真看成就在 finally 里还回去（haiku 复核 r33-1/8）
			const prev = lastAt[target];
			lastAt[target] = Date.now();
			looking.add(target);
			let seen = false;
			try {
				const t0 = Date.now();
				let shot: Response;
				try {
					shot = await requestCapture(url, path, token, SHOT_TIMEOUT_MS);
				} catch (e) {
					return text(`【${LABEL[target]}】采集端连不上（${String((e as Error)?.message || e).slice(0, 120)}），这次没看到。`);
				}
				if (!shot.ok) return text(`【${LABEL[target]}】采集端返回 ${shot.status}，这次没看到。`);
				let bytes: Buffer;
				try {
					bytes = Buffer.from(await shot.arrayBuffer());
				} catch (e) {
					// 图还没传完就到了取图上限：这一步以前不在 try 里，超时会整个抛出去
					return text(`【${LABEL[target]}】图传到一半断了（${String((e as Error)?.message || e).slice(0, 120)}），这次没看到。`);
				}
				// 坏图交给主模型会让整轮 400（haiku 复核 I）：先认格式、看大小，认不出就不交
				const mimeType = imageType(bytes);
				if (!mimeType) return text(`【${LABEL[target]}】采集端给回来的不是能认的图片（${bytes.length} 字节），这次没看到。`);
				if (bytes.length > MAX_IMAGE_BYTES)
					return text(`【${LABEL[target]}】这张图太大（${Math.round(bytes.length / 1024)}KB），这次没交给眼睛。`);
				const data = bytes.toString("base64");
				const stamp = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });

				const mainSeesImages = ctx?.model?.input?.includes("image") ?? false;
				if (target !== "cam" && mainSeesImages) {
					seen = true;
					return {
						content: [
							{ type: "text" as const, text: `【${LABEL[target]}·截图原图，${stamp} 刚拍的】看得清的照实说，看不清就说看不清。` },
							{ type: "image" as const, data, mimeType },
						],
					};
				}

				// 屏幕截图、主模型不收图：原图交号池的 grok-4.7 转述（认字比本地 4B 准得多）；它不行再退到本地视觉模型
				let grokMiss = "";
				if (target !== "cam") {
					const g = await describeByGrok(`data:${mimeType};base64,${data}`);
					if ("desc" in g) {
						seen = true;
						return text(
							`【${LABEL[target]}·${stamp} 拍的，截图原图交 grok-4.7 看过后写下的描述（耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s）】\n${g.desc}\n` +
								"（描述里写到的才算看到；没写的别补。）",
						);
					}
					grokMiss = `grok 转述没成（${g.error}），改交本地视觉模型；`;
				}

				// 摄像头（永远只走这里）和 grok 转述失败的截图：交给本地视觉模型
				const what = target === "cam" ? "照片" : "截图";
				const vlm = (cfg.ALBION_EYES_VLM || "").replace(/\/+$/, "");
				if (!vlm) return text(`【${LABEL[target]}】本地视觉模型没配好，${what}不交给别处，这次看不到。`);
				try {
					const r = await fetch(`${vlm}/chat/completions`, {
						method: "POST",
						headers: { "content-type": "application/json" },
						body: JSON.stringify({
							model: VLM_MODEL,
							max_tokens: 500,
							temperature: 0.2,
							messages: [{ role: "user", content: [{ type: "text", text: target === "cam" ? CAM_PROMPT : SCREEN_PROMPT }, { type: "image_url", image_url: { url: `data:${mimeType};base64,${data}` } }] }],
						}),
						signal: AbortSignal.timeout(90_000),
					});
					const body = await r.text();
					if (!r.ok) return text(`【${LABEL[target]}】本地视觉模型返回 ${r.status}，这次没看清。`);
					let parsed: any;
					try {
						parsed = JSON.parse(body);
					} catch {
						return text(`【${LABEL[target]}】本地视觉模型回的不是能读的结果，这次没看清。`);
					}
					const desc = String(parsed?.choices?.[0]?.message?.content ?? "").trim();
					if (!desc) return text(`【${LABEL[target]}】本地视觉模型这次没给描述，没看清。`);
					seen = true;
					return text(
						`【${LABEL[target]}·${stamp} 拍的，${grokMiss}本地视觉模型看过后写下的描述（${what}没交给我，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s）】\n${desc}\n` +
							"（描述里写到的才算看到；没写的别补。）",
					);
				} catch (e) {
					return text(`【${LABEL[target]}】本地视觉模型连不上（${String((e as Error)?.message || e).slice(0, 120)}），这次没看清。`);
				}
			} finally {
				looking.delete(target);
				if (!seen) lastAt[target] = prev; // 没看成：不占 60 秒窗口
			}
		},
	});
}
