/**
 * albion-relay —— 把本机转发做进 Pi 自己。
 *
 * 为什么必须有这一层：上游 `api.commandcode.ai` 在本机建连要 ~10s（其实是 IPv6 DNS 卡住，见下），而 Node/undici 的默认
 * 建连超时正好是 10s ⇒ Pi 直连上游稳定报 `Request timed out.`（实测直连 3/3 失败）。
 * 让它连 localhost 就绕开了这一条。转发自带 `User-Agent: curl/8.5.0`——上游还有 Cloudflare 按 UA 拦。
 *
 * 于是「直接跑 pi 就得到她」，宿主不必再替它开门。
 * 用法：pi -e /root/albion-pi/agent/extensions/albion-relay.ts
 * 端口被占（已有别的转发在跑）就安静跳过，不抢。
 */
import http from "node:http";
import https from "node:https";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const UPSTREAM = new URL("https://api.commandcode.ai/provider/v1");
const PORT = 8798;
const UA = "curl/8.5.0";
const TIMEOUT_MS = 300_000;
// 2026-10-14 查明那 ~10s 是 DNS：这台机器查 IPv6（AAAA）要卡 11s 才超时，只查 IPv4 是 0.2s（建连+TLS 共 0.9s）。
// 原来这里用 fetch，自己也吃 Node 的 10s 建连上限：冷连接必挂（relay: TypeError: fetch failed），换 DS 当主力后
// 宿主重启后的第一个会话两轮全 502。改用 node:https：只走 IPv4、长连接复用。
const agent = new https.Agent({ keepAlive: true, maxSockets: 16 });

let server: http.Server | undefined;

async function handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
	const chunks: Buffer[] = [];
	for await (const chunk of req) chunks.push(chunk as Buffer);
	const body = Buffer.concat(chunks);
	const tail = (req.url ?? "/").replace(/^\/v1/, "");
	const method = req.method ?? "POST";
	const headers: Record<string, string | number> = {
		"content-type": (req.headers["content-type"] as string) ?? "application/json",
		"user-agent": UA,
		accept: (req.headers.accept as string) ?? "application/json",
	};
	if (req.headers.authorization) headers.authorization = String(req.headers.authorization);
	const hasBody = method !== "GET" && method !== "HEAD";
	if (hasBody) headers["content-length"] = body.length;
	const fail = (err: unknown) => {
		if (!res.headersSent) {
			res.writeHead(502, { "content-type": "application/json" });
			res.end(JSON.stringify({ error: { message: `relay: ${String(err)}` } }));
		} else res.destroy();
	};
	const up = https.request(
		{
			protocol: "https:",
			hostname: UPSTREAM.hostname,
			port: 443,
			path: UPSTREAM.pathname.replace(/\/$/, "") + tail,
			method,
			headers,
			agent,
			family: 4,
			timeout: TIMEOUT_MS,
		},
		(upRes) => {
			res.writeHead(upRes.statusCode ?? 502, {
				"content-type": (upRes.headers["content-type"] as string) ?? "application/json",
				"cache-control": (upRes.headers["cache-control"] as string) ?? "no-store",
			});
			upRes.pipe(res);
			upRes.on("error", fail);
			// 下游（pi）先断了：上游这条也撤掉，别占着连接干等 300 秒
			res.on("close", () => {
				if (!upRes.complete) up.destroy();
			});
		},
	);
	// 2026-10-15：`upstream timeout` 曾以 "Unhandled 'error' event on TLSSocket" 把整个 pi 进程带崩（会话 died→重启）。
	// 请求/响应层的错误照旧走 fail；套接字层再兜一个监听，保证错误永远不会无人接。
	up.on("socket", (socket) => socket.on("error", () => {}));
	up.on("timeout", () => up.destroy(new Error("upstream timeout")));
	up.on("error", fail);
	res.on("error", () => {});
	if (hasBody) up.write(body);
	up.end();
}

export default function albionRelay(pi: ExtensionAPI) {
	// 立刻起（不等 session 事件）：`pi -p` 这种一次性运行也常有，模式不该决定通路在不在。
	ensure();

	pi.on("session_start", () => {
		ensure();
	});

	pi.on("session_shutdown", () => {
		server?.close();
		server = undefined;
	});
}

let attempts = 0;

/** 起转发；端口被占就等一会儿再试（别人只是路过用一下的话，得把口拿回来）。 */
function ensure(): void {
	if (server || attempts > 60) return;
	attempts += 1;
	const s = http.createServer((req, res) => {
		void handle(req, res);
	});
	s.on("error", () => {
		// EADDRINUSE 等：先假定已有转发在跑，但每隔 5s 再探一次，免得那个主人先走了。
		// ponytail: 60 次（5 分钟）后放弃；真需要长跑就换成定时器常驻。
		setTimeout(ensure, 5000);
	});
	s.listen(PORT, "127.0.0.1", () => {
		server = s;
		attempts = 0;
	});
}
