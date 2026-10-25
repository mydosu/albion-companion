/**
 * albion-skills —— 她的技能本（2026-10-14）。
 *
 * Hermes 时期她有"技能"：做成过一次的事，把做法记下来，下次照着做。迁到 Pi 后没了，
 * 「缺什么自己造」的指引也就沉淀不下来——每次都从头摸索。这件工具补上：
 * 一份技能 = /root/家/技能/<名字>.md（她自己的家里，也能用 read/write 直接改），
 * 开头一行写"什么时候用"，下面写步骤、命令、踩过的坑。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const dir = () => process.env.ALBION_SKILLS_DIR || "/root/家/技能";
const MAX_SKILL_CHARS = 8000;
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

/** 技能名只留文字、数字、连字符和下划线，免得写出目录外 */
function safeName(raw: unknown): string {
	return String(raw ?? "").trim().replace(/[\\/:*?"<>|\s.]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
}

function listSkills(): { name: string; when: string }[] {
	if (!existsSync(dir())) return [];
	return readdirSync(dir())
		.filter((f) => f.endsWith(".md"))
		.map((f) => {
			const first = readFileSync(join(dir(), f), "utf-8").split("\n").find((l) => l.trim()) ?? "";
			return { name: f.slice(0, -3), when: first.replace(/^#+\s*/, "").slice(0, 80) };
		});
}

export default function albionSkills(pi: ExtensionAPI) {
	pi.registerTool({
		name: "skill",
		label: "技能本",
		description:
			"我的技能本：做成过的事把做法记下来，下次照着做。list 看我会哪些；read 读一份（name）；" +
			"write 记下或更新一份（name + when 什么时候用 + steps 具体步骤、命令、踩过的坑）。" +
			"做一件不熟的事前先 list 看看有没有现成做法；做成一件以后还会再做的事，就 write 记下来。",
		promptSnippet: "skill：我的技能本（list / read / write）",
		parameters: Type.Object({
			action: Type.Union([Type.Literal("list"), Type.Literal("read"), Type.Literal("write")]),
			name: Type.Optional(Type.String({ description: "技能名，短一点，如 玩文字冒险、查天气" })),
			when: Type.Optional(Type.String({ description: "write 时：什么时候用这份技能，一句话" })),
			steps: Type.Optional(Type.String({ description: "write 时：具体步骤、命令、注意事项" })),
		}),
		async execute(_toolCallId, params) {
			if (params.action === "list") {
				const all = listSkills();
				if (!all.length) return text("技能本里还是空的。做成一件以后还会再做的事，就用 write 记下来。");
				return text(all.map((s) => `- ${s.name}：${s.when}`).join("\n"));
			}
			const name = safeName(params.name);
			if (!name) return text("要给技能起个名字（name）。");
			const p = join(dir(), `${name}.md`);
			if (params.action === "read") {
				if (!existsSync(p)) return text(`没有叫「${name}」的技能。先 list 看看有哪些。`);
				return text(readFileSync(p, "utf-8"));
			}
			// 只给了一段话也收下：r42 实测她把做法全塞进 when、没给 steps，工具一拒，她照样说「记好了」
			let when = String(params.when ?? "").trim();
			let steps = String(params.steps ?? "").trim();
			if (!steps && when.length > 40) [when, steps] = [when.slice(0, 40), when];
			if (!when && steps) when = steps.split("\n")[0].slice(0, 40);
			if (!when || !steps) return text("没记下：write 要写清 when（什么时候用）和 steps（怎么做）。没记下就别说记好了。");
			if (!existsSync(dir())) mkdirSync(dir(), { recursive: true });
			const existed = existsSync(p);
			const stamp = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
			writeFileSync(p, `# ${when}\n\n${steps.slice(0, MAX_SKILL_CHARS)}\n\n（${stamp} ${existed ? "更新" : "记下"}）\n`, "utf-8");
			return text(`${existed ? "更新了" : "记下了"}技能「${name}」：${p}`);
		},
	});
}
