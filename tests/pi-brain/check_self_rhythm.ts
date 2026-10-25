/**
 * next_wake 的开口节奏（speak_every_hours，2026-10-16 起由她自己定）：夹在 1～6 小时、不填沿用上次、从没定过是 3 小时。
 * 不叫模型、不碰生产 state/（写进临时目录）。bin/wake.py 读同一个字段，它那头的口径在 wake.py 自检里。
 * Windows/WSL 都能跑（没有 typebox 时用桩，见下）。
 *
 * 跑：node tests/pi-brain/check_self_rhythm.ts [albion-self.ts 路径]
 */
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { register } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

// typebox 只装在 WSL 的 agent/extensions/node_modules；仓库快照/Windows 侧解析不到时换成最小桩（只拼 schema，不校验），
// 解析得到就照常用真包。
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

const dir = mkdtempSync(join(tmpdir(), "albion-self-"));
process.env.ALBION_STATE_DIR = dir;
const ext = process.argv[2]
  ? pathToFileURL(process.argv[2]).href
  : new URL("../../integrations/pi-brain/agent/extensions/albion-self.ts", import.meta.url).href;
const tools: Record<string, any> = {};
(await import(ext)).default({ registerTool: (t: any) => (tools[t.name] = t) } as any);
const wake = (p: Record<string, unknown>) => tools.next_wake.execute("t", p);
const plan = () => JSON.parse(readFileSync(join(dir, "self.json"), "utf-8"));

await wake({ minutes: 30, intent: "看看书" });
assert.equal(plan().speak_every_hours, 3, "从没定过：3 小时");
const r = await wake({ minutes: 20, intent: "x", speak_every_hours: 2 });
assert.equal(plan().speak_every_hours, 2);
assert.match(r.content[0].text, /每 2 小时/);
await wake({ minutes: 20, intent: "y" });
assert.equal(plan().speak_every_hours, 2, "不填就沿用上次定的");
await wake({ minutes: 20, intent: "z", speak_every_hours: 0.1 });
assert.equal(plan().speak_every_hours, 1, "低于 1 小时夹到 1");
await wake({ minutes: 20, intent: "z", speak_every_hours: 24 });
assert.equal(plan().speak_every_hours, 6, "高于 6 小时夹到 6");
await wake({ minutes: 20, intent: "z", speak_every_hours: "" });
assert.equal(plan().speak_every_hours, 6, "空串当没填");
writeFileSync(join(dir, "self.json"), JSON.stringify({ speak_every_hours: null }));
await wake({ minutes: 20, intent: "z" });
assert.equal(plan().speak_every_hours, 3, "上次的值坏了（null）：回到默认 3 小时，不夹成 1");
assert.ok(Math.abs(plan().wake_at - (Date.now() / 1000 + 20 * 60)) < 5, "醒来时间照旧");
assert.match(tools.next_wake.description, /1～6 小时/);
rmSync(dir, { recursive: true, force: true });
console.log("SELF RHYTHM OK：默认 3 小时 / 照她定的 / 不填沿用 / 夹在 1～6 / 坏值回默认");
