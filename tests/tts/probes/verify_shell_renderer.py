"""Opt-in CDP test instrumentation in the existing Electron renderer; no chat/profile access."""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import websockets

CASES = {"aside": "（心想：别让他担心。）我已经回来了。", "silent": "（心想：明天再说。）",
         "long": "こんにちは。今日はいい天気ですね。はい、わかりました。" * 5,
         "cancel": "こんにちは。今日はいい天気ですね。" * 5, "after-cancel": "こんにちは。"}


async def main(args):
    out = Path(args.out).resolve()
    assert "tests" in out.parts and "output" in out.parts
    out.mkdir(parents=True, exist_ok=True)
    targets = json.load(urllib.request.urlopen(args.cdp+"/json/list", timeout=3))
    pages = [x for x in targets if x.get("type") == "page" and "synced-leader=true#/" in x.get("url", "")]
    assert len(pages) == 1, "need the existing shell main renderer"
    bundle = Path(args.bundle).read_text("utf8")
    records = []
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=16*1024*1024) as ws:
        async def evaluate(expression, ident):
            await ws.send(json.dumps(dict(id=ident, method="Runtime.evaluate", params=dict(
                expression=expression, awaitPromise=True, returnByValue=True, userGesture=True))))
            while True:
                response = json.loads(await asyncio.wait_for(ws.recv(), 210))
                if response.get("id") == ident:
                    assert "error" not in response and "exceptionDetails" not in response.get("result", {}), "renderer exception"
                    return response["result"]["result"].get("value")
        for i, (case, text) in enumerate(CASES.items()):
            started = time.monotonic()
            expression = "(async()=>{"+bundle+";return await TtsRendererProbe.runCase("+json.dumps(args.server)+","+json.dumps(text)+","+str(case=="cancel").lower()+");})()"
            data = await evaluate(expression, i+1)
            recording = data.pop("outputGraphBase64", None)
            if recording:
                (out/(case+"-output-graph.webm")).write_bytes(base64.b64decode(recording, validate=True))
            data["elapsed_s"] = round(time.monotonic()-started, 3)
            passed = not data["errors"] and data["contextState"] == "running"
            if case != "cancel":
                passed = passed and data["display"] == text and len(data["decoded"]) == data["stats"]["spansPlayed"]
            else:
                passed = passed and data["cancelled"]
            if case == "long":
                passed = passed and sum(data["decoded"]) > 15
            data["passed"] = bool(passed)
            (out/(case+".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
            record = dict(case=case, passed=bool(passed), seconds=round(sum(data["decoded"]), 3),
                          played=data["stats"]["spansPlayed"], errors=data["errors"])
            records.append(record)
            print(json.dumps(record), flush=True)
    summary = dict(requested=len(records), passed=sum(x["passed"] for x in records),
        failed=sum(not x["passed"] for x in records), cases=records,
        bundle_sha256=hashlib.sha256(bundle.encode()).hexdigest(),
        actual_chat_ui="UNVERIFIED", wasapi_loopback="UNVERIFIED", human_listening="UNVERIFIED")
    (out/"summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    return summary["failed"]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cdp", default="http://127.0.0.1:9335")
    p.add_argument("--server", default="http://127.0.0.1:9885")
    p.add_argument("--bundle", required=True)
    p.add_argument("--out", required=True)
    raise SystemExit(bool(asyncio.run(main(p.parse_args()))))
