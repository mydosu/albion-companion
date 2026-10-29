/**
 * 会话隔离（2026-10-29）：探针/验收/自检会话不写生产心事本/self/todo/voice/last-context，真实会话一个字节都不变。
 *
 * 背景：2026-10-25 的口癖测试脚本用独立会话在生产脑上跑，台词「今天好累，实验做了一下午」被当成他说的话
 * 写进共享的 state/notebook.json（#16），之后自主醒来轮次把这条念回来，OpenViking 又把它抽成假事件。
 *
 * 覆盖：isEphemeralSession 的口径（名单/覆盖/空会话名）、personalStateDir 落点、
 * 以及至少一处真实写入被拦（notebook / todo 在 ephemeral 会话写临时目录、生产目录不动）。
 *
 * 不叫模型、不碰生产 state（全部在临时目录里）。Windows/WSL 都能跑。
 * 跑：node tests/pi-brain/check_session_scope.ts
 */
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { register } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// typebox 只装在 WSL 的 agent/extensions/node_modules；仓库快照 / Windows 侧解析不到时换成最小桩（见 check_self_rhythm.ts）
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

const agentDir = fileURLToPath(new URL("../../integrations/pi-brain/agent/", import.meta.url));
process.env.PI_CODING_AGENT_DIR = agentDir;
const prod = mkdtempSync(join(tmpdir(), "albion-prod-state-"));
process.env.ALBION_STATE_DIR = prod;
delete process.env.ALBION_PERSONAL_STATE_DIR;
delete process.env.ALBION_EPHEMERAL;
delete process.env.ALBION_MEMORY_SESSIONS;
delete process.env.ALBION_SESSION_ID;

const { isEphemeralSession, personalStateDir, personalStatePath, realSessions } = await import(
  new URL("../../integrations/pi-brain/agent/lib/session-scope.ts", import.meta.url).href
);

// ── 1. 名单与判断口径 ──────────────────────────────────────────────────────
assert.deepEqual(realSessions(), ["albion-desktop", "albion-cli"], "默认真实会话 = 桌面 + CLI");
assert.equal(isEphemeralSession("albion-desktop"), false, "桌面会话是真实会话");
assert.equal(isEphemeralSession("albion-cli"), false, "CLI 会话是真实会话");
for (const sid of [
  "albion-voice-probe-after4c-05",
  "albion-probe-b6-20261024-133239",
  "albion-accept",
  "albion-dsh-probe",
  "albion-prod-smoke",
  "chain-r1-host",
  "probetest",
]) {
  assert.equal(isEphemeralSession(sid), true, `探针/验收会话要算 ephemeral：${sid}`);
}
assert.equal(isEphemeralSession(""), false, "没有会话名的手工运行按生产算（旧行为不变）");

process.env.ALBION_EPHEMERAL = "1";
assert.equal(isEphemeralSession("albion-desktop"), true, "ALBION_EPHEMERAL=1 强制隔离（宿主兜底）");
delete process.env.ALBION_EPHEMERAL;

process.env.ALBION_MEMORY_SESSIONS = "albion-desktop";
assert.equal(isEphemeralSession("albion-cli"), true, "ALBION_MEMORY_SESSIONS 覆盖名单");
delete process.env.ALBION_MEMORY_SESSIONS;

// ── 2. 状态目录落点 ────────────────────────────────────────────────────────
assert.equal(personalStateDir("albion-desktop"), prod, "真实会话仍写 ALBION_STATE_DIR");
const probeDir = personalStateDir("albion-voice-probe-after4c-05");
assert.ok(
  probeDir.includes(join("albion-pi-ephemeral", "albion-voice-probe-after4c-05")),
  `ephemeral 会话要落到临时目录，实得 ${probeDir}`,
);
assert.notEqual(probeDir, prod);
assert.ok(personalStatePath("notebook.json", "albion-accept").endsWith(join("albion-accept", "notebook.json")));
process.env.ALBION_PERSONAL_STATE_DIR = join(prod, "given-by-host");
assert.equal(personalStateDir("albion-accept"), join(prod, "given-by-host"), "宿主给了 ALBION_PERSONAL_STATE_DIR 就用它");
delete process.env.ALBION_PERSONAL_STATE_DIR;

// ── 3. 真实写入被拦：notebook / todo 在 ephemeral 会话不落生产 ─────────────
async function loadTools(rel: string): Promise<Record<string, any>> {
  const tools: Record<string, any> = {};
  const mod = await import(new URL(`../../integrations/pi-brain/agent/extensions/${rel}`, import.meta.url).href);
  mod.default({ registerTool: (t: any) => (tools[t.name] = t) } as any);
  return tools;
}

const self = await loadTools("albion-self.ts");
const todo = await loadTools("albion-todo.ts");

// 真实会话：照旧写生产目录
process.env.ALBION_SESSION_ID = "albion-desktop";
let r = await self.notebook.execute("t", { action: "add", text: "真实会话照旧写生产" });
assert.match(r.content[0].text, /^记下了：#1 /, "工具照常返回");
assert.ok(existsSync(join(prod, "notebook.json")), "真实会话必须还写生产心事本");
assert.match(readFileSync(join(prod, "notebook.json"), "utf-8"), /真实会话照旧写生产/);
await todo.todo.execute("t", { action: "plan", title: "真实", steps: "一\n二" });
assert.ok(existsSync(join(prod, "todo.json")), "真实会话必须还写生产 todo");
rmSync(join(prod, "notebook.json"), { force: true });
rmSync(join(prod, "todo.json"), { force: true });

// ephemeral 会话：工具照常返回，但只写临时目录；生产目录一个文件都不新增
process.env.ALBION_SESSION_ID = "albion-voice-probe-after4c-05";
r = await self.notebook.execute("t", { action: "add", text: "探针台词不该进生产" });
assert.match(r.content[0].text, /^记下了：#1 探针台词不该进生产/, "ephemeral 会话工具照常返回");
assert.equal(existsSync(join(prod, "notebook.json")), false, "ephemeral 会话不能写生产心事本");
const probeNotebook = personalStatePath("notebook.json");
assert.ok(existsSync(probeNotebook), `ephemeral 心事本要落在临时目录：${probeNotebook}`);
assert.match(readFileSync(probeNotebook, "utf-8"), /探针台词不该进生产/);
r = await self.notebook.execute("t", { action: "list" });
assert.match(r.content[0].text, /探针台词不该进生产/, "同一会话里读得回刚记的那条");
r = await self.notebook.execute("t", { action: "done", id: 1 });
assert.match(r.content[0].text, /^划掉了：#1 /);
r = await self.notebook.execute("t", { action: "list" });
assert.match(r.content[0].text, /没有没划掉的事/);

await todo.todo.execute("t", { action: "plan", title: "探针", steps: "一\n二" });
assert.equal(existsSync(join(prod, "todo.json")), false, "ephemeral 会话不能写生产 todo");
assert.ok(existsSync(personalStatePath("todo.json")), "ephemeral todo 要落在临时目录");

// next_wake 也不碰生产 self.json
await self.next_wake.execute("t", { minutes: 20, intent: "探针" });
assert.equal(existsSync(join(prod, "self.json")), false, "ephemeral 会话不能写生产 self.json");
assert.ok(existsSync(personalStatePath("self.json")), "ephemeral self.json 要落在临时目录");

console.log("SESSION SCOPE OK：名单口径 / 覆盖 / 临时目录落点 / notebook+todo+next_wake 写入被拦");
rmSync(prod, { recursive: true, force: true });
rmSync(personalStateDir("albion-voice-probe-after4c-05"), { recursive: true, force: true });
