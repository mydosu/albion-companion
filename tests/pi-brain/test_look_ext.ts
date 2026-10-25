/** Offline checks for the request helper used by albion-look. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { requestCapture } from "../../integrations/pi-brain/agent/eyes-http.ts";

const TEST_TOKEN = "unit-test";
const requests: Array<{ url: URL; init?: RequestInit }> = [];
const oldFetch = globalThis.fetch;

try {
	const extension = readFileSync(
		new URL("../../integrations/pi-brain/agent/extensions/albion-look.ts", import.meta.url),
		"utf-8",
	);
	assert.ok(extension.includes('import { requestCapture } from "../eyes-http.ts"'));
	assert.ok(extension.includes("requestCapture(url, path, token, SHOT_TIMEOUT_MS)"));

	globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
		requests.push({ url: new URL(String(input)), init });
		const status = requests.length === 1 ? 200 : 403;
		return new Response(status === 200 ? Buffer.from([0xff, 0xd8, 0xff, 0xd9]) : "unauthorized", {
			status,
			headers: { "content-type": status === 200 ? "image/jpeg" : "text/plain" },
		});
	};

	const success = await requestCapture(
		"http://eyes.invalid/private?legacy=unit-test#old", "/shot", TEST_TOKEN, 1000,
	);
	assert.equal(success.status, 200);
	assert.equal(requests[0].url.pathname, "/private/shot");
	assert.equal(requests[0].url.search, "", "configured query strings must not reach capture requests");
	assert.equal(requests[0].url.hash, "");
	assert.equal((requests[0].init?.headers as Record<string, string>).authorization, `Bearer ${TEST_TOKEN}`);
	assert.equal((requests[0].init?.signal as AbortSignal).aborted, false);

	const denied = await requestCapture(
		"http://eyes.invalid/private?legacy=unit-test#old", "/cam", TEST_TOKEN, 1000,
	);
	assert.equal(denied.status, 403);
	assert.equal(requests[1].url.pathname, "/private/cam");
	assert.equal(requests[1].url.search, "");
	assert.equal((requests[1].init?.headers as Record<string, string>).authorization, `Bearer ${TEST_TOKEN}`);

	console.log("albion-look capture request helper uses query-free URLs and Bearer headers OK");
} finally {
	globalThis.fetch = oldFetch;
}
