"""壳插件工具桥（bin/shell_tools.py + server.py 的接线）离线单测：不起 Pi、不叫模型，只走本机回环 HTTP。

覆盖：清单解析与逐件校验（坏的只跳过那一件）、插件注销后调用被拒、调用 → tool_calls → 结果 → 接着跑的往返、
并发调用一起交给壳、壳不回结果按超时失败、新一轮作废上一轮还在等的调用、晚到结果补交、chat 出错隔离。

跑：python -B -m unittest discover -s tests/pi-brain -p "test_shell_tools.py"
Windows 需先 pip install tzdata（server.py 导入 wake.py 要用时区库，见 tests/README.md）。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "integrations", "pi-brain", "bin"))

import shell_tools  # noqa: E402

KEY = "unit-test-key"
PARAMS = {"type": "object", "properties": {"move": {"type": "string"}}}


def manifest(tools, prompts=()):
    raw = json.dumps({"v": 1, "tools": list(tools), "prompts": list(prompts)}, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def tool(name, params=PARAMS, description="d"):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


class ToolsetParsing(unittest.TestCase):
    def test_only_header_named_tools_are_bridged(self):
        body = {"tools": [tool("play_chess"), tool("builtIn_debugRandomNumber"), tool("web_search")]}
        got = shell_tools.toolset_from(body, {shell_tools.HEADER: manifest(["play_chess"], [
            {"id": "chess:prompt", "title": "国际象棋", "content": "新开局不要传 fen。"}])})
        self.assertEqual([t["name"] for t in got["tools"]], ["play_chess"])
        self.assertEqual(got["prompts"][0]["content"], "新开局不要传 fen。")
        self.assertEqual(got["skipped"], [])

    def test_no_header_means_no_shell_tools(self):
        got = shell_tools.toolset_from({"tools": [tool("play_chess")]}, {})
        self.assertEqual(got, {"tools": [], "prompts": [], "skipped": []})

    def test_one_bad_definition_does_not_drop_the_others(self):
        body = {"tools": [tool("good"), tool("bad_schema", params={"type": "string"}), tool("bad name"),
                          tool("good"), "not-a-tool"]}
        got = shell_tools.toolset_from(body, {shell_tools.HEADER: manifest(["good", "bad_schema", "bad name", "gone"])})
        self.assertEqual([t["name"] for t in got["tools"]], ["good"])
        reasons = {s["name"]: s["reason"] for s in got["skipped"]}
        self.assertIn("not an object schema", reasons["bad_schema"])
        self.assertEqual(reasons["bad name"], "invalid tool name")
        self.assertIn("missing from request", reasons["gone"])
        self.assertTrue(any(s["reason"] == "duplicate name in request" for s in got["skipped"]))

    def test_broken_header_is_ignored_not_fatal(self):
        got = shell_tools.toolset_from({"tools": [tool("play_chess")]}, {shell_tools.HEADER: "%%%not-base64%%%"})
        self.assertEqual(got["tools"], [])
        self.assertTrue(got["skipped"][0]["reason"].startswith("toolset header unreadable"))
        huge = {shell_tools.HEADER: "A" * (shell_tools.MAX_HEADER_CHARS + 1)}
        self.assertEqual(shell_tools.toolset_from({"tools": []}, huge)["skipped"][0]["reason"], "toolset header too large")

    def test_tool_results_take_only_the_trailing_run(self):
        msgs = [{"role": "user", "content": "下棋"},
                {"role": "assistant", "tool_calls": [{"id": "old"}]}, {"role": "tool", "tool_call_id": "old", "content": "旧"},
                {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                {"role": "tool", "tool_call_id": "a", "content": "A"},
                {"role": "tool", "tool_call_id": "b", "content": [{"type": "text", "text": "B"}]}]
        self.assertEqual(shell_tools.tool_results(msgs), {"a": "A", "b": "B"})
        self.assertEqual(shell_tools.tool_results(msgs[:1]), {})


class BridgeTurns(unittest.TestCase):
    def setUp(self):
        self.bridge = shell_tools.Bridge("unit")
        self.toolset = {"tools": [{"name": "play_chess", "description": "d", "parameters": PARAMS}], "prompts": []}

    def test_call_round_trip_then_done(self):
        def brain():
            ok, text = self.bridge.call("play_chess", {"move": "e4"}, timeout=5)
            return "走了：%s %s" % (ok, text)
        turn = self.bridge.start(brain, toolset=self.toolset)
        kind, calls = self.bridge.wait(turn, 5)
        self.assertEqual(kind, "calls")
        self.assertEqual([(c.name, c.args) for c in calls], [("play_chess", {"move": "e4"})])
        self.assertIs(self.bridge.resume({calls[0].id: "e2e4 ok"}), turn)
        self.assertEqual(self.bridge.wait(turn, 5), ("done", "走了：True e2e4 ok"))

    def test_parallel_calls_are_handed_over_together(self):
        def brain():
            out = {}
            ts = [threading.Thread(target=lambda m=m: out.__setitem__(m, self.bridge.call("play_chess", {"move": m}, timeout=5)))
                  for m in ("e4", "d4")]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            return ",".join(sorted(v[1] for v in out.values()))
        turn = self.bridge.start(brain, toolset=self.toolset)
        kind, calls = self.bridge.wait(turn, 5)
        self.assertEqual((kind, len(calls)), ("calls", 2))
        self.bridge.resume({c.id: "ok-" + c.args["move"] for c in calls})
        self.assertEqual(self.bridge.wait(turn, 5), ("done", "ok-d4,ok-e4"))

    def test_unregistered_tool_and_foreign_turn_are_rejected(self):
        self.assertFalse(self.bridge.call("play_chess", {}, timeout=1)[0], "没有正在跑的一轮")
        turn = self.bridge.start(lambda: self.bridge.call("play_chess", {}, timeout=1), toolset={"tools": [], "prompts": []})
        kind, (ok, text) = self.bridge.wait(turn, 5)
        self.assertEqual(kind, "done")
        self.assertFalse(ok)
        self.assertIn("不在了", text)

    def test_shell_that_never_answers_times_out_as_failure(self):
        turn = self.bridge.start(lambda: self.bridge.call("play_chess", {}, timeout=0.3), toolset=self.toolset)
        self.assertEqual(self.bridge.wait(turn, 5)[0], "calls")
        kind, (ok, text) = self.bridge.wait(turn, 5)
        self.assertEqual(kind, "done")
        self.assertFalse(ok)
        self.assertIn("秒内回", text)
        # 晚到的结果对不上任何调用；那一轮的话已经交过了，不再补交
        self.assertIsNone(self.bridge.resume({"call_shell_late": "x"}))
        self.assertIsNone(self.bridge.orphan_text())

    def test_new_round_from_bridging_shell_abandons_waiting_calls(self):
        first = self.bridge.start(lambda: self.bridge.call("play_chess", {}, timeout=30), toolset=self.toolset)
        self.assertEqual(self.bridge.wait(first, 5)[0], "calls")
        started = time.time()
        second = self.bridge.start(lambda: "第二轮", toolset=self.toolset, supersede=True)
        self.assertLess(time.time() - started, 5, "作废后上一轮应立即收尾，而不是等满调用超时")
        ok, text = first.final
        self.assertFalse(ok)
        self.assertIn("作废", text)
        self.assertEqual(self.bridge.wait(second, 5), ("done", "第二轮"))

    def test_non_bridging_client_queues_behind_running_turn(self):
        gate = threading.Event()
        first = self.bridge.start(lambda: gate.wait(5) and "第一轮", toolset=self.toolset)
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("turn", self.bridge.start(lambda: "第二轮", toolset={"tools": [], "prompts": []})))
        t.start()
        time.sleep(0.2)
        self.assertTrue(self.bridge.has_tool("play_chess"), "排队的请求不能在上一轮跑完前撤掉它的工具")
        gate.set()
        t.join(5)
        self.assertEqual(self.bridge.wait(first, 5), ("done", "第一轮"))
        self.assertEqual(self.bridge.wait(box["turn"], 5), ("done", "第二轮"))
        self.assertFalse(self.bridge.has_tool("play_chess"))

    def test_brain_error_is_reported_not_raised(self):
        def boom():
            raise RuntimeError("pi died")
        turn = self.bridge.start(boom, toolset=self.toolset)
        kind, exc = self.bridge.wait(turn, 5)
        self.assertEqual(kind, "error")
        self.assertIsInstance(exc, RuntimeError)

    def test_publish_version_changes_only_when_toolset_changes(self):
        v1 = self.bridge.publish(self.toolset)
        self.assertEqual(self.bridge.publish(dict(self.toolset)), v1)
        self.assertEqual(self.bridge.publish({"tools": [], "prompts": []}), v1 + 1)


class HostHttpRoundTrip(unittest.TestCase):
    """真 server.Brain 处理器 + 回环 HTTP：壳的请求、扩展的阻塞调用、SSE 里的 tool_calls 都走真实格式。"""

    @classmethod
    def setUpClass(cls):
        os.environ["API_SERVER_KEY"] = KEY
        import server
        cls.server_mod = server
        cls._orig_chat, cls._orig_log = server.chat, server.log
        server.log = lambda *a, **k: None          # 不往仓库里的 logs/brain.log 写
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Brain)
        cls.httpd.daemon_threads = True
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.server_mod.chat, cls.server_mod.log = cls._orig_chat, cls._orig_log
        os.environ.pop("API_SERVER_KEY", None)

    def post(self, path, body, headers=None, key=KEY):
        h = {"content-type": "application/json", "authorization": "Bearer " + key, **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def get(self, path, key=KEY):
        req = urllib.request.Request(self.base + path, headers={"authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None

    def fake_pi(self, session):
        """像 Pi 那样：开跑前取清单，调用壳工具，再把结果说出来。"""
        def chat(session_id, msgs):
            status, snap = self.get("/albion/shell-tools?session=" + session)
            assert status == 200, status
            names = [t["name"] for t in snap["tools"]]
            if "play_chess" not in names:
                return "（没有棋盘工具：%s）" % names
            status, raw = self.post("/albion/shell-tools/call", {"session": session, "name": "play_chess",
                                                                 "arguments": {"move": "e4"}})
            out = json.loads(raw)
            return "我走了 e4，壳说：%s/%s" % (out["ok"], out["text"])
        return chat

    def test_stream_round_trip_and_unload(self):
        session = "albion-unit-stream"
        self.server_mod.chat = self.fake_pi(session)
        hdr = {"X-Hermes-Session-Id": session, shell_tools.HEADER: manifest(["play_chess"])}
        user = {"role": "user", "content": "下棋"}
        status, sse = self.post("/v1/chat/completions", {"stream": True, "messages": [user],
                                                        "tools": [tool("play_chess"), tool("builtIn_debugRandomNumber")]}, hdr)
        self.assertEqual(status, 200)
        chunks = [json.loads(line[6:]) for line in sse.splitlines() if line.startswith("data: {")]
        calls = [tc for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])]
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual([(c["index"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls],
                         [(0, "play_chess", {"move": "e4"})])

        follow = [user, {"role": "assistant", "content": None, "tool_calls": [{k: v for k, v in calls[0].items() if k != "index"}]},
                  {"role": "tool", "tool_call_id": calls[0]["id"], "content": "{\"board\":\"ok\"}"}]
        status, sse = self.post("/v1/chat/completions", {"stream": True, "messages": follow,
                                                        "tools": [tool("play_chess")]}, hdr)
        text = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                       for line in sse.splitlines() if line.startswith("data: {"))
        self.assertEqual(text, "我走了 e4，壳说：True/{\"board\":\"ok\"}")

        # 插件卸载：下一轮请求里没有它，她那边清单随之清空，不用重启
        status, sse = self.post("/v1/chat/completions", {"stream": False, "messages": [user], "tools": []},
                                {"X-Hermes-Session-Id": session, shell_tools.HEADER: manifest([])})
        self.assertEqual(json.loads(sse)["choices"][0]["message"]["content"], "（没有棋盘工具：[]）")

    def test_non_stream_tool_calls_and_auth(self):
        session = "albion-unit-json"
        self.server_mod.chat = self.fake_pi(session)
        hdr = {"X-Hermes-Session-Id": session, shell_tools.HEADER: manifest(["play_chess"])}
        status, raw = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "下棋"}],
                                                        "tools": [tool("play_chess")]}, hdr)
        choice = json.loads(raw)["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        # 扩展端点同样要宿主的 key
        self.assertEqual(self.get("/albion/shell-tools?session=" + session, key="wrong")[0], 401)
        self.assertEqual(self.post("/albion/shell-tools/call", {"session": session, "name": "play_chess"}, key="wrong")[0], 401)
        status, raw = self.post("/v1/chat/completions", {"messages": [
            {"role": "user", "content": "下棋"}, {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": "Error: plugin crashed"}]}, hdr)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "我走了 e4，壳说：True/Error: plugin crashed")

    def test_stale_tool_result_gets_empty_reply_not_a_new_turn(self):
        session = "albion-unit-stale"
        ran = []
        self.server_mod.chat = lambda sid, msgs: ran.append(sid) or "不该跑"
        status, raw = self.post("/v1/chat/completions", {"messages": [
            {"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "call_shell_unknown", "content": "late"}]},
            {"X-Hermes-Session-Id": session})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "")
        self.assertEqual(ran, [])

    def test_brain_error_still_maps_to_502(self):
        session = "albion-unit-error"

        def broken(sid, msgs):
            raise self.server_mod.PiError("pi process is not running")
        self.server_mod.chat = broken
        status, raw = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "在吗"}]},
                                {"X-Hermes-Session-Id": session})
        self.assertEqual(status, 502)
        self.assertIn("pi process is not running", raw)


if __name__ == "__main__":
    unittest.main()
