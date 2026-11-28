#!/usr/bin/env node
/**
 * agy_host —— 阿尔比恩换 agy 内核后的「扩展宿主」（2026-11-27，B 块）。
 *
 * 为什么要有：换 agy 以后模型的循环在 Windows 的 agy 里跑，Pi 不再是那个进程；
 * 但她的 22 个扩展（18 件工具 + 每轮回忆/说说约定/守卫/活动库/复读断路/OpenViking）不能丢。
 * 这里用 Pi 自己的加载器（core/extensions/loader.js 的 loadExtensions）把这些扩展原样加载，
 * 喂一个模拟的 ExtensionAPI 运行时和 ctx，然后按任务书 B 节对外开三个面：
 *
 *   /mcp/<session>               Streamable HTTP MCP（initialize / tools/list / tools/call）
 *   /turn/<session>/before       {"text","images"} → {"prompt","system_append"}
 *   /turn/<session>/event        {"type":"tool_call|tool_result|message_end|agent_end",…}
 *   /turn/<session>/final        {"text"} → {"text"}       message_end 的 guard 整理
 *   /health
 *
 * 设计要点（细节见 docs 或交回报告）：
 *  · 会话隔离：一个宿主进程里每个会话各自**重新加载一遍**扩展（jiti moduleCache:false，
 *    模块级常量如 SESSION_ID 各自一份），执行扩展代码时串行（一把全局锁）+ 换 env 快照 +
 *    globalThis 账本进出各存一份。理由：扩展在模块顶层/调用时直接读 process.env，
 *    只有「按会话重新求值 + 同一时刻只有一个人在跑」才能保证读到的永远是自己的会话。
 *  · 不加载 albion-relay（模型转发）：agy 自己连 Google，没有 Pi 的 provider 请求要转发；
 *    而且它会占 8798 端口、拖住进程退出。可用 ALBION_HOST_SKIP 调整。
 *  · Pi 的压缩钩子（session_before_compact / session_compact）在 agy 下不发事件：空跑。
 *  · guard 的「整句重说」已于 2026-11-28 删除（用户定：Gemini 类模型几乎不触发），agy 与 Pi 下都只做
 *    确定性整理（ACT 补竖线、语料替换等）；payload.model 仍要合成，guard 用它判「这一轮是不是 DS」。
 *
 * 跑：node /root/albion-pi/bin/agy_host.ts         （只听 127.0.0.1:8661，ALBION_HOST_PORT 可改）
 * 自检：tests/pi-brain/check_agy_host.mjs（在 WSL 里跑，临时 state 目录，不碰生产）
 */
import { createServer } from "node:http";
import { appendFileSync, existsSync, readdirSync, realpathSync } from "node:fs";
import { basename, join } from "node:path";
import { pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";

const HOST = process.env.ALBION_HOST_BIND || "127.0.0.1";
const PORT = Number(process.env.ALBION_HOST_PORT || "8661");
const AGENT_DIR = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
const EXT_DIR = process.env.ALBION_HOST_EXT_DIR || join(AGENT_DIR, "extensions");
const CWD = process.env.ALBION_HOST_CWD || "/root";
/** 不加载的扩展（文件名）：模型转发在 agy 下不需要，而且会占 8798、拖住进程退出。 */
const SKIP = new Set(
  (process.env.ALBION_HOST_SKIP ?? "albion-relay.ts")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),
);
/** 这一轮「写的人」是谁：agy 的模型名。guard 用它判断要不要按 DeepSeek 的口癖挑稿子。 */
const MODEL = process.env.ALBION_HOST_MODEL || process.env.ALBION_AGY_MODEL || "Gemini 3.8 Flash (Medium)";
const MAX_BODY = 48 * 1024 * 1024; // 图片走 base64，给够
const SESSION_RE = /^[A-Za-z0-9._-]{1,120}$/;
/** 会话级环境变量：执行扩展代码前后按会话换一份，退出时还原。 */
const SESSION_ENV_KEYS = ["ALBION_SESSION_ID"];

const LOG_FILE = (process.env.ALBION_HOST_LOG || "").trim();
function log(msg: string, extra?: unknown): void {
  const line = `[agy_host] ${new Date().toISOString()} ${msg}${extra === undefined ? "" : " " + safeJson(extra)}`;
  process.stderr.write(line + "\n");
  if (!LOG_FILE) return;
  try {
    // 只在给了路径时落盘：自检写临时文件，生产由 start.sh 指到 logs/
    appendFileSync(LOG_FILE, line + "\n");
  } catch {
    /* 日志写不了不影响服务 */
  }
}
function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

// ── Pi 的加载器 ──────────────────────────────────────────────────────────────
// 跟着 /usr/local/bin/pi 实际指向的那份 Pi 走（换版本、回滚都只改那个链接），
// 和 bin/check_extensions.ts 用的是同一条规则；ALBION_PI_LOADER 可显式指定。
function loaderPath(): string {
  const explicit = (process.env.ALBION_PI_LOADER || "").trim();
  if (explicit) return explicit;
  try {
    const link = realpathSync("/usr/local/bin/pi");
    const guess = join(link, "../../core/extensions/loader.js");
    if (existsSync(guess)) return guess;
  } catch {
    /* 没装在这个位置就退到全局 node_modules */
  }
  return "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";
}

type PiLoader = {
  loadExtensions: (paths: string[], cwd: string, bus?: unknown, runtime?: unknown) => Promise<any>;
  createExtensionRuntime: () => any;
};

let piLoaderPromise: Promise<PiLoader> | null = null;
function piLoader(): Promise<PiLoader> {
  if (!piLoaderPromise) {
    const p = loaderPath();
    piLoaderPromise = import(pathToFileURL(p).href).then((m) => ({ loadExtensions: m.loadExtensions, createExtensionRuntime: m.createExtensionRuntime }));
  }
  return piLoaderPromise;
}

/** 极简事件总线（pi.events.emit/on 用；扩展里目前没人用，给将来留个口）。 */
function makeBus() {
  const listeners = new Map<string, Array<(data: unknown) => void>>();
  return {
    emit(channel: string, data: unknown) {
      for (const fn of listeners.get(channel) ?? []) {
        try {
          fn(data);
        } catch {
          /* 订阅者自己的错不影响别人 */
        }
      }
    },
    on(channel: string, handler: (data: unknown) => void) {
      const list = listeners.get(channel) ?? [];
      list.push(handler);
      listeners.set(channel, list);
      return () => {
        const now = listeners.get(channel) ?? [];
        const i = now.indexOf(handler);
        if (i >= 0) now.splice(i, 1);
      };
    },
  };
}

// ── 会话 ─────────────────────────────────────────────────────────────────────
type ToolInfo = { name: string; description: string; parameters: unknown; exposure?: string; execute: (...a: any[]) => any };

class Session {
  id: string;
  extensions: any[] = [];
  errors: Array<{ path: string; error: string }> = [];
  runtime: any = null;
  loadedAt = 0;
  loading: Promise<void> | null = null;
  hookErrors: Array<{ event: string; path: string; error: string }> = [];
  skillPaths: string[] = [];
  lastSource = "";
  lastProviderPayload: any = null;
  shellSyncedAt = 0;
  /** 本轮的临时状态：before 开头建、final 结束清。 */
  turn: {
    active: boolean;
    text: string;
    images: any[];
    messages: any[];
    systemSections: Record<string, string>;
    toolCalls: Array<{ name: string; args: Record<string, unknown> }>;
    textOnly: boolean;
  } = { active: false, text: "", images: [], messages: [], systemSections: {}, toolCalls: [], textOnly: false };

  constructor(id: string) {
    this.id = id;
  }
}

const sessions = new Map<string, Session>();

// ── 串行锁 + 会话环境 ─────────────────────────────────────────────────────────
// 扩展按进程读 env（模块顶层常量 + 调用时再读），一个进程里要同时招待几个会话，
// 就只能「同一时刻只有一个人在跑」：全局一把锁，进出各换一次 env 和 globalThis 账本。
let tail: Promise<unknown> = Promise.resolve();
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const run = tail.then(fn, fn);
  tail = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

type EnvSnapshot = Map<string, string | undefined>;
function snapshotEnv(): EnvSnapshot {
  const m = new Map<string, string | undefined>();
  for (const k of SESSION_ENV_KEYS) m.set(k, process.env[k]);
  return m;
}
function restoreEnv(snap: EnvSnapshot): void {
  for (const [k, v] of snap) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}
function applySessionEnv(s: Session): void {
  process.env.ALBION_SESSION_ID = s.id;
}

async function inSession<T>(s: Session, fn: () => Promise<T>): Promise<T> {
  return serial(async () => {
    const env = snapshotEnv();
    const g = globalThis as any;
    const ledger = g.__albionLedger;
    const facts = g.__albionTurnFacts;
    const mark = g.__OPENVIKING_PI_EXTENSION__;
    applySessionEnv(s);
    try {
      return await fn();
    } finally {
      g.__albionLedger = ledger;
      g.__albionTurnFacts = facts;
      g.__OPENVIKING_PI_EXTENSION__ = mark;
      restoreEnv(env);
    }
  });
}

// ── ctx 模拟 ─────────────────────────────────────────────────────────────────
function noopUi() {
  const noop = () => {};
  return {
    notify: noop,
    warn: noop,
    error: noop,
    info: noop,
    log: noop,
    confirm: async () => false,
    input: async () => undefined,
    select: async () => undefined,
    multiselect: async () => [],
    spinner: () => ({ start: noop, stop: noop, update: noop }),
    setStatus: noop,
    setWidget: noop,
    setTitle: noop,
    requestRender: noop,
    onKey: () => () => {},
  };
}

function makeCtx(s: Session, signal?: AbortSignal): any {
  const ctx: any = {
    ui: noopUi(),
    mode: "rpc",
    hasUI: false,
    cwd: CWD,
    sessionManager: {
      getSessionId: () => s.id,
      getSessionName: () => s.id,
      getBranch: () => [],
      getEntries: () => [],
      getSessionPath: () => undefined,
    },
    modelRegistry: {
      getModel: () => undefined,
      findModel: () => undefined,
      list: () => [],
      getAvailableModels: () => [],
    },
    // 扩展里唯一用到的是 ctx.model.input（albion-look 判断主模型收不收图）：
    // agy 走的 Gemini 收图，所以两样都给。
    model: { id: MODEL, name: MODEL, provider: "agy", api: "agy", input: ["text", "image"] },
    scopedModels: [],
    thinkingLevel: "off",
    isIdle: () => !s.turn.active,
    isProjectTrusted: () => true,
    signal,
    abort: () => {},
    hasPendingMessages: () => false,
    shutdown: () => {},
    getContextUsage: () => undefined,
    compact: () => {},
    getSystemPrompt: () => renderSections(s.turn.systemSections),
  };
  // 工具内部互相调用（Pi 的 executeTool）：暂时没有扩展用，给了也不越权。
  ctx.executeTool = async (name: string, args: unknown) => ({ isError: true, toolName: name, args, reason: "agy_host 里不能嵌套调用工具" });
  ctx.tools = [];
  return ctx;
}

// ── 系统提示的扩展区块 ────────────────────────────────────────────────────────
/**
 * Pi 的 system prompt 由「preamble + 各 section」拼成，扩展只往里塞 section
 * （albion-turn 的常驻技能、albion-shell-tools 的壳插件规则）。
 * agy 下 preamble 由 server.py 用她的 SYSTEM.md 给桥，这边只回扩展区块：
 * 按 Pi 的包裹格式 `<name>\n内容\n</name>` 拼成一段 system_append。
 */
function renderSections(sections: Record<string, string>): string {
  const parts: string[] = [];
  for (const [name, body] of Object.entries(sections || {})) {
    if (!body || !body.trim()) continue;
    parts.push(`<${name}>\n${body}\n</${name}>`);
  }
  return parts.join("\n\n");
}

/** Pi 的 NormalizedBuildSystemPromptOptions 里扩展真正会碰的那几个字段。 */
function makePromptOptions(): any {
  return {
    customPrompt: undefined,
    forceSystemPrompt: undefined,
    selectedTools: [] as string[],
    hiddenTools: [] as string[],
    toolSnippets: {},
    toolGuidelines: {},
    promptGuidelines: [] as string[],
    appendSystemPrompt: "",
    sections: {} as Record<string, string>,
    cwd: CWD,
    contextFiles: [],
    skills: [],
  };
}

// ── 加载一个会话的扩展 ────────────────────────────────────────────────────────
function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
  return Promise.race([
    p,
    new Promise<T>((_, reject) => {
      const t = setTimeout(() => reject(new Error(`${what} 超过 ${ms}ms`)), ms);
      // 超时只是不再等它：定时器自己退场，别拖住进程
      if (typeof (t as any).unref === "function") (t as any).unref();
      p.finally(() => clearTimeout(t));
    }),
  ]);
}

function extensionFiles(): string[] {
  let names: string[] = [];
  try {
    names = readdirSync(EXT_DIR).filter((f) => f.endsWith(".ts") || f.endsWith(".js")).sort();
  } catch (e) {
    log("extensions dir unreadable", { dir: EXT_DIR, error: String((e as Error)?.message ?? e) });
    return [];
  }
  return names.filter((n) => !SKIP.has(n)).map((n) => join(EXT_DIR, n));
}

/** 当前会话能用的工具（后注册的覆盖先注册的，和 Pi 的 refreshTools 一致；hidden 不对外）。 */
function allTools(s: Session): ToolInfo[] {
  const map = new Map<string, ToolInfo>();
  for (const ext of s.extensions) {
    for (const [, entry] of ext.tools ?? new Map()) {
      const def = (entry as any)?.definition;
      if (!def?.name) continue;
      map.set(def.name, def as ToolInfo);
    }
  }
  return [...map.values()];
}
function visibleTools(s: Session): ToolInfo[] {
  return allTools(s).filter((t) => t.exposure !== "hidden");
}

async function ensureSession(id: string): Promise<Session> {
  if (!SESSION_RE.test(id)) throw new HttpError(400, `会话名不合法：${JSON.stringify(id)}`);
  let s = sessions.get(id);
  if (!s) {
    s = new Session(id);
    sessions.set(id, s);
  }
  if (!s.loadedAt) {
    if (!s.loading) s.loading = loadSession(s).finally(() => (s.loading = null));
    await s.loading;
  }
  return s;
}

async function loadSession(s: Session): Promise<void> {
  const t0 = Date.now();
  const files = extensionFiles();
  await inSession(s, async () => {
    const { loadExtensions, createExtensionRuntime } = await piLoader();
    const runtime = createExtensionRuntime();
    // 模拟 ExtensionAPI 的 action 面：登记类 registerTool/on 由 Pi 自己的 loader 处理，
    // 这里只补运行时动作。agi 下没有会话队列，sendMessage 记下来当诊断。
    runtime.refreshTools = () => {};
    runtime.getAllTools = () => allTools(s).map((t) => ({ name: t.name, description: t.description }));
    runtime.getActiveTools = () => visibleTools(s).map((t) => t.name);
    runtime.setActiveTools = () => {};
    runtime.getSettings = () => ({});
    runtime.getCommands = () => [];
    runtime.setModel = async () => {};
    runtime.getThinkingLevel = () => "off";
    runtime.setThinkingLevel = () => {};
    runtime.sendMessage = () => log("extension sendMessage ignored (agy kernel)", { session: s.id });
    runtime.sendUserMessage = () => log("extension sendUserMessage ignored (agy kernel)", { session: s.id });
    runtime.appendEntry = () => {};
    runtime.setSessionName = () => {};
    runtime.getSessionName = () => s.id;
    runtime.setLabel = () => {};
    runtime.createContext = () => makeCtx(s);

    const res = await loadExtensions(files, CWD, makeBus(), runtime);
    s.extensions = res.extensions ?? [];
    s.errors = (res.errors ?? []).map((e: any) => ({ path: basename(String(e.path)), error: String(e.error).split("\n")[0] }));
    s.runtime = runtime;
    s.loadedAt = Date.now();
    // 技能目录（openviking 的 resources_discover）：agy 下 system prompt 由 server.py 拼，
    // 这里只把路径交回去，供 C 块决定要不要带进 prompt。
    for (const { handler } of handlersFor(s, "resources_discover")) {
      try {
        const r = await handler({ type: "resources_discover", cwd: CWD, reason: "startup" }, makeCtx(s));
        for (const p of r?.skillPaths ?? []) if (typeof p === "string") s.skillPaths.push(p);
      } catch (e) {
        recordHookError(s, "resources_discover", "<resources>", e);
      }
    }
    for (const { handler, ext } of handlersFor(s, "session_start")) {
      try {
        // 官方 OpenViking 扩展在这个钩子里连服务端；宿主不能因为它的网络慢就起不来
        await withTimeout(handler({ type: "session_start", reason: "startup" }, makeCtx(s)), 15000, "session_start");
      } catch (e) {
        recordHookError(s, "session_start", ext.path, e);
      }
    }
  });
  log("session loaded", { session: s.id, extensions: s.extensions.length, errors: s.errors.length, tools: visibleTools(s).length, ms: Date.now() - t0 });
}

// ── 钩子派发（对齐 Pi 的 ExtensionRunner 语义）─────────────────────────────────
function handlersFor(s: Session, event: string): Array<{ ext: any; handler: (...a: any[]) => any }> {
  const out: Array<{ ext: any; handler: (...a: any[]) => any }> = [];
  for (const ext of s.extensions) {
    for (const h of ext.handlers?.get(event) ?? []) out.push({ ext, handler: h });
  }
  return out;
}

function recordHookError(s: Session, event: string, path: string, err: unknown): void {
  const msg = String((err as Error)?.message ?? err).split("\n")[0];
  s.hookErrors.push({ event, path: basename(String(path)), error: msg });
  if (s.hookErrors.length > 50) s.hookErrors.shift();
  log("hook failed", { session: s.id, event, ext: basename(String(path)), error: msg });
}

/** 普通事件：返回最后一个非 undefined 的结果（Pi 的 emit 也是这样）。 */
async function emit(s: Session, type: string, event: any, ctx: any): Promise<any> {
  let result: any;
  for (const { ext, handler } of handlersFor(s, type)) {
    try {
      const r = await handler(event, ctx);
      if (r !== undefined) result = r;
    } catch (e) {
      recordHookError(s, type, ext.path, e);
    }
  }
  return result;
}

/** message_end：每个 handler 可以换掉消息，同 role 才收（Pi 的 emitMessageEnd）。 */
async function emitMessageEnd(s: Session, message: any, ctx: any): Promise<any> {
  let current = message;
  let modified = false;
  for (const { ext, handler } of handlersFor(s, "message_end")) {
    try {
      const r = await handler({ type: "message_end", message: current }, ctx);
      if (!r?.message) continue;
      if (r.message.role !== current.role) {
        recordHookError(s, "message_end", ext.path, new Error("message_end 换消息不能改 role"));
        continue;
      }
      current = r.message;
      modified = true;
    } catch (e) {
      recordHookError(s, "message_end", ext.path, e);
    }
  }
  return modified ? current : undefined;
}

/** context：handler 可以返回新数组，也可以原地改（Pi 的 emitContext）。 */
async function emitContext(s: Session, messages: any[], ctx: any): Promise<any[]> {
  let current = structuredClone(messages);
  for (const { ext, handler } of handlersFor(s, "context")) {
    try {
      const before = structuredClone(current);
      const event = { type: "context", messages: current };
      const r = await handler(event, ctx);
      if (Array.isArray(r?.messages)) current = r.messages;
      else if (safeJson(event.messages) !== safeJson(before)) current = event.messages;
    } catch (e) {
      recordHookError(s, "context", ext.path, e);
    }
  }
  return current;
}

/** tool_call：任一 handler 回 block 就停（Pi 的 emitToolCall）。 */
async function emitToolCall(s: Session, event: any, ctx: any): Promise<any> {
  let result: any;
  for (const { ext, handler } of handlersFor(s, "tool_call")) {
    try {
      const r = await handler(event, ctx);
      if (r) {
        result = r;
        if (r.block) return r;
      }
    } catch (e) {
      recordHookError(s, "tool_call", ext.path, e);
    }
  }
  return result;
}

// ── 工具执行 ─────────────────────────────────────────────────────────────────
function normalizeToolResult(res: any): { content: any[]; isError: boolean } {
  const isError = res?.isError === true;
  if (res && Array.isArray(res.content)) {
    const content = res.content
      .filter((p: any) => p && typeof p === "object")
      .map((p: any) =>
        p.type === "image"
          ? { type: "image", data: String(p.data ?? ""), mimeType: String(p.mimeType ?? "image/png") }
          : { type: "text", text: String(p.text ?? "") },
      );
    return { content, isError };
  }
  if (typeof res === "string") return { content: [{ type: "text", text: res }], isError };
  return { content: [{ type: "text", text: res === undefined ? "（这件工具没有返回内容）" : safeJson(res) }], isError };
}

async function callTool(s: Session, name: string, args: unknown, signal: AbortSignal): Promise<{ content: any[]; isError: boolean }> {
  return inSession(s, async () => {
    const tool = visibleTools(s).find((t) => t.name === name);
    if (!tool) throw new HttpError(404, `没有这件工具：${name}`);
    const ctx = makeCtx(s, signal);
    const id = "call_" + randomUUID();
    try {
      const res = await tool.execute(id, args ?? {}, signal, () => {}, ctx);
      return normalizeToolResult(res);
    } catch (e) {
      // Pi 里工具抛错就是一次失败的工具结果，她看到原因自己决定怎么说
      return { content: [{ type: "text", text: String((e as Error)?.message ?? e) }], isError: true };
    }
  });
}

/** 壳插件工具在 before_agent_start 里同步；tools/list 先到一步时补一次（5 秒内不重复打宿主）。 */
async function ensureShellTools(s: Session): Promise<void> {
  if (Date.now() - s.shellSyncedAt < 5000) return;
  const ext = s.extensions.find((e) => basename(String(e.path)) === "albion-shell-tools.ts");
  if (!ext) return;
  await inSession(s, async () => {
    const options = makePromptOptions();
    const ctx = makeCtx(s);
    for (const handler of ext.handlers?.get("before_agent_start") ?? []) {
      try {
        await handler({ type: "before_agent_start", prompt: "", images: undefined, systemPrompt: "", systemPromptOptions: options }, ctx);
      } catch (e) {
        recordHookError(s, "before_agent_start", ext.path, e);
      }
    }
    s.shellSyncedAt = Date.now();
  });
}

// ── 三个每轮接口 ─────────────────────────────────────────────────────────────
function asUserContent(text: string, images: any[]): any {
  if (!images.length) return text;
  return [{ type: "text", text }, ...images.map((im) => ({ type: "image", data: im.data, mimeType: im.mime }))];
}

function lastUserText(messages: any[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m?.role !== "user") continue;
    const c = m.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) return c.filter((p: any) => p && (p.type === undefined || p.type === "text")).map((p: any) => p.text ?? "").join("\n");
  }
  return "";
}

function textOfContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((b: any) => b && b.type === "text").map((b: any) => b.text ?? "").join("\n");
}

function normalizeImages(raw: unknown): any[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((im: any) => im && typeof im.data === "string" && im.data)
    .map((im: any) => ({ mime: String(im.mime || im.mimeType || "image/png"), data: String(im.data) }));
}

async function handleBefore(s: Session, body: any): Promise<any> {
  const text = String(body?.text ?? "");
  const images = normalizeImages(body?.images);
  return inSession(s, async () => {
    const ctx = makeCtx(s);
    s.turn = { active: true, text, images, messages: [], systemSections: {}, toolCalls: [], textOnly: false };
    // ① agent_start：loopguard 在这里清本轮状态、给她的目录拍快照
    await emit(s, "agent_start", { type: "agent_start" }, ctx);
    // ② before_agent_start：常驻技能塞 section、壳插件工具同步
    const options = makePromptOptions();
    const extraMessages: any[] = [];
    const event: any = {
      type: "before_agent_start",
      prompt: text,
      images: images.length ? images.map((im) => ({ type: "image", data: im.data, mimeType: im.mime })) : undefined,
      get systemPrompt() {
        return renderSections(options.sections);
      },
      systemPromptOptions: options,
    };
    for (const { ext, handler } of handlersFor(s, "before_agent_start")) {
      try {
        const r = await handler(event, ctx);
        if (r?.systemPrompt !== undefined) options.forceSystemPrompt = r.systemPrompt;
        if (r?.message) extraMessages.push(r.message);
      } catch (e) {
        recordHookError(s, "before_agent_start", ext.path, e);
      }
    }
    s.shellSyncedAt = Date.now();
    // ③ context：albion-turn 把最后一条 user 改写成「原文 + 回忆块 + 逐轮约定」
    let messages = [{ role: "user", content: asUserContent(text, images), timestamp: Date.now() }];
    messages = await emitContext(s, messages, ctx);
    // ④ before_provider_request：guard 在这里记下本轮原话（message_end 的整理要用），
    //    loopguard 在这里摘掉 schema 不合格的工具、【接着说完】那一轮整个不给工具。
    //    没有真实 provider 请求，只喂一份合成的（model + messages + 工具声明）：
    //    guard 用 payload.model 判这一轮是不是 DS（口癖语料替换/姿态额度只对 DS 生效）。
    const payload: any = {
      model: MODEL,
      messages,
      tools: visibleTools(s).map((t) => ({ type: "function", function: { name: t.name, description: t.description, parameters: t.parameters } })),
    };
    s.lastProviderPayload = payload;
    const after = await emit(s, "before_provider_request", { type: "before_provider_request", payload }, ctx);
    // loopguard 去掉 tools = 这一轮只让她用文字（【接着说完】）：tools/list 清空、tools/call 直接拒
    s.turn.textOnly = !!(after && Array.isArray(payload.tools) && !Array.isArray(after.tools));
    const prompt = lastUserText(messages) || text;
    s.lastSource = prompt;
    s.turn.messages = messages;
    s.turn.systemSections = options.sections;
    const systemAppend = [renderSections(options.sections), ...extraMessages.map((m) => textOfContent(m?.content ?? ""))]
      .filter((x) => x && x.trim())
      .join("\n\n");
    return {
      prompt,
      system_append: systemAppend,
      session: s.id,
      model: MODEL,
      text_only: s.turn.textOnly,
      tools: s.turn.textOnly ? [] : visibleTools(s).map((t) => t.name),
      skills: s.skillPaths,
      extensions_broken: s.errors,
    };
  });
}

async function handleEvent(s: Session, body: any): Promise<any> {
  const type = String(body?.type ?? "");
  return inSession(s, async () => {
    const ctx = makeCtx(s);
    const toolCallId = String(body?.toolCallId ?? body?.id ?? "");
    const toolName = String(body?.toolName ?? body?.name ?? "");
    const input = (body?.input ?? body?.args ?? {}) as Record<string, unknown>;
    if (type === "tool_call") {
      const r = await emitToolCall(s, { type: "tool_call", toolCallId, toolName, input }, ctx);
      if (r?.block) return { abort: true, reason: String(r.reason ?? "这一轮的工具停用了。"), terminate: r.terminate === true };
      return { abort: false };
    }
    if (type === "tool_result") {
      s.turn.toolCalls.push({ name: toolName, args: input });
      const text = typeof body?.text === "string" ? body.text : textOfContent(body?.content);
      await emit(
        s,
        "tool_result",
        { type: "tool_result", toolCallId, toolName, input, content: [{ type: "text", text }], isError: body?.isError === true },
        ctx,
      );
      return { abort: false };
    }
    if (type === "message_end") {
      const message = body?.message ?? { role: "assistant", content: [{ type: "text", text: String(body?.text ?? "") }], stopReason: "stop", timestamp: Date.now() };
      const out = await emitMessageEnd(s, message, ctx);
      return { abort: false, text: out ? textOfContent(out.content) : textOfContent(message.content) };
    }
    if (type === "agent_end") {
      const messages = Array.isArray(body?.messages) && body.messages.length ? body.messages : s.turn.messages;
      await emit(s, "agent_end", { type: "agent_end", messages }, ctx);
      return { abort: false };
    }
    throw new HttpError(400, `不认识的事件类型：${JSON.stringify(type)}`);
  });
}

async function handleFinal(s: Session, body: any): Promise<any> {
  const text = String(body?.text ?? "");
  return inSession(s, async () => {
    // C 块可以把本轮原话一起带过来（before 之后 agy 可能改写/拼接）；没带就用 before 记下的那份
    if (typeof body?.source === "string" && body.source.trim()) s.lastSource = body.source;
    const ctx = makeCtx(s);
    const draft = { role: "assistant", content: [{ type: "text", text }], stopReason: "stop", timestamp: Date.now() };
    // message_end：guard 的整理（ACT 补竖线、语料、心声上限…）、loopguard 记账
    const out = await emitMessageEnd(s, draft, ctx);
    const finalText = out ? textOfContent(out.content) : text;
    // agent_end：albion-turn 真正落活动库的那一步（她在摆件上说了什么）。
    // 没走过 before（没有他的话）就不落库，免得记出一条空 user 行。
    const users = s.turn.messages.filter((m) => m?.role === "user");
    if (users.length) {
      const messages = [...users, { role: "assistant", content: [{ type: "text", text: finalText }], stopReason: "stop", timestamp: Date.now() }];
      await emit(s, "agent_end", { type: "agent_end", messages }, ctx);
    }
    s.turn.active = false;
    s.turn.textOnly = false; // 这一轮说完了：下一轮由 before 重新判
    return { text: finalText, changed: finalText !== text, session: s.id };
  });
}

// ── MCP（Streamable HTTP，只做 agy 用得到的那部分）───────────────────────────
function mcpToolList(s: Session): any[] {
  return visibleTools(s).map((t) => ({
    name: t.name,
    description: String(t.description ?? ""),
    inputSchema: (t.parameters as any) ?? { type: "object", properties: {} },
  }));
}

function rpcResult(id: any, result: unknown): any {
  return { jsonrpc: "2.0", id, result };
}
function rpcError(id: any, code: number, message: string): any {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function handleMcp(s: Session, msg: any, signal: AbortSignal): Promise<{ reply: any | null; mcpSession?: string }> {
  const method = String(msg?.method ?? "");
  const id = msg?.id;
  const isNotification = id === undefined || id === null;
  if (method === "initialize") {
    const asked = String(msg?.params?.protocolVersion ?? "");
    return {
      reply: rpcResult(id, {
        protocolVersion: asked || "2025-06-18",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "albion-agy-host", version: "1.0.0" },
        instructions: `阿尔比恩的 ${s.id} 会话：工具来自她自己的扩展。`,
      }),
      mcpSession: randomUUID(),
    };
  }
  if (method.startsWith("notifications/")) return { reply: null };
  if (method === "ping") return { reply: rpcResult(id, {}) };
  if (method === "tools/list") {
    await ensureShellTools(s);
    if (s.turn.textOnly) return { reply: rpcResult(id, { tools: [] }) };
    return { reply: rpcResult(id, { tools: mcpToolList(s) }) };
  }
  if (method === "tools/call") {
    const name = String(msg?.params?.name ?? "");
    const args = msg?.params?.arguments ?? {};
    if (!name) return { reply: rpcError(id, -32602, "tools/call 少了 name") };
    if (s.turn.textOnly) {
      // 【接着说完】那一轮她只用文字收尾：工具全不给，发过来也不执行
      return { reply: rpcResult(id, { content: [{ type: "text", text: "这一轮不要再调工具，直接用文字把话说完。" }], isError: true }) };
    }
    await ensureShellTools(s);
    try {
      const out = await callTool(s, name, args, signal);
      return { reply: rpcResult(id, { content: out.content, ...(out.isError ? { isError: true } : {}) }) };
    } catch (e) {
      const err = e as HttpError;
      if (err?.status === 404) return { reply: rpcError(id, -32602, err.message) };
      return { reply: rpcResult(id, { content: [{ type: "text", text: String((e as Error)?.message ?? e) }], isError: true }) };
    }
  }
  if (method === "resources/list" || method === "prompts/list") {
    return { reply: rpcResult(id, method === "prompts/list" ? { prompts: [] } : { resources: [] }) };
  }
  if (!isNotification) return { reply: rpcError(id, -32601, `不支持的方法：${method}`) };
  return { reply: null };
}

// ── HTTP ─────────────────────────────────────────────────────────────────────
class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function readBody(req: any): Promise<any> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(new HttpError(413, "请求体太大"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf-8").trim();
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new HttpError(400, "请求体不是合法 JSON"));
      }
    });
    req.on("error", (e: Error) => reject(new HttpError(400, e.message)));
  });
}

function sendJson(res: any, status: number, data: unknown, headers: Record<string, string> = {}): void {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    ...headers,
  });
  res.end(body);
}

/** MCP 的 Streamable HTTP：客户端接受 SSE 就按 SSE 回一条消息，否则回 JSON。 */
function sendMcp(res: any, status: number, data: unknown, accept: string, headers: Record<string, string> = {}): void {
  const body = JSON.stringify(data);
  if (accept.includes("text/event-stream")) {
    res.writeHead(status, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store",
      connection: "keep-alive",
      ...headers,
    });
    res.end(`event: message\ndata: ${body}\n\n`);
    return;
  }
  sendJson(res, status, data, headers);
}

function healthSnapshot(): any {
  return {
    ok: true,
    pid: process.pid,
    port: PORT,
    model: MODEL,
    ext_dir: EXT_DIR,
    skip: [...SKIP],
    uptime_s: Math.round(process.uptime()),
    sessions: [...sessions.values()].map((s) => ({
      session: s.id,
      loaded: !!s.loadedAt,
      extensions: s.extensions.length,
      tools: visibleTools(s).length,
      tool_names: visibleTools(s).map((t) => t.name),
      errors: s.errors,
      hook_errors: s.hookErrors.slice(-5),
      skill_paths: s.skillPaths,
      turn_active: s.turn.active,
    })),
  };
}

async function route(req: any, res: any): Promise<void> {
  const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  if (req.method === "GET" && (path === "/health" || path === "/")) return sendJson(res, 200, healthSnapshot());

  const mcp = /^\/mcp\/([^/]+)$/.exec(path);
  if (mcp) {
    const sessionId = decodeURIComponent(mcp[1]);
    const accept = String(req.headers.accept ?? "");
    if (req.method === "GET") {
      // 没有服务端主动推的东西：按 Streamable HTTP 的说法回 405
      return sendJson(res, 405, rpcError(null, -32000, "这个宿主不提供服务端 SSE 流"), { allow: "POST, DELETE" });
    }
    if (req.method === "DELETE") {
      sessions.delete(sessionId);
      return sendJson(res, 200, { ok: true });
    }
    if (req.method !== "POST") return sendJson(res, 405, rpcError(null, -32000, "只接 POST"), { allow: "POST" });
    const body = await readBody(req);
    const s = await ensureSession(sessionId);
    const ac = new AbortController();
    req.on("close", () => {
      if (!res.writableEnded) ac.abort();
    });
    // 批量请求（JSON-RPC batch）也认：agy 一般单发，但规范允许
    const batch = Array.isArray(body) ? body : null;
    const msgs = batch ?? [body];
    const replies: any[] = [];
    let mcpSession: string | undefined = undefined;
    for (const m of msgs) {
      const { reply, mcpSession: sid } = await handleMcp(s, m, ac.signal);
      if (sid) mcpSession = sid;
      if (reply) replies.push(reply);
    }
    const headers: Record<string, string> = {};
    const given = String(req.headers["mcp-session-id"] ?? "") || mcpSession;
    if (given) headers["mcp-session-id"] = given;
    if (!replies.length) {
      res.writeHead(202, headers);
      res.end();
      return;
    }
    return sendMcp(res, 200, batch ? replies : replies[0], accept, headers);
  }

  const turn = /^\/turn\/([^/]+)\/(before|event|final)$/.exec(path);
  if (turn) {
    if (req.method !== "POST") return sendJson(res, 405, { ok: false, error: "只接 POST" }, { allow: "POST" });
    const sessionId = decodeURIComponent(turn[1]);
    const action = turn[2];
    const body = await readBody(req);
    const s = await ensureSession(sessionId);
    if (action === "before") return sendJson(res, 200, await handleBefore(s, body));
    if (action === "event") return sendJson(res, 200, await handleEvent(s, body));
    return sendJson(res, 200, await handleFinal(s, body));
  }

  return sendJson(res, 404, { ok: false, error: `没有这个路径：${path}` });
}

const server = createServer((req, res) => {
  route(req, res).catch((e) => {
    const status = e instanceof HttpError ? e.status : 500;
    const message = String((e as Error)?.message ?? e);
    if (status >= 500) log("request failed", { url: req.url, error: message });
    if (!res.headersSent) sendJson(res, status, { ok: false, error: message });
    else res.end();
  });
});

server.listen(PORT, HOST, () => {
  log(`agy_host listening on http://${HOST}:${PORT}`, { agent_dir: AGENT_DIR, ext_dir: EXT_DIR, skip: [...SKIP], model: MODEL });
});

function shutdown(sig: string): void {
  log("shutting down", { signal: sig });
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
