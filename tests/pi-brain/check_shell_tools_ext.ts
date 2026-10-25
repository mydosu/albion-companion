/**
 * albion-shell-tools 的代理工具同步（agent/shell-tools-bridge.ts）离线检查：假的 Pi 工具表 + 假的宿主 IO，不起 Pi、不联网。
 *
 * 覆盖：插件工具注册成代理、清单不变不重注册、定义变了重注册、插件注销改成 hidden、回来再 direct；
 * 与她自己的工具重名改叫 shell_<名>、改名还撞跳过；坏 schema 只跳过那一件；取不到清单撤下全部；
 * 调用成功交回文字、失败抛错（Pi 记成失败的工具结果）。
 *
 * 跑：node tests/pi-brain/check_shell_tools_ext.ts（Windows / WSL 都行，Node 22.6+ 直接跑 .ts）
 */
import assert from "node:assert/strict";
import type { NormalizedBuildSystemPromptOptions } from "@earendil-works/pi-coding-agent";
import { ShellToolBridge, type ShellToolset } from "../../integrations/pi-brain/agent/shell-tools-bridge.ts";

const PARAMS = { type: "object", properties: { move: { type: "string" } } };

class FakePi {
	tools = new Map<string, any>();
	registrations: any[] = [];
	constructor(native: string[]) {
		for (const name of native) this.tools.set(name, { name, exposure: "direct" });
	}
	registerTool(tool: any) {
		this.registrations.push(tool);
		this.tools.set(tool.name, tool);
	}
	getAllTools() {
		return [...this.tools.values()].map((t) => ({ name: t.name }));
	}
	exposure(name: string) {
		return this.tools.get(name)?.exposure;
	}
}

let toolset: ShellToolset = { version: 1, tools: [], prompts: [] };
let fetchFails = false;
const invoked: Array<{ name: string; args: unknown }> = [];
const io = {
	async fetchToolset() {
		if (fetchFails) throw new Error("toolset HTTP 404");
		return toolset;
	},
	async invoke(name: string, args: unknown) {
		invoked.push({ name, args });
		return name === "broken_tool" ? { ok: false, text: "Error: plugin crashed" } : { ok: true, text: `done:${name}` };
	},
};

const pi = new FakePi(["bash", "read", "web_search", "shell_read"]);
const bridge = new ShellToolBridge(pi, io);

// 1. 注册：一件正常、一件与她自己的 web_search 重名、一件改名后仍撞（shell_read 已是她的）、一件坏 schema
toolset = {
	version: 1,
	tools: [
		{ name: "play_chess", description: "下一步棋", parameters: PARAMS },
		{ name: "web_search", description: "插件的搜索", parameters: PARAMS },
		{ name: "read", description: "插件的读", parameters: PARAMS },
		{ name: "bad_schema", description: "坏的", parameters: { type: "string" } as any },
		{ name: "broken_tool", description: "会出错", parameters: PARAMS },
	],
	prompts: [{ id: "chess", title: "国际象棋", content: "新开局不要传 fen。" }],
};
let report = await bridge.refresh();
assert.deepEqual(report.exposed, [
	{ proxy: "play_chess", shell: "play_chess" },
	{ proxy: "shell_web_search", shell: "web_search" },
	{ proxy: "broken_tool", shell: "broken_tool" },
]);
assert.deepEqual(report.skipped.map((s) => s.name), ["read", "bad_schema"]);
assert.match(report.skipped[0].reason, /name conflict \(shell_read\)/);
assert.equal(pi.tools.get("web_search").exposure, "direct", "她自己的 web_search 不能被插件顶替");
assert.equal(pi.tools.get("web_search").description, undefined);
assert.equal(pi.exposure("shell_web_search"), "direct");
assert.match(pi.tools.get("shell_web_search").description, /桌面壳插件的 web_search/);
assert.equal(pi.tools.get("play_chess").description, "下一步棋", "插件规则不能混入每件工具描述");
assert.equal(pi.registrations.length, 3);

// ROOT CAUSE: 所有插件规则曾拼进每件工具描述，且总共截在 2000 字；自定义 SYSTEM.md 又跳过原生规则。
const options: NormalizedBuildSystemPromptOptions = {
	customPrompt: "测试人格，不得替换。", cwd: ".", selectedTools: ["bash", "hidden_tool"], hiddenTools: ["hidden_tool"],
	toolSnippets: {}, toolGuidelines: { bash: ["保留工具规则。", "  保留工具规则。  ", ""], hidden_tool: ["隐藏规则不得出现。"], inactive: ["未选规则不得出现。"] },
	promptGuidelines: ["保留额外规则。"], appendSystemPrompt: "", sections: { existing: "原有区块" }, contextFiles: [], skills: [],
};
bridge.applyPromptSections(options);
assert.equal(options.customPrompt, "测试人格，不得替换。");
assert.equal(options.sections.existing, "原有区块");
assert.equal(options.sections.albion_tool_guidelines, "- 保留工具规则。\n- 保留额外规则。");
assert.equal(Object.keys(options.sections).filter((k) => k.startsWith("albion_shell_plugin_")).length, 1);
assert.match(Object.values(options.sections).join("\n"), /国际象棋.*\n新开局不要传 fen/);

// 两插件各有区块，后一个不受前一个长度影响；纯提示更新不重注册工具。
const originalPrompts = toolset.prompts;
toolset = { ...toolset, prompts: [
	{ id: "plugin:a/rules", title: "A", content: "A".repeat(2100) + "A规则末尾" },
	{ id: "plugin:b/rules", title: "B", content: "B规则。" },
] };
await bridge.refresh();
bridge.applyPromptSections(options);
const sections = Object.entries(options.sections).filter(([k]) => k.startsWith("albion_shell_plugin_"));
assert.equal(sections.length, 2);
assert.match(sections[0][1], /A规则末尾$/);
assert.doesNotMatch(sections[0][1], /B规则/);
assert.match(sections[1][1], /B规则。$/);
assert.doesNotMatch(sections[1][1], /A规则/);
assert.equal(pi.registrations.length, 3);
toolset = { ...toolset, prompts: originalPrompts };
await bridge.refresh();

// 2. 调用：代理名调用、按壳的原名交给宿主；失败要抛
const ok = await pi.tools.get("shell_web_search").execute("t1", { move: "x" });
assert.deepEqual(ok.content, [{ type: "text", text: "done:web_search" }]);
assert.deepEqual(invoked.at(-1), { name: "web_search", args: { move: "x" } });
await assert.rejects(() => pi.tools.get("broken_tool").execute("t2", {}), /plugin crashed/);

// 3. 清单不变：不重注册
report = await bridge.refresh();
assert.equal(pi.registrations.length, 3);
assert.equal(report.hidden.length, 0);

// 4. 定义变了 + 一个插件注销：变了的重注册，注销的改 hidden（Pi 工具不能注销）
toolset = { version: 2, tools: [{ name: "play_chess", description: "下一步棋（新版）", parameters: PARAMS }], prompts: [] };
report = await bridge.refresh();
assert.deepEqual(report.hidden.sort(), ["broken_tool", "shell_web_search"]);
assert.equal(pi.tools.get("play_chess").description, "下一步棋（新版）", "提示词随插件一起撤掉");
assert.equal(pi.exposure("shell_web_search"), "hidden");
await assert.rejects(() => pi.tools.get("shell_web_search").execute("t3", {}), /已经撤下/);
assert.equal(pi.registrations.length, 6);
bridge.applyPromptSections(options);
assert.equal(Object.keys(options.sections).some((k) => k.startsWith("albion_shell_plugin_")), false);

// 5. 插件回来了：hidden 的按 direct 重新注册，名字不变
toolset = { version: 3, tools: [...toolset.tools, { name: "web_search", description: "插件的搜索", parameters: PARAMS }], prompts: originalPrompts };
report = await bridge.refresh();
assert.equal(pi.exposure("shell_web_search"), "direct");
assert.deepEqual(report.exposed.map((e) => e.proxy), ["play_chess", "shell_web_search"]);
options.customPrompt = undefined;
bridge.applyPromptSections(options);
assert.equal(options.sections.albion_tool_guidelines, undefined, "默认提示不重复添加工具规则");
assert.equal(Object.keys(options.sections).filter((k) => k.startsWith("albion_shell_plugin_")).length, 1, "默认提示也独立注入插件规则");
options.customPrompt = "测试人格";

// 6. 取不到清单（宿主旧版本 / 宿主挂了）：撤下全部，不给她留会失败的工具
fetchFails = true;
report = await bridge.refresh();
assert.equal(report.error, "toolset HTTP 404");
assert.equal(pi.exposure("play_chess"), "hidden");
assert.equal(pi.exposure("shell_web_search"), "hidden");
assert.equal(pi.exposure("bash"), "direct", "她自己的工具不受影响");
bridge.applyPromptSections(options);
assert.equal(Object.keys(options.sections).some((k) => k.startsWith("albion_shell_plugin_")), false);
options.customPrompt = undefined;
bridge.applyPromptSections(options);
assert.equal(options.sections.albion_tool_guidelines, undefined, "原生默认提示负责渲染规则，不重复添加");
options.customPrompt = "测试人格";
options.selectedTools = [];
options.promptGuidelines = [];
bridge.applyPromptSections(options);
assert.equal(options.sections.albion_tool_guidelines, undefined, "撤下规则不留下旧内容");

console.log("albion-shell-tools proxy sync: register / unchanged / update / hide / restore / conflict / fail-closed");
console.log("SHELL_TOOLS_EXT SELFTEST OK");
