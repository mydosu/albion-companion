"""离线比较准备阶段；固定延迟替代翻译，不代表真实 API/GPU 或首音频达标。"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tts/irodori"))
import bridge_v2


async def measure(module, rounds, delay):
    def translate(text):
        time.sleep(delay)
        return "一つ目です。" if text.startswith("第一") else "二つ目です。"
    svc = module.PrepareService(translate)
    samples = []
    for i in range(rounds):
        req = module.PrepareRequest("benchmark", 1, str(i), "albion", (0, 6),
                                    "第一。第二。", plan_version=4)
        start = time.perf_counter()
        result = await svc.prepare(req)
        samples.append((time.perf_counter() - start) * 1000)
        assert result.status == "ready"
        assert [s["text"] for s in result.ja_segments] == ["一つ目です。", "二つ目です。"]
        assert not svc._held and not svc._running
    return {"rounds": rounds, "passed": rounds, "p50_ms": statistics.median(samples),
            "min_ms": min(samples), "max_ms": max(samples),
            "peak_in_flight": svc.stats["peak_in_flight"]}


async def main(args):
    spec = importlib.util.spec_from_file_location("baseline_bridge_v2", args.baseline_file)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    before = await measure(baseline, args.rounds, args.delay)
    after = await measure(bridge_v2, args.rounds, args.delay)
    result = {"mode": "offline_fixed_translation_delay", "delay_s": args.delay,
              "before": before, "after": after,
              "p50_reduction_percent": 100 * (1 - after["p50_ms"] / before["p50_ms"]),
              "real_first_audio_target": "UNVERIFIED"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--delay", type=float, default=.05)
    args = parser.parse_args()
    if args.rounds < 1 or args.delay < 0:
        parser.error("rounds >= 1 and delay >= 0 required")
    asyncio.run(main(args))
