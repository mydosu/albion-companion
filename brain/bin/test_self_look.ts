/**
 * next_wake（一条计时）与 look（随时可用）的自检：不碰网络、不起子进程，全在临时 state 目录里跑。
 *
 * 覆盖：
 *   · next_wake 的分钟数夹在 30～180（没给有效值就 60），self.json 是一条计时
 *     {wake_at,intent,set_at,speak_every_hours}；写回时不再分说话轮/生活轮；
 *   · 旧的两线 self.json（speak / life）和旧顶层格式都读得出开口节奏，写回摊成一条；
 *   · did 记进 state/life.json，只留最近 20 条，每条带时间；
 *   · look 随时可用：wake.json 里留着旧的生活轮字段（round_kind/round_at）也照样去取图
 *     （没配采集端就如实说没看到，不会被闸门挡回）。
 *
 * 跑：node bin/test_self_look.ts   （typebox 解析不到时用最小桩，和 tests/pi-brain/check_self_rhythm.ts 一样）
 */
import assert from "node:assert";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { register } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

// pi 把 typebox 装在自己的 node_modules 里；仓库快照/Windows 侧解析不到时换成最小桩（只拼 schema，不校验）
const typeboxStub =
	"const s=(t)=>(o={})=>({type:t,...o});" +
	"export const Type={Object:(p)=>({type:'object',properties:p}),Number:s('number'),String:s('string')," +
	"Literal:(v)=>({const:v}),Union:(a)=>({anyOf:a}),Optional:(x)=>x};";
register(
	"data:text/javascript," +
		encodeURIComponent(
			`export async function resolve(spec, ctx, next) {
        try { return await next(spec, ctx); }
        catch (e) {
          if (spec === "typebox") return { url: "data:text/javascript," + encodeURIComponent(${JSON.stringify(typeboxStub)}), shortCircuit: true };
          throw e;
        }
      }`,
		),
);

const TMP = mkdtempSync(join(tmpdir(), "albion-self-look-"));
process.env.ALBION_STATE_DIR = TMP;
process.env.PI_CODING_AGENT_DIR = TMP; // eyesConfig 读不到 .env：放行后只会说「没配好」，不发请求
process.env.ALBION_SESSION_ID = "albion-desktop";
delete process.env.ALBION_PERSONAL_STATE_DIR;
delete process.env.ALBION_EPHEMERAL;

const { default: albionSelf } = await import("../agent/extensions/albion-self.ts");
const lookMod: any = await import("../agent/extensions/albion-look.ts");
assert.equal(lookMod.inLifeRound, undefined, "生活轮闸门已撤掉");

const tools: Record<string, any> = {};
const pi = { registerTool: (t: any) => void (tools[t.name] = t) } as any;
albionSelf(pi);
lookMod.default(pi);
assert.ok(tools.next_wake && tools.look, "两个工具都注册上了");

const read = (name: string) => JSON.parse(readFileSync(join(TMP, name), "utf-8"));
const write = (name: string, v: unknown) => writeFileSync(join(TMP, name), JSON.stringify(v), "utf-8");
const run = async (name: string, params: any): Promise<string> => {
	const r = await tools[name].execute("call-1", params, undefined, undefined, { model: { input: ["text"] } });
	return r.content
		.filter((b: any) => b.type === "text")
		.map((b: any) => b.text)
		.join("");
};
const near = (a: number, b: number, tol = 3) => Math.abs(a - b) <= tol;

// A) 一条计时：15 分钟夹到 30；self.json 是 {wake_at,intent,set_at,speak_every_hours}
{
	const out = await run("next_wake", { minutes: 15, intent: "跟他说说今天的梦" });
	const plan = read("self.json");
	assert.ok(near(plan.wake_at, Date.now() / 1000 + 30 * 60), JSON.stringify(plan));
	assert.equal(plan.intent, "跟他说说今天的梦");
	assert.ok(plan.set_at > 0);
	assert.equal(plan.speak, undefined, "不再分说话轮");
	assert.equal(plan.life, undefined, "不再分生活轮");
	assert.equal(plan.speak_every_hours, 3, "没定过开口节奏：默认 3 小时");
	assert.ok(out.includes("左右醒") && out.includes("今天的梦"), out);
	console.log("A 一条计时：15 分钟夹到 30、self.json 扁平 OK");
}

// B) 上限 180：500 分钟夹到 180；下限 30；did 进生活记录
{
	const out = await run("next_wake", { minutes: 500, intent: "画完那只猫", did: "画了一半" });
	const plan = read("self.json");
	assert.ok(near(plan.wake_at, Date.now() / 1000 + 180 * 60), "上限 180 分钟：" + JSON.stringify(plan));
	assert.equal(plan.intent, "画完那只猫");
	assert.ok(out.includes("调成了 180 分钟"), out);
	const life = read("life.json");
	assert.equal(life.length, 1);
	assert.equal(life[0].did, "画了一半");
	assert.ok(near(life[0].at, Date.now() / 1000));
	const out2 = await run("next_wake", { minutes: 10, intent: "上网看看" });
	const plan2 = read("self.json");
	assert.ok(near(plan2.wake_at, Date.now() / 1000 + 30 * 60), "下限 30 分钟：" + JSON.stringify(plan2));
	assert.ok(out2.includes("调成了 30 分钟"), out2);
	console.log("B 分钟数夹在 30～180、did 落库 OK");
}

// C) 旧 self.json：两线格式和顶层格式都读得出开口节奏，写回摊成一条
{
	const old = {
		speak: { wake_at: 1234567890, intent: "旧说话轮", set_at: 1234567000 },
		life: { wake_at: 1234567000, intent: "旧生活轮", set_at: 1234566000 },
		speak_every_hours: 2,
	};
	write("self.json", old);
	await run("next_wake", { minutes: 45, intent: "整理画廊" });
	const plan = read("self.json");
	assert.equal(plan.speak, undefined, "旧两线摊成一条");
	assert.equal(plan.life, undefined, "旧两线摊成一条");
	assert.equal(plan.intent, "整理画廊");
	assert.equal(plan.speak_every_hours, 2, "她定过的开口节奏要留着");
	assert.ok(near(plan.wake_at, Date.now() / 1000 + 45 * 60), JSON.stringify(plan));
	write("self.json", { wake_at: 1234567890, intent: "旧顶层", set_at: 1234567000, speak_every_hours: 4 });
	await run("next_wake", { minutes: 60, intent: "一条计时" });
	const plan2 = read("self.json");
	assert.equal(plan2.speak_every_hours, 4, "旧顶层格式的开口节奏也留着");
	assert.equal(plan2.intent, "一条计时");
	console.log("C 旧两线/顶层 self.json 的开口节奏不丢、写回扁平 OK");
}

// D) life.json 只留最近 20 条
{
	write("life.json", []);
	for (let i = 0; i < 25; i++) await run("next_wake", { minutes: 60, intent: "待会儿", did: `第${i}件` });
	const life = read("life.json");
	assert.equal(life.length, 20, "只留最近 20 条：" + life.length);
	assert.equal(life[19].did, "第24件");
	assert.equal(life[0].did, "第5件");
	console.log("D life.json 只留最近 20 条 OK");
}

// E) look 随时可用：旧 wake.json 里留着生活轮字段也不再挡
{
	const now = Date.now();
	for (const wake of [
		{ round_kind: "life", round_at: now / 1000 },
		{ round_kind: "life", round_at: 0 },
		{ round_kind: "life", round_at: "坏" },
	]) {
		write("wake.json", wake);
		const out = await run("look", { target: "his" });
		assert.ok(out.includes("没配好"), "放行后如实说没看到（不是被闸门挡回）：" + out);
	}
	writeFileSync(join(TMP, "wake.json"), "{坏", "utf-8");
	const out = await run("look", { target: "cam" });
	assert.ok(out.includes("没配好"), out);
	console.log("E look 任何时候都放行（旧生活轮字段/坏状态都不挡）OK");
}

console.log("SELF_LOOK SELFTEST OK");
