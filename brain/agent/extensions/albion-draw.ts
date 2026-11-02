/**
 * albion-draw —— 她自己画画（2026-10-14）。
 *
 * Hermes 时期她能生成图片；号池（grokpool）里有 grok-imagine-image 系列。
 * 画好的图存进她的画廊 /root/家/画廊/画的/，并把图本身交回给她自己看（看过才说画了什么）。
 * 一张约 30 秒；画不出来照实说。
 *
 * 2026-10-27 加「参照图」：本地图先传公开图床 catbox 换 https 直链（/images/edits 也收
 * data:image/...;base64 的入参，见 provider/web/attachments.go 的 loadChatImage）。
 * 同夜实测：两种入参都不报错、也都能出图，但出的图和输入图毫无关系——上游 issue #1012
 * 说这是账号被风控/降智，编辑请求会丢掉输入图、返回固定的那几张风景图。所以这条路
 * 眼下不通，问题不在参数也不在图床。
 * 注意：上传=那张图在公网上谁都看得到，私密的图别走这条路。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

const OUT_DIR = () => process.env.ALBION_DRAW_DIR || "/root/家/画廊/画的";
// 2.0 实测：同一句提示，构图和光影明显比 grok-imagine-image 好（灯的结构对、不糊），慢约 7 秒（26s vs 19s）
const MODEL = process.env.ALBION_DRAW_MODEL || "grok-imagine-image-2.0";
const EDIT_MODEL = process.env.ALBION_DRAW_EDIT_MODEL || "grok-imagine-image-edit";
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

function pool(): { base: string; key: string } {
	const dir = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
	const cfg = JSON.parse(readFileSync(join(dir, "models.json"), "utf-8"));
	const p = cfg?.providers?.grokpool || {};
	return { base: String(p.baseUrl || "http://127.0.0.1:8000/v1").replace(/\/+$/, ""), key: String(p.apiKey || "") };
}

/** 按文件头认图片类型（文件名靠不住：实测有 .png 其实是 jpeg） */
function sniffMime(buf: Buffer): string {
	const h = buf.slice(0, 4).toString("hex");
	if (h.startsWith("ffd8")) return "image/jpeg";
	if (h.startsWith("8950")) return "image/png";
	if (h.startsWith("4749")) return "image/gif";
	if (h.startsWith("5249")) return "image/webp";
	return "image/jpeg";
}

/**
 * 把本地图传到公开图床，换一个 https 直链（catbox 永久公开，tmpfiles 只有 24 小时）。
 * 走 curl 子进程而不是 node 的 fetch：这台机器上 node fetch 不走代理、连 catbox 直接超时（2026-10-27 实测），curl 能通。
 */
async function toPublicUrl(path: string, mime: string): Promise<string> {
	const typeArg = `;type=${mime}`;
	try {
		const { stdout } = await run("curl", ["-s", "--max-time", "120", "-F", "reqtype=fileupload", "-F", `fileToUpload=@${path}${typeArg}`, "https://catbox.moe/user/api.php"], { maxBuffer: 4 * 1024 * 1024 });
		const t = stdout.trim();
		if (/^https:\/\//.test(t)) return t;
	} catch {
		/* 换下一个图床 */
	}
	try {
		const { stdout } = await run("curl", ["-s", "--max-time", "120", "-F", `file=@${path}${typeArg}`, "https://tmpfiles.org/api/v1/upload"], { maxBuffer: 4 * 1024 * 1024 });
		const j = JSON.parse(stdout) as { data?: { url?: string } };
		const m = String(j?.data?.url || "").match(/^https?:\/\/tmpfiles\.org\/(.+)$/);
		if (m) return `https://tmpfiles.org/dl/${m[1]}`;
	} catch {
		/* 也是没成 */
	}
	throw new Error("两个图床都没换成 https 直链（catbox / tmpfiles）");
}

export type DrawArgs = { prompt: string; reference?: string };

/** 画一张图，返回存档路径（给工具用，也方便单独跑一次自测） */
export async function drawImage({ prompt, reference }: DrawArgs): Promise<{ path: string; bytes: Buffer; mimeType: string; usedReference: boolean }> {
	const { base, key } = pool();
	const auth = { authorization: "Bearer " + key };

	let url = `${base}/images/generations`;
	let body: Record<string, unknown> = { model: MODEL, prompt, n: 1 };
	let usedReference = false;

	if (reference) {
		let refUrl: string;
		if (/^https:\/\//.test(reference)) refUrl = reference;
		else {
			const p = reference.replace(/^~/, "/root");
			if (!existsSync(p)) throw new Error(`找不到参照图：${p}`);
			refUrl = await toPublicUrl(p, sniffMime(readFileSync(p)));
		}
		url = `${base}/images/edits`;
		body = { model: EDIT_MODEL, prompt, n: 1, image: { url: refUrl } };
		usedReference = true;
	}

	const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json", ...auth }, body: JSON.stringify(body), signal: AbortSignal.timeout(180_000) });
	if (!r.ok) throw new Error(`号池返回 ${r.status}：${(await r.text()).slice(0, 200)}`);
	const item = ((await r.json())?.data?.[0] ?? {}) as { b64_json?: string; url?: string; mime_type?: string };
	let bytes: Buffer;
	if (item.b64_json) bytes = Buffer.from(String(item.b64_json), "base64");
	else if (item.url) {
		const img = await fetch(String(item.url), { headers: auth, signal: AbortSignal.timeout(60_000) });
		if (!img.ok) throw new Error(`图画好了但取不回来（${img.status}）`);
		bytes = Buffer.from(await img.arrayBuffer());
	} else throw new Error("号池这次没给图");

	const mimeType = String(item.mime_type || "image/jpeg");
	const ext = mimeType.includes("png") ? "png" : "jpg";
	if (!existsSync(OUT_DIR())) mkdirSync(OUT_DIR(), { recursive: true });
	const stamp = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" }).replace(/[: ]/g, "").replace(/-/g, "");
	const path = join(OUT_DIR(), `${stamp}.${ext}`);
	writeFileSync(path, bytes);
	return { path, bytes, mimeType, usedReference };
}

export default function albionDraw(pi: ExtensionAPI) {
	pi.registerTool({
		name: "draw",
		label: "画画",
		description:
			"自己画一张图：prompt 写想画什么（中文或英文都行，写清主体、场景、风格）。约 30 秒；画好存进 /root/家/画廊/画的/，并把图交给我看。" +
			"想照着某张图改（图生图），把那张图的本地路径写进 reference——注意它会被传到公开图床上换 https 直链，所以私密的图先别用这条路。" +
			"看过图再说画了什么；画不出来就照实说。",
		promptSnippet: "draw：自己画一张图（存进画廊）；给 reference 就照着那张图改",
		parameters: Type.Object({
			prompt: Type.String({ description: "想画什么" }),
			reference: Type.Optional(Type.String({ description: "参照图的本地路径（可选）；给了就照着它改画" })),
		}),
		async execute(_toolCallId, params) {
			const prompt = String(params.prompt ?? "").trim().slice(0, 1000);
			if (!prompt) return text("要写想画什么（prompt）。");
			const reference = params.reference ? String(params.reference) : undefined;
			try {
				const { path, bytes, mimeType, usedReference } = await drawImage({ prompt, reference });
				const kb = Math.round(bytes.length / 1024);
				const head = usedReference ? `【画好了（照着参照图改的）】` : `【画好了】`;
				return {
					content: [
						{ type: "text" as const, text: `${head}存在 ${path}（${kb}KB）。下面就是这张图，看过再说画了什么。` },
						{ type: "image" as const, data: bytes.toString("base64"), mimeType },
					],
				};
			} catch (e) {
				return text(`画画失败：${String((e as Error)?.message || e).slice(0, 200)}`);
			}
		},
	});
}
