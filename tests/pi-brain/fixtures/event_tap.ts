/**
 * event_tap —— 探针专用：把 Pi 发的事件按行记下来（不注册任何工具、不改发言）。
 *
 * 用来验"长驻 RPC 会话里每一轮都会发 agent_start"——albion-loopguard 的每轮清零就靠它。
 * 只写 EVENT_TAP_LOG 指定的文件，默认 /tmp/event-tap.log。
 *
 * 用法：pi --mode rpc -e /root/pi-brain-probe/fixtures/event_tap.ts
 */
import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const EVENTS = [
	"session_start",
	"agent_start",
	"turn_start",
	"turn_end",
	"agent_end",
	"agent_settled",
	"message_end",
	"tool_call",
	"tool_result",
];

export default function eventTap(pi: ExtensionAPI) {
	const log = process.env.EVENT_TAP_LOG || "/tmp/event-tap.log";
	for (const name of EVENTS) {
		pi.on(name as any, (event: any) => {
			try {
				appendFileSync(
					log,
					JSON.stringify({
						event: name,
						at: Date.now(),
						role: event?.message?.role,
						stopReason: event?.message?.stopReason,
					}) + "\n",
				);
			} catch {
				/* 记不上不能影响这一轮 */
			}
		});
	}
}
