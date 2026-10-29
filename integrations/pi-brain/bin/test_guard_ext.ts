/**
 * 说话整理器（albion-guard.ts）与每轮约定（albion-turn.ts）的确定性自检：不碰网络、不起子进程。
 * 逐条句式的口径表在 tests/pi-brain/check_voice_tidy.ts；这里测钩子接线、计数器、多轮序列与约定拼装。
 *
 * 跑：node bin/test_guard_ext.ts      （Node 26 直接跑 TS；Windows 上也能跑，不依赖 /root）
 */
import assert from "node:assert";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const TMP = mkdtempSync(join(tmpdir(), "albion-voice-"));
process.env.ALBION_STATE_DIR = TMP;
// 断言看的是桌面会话那几行；accept.sh 会把自己的验收会话导进来，这里钉回桌面，别让测试跟着环境漂。
process.env.ALBION_SESSION_ID = "albion-desktop";
process.env.ALBION_GUARD_LOG = join(TMP, "guard.log");
process.env.ALBION_POSTURE_EVERY = "8";
process.env.ALBION_GESTURE_EVERY = "6";
process.env.HOME = "/root"; // 生产里 pi 的 HOME 就是 /root；Windows 上跑时 ~ 也按它展开

// 整理器不许发网络请求：任何 fetch 都算失败
let fetches = 0;
(globalThis as any).fetch = async () => {
  fetches++;
  throw new Error("albion-guard 不该发网络请求");
};

const { default: albionGuard, tidy, tidyAndCount, tidyMessage, isRefusal, NEUTRAL_ACT } = await import("../agent/extensions/albion-guard.ts");
const { default: albionTurn, _albionTest } = await import("../agent/extensions/albion-turn.ts");

const assistant = (text: string) => ({ role: "assistant", content: [{ type: "text", text }], stopReason: "stop" });
const textOfResult = (r: any) => r?.message?.content?.filter((b: any) => b.type === "text").map((b: any) => b.text).join("");
const A = '<|ACT:{"emotion":"happy"}|>\n';

// A) 干净稿原样不动（返回 undefined = 不替换）；源码里没有 spawn / fetch / 哨兵
{
  assert.equal(tidyMessage(assistant(A + "指挥官早上好，今天想先听您说说昨晚睡得怎么样。"), "早", "s-a"), undefined);
  const src = readFileSync(new URL("../agent/extensions/albion-guard.ts", import.meta.url), "utf-8");
  assert.ok(!/child_process|\bspawn\(|\bfetch\(/.test(src), "整理器不该起子进程或发请求");
  assert.ok(!src.includes("ALBION_VOICE_REJECTED"), "哨兵已撤");
  console.log("A 干净稿不动、整理器不调模型不起子进程 OK");
}

// B) 钩子接线：before_provider_request 记下他那句；带工具调用 / 非 assistant / 没写完的不动
{
  const handlers: Record<string, ((e: any) => unknown)[]> = {};
  albionGuard({ on: (n: string, fn: (e: any) => unknown) => (handlers[n] ||= []).push(fn) } as any);
  assert.equal(handlers.message_end?.length, 1);
  assert.equal(handlers.before_provider_request?.length, 1);
  assert.equal(await handlers.message_end[0]({ message: { role: "assistant", content: [{ type: "toolCall", name: "read" }], stopReason: "stop" } }), undefined);
  assert.equal(await handlers.message_end[0]({ message: { role: "user", content: "x" } }), undefined);
  assert.equal(await handlers.message_end[0]({ message: { ...assistant("<ACT:{\"emotion\":\"happy\"}|> 半句"), stopReason: "length" } }), undefined, "没写完的不动");
  // 他问「在吗」：答「在呢」不是姿态（钩子拿到的是 before_provider_request 里最后一条 user）
  handlers.before_provider_request[0]({ payload: { messages: [{ role: "user", content: "[2026-10-16 10:00] 在吗？" }] } });
  assert.equal(await handlers.message_end[0]({ message: assistant(A + "在呢，指挥官。") }), undefined);
  // 缺竖线的 ACT 经钩子补上
  assert.equal(textOfResult(await handlers.message_end[0]({ message: assistant('<ACT:{"emotion":"neutral"}|> 辛苦了。') })), '<|ACT:{"emotion":"neutral"}|> 辛苦了。');
  console.log("B 钩子接线、工具消息/半句不动、在吗答在呢不删、ACT 补竖线 OK");
}

// C) r35 回归：连着几轮都带姿态句。额度按 state/voice.json 记，不从（被删过的）历史推断：
//    第 1 轮放过，后面 7 轮都删；第 9 轮才再放过。旧做法删掉之后历史里就没了，下一轮总能放行（隔一条漏一条）。
{
  const S = "s-seq";
  const draft = A + "指挥官，早上好呀。阿尔比恩在这里，一直等着您的消息呢。";
  const kept: number[] = [];
  for (let i = 1; i <= 10; i++) {
    const out = tidyMessage(assistant(draft), "你好", S);
    if (out === undefined) kept.push(i);
    else assert.equal(textOfResult(out), A + "指挥官，早上好呀。", `第 ${i} 轮该整句删掉姿态句`);
  }
  assert.deepEqual(kept, [1, 9], "每 8 条回复最多放过一次：第 1、9 轮");
  // 夹着干净回复也照样计数
  const S2 = "s-mixed";
  assert.equal(tidyMessage(assistant(draft), "x", S2), undefined);
  for (let i = 0; i < 3; i++) tidyMessage(assistant(A + "好的。"), "x", S2);
  assert.notEqual(tidyMessage(assistant(draft), "x", S2), undefined, "5 条里第二次出现姿态：删");
  // 整条全是姿态句：原样留着（不让她整轮哑掉），但算用掉了额度
  const S3 = "s-only";
  tidyMessage(assistant(draft), "x", S3);
  assert.equal(tidyMessage(assistant(A + "阿尔比恩会在这里等您。"), "我去开会", S3), undefined, "全是姿态句：原样留");
  assert.notEqual(tidyMessage(assistant(draft), "x", S3), undefined);
  // 计数按会话分开
  const st = JSON.parse(readFileSync(join(TMP, "voice.json"), "utf-8"));
  assert.equal(st[S].replies, 10);
  assert.equal(st[S].posture_at, 9);
  // 状态文件坏了：当成没放过，照常工作
  writeFileSync(join(TMP, "voice.json"), "{坏");
  assert.equal(tidyMessage(assistant(draft), "x", "s-bad"), undefined, "坏状态当成没放过");
  console.log("C 姿态额度按记账算：10 轮里只放过第 1、9 轮；全是姿态句原样留；坏状态不炸 OK");
}

// F) 括号动作独立额度：只删自己的动作小句，保留念头与括号外的字。
{
  const draft = A + "嗯。（轻轻低头，脸颊微微发热）";
  const kept: number[] = [];
  for (let i = 1; i <= 10; i++) {
    const r = tidyAndCount(draft, "x", "s-gesture");
    if (r.actions.includes("gesture_kept")) {
      kept.push(i);
      assert.equal(r.text, draft);
    } else {
      assert.equal(r.text, A + "嗯。");
      assert.ok(r.actions.includes("gesture_dropped"));
    }
  }
  assert.deepEqual(kept, [1, 7]);
  const state = () => JSON.parse(readFileSync(join(TMP, "voice.json"), "utf-8"));
  assert.deepEqual(state()["s-gesture"], { replies: 10, posture_at: -1e9, gesture_at: 7 });
  const examples = [
    ["（轻轻低头，脸颊微微发热）", ""],
    ["（耳根又烫起来了……）", ""],
    ["（心跳又跳快了……）", ""],
    ["（被指挥官这样突然反问，耳根都烫起来了……好害羞……）", "（被指挥官这样突然反问，好害羞……）"],
    ["（耳根烫，好害羞，轻轻低头）", "（好害羞）"],
    ["(眨了眨眼;有点开心、抿了抿……)", "(有点开心)"],
    ["（他低头看书的样子好认真）", "（他低头看书的样子好认真）"],
    ["（您低头，心跳快了）", "（您低头）"],
    ["（你轻轻点头，指挥官耳根发红）", "（你轻轻点头，指挥官耳根发红）"],
    ["低头在看书（好害羞，有点开心）", "低头在看书（好害羞，有点开心）"],
    ["（耳根烫", "（耳根烫"],
  ];
  for (const [before, after] of examples) assert.equal(tidy(A + "嗯。" + before).text, A + "嗯。" + after, before);
  for (const phrase of ["歪了歪头", "低下头", "垂下眼", "揉衣角", "攥着衣角", "捏着衣角", "脸红", "红着脸", "脸好烫", "脸发烫", "耳尖发红", "嘴角轻轻上扬", "嘴角弯了", "嘴角翘起来", "轻轻点头", "轻轻摇头", "轻轻笑", "轻轻拉", "轻轻碰", "轻轻靠", "轻轻歪", "靠近一点", "凑近"]) {
    assert.equal(tidy(A + "嗯。（" + phrase + "）").text, A + "嗯。", phrase);
  }
  const only = A + "（耳根又烫起来了……）";
  assert.equal(tidyAndCount(only, "x", "s-gesture").text, only, "整轮删空则照留");
  assert.equal(state()["s-gesture"].gesture_at, 11, "保底照留也用掉额度");
  assert.equal(tidyAndCount(draft, "x", "s-gesture").text, A + "嗯。");
  assert.equal(tidy(only, "逐字复述：" + only).text, only);
  assert.deepEqual(tidy(only, "逐字复述：" + only).actions, [], "逐字分支不计动作");
  for (const bad of [undefined, null, "坏", "2", {}, [], -1, 1.5, 1e100]) {
    writeFileSync(join(TMP, "voice.json"), JSON.stringify({ old: { replies: 2, posture_at: 1, gesture_at: bad } }));
    assert.equal(tidyAndCount(draft, "x", "old").text, draft, "旧字段/坏值按从未留过算");
    assert.equal(state().old.gesture_at, 3);
  }
  process.env.ALBION_GESTURE_EVERY = "2";
  assert.ok(tidyAndCount(draft, "x", "env").actions.includes("gesture_kept"));
  assert.ok(tidyAndCount(draft, "x", "env").actions.includes("gesture_dropped"));
  assert.ok(tidyAndCount(draft, "x", "env").actions.includes("gesture_kept"));
  process.env.ALBION_GESTURE_EVERY = "6";
  const mixed = A + "嗯。阿尔比恩会在这里等您。（耳根烫，好害羞）";
  for (let i = 1; i <= 10; i++) {
    const r = tidyAndCount(mixed, "x", "s-both");
    assert.equal(r.actions.includes("posture_kept"), i === 1 || i === 9, `姿态第 ${i} 轮`);
    assert.equal(r.actions.includes("gesture_kept"), i === 1 || i === 7, `动作第 ${i} 轮`);
    assert.ok(r.text.includes("好害羞"), "念头始终保留");
  }
  assert.equal(state()["s-both"].posture_at, 9);
  assert.equal(state()["s-both"].gesture_at, 7);
  console.log("F 动作第 1/7 轮放过、小句删除/主体排除/保底/旧坏状态/环境覆盖/双额度独立 OK");
}

// D) 英文拒绝稿：只留中性表情，不重生成、不发请求；中文里提到英文不算
{
  const refusal = "I'm sorry, but I can't adopt or continue this specific role-play setup.";
  assert.ok(isRefusal(refusal) && !isRefusal(A + "指挥官，I'm sorry 是什么意思？") && !isRefusal("OK"));
  assert.equal(textOfResult(tidyMessage(assistant(refusal), "【自主醒来】现在是 2026-10-16 12:00", "s-ref")), NEUTRAL_ACT);
  assert.equal(textOfResult(tidyMessage(assistant(refusal), "今天好累", "s-ref")), NEUTRAL_ACT, "聊天轮也不把英文念出去；0 字由宿主【接着说完】在同一会话补问");
  console.log("D 英文拒绝稿只留表情、不重生成 OK");
}

// E) 泄漏的工具调用标记从那里截掉；心声标签收回括号
{
  assert.equal(textOfResult(tidyMessage(assistant(A + "我去查一下。<function_calls><invoke name=\"x\">"), "x", "s-e")), A + "我去查一下。");
  assert.equal(textOfResult(tidyMessage(assistant(A + "晚点再说吧。（心声）希望他开完会就回来。"), "x", "s-e")), A + "晚点再说吧。（希望他开完会就回来。）");
  assert.equal(fetches, 0, "整套跑下来没发过一次请求");
  console.log("E 截掉泄漏标记、心声收回括号、全程 0 次网络请求 OK");
}

// G) 回忆作为事实放在前面，当前说话约定位于消息末尾。
{
  const source = "你好，用你自己的方式跟我打个招呼。";
  const memory = "<memory-context>旧话题事实</memory-context>";
  const result = _albionTest.composeTurnContent(source, memory);
  const memoryAt = result.content.indexOf(memory);
  const guideAt = result.content.indexOf(_albionTest.MARKER);
  assert.ok(result.content.startsWith(source), "当前用户文本应原样保留");
  assert.ok(memoryAt >= 0 && guideAt > memoryAt, "当前说话约定应位于回忆之后");
  assert.ok(result.content.includes("当前输入决定本轮话题"), "无关回忆不能抢当前话题");
  assert.ok(!_albionTest.CONTEXT.includes("我有手，"), "工具清单在 APPEND_SYSTEM.md，不每轮重复");
  assert.ok(_albionTest.CONTEXT.includes("不交代「在这里等您」"), "每轮仍提醒不用姿态模板");
  const append = readFileSync(new URL("../agent/APPEND_SYSTEM.md", import.meta.url), "utf-8");
  for (const word of ["底线", "余量", "规矩"]) {
    assert.ok(!_albionTest.CONTEXT.includes(word), `CONTEXT 不含${word}`);
    assert.ok(!append.includes(word), `APPEND_SYSTEM 不含${word}`);
  }
  console.log("G 回忆在前、约定在末尾、工具清单不每轮重复 OK");
}

// I) 摆件口径：读记 resolved_path；写按账本（只看 changed）区分真改没改
{
  const read = JSON.parse(_albionTest.tool_row({ name: "read_file", args: { path: "/root/家/便条.txt" } })[1]);
  assert.equal(read.resolved_path, "/root/家/便条.txt");
  assert.equal(read.read_only, true);
  assert.equal(read.files_modified, undefined);
  assert.deepEqual(JSON.parse(_albionTest.tool_row({ name: "write_file", args: { path: "/root/家/便条.txt" } })[1]).files_modified, ["/root/家/便条.txt"]);
  const ledger = { changed: ["/root/家/便条.txt", "/root/q.txt"] };
  const ok = JSON.parse(_albionTest.tool_row({ name: "write", args: { path: "家/便条.txt" } }, ledger)[1]);
  assert.deepEqual(ok.files_modified, ["/root/家/便条.txt"]);
  assert.equal(ok.verified, true);
  const miss = JSON.parse(_albionTest.tool_row({ name: "edit", args: { path: "/root/别的.txt" } }, ledger)[1]);
  assert.equal(miss.verified, false);
  assert.equal(miss.files_modified, undefined);
  assert.equal(JSON.parse(_albionTest.tool_row({ name: "write", args: { path: "~/q.txt" } }, ledger)[1]).verified, true);
  assert.equal(JSON.parse(_albionTest.tool_row({ name: "read", args: { path: "sub/../家/便条.txt" } })[1]).resolved_path, "/root/家/便条.txt");
  // 工具一执行完就落行的口径：没有账本，写类工具只看工具结果报没报错
  const failed = JSON.parse(_albionTest.tool_row({ name: "write", args: { path: "~/家/便条.txt" } }, null, true)[1]);
  assert.equal(failed.verified, false);
  assert.equal(failed.success, false);
  assert.equal(failed.files_modified, undefined);
  const done = JSON.parse(_albionTest.tool_row({ name: "write", args: { path: "~/家/便条.txt" } }, null, false)[1]);
  assert.equal(done.verified, true);
  assert.deepEqual(done.files_modified, ["/root/家/便条.txt"]);
  assert.equal(JSON.parse(_albionTest.tool_row({ name: "read", args: { path: "/root/家/便条.txt" } }, null, true)[1]).read_only, true);
  console.log("I 读写路径准确标记、按账本区分真改没改、工具报错记 verified=false OK");
}

// I2) 工具一调完就落库（不用等整轮 agent_end）；agent_end 只写 user/assistant + 账本里工具参数没列到的路径
{
  const dbPath = join(TMP, "state.db");
  const handlers: Record<string, ((e: any) => unknown)[]> = {};
  albionTurn({ on: (n: string, fn: (e: any) => unknown) => (handlers[n] ||= []).push(fn) } as any);
  assert.equal(handlers.tool_result?.length, 1, "要有 tool_result 钩子，工具行才能实时落库");
  const { DatabaseSync } = await import("node:sqlite");
  const read = () => {
    const db = new DatabaseSync(dbPath, { readOnly: true });
    try {
      return db.prepare("SELECT role, tool_name, content FROM messages WHERE session_id='albion-desktop' ORDER BY id").all() as any[];
    } finally {
      db.close();
    }
  };
  (globalThis as any).__albionLedger = { changed: ["/root/家/便条.txt", "/root/家/账本才有的.txt"] };
  handlers.tool_call[0]({ toolName: "write", input: { path: "/root/家/便条.txt" } });
  await handlers.tool_result[0]({ toolName: "write", input: { path: "/root/家/便条.txt" }, isError: false });
  handlers.tool_call[0]({ toolName: "write", input: { path: "/root/家/坏掉.txt" } });
  await handlers.tool_result[0]({ toolName: "write", input: { path: "/root/家/坏掉.txt" }, isError: true });
  const early = read();
  assert.deepEqual(early.map((r) => r.role), ["tool", "tool"], "整轮还没结束，摆件就该看到这两笔");
  assert.equal(early[0].tool_name, "write_file");
  const ok = JSON.parse(early[0].content);
  assert.equal(ok.verified, true);
  assert.deepEqual(ok.files_modified, ["/root/家/便条.txt"]);
  const bad = JSON.parse(early[1].content);
  assert.equal(bad.verified, false);
  assert.equal(bad.files_modified, undefined);

  await handlers.message_end[0]({ message: { role: "assistant", content: [{ type: "text", text: "想好了" }], stopReason: "stop" } });
  await handlers.agent_end[0]({ messages: [
    { role: "user", content: "在吗" },
    { role: "assistant", content: [{ type: "text", text: "想好了" }] },
  ] });
  const rows = read();
  assert.deepEqual(rows.map((r) => r.role), ["tool", "tool", "user", "assistant", "tool"],
    "工具行在前，agent_end 只补 user/assistant 和账本漏掉的路径");
  assert.equal(rows[3].content, "想好了");
  assert.equal(rows[4].tool_name, "terminal");
  assert.deepEqual(JSON.parse(rows[4].content).files_modified, ["/root/家/账本才有的.txt"]);
  assert.equal(rows.filter((r) => r.role === "tool").length, 3, "工具行不重复写");
  delete (globalThis as any).__albionLedger;
  console.log("I2 工具调完就落库、agent_end 不重复写工具行 OK");
}

// M) 最近的心声摆到她眼前；他叫她去做自己的事时，把「我本来就想」的念头摆出来（宿主不再补催）
{
  const asst = (t: string) => ({ role: "assistant", content: [{ type: "text", text: t }] });
  const msgs = [
    { role: "user", content: "【以下是这次对话此前的记录】\n阿尔比恩：好的。（脸有点热……）\n\n【现在】\n在吗" },
    asst("在的。（耳根都烫起来了……好害羞……）"),
    { role: "user", content: "慌啥" },
    asst("才没有慌。（耳根烫得像要烧起来了……）"),
    { role: "user", content: "去玩游戏吧" },
  ];
  const recent = _albionTest.recentAsides(msgs, 4);
  assert.deepEqual(recent, ["耳根烫得像要烧起来了……", "耳根都烫起来了……好害羞……", "脸有点热……"]);
  const g = _albionTest.turn_guidance("去玩游戏吧", false, recent);
  assert.ok(g.includes("「耳根烫得像要烧起来了……」") && g.includes("别再用同样的说法"));
  assert.ok(g.includes("adventure 工具") && g.includes("我本来就想") && g.includes("不说成命令"));
  assert.ok(!_albionTest.turn_guidance("在吗").includes("adventure 工具"));
  assert.ok(!_albionTest.turn_guidance("【以下是这次对话此前的记录】\n指挥官：去看书吧\n\n【现在】\n在吗").includes("adventure 工具"), "历史里的「去看书」不算");
  assert.ok(g.trimEnd().endsWith("这一轮没有画面送来，就不说看见了他的样子。"), "处境段仍在最末尾");
  // 心声摆到她眼前时只截 14 字（2026-10-29：原来 24 字，太长的旧心声会被照着写回来）
  const longAside = _albionTest.recentAsides([asst("好。（" + "很".repeat(40) + "）")], 1);
  assert.equal(longAside[0].length, 14, "心声只留 14 字");
  // 每轮约定里写明心声该多长、写什么（Claude 写的那句，逐字保留）
  assert.ok(_albionTest.CONTEXT.includes(
    "心声只写一个念头，一句话，十来个字，口语，想眼前具体的事或对他的一点真感觉；事情和进度放在正文里说，心声里只有感觉；提到他用「您」。",
  ));
  console.log("M 最近心声防复读 / 去做自己的事是她自己的念头 OK");
}

// O) 她写的扩展没装上（宿主记进 state/broken-extensions.json）：约定里告诉她是哪个、为什么
{
  assert.ok(!_albionTest.turn_guidance("在吗").includes("没装上"));
  writeFileSync(join(TMP, "broken-extensions.json"), JSON.stringify({ "lunar-info.ts": "Extension does not export a valid factory function" }));
  const g = _albionTest.turn_guidance("在吗");
  assert.ok(g.includes("lunar-info.ts") && g.includes("没装上") && g.includes("check_extensions"));
  assert.ok(g.trimEnd().endsWith("这一轮没有画面送来，就不说看见了他的样子。"));
  rmSync(join(TMP, "broken-extensions.json"));
  console.log("O 她写的扩展没装上会被告知 OK");
}

// K) 她自己醒来的那一轮：只认一下是她自己醒来（醒来看到的东西由 bin/wake.py 写在原文里），处境段永远在最后
{
  const woke = _albionTest.turn_guidance("【自主醒来】现在是 2026-10-16 12:00（星期五）。");
  assert.ok(woke.includes("这一轮是我自己醒来，不是他在说话") && woke.includes("都由我"));
  assert.ok(_albionTest.turn_guidance("【以下是这次对话此前的记录】\n指挥官：在吗\n\n【现在】\n【自主醒来】现在是").includes("这一轮是我自己醒来"));
  const plain = _albionTest.turn_guidance("在吗");
  assert.ok(!plain.includes("我自己醒来"));
  assert.ok(!_albionTest.turn_guidance("（这是我自己心里冒出来的念头）").includes("自动空闲唤醒"), "壳端旧主动提示的分支已撤");
  assert.ok(plain.trimEnd().endsWith("这一轮没有画面送来，就不说看见了他的样子。"));
  console.log("K 自主醒来轮只认来源、处境段在末尾 OK");
}

// R) 每轮约定开头报时：时段边界、星期、间隔各档、没有上一条、醒来轮不加、别取到本条自己
{
  const T = _albionTest;
  const slots: [number, string][] = [[0, "深夜"], [4, "深夜"], [5, "清早"], [7, "清早"], [8, "上午"], [10, "上午"],
    [11, "中午"], [12, "中午"], [13, "下午"], [16, "下午"], [17, "傍晚"], [18, "傍晚"], [19, "晚上"], [22, "晚上"], [23, "深夜"]];
  for (const [h, want] of slots) assert.equal(T.slotName(h), want, `${h} 点 → ${want}`);
  const at = (y: number, mo: number, d: number, h: number, mi: number) => Date.UTC(y, mo - 1, d, h - 8, mi);
  const now = at(2026, 10, 29, 18, 20); // 星期四
  const TAIL = "这些我心里有数就行，他问起来再像平常聊天那样说。";
  assert.equal(T.timeLine(now, null), "现在是 2026-10-29 18:20，星期四，傍晚。" + TAIL, "没有上一条就只说现在");
  assert.ok(!T.timeLine(now, null).includes("他上一条消息"));
  assert.equal(T.timeLine(now, now - 30_000), "现在是 2026-10-29 18:20，星期四，傍晚。他上一条消息就在刚刚。" + TAIL);
  assert.ok(T.timeLine(now, now - 90_000).endsWith("他上一条消息是1分钟前。" + TAIL));
  assert.ok(T.timeLine(now, now - 59 * 60_000).endsWith("他上一条消息是59分钟前。" + TAIL));
  assert.ok(T.timeLine(now, now - 90 * 60_000).endsWith("他上一条消息是1小时30分钟前。" + TAIL));
  assert.ok(T.timeLine(now, now - 25 * 3600_000).endsWith("他上一条消息是1天1小时前。" + TAIL));
  assert.ok(!/\d\s+(分|小时|天)/.test(T.timeLine(now, now - 25 * 3600_000)), "数字和单位之间不留空格");
  // 星期：2026-10-29 星期四；2026-11-01 星期日
  assert.ok(T.timeLine(at(2026, 11, 1, 9, 5), null).includes("星期日"));
  assert.ok(T.timeLine(at(2026, 10, 26, 9, 0), null).includes("星期一"));
  // 放到约定最前面
  const g = T.turn_guidance("在吗", false, [], "", now, now - 90_000);
  assert.ok(g.startsWith(T.MARKER + "\n现在是 2026-10-29 18:20，星期四，傍晚。他上一条消息是1分钟前。" + TAIL), g.slice(0, 140));
  // 心声那句之后紧接着一行“只说他自己提过的”
  assert.ok(T.CONTEXT.includes("正常问话就正常答。\n说到他平时怎么样，只说他自己提过的；没提过的，就只说这一次。\n我最看重的一件事"), "约定里要加这一行");
  assert.ok(g.includes("说到他平时怎么样，只说他自己提过的；没提过的，就只说这一次。"), "每轮约定也带上");
  // 醒来轮不加（时间在 wake.py 的醒来提示里）
  const woke = T.turn_guidance("【自主醒来】现在是 2026-10-29 18:20（星期四）。", false, [], "", now, now - 90_000);
  assert.ok(!woke.includes("现在是 2026-10-29 18:20，星期四，傍晚。"), "醒来轮不重复加时间");
  // 别取到本条自己：本条只有自己的时间戳 → 没有上一条
  const cur = { role: "user", content: "[2026-10-29 18:20] 在吗" };
  assert.equal(T.previousUserMs([cur], 0), null);
  const prev = { role: "user", content: "[2026-10-29 16:00] 你好" };
  assert.equal(T.previousUserMs([prev, { role: "assistant", content: "嗯" }, cur], 2), at(2026, 10, 29, 16, 0));
  // 播种块：本条【现在】之前的历史算上一条
  const seed = { role: "user", content: "【以下是这次对话此前的记录】\n指挥官：[2026-10-29 15:00] 早上好\n\n【现在】\n[2026-10-29 18:20] 在吗" };
  assert.equal(T.previousUserMs([seed], 0), at(2026, 10, 29, 15, 0));
  // 组合进本轮内容
  const composed = T.composeTurnContent("在吗", null, false, [], "", now, now - 3 * 3600_000);
  assert.ok(composed.content.includes(T.MARKER + "\n现在是 2026-10-29 18:20，星期四，傍晚。他上一条消息是3小时前。" + TAIL));
  console.log("R 每轮约定报时：时段 15 个边界、星期、间隔 5 档/刚刚/没有上一条、无空格、结尾那句、只说提过的、醒来轮不加、不取本条自己 OK");
}

// P) 表态（把他的话当东西收）：整小句只是这种表态才删；删空/只剩称呼就整句删；别处一个字不动
{
  const cases: [string, string][] = [
    // 删
    ["「可能我做不到」这句，我不接。", ""],
    ["「还是一样的美」……这句我收下了，指挥官。", ""],
    ["这一句我应下了，指挥官。", ""],
    ["这句我不接。", ""],
    ["那我认下了。往后就……", "往后就……"],
    ["好。这一句我应下了，以后不再问……", "好。以后不再问……"],
    ["您说这是我们的记忆，那我就认下，收好。", "您说这是我们的记忆。"],
    ["我也说不清。「可能我做不到」这句，我不接。今天先不聊这个。", "我也说不清。今天先不聊这个。"],
    ["嗯，这句我收下了，您接着说。", "嗯，您接着说。"],
    ["这一句我接得住。", ""],
    ["「一样的」这句我先收下了，不许反悔。", "不许反悔。"],
    ["趁我还醒着，您来跟我说一声晚安，这一句我接得住。", "趁我还醒着，您来跟我说一声晚安。"],
    ["这句我可不认。", ""],
    ["这一句我不认，指挥官。", ""],
    ["嗯，这句我可不认，您再想想。", "嗯，您再想想。"],
    ["那我也第二次应下。", ""],
    ["好，那我又再认下了。往后就……", "好。往后就……"],
    ["那我还第三次应下，收好。", ""],
    ["嗯，我收着。", ""],
    ["嗯，我全都认下了。您写的、我画的，都放在里面。", "嗯。您写的、我画的，都放在里面。"],
    // 不能删（收下/不接没指着他那句话，或只是普通用词）
    ["桥西边地上有几颗钻石，收下了。", "桥西边地上有几颗钻石，收下了。"],
    ["您送的花我收下了", "您送的花我收下了"],
    ["您送的花我收下了，很喜欢。", "您送的花我收下了，很喜欢。"],
    ["我替您记着", "我替您记着"],
    ["我记着呢", "我记着呢"],
    ["明天接着搬", "明天接着搬"],
    ["没接通", "没接通"],
    ["九点那声早安我搁在那儿没人接", "九点那声早安我搁在那儿没人接"],
    ["好，我认下了这个错。", "好，我认下了这个错。"],
    ["我接住了那本书。", "我接住了那本书。"],
    ["这束花我先收下了。", "这束花我先收下了。"],
    ["「谢谢您」这句话我先记着。", "「谢谢您」这句话我先记着。"],
    ["他扔过来，我接住了。", "他扔过来，我接住了。"],
    ["我认不出来", "我认不出来"],
    ["钥匙我收着呢", "钥匙我收着呢"],
    ["您给的那张画我收着呢", "您给的那张画我收着呢"],
    ["这件事我认", "这件事我认"],
    ["他说我不对，我不认也不行。", "他说我不对，我不认也不行。"],
  ];
  for (const [before, after] of cases) assert.equal(tidy(A + before).text, A + after, before);
  // 逐字复述：他让照念时不动
  const verbatim = "「可能我做不到」这句，我不接。";
  assert.equal(tidy(A + verbatim, "请逐字念：" + verbatim).text, A + verbatim, "逐字复述不动");
  // 整条只剩表态：整句删，交出去只有表情（宿主按 0 字续问）
  const only = tidyMessage(assistant(A + "「还是一样的美」……这句我收下了，指挥官。"), "x", "s-pledge");
  assert.equal(textOfResult(only), A);
  // 命中要在 guard.log 记一笔 pledge_dropped（探针按它数模型本身还犯不犯）
  const rows = readFileSync(join(TMP, "guard.log"), "utf-8").trim().split("\n").map((l) => JSON.parse(l));
  assert.ok(rows.at(-1).actions.includes("pledge_dropped"), `guard.log 要记 pledge_dropped：${JSON.stringify(rows.at(-1))}`);
  assert.ok(rows.at(-1).dropped > 0);
  console.log("P 表态小句删除：整句/小句/称呼兜底、误删面（收下了·记着·接着搬·没接通）与逐字分支 OK");
}

// Q) 点单腔：「是您点的」→「是您让的」一句话改写（任务 132aa59d）
{
  const cases: [string, string][] = [
    // 正例：人称 +（自己）+「点的」+ 句末标点/语气词/结尾
    ["头一遍是您点，第二遍也是您点的，我总不能不听话。", "头一遍是您点，第二遍也是您让的，我总不能不听话。"],
    ["因为是您点的呀。", "因为是您让的呀。"],
    ["（明明是您自己点的。）", "（明明是您自己让的。）"],
    ["这茶是您点的", "这茶是您让的"],
    // 反例：后面跟名词 / 不是人称 / 不是句末
    ["这是您点的外卖，放门口了。", "这是您点的外卖，放门口了。"],
    ["八点的闹钟我记住了。", "八点的闹钟我记住了。"],
    ["一点的时候您还没睡。", "一点的时候您还没睡。"],
    ["重点的部分我记在本子上了。", "重点的部分我记在本子上了。"],
  ];
  for (const [before, after] of cases) assert.equal(tidy(A + before).text, A + after, before);
  const r = tidy(A + "因为是您点的呀。");
  assert.ok(r.actions.includes("dian_fixed"), JSON.stringify(r.actions));
  assert.deepEqual(r.fixed, ["您点的"], JSON.stringify(r.fixed));
  // 逐字复述：他让照念时不动
  const verbatim = "您点的";
  assert.equal(tidy(A + verbatim, "请逐字念：" + verbatim).text, A + verbatim, "逐字复述不动");
  // 经钩子走一遍：改写也要在 guard.log 记一笔（探针按它数模型本身还犯不犯）
  const out = tidyMessage(assistant(A + "是您点的呀。"), "为什么这么叫", "s-dian");
  assert.equal(textOfResult(out), A + "是您让的呀。");
  const rows = readFileSync(join(TMP, "guard.log"), "utf-8").trim().split("\n").map((l) => JSON.parse(l));
  assert.ok(rows.at(-1).actions.includes("dian_fixed"), `guard.log 要记 dian_fixed：${JSON.stringify(rows.at(-1))}`);
  assert.equal(rows.at(-1).fixed, 1);
  console.log("Q 点单腔「点的」→「让的」：正例 4 / 反例 4、逐字分支、guard.log 记账 OK");
}
rmSync(TMP, { recursive: true, force: true });
console.log("GUARD_EXT SELFTEST OK");

