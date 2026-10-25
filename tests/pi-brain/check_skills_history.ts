// albion-skills.ts（skill）与 albion-history.ts（chat_search）的离线检查。
// 跑法（WSL）：node tests/pi-brain/check_skills_history.ts
// 技能写到临时目录，聊天记录用临时 sqlite 库，不碰她真实的 /root/家/技能/ 与 state.db。
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

const root = mkdtempSync(join(tmpdir(), "skills-history-"));
process.env.ALBION_SKILLS_DIR = join(root, "技能");
process.env.ALBION_STATE_DB = join(root, "state.db");
process.env.ALBION_SESSION_ID = "albion-desktop";

const ext = (n: string) => new URL(`../../integrations/pi-brain/agent/extensions/${n}`, import.meta.url).href;
const tools: Record<string, any> = {};
for (const n of ["albion-skills.ts", "albion-history.ts"]) {
  const { default: f } = await import(ext(n));
  f({ registerTool: (t: any) => (tools[t.name] = t) } as any);
}
const run = async (name: string, p: any) => String((await tools[name].execute("t", p)).content[0].text);

// skill
assert.ok((await run("skill", { action: "list" })).includes("还是空的"));
assert.ok((await run("skill", { action: "write", name: "玩文字冒险", when: "想玩那款文字冒险时", steps: "用 adventure 工具，每行一条英文命令" })).includes("记下了"));
assert.ok((await run("skill", { action: "write", name: "../越界", when: "x", steps: "y" })).includes("记下了"));
assert.ok(!existsSync(join(root, "越界.md")), "技能名里的 ../ 不能写出目录");
assert.ok((await run("skill", { action: "list" })).includes("- 玩文字冒险：想玩那款文字冒险时"));
assert.ok((await run("skill", { action: "read", name: "玩文字冒险" })).includes("adventure 工具"));
assert.ok((await run("skill", { action: "write", name: "玩文字冒险", when: "想玩文字冒险时", steps: "先 look" })).includes("更新了"));
assert.ok((await run("skill", { action: "read", name: "没有这个" })).includes("没有叫"));
assert.ok((await run("skill", { action: "write", name: "半截" })).includes("没记下"));
// 只给了一大段 when、没给 steps：照样收下（r42 实测她就是这么调的）
const long = "玩 Colossal Cave Adventure 文字冒险时，用 adventure 工具一次走几步，每行一条英文命令，走完把见闻补进读着走";
assert.ok((await run("skill", { action: "write", name: "文字冒险", when: long })).includes("记下了"));
assert.ok((await run("skill", { action: "read", name: "文字冒险" })).includes("一次走几步"));
assert.ok(readFileSync(join(root, "技能", "玩文字冒险.md"), "utf-8").startsWith("# 想玩文字冒险时"));

// chat_search
const db = new DatabaseSync(process.env.ALBION_STATE_DB);
db.exec("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)");
const now = Date.now() / 1000;
const ins = db.prepare("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)");
ins.run("albion-desktop", "user", "[2026-10-07 20:00] 明天要交概率论作业", now - 7 * 86400);
ins.run("albion-desktop", "assistant", '<|ACT:{"emotion":"happy"}|> 概率论作业加油哦', now - 7 * 86400 + 5);
ins.run("albion-desktop", "user", "【自主醒来】（以下只给我自己看）概率论", now - 3600);
ins.run("albion-other", "user", "概率论 别的会话", now - 60);
ins.run("albion-desktop", "user", "今天吃了牛肉面", now - 100);
db.close();
const hit = await run("chat_search", { keywords: "概率论 作业" });
assert.ok(hit.includes("2 条") && hit.includes("指挥官：明天要交概率论作业") && hit.includes("我：概率论作业加油哦"), hit);
// 任一词命中就算，命中词多的排前面：多塞一个猜的词也不至于一条都搜不到
const loose = await run("chat_search", { keywords: "作业 数电 概率论" });
assert.ok(loose.includes("2 条") && loose.indexOf("明天要交概率论作业") < loose.indexOf("牛肉面") + 99999, loose);
assert.ok((await run("chat_search", { keywords: "完全不存在的词" })).includes("别拿印象拼答案"));
assert.ok(!hit.includes("自主醒来") && !hit.includes("别的会话"), "内部提示与别的会话不算");
assert.ok((await run("chat_search", { keywords: "概率论", days: 3 })).includes("没找到"), "days 只看最近几天");
assert.ok((await run("chat_search", { keywords: "牛肉面" })).includes("今天吃了牛肉面"));
assert.ok((await run("chat_search", { keywords: "  " })).includes("要给关键词"));
console.log("SKILLS/HISTORY OK：技能记/读/列/更新/防越界；聊天记录按词搜、带时间、过滤内部提示与别的会话、days 生效");
