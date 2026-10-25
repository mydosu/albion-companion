// albion-play.ts 的 adventure 工具：真把命令输进 /usr/games/adventure、记录步子、一局结束会挪开记录。
// 前置：WSL 里装了 bsdgames（/usr/games/adventure）。跑法（WSL）：
//   ALBION_ADVENTURE_MOVES=/tmp/adv-test/adventure.moves node tests/pi-brain/check_adventure_tool.ts
// 只碰 ALBION_ADVENTURE_MOVES 指向的临时文件，不碰她真实的 /root/家/游戏/。
import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { dirname } from "node:path";

const movesPath = process.env.ALBION_ADVENTURE_MOVES ?? "/tmp/adv-test/adventure.moves";
process.env.ALBION_ADVENTURE_MOVES = movesPath;
rmSync(dirname(movesPath), { recursive: true, force: true });
mkdirSync(dirname(movesPath), { recursive: true });

const { default: albionPlay } = await import(new URL("../../integrations/pi-brain/agent/extensions/albion-play.ts", import.meta.url).href);
let tool: any;
albionPlay({ registerTool: (t: any) => (tool = t) } as any);
const run = async (moves: string) => String((await tool.execute("t", { moves })).content[0].text);

const first = await run("no\nin\ntake lamp");
assert.ok(first.includes("新开了一局"), first.slice(0, 200));
assert.ok(first.includes("真走了 3 步"), first.slice(0, 200));
assert.ok(/lamp|OK/i.test(first), "游戏应有回应：" + first.slice(-300));
assert.deepEqual(readFileSync(movesPath, "utf-8").trim().split("\n"), ["no", "in", "take lamp"]);

const second = await run("out");
assert.ok(second.includes("真走了 1 步") && !second.includes("新开了一局"), "进程常驻，接着走：" + second.slice(0, 200));
assert.equal(readFileSync(movesPath, "utf-8").trim().split("\n").length, 4);

const end = await run("quit\nyes");
assert.ok(end.includes("这一局结束了"), "quit 后应说明一局结束：" + end.slice(-300));
assert.ok(!existsSync(movesPath) && readdirSync(dirname(movesPath)).some((n) => n.startsWith("adventure.moves.done-")), "记录挪到 .done-");

const again = await run("look");
assert.ok(again.includes("新开了一局"), "结束后再玩是新局：" + again.slice(0, 200));
console.log("ADVENTURE TOOL OK：真输命令 / 记步子 / 进程常驻接着走 / 一局结束挪开记录 / 重开新局");
process.exit(0);
