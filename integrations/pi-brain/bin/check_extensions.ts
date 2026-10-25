/**
 * check_extensions —— 不叫模型、不起会话，用 Pi 自己的加载器把 agent/extensions/*.ts 整批加载一遍。
 *
 * 宿主换身子时是整批加载：一个扩展写坏（漏 import、相对路径错、语法错），整批都起不来。
 * 只用 `node -e "import(...)"` 查不出两件事：Pi 的 jiti 加载结果、工具 parameters 的形状
 * （普通对象本地照样能 import，上游却回 400 invalid_client_tool_schema）。这里两件都查。
 *
 * 跑：node /root/albion-pi/bin/check_extensions.ts [扩展文件…]   （不给参数 = 查整个目录）
 * 退出码 0 = 全部能加载、工具形状都对；1 = 有问题，逐条列出。
 */
import { readdirSync } from "node:fs";
import { join, resolve } from "node:path";

const PI = "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";
const EXT_DIR = "/root/albion-pi/agent/extensions";

const args = process.argv.slice(2);
const files = args.length
	? args.map((p) => resolve(p))
	: readdirSync(EXT_DIR).filter((f) => f.endsWith(".ts")).sort().map((f) => join(EXT_DIR, f));

// 加载扩展会让 loopguard 记一行 loaded；自检不往生产日志里写
process.env.ALBION_LOOPGUARD_LOG ??= "/tmp/albion-check-extensions-loopguard.log";
const { loadExtensions } = await import(PI);
const result = await loadExtensions(files, "/root");
const problems: string[] = [];

for (const e of result.errors ?? []) problems.push(`加载失败  ${e.path}\n          ${String(e.error).split("\n")[0]}`);

const tools: string[] = [];
for (const ext of result.extensions ?? []) {
	for (const [name, t] of ext.tools ?? new Map()) {
		const params = (t as any)?.definition?.parameters;
		if (params !== undefined && params !== null && (typeof params !== "object" || params.type !== "object")) {
			problems.push(`工具形状  ${name}（${ext.path}）\n          parameters 根不是 object：要写成 Type.Object({...})，并 import { Type } from "typebox"`);
		} else {
			tools.push(name);
		}
	}
}

if (problems.length) {
	console.log(`扩展自检没过（${problems.length} 处）：`);
	for (const p of problems) console.log("- " + p);
	process.exit(1);
}
console.log(`扩展自检通过：${(result.extensions ?? []).length} 个扩展、${tools.length} 件工具都能加载（${tools.join(", ")}）`);
process.exit(0);
