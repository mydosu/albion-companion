/**
 * next_wake（两条计时）与 look（生活轮闸门）的自检：不碰网络、不起子进程，全在临时 state 目录里跑。
 *
 * 覆盖：
 *   · next_wake 默认 kind=speak；kind=life 的分钟数夹在 30～180；两条计时各写各的、互不冲掉；
 *   · 旧 self.json（顶层 wake_at/intent/set_at）当成 speak，写新结构时原样带过去；
 *   · did 记进 state/life.json，只留最近 20 条，每条带时间；确认文字按 kind 说清是哪一条；
 *   · look 在生活轮里不取图，返回「这是我自己的时间，先不看他。」；轮结束（wake.json 清掉）或 15 分钟超时后放行。
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
const { default: albionLook, inLifeRound } = await import("../agent/extensions/albion-look.ts");

const tools: Record<string, any> = {};
const pi = { registerTool: (t: any) => void (tools[t.name] = t) } as any;
albionSelf(pi);
albionLook(pi);
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

// A) 默认 kind=speak：10～360 分钟，self.json 是两条计时的新结构
{
	const out = await run("next_wake", { minutes: 15, intent: "跟他说说今天的梦" });
	const plan = read("self.json");
	assert.ok(near(plan.speak.wake_at, Date.now() / 1000 + 900), JSON.stringify(plan.speak));
	assert.equal(plan.speak.intent, "跟他说说今天的梦");
	assert.ok(plan.speak.set_at > 0);
	assert.deepEqual(plan.life, {}, "定说话轮不编造生活轮");
	assert.equal(plan.speak_every_hours, 3, "没定过开口节奏：默认 3 小时");
	assert.ok(out.includes("下次跟他说话") && out.includes("今天的梦"), out);
	console.log("A 默认 speak：15 分钟、新结构、确认文字 OK");
}

// B) kind=life：30～180 分钟；两条计时互不冲掉；did 进生活记录
{
	const out = await run("next_wake", { kind: "life", minutes: 10, intent: "画完那只猫", did: "画了一半" });
	const plan = read("self.json");
	assert.ok(near(plan.life.wake_at, Date.now() / 1000 + 30 * 60), "life 下限 30 分钟：" + JSON.stringify(plan.life));
	assert.equal(plan.life.intent, "画完那只猫");
	assert.equal(plan.speak.intent, "跟他说说今天的梦", "定生活轮不冲掉说话轮");
	assert.ok(out.includes("回到我自己的时间") && !out.includes("下次跟他说话"), out);
	const life = read("life.json");
	assert.equal(life.length, 1);
	assert.equal(life[0].did, "画了一半");
	assert.ok(near(life[0].at, Date.now() / 1000));
	const out2 = await run("next_wake", { kind: "life", minutes: 500, intent: "上网看看" });
	const plan2 = read("self.json");
	assert.ok(near(plan2.life.wake_at, Date.now() / 1000 + 180 * 60), "life 上限 180 分钟：" + JSON.stringify(plan2.life));
	assert.equal(plan2.speak.intent, "跟他说说今天的梦");
	assert.ok(out2.includes("调成了 180 分钟"), out2);
	console.log("B life 夹在 30～180、两条计时互不冲掉、did 落库 OK");
}

// C) 旧 self.json 格式：顶层 wake_at/intent/set_at 当成 speak，写新结构时带过去
{
	const old = { wake_at: 1234567890, intent: "旧格式定的事", set_at: 1234567000, speak_every_hours: 2 };
	write("self.json", old);
	await run("next_wake", { kind: "life", minutes: 45, intent: "整理画廊" });
	const plan = read("self.json");
	assert.equal(plan.speak.wake_at, old.wake_at);
	assert.equal(plan.speak.intent, old.intent);
	assert.equal(plan.speak.set_at, old.set_at);
	assert.equal(plan.speak_every_hours, 2, "她定过的开口节奏要留着");
	assert.equal(plan.life.intent, "整理画廊");
	console.log("C 旧 self.json 兼容成 speak、开口节奏不丢 OK");
}

// D) life.json 只留最近 20 条
{
	write("life.json", []);
	for (let i = 0; i < 25; i++) await run("next_wake", { kind: "life", minutes: 60, intent: "待会儿", did: `第${i}件` });
	const life = read("life.json");
	assert.equal(life.length, 20, "只留最近 20 条：" + life.length);
	assert.equal(life[19].did, "第24件");
	assert.equal(life[0].did, "第5件");
	console.log("D life.json 只留最近 20 条 OK");
}

// E) 生活轮闸门：life 轮中不取图；after_wake 清掉后放行；15 分钟超时也放行
{
	const now = Date.now();
	write("wake.json", { round_kind: "life", round_at: now / 1000 });
	assert.equal(inLifeRound(), true);
	for (const target of ["his", "mine", "cam"]) assert.equal(await run("look", { target }), "这是我自己的时间，先不看他。");
	// 轮结束：wake.json 的清理由 wake.after_wake 做，这里照它写出来的样子
	write("wake.json", { round_kind: "", round_at: 0 });
	assert.equal(inLifeRound(), false);
	const allowed = await run("look", { target: "his" });
	assert.notEqual(allowed, "这是我自己的时间，先不看他。", allowed);
	assert.ok(allowed.includes("没配好"), "没配采集端时如实说没看到：" + allowed);
	// 超时兜底：这一轮卡了 16 分钟没结束，也放行
	write("wake.json", { round_kind: "life", round_at: (now - 16 * 60_000) / 1000 });
	assert.equal(inLifeRound(), false);
	assert.notEqual(await run("look", { target: "his" }), "这是我自己的时间，先不看他。");
	// 坏状态不炸、也不挡
	write("wake.json", { round_kind: "life", round_at: "坏" });
	assert.equal(inLifeRound(), false);
	writeFileSync(join(TMP, "wake.json"), "{坏", "utf-8");
	assert.equal(inLifeRound(), false);
	console.log("E look 生活轮闸门：拒绝 / 轮结束放行 / 超时放行 / 坏状态放行 OK");
}

console.log("SELF_LOOK SELFTEST OK");
