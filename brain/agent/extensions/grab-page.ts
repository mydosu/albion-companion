/**
 * grab-page —— 抓一个网页回来（搜索挂住时的备用路）
 *
 * 为什么有它：grok_web_search 走号池 Web 通道，在成人向内容、DLsite 这类站点上会挂住
 * （实测 90~320 秒没有任何响应，不是拒答，是卡死）。这条路直接 GET 页面，不经过模型。
 *
 * 只做一件事：标题 + 页面上成条的条目（标题 + 链接）；一条都没认出来时退一步给一段可读正文。
 * 抓不到就说抓不到，不编。
 *
 * 用法：pi -e /root/albion-pi/agent/extensions/grab-page.ts
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const UA =
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36";
const TIMEOUT_MS = 30_000;
const MAX_ENTRIES = 30;
const MAX_TEXT = 1500;

/** 什么算「条目」：详情页链接（商品/作品/文章/新闻），不是站内导航 */
const DETAIL_HREF = /product_id|\/work\/|\/item\/|\/items\/|\/post\/|\/article\/|\/news\/|\/products?\//i;

function absolute(href: string, base: string): string {
	try {
		return new URL(href, base).toString();
	} catch {
		return href;
	}
}

function stripTags(html: string): string {
	return html
		.replace(/<(script|style|noscript)[\s\S]*?<\/\1>/gi, " ")
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/gi, " ")
		.replace(/&amp;/gi, "&")
		.replace(/&lt;/gi, "<")
		.replace(/&gt;/gi, ">")
		.replace(/\s+/g, " ")
		.trim();
}

export async function grabWebPage(url: string): Promise<string> {
	if (!/^https?:\/\//i.test(url)) return `抓取失败：只收 http(s) 网址（给的是 ${url}）`;
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
	try {
		const response = await fetch(url, {
			headers: { "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7" },
			redirect: "follow",
			signal: controller.signal,
		});
		const html = await response.text();
		const title =
			(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] || "").replace(/\s+/g, " ").trim() ||
			"(没读到标题)";

		// 条目：带文字的详情页链接，去重、保序
		const entries: string[] = [];
		const seen = new Set<string>();
		const linkRe = /<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
		let match: RegExpExecArray | null;
		while ((match = linkRe.exec(html)) && entries.length < MAX_ENTRIES) {
			const rawHref = match[1];
			if (!DETAIL_HREF.test(rawHref)) continue;
			const text = stripTags(match[2]);
			if (text.length < 4 || text.length > 120) continue;
			const href = absolute(rawHref, url);
			if (seen.has(href)) continue;
			seen.add(href);
			entries.push(`- ${text}  ${href}`);
		}

		const head = `标题: ${title}\n网址: ${url}\nHTTP ${response.status} · ${html.length} 字节`;
		if (entries.length) return `${head}\n条目 ${entries.length} 条:\n${entries.join("\n")}`;
		const text = stripTags(html).slice(0, MAX_TEXT);
		return `${head}\n（没认出成条的条目，下面是正文开头）\n${text || "(正文是空的)"}`;
	} catch (error) {
		const why = error instanceof Error ? error.message : String(error);
		return `抓取失败: ${why} (URL: ${url})`;
	} finally {
		clearTimeout(timer);
	}
}

export default function albionGrabPage(pi: ExtensionAPI) {
	pi.registerTool({
		name: "grab_page",
		label: "抓页面",
		description:
			"直接抓一个网页：拿回标题 + 页面上成条的条目（标题 + 链接），认不出条目就给正文开头。" +
			"搜索（grok_web_search）在成人向内容 / 资源站上挂住时用这个。",
		promptSnippet: "grab_page：直接抓这个网址，拿回标题和条目",
		promptGuidelines: [
			"要的是「这页上有哪些作品/条目、链接是什么」，或者搜索那条挂住/超时了——用它。",
			"一次一个网址；拿回来的条目是材料，最后用自己的语气说给指挥官。",
			"没抓到就说没抓到，不要凭印象补内容。",
		],
		parameters: Type.Object({
			url: Type.String({ description: "要抓的完整网址（含 http/https）" }),
		}),
		async execute(_toolCallId, params) {
			const text = await grabWebPage(String(params.url || "").trim());
			return { content: [{ type: "text", text }] };
		},
	});
}
