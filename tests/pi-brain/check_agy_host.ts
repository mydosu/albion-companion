/**
 * agy 扩展宿主自检（2026-11-27，B 块）：`brain/bin/agy_host.ts` 起得来、三个面都对、不碰生产。
 *
 * 只在 WSL 里跑：要 Pi 的加载器（/usr/local/bin/pi）和 agent/extensions/node_modules。
 * 跑法（Windows 侧把仓库拷进来，再在 WSL 里跑；见 check_agy_host.ps1）：
 *   node /tmp/agy-host-dev/repo/tests/pi-brain/check_agy_host.ts
 *
 * 覆盖：
 *   ① tools/list = 18 件扩展工具（加上壳插件同步来的代理工具）；
 *   ② 调两件纯本地工具（lunar_calendar、todo），壳插件工具也真能调（假的壳桥）；
 *   ③ before 的 prompt 里带「【阿尔比恩本轮说话约定】」块，system_append 带壳插件规则；
 *   ④ event 把工具行写进临时活动库（state.db）；
 *   ⑤ final 跑 guard 的确定性整理（`<ACT:…>` 补成 `<|ACT:…|>`）；
 *   ⑥ 两个会话互不串：各自的 ALBION_SESSION_ID、各自的 todo；
 *   ⑦ /health 报得出会话、工具数、扩展错误。
 *
 * 不叫模型、不联网（OpenViking 端点指到丢弃端口，载荷全部落在临时目录）。
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { appendFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const KEEP = process.argv.includes("--keep");
const PROD_NODE_MODULES = "/root/albion-pi/agent/extensions/node_modules";
const PI_LOADER = "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";

const BASE_TOOLS = [
  "adventure", "browser", "chat_search", "delegate", "draw", "find_sticker", "grab_page", "grok_web_search",
  "look", "look_image", "lunar_calendar", "next_wake", "notebook", "page_outline", "send_message", "skill",
  "todo", "web_search",
];

const results: Array<{ name: string; ok: boolean; detail?: string }> = [];
function check(name: string, fn: () => void | Promise<void>): Promise<void> {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      results.push({ name, ok: true });
      console.log(`  ok   ${name}`);
    })
    .catch((e) => {
      results.push({ name, ok: false, detail: String(e?.message ?? e) });
      console.log(`  FAIL ${name}\n       ${String(e?.message ?? e)}`);
    });
}

// ── 临时目录：宿主、状态、日志、活动库全在这里 ────────────────────────────────
const root = mkdtempSync(join(tmpdir(), "agy-host-check-"));
const sandbox = join(root, "brain");
const stateDir = join(root, "state");
const logDir = join(root, "logs");
const myTmp = join(root, "tmp");
for (const d of [join(sandbox, "bin"), join(sandbox, "agent"), stateDir, logDir, myTmp]) mkdirSync(d, { recursive: true });

cpSync(join(repo, "brain", "bin", "agy_host.ts"), join(sandbox, "bin", "agy_host.ts"));
cpSync(join(repo, "brain", "agent"), join(sandbox, "agent"), {
  recursive: true,
  filter: (src: string) => !src.includes("node_modules"),
});
// typebox / lunar-javascript 只装在生产的扩展目录旁；只读地借过来，不往仓库里写
if (existsSync(PROD_NODE_MODULES)) symlinkSync(PROD_NODE_MODULES, join(sandbox, "agent", "extensions", "node_modules"), "dir");

const stateDb = join(stateDir, "state.db");
const port = 18661;
const brainPort = 18650;

// ── 假壳桥：壳插件的工具清单 + 调用 ──────────────────────────────────────────
const shellCalls: any[] = [];
const fakeBrain = createServer((req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${brainPort}`);
  if (req.method === "GET" && url.pathname === "/albion/shell-tools") {
    const body = JSON.stringify({
      version: 1,
      tools: [{ name: "say_hi", description: "壳插件的打招呼工具", parameters: { type: "object", properties: { who: { type: "string" } } } }],
      prompts: [{ id: "demo", title: "演示规则", content: "这条规则来自壳插件。" }],
    });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(body);
    return;
  }
  if (req.method === "POST" && url.pathname === "/albion/shell-tools/call") {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const msg = JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}");
      shellCalls.push(msg);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, text: `壳说：你好 ${msg?.arguments?.who ?? "？"}（session=${msg?.session}）` }));
    });
    return;
  }
  res.writeHead(404, { "content-type": "application/json" });
  res.end("{}");
});
await new Promise<void>((r) => fakeBrain.listen(brainPort, "127.0.0.1", () => r()));

// ── 起宿主 ───────────────────────────────────────────────────────────────────
const hostLog = join(logDir, "host.log");
const env: Record<string, string> = {
  PATH: process.env.PATH ?? "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
  HOME: process.env.HOME ?? "/root",
  TZ: "Asia/Shanghai",
  PI_CODING_AGENT_DIR: join(sandbox, "agent"),
  ALBION_HOST_PORT: String(port),
  ALBION_HOST_EXT_DIR: join(sandbox, "agent", "extensions"),
  ALBION_HOST_CWD: "/root",
  ALBION_HOST_MODEL: "Gemini 3.8 Flash (Medium)",
  ALBION_HOST_LOG: hostLog,
  // relay（模型转发）与 openviking（真记忆）都不进这次自检
  ALBION_HOST_SKIP: "albion-relay.ts,openviking.ts",
  ALBION_STATE_DIR: stateDir,
  ALBION_STATE_DB: stateDb,
  ALBION_LOOPGUARD_LOG: join(logDir, "loopguard.log"),
  ALBION_GUARD_LOG: join(logDir, "guard.log"),
  ALBION_BRAIN_PORT: String(brainPort),
  API_SERVER_KEY: "check-agy-host",
  OPENVIKING_BYPASS_SESSION: "1",
  OPENVIKING_ENDPOINT: "http://127.0.0.1:9", // 丢弃端口：回忆立刻失败，一条都不写
  TMPDIR: myTmp,
  NODE_NO_WARNINGS: "1",
};
const host = spawn(process.execPath, [join(sandbox, "bin", "agy_host.ts")], { env, stdio: ["ignore", "pipe", "pipe"] });
let hostErr = "";
host.stderr.on("data", (c: Buffer) => (hostErr += c.toString("utf-8")));
host.stdout.on("data", () => {});

async function waitHealth(timeoutMs = 40000): Promise<any> {
  const t0 = Date.now();
  let lastErr = "";
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) return await r.json();
      lastErr = `HTTP ${r.status}`;
    } catch (e) {
      lastErr = String((e as Error)?.message ?? e);
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`宿主没起来（${lastErr}）\n${hostErr.slice(-2000)}`);
}

async function mcp(session: string, msg: any): Promise<any> {
  const r = await fetch(`http://127.0.0.1:${port}/mcp/${encodeURIComponent(session)}`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(msg),
  });
  assert.equal(r.status, 200, `MCP ${msg.method} HTTP ${r.status}`);
  return await r.json();
}
async function mcpSse(session: string, msg: any): Promise<{ ctype: string; text: string; data: any }> {
  const r = await fetch(`http://127.0.0.1:${port}/mcp/${encodeURIComponent(session)}`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify(msg),
  });
  const ctype = String(r.headers.get("content-type") ?? "");
  const text = await r.text();
  const data = ctype.includes("text/event-stream") ? JSON.parse(text.replace(/^event: message\ndata: /, "").trim()) : JSON.parse(text);
  return { ctype, text, data };
}
async function turn<T = any>(session: string, action: string, body: any): Promise<T> {
  const r = await fetch(`http://127.0.0.1:${port}/turn/${encodeURIComponent(session)}/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await r.json()) as T;
}
function toolNames(list: any): string[] {
  return (list?.result?.tools ?? []).map((t: any) => t.name).sort();
}
async function callTool(session: string, name: string, args: any): Promise<any> {
  return await mcp(session, { jsonrpc: "2.0", id: 99, method: "tools/call", params: { name, arguments: args } });
}
function textOf(out: any): string {
  const content = out?.result?.content ?? [];
  return content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
}
function readDbRows(where = ""): any[] {
  const { DatabaseSync } = process.getBuiltinModule("node:sqlite") as any;
  const db = new DatabaseSync(stateDb, { readOnly: true });
  try {
    return db.prepare(`SELECT session_id, role, content, tool_name FROM messages ${where} ORDER BY id`).all();
  } finally {
    db.close();
  }
}

let health: any = null;
try {
  health = await waitHealth();
  console.log(`宿主起来了：pid=${health.pid} port=${health.port}`);

  await check("/health 报得出模型、跳过清单与会话", () => {
    assert.equal(health.ok, true);
    assert.equal(health.model, "Gemini 3.8 Flash (Medium)");
    assert.ok(health.skip.includes("albion-relay.ts"));
    assert.ok(health.skip.includes("openviking.ts"));
  });

  const S = "albion-probe-agy-a";

  await check("MCP initialize 回协议版本与 serverInfo", async () => {
    const r = await mcp(S, { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "check", version: "0" } } });
    assert.equal(r.result.serverInfo.name, "albion-agy-host");
    assert.equal(r.result.protocolVersion, "2025-06-18");
    assert.ok(r.result.capabilities.tools);
  });

  await check("notifications/initialized 回 202 无正文", async () => {
    const r = await fetch(`http://127.0.0.1:${port}/mcp/${S}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    });
    assert.equal(r.status, 202);
  });

  await check("tools/list 列得出 18 件扩展工具", async () => {
    const list = await mcp(S, { jsonrpc: "2.0", id: 2, method: "tools/list" });
    const names = toolNames(list);
    for (const n of BASE_TOOLS) assert.ok(names.includes(n), `少了 ${n}`);
    assert.equal(BASE_TOOLS.length, 18);
  });

  await check("tools/list 带上壳插件代理工具（say_hi）", async () => {
    const names = toolNames(await mcp(S, { jsonrpc: "2.0", id: 3, method: "tools/list" }));
    assert.ok(names.includes("say_hi"), `没有 say_hi：${names.join(",")}`);
  });

  await check("tools/call 调 lunar_calendar（纯本地，不联网）", async () => {
    const out = await callTool(S, "lunar_calendar", { date: "2026-10-14" });
    const text = textOf(out);
    assert.ok(text.includes("2026-10-14") || text.includes("农历"), text.slice(0, 200));
    assert.ok(!out.result.isError, text.slice(0, 200));
  });

  await check("tools/call 调 todo（写的是临时状态目录）", async () => {
    const out = await callTool(S, "todo", { action: "plan", title: "自检清单", steps: "第一步\n第二步" });
    const text = textOf(out);
    assert.ok(text.includes("自检清单"), text);
    const f = join(myTmp, "albion-pi-ephemeral", S, "todo.json");
    assert.ok(existsSync(f), `没写到 ${f}`);
    assert.ok(!existsSync(join(stateDir, "todo.json")), "不该写进 ALBION_STATE_DIR");
  });

  await check("tools/call 调壳插件工具，转给壳并带会话名", async () => {
    const out = await callTool(S, "say_hi", { who: "指挥官" });
    const text = textOf(out);
    assert.ok(text.includes("壳说：你好 指挥官"), text);
    assert.equal(shellCalls.at(-1)?.session, S);
  });

  await check("before 的 prompt 里带回忆/约定块，system_append 带壳插件规则", async () => {
    const out = await turn<any>(S, "before", { text: "今天有点累" });
    assert.equal(typeof out.prompt, "string");
    assert.ok(out.prompt.startsWith("今天有点累"), out.prompt.slice(0, 80));
    assert.ok(out.prompt.includes("【阿尔比恩本轮说话约定】"), "约定块没进去");
    assert.ok(out.prompt.includes("【本轮输入来源】"), "本轮输入来源没进去");
    const section = "albion_shell_plugin_" + Buffer.from("demo").toString("hex");
    assert.ok(out.system_append.includes(`<${section}>`), out.system_append.slice(0, 200));
    assert.ok(out.system_append.includes("这条规则来自壳插件。"));
    assert.ok(Array.isArray(out.tools) && out.tools.length >= 18);
  });

  await check("event/tool_call 不拦普通工具", async () => {
    const out = await turn<any>(S, "event", { type: "tool_call", toolCallId: "c1", toolName: "todo", input: { action: "list" } });
    assert.equal(out.abort, false);
  });

  await check("event/tool_result 把工具行写进临时活动库", async () => {
    const out = await turn<any>(S, "event", { type: "tool_result", toolCallId: "c1", toolName: "todo", input: { action: "list" }, text: "清单是空的。", isError: false });
    assert.equal(out.abort, false);
    const rows = readDbRows(`WHERE session_id='${S}' AND role='tool'`);
    assert.ok(rows.length >= 1, "活动库没有工具行");
    assert.ok(rows.some((r: any) => r.tool_name === "todo"), JSON.stringify(rows));
  });

  await check("final 跑 guard：<ACT:…> 补成 <|ACT:…|>", async () => {
    const out = await turn<any>(S, "final", { text: '<ACT:{"emotion":"neutral"}>今天还好。' });
    assert.ok(out.text.startsWith('<|ACT:{"emotion":"neutral"}|>'), out.text);
    assert.ok(out.text.includes("今天还好。"), `正文被动了：${out.text}`);
    assert.equal(out.changed, true);
  });

  await check("final 之后活动库里有她这一轮说的话", async () => {
    const rows = readDbRows(`WHERE session_id='${S}' AND role='assistant'`);
    assert.ok(rows.length >= 1, "活动库没有 assistant 行");
    assert.ok(rows.some((r: any) => r.content.includes("今天还好")), JSON.stringify(rows.slice(-2)));
  });

  await check("第二个会话与第一个互不串（各自的 ALBION_SESSION_ID / 各自的 todo）", async () => {
    const B = "albion-probe-agy-b";
    await mcp(B, { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } });
    const empty = textOf(await callTool(B, "todo", { action: "list" }));
    assert.ok(empty.includes("清单是空的"), `B 看到了 A 的清单：${empty}`);
    await callTool(B, "todo", { action: "plan", title: "B 的清单", steps: "只有一步" });
    const a = textOf(await callTool(S, "todo", { action: "list" }));
    assert.ok(a.includes("自检清单") && !a.includes("B 的清单"), a);
    assert.ok(existsSync(join(myTmp, "albion-pi-ephemeral", B, "todo.json")));
    const h = (await (await fetch(`http://127.0.0.1:${port}/health`)).json()) as any;
    assert.ok(h.sessions.length >= 2);
    for (const s of h.sessions) assert.ok(s.tools >= 18, `${s.session} 只有 ${s.tools} 件工具`);
  });

  await check("SSE 客户端拿到的是 text/event-stream 一条消息", async () => {
    const r = await mcpSse(S, { jsonrpc: "2.0", id: 7, method: "tools/list" });
    assert.ok(r.ctype.includes("text/event-stream"), r.ctype);
    assert.ok(r.text.startsWith("event: message\n"), r.text.slice(0, 40));
    assert.ok(toolNames(r.data).includes("lunar_calendar"));
  });

  await check("没这件工具时报 -32602，不 500", async () => {
    const r = await mcp(S, { jsonrpc: "2.0", id: 8, method: "tools/call", params: { name: "no_such_tool", arguments: {} } });
    assert.equal(r.error?.code, -32602);
  });

  await check("【接着说完】那一轮 loopguard 不给工具：tools/list 空、tools/call 拒", async () => {
    const B = "albion-probe-agy-b";
    const out = await turn<any>(B, "before", { text: "【接着说完】刚才那句没说完" });
    assert.equal(out.text_only, true, JSON.stringify(out).slice(0, 200));
    assert.deepEqual(out.tools, []);
    const names = toolNames(await mcp(B, { jsonrpc: "2.0", id: 11, method: "tools/list" }));
    assert.deepEqual(names, []);
    const call = await callTool(B, "lunar_calendar", {});
    assert.equal(call.result?.isError, true, JSON.stringify(call).slice(0, 200));
    // 下一轮正常开始，工具回来
    const again = await turn<any>(B, "before", { text: "在吗" });
    assert.equal(again.text_only, false);
    assert.ok(again.tools.length >= 18);
  });

  await check("扩展加载 0 错、guard/loopguard 没记 hook 失败", async () => {
    const h = (await (await fetch(`http://127.0.0.1:${port}/health`)).json()) as any;
    for (const s of h.sessions) {
      assert.deepEqual(s.errors, [], JSON.stringify(s.errors));
      assert.deepEqual(s.hook_errors, [], JSON.stringify(s.hook_errors));
    }
  });
} finally {
  host.kill("SIGTERM");
  await new Promise((r) => setTimeout(r, 300));
  host.kill("SIGKILL");
  await new Promise<void>((r) => fakeBrain.close(() => r()));
  if (!KEEP) {
    try {
      rmSync(root, { recursive: true, force: true });
    } catch {
      /* 临时目录删不掉不算失败 */
    }
  } else {
    writeFileSync(join(root, "last-run.log"), hostErr, "utf-8");
    console.log(`保留临时目录：${root}`);
  }
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过${failed.length ? "，" + failed.length + " 处失败" : ""}`);
if (failed.length) {
  appendFileSync(join(logDir, "failures.log"), JSON.stringify(failed, null, 2));
  console.log(hostErr.slice(-1500));
  process.exit(1);
}
process.exit(0);
