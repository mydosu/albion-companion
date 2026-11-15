# -*- coding: utf-8 -*-
"""翻译首腿单独走 magpie/DS v4.1 的耗时探针（2026-11-15）。

背景：脑换到 agy-cli/gemini-3.8-flash 后翻译跟着它一句 10~13s，指挥官定翻译单独走
`commandcode/deepseek/deepseek-v4.1-flash`。这个探针回答一个问题：**DS 认不认 `reasoning_effort`**，
所以 `TTS_TRANSLATE_MAGPIE_EFFORT` 该设 low 还是 off。

做法：在**自己的进程里** import 工作树的 `tts/irodori/bridge.py`，用 `TTS_TRANSLATE_ROUTE_PROVIDER/MODEL`
把首腿钉在 magpie+DS v4.1（不动生产、不重启任何服务），10 句仓库里现成的中文台词各译两遍。
每句前重置 magpie 的两个冷却，让每句都是干净的首次调用；因此「进了 600s 压思考冷却」= 上游拒了
`reasoning_effort`，这一组不可用。

跑法（需本机 :3425 magpie 在跑）：
    python tests/tts/probes/magpie_effort_probe.py [--env D:/AI/Wife/config/local.env]

前置：`--env` 指向生产用的 env 文件（里面有 TTS_TRANSLATE_KEY_FILE 等；只读，不打印值）。
"""
import argparse
import json
import os
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tts" / "irodori"))

# 仓库里现成的中文材料（tests/tts/eval/ab_stream_v4.py 与 probes/stream_rhythm_probe.py），不另编
LINES = [
    "今天天气不错，我下午去图书馆还书。",
    "你要一起吗？",
    "不急，你忙完再说。",
    "（凑到耳边小声说）别出声，跟我来。",
    "（耳语）就在前面，小心点。",
    "好了，安全了。",
    "不行，别这样。",
    "我说了不要，快住手。",
    "……你先冷静一下。",
    "指挥官，今天辛苦了。实验做完了吗？……做完的话，就早点休息吧。",
]
PROVIDER = "magpie"
MODEL = "commandcode/deepseek/deepseek-v4.1-flash"


def load_env(path):
    """把 env 文件灌进 os.environ（真环境变量优先）；不打印任何值。"""
    if not path:
        return 0
    p = pathlib.Path(path)
    if not p.is_file():
        raise SystemExit("env 文件不存在：%s" % p)
    n = 0
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
        n += 1
    return n


def run_group(bridge, effort):
    bridge.MAGPIE_EFFORT = effort
    rows = []
    for text in LINES:
        bridge._magpie_down_until = 0.0
        bridge._magpie_compress_down_until = 0.0
        t = time.time()
        got = bridge.ask_brain(text)
        dt = time.time() - t
        cooled = bridge._magpie_compress_down_until > t
        rows.append({"text": text, "sec": round(dt, 2), "ok": bool(got), "cooled": cooled,
                     "ja": (got or "")[:24]})
    return rows


def summarize(rows):
    ok = [r for r in rows if r["ok"]]
    secs = [r["sec"] for r in ok]
    return {
        "n": len(rows), "ok": len(ok),
        "cooled": sum(1 for r in rows if r["cooled"]),
        "median": round(statistics.median(secs), 2) if secs else None,
        "max": round(max(secs), 2) if secs else None,
        "min": round(min(secs), 2) if secs else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=str(ROOT / "config" / "local.env"))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    load_env(a.env)
    os.environ["TTS_TRANSLATE_ROUTE_PROVIDER"] = PROVIDER
    os.environ["TTS_TRANSLATE_ROUTE_MODEL"] = MODEL
    import bridge

    print("route=%s / %s  magpie=%s" % (bridge.brain_route()["provider"],
                                        bridge.brain_route()["model"], bridge.MAGPIE_BASE))
    report = {}
    for effort in ("low", "off"):
        print("\n== effort=%s ==" % effort)
        rows = run_group(bridge, effort)
        for i, r in enumerate(rows, 1):
            print("  %2d %5.2fs ok=%-5s cooled=%-5s %s | %s"
                  % (i, r["sec"], r["ok"], r["cooled"], r["text"][:16], r["ja"]))
        report[effort] = {"rows": rows, "summary": summarize(rows)}
        print("  ->", json.dumps(report[effort]["summary"], ensure_ascii=False))

    print("\n== 汇总 ==")
    for effort, d in report.items():
        s = d["summary"]
        print("%-4s 中位 %s / 最大 %s / 成功 %d/10 / 进冷却 %d"
              % (effort, s["median"], s["max"], s["ok"], s["cooled"]))
    if a.out:
        out = pathlib.Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("明细写到", out)


if __name__ == "__main__":
    main()
