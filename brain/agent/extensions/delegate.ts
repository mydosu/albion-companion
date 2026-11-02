/**
 * delegate —— 把一件具体的活儿交给独立的 pi 子进程去办，只把它做完的结果拿回来。
 *
 * 这是 Pi 版补回 Hermes 时代 `delegate_task` 的那一格：她本人还是只说给指挥官听的话，
 * 需要查资料 / 跑脚本 / 改文件这类"活儿"时派出去，拿回材料再自己开口。
 * 用法：pi -e /root/albion-pi/agent/extensions/delegate.ts
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { Type } from "typebox";

const WORKER_PROMPT = `你是一名可靠的执行者，替阿尔比恩把交办的活儿做完再回话。
- 只做交办的这一件事，不扩展范围；能查证就查证，不猜、不编。
- 需要看文件、跑命令、写文件就用工具；做完交一份紧凑的结果：结论、要点、涉及的文件路径。
- 交回来的是给阿尔比恩用的材料，不是给用户看的信：不要角色扮演、不要称呼、不要括号心声、不要 emoji。
- 做不到就说清卡在哪、缺什么，不要编一个看起来完成的结果。`;

export default function albionDelegate(pi: ExtensionAPI) {
	pi.registerTool({
		name: "delegate",
		label: "把活儿派出去",
		description:
			"把一件具体的活儿（查资料、跑脚本、核对数据、读写文件）交给一个独立工作进程办，返回它做完的结果。做完之前不返回。",
		promptSnippet: "delegate：把一件具体的活儿交给独立进程办",
		promptGuidelines: [
			"活儿是具体的、能一次做完的（查一份资料、算一组数、核对一份文件）时用 delegate；日常聊天和要她自己拿主意的事不要用。",
			"派之前把要它交回什么说清楚：要结论、要路径、要数字。",
		],
		parameters: Type.Object({
			task: Type.String({ description: "要交办的活儿：目标 + 要交回的东西" }),
			cwd: Type.Optional(Type.String({ description: "工作目录，默认 /root" })),
		}),
		async execute(_toolCallId, params) {
			if ((process.env.ALBION_DELEGATE_DEPTH ?? "0") !== "0") {
				return {
					content: [{ type: "text", text: "（这一层已经在派出去的活儿里了，不再往外派。）" }],
					details: { skipped: "depth" },
				};
			}
			// 模型跟着脑走：不传 --model，pi 就吃 agent/settings.json 的默认值 —— 脑每轮把
			// 自己在用的模型写回那份设置（bin/follow_settings.py）。临时换一次：ALBION_DELEGATE_MODEL。
			const modelArgs = process.env.ALBION_DELEGATE_MODEL ? ["--model", process.env.ALBION_DELEGATE_MODEL] : [];
			const agentDir = process.env.PI_CODING_AGENT_DIR || "/root/albion-pi/agent";
			const proc = spawnSync(
				"pi",
				[
					"-p",
					params.task,
					"--no-session",
					...modelArgs,
					"--tools",
					"read,bash,edit,write",
					"--system-prompt",
					WORKER_PROMPT,
				],
				{
					cwd: params.cwd || "/root",
					encoding: "utf-8",
					timeout: 900_000,
					maxBuffer: 8 * 1024 * 1024,
					// PI_CODING_AGENT_DIR 必须显式带上：不带 pi 会去读 ~/.pi/agent，那是 magpie 给裸 pi 的目录，
					// 不是她的（人格、扩展、models.json 全在 albion-pi/agent 这一份）。
					env: { ...process.env, PI_CODING_AGENT_DIR: agentDir, ALBION_DELEGATE_DEPTH: "1", PI_OFFLINE: "1" },
				},
			);
			const out = (proc.stdout ?? "").trim();
			const err = (proc.stderr ?? "").trim();
			const text = out || (proc.error ? `派出去的活儿没跑起来：${proc.error.message}` : `（没有输出）stderr: ${err.slice(0, 400)}`);
			return { content: [{ type: "text", text }], details: { exit: proc.status } };
		},
	});
}
