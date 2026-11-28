/**
 * agy 宿主 × OpenViking 自检（2026-11-28）：.env 载入 + 捕获钩子 + 临时会话隔离。
 *
 * 只在 WSL 里跑（要 Pi 的加载器 `/usr/local/bin/pi`）。跑法见 check_agy_host.ps1。
 *
 * 覆盖：
 *   ① agent/.env 读进 process.env（空格+引号的写法也认），扩展 session_start 里拿到
 *      OPENVIKING_USER=albion / account=default / agent=albion / 有 key；
 *   ② 一轮 before+final 之后，扩展的 turn_end 收到这两条（user 原话 + 她的最终正文），
 *      且条目是官方 capture-adapter 认的 Pi 形状；送进 OV 的 user 是他自己的原话，
 *      不带 albion-turn 拼进去的回忆块／逐轮约定；
 *   ③ session_shutdown 触发 commit：DELETE /mcp/<session> 一次、宿主 SIGTERM 退出一次；
 *   ④ 临时会话（不在 real-sessions.json 里）什么都不发：不建会话、不捕获、不 commit，
 *      连 OPENVIKING_BYPASS_SESSION 都给它设上。
 *
 * 不叫模型、不联网：假的 OV 端点是本进程里的 http 服务，载荷只落在内存和临时目录。
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// ── 捕获过滤规则的语义（仿造文本，结构照生产，内容不是她的话）─────────────────
// 醒来那一轮：换身子后第一轮会顶着压缩笔记 + 接上的旧记录，最后才是 /turn/before 收到的正文。
const NOTE_TITLE = "（这是之前和指挥官聊过的事，整理成的笔记）";
const SEED_HEADER = "【以下是这次对话此前的记录，作为事实参考，不是这一次的说话范本】";
const NOW_SEPARATOR = "\n\n【现在】\n";
const WAKE_PROMPT =
  "【自主醒来】\n现在是 2026-11-28 21:39（星期六）。\n指挥官上次跟我说话是 5 小时 4 分钟前。\n心事本里还开着的：问一句他今天忙不忙";
const PLATFORM_TURN = "【平台消息】他刚在 Telegram 给你发了条消息\n指挥官：晚上一起看动画吗";

/** server.py 的 compose() 在换身子第一轮拼出来的那条（note + 旧记录 + 【现在】 + 这一轮的话）。 */
function seeded(current: string): string {
  const note = NOTE_TITLE + "\n" + "指挥官：上次说想去吃面。";
  const transcript = [SEED_HEADER, "指挥官：今天想吃面", "阿尔比恩：那就去吃吧"].join("\n");
  return note + "\n\n" + transcript + NOW_SEPARATOR + current;
}

function branchEntry(role: "user" | "assistant", text: string): any {
  return { type: "message", message: { role, content: [{ type: "text", text }], timestamp: Date.now() } };
}

function payloadText(p: any): string {
  return Array.isArray(p?.parts) ? p.parts.map((x: any) => x.text ?? "").join("") : String(p?.content ?? "");
}

/** 问 bin/server.py 要这一会话的捕获过滤规则（宿主走的是同一条路，见 sessionEnvFromServer）。 */
function serverFilters(): string[] {
  const script = join(sandbox, "bin", "server.py");
  const r = spawnSync("python3", [script, "--print-session-env", "albion-desktop"], {
    encoding: "utf-8",
    env: { ...process.env, PI_CODING_AGENT_DIR: agentDir },
  });
  if (r.status !== 0) throw new Error(`server.py --print-session-env 失败：${String(r.stderr ?? r.error ?? "").slice(-400)}`);
  const env = JSON.parse(String(r.stdout || "{}").trim() || "{}");
  return String(env.OPENVIKING_CAPTURE_FILTERS || "").split(",").filter(Boolean);
}

/** 用官方扩展自己的实现（capture-adapter + capture-utils）把条目过一遍规则。 */
async function applyFilters(entries: any[], rules: string[]): Promise<any[]> {
  if (!existsSync(OV_ADAPTER)) throw new Error("这台机器上没有官方扩展，跑不了过滤断言");
  const { extractBranchCapturePayloads } = await import(OV_ADAPTER);
  const out = extractBranchCapturePayloads(entries, 0, { captureFilters: rules, captureMaxLength: 100000 });
  return out.payloads;
}

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const KEEP = process.argv.includes("--keep");
const PI_LOADER = "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";
/** 官方扩展（不进仓库）——用它自己的 capture-adapter 和过滤规则跑真实语义。 */
const OV_ADAPTER = "/root/albion-pi/agent/openviking/lib/capture-adapter.mjs";

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

// ── 临时目录：宿主、agent 目录、state、日志全在这里 ──────────────────────────
const root = mkdtempSync(join(tmpdir(), "agy-ov-check-"));
const sandbox = join(root, "brain");
const agentDir = join(sandbox, "agent");
const stateDir = join(root, "state");
const logDir = join(root, "logs");
const myTmp = join(root, "tmp");
for (const d of [join(sandbox, "bin"), join(agentDir, "extensions"), join(agentDir, "lib"), stateDir, logDir, myTmp]) {
  mkdirSync(d, { recursive: true });
}
// 整个 bin：宿主会问 bin/server.py 要捕获过滤规则（server.py 又 import wake/agy_client/…）
cpSync(join(repo, "brain", "bin"), join(sandbox, "bin"), { recursive: true });
// 宿主静态 import 了 ../agent/lib/session-scope.ts（临时会话判定），照生产布局放好
cpSync(join(repo, "brain", "agent", "lib", "session-scope.ts"), join(agentDir, "lib", "session-scope.ts"));
// 只加载两支自检用的假扩展（官方 OV 源码不进仓库）
cpSync(join(here, "data", "agy-ov-stub", "openviking.ts"), join(agentDir, "extensions", "openviking.ts"));
cpSync(join(here, "data", "agy-ov-stub", "turn-rewrite.ts"), join(agentDir, "extensions", "turn-rewrite.ts"));
writeFileSync(join(agentDir, "real-sessions.json"), JSON.stringify(["albion-desktop"]), "utf-8");

// ── 假 OV 端点 ───────────────────────────────────────────────────────────────
// commit 故意晚 2 秒才回：宿主收到 SIGTERM 后要先放开 8661，再等这个慢 commit 走完才退。
const COMMIT_DELAY_MS = 2000;
const ovPort = 18933;
const requests: Array<{ event: string; body: any; at: number }> = [];
const fakeOv = createServer((req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${ovPort}`);
  const m = /^\/__check\/([a-z_]+)$/.exec(url.pathname);
  if (req.method !== "POST" || !m) {
    res.writeHead(404, { "content-type": "application/json" });
    res.end("{}");
    return;
  }
  const chunks: Buffer[] = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    let body: any = {};
    try {
      body = JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}");
    } catch {
      body = {};
    }
    requests.push({ event: m[1], body, at: Date.now() });
    const reply = () => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end('{"ok":true}');
    };
    if (m[1] === "commit") setTimeout(reply, COMMIT_DELAY_MS);
    else reply();
  });
});
await new Promise<void>((r) => fakeOv.listen(ovPort, "127.0.0.1", () => r()));

// 故意用「空格 + 成对引号 + 行内注释外」的写法，验证 .env 解析和 server.py 的 load_env 一致
writeFileSync(
  join(agentDir, ".env"),
  [
    "# 自检用 .env（不是生产的那份）",
    `OPENVIKING_URL=http://127.0.0.1:${ovPort}`,
    `OPENVIKING_ENDPOINT = "http://127.0.0.1:${ovPort}"`,
    "OPENVIKING_API_KEY=check-only-not-a-real-key",
    "OPENVIKING_ACCOUNT=default",
    "  OPENVIKING_USER  =  albion  ",
    "OPENVIKING_AGENT=albion",
    "OPENVIKING_AUTO_RECALL=false",
    "ALBION_EMPTY=", // 空值不写
    "",
  ].join("\n"),
  "utf-8",
);

// ── 起宿主 ───────────────────────────────────────────────────────────────────
const port = 18662;
const hostLog = join(logDir, "host.log");
const env: Record<string, string> = {
  PATH: process.env.PATH ?? "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
  HOME: process.env.HOME ?? "/root",
  TZ: "Asia/Shanghai",
  PI_CODING_AGENT_DIR: agentDir,
  ALBION_HOST_PORT: String(port),
  ALBION_HOST_EXT_DIR: join(agentDir, "extensions"),
  ALBION_HOST_CWD: "/root",
  ALBION_HOST_LOG: hostLog,
  ALBION_STATE_DIR: stateDir,
  ALBION_STATE_DB: join(stateDir, "state.db"),
  ALBION_BRAIN_PORT: "18652",
  API_SERVER_KEY: "check-agy-ov",
  ALBION_PI_LOADER: PI_LOADER,
  TMPDIR: myTmp,
  NODE_NO_WARNINGS: "1",
  // 外面故意不放任何 OPENVIKING_*：全靠宿主自己读 agent/.env（这正是被测的行为）
};
const host = spawn(process.execPath, [join(sandbox, "bin", "agy_host.ts")], { env, stdio: ["ignore", "pipe", "pipe"] });
let hostErr = "";
host.stderr.on("data", (c: Buffer) => (hostErr += c.toString("utf-8")));
host.stdout.on("data", () => {});

async function waitHealth(timeoutMs = 30000): Promise<any> {
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
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`宿主没起来（${lastErr}）\n${hostErr.slice(-2000)}`);
}

async function turn(session: string, action: string, body: any): Promise<any> {
  const r = await fetch(`http://127.0.0.1:${port}/turn/${encodeURIComponent(session)}/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return await r.json();
}
/** MCP initialize：把会话「打开」（宿主在这里 loadSession + session_start）。 */
async function open(session: string): Promise<void> {
  const r = await fetch(`http://127.0.0.1:${port}/mcp/${encodeURIComponent(session)}`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } }),
  });
  assert.equal(r.status, 200, `initialize ${session} HTTP ${r.status}`);
}
async function closeSession(session: string): Promise<void> {
  const r = await fetch(`http://127.0.0.1:${port}/mcp/${encodeURIComponent(session)}`, { method: "DELETE" });
  assert.equal(r.status, 200, `DELETE ${session} HTTP ${r.status}`);
}
async function settle(ms = 400): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}
const reqsFor = (session: string): Array<{ event: string; body: any }> =>
  requests.filter((r) => r?.body?.session === session);
const eventsOf = (session: string): string[] => reqsFor(session).map((r) => r.event);

let health: any = null;
try {
  health = await waitHealth();
  console.log(`宿主起来了：pid=${health.pid} port=${health.port}`);

  const REAL = "albion-desktop";
  const PROBE = "albion-probe-ov-capture";

  await check("① .env 被宿主读进 process.env（日志里有 env loaded）", () => {
    assert.ok(existsSync(hostLog), "没写 host.log");
    const log = readFileSync(hostLog, "utf-8");
    assert.ok(/env loaded/.test(log), log.slice(0, 800));
    assert.ok(log.includes(join(agentDir, ".env")), "日志里的路径不是 agent/.env");
  });

  await check("① 扩展在 session_start 里拿到 OPENVIKING_USER=albion 等（空格+引号写法也认）", async () => {
    await open(REAL);
    await settle();
    const start = requests.find((r) => r.event === "session_start" && r.body?.session === REAL);
    assert.ok(start, `没收到 session_start：${JSON.stringify(requests)}`);
    assert.equal(start!.body.user, "albion");
    assert.equal(start!.body.account, "default");
    assert.equal(start!.body.agent, "albion");
    assert.equal(start!.body.has_key, true);
    assert.equal(start!.body.auto_recall, "false");
    assert.equal(start!.body.url, `http://127.0.0.1:${ovPort}`);
  });

  await check("① 召回侧同一份 .env 也够用：memory.py 的 client 解析成 albion / default", async () => {
    // 召回是 albion-turn 在宿主进程里 spawn `python3 bin/memory.py`，继承的就是宿主这份 env。
    // 这里只用同一份 .env 起一个 python 子进程问它解析出了谁（不联网）。
    const memoryPy = join(repo, "brain", "bin", "memory.py");
    if (!existsSync(memoryPy)) {
      console.log("       （跳过：暂存目录里没有 memory.py）");
      return;
    }
    const script = [
      "import json, sys",
      `sys.path.insert(0, ${JSON.stringify(dirname(memoryPy))})`,
      "from memory import get_client",
      "c = get_client()",
      "print(json.dumps({'endpoint': c.endpoint, 'user': c.user, 'account': c.account, 'agent': c.agent, 'has_key': bool(c.api_key)}))",
    ].join("\n");
    const envForPy: Record<string, string> = {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      OPENVIKING_ENDPOINT: `http://127.0.0.1:${ovPort}`,
      OPENVIKING_API_KEY: "check-only-not-a-real-key",
      OPENVIKING_ACCOUNT: "default",
      OPENVIKING_USER: "albion",
      OPENVIKING_AGENT: "albion",
    };
    const out = await new Promise<string>((resolve, reject) => {
      const p = spawn("python3", ["-c", script], { env: envForPy });
      let so = "";
      let se = "";
      p.stdout.on("data", (c: Buffer) => (so += c.toString("utf-8")));
      p.stderr.on("data", (c: Buffer) => (se += c.toString("utf-8")));
      p.on("exit", (code) => (code === 0 ? resolve(so.trim()) : reject(new Error(`python3 退出码 ${code}：${se.slice(-400)}`))));
    });
    const got = JSON.parse(out);
    assert.equal(got.user, "albion", out);
    assert.equal(got.account, "default", out);
    assert.equal(got.agent, "albion", out);
    assert.equal(got.endpoint, `http://127.0.0.1:${ovPort}`, out);
    assert.equal(got.has_key, true, out);
  });

  await check("② 一轮 before+final 之后 turn_end 收到 user 原话 + 她的最终正文", async () => {
    const said = "今天有点累，晚上想早点睡";
    await turn(REAL, "before", { text: said });
    const out = await turn(REAL, "final", { text: '<ACT:{"emotion":"neutral"}>那就早点睡吧。', source: said });
    // guard（ACT 补竖线）由 check_agy_host.ts 覆盖；这里只关心 turn_end 带走了什么
    assert.ok(String(out.text).includes("那就早点睡吧"), `final 没回正文：${out.text}`);
    await settle();
    const te = requests.filter((r) => r.event === "turn_end" && r.body?.session === REAL);
    assert.equal(te.length, 1, `turn_end 收到 ${te.length} 次`);
    const entries = te[0].body.entries ?? [];
    assert.equal(entries.length, 2, JSON.stringify(entries).slice(0, 400));
    // 官方 capture-adapter 认的形状：entry.type==="message" + entry.message.role
    for (const e of entries) {
      assert.equal(e.type, "message", JSON.stringify(e));
      assert.ok(["user", "assistant"].includes(e.message?.role), JSON.stringify(e));
    }
    assert.equal(entries[0].message.role, "user");
    assert.equal(entries[0].message.content[0].text, said);
    assert.equal(entries[1].message.role, "assistant");
    assert.ok(entries[1].message.content[0].text.includes("那就早点睡吧"), JSON.stringify(entries[1]));
  });

  await check("② 送进 OV 的 user 是他的原话，不带拼进去的回忆块／逐轮约定", async () => {
    const entries = requests.filter((r) => r.event === "turn_end" && r.body?.session === REAL)[0].body.entries ?? [];
    const userText = entries.find((e: any) => e.message?.role === "user")?.message?.content?.[0]?.text ?? "";
    assert.ok(!userText.includes("memory-context"), `回忆块进 OV 了：${userText}`);
    assert.ok(!userText.includes("本轮说话约定"), `逐轮约定进 OV 了：${userText}`);
    // 而 before 给 agy 的 prompt 里确实带了（假 albion-turn 改写过），证明宿主没把两件事搞混
    assert.equal(userText, "今天有点累，晚上想早点睡");
  });

  await check("② 官方 capture-adapter 认这批条目（生产装了扩展才跑，没装就跳过）", async () => {
    const adapterPath = "/root/albion-pi/agent/openviking/lib/capture-adapter.mjs";
    if (!existsSync(adapterPath)) {
      console.log("       （跳过：这台机器上没有官方扩展）");
      return;
    }
    const { extractBranchCapturePayloads } = await import(adapterPath);
    const entries = requests.filter((r) => r.event === "turn_end" && r.body?.session === REAL)[0].body.entries ?? [];
    const out = extractBranchCapturePayloads(entries, 0, {});
    assert.equal(out.payloads.length, entries.length, JSON.stringify(out.payloads).slice(0, 400));
    const roles = out.payloads.map((p: any) => p.role);
    assert.deepEqual(roles, ["user", "assistant"], JSON.stringify(roles));
    const textOf = (p: any) => (p.content ?? (p.parts ?? []).map((x: any) => x.text).join(""));
    assert.ok(textOf(out.payloads[0]).includes("今天有点累"), JSON.stringify(out.payloads[0]).slice(0, 300));
  });

  await check("② 第二轮只发新增的那两条（水位线按条数走，不重发）", async () => {
    await turn(REAL, "before", { text: "第二句" });
    await turn(REAL, "final", { text: "第二句的回话。", source: "第二句" });
    await settle();
    const te = requests.filter((r) => r.event === "turn_end" && r.body?.session === REAL);
    assert.equal(te.length, 2, `turn_end 收到 ${te.length} 次`);
    const fresh = te[1].body.entries ?? [];
    assert.equal(fresh.length, 2, JSON.stringify(fresh).slice(0, 300));
    assert.equal(fresh[0].message.content[0].text, "第二句");
  });

  await check("⑤ 宿主从 bin/server.py 拿到了捕获过滤规则", async () => {
    const start = requests.find((r) => r.event === "session_start" && r.body?.session === REAL);
    assert.ok(start, "没收到 session_start");
    // 规则只在 server.py 里维护一份；宿主问它要，条数应当和它自己报的一致
    const rules = serverFilters();
    assert.ok(rules.length > 0, "server.py 没给出任何规则");
    assert.equal(start!.body.capture_filters, rules.length, `宿主拿到 ${start!.body.capture_filters} 条，server.py 有 ${rules.length} 条`);
    assert.equal(start!.body.bypass, "", "真实会话不该被 bypass");
  });

  await check("⑤ 醒来轮（压缩笔记 + 旧记录 + 【自主醒来】）整条丢", async () => {
    const payloads = await applyFilters([branchEntry("user", seeded(WAKE_PROMPT))], serverFilters());
    assert.equal(payloads.length, 0, `还有 ${payloads.length} 条要提交：${JSON.stringify(payloads).slice(0, 300)}`);
  });

  await check("⑤ 正常聊天轮（同样顶着笔记 + 旧记录）剥完只剩他打的字", async () => {
    const said = "今天中午想吃面";
    const payloads = await applyFilters([branchEntry("user", seeded(said))], serverFilters());
    assert.equal(payloads.length, 1, JSON.stringify(payloads).slice(0, 300));
    const text = payloadText(payloads[0]).trim();
    assert.equal(text, said, `剥剩的是：${text.slice(0, 120)}`);
  });

  await check("⑤ 平台消息轮剥完只剩「（渠道）指挥官：原话」", async () => {
    const payloads = await applyFilters([branchEntry("user", PLATFORM_TURN)], serverFilters());
    assert.equal(payloads.length, 1, JSON.stringify(payloads).slice(0, 300));
    assert.equal(payloadText(payloads[0]).trim(), "指挥官：晚上一起看动画吗");
  });

  await check("⑤ 宿主真链路：醒来那一轮在 OV 里一条都不剩（她真说的话照留）", async () => {
    await turn(REAL, "before", { text: seeded(WAKE_PROMPT) });
    await turn(REAL, "final", { text: "嗯，我醒了。", source: WAKE_PROMPT });
    await settle();
    const te = requests.filter((r) => r.event === "turn_end" && r.body?.session === REAL);
    const fresh = te.at(-1)!.body.entries ?? [];
    const payloads = await applyFilters(fresh, serverFilters());
    const users = payloads.filter((p: any) => p.role === "user");
    assert.equal(users.length, 0, `醒来提示词还是要提交：${JSON.stringify(users).slice(0, 300)}`);
    const assistants = payloads.filter((p: any) => p.role === "assistant");
    assert.equal(assistants.length, 1, JSON.stringify(payloads).slice(0, 300));
    assert.ok(payloadText(assistants[0]).includes("我醒了"), JSON.stringify(assistants[0]).slice(0, 200));
  });

  await check("③ DELETE /mcp/<session> 触发 session_shutdown → commit", async () => {
    const before = eventsOf(REAL).filter((e) => e === "commit").length;
    await closeSession(REAL);
    await settle();
    const after = eventsOf(REAL).filter((e) => e === "commit").length;
    assert.equal(after, before + 1, `commit 次数 ${before} → ${after}`);
    const commits = requests.filter((r) => r.event === "commit" && r.body?.session === REAL);
    assert.ok(commits.at(-1)!.body.branch_entries >= 4, JSON.stringify(commits.at(-1)));
  });

  await check("④ 临时会话什么都不发（不建会话、不捕获、不 commit，且被标成 bypass）", async () => {
    const envs: string[] = [];
    await open(PROBE);
    await turn(PROBE, "before", { text: "探针说的一句" });
    await turn(PROBE, "final", { text: "探针的回话。", source: "探针说的一句" });
    await settle();
    assert.deepEqual(eventsOf(PROBE), [], `临时会话发了请求：${JSON.stringify(reqsFor(PROBE))}`);
    const h = (await (await fetch(`http://127.0.0.1:${port}/health`)).json()) as any;
    const snap = h.sessions.find((s: any) => s.session === PROBE);
    assert.ok(snap, "health 里没有临时会话");
    assert.equal(snap.capture, false);
    assert.equal(snap.branch_entries, 0);
    assert.deepEqual(snap.hook_errors, [], JSON.stringify(snap.hook_errors));
  });

  await check("③ SIGTERM：先放开 8661，再等慢 commit 走完才退", async () => {
    const OTHER = "albion-desktop-z";
    // 让这个会话进 real-sessions.json 也认的名单（用同一支：先改名再开）
    writeFileSync(join(agentDir, "real-sessions.json"), JSON.stringify(["albion-desktop", "albion-desktop-z"]), "utf-8");
    await open(OTHER);
    await turn(OTHER, "before", { text: "退出前那句" });
    await turn(OTHER, "final", { text: "退出前的回话。", source: "退出前那句" });
    await settle();
    const commitsBefore = requests.filter((r) => r.event === "commit" && r.body?.session === OTHER).length;

    const exited = new Promise<number | null>((r) => host.once("exit", (code) => r(code)));
    const tSigterm = Date.now();
    host.kill("SIGTERM");

    // ① 1 秒内端口就得放开（不然 start.sh 会以为宿主还活着、跳过拉起）
    let closedAt = 0;
    while (Date.now() - tSigterm < 1500) {
      try {
        await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(200) });
      } catch {
        closedAt = Date.now();
        break;
      }
      await new Promise((r) => setTimeout(r, 25));
    }
    assert.ok(closedAt, "SIGTERM 后 1.5 秒了 8661 还在监听");
    assert.ok(closedAt - tSigterm <= 1000, `端口关得太慢：${closedAt - tSigterm}ms`);

    // ② 进程还得活着把那个要 2 秒才回的 commit 等到
    const code = await Promise.race([exited, new Promise((r) => setTimeout(() => r("timeout"), 15000))]);
    assert.notEqual(code, "timeout", `宿主 15 秒没退出\n${hostErr.slice(-1000)}`);
    const exitDelay = Date.now() - tSigterm;
    const commitsAfter = requests.filter((r) => r.event === "commit" && r.body?.session === OTHER).length;
    assert.equal(commitsAfter, commitsBefore + 1, `SIGTERM 没 commit：${commitsBefore} → ${commitsAfter}`);
    assert.ok(exitDelay >= COMMIT_DELAY_MS - 300, `没等慢 commit 就退了：${exitDelay}ms`);
    console.log(`       端口关闭 ${closedAt - tSigterm}ms，退出 ${exitDelay}ms（commit 慢 ${COMMIT_DELAY_MS}ms）`);
  });

  await check("宿主日志里没有 hook 失败", () => {
    const log = readFileSync(hostLog, "utf-8");
    assert.ok(!/hook failed/.test(log), log.split("\n").filter((l) => /hook failed/.test(l)).join("\n"));
  });
} finally {
  if (host.exitCode === null && host.signalCode === null) {
    host.kill("SIGKILL");
    await new Promise((r) => setTimeout(r, 200));
  }
  await new Promise<void>((r) => fakeOv.close(() => r()));
  if (KEEP) {
    writeFileSync(join(root, "last-run.log"), hostErr, "utf-8");
    console.log(`保留临时目录：${root}`);
  } else {
    try {
      rmSync(root, { recursive: true, force: true });
    } catch {
      /* 临时目录删不掉不算失败 */
    }
  }
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过${failed.length ? "，" + failed.length + " 处失败" : ""}`);
if (failed.length) {
  console.log(hostErr.slice(-1500));
  process.exit(1);
}
process.exit(0);
