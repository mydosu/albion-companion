/**
 * albion-shell-tools —— 桌面壳里插件提供的工具，她这边也能用（2026-10-20）。
 *
 * 插件在壳里随时加载、卸载；壳每次发话都带上当时的插件工具清单，宿主记下来。
 * 这里每轮开跑前去宿主取一次清单：新来的注册成代理工具，没了的撤下，不用换身子。
 * 她调用代理工具时，宿主把调用交给壳执行，等壳回结果（超时按失败）。细节见 ../shell-tools-bridge.ts。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { hostBridgeIo, ShellToolBridge } from "../shell-tools-bridge.ts";

const CALL_TIMEOUT_S = Number(process.env.ALBION_SHELL_TOOL_TIMEOUT || "120");

export default function albionShellTools(pi: ExtensionAPI) {
	const bridge = new ShellToolBridge(
		pi,
		hostBridgeIo({
			base: `http://127.0.0.1:${process.env.ALBION_BRAIN_PORT || "8650"}`,
			session: process.env.ALBION_SESSION_ID || "albion-desktop",
			key: process.env.API_SERVER_KEY || "",
			fetchTimeoutMs: 5_000,
			// 比宿主那边的等待多留一点，让宿主先按超时交回明确的失败原因
			callTimeoutMs: (CALL_TIMEOUT_S + 15) * 1000,
		}),
	);
	let lastVersion: number | undefined;

	pi.on("before_agent_start", async (event) => {
		const report = await bridge.refresh();
		bridge.applyPromptSections(event.systemPromptOptions);
		// stdout 是 RPC 协议通道，诊断只写 stderr（宿主收进 logs/pi-<会话>.err）；清单没变就不写
		if (report.error || report.skipped.length || report.version !== lastVersion) {
			console.error(`[albion-shell-tools] ${JSON.stringify(report)}`);
		}
		lastVersion = report.version;
	});
}
