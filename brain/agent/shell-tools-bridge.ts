/**
 * shell-tools-bridge —— 把壳里插件运行时注册的工具，变成 Pi 里她能直接调用的代理工具（2026-10-20）。
 *
 * 由 extensions/albion-shell-tools.ts 装上；放在 agent/ 而不是 extensions/，宿主不会把它当扩展单独加载。
 * 清单从宿主取（GET /albion/shell-tools），调用交宿主转给壳（POST /albion/shell-tools/call，阻塞到壳回结果）。
 * 协议与状态见 bin/shell_tools.py。
 *
 * 这里管的状态只有 owned：本扩展注册过的代理工具名 → 对应的壳工具名、定义签名、是否已撤下。
 * Pi 的工具不能注销，只能用同名 `exposure: "hidden"` 重新注册来撤下；插件回来了再按 direct 注册一次。
 *
 * 重名：她自己的工具（内置四件、别的扩展）永远优先，插件不能顶替 bash / read 之类。
 * 撞名的插件工具改叫 `shell_<原名>`，调用时仍按原名交给壳；改名后还撞就跳过这一件。
 */

import type { NormalizedBuildSystemPromptOptions } from "@earendil-works/pi-coding-agent";

export interface ShellTool {
	name: string;
	description: string;
	parameters: Record<string, unknown>;
}

export interface ShellPrompt {
	id: string;
	title: string;
	content: string;
}

export interface ShellToolset {
	version: number;
	tools: ShellTool[];
	prompts: ShellPrompt[];
}

/** 用到的 ExtensionAPI 子集；单测给假的。 */
export interface ToolHost {
	registerTool(tool: any): void;
	getAllTools(): Array<{ name: string }>;
}

export interface BridgeIo {
	fetchToolset(): Promise<ShellToolset>;
	invoke(name: string, args: unknown, signal?: AbortSignal): Promise<{ ok: boolean; text: string }>;
}

export interface SyncReport {
	version: number;
	exposed: Array<{ proxy: string; shell: string }>;
	hidden: string[];
	skipped: Array<{ name: string; reason: string }>;
	error?: string;
}

const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;
const RENAME_PREFIX = "shell_";
const EMPTY_PARAMS = { type: "object", properties: {} };
const PLUGIN_SECTION_PREFIX = "albion_shell_plugin_";

export class ShellToolBridge {
	private readonly owned = new Map<string, { shell: string; signature: string; hidden: boolean }>();
	private prompts: ShellPrompt[] = [];
	private readonly host: ToolHost;
	private readonly io: BridgeIo;

	// 不用参数属性：测试直接用 node 跑（只剥类型），那种写法要编译
	constructor(host: ToolHost, io: BridgeIo) {
		this.host = host;
		this.io = io;
	}

	/** 每轮开跑前调用。取不到清单（宿主旧版本、网络错）就当插件全没了：撤下全部代理工具，不留会失败的工具给她。 */
	async refresh(): Promise<SyncReport> {
		let toolset: ShellToolset;
		let error: string | undefined;
		try {
			toolset = await this.io.fetchToolset();
		} catch (e) {
			error = String((e as Error)?.message || e).slice(0, 200);
			toolset = { version: -1, tools: [], prompts: [] };
		}
		const report = this.sync(toolset);
		if (error) report.error = error;
		return report;
	}

	sync(toolset: ShellToolset): SyncReport {
		const report: SyncReport = { version: toolset.version, exposed: [], hidden: [], skipped: [] };
		// 「她自己的」= Pi 现在所有工具里不是本扩展注册的那些（含已撤下的代理名也不算她的）
		const native = new Set(this.host.getAllTools().map((t) => t.name).filter((n) => !this.owned.has(n)));
		const desired = new Map<string, ShellTool>();
		for (const tool of toolset.tools || []) {
			const name = String(tool?.name || "");
			if (!NAME_RE.test(name)) {
				report.skipped.push({ name, reason: "invalid tool name" });
				continue;
			}
			const params = tool.parameters ?? EMPTY_PARAMS;
			if (typeof params !== "object" || (params as any).type !== "object") {
				report.skipped.push({ name, reason: "parameters root is not an object schema" });
				continue;
			}
			const proxy = native.has(name) ? RENAME_PREFIX + name : name;
			if (!NAME_RE.test(proxy) || native.has(proxy) || desired.has(proxy)) {
				report.skipped.push({ name, reason: `name conflict (${proxy})` });
				continue;
			}
			desired.set(proxy, { name, description: String(tool.description || ""), parameters: params });
		}

		for (const [proxy, tool] of desired) {
			const signature = JSON.stringify([tool.name, tool.description, tool.parameters]);
			const prev = this.owned.get(proxy);
			report.exposed.push({ proxy, shell: tool.name });
			if (prev && !prev.hidden && prev.signature === signature) continue;
			this.host.registerTool(this.definition(proxy, tool));
			this.owned.set(proxy, { shell: tool.name, signature, hidden: false });
		}

		for (const [proxy, state] of this.owned) {
			if (desired.has(proxy) || state.hidden) continue;
			this.host.registerTool({
				name: proxy,
				label: `壳插件 ${state.shell}（已撤下）`,
				description: `壳插件工具 ${state.shell} 已经撤下。`,
				parameters: EMPTY_PARAMS,
				exposure: "hidden",
				async execute() {
					throw new Error(`壳插件工具 ${state.shell} 已经撤下，现在用不了。`);
				},
			});
			state.hidden = true;
			report.hidden.push(proxy);
		}
		// 提示词独立于工具描述更新，不能让一件插件的规则变成所有工具的用法。
		this.prompts = desired.size ? (toolset.prompts || []).filter((p) => p.content.trim()).map((p) => ({ ...p })) : [];
		return report;
	}

	/** 每轮刷新清单后更新独立区块；Pi 会把变更和撤销作为系统提示增量送给模型。 */
	applyPromptSections(options: NormalizedBuildSystemPromptOptions): void {
		for (const name of Object.keys(options.sections)) {
			if (name.startsWith(PLUGIN_SECTION_PREFIX)) delete options.sections[name];
		}
		for (const [index, prompt] of this.prompts.entries()) {
			// 壳的 id 含插件 owner；编码后可直接作为 Pi 的 XML section 名，更新时保持身份。
			const identity = Buffer.from(prompt.id || String(index)).toString("hex");
			options.sections[PLUGIN_SECTION_PREFIX + identity] =
				`壳插件规则 ${prompt.id}${prompt.title ? `（${prompt.title}）` : ""}\n${prompt.content.trim()}`;
		}

		delete options.sections.albion_tool_guidelines;
		// Pi 的 customPrompt 分支跳过 rules。只补当前可见工具与额外规则，不引入默认人格。
		// 无自定义 SYSTEM.md 时由 Pi 原生渲染，避免重复。
		if (!options.customPrompt) return;
		const hidden = new Set(options.hiddenTools);
		const rules: string[] = [];
		const seen = new Set<string>();
		const add = (rule: string) => {
			const text = rule.trim();
			if (!text || seen.has(text)) return;
			seen.add(text);
			rules.push(`- ${text}`);
		};
		for (const name of options.selectedTools) {
			if (hidden.has(name)) continue;
			for (const rule of options.toolGuidelines[name] || []) add(rule);
		}
		for (const rule of options.promptGuidelines) add(rule);
		if (rules.length) options.sections.albion_tool_guidelines = rules.join("\n");
	}

	private definition(proxy: string, tool: ShellTool) {
		const renamed = proxy !== tool.name;
		const io = this.io;
		return {
			name: proxy,
			label: `壳插件 ${tool.name}`,
			description:
				(renamed ? `（这是桌面壳插件的 ${tool.name}，和我自己的同名工具不是一件。）` : "") +
				(tool.description || `桌面壳插件提供的工具 ${tool.name}。`),
			parameters: tool.parameters,
			exposure: "direct",
			async execute(_toolCallId: string, params: unknown, signal?: AbortSignal) {
				const out = await io.invoke(tool.name, params, signal);
				// 失败要抛出来，Pi 才把它记成失败的工具结果；她看到原因自己决定怎么说
				if (!out.ok) throw new Error(out.text || `壳插件工具 ${tool.name} 调用失败。`);
				return { content: [{ type: "text" as const, text: out.text }], details: { shellTool: tool.name } };
			},
		};
	}
}

/** 宿主 HTTP 这一侧：同机 127.0.0.1，带宿主的 API_SERVER_KEY。 */
export function hostBridgeIo(opts: {
	base: string;
	session: string;
	key: string;
	fetchTimeoutMs: number;
	callTimeoutMs: number;
}): BridgeIo {
	const headers = (): Record<string, string> => ({
		"content-type": "application/json",
		...(opts.key ? { authorization: `Bearer ${opts.key}` } : {}),
	});
	return {
		async fetchToolset() {
			const url = `${opts.base}/albion/shell-tools?session=${encodeURIComponent(opts.session)}`;
			const res = await fetch(url, { headers: headers(), signal: AbortSignal.timeout(opts.fetchTimeoutMs) });
			if (!res.ok) throw new Error(`toolset HTTP ${res.status}`);
			const data = (await res.json()) as ShellToolset;
			return {
				version: Number(data?.version ?? 0),
				tools: Array.isArray(data?.tools) ? data.tools : [],
				prompts: Array.isArray(data?.prompts) ? data.prompts : [],
			};
		},
		async invoke(name, args, signal) {
			const timeout = AbortSignal.timeout(opts.callTimeoutMs);
			const res = await fetch(`${opts.base}/albion/shell-tools/call`, {
				method: "POST",
				headers: headers(),
				body: JSON.stringify({ session: opts.session, name, arguments: args ?? {} }),
				signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
			});
			if (!res.ok) return { ok: false, text: `壳工具桥 HTTP ${res.status}` };
			const data = (await res.json()) as { ok?: boolean; text?: string };
			return { ok: data?.ok === true, text: String(data?.text ?? "") };
		},
	};
}
