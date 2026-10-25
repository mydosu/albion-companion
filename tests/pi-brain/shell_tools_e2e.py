#!/usr/bin/env python3
"""壳插件工具桥的真 Pi 端到端探针：真 server.py + 真 `pi --mode rpc` + 假的 OpenAI 兼容模型，不花钱、不碰生产。

在 WSL 里跑（要有 pi、node、python3）。临时目录里搭一具最小的脑：只装 albion-shell-tools.ts，
宿主端口 18650、假模型 18651，会话名 albion-shell-e2e；生产 8650 那具不动。

验什么（扮演壳发请求）：
  1. 插件工具随请求带来 → 同一轮里模型就看得到；插件提示词各有系统区块，自定义 SYSTEM.md 的工具规则也到模型；
     和她自己的 bash 重名的插件工具叫 shell_bash，非插件的壳工具不过来
  2. 模型调用 → 宿主回 tool_calls → 壳带结果再发 → 她接着说完
  3. 插件卸载 → 下一轮模型看不到它；插件回来 → 又看得到；全程同一个 Pi 进程（没换身子）
  4. 插件执行出错（壳把错误当 tool 结果送回）→ 她照样说完这一轮

用法（仓库在 WSL 里看不到，先把两样拷进去）：
  Windows：Copy-Item -Recurse integrations\\pi-brain \\\\wsl.localhost\\Ubuntu\\tmp\\shell-e2e\\pi-brain
           Copy-Item tests\\pi-brain\\shell_tools_e2e.py \\\\wsl.localhost\\Ubuntu\\tmp\\shell-e2e\\
  WSL：    python3 /tmp/shell-e2e/shell_tools_e2e.py --src /tmp/shell-e2e/pi-brain --out /tmp/shell-e2e/out
退出码 0 = 全部通过；1 = 有断言没过（看 summary.json）；2 = 前置条件不满足。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST_PORT = 18650
MODEL_PORT = 18651
KEY = "shell-e2e-key"
SESSION = "albion-shell-e2e"
PARAMS = {"type": "object", "properties": {"move": {"type": "string"}}, "required": ["move"]}
SEEN = []          # 假模型收到的每个请求：{tools, system, last_role}


class FakeModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps({"object": "list", "data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        msgs = body.get("messages") or []
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        system = "\n".join(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"), ensure_ascii=False)
                           for m in msgs if m.get("role") in ("system", "developer"))
        last = msgs[-1] if msgs else {}
        # Pi 把开跑后才变的工具/规则作为增量消息追加，不一定在 system 里：整份请求都留着查
        SEEN.append({"tools": tools, "system": system, "last_role": last.get("role"),
                     "everything": json.dumps(body, ensure_ascii=False)})
        if last.get("role") == "tool":
            content = last.get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            delta = {"content": "收到：%s" % content}
            finish = "stop"
        elif "play_chess" in tools:
            delta = {"tool_calls": [{"index": 0, "id": "call_fake_%d" % len(SEEN), "type": "function",
                                     "function": {"name": "play_chess", "arguments": json.dumps({"move": "e4"})}}]}
            finish = "tool_calls"
        else:
            delta = {"content": "没有棋盘工具"}
            finish = "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(d, f=None, usage=None):
            obj = {"id": "fake", "object": "chat.completion.chunk", "created": 0, "model": "fake-model",
                   "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
            if usage:
                obj["usage"] = usage
            return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
        self.wfile.write(chunk({"role": "assistant", "content": ""}))
        self.wfile.write(chunk(delta))
        self.wfile.write(chunk({}, finish, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def manifest(tools, prompts=()):
    raw = json.dumps({"v": 1, "tools": list(tools), "prompts": list(prompts)}, ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def tool(name):
    return {"type": "function", "function": {"name": name, "description": "壳里的 %s" % name, "parameters": PARAMS}}


def shell_request(messages, plugin_tools, extra_tools=(), prompts=()):
    body = {"model": "albion", "stream": True, "messages": messages,
            "tools": [tool(n) for n in list(plugin_tools) + list(extra_tools)]}
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % HOST_PORT, method="POST",
                                 data=json.dumps(body, ensure_ascii=False).encode(), headers={
                                     "content-type": "application/json", "authorization": "Bearer " + KEY,
                                     "X-Hermes-Session-Id": SESSION, "X-Albion-Toolset": manifest(plugin_tools, prompts)})
    with urllib.request.urlopen(req, timeout=180) as r:
        lines = [json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith("data: {")]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in lines)
    calls = [tc for c in lines for tc in c["choices"][0]["delta"].get("tool_calls") or []]
    return text, calls, lines[-1]["choices"][0]["finish_reason"] if lines else None


def stage(src, root):
    os.makedirs(os.path.join(root, "bin"))
    os.makedirs(os.path.join(root, "agent", "extensions"))
    os.makedirs(os.path.join(root, "state"))
    for name in ("server.py", "pi_client.py", "shell_tools.py", "wake.py", "check_extensions.ts"):
        shutil.copy(os.path.join(src, "bin", name), os.path.join(root, "bin", name))
    shutil.copy(os.path.join(src, "agent", "shell-tools-bridge.ts"), os.path.join(root, "agent"))
    shutil.copy(os.path.join(src, "agent", "extensions", "albion-shell-tools.ts"), os.path.join(root, "agent", "extensions"))
    with open(os.path.join(root, "agent", "extensions", "albion-prompt-probe.ts"), "w", encoding="utf-8") as f:
        f.write('''export default function probe(pi) {
            for (const [name, exposure, rule] of [
                ["guideline_probe", "direct", "NATIVE_GUIDELINE_MARKER"],
                ["hidden_probe", "hidden", "HIDDEN_GUIDELINE_MARKER"],
            ]) pi.registerTool({ name, label: name, description: "probe", exposure,
                parameters: {type: "object", properties: {}}, promptGuidelines: [rule],
                async execute() { return {content: [{type: "text", text: "ok"}], details: {}}; }
            });
            pi.on("before_agent_start", (event) => {
                event.systemPromptOptions.promptGuidelines.push("EXTRA_GUIDELINE_MARKER");
            });
        }''')
    with open(os.path.join(root, "agent", "SOUL.md"), "w", encoding="utf-8") as f:
        f.write("你是端到端探针用的测试人格。\n")
    with open(os.path.join(root, "agent", "models.json"), "w", encoding="utf-8") as f:
        json.dump({"providers": {"fake": {"baseUrl": "http://127.0.0.1:%d/v1" % MODEL_PORT, "api": "openai-completions",
                                          "apiKey": "fake", "models": [{"id": "fake-model", "name": "fake"}]}}}, f)
    with open(os.path.join(root, "agent", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"transport": "sse", "defaultProvider": "fake", "defaultModel": "fake-model",
                   "retry": {"enabled": False}}, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="integrations/pi-brain 的拷贝")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    for exe in ("pi", "node"):
        if not shutil.which(exe):
            print("missing %s on PATH" % exe)
            return 2
    os.makedirs(args.out, exist_ok=True)
    root = tempfile.mkdtemp(prefix="albion-shell-e2e-")
    stage(args.src, root)

    model = ThreadingHTTPServer(("127.0.0.1", MODEL_PORT), FakeModel)
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()
    env = dict(os.environ, ALBION_BRAIN_PORT=str(HOST_PORT), ALBION_RELAY_PORT=str(MODEL_PORT),
               ALBION_PROVIDER="fake", ALBION_MODEL="fake-model", ALBION_STATE_DIR=os.path.join(root, "state"),
               API_SERVER_KEY=KEY, ALBION_MEMORY_SESSIONS="none", ALBION_SHELL_TOOL_TIMEOUT="30")
    host_log = open(os.path.join(args.out, "host.stdout.log"), "wb")
    host = subprocess.Popen([sys.executable, os.path.join(root, "bin", "server.py")], cwd=root, env=env,
                            stdout=host_log, stderr=subprocess.STDOUT)
    checks, notes = [], {}

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % HOST_PORT, timeout=2)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        prompts = [{"id": "chess", "title": "国际象棋", "content": "新开局不要传 fen。"},
                   {"id": "terminal", "title": "终端插件", "content": "终端规则独立。"}]
        user = {"role": "user", "content": "下一步棋"}

        # 1+2：带插件 → 模型看到 → 调用 → 壳回结果 → 说完
        text, calls, finish = shell_request([user], ["play_chess", "bash"], ["builtIn_debugRandomNumber"], prompts)
        first_seen = SEEN[0] if SEEN else {"tools": [], "system": ""}
        notes["turn1_model_tools"] = first_seen["tools"]
        check("turn1 returns tool_calls", finish == "tool_calls" and [c["function"]["name"] for c in calls] == ["play_chess"],
              json.dumps(calls, ensure_ascii=False))
        check("plugin tool declared to model in the same run", "play_chess" in first_seen["tools"])
        check("conflicting plugin bash exposed as shell_bash, native bash kept",
              "shell_bash" in first_seen["tools"] and "bash" in first_seen["tools"])
        check("non-plugin shell tool not bridged", "builtIn_debugRandomNumber" not in first_seen["tools"])
        with open(os.path.join(args.out, "turn1-model-request.json"), "w", encoding="utf-8") as f:
            f.write(first_seen.get("everything", ""))
        check("toolset prompt reached the model request", "新开局不要传 fen" in first_seen.get("everything", ""))
        request_text = first_seen.get("everything", "")
        check("plugins have independent prompt sections", "albion_shell_plugin_6368657373" in request_text
              and "albion_shell_plugin_7465726d696e616c" in request_text)
        check("custom SYSTEM preserves persona", "你是端到端探针用的测试人格。" in request_text)
        check("custom SYSTEM renders tool guidelines", "NATIVE_GUIDELINE_MARKER" in request_text)
        check("custom SYSTEM renders extra guidelines", "EXTRA_GUIDELINE_MARKER" in request_text)
        check("hidden tool guidelines excluded", "HIDDEN_GUIDELINE_MARKER" not in request_text)
        first_body = json.loads(request_text)
        descriptions = "\n".join(t["function"].get("description", "") for t in first_body.get("tools", []))
        check("plugin guidance does not contaminate tool descriptions", "新开局不要传 fen" not in descriptions
              and "终端规则独立" not in descriptions)
        call = {k: v for k, v in calls[0].items() if k != "index"} if calls else {}
        follow = [user, {"role": "assistant", "content": None, "tool_calls": [call]},
                  {"role": "tool", "tool_call_id": call.get("id"), "content": "棋盘已更新"}]
        text, calls2, finish = shell_request(follow, ["play_chess", "bash"], prompts=prompts)
        check("tool result resumes the same turn", text == "收到：棋盘已更新" and not calls2, text)

        # 3：插件卸载 → 下一轮看不到；回来 → 又看到
        before = len(SEEN)
        text, calls3, _ = shell_request([{"role": "user", "content": "不下了"}], [])
        unload_seen = SEEN[before]["tools"] if len(SEEN) > before else []
        notes["unload_model_tools"] = unload_seen
        check("unloaded plugin hidden next turn", "play_chess" not in unload_seen and "shell_bash" not in unload_seen
              and text == "没有棋盘工具", text)
        before = len(SEEN)
        text, calls4, finish = shell_request([{"role": "user", "content": "再下一盘"}], ["play_chess"])
        check("reloaded plugin visible again", finish == "tool_calls" and len(SEEN) > before and "play_chess" in SEEN[before]["tools"])

        # 4：插件出错 → 错误作为工具结果送回 → 她照样说完
        call = {k: v for k, v in calls4[0].items() if k != "index"} if calls4 else {}
        text, _, _ = shell_request([{"role": "user", "content": "再下一盘"}, {"role": "assistant", "content": None, "tool_calls": [call]},
                                    {"role": "tool", "tool_call_id": call.get("id"), "content": "Error: plugin crashed"}], ["play_chess"])
        check("plugin error delivered as tool result, turn completes", text == "收到：Error: plugin crashed", text)

        with open(os.path.join(root, "logs", "brain.log"), encoding="utf-8") as f:
            brain_log = f.read()
        starts = brain_log.count("started pi session")
        check("same pi process for every turn (no restart)", starts == 1, "started pi session x%d" % starts)
        shutil.copy(os.path.join(root, "logs", "brain.log"), os.path.join(args.out, "brain.log"))
        err = os.path.join(root, "logs", "pi-%s.err" % SESSION)
        if os.path.exists(err):
            shutil.copy(err, os.path.join(args.out, "pi.err"))
    except Exception as exc:
        check("probe ran without exceptions", False, repr(exc))
    finally:
        host.send_signal(signal.SIGINT)
        try:
            host.wait(timeout=20)
        except subprocess.TimeoutExpired:
            host.kill()
        model.shutdown()
        host_log.close()
        left = subprocess.run(["pgrep", "-f", root], capture_output=True, text=True).stdout.split()
        check("no leftover processes", not left, " ".join(left))
        shutil.rmtree(root, ignore_errors=True)

    summary = {"passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks, "notes": notes,
               "model_requests": len(SEEN)}
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    for c in checks:
        print("%s  %s  %s" % ("PASS" if c["ok"] else "FAIL", c["name"], c["detail"][:160]))
    print("%d/%d" % (summary["passed"], summary["total"]))
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
