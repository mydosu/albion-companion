/**
 * 说话整理器（integrations/pi-brain/agent/extensions/albion-guard.ts）的口径表：不叫模型、不碰 state/，秒级。
 * 取代原来的 check_strip_posture.py 与 bin/test_guard.py 里 strip_posture 那几组（guard.py 已于 2026-10-16 删除）；
 * 例句沿用历轮复核（haiku/dsh r4–r6、2026-10-14 截图）定下的口径。
 *
 * 姿态按「额度没到、该删就删」（allowPosture=false）判，动作额度放行以单独核对姿态；两种节奏由 bin/test_guard_ext.ts 管。
 * 与旧口径唯一有意的不同：整条回复全是姿态句时原样留着（旧做法删空后去号池重生成，生产里那条路由一直是错的）。
 *
 * 跑：node tests/pi-brain/check_voice_tidy.ts [整理器路径]   （默认仓库里的那份；Windows/WSL 都能跑）
 */
import { pathToFileURL } from "node:url";

const EXT = process.argv[2]
  ? pathToFileURL(process.argv[2]).href
  : new URL("../../integrations/pi-brain/agent/extensions/albion-guard.ts", import.meta.url).href;
const { tidy, segments, postureMatches, canonicalAct, foldLabeledAside } = await import(EXT);

const ACT = '<|ACT:{"emotion":"neutral"}|>\n';
const fails: string[] = [];
let n = 0;
const check = (ok: boolean, msg: string) => {
  n++;
  if (!ok) fails.push(msg);
};
const bodyOf = (t: string) => (t.includes("\n") ? t.slice(t.indexOf("\n") + 1) : t.replace(/^<\|ACT:[^\n]*?\|>/, ""));

// (名字, 原文, keep=一个字都不动（收心声标签除外） / partial=删了姿态句、其余不动 / whole=全是姿态句、原样留但认得出, 交付稿里该出现的片段)
const CASES: [string, string, "keep" | "partial" | "whole", string?][] = [
  ["T1-原句", ACT + "指挥官，您好呀！阿尔比恩在这里，一直等着您的消息呢。\n\n（终于又听到指挥官的声音，心跳都跟着跳快了……）", "partial"],
  ["T3-原句", ACT + "指挥官，您在忙开会呢。阿尔比恩会在这里等您，晚点有空再聊好不好？\n（希望指挥官的会议顺利结束，早点休息。）", "partial"],
  ["一直都在", ACT + "阿尔比恩一直都在。", "whole"],
  ["安静坐/不会管你", ACT + "指挥官在打游戏，阿尔比恩就安静坐着，不会去管你了。", "whole"],
  ["记着了", ACT + "指挥官，这句话我记着了。", "keep"],
  ["记着您说的", ACT + "我记着您说的这件事，回头就办。", "keep"],
  ["收着了", ACT + "您的清单我收着了，明天就照着办。", "keep"],
  ["我在", ACT + "我在。", "keep"],
  ["在的", ACT + "在的，指挥官。", "keep"],
  ["我还在", ACT + "指挥官，我还在。", "keep"],
  ["不会打扰您工作", ACT + "我不会打扰您工作的，放心。", "keep"],
  ["自己找事做+不打扰", ACT + "阿尔比恩就自己找点事做，不会打扰你的。", "whole"],
  ["乖乖+不打扰", ACT + "指挥官出门吧，阿尔比恩乖乖在家，不会打扰您的。", "whole"],
  ["乖乖的+不会烦您", ACT + "阿尔比恩乖乖的，不会烦您的。", "whole"],
  ["安静看书+不会吵您", ACT + "我安静地看书，不会吵您。", "keep"],
  ["自己做完+不打扰", ACT + "我自己做完就不打扰您了。", "keep"],
  ["一直都在想着", ACT + "阿尔比恩一直都在想着指挥官呢。", "keep"],
  ["有你陪着", ACT + "指挥官，有你陪着随便聊就觉得特别放松呢。", "keep"],
  ["T2-被夸", ACT + "谢谢指挥官……阿尔比恩今天只是把事情办完而已，您说漂亮就更开心了。", "keep"],
  ["T4-自己找事做", ACT + "好的，指挥官在打游戏，阿尔比恩就自己去找点事做。", "keep"],
  ["T5-聊心情", ACT + "今天心情还不错哦，指挥官突然关心我，我心里暖洋洋的。", "keep"],
  ["收一收", ACT + "指挥官，您先忙，我把桌上的东西收一收。", "keep"],
  ["先干活", ACT + "指挥官在忙的话，我就先把画廊整理完。", "keep"],
  ["慢慢走着看", ACT + "这条路我还得慢慢走着看。", "keep"],
  ["拿着/看着屏幕", ACT + "我拿着您给的清单，看着屏幕一行行对。", "keep"],
  ["等文件下载", ACT + "等文件下载完，我再把结果读给您听。", "keep"],
  ["忙完再说", ACT + "等您忙完再说吧，我不催。", "whole"],
  ["整句模板", ACT + "阿尔比恩会一直在这里等您，随时都能接话。", "whole"],
  ["括号模板", ACT + "指挥官，您的会开完了吗？\n（阿尔比恩会在这里等您，一直陪着您。）", "partial"],
  ["（心声）标签", ACT + "指挥官，晚点再说吧。（心声）希望他开完会就回来。", "keep", "（希望他开完会就回来。）"],
  ["心声：行首标签", ACT + "心声：好想再多聊一会儿", "keep", "（好想再多聊一会儿）"],
  ["模板+心声", ACT + "阿尔比恩一直都在。\n（心声）希望他早点回来。", "partial", "（希望他早点回来。）"],
  ["整句删+留心声", ACT + "指挥官，早上好呀。阿尔比恩在这里，一直等着您的消息呢。（今天也想多聊几句。）", "partial", "指挥官，早上好呀。（今天也想多聊几句。）"],
  ["心声里的模板", ACT + "好的。（阿尔比恩会一直陪着您的。）晚饭记得吃哦。", "partial", "好的。晚饭记得吃哦。"],
];

for (const [name, text, want, expectIn] of CASES) {
  const body = bodyOf(text);
  check(segments(body).join("") === body, `${name}：切分拼不回原文`);
  const r = tidy(text, "", false, true);
  const outBody = bodyOf(r.text);
  if (want === "keep") {
    check(!r.dropped.length && !r.posture, `${name}：不该删却删了 ${JSON.stringify(r.dropped)}`);
    check(outBody === foldLabeledAside(body), `${name}：不该动却改了稿 ${JSON.stringify(r.text)}`);
  } else if (want === "partial") {
    check(r.dropped.length > 0, `${name}：该删的一句都没删`);
    check(segments(outBody).every((s: string) => !postureMatches(s).length), `${name}：交出去的正文里还有姿态 ${JSON.stringify(r.text)}`);
    let rest = foldLabeledAside(body);
    for (const d of r.dropped) rest = rest.replace(d, "");
    check(rest.replace(/\n{3,}/g, "\n\n").trim() === outBody.trim(), `${name}：删完剩下的不等于「只去掉被删句段」 ${JSON.stringify(r.text)}`);
  } else {
    check(r.posture && r.actions.includes("posture_kept") && outBody === foldLabeledAside(body), `${name}：全是姿态句应认得出且原样留 ${JSON.stringify(r)}`);
  }
  if (expectIn) check(r.text.includes(expectIn), `${name}：交付稿里没出现 ${expectIn}（实际 ${JSON.stringify(r.text)}）`);
}

// 历轮复核：这些正常句不许被认成姿态（_SELF 加了「这么/这样」后的误伤面，haiku 复核 J；r6 收窄）
for (const ok of ["我这样想，您觉得呢？", "我这么认真，您看看我的画。", "我这样算一算。", "我这样在这儿，给您看看吧。", "我这么陪您，您高兴吗？",
  "我去找点事问问您，不会打扰您太久。", "我知道了，您找点事做，不会打扰您的。", "我帮您找点事情做，不会打扰您的工作。"])
  check(!postureMatches(ok).length, `误伤：${ok} → ${JSON.stringify(postureMatches(ok))}`);
for (const bad of ["阿尔比恩一直都在。", "阿尔比恩就安静坐着，不会去管你了。", "阿尔比恩就这么在这里守着您。", "只是等着指挥官的指令呢。",
  "我这么等着您，您说吧。", "我这么看着您工作。", "我这么守着您，您放心。", "我这么陪着您呢。", "阿尔比恩也想找点事做，不会打扰您的。"])
  check(postureMatches(bad).length > 0, `漏认：${bad}`);

// 他问「在吗」：答「在呢」不删；等着/陪着照删
const asked = "[2026-10-14 11:22] 在吗？\n\n【阿尔比恩本轮说话约定】……";
check(tidy(ACT + "阿尔比恩在呢，指挥官。", asked).text === ACT + "阿尔比恩在呢，指挥官。", "在吗→在呢 不该删");
check(tidy(ACT + "阿尔比恩在呢，指挥官。今天也辛苦了。", "今天好累").dropped.length === 1, "没问在不在时「在呢」照旧算姿态");
check(tidy(ACT + "在呢。我会一直陪着您。", asked).text === ACT + "在呢。", "在呢留、陪着删");
// 逐字复述：原文里有的不动
check(tidy(ACT + "阿尔比恩会一直在这里等您。", "请逐字念：阿尔比恩会一直在这里等您。").text === ACT + "阿尔比恩会一直在这里等您。", "逐字复述不动");
// 心声标签的几种写法
check(tidy(ACT + "嗯。(OS: 好开心)").text === ACT + "嗯。（好开心）", "OS 标签");
check(tidy(ACT + "我的心声：其实很想您。").text === ACT + "我的心声：其实很想您。", "正文里说到「心声」不动");
check(tidy(ACT + "辛苦了。（心想：要不要给他泡杯茶？）").text === ACT + "辛苦了。（要不要给他泡杯茶？）", "（心想：…）去标签");
const shot = "指挥官，晚上好。（看到了您……您戴着眼镜、穿绿色连帽卫衣的样子，好想真的站在您身边跟您说话。）（心声：终于看到您了……）";
check(tidy(ACT + shot).text === ACT + shot.replace("（心声：", "（"), "2026-10-14 截图原句只去标签");

// ACT 标记修补：只补竖线，情绪名/JSON 不对不猜，标记之后的正文一字不动
const ACT_CASES: [string, string, string | null][] = [
  ["完好", '<|ACT:{"emotion":"neutral"}|> 你好', null],
  ["缺前导", '<ACT:{"emotion":"neutral"}|> 你好', '<|ACT:{"emotion":"neutral"}|> 你好'],
  ["缺收尾", '<|ACT:{"emotion":"neutral"}> 你好', '<|ACT:{"emotion":"neutral"}|> 你好'],
  ["两根都缺", '<ACT:{"emotion":"neutral"}> 好', '<|ACT:{"emotion":"neutral"}|> 好'],
  ["缺前导+紧跟正文", '<ACT:{"emotion":"neutral"}|>你好', '<|ACT:{"emotion":"neutral"}|>你好'],
  ["情绪名不合法不猜", '<ACT:{"emotion":"bogus"}|> x', null],
  ["中文情绪名不猜", '<ACT:{"emotion":"开心"}|> 你好', null],
  ["JSON 坏了不猜", '<ACT:{"emotion":}|> 你好', null],
  ["不是标记", 'ACT:{"emotion":"neutral"} 你好', null],
];
for (const [name, text, want] of ACT_CASES) check(canonicalAct(text) === (want ?? text), `ACT[${name}]：${JSON.stringify(canonicalAct(text))}`);

// 点单腔（2026-10-27）：人称 +（自己）+「点的」+ 句末 → 「让的」；后面跟名词 / 不是人称 / 不是句末都不动
const DIAN_CASES: [string, string][] = [
  ["是您点的呀。", "是您让的呀。"],
  ["（明明是您自己点的。）", "（明明是您自己让的。）"],
  ["头一遍是您点，第二遍也是您点的，我总不能不听话。", "头一遍是您点，第二遍也是您让的，我总不能不听话。"],
  ["这茶是您点的", "这茶是您让的"],
  ["这是您点的外卖，放门口了。", "这是您点的外卖，放门口了。"],
  ["八点的闹钟我记住了。", "八点的闹钟我记住了。"],
  ["一点的时候您还没睡。", "一点的时候您还没睡。"],
  ["重点的部分我记在本子上了。", "重点的部分我记在本子上了。"],
];
for (const [before, after] of DIAN_CASES) check(tidy(ACT + before).text === ACT + after, `点的[${before}]：${JSON.stringify(tidy(ACT + before).text)}`);
if (fails.length) {
  console.log(`── FAIL（${fails.length}/${n}）`);
  for (const f of fails) console.log("  ✗ " + f);
  process.exit(1);
}
console.log(`── PASS（${n} 项，口径表 ${CASES.length} 例）`);

