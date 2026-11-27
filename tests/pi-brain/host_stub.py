#!/usr/bin/env python3
"""Stand-in for `bin/agy_host.ts` for the offline/real-agy smoke probes.

Serves just enough of the host surface:

  POST /mcp/<session>            Streamable HTTP MCP; tools are proxied to a
                                 mini MCP server (mini_mcp.py) on another port
  POST /turn/<session>/before    {"text","images"} → {"prompt","system_append","text_only"}
  POST /turn/<session>/event     accepts anything, returns {"abort": false}
  POST /turn/<session>/final     {"text"} → {"text"} (identity)
  GET  /health

Usage: python host_stub.py <port> [--mcp <mini mcp port>] [--append TEXT] [--text-only]
"""
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = {"mcp_port": None, "append": "", "text_only": False}


def mcp_forward(payload):
    if not CONF["mcp_port"]:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "result": {"tools": [], "content": [{"type": "text", "text": "no backend"}]}}
    req = urllib.request.Request(
        "http://127.0.0.1:%d/mcp" % CONF["mcp_port"],
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._json(200, {"ok": True, "stub": True})
        self._json(404, {"ok": False})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        body = self._body()
        if path.startswith("/mcp/"):
            msg = body
            method = msg.get("method")
            if method == "initialize":
                return self._json(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agy-host-stub", "version": "1.0"},
                }})
            if method in ("notifications/initialized",) or (msg.get("id") is None and method and method.startswith("notifications/")):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "tools/list":
                if CONF["text_only"]:
                    return self._json(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": []}})
                out = mcp_forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                tools = (out.get("result") or {}).get("tools") or []
                return self._json(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": tools}})
            if method == "tools/call":
                if CONF["text_only"]:
                    return self._json(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
                        "content": [{"type": "text", "text": "这一轮不要调工具。"}], "isError": True}})
                params = msg.get("params") or {}
                out = mcp_forward({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": params.get("name"),
                                              "arguments": params.get("arguments") or {}}})
                return self._json(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                                        "result": out.get("result") or {"content": [], "isError": True}})
            return self._json(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                                    "error": {"code": -32601, "message": "stub: unsupported"}})

        if path.endswith("/before"):
            return self._json(200, {"prompt": body.get("text") or "",
                                    "system_append": CONF["append"],
                                    "text_only": CONF["text_only"],
                                    "session": path.split("/")[2] if len(path.split("/")) > 2 else ""})
        if path.endswith("/event"):
            return self._json(200, {"abort": False})
        if path.endswith("/final"):
            return self._json(200, {"text": body.get("text") or ""})
        self._json(404, {"ok": False})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8661
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--mcp" and i + 1 < len(args):
            CONF["mcp_port"] = int(args[i + 1])
        elif a == "--append" and i + 1 < len(args):
            CONF["append"] = args[i + 1]
        elif a == "--text-only":
            CONF["text_only"] = True
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
