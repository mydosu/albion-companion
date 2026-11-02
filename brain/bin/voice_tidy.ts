/**
 * voice_tidy —— 给 Python 探针（tests/pi-brain/check_speech.py）用的命令行壳：规则直接 import 整理器，不另写一份。
 *
 * stdin：一行一个 JSON {"text": "...", "source": "..."}；stdout：一行一个 JSON
 *   {"posture": [这条正文里命中的陪伴/等候姿态], "tidy": {text, actions, dropped}（不放过姿态时会交出去的样子）}
 * 不记账、不写日志、不碰 state/。
 *
 * 跑：node bin/voice_tidy.ts < rows.jsonl
 */
import { readFileSync } from "node:fs";
import { postureMatches, segments, tidy } from "../agent/extensions/albion-guard.ts";

for (const line of readFileSync(0, "utf-8").split("\n")) {
	if (!line.trim()) continue;
	const { text = "", source = "" } = JSON.parse(line);
	const body = String(text).replace(/^<\|ACT:[^\n]*?\|>/, "");
	const posture = segments(body).flatMap((s) => postureMatches(s));
	const r = tidy(String(text), String(source), false);
	console.log(JSON.stringify({ posture, tidy: { text: r.text, actions: r.actions, dropped: r.dropped } }));
}
