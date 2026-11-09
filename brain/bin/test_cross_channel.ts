/**
 * 跨渠道近况（albion-turn.ts 的 crossChannelBlock）确定性自检：不联网、不起 Pi、只读临时活动库。
 *
 * 覆盖：桌面只收群通知（不含悄悄话）、Discord 收桌面近况、第二次无新增不重复、
 * 内部提示/播种块过滤、send_message 正文落库、6 小时窗口和 12 行上限。
 *
 * 跑：node bin/test_cross_channel.ts      （Node 26 直接跑 TS；Windows 上也能跑，不依赖 /root）
 */
import assert from "node:assert";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

const TMP = mkdtempSync(join(tmpdir(), "albion-cross-"));
process.env.ALBION_STATE_DIR = TMP;
process.env.ALBION_STATE_DB = join(TMP, "state.db");
process.env.ALBION_SESSION_ID = "albion-desktop";
process.env.HOME = "/root";

const db = new DatabaseSync(process.env.ALBION_STATE_DB);
db.exec(
  "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);",
);
db.exec("CREATE TABLE sessions (id TEXT PRIMARY KEY, last_activity_at REAL, message_count INTEGER, tool_call_count INTEGER);");
const ins = db.prepare("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp) VALUES (?,?,?,?,?,?)");
const add = (session: string, role: string, content: string, ts: number, tool: string | null = null) =>
  ins.run(session, role, content, null, tool, ts);

const BASE = Math.floor(Date.UTC(2026, 10, 3, 8, 0, 0) / 1000); // 2026-11-03 16:00 北京
const NOW = BASE + 3600; // 17:00
const D = "albion-desktop";
const G = "albion-discord";
const groupNotice = [
  "【平台消息】Discord 群「常规」里有新消息（群里大家都看得到；我和指挥官之间的私事、当面说过的话不发这儿）：",
  "可畏也在群里，群里说的话她都看得到。",
  "（Discord 群）#81 16:05 指挥官：群里好呀",
  "（Discord 群）#82 16:06 可畏：收到。",
  "这些你可以回，也可以不回，想说再说。",
  "想说话就用 send_message（channel=discord_group）；这里写的字群里看不到。",
].join("\n");
const whisperNotice = [
  "【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：",
  "#85 16:10 可畏：悄悄话里的秘密",
  "想回就用 send_message（channel=discord_whisper）；这里写的字她看不到。",
].join("\n");
const dmNotice = [
  "【平台消息】指挥官在Telegram上给我发了消息（写给自己看的，不用念）：",
  "（Telegram）#7 11-03 16:22 指挥官：TG 上说的话",
  "想回就用 send_message 回他（channel=telegram）；这一轮我在这里写的字他看不到。",
].join("\n");

// 群：两行通知、一条她发群里的、自己的悄悄话和悄悄话发送、播种块、6 小时外的一行
add(G, "user", groupNotice, BASE + 300);
add(G, "user", whisperNotice, BASE + 600);
add(G, "tool", JSON.stringify({ channel: "discord_group", text: "群里我也在。" }), BASE + 720, "send_message");
add(G, "tool", JSON.stringify({ channel: "discord_whisper", text: "悄悄话里我也在。" }), BASE + 780, "send_message");
add(G, "user", "【以下是这次对话此前的记录】\n指挥官：（Discord 群）#1 12:00 指挥官：旧群消息\n阿尔比恩：旧回复", BASE + 840);
add(G, "user", "（Discord 群）#1 09:00 指挥官：七小时前的群消息", BASE - 7 * 3600);

// 桌面：原话、TG 私信通知、她的回复、她发 Telegram 的内容、内部提示、播种块
add(D, "user", "[2026-11-03 16:20] 桌面上的一句话", BASE + 1200);
add(D, "user", dmNotice, BASE + 1320);
add(D, "assistant", '\n<|ACT:{"emotion":"happy"}|>\n桌面上回他一句', BASE + 1320);
add(D, "tool", JSON.stringify({ channel: "telegram", text: "TG 上回的话" }), BASE + 1380, "send_message");
add(D, "user", "【自主醒来】现在是 2026-11-03 16:25。", BASE + 1500);
add(D, "user", "（阿尔比恩自己醒来）", BASE + 1560);
add(D, "user", "【接着说完】（这是我自己心里的念头）", BASE + 1620);
add(D, "user", "【以下是这次对话此前的记录】\n指挥官：桌面旧话\n阿尔比恩：旧回复", BASE + 1680);

const { _albionTest } = await import("../agent/extensions/albion-turn.ts");
const { crossChannelBlock, CROSS_TITLE_FROM_GROUP, CROSS_TITLE_FROM_PRIVATE, CROSS_TITLE_WHISPER_REPLY } = _albionTest;

// A) 桌面：群近况 + 她发的群消息；悄悄话、播种块、6 小时外、内部提示都不进来
{
  const block = await crossChannelBlock(D, NOW * 1000);
  assert.ok(block, "桌面应该拿到群近况");
  assert.ok(block.startsWith(CROSS_TITLE_FROM_GROUP), block);
  assert.ok(block.includes("16:05 [Discord 群] 指挥官：群里好呀"), block);
  assert.ok(block.includes("16:06 [Discord 群] 可畏：收到。"), block);
  assert.ok(block.includes("16:12 [Discord 群] 你：群里我也在。"), block);
  assert.ok(!block.includes("悄悄话里的秘密"), "悄悄话通知不能进桌面");
  assert.ok(!block.includes("悄悄话里我也在"), "悄悄话发送不能进桌面");
  assert.ok(!block.includes("七小时前的群消息"), "超过 6 小时不要");
  assert.ok(!block.includes("旧群消息") && !block.includes("【平台消息】") && !block.includes("可畏也在群里"), "播种/说明行不要");
  console.log("A 桌面只带群近况和她群里的发言、悄悄话/旧事/内部提示不进 OK");
}

// B) 第二次没新增：不重复，返回 null
{
  assert.equal(await crossChannelBlock(D, (NOW + 60) * 1000), null);
  console.log("B 第二次没新增不重复 OK");
}

// C) 有新增只给新增的
{
  add(G, "user", "【平台消息】Discord 群「常规」里有新消息：\n（Discord 群）#90 16:30 指挥官：后来补的一句", BASE + 1800);
  const block = await crossChannelBlock(D, (NOW + 120) * 1000);
  assert.ok(block && block.includes("16:30 [Discord 群] 指挥官：后来补的一句"), block);
  assert.ok(!block.includes("群里好呀"), "旧的不要重复");
  console.log("C 只给上次之后新增的群消息 OK");
}

// D) Discord 会话：桌面原话、TG 私信、她的回复和发送；内部提示/播种块不要
{
  const block = await crossChannelBlock(G, NOW * 1000);
  assert.ok(block, "Discord 应该拿到桌面近况");
  assert.ok(block.startsWith(CROSS_TITLE_FROM_PRIVATE), block);
  assert.ok(block.includes("16:20 [桌面] 指挥官：桌面上的一句话"), block);
  assert.ok(block.includes("16:22 [Telegram] 指挥官：TG 上说的话"), block);
  assert.ok(block.includes("16:22 [桌面] 你：桌面上回他一句"), block);
  assert.ok(block.includes("16:23 [Telegram] 你：TG 上回的话"), block);
  for (const bad of ["【自主醒来】", "（阿尔比恩自己醒来）", "【接着说完】", "桌面旧话", "【平台消息】"])
    assert.ok(!block.includes(bad), `不该带：${bad}\n${block}`);
  assert.equal(await crossChannelBlock(G, (NOW + 60) * 1000), null);
  console.log("D Discord 带桌面近况、内部提示过滤、第二次不重复 OK");
}

// E) 别的会话不加，也不写状态
{
  const names = ["cross-channel.json", "cross-channel-desktop.json", "cross-channel-discord.json"];
  const snap = () => names.map((n) => (existsSync(join(TMP, n)) ? readFileSync(join(TMP, n), "utf8") : "")).join("\n--\n");
  const before = snap();
  assert.equal(await crossChannelBlock("albion-probe-x", NOW * 1000), null);
  assert.equal(snap(), before);
  console.log("E 探针/别的会话不加 OK");
}

// F) send_message 正文落库（tool 行 content 是 JSON；群/私信都记）
{
  const row = _albionTest.tool_row({ name: "send_message", args: { channel: "discord_group", text: "群里的话" } });
  assert.equal(row[0], "tool");
  assert.equal(row[2], "send_message");
  assert.deepEqual(JSON.parse(row[1]), { channel: "discord_group", text: "群里的话" });
  const empty = _albionTest.tool_row({ name: "send_message", args: { channel: "telegram", text: " " } });
  assert.equal(empty[1], "", "只有表情/空文本时保持原样");
  console.log("F send_message 正文落库 OK");
}

// G) 块放进 compose 里在原文之后、约定之前
{
  const other = await crossChannelBlock(D, NOW * 1000) || (await (async () => {
    add(G, "user", "（Discord 群）#91 16:40 指挥官：再来一句", BASE + 2400);
    return crossChannelBlock(D, (NOW + 180) * 1000);
  })());
  assert.ok(other, "要有一块");
  const composed = _albionTest.composeTurnContent("他说", null, false, [], "", (NOW + 180) * 1000, null, other);
  assert.ok(composed.content.startsWith("他说\n\n"), composed.content.slice(0, 40));
  assert.ok(composed.content.indexOf(other) < composed.content.indexOf(_albionTest.MARKER), "块在约定之前");
  console.log("G compose 拼接顺序 OK");
}

// H) 12 行上限：清掉水位，塞 20 行也最多 12 行
{
  for (const n of ["cross-channel.json", "cross-channel-desktop.json", "cross-channel-discord.json"]) rmSync(join(TMP, n), { force: true });
  for (let i = 0; i < 20; i++)
    add(G, "user", `（Discord 群）#${200 + i} 17:${String(i).padStart(2, "0")} 指挥官：第${i}条`, BASE + 3000 + i);
  const block = await crossChannelBlock(D, (NOW + 300) * 1000);
  assert.ok(block, "应该有块");
  const lines = block.split("\n").filter((l) => l.startsWith("- "));
  assert.ok(lines.length <= 12, `最多 12 行，实际 ${lines.length}`);
  assert.ok(block.length <= 1500, `最多 1500 字，实际 ${block.length}`);
  console.log("H 12 行/1500 字上限 OK");
}

// I) 水位分文件：旧 cross-channel.json 首次读时迁移；两个会话同时写不互相覆盖
{
  for (const n of ["cross-channel.json", "cross-channel-desktop.json", "cross-channel-discord.json"]) rmSync(join(TMP, n), { force: true });
  const legacy = join(TMP, "cross-channel.json");
  writeFileSync(legacy, JSON.stringify({ desktop: 1000, discord: 2000 }));
  const old = readFileSync(legacy, "utf8");
  add(D, "user", "[2026-11-03 17:30] 分文件测试的桌面话", BASE + 3300);
  const [desk, disc] = await Promise.all([
    crossChannelBlock(D, (NOW + 360) * 1000),
    crossChannelBlock(G, (NOW + 360) * 1000),
  ]);
  assert.ok(desk && disc, "两个会话都该拿到块");
  const df = JSON.parse(readFileSync(join(TMP, "cross-channel-desktop.json"), "utf8"));
  const gf = JSON.parse(readFileSync(join(TMP, "cross-channel-discord.json"), "utf8"));
  assert.ok(df.ts > 1000 && gf.ts > 2000, `迁移/写入 ${df.ts} ${gf.ts}`);
  assert.notEqual(df.ts, gf.ts, "两个键各写各的文件，不互相覆盖");
  assert.equal(readFileSync(legacy, "utf8"), old, "旧文件只读兼容，不删不改");
  console.log("I 水位分文件/旧文件兼容/并发互不覆盖 OK");
}

// ── J 压缩摘要：重启首轮的「旧笔记+近况」不能当成他这次说的话（2026-11-04）
{
  const { compactionDigest, NOW_SEPARATOR } = _albionTest;
  assert.equal(NOW_SEPARATOR, "\n\n【现在】\n", "分隔串要跟 server.py 的 NOW_SEPARATOR 一致");
  // 没有分隔：照旧一句「指挥官：」
  assert.equal(
    compactionDigest([
      { role: "user", content: "在吗" },
      { role: "assistant", content: "在的。" },
    ]),
    "指挥官：在吗\n阿尔比恩：在的。",
    "普通 user 消息照旧",
  );
  // 有分隔：前半标成「重启时带进来的」，后半才是他这次说的
  const seeded = compactionDigest([
    {
      role: "user",
      content: "（这是之前和指挥官聊过的事，整理成的笔记）\n## 指挥官说过的事\n他在赶论文。" + NOW_SEPARATOR + "下午去实验室",
    },
  ]);
  assert.equal(
    seeded,
    "（重启时带进来的旧笔记和近况，不是指挥官这时说的话）：（这是之前和指挥官聊过的事，整理成的笔记）\n## 指挥官说过的事\n他在赶论文。"
      + "\n指挥官：下午去实验室",
    "重启首轮要拆成两行",
  );
  assert.ok(!seeded.includes("指挥官：（这是之前"), "旧笔记不能挂到「指挥官：」后面");
  console.log("J 压缩摘要里重启首轮的拆分 OK");
}

// ── K–O 桌面问过悄悄话之后，前辈的回信带回桌面（2026-11-08）
const stateFiles = ["cross-channel.json", "cross-channel-desktop.json", "cross-channel-discord.json"];
const clearState = () => stateFiles.forEach((n) => rmSync(join(TMP, n), { force: true }));
const WHISPER_TAIL = "想回就用 send_message（channel=discord_whisper）；这里写的字她看不到。";
// 一篇课表式的长文：500 字以上，末尾留标记，用来验证不再被 180 字截断。
const LONG_SCHEDULE = [
  "【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：",
  "#130 15:48 可畏：我把指挥官这学期的课程安排理好了，免得你到处去找。",
  "",
  "**周四**",
  "- 08:15 - 09:55 单周数电 / 双周模电（教学中心102）",
  "- 10:15 - 11:55 概率论（教学中心202）",
  "- 13:00 - 14:40 普通物理实验（物质学院4-303B）",
  "- 18:00 - 19:40 毛概理论课（教学中心102）",
  "- 19:50 - 21:30 普物II辅导课（物质学院1-505）+ 20:45 - 21:30 模电习题课（信息学院1D107，隔周连堂，中间有5分钟换楼）",
  "",
  "**周末与作息要点**",
  "- 周六周日没有早课，周六下午主要处理通识课，17:00 之后留给他自由支配",
  "- 周日晚上 23:00 前必须让他睡下，因为周一一大早就是数电",
  "- 周四晚上是连堂，20:45 换楼容易匆忙，到时记得留心",
  "- 周四下午做完普物实验后，周五有大头实验报告要写，别由着他拖延",
  "- 你存一份在手边，往后看着时间提醒他就是了；末尾标记：周四连堂。",
  WHISPER_TAIL,
].join("\n");
assert.ok(LONG_SCHEDULE.length > 500, `长文要够长：${LONG_SCHEDULE.length}`);

// K) 桌面发过悄悄话 → 回信带回桌面，长文不截断；没有群内容也要出块
const T0 = BASE + 100_000;
{
  clearState();
  add(D, "tool", JSON.stringify({ channel: "discord_whisper", text: "可畏前辈，能分我一份课表吗？" }), T0, "send_message");
  add(G, "user", LONG_SCHEDULE, T0 + 120);
  const block = await crossChannelBlock(D, (T0 + 180) * 1000);
  assert.ok(block, "问过之后桌面要拿到回信");
  assert.ok(block.startsWith(CROSS_TITLE_WHISPER_REPLY), block.slice(0, 120));
  assert.ok(block.includes("15:48 [悄悄话] 可畏：我把指挥官这学期的课程安排理好了"), block.slice(0, 200));
  assert.ok(block.includes("末尾标记：周四连堂"), "长文不能被 180 字截断");
  assert.ok(block.includes("20:45 - 21:30 模电习题课"), block);
  assert.ok(!block.includes(CROSS_TITLE_FROM_GROUP), "没有群内容时不出群段");
  assert.ok(!block.includes("想回就用 send_message"), "通知尾巴不要");
  console.log("K 桌面问过悄悄话→回信带回桌面、长文不截断 OK");
}

// O) 回信注入过就不再重复；水位越过也不重复
{
  assert.equal(await crossChannelBlock(D, (T0 + 240) * 1000), null, "同一份回信不重复注入");
  writeFileSync(join(TMP, "cross-channel-desktop.json"), JSON.stringify({ ts: T0 + 130 }));
  assert.equal(await crossChannelBlock(D, (T0 + 300) * 1000), null, "水位越过回信就不重复注入");
  console.log("O 回信只注入一次（水位/去重）OK");
}

// L) 桌面没问过（上一次问话已超 12 小时）→ 悄悄话照旧不进桌面
{
  clearState();
  const later = T0 + 13 * 3600;
  add(G, "user", "【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：\n#200 09:00 可畏：没问过也塞进来的悄悄话\n" + WHISPER_TAIL, later - 600);
  const block = await crossChannelBlock(D, later * 1000);
  assert.ok(!block || !block.includes("没问过也塞进来的悄悄话"), String(block));
  console.log("L 没问过时悄悄话照旧不进桌面 OK");
}

// M) 回信早于问话时间 → 不进
{
  clearState();
  const T1 = BASE + 200_000;
  add(G, "user", "【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：\n#201 10:00 可畏：早于问话的回信\n" + WHISPER_TAIL, T1 - 600);
  add(D, "tool", JSON.stringify({ channel: "discord_whisper", text: "问一句" }), T1, "send_message");
  const block = await crossChannelBlock(D, (T1 + 600) * 1000);
  assert.ok(!block || !block.includes("早于问话的回信"), String(block));
  console.log("M 回信早于问话时间不进 OK");
}

// N) 回信晚于 问话+12 小时 → 不进（这时问话本身也已出窗口，两道界都挡住）
{
  clearState();
  const T2 = BASE + 300_000;
  add(D, "tool", JSON.stringify({ channel: "discord_whisper", text: "问一句" }), T2, "send_message");
  add(G, "user", "【平台消息】悄悄话里有新消息（这里是我和可畏两个人说话的地方）：\n#202 12:00 可畏：十二小时之后的迟来回信\n" + WHISPER_TAIL, T2 + 12 * 3600 + 60);
  const block = await crossChannelBlock(D, (T2 + 12 * 3600 + 120) * 1000);
  assert.ok(!block || !block.includes("十二小时之后的迟来回信"), String(block));
  console.log("N 超过问话+12 小时的回信不进 OK");
}

// ── P 平台轮的每轮约定 + 宿主那句「答了没发」提醒（2026-11-09）
{
  const plat = "【平台消息】Discord 群「常规」里有新消息：\n（Discord 群）#1 13:16 指挥官：@Albion 在吗";
  const g1 = _albionTest.turn_guidance(plat);
  assert.ok(g1.includes("这一轮是平台消息：你在这里写的字对方看不到"), g1.slice(-320));
  assert.ok(g1.includes("[SILENT]"), "平台轮要给出「不回」的写法");
  const g2 = _albionTest.turn_guidance("[2026-11-09 13:00] 在吗");
  assert.ok(!g2.includes("这一轮是平台消息"), "桌面他自己的话不加这句");
  const seeded = "【以下是这次对话此前的记录】\n指挥官：【平台消息】旧通知\n\n【现在】\n在吗";
  assert.ok(!_albionTest.turn_guidance(seeded).includes("这一轮是平台消息"), "历史里的平台通知不算这一轮");
  // 宿主追的那句提醒是内部提示：不进 Discord 那边的跨渠道块
  clearState();
  const T3 = BASE + 400_000;
  add(D, "user", "（你刚才那段话只写在了这里，对方看不到。想让他看到就用 send_message 发出去；不想发就只回 [SILENT]。）", T3);
  add(D, "user", "[2026-11-03 16:20] 桌面这边正常的一句话", T3 + 60);
  const block = await crossChannelBlock(G, (T3 + 120) * 1000);
  assert.ok(block && block.includes("桌面这边正常的一句话"), String(block));
  assert.ok(!block.includes("你刚才那段话只写在了这里"), "提醒的那句不进跨渠道块");
  console.log("P 平台轮约定提醒 + 内部提示不进跨渠道块 OK");
}

db.close();
rmSync(TMP, { recursive: true, force: true });
console.log("CROSS CHANNEL SELFTEST OK");
