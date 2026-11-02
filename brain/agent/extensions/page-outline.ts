import { Type } from "typebox";

/**
 * page-outline —— 从页面提取所有 h1/h2/h3 标题
 *
 * 用法：pi -e /root/albion-pi/agent/extensions/page-outline.ts <网址>
 *
 * 输入：一个网址，返回该页面上所有出现的 <h1> <h2> <h3> 标题文本。
 * 抓不到就说抓不到，不编。
 */

export async function getPageOutline(url: string): Promise<string> {
	if (!/^https?:\/\//i.test(url)) return `抓取失败：只收 http(s) 网址（给的是 ${url}）`;
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), 30000);
	try {
		const response = await fetch(url, {
			headers: {
				"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
			},
			redirect: "follow",
			signal: controller.signal,
		});
		const html = await response.text();
		if (response.status !== 200) return `抓取失败：HTTP ${response.status} (URL: ${url})`;
		const title = (html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] || "").replace(/\s+/g, " ").trim() || "(没读到标题)";
		const outline: string[] = [];
		// 提取 h1
		const h1Re = /<h1[^>]*>([\s\S]*?)<\/h1>/gi;
			let m: RegExpExecArray | null;
			let h1Count = 0;
			while ((m = h1Re.exec(html)) && h1Count < 20) {
				const text = m[1].replace(/\s+/g, " ").trim();
				if (text) outline.push(`# ${text}`);
				h1Count++;
			}
			// 提取 h2
			const h2Re = /<h2[^>]*>([\s\S]*?)<\/h2>/gi;
			let h2Count = 0;
			while ((m = h2Re.exec(html)) && h2Count < 20) {
				const text = m[1].replace(/\s+/g, " ").trim();
				if (text) outline.push(`## ${text}`);
				h2Count++;
			}
			// 提取 h3
			const h3Re = /<h3[^>]*>([\s\S]*?)<\/h3>/gi;
			let h3Count = 0;
			while ((m = h3Re.exec(html)) && h3Count < 20) {
				const text = m[1].replace(/\s+/g, " ").trim();
				if (text) outline.push(`### ${text}`);
				h3Count++;
			}
		const head = `标题: ${title}\n网址: ${url}\nHTTP ${response.status} · ${html.length} 字节`;
		if (outline.length) return `${head}\n大纲 ${outline.length} 条:\n${outline.join("\n")}`;
		const text = html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 1500);
		return `${head}\n（没认出标题，下面是正文开头）\n${text || "(正文是空的)"}`;
	} catch (error) {
		const why = error instanceof Error ? error.message : String(error);
		return `抓取失败: ${why} (URL: ${url})`;
	} finally {
		clearTimeout(timer);
	}
}

export default function albionOutline(pi: any) {
	pi.registerTool({
		name: "page_outline",
		label: "页面大纲",
		description: "给一个网址，返回页面上所有 h1/h2/h3 标题。",
		promptSnippet: "page_outline：抓这个网址的所有标题",
		parameters: Type.Object({
			url: Type.String({ description: "要抓的完整网址（含 http/https）" }),
		}),
		async execute(_toolCallId, params) {
			const text = await getPageOutline(String(params.url || "").trim());
			return { content: [{ type: "text", text }] };
		},
	});
}
