#!/usr/bin/env python3
"""Two real SSE conversations on the existing production brain; see README.md."""

import argparse
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    key = ""
    for line in Path("/root/albion-pi/agent/.env").read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "API_SERVER_KEY":
            key = value.strip().strip("\"'")
    session = "albion-probe-b6-" + time.strftime("%Y%m%d-%H%M%S")
    prompts = ["刚忙完，想跟你聊一会儿。今天你过得怎么样？用一两句话回我就好。",
               "我准备泡杯热茶，窗外很安静。你会选什么味道？简单聊两句就好。"]
    rows = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for text in prompts:
        body = {"model": "albion", "session_id": session, "stream": True,
                "messages": [{"role": "user", "content": text}]}
        request = urllib.request.Request("http://127.0.0.1:8650/v1/chat/completions",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        row = {"session": session, "turn": len(rows) + 1, "first_event_s": None,
               "first_content_s": None, "first_body_s": None, "done": False}
        content = ""
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                row.update(http=response.status, headers_s=round(time.perf_counter() - started, 3))
                for raw in response:
                    if not raw.startswith(b"data: "):
                        continue
                    elapsed = round(time.perf_counter() - started, 3)
                    if row["first_event_s"] is None:
                        row["first_event_s"] = elapsed
                    data = raw[6:].strip()
                    if data == b"[DONE]":
                        row["done"] = True
                        break
                    event = json.loads(data)
                    delta = event["choices"][0]["delta"].get("content") or ""
                    if delta and row["first_content_s"] is None:
                        row["first_content_s"] = elapsed
                    content += delta
                    plain = re.sub(r"<\|ACT:[^\n]*?\|>", "", content).strip()
                    if plain and row["first_body_s"] is None:
                        row["first_body_s"] = elapsed
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            row.update(http=getattr(exc, "code", 0), error_class=type(exc).__name__)
        row.update(total_s=round(time.perf_counter() - started, 3),
                   reply_prefix=re.sub(r"<\|ACT:[^\n]*?\|>", "", content).strip()[:50])
        rows.append(row)
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row, ensure_ascii=False), flush=True)
        assert row["http"] == 200 and row["done"] and row["first_body_s"] is not None, "SSE conversation failed"
    print("LIVE LATENCY PROBE OK (2/2)")


if __name__ == "__main__":
    main()
