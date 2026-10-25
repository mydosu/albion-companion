/** Query-free authenticated requests to the local eye capture service. */
export function captureUrl(base: string, path: string): string {
	const endpoint = new URL(base);
	endpoint.pathname = `${endpoint.pathname.replace(/\/+$/, "")}${path}`;
	endpoint.search = "";
	endpoint.hash = "";
	return endpoint.toString();
}

export function requestCapture(
	base: string,
	path: string,
	token: string,
	timeoutMs: number,
): Promise<Response> {
	return fetch(captureUrl(base, path), {
		headers: { authorization: `Bearer ${token}` },
		signal: AbortSignal.timeout(timeoutMs),
	});
}
