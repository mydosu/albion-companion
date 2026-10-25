/**
 * albion-loopguard 自检：坏工具摘除 / 【接着说完】撤工具 / 复读断路（按「同一调用 + 文件状态没变」算）/ 快照账本。
 * 不叫模型、不碰网络；只在临时目录里造文件。Windows / WSL 都能跑。
 *
 * 跑：node bin/test_loopguard.ts
 */
import assert from "node:assert";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";

const ROOT = mkdtempSync(join(tmpdir(), "albion-lg-"));
const HOME = join(ROOT, "家");
const EXT = join(ROOT, "extensions");
const OUT = join(ROOT, "outside");
for (const d of [HOME, EXT, OUT, join(EXT, "node_modules", "x")]) mkdirSync(d, { recursive: true });
process.env.ALBION_WATCH_DIRS = [HOME, EXT].join(delimiter);
process.env.ALBION_LOOPGUARD_LOG = join(ROOT, "loopguard.log");
process.env.ALBION_LEDGER_IGNORE = join(ROOT, "junk");
writeFileSync(join(HOME, "note.txt"), "第一行\n");
writeFileSync(join(EXT, "a.ts"), "export default 1;\n");

const lg = await import("../agent/extensions/albion-loopguard.ts");
const { schemaOk, snapshot, changedBetween, turnFacts, normKey } = lg;
let n = 0;
const ok = (c: unknown, msg: string) => {
  n++;
  assert.ok(c, msg);
};

function mk() {
  const h: Record<string, ((e: any) => any)[]> = {};
  lg.default({ on: (name: string, fn: (e: any) => any) => (h[name] ||= []).push(fn) } as any);
  const fire = (name: string, e: any) => (h[name] ?? []).map((fn) => fn(e)).find((r) => r !== undefined);
  let id = 0;
  return {
    start: () => fire("agent_start", { type: "agent_start" }),
    call: (toolName: string, input: any) => fire("tool_call", { toolName, input, toolCallId: String(++id) }),
    say: (text: string) => fire("message_end", { message: { role: "assistant", content: [{ type: "text", text }] } }),
    sayTool: () => fire("message_end", { message: { role: "assistant", content: [{ type: "toolCall", name: "bash" }] } }),
    end: () => fire("agent_end", {}),
    req: (payload: any) => fire("before_provider_request", { payload }),
  };
}
const ledger = () => (globalThis as any).__albionLedger?.changed ?? null;
const facts = () => String((globalThis as any).__albionTurnFacts ?? "");

// 1) schema 形状
ok(schemaOk(undefined) && schemaOk(null) && schemaOk({ type: "object", properties: {} }), "没声明或根为 object 都算合格");
ok(!schemaOk({ url: "x" }) && !schemaOk("x") && !schemaOk({ type: "string" }), "普通对象/字符串/非 object 根都不合格");

// 2) 出门前摘掉坏工具；全合格就不动
{
  const g = mk();
  const good = { type: "function", function: { name: "good", parameters: { type: "object" } } };
  const bad = { type: "function", function: { name: "bad", parameters: { url: "x" } } };
  const out = g.req({ messages: [{ role: "user", content: "hi" }], tools: [good, bad, good] });
  ok(out && out.tools.length === 2 && out.tools.every((t: any) => t.function.name === "good"), "坏工具被摘、好的留着");
  ok(g.req({ messages: [{ role: "user", content: "hi" }], tools: [good] }) === undefined, "全合格不改请求");
  ok(readFileSync(process.env.ALBION_LOOPGUARD_LOG!, "utf-8").includes('"dropped_tools":["bad"]'), "摘除记进日志（走 ALBION_LOOPGUARD_LOG，不写生产日志）");
}

// 3) 【接着说完】那一轮只用文字：工具和 tool_choice 一并撤掉（带时间戳前缀也认）
{
  const g = mk();
  const out = g.req({ messages: [{ role: "user", content: "[2026-10-16 10:00] 【接着说完】刚才那一轮……" }], tools: [{ function: { name: "x" } }], tool_choice: "auto" });
  ok(out && !("tools" in out) && !("tool_choice" in out), "补问那一轮不给工具");
}

// 4) 同一条命令第 3 次挡；空白折叠、键顺序不同也算同一条
{
  const g = mk();
  g.start();
  ok(g.call("bash", { command: "ls /root" }) === undefined, "第 1 次放行");
  ok(g.call("bash", { command: "ls   /root\n" }) === undefined, "第 2 次（空白不同）放行");
  const third = g.call("bash", { command: " ls /root " });
  ok(third?.block === true && !third.terminate && /跑过 2 次/.test(third.reason), "第 3 次挡下，说明为什么");
  ok(normKey({ toolName: "read", input: { path: "a", offset: 1 } }) === normKey({ toolName: "read", input: { offset: 1, path: "a" } }), "参数键顺序不同算同一条");
}

// 5) 两条命令来回换也挡（按最近 12 次算，不只看连续）
{
  const g = mk();
  g.start();
  g.call("bash", { command: "A" });
  g.call("bash", { command: "B" });
  g.call("bash", { command: "A" });
  g.call("bash", { command: "B" });
  ok(g.call("bash", { command: "A" })?.block === true, "A/B 来回换，A 第 3 次照挡");
}

// 6) 同一个文件 read 149 次的回归：第 3 次挡、第 8 次停用全部工具、停用后再硬发到第 3 次才终止本轮
{
  const g = mk();
  g.start();
  const rs: any[] = [];
  for (let i = 0; i < 8; i++) rs.push(g.call("read", { path: "/root/家/书/长篇.txt" }));
  ok(rs[0] === undefined && rs[1] === undefined && rs[2]?.block, "read 也守：第 3 次挡");
  ok(rs[7]?.block && /停用/.test(rs[7].reason), "第 8 次停用这一轮的工具");
  const after = [g.call("edit", { path: "x", oldText: "a", newText: "b" }), g.call("bash", { command: "echo hi" }), g.call("bash", { command: "echo hi2" }), g.call("read", { path: "y" })];
  ok(after[0]?.block && after[1]?.block && !after[1].terminate, "停用后任何工具都不执行（含 edit）");
  ok(after[3]?.terminate === true, "停用后硬发到第 3 次以上才终止本轮");
}

// 7) edit / write 不守（重发多是正常重试）
{
  const g = mk();
  g.start();
  for (let i = 0; i < 5; i++) ok(g.call("edit", { path: join(OUT, "x.txt"), oldText: "a", newText: "b" }) === undefined, `edit 第 ${i + 1} 次放行`);
}

// 8) 改了新内容再跑同一条自检不算复读；往忽略目录写垃圾、原样重写同一份内容都不算改
{
  const g = mk();
  g.start();
  const test = { command: "node bin/check_extensions.ts" };
  g.call("bash", test);
  g.call("bash", test);
  writeFileSync(join(EXT, "a.ts"), "export default 2;\n");
  ok(g.call("bash", test) === undefined, "扩展真改了：第 3 次自检放行");
  g.call("bash", test);
  mkdirSync(join(ROOT, "junk"), { recursive: true });
  writeFileSync(join(ROOT, "junk", "g.txt"), String(Date.now()));
  ok(g.call("bash", test)?.block === true, "往监视目录外写垃圾不清零");
  const g2 = mk();
  g2.start();
  g2.call("bash", test);
  g2.call("bash", test);
  writeFileSync(join(EXT, "a.ts"), "export default 2;\n"); // 内容一模一样
  ok(g2.call("bash", test)?.block === true, "原样重写同一份内容不算改，照挡");
}

// 9) 每轮清零；窗口外的旧调用不算
{
  const g = mk();
  g.start();
  g.call("bash", { command: "Q" });
  g.call("bash", { command: "Q" });
  g.start();
  ok(g.call("bash", { command: "Q" }) === undefined, "新的一轮重新计数");
  for (let i = 0; i < 12; i++) g.call("bash", { command: "other-" + i });
  g.call("bash", { command: "Q" });
  ok(g.call("bash", { command: "Q" }) === undefined, "隔了 12 次以上，旧的那次不算");
}

// 10) 账本：一轮前后比快照——新建、改动、删除都记；内容没变的不记；node_modules 不看；监视外但 edit/write 写到的也记
{
  const g = mk();
  writeFileSync(join(HOME, "gone.txt"), "x");
  writeFileSync(join(OUT, "outside.txt"), "v1");
  g.start();
  writeFileSync(join(HOME, "new.txt"), "新");
  writeFileSync(join(HOME, "note.txt"), "第一行\n第二行\n");
  unlinkSync(join(HOME, "gone.txt"));
  writeFileSync(join(EXT, "a.ts"), "export default 2;\n"); // 与上一步相同内容：不算
  writeFileSync(join(EXT, "node_modules", "x", "i.js"), "1");
  g.call("write", { path: join(OUT, "outside.txt"), content: "v2" });
  writeFileSync(join(OUT, "outside.txt"), "v2");
  ok(g.sayTool() === undefined && ledger()?.length === 0, "还在调工具时不记账");
  g.say("改好了。");
  const ch: string[] = ledger();
  ok(ch.includes(join(HOME, "new.txt")) && ch.includes(join(HOME, "note.txt")) && ch.includes(join(HOME, "gone.txt")), "新建/改动/删除都在账本里：" + JSON.stringify(ch));
  ok(!ch.includes(join(EXT, "a.ts")), "内容没变的不记");
  ok(!ch.some((p) => p.includes("node_modules")), "node_modules 不看");
  ok(ch.includes(join(OUT, "outside.txt")), "监视目录外、但这一轮 write 写到的也记");
  ok(facts().includes("真改了的文件") && facts().includes("new.txt"), "下一轮的事实句列出真改了的文件");
}

// 11) 动了写工具却没有文件变化：事实句照实说；什么都没写：不说
{
  const g = mk();
  g.start();
  g.call("bash", { command: "sed -i s/不存在/x/ " + join(HOME, "note.txt") });
  g.say("改好了。");
  ok(ledger()?.length === 0 && /没有文件真的变化/.test(facts()), "写了但没变：照实说");
  const g2 = mk();
  g2.start();
  g2.call("read", { path: join(HOME, "note.txt") });
  g2.say("看完了。");
  ok(facts() === "", "只读了：不说");
  const g3 = mk();
  g3.start();
  writeFileSync(join(HOME, "late.txt"), "x");
  g3.end();
  ok(ledger()?.includes(join(HOME, "late.txt")), "被终止、没有定稿的那一轮，agent_end 照样记账");
}

// 12) 事实句的形状
{
  ok(turnFacts([], false) === "", "没写没变不说");
  const many = Array.from({ length: 12 }, (_, i) => `/root/家/f${i}.txt`);
  ok(/等 12 个/.test(turnFacts(many, true)) && !turnFacts(many, true).includes("f11"), "太多只列前 8 个");
  const s1 = snapshot([HOME]);
  ok(changedBetween(s1, s1).length === 0, "同一份快照没有变化");
}

// 13) albion-turn 把事实句放进下一轮约定（读一次就清）
{
  const { _albionTest } = await import("../agent/extensions/albion-turn.ts");
  const g = _albionTest.turn_guidance("在吗", false, [], "【上一轮改动的事实】真改了的文件：/root/家/a.txt。");
  ok(g.includes("真改了的文件：/root/家/a.txt") && g.trimEnd().endsWith("这一轮没有画面送来，就不说看见了他的样子。"), "事实句进了约定，处境段仍在最末尾");
}

ok(existsSync(process.env.ALBION_LOOPGUARD_LOG!), "日志写在指定位置");
rmSync(ROOT, { recursive: true, force: true });
console.log(`loopguard 自检通过 —— ${n} 项：坏工具被摘 / 补问轮撤工具 / 第 3 次挡、空白与键序折叠 / 来回换也挡 / read 149 次回归：挡→停用→终止 / edit 不守 / 改了才算新状态、垃圾与原样重写不算 / 每轮清零与窗口 / 快照账本：增改删、没变不记、node_modules 不看、监视外写入也记 / 事实句 / 进下一轮约定`);
