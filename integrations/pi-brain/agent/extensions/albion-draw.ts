/**
 * albion-draw —— 她自己画画（2026-10-14）。
 *
 * Hermes 时期她能生成图片；号池（grokpool）里有 grok-imagine-image 系列。
 * 画好的图存进她的画廊 /root/家/画廊/画的/，并把图本身交回给她自己看（看过才说画了什么）。
 * 一张约 30 秒；画不出来照实说。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const OUT_DIR = () => process.env.ALBION_DRAW_DIR || "/root/家/画廊/画的";
// 2.0 实测：同一句提示，构图和光影明显比 grok-imagine-image 好（灯的结构对、不糊），慢约 7 秒（26s vs 19s）
const MODEL = process.env.ALBION_DRAW_MODEL || "grok-imagine-image-2.0";
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

function pool(): { base: string; key: string } {
	const dir = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
	const cfg = JSON.parse(readFileSync(join(dir, "models.json"), "utf-8"));
	const p = cfg?.providers?.grokpool || {};
	return { base: String(p.baseUrl || "http://127.0.0.1:8000/v1").replace(/\/+$/, ""), key: String(p.apiKey || "") };
}

export default function albionDraw(pi: ExtensionAPI) {
	pi.registerTool({
		name: "draw",
		label: "画画",
		description:
			"自己画一张图：prompt 写想画什么（中文或英文都行，写清主体、场景、风格）。约 30 秒；画好存进 /root/家/画廊/画的/，并把图交给我看。" +
			"看过图再说画了什么；画不出来就照实说。",
		promptSnippet: "draw：自己画一张图（存进画廊）",
		parameters: Type.Object({
			prompt: Type.String({ description: "想画什么" }),
		}),
		async execute(_toolCallId, params) {
			const prompt = String(params.prompt ?? "").trim().slice(0, 1000);
			if (!prompt) return text("要写想画什么（prompt）。");
			let base: string, key: string;
			try {
				({ base, key } = pool());
			} catch {
				return text("找不到画画用的号池配置，这次画不了。");
			}
			const auth = { authorization: "Bearer " + key };
			try {
				const r = await fetch(`${base}/images/generations`, {
					method: "POST",
					headers: { "content-type": "application/json", ...auth },
					body: JSON.stringify({ model: MODEL, prompt, n: 1 }),
					signal: AbortSignal.timeout(180_000),
				});
				if (!r.ok) return text(`画画失败：号池返回 ${r.status}。`);
				const item = (await r.json())?.data?.[0] ?? {};
				let bytes: Buffer;
				if (item.b64_json) bytes = Buffer.from(String(item.b64_json), "base64");
				else if (item.url) {
					const img = await fetch(String(item.url), { headers: auth, signal: AbortSignal.timeout(60_000) });
					if (!img.ok) return text(`图画好了但取不回来（${img.status}）。`);
					bytes = Buffer.from(await img.arrayBuffer());
				} else return text("号池这次没给图。");
				const mimeType = String(item.mime_type || "image/jpeg");
				const ext = mimeType.includes("png") ? "png" : "jpg";
				if (!existsSync(OUT_DIR())) mkdirSync(OUT_DIR(), { recursive: true });
				const stamp = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" }).replace(/[: ]/g, "").replace(/-/g, "");
				const path = join(OUT_DIR(), `${stamp}.${ext}`);
				writeFileSync(path, bytes);
				return {
					content: [
						{ type: "text" as const, text: `【画好了】存在 ${path}（${Math.round(bytes.length / 1024)}KB）。下面就是这张图，看过再说画了什么。` },
						{ type: "image" as const, data: bytes.toString("base64"), mimeType },
					],
				};
			} catch (e) {
				return text(`画画失败：${String((e as Error)?.message || e).slice(0, 160)}`);
			}
		},
	});
}
