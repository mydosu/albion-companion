/**
 * session-scope —— 哪些会话算「他本人那一只」，哪些算探针/验收/自检（2026-10-29）。
 *
 * 为什么要有：2026-10-25 的口癖测试脚本用独立会话（albion-voice-probe-*）在生产脑上跑，
 * 台词「今天好累，实验做了一下午」被当成他说的话写进共享的 state/notebook.json（心事本 #16），
 * 之后的自主醒来轮次把这条念回来，OpenViking 又把醒来提示词抽成「指挥官做实验一天」的假事件。
 * 会话 id 各自独立并没有用——心事本、self/plan、OpenViking 记忆是全进程/全空间共享的。
 *
 * 约定：**真实会话只有 agent/real-sessions.json 里那几个**（env ALBION_MEMORY_SESSIONS 可覆盖），
 * 别的会话一律算 ephemeral：
 *   · 不写生产的 notebook/self/todo/voice/last-context（改写 ALBION_PERSONAL_STATE_DIR 下的临时文件，
 *     工具照常返回，测试脚本不会因为读不到刚写的条目而误判）；
 *   · 不提交给 OpenViking（服务端另有 OPENVIKING_BYPASS_SESSION，见 bin/server.py）。
 * 判断放在这里共用：宿主（bin/server.py）给 pi 子进程设 ALBION_EPHEMERAL=1 / ALBION_PERSONAL_STATE_DIR；
 * 直接 `pi -e ...` 起的探针（tests/pi-brain 下那几支）没有宿主，就靠 ALBION_SESSION_ID 自己判。
 *
 * 没有 ALBION_SESSION_ID 的手工运行按生产算（旧行为不变）；宁可漏判一次手工调试，也不改桌面那只的路径。
 */
import { existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const DEFAULT_REAL_SESSIONS = ["albion-desktop", "albion-cli"];

const agentDir = () => process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";

function readRealSessionsFile(): string[] | null {
  const p = join(agentDir(), "real-sessions.json");
  if (!existsSync(p)) return null;
  try {
    const v = JSON.parse(readFileSync(p, "utf-8"));
    const list = Array.isArray(v) ? v : Array.isArray(v?.sessions) ? v.sessions : null;
    const out = (list ?? []).filter((x: unknown) => typeof x === "string" && x.trim()).map((x: string) => x.trim());
    return out.length ? out : null;
  } catch {
    return null;
  }
}

/** 真实会话名单：env 优先（宿主用同一份），其次 agent/real-sessions.json，最后是内置默认。 */
export function realSessions(): string[] {
  const env = (process.env.ALBION_MEMORY_SESSIONS || "").trim();
  if (env) return env.split(",").map((s) => s.trim()).filter(Boolean);
  return readRealSessionsFile() ?? DEFAULT_REAL_SESSIONS;
}

const currentSession = () => (process.env.ALBION_SESSION_ID || "").trim();

/** 这个会话是不是探针/验收/自检（不写生产状态、不进 OpenViking）。 */
export function isEphemeralSession(sessionId: string = currentSession()): boolean {
  if ((process.env.ALBION_EPHEMERAL || "").trim() === "1") return true;
  if (!sessionId) return false; // 没有会话名的手工运行：按旧行为当生产
  return !realSessions().includes(sessionId);
}

/** 会话名进目录名：只留字母数字和横线，别的换成下划线（避免 ../ 之类跑出临时目录）。 */
function safeName(sessionId: string): string {
  const s = sessionId.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120);
  return s || "unknown";
}

/**
 * 该往哪个目录写「她自己的状态」（心事本、self/plan、todo、voice、last-context）。
 * 真实会话就是 ALBION_STATE_DIR（生产 /root/albion-pi/state，行为完全不变）；
 * ephemeral 会话写到临时目录：宿主给了 ALBION_PERSONAL_STATE_DIR 就用它，否则按会话名落到 /tmp。
 */
export function personalStateDir(sessionId: string = currentSession()): string {
  const explicit = (process.env.ALBION_PERSONAL_STATE_DIR || "").trim();
  if (explicit) return explicit;
  const base = (process.env.ALBION_STATE_DIR || "").trim() || "/root/albion-pi/state";
  if (!isEphemeralSession(sessionId)) return base;
  return join(tmpdir(), "albion-pi-ephemeral", safeName(sessionId));
}

export function personalStatePath(name: string, sessionId: string = currentSession()): string {
  return join(personalStateDir(sessionId), name);
}
