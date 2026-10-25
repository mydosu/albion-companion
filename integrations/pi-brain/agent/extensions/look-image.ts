/**
 * look-image —— 原图直传视觉模型（本地一个像素都不碰）
 *
 * 把**原图**（原字节、不缩不转）POST 给号池里那个多模态模型，把它的描述当工具结果交回来。
 * 只回文字，所以看一批大图也不会把整张图塞进之后每一轮的上下文。
 * 实测（2026-10-11）：9.5MB 的 PNG 20–30s 出准确描述。
 * 2026-10-13：`agent/settings.json` 关了 `images.autoResize`，`read` 现在也交原字节；
 * 原来写的"read 在本机重编码、≥1024px 永不返回"，本机这一段复核不成立（Pi 的缩图 9.6MB/3721px 只要 2s，
 * 2000px 以内本来就不动）；若整轮里 read 大图仍卡，卡在上游，不是本机转码能解的——不再让她用 ffmpeg 缩图。
 *
 * 记住：拿回来的是"看图工具转述的画面"，不是我自己长眼睛；可以照它说画面里有什么，
 * 但不能把转述没提到的部分补出来。
 * 用法：pi -e /root/albion-pi/agent/extensions/look-image.ts
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync, statSync } from "node:fs";
import { basename, extname, join } from "node:path";

const MIME: Record<string, string> = {
	".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
	".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
};
const ASK = "请客观、具体地描述这张画面：主体（人物或物件）、姿态与动作、衣着、场景与背景、光线与色调；画面里若有文字照抄下来。只说你真看到的，不评价、不猜测、不省略。";

function pool(): { base: string; key: string; model: string } {
	const dir = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
	try {
		const cfg = JSON.parse(readFileSync(join(dir, "models.json"), "utf-8"));
		const p = cfg?.providers?.grokpool || {};
		return {
			base: String(p.baseUrl || "http://127.0.0.1:8000/v1").replace(/\/+$/, ""),
			key: String(p.apiKey || ""),
			model: "grok-4.7",
		};
	} catch {
		return { base: "http://127.0.0.1:8000/v1", key: "", model: "grok-4.7" };
	}
}

export default function albionLookImage(pi: ExtensionAPI) {
	pi.registerTool({
		name: "look_image",
		label: "看图",
		description:
			"看一张图片文件：把**原图**原字节交给视觉模型，拿回它的文字描述（本机不缩放、不转码）。看家里/画廊的图片优先用这个；不要自己用 ffmpeg 之类先缩图。",
		promptSnippet: "look_image：看这张图（原图直传视觉模型）",
		parameters: Type.Object({
			path: Type.String({ description: "图片文件的绝对路径" }),
			model: Type.Optional(Type.String({ description: "换一个视觉模型（默认号池的 grok-4.7）" })),
		}),
		async execute(_toolCallId, params) {
			const src = String(params.path || "").trim();
			if (!src)
				return { content: [{ type: "text", text: "没给路径。" }] };
			let sizeKB = 0;
			try {
				sizeKB = Math.round(statSync(src).size / 1024);
			} catch {
				return { content: [{ type: "text", text: `读不到这个文件：${src}` }] };
			}
			const mime = MIME[extname(src).toLowerCase()] || "image/png";
			const data = readFileSync(src).toString("base64");
			const { base, key, model } = pool();
			const body = {
				model: String(params.model || model),
				max_tokens: 700,
				messages: [{
					role: "user",
					content: [
						{ type: "text", text: ASK },
						{ type: "image_url", image_url: { url: `data:${mime};base64,${data}` } },
					],
				}],
			};
			const t0 = Date.now();
			try {
				const r = await fetch(base + "/chat/completions", {
					method: "POST",
					headers: { "content-type": "application/json", "authorization": "Bearer " + key },
					body: JSON.stringify(body),
					signal: AbortSignal.timeout(300_000),
				});
				const txt = await r.text();
				if (!r.ok)
					return { content: [{ type: "text", text: `看图失败（${basename(src)}）：视觉模型返回 ${r.status} ${txt.slice(0, 200)}` }] };
				const desc = JSON.parse(txt)?.choices?.[0]?.message?.content || "";
				if (!String(desc).trim())
					return { content: [{ type: "text", text: `视觉模型这次没给画面描述（${basename(src)}）。` }] };
				return {
					content: [{
						type: "text",
						text: `【看图工具·转述（原图 ${sizeKB}KB 原字节直传 ${body.model}，本机没做任何处理，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s）】\n${desc}`,
					}],
				};
			} catch (e) {
				return { content: [{ type: "text", text: `看图失败（${basename(src)}）：${String((e as Error)?.message || e).slice(0, 200)}` }] };
			}
		},
	});
}
