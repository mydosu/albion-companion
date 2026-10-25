"""真实 WS/GPU 冒烟；需三个 TTS 服务，会合成固定文本，ACK 模拟播放，不做听审。"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

import httpx
from verify_tts_live import CASES, check_resources, ws_case


async def main(args):
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    cases = ["ja"] * args.rounds
    if args.extended:
        cases += ["silent", "cancel", "ja"]
    if args.chinese:
        CASES["chinese"] = "先关灯。再关门。"
        cases.append("chinese")
    for i, case in enumerate(cases):
        row = {"case": case}
        start = time.monotonic()
        try:
            row.update(await asyncio.wait_for(ws_case(args.ws, case, args.out,
                                                       suffix="%02d-%s" % (i, case)), 60))
            row["passed"] = True
        except Exception as e:
            row.update(passed=False, error_type=type(e).__name__)
        row["elapsed_s"] = round(time.monotonic() - start, 3)
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("case", "passed", "elapsed_s")}), flush=True)
    await asyncio.sleep(3)  # 等已取消的 GPU 归还队列引用。
    async with httpx.AsyncClient(timeout=3) as client:
        resources = (await client.get("http://127.0.0.1:9889/v2/render/stats")).json()
        await check_resources(resources)
        health = []
        for port, path in ((9881, "/v2/health"), (9885, "/health"), (9889, "/health")):
            response = await client.get(f"http://127.0.0.1:{port}{path}")
            response.raise_for_status()
            health.append(bool(response.json()["ok"]))
    ja = [r["first_audio_s"] for r in rows if r["case"] == "ja" and r["passed"]]
    result = {"passed": sum(r["passed"] for r in rows), "failed": sum(not r["passed"] for r in rows),
              "health_passed": sum(health), "health_total": len(health),
              "ja_first_audio_p50_s": statistics.median(ja) if ja else None,
              "resources": resources, "cases": rows, "actual_speaker_playback": "UNVERIFIED",
              "human_listening": "UNVERIFIED"}
    (args.out / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return bool(result["failed"] or not all(health))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ws", default="ws://127.0.0.1:9885/api/v1/audio/speech/ws")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--extended", action="store_true")
    p.add_argument("--chinese", action="store_true")
    args = p.parse_args()
    if args.rounds < 1 or not {"tests", "output"}.issubset(args.out.resolve().parts):
        p.error("rounds >= 1 and output under tests/output required")
    raise SystemExit(asyncio.run(main(args)))
