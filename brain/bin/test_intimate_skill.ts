/**
 * 常驻技能 albion-intimate 的自检：读文件、去 frontmatter、接在 system 末尾、没有文件就不动。
 * 不碰网络、不碰生产，全部用临时目录里的假技能文本。
 *
 * 跑：node bin/test_intimate_skill.ts      （Node 22.18+ 直接跑 TS；Windows 上也能跑）
 */
import assert from "node:assert";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const TMP = mkdtempSync(join(tmpdir(), "albion-skill-"));
process.env.PI_CODING_AGENT_DIR = TMP;   // 必须放在 import 之前：AGENT_DIR 是模块加载时算的
process.env.ALBION_STATE_DIR = join(TMP, "state");

const SKILL_DIR = join(TMP, "skills", "albion-intimate");
mkdirSync(SKILL_DIR, { recursive: true });
const FILE = join(SKILL_DIR, "SKILL.md");
const BODY = "（假技能正文：一声一顿，别连成长句。）\n第二行。";
writeFileSync(FILE, "---\nname: albion-intimate\ndescription: test only\n---\n" + BODY + "\n", "utf-8");

const ext: any = await import("../agent/extensions/albion-turn.ts");
const { readIntimateSkill, withIntimateSkill, stripFrontmatter, _albionTest } = ext;
const SKILL_HEAD = _albionTest.SKILL_HEAD as string;

// A) frontmatter：有就去掉，没有就原样
assert.equal(stripFrontmatter("---\na: 1\n---\n正文"), "正文");
assert.equal(stripFrontmatter("正文\n---\n不是开头"), "正文\n---\n不是开头");
assert.equal(stripFrontmatter("---\na: 1\nb: 2\n---\n"), "");

// B) 有文件：读出的正文不含 frontmatter，且是原文
assert.equal(readIntimateSkill(), BODY, "读出的正文应等于去掉 frontmatter 的原文");

// C) 接在 system 末尾：原 system 一个字不改，后面正好是标题行 + 正文
assert.equal(withIntimateSkill("SYS", BODY), "SYS" + SKILL_HEAD + BODY);
assert.ok(withIntimateSkill("SYS", BODY).startsWith("SYS\n\n【我已经读过的技能：albion-intimate】\n"));
// 没有正文就原样返回（同一个引用，不是新串）
const same = "SYS-2";
assert.equal(withIntimateSkill(same, null), same);

// D) 没有文件：null，且 hook 返回 undefined（Pi 不会替换 system）
assert.equal(readIntimateSkill(join(TMP, "nope")), null);

// E) 钩子接线：before_agent_start 真的注册了，且按上面规则返回
const handlers: Record<string, Function> = {};
const fakePi = { on: (name: string, fn: Function) => { handlers[name] = fn; return () => {}; } };
ext.default(fakePi as any);
assert.equal(typeof handlers.before_agent_start, "function", "应当注册 before_agent_start");
const r1: any = await handlers.before_agent_start({ type: "before_agent_start", prompt: "x", systemPrompt: "BASE" });
assert.equal(r1.systemPrompt, "BASE" + SKILL_HEAD + BODY);

// F) 技能文件删掉之后：缓存不再顶用，hook 返回 undefined
rmSync(FILE);
assert.equal(readIntimateSkill(), null);
assert.equal(await handlers.before_agent_start({ type: "before_agent_start", prompt: "x", systemPrompt: "BASE" }), undefined);

// G) 每轮约定里那句已经换成主脑定的措辞，旧句不在了
assert.ok(_albionTest.CONTEXT.includes("这时照 albion-intimate 技能出声和断句。"));
assert.ok(!_albionTest.CONTEXT.includes("一进到亲密就先读它"));

rmSync(TMP, { recursive: true, force: true });
console.log("test_intimate_skill: 全部通过");
