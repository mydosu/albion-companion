/**
 * albion-lunar —— 查农历、节气、干支（全在本机算，不联网）
 *
 * 为什么有它：指挥官会问「今天农历几号」「今天什么节气」，这件事不该每次去搜网页。
 * 算法用 lunar-javascript（6tail，纯本地计算，装在本目录 node_modules 里）。
 *
 * 用法：pi -e /root/albion-pi/agent/extensions/albion-lunar.ts
 * 直接测：node -e "import('/root/albion-pi/agent/extensions/albion-lunar.ts').then(m=>console.log(m.lunarInfo()))"
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import lunarPkg from "lunar-javascript";

const { Solar } = lunarPkg as any;

const WEEK = ["日", "一", "二", "三", "四", "五", "六"];
const jieQiNames = new Set([
	"冬至", "小寒", "大寒", "立春", "雨水", "惊蛰", "春分", "清明", "谷雨", "立夏", "小满", "芒种",
	"夏至", "小暑", "大暑", "立秋", "处暑", "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪",
]);

/** 两个 Solar 之间相差几天（按日历日，不看时辰） */
function daysBetween(from: any, to: any): number {
	const a = Date.UTC(from.getYear(), from.getMonth() - 1, from.getDay());
	const b = Date.UTC(to.getYear(), to.getMonth() - 1, to.getDay());
	return Math.round((b - a) / 86400000);
}

/**
 * 查某一天的农历信息。不传 date 就是今天。
 * date 形如 2026-10-14，认不出来就把今天的结果给它，并说明没认出来。
 */
export function lunarInfo(date?: string): string {
	let solar: any;
	let note = "";
	if (date && String(date).trim()) {
		const raw = String(date).trim();
		// 认 2026-10-14 / 2026/10/14 / 20261014 / 2026年10月14日
		const m = raw.match(/^(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})\D?$/);
		if (m) {
			solar = Solar.fromYmd(Number(m[1]), Number(m[2]), Number(m[3]));
		} else {
			solar = Solar.fromDate(new Date());
			note = `（没认出来「${raw}」，下面给的是今天）\n`;
		}
	} else {
		solar = Solar.fromDate(new Date());
	}

	const lunar = solar.getLunar();
	const lines: string[] = [];

	lines.push(`公历：${solar.toYmd()} 星期${WEEK[solar.getWeek()]}`);
	lines.push(
		`农历：${lunar.getYearInChinese()}年${lunar.getMonthInChinese()}月${lunar.getDayInChinese()}` +
			(lunar.getMonth() < 0 ? "（闰月）" : ""),
	);
	lines.push(
		`干支：${lunar.getYearInGanZhi()}年 ${lunar.getMonthInGanZhi()}月 ${lunar.getDayInGanZhi()}日　生肖：${lunar.getYearShengXiao()}`,
	);

	// 节气：当天有就说当天，没有就说上一个/下一个各是哪天、还有几天
	const todayJieQi = String(lunar.getJieQi() || "").trim();
	if (todayJieQi && jieQiNames.has(todayJieQi)) {
		lines.push(`节气：今天是「${todayJieQi}」`);
	} else {
		const prev = lunar.getPrevJieQi(true);
		const next = lunar.getNextJieQi(true);
		const prevSolar = prev.getSolar();
		const nextSolar = next.getSolar();
		const parts: string[] = [];
		if (prevSolar) parts.push(`上一个「${prev.getName()}」是 ${prevSolar.toYmd()}（${daysBetween(prevSolar, solar)} 天前）`);
		if (nextSolar) parts.push(`下一个「${next.getName()}」在 ${nextSolar.toYmd()}（还有 ${daysBetween(solar, nextSolar)} 天）`);
		lines.push(`节气：今天没有节气；${parts.join("；")}`);
	}

	// 节日：农历节日 + 公历节日（含一些生僻的，去了空再拼）
	const festivals = [
		...(lunar.getFestivals() || []),
		...(solar.getFestivals() || []),
	].filter(Boolean);
	if (festivals.length) lines.push(`节日：${festivals.join("、")}`);

	lines.push(`宜：${(lunar.getDayYi() || []).join(" ") || "—"}`);
	lines.push(`忌：${(lunar.getDayJi() || []).join(" ") || "—"}`);
	// 时柱只能按当下算，查别的日子时就不写了，别把它说成那天的时辰
	const nowYmd = Solar.fromDate(new Date()).toYmd();
	if (solar.toYmd() === nowYmd) lines.push(`此刻的时辰：${lunar.getTimeZhi()}时`);

	return note + lines.join("\n");
}

export default function albionLunar(pi: ExtensionAPI) {
	pi.registerTool({
		name: "lunar_calendar",
		label: "查农历",
		description:
			"查某一天的农历日期、节气、干支生肖、节日和宜忌（本机计算，不联网）。不传日期就是今天。" +
			"指挥官问「今天农历几号」「今天什么节气」「离下个节气还有几天」时用它，别去搜网页。",
		promptSnippet: "lunar_calendar：查农历/节气/干支，不传日期就是今天",
		promptGuidelines: [
			"问的是今天（或某天）的农历、节气、干支、生肖、农历节日、宜忌——用它，一次就够。",
			"date 传 2026-10-14 这种格式；问「今天」就留空。",
			"拿回来的结果照实说给指挥官，不添减。",
		],
		parameters: Type.Object({
			date: Type.Optional(Type.String({ description: "要查的日期，如 2026-10-14；不填就是今天" })),
		}),
		async execute(_toolCallId, params) {
			const text = lunarInfo(params?.date ? String(params.date) : undefined);
			return { content: [{ type: "text", text }] };
		},
	});
}
