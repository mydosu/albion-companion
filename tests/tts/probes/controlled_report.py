# -*- coding: utf-8 -*-
"""受控描述离线评测的汇总表（阶段 3d）。

读 `emotion_bench.py` 产出的 `metrics.json`，按场景对比：
  * `anc`（现在的生产锚路径）相对同句 `neutral_quiet` 的 Δ；
  * `bridge`（桥自动写的受控描述，`caption_mode=free`）相对同一基线的 Δ；
  * **F0 抖动（f0_std）**：这一轮最关心的量（ck500 快哭弱就是颤音被磨平）。
另外报坏音条数、可测条数（同 bench 口径：|均值| 超过噪声底与最小阈值，且两个 seed 同号）。

用法（仓库根）：
    tts/irodori/.venv/Scripts/python.exe -B tests/tts/probes/controlled_report.py \\
        --metrics D:/AI/Wife/tests/output/tts-controlled-caption-20261026/metrics.json
"""
import argparse
import json

# 最小可辨阈值（与 emotion_bench / 前几轮报告同一口径）
MIN = {"dur": 0.15, "f0": 10.0, "dyn": 2.0, "rate": 0.3, "f0std": 10.0}

# 同上一次的 ck500 数字（tests/output/tts-emotion-scene-20261026/REPORT.md §3.1/§3.2），
# 用来确认这一轮跑的还是同一份资产、同一口径（锚路径应当复现）。
PREV_CK500_ANC = {
    "sad": (-0.29, 2), "whi": (0.01, -5), "coq": (-0.59, -1), "cry": (-0.23, 4),
    "exc": (-0.99, -2), "ang": (-0.45, 13), "gen": (0.24, 0),
}


def load(path):
    payload = json.loads(open(path, encoding="utf8").read())
    rows = payload["rows"]
    by_id = {r["id"]: r for r in rows}
    return payload, rows, by_id


def scene_of(row):
    """从 id 取场景：`sad1-anc-sad_low-777` → sad。"""
    return row["id"].split("-", 1)[0].rstrip("0123456789")


def base_of(row, by_id):
    base = by_id.get(row.get("base_id") or "")
    return base


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def group_of(r):
    """归类：锚条件在矩阵里是 `group=base` + id 带 `-anc-`（上一轮的写法），按 id 认。"""
    if "-anc-" in r["id"]:
        return "anc"
    if r.get("group") == "bridge":
        return "bridge"
    return "base"


def per_sentence(rows, by_id, group):
    """(scene, sentence) → {seed: row}。"""
    out = {}
    for r in rows:
        if group_of(r) != group:
            continue
        out.setdefault((scene_of(r), r["sentence"]), {})[r["seed"]] = r
    return out


def deltas(group_rows, by_id, key):
    """每个句子的两个 seed 的 Δ 均值，以及是否两个 seed 同号。"""
    out = {}
    for sk, seeds in group_rows.items():
        vals = []
        for r in seeds.values():
            b = base_of(r, by_id)
            if not b:
                continue
            if key == "f0std":
                vals.append(r["f0_std"] - b["f0_std"])
            else:
                vals.append((r.get("delta") or {}).get({"dur": "dur", "f0": "f0",
                                                        "dyn": "dyn", "rate": "rate"}[key]))
        vals = [v for v in vals if v is not None]
        if vals:
            same_sign = all(v > 0 for v in vals) or all(v < 0 for v in vals)
            out[sk] = (mean(vals), same_sign)
    return out


def measurable(ds, key, noise=None):
    floor = max(MIN[key], abs(noise or 0.0))
    return sum(1 for (m, same) in ds.values() if m is not None and abs(m) > floor and same)


def main(argv=None):
    p = argparse.ArgumentParser(description="受控描述评测汇总")
    p.add_argument("--metrics", required=True)
    p.add_argument("--json", help="把汇总也写成 JSON（给报告用）")
    a = p.parse_args(argv)
    payload, rows, by_id = load(a.metrics)
    noise = payload.get("noise") or {}
    bad = [r["id"] for r in rows if r.get("bad")]
    print("条件 %d 条，坏音 %d 条，噪声底 %s" % (len(rows), len(bad), json.dumps(noise, ensure_ascii=False)))

    groups = {"anc": per_sentence(rows, by_id, "anc"),
              "bridge": per_sentence(rows, by_id, "bridge")}
    dd = {g: {k: deltas(groups[g], by_id, k) for k in ("dur", "f0", "dyn", "rate", "f0std")}
          for g in groups}

    scenes = sorted({s for s in (scene_of(r) for r in rows)})
    summary = {}
    print("\n## 场景（Δ 相对同句 neutral_quiet，两个 seed 平均；f0_std 是绝对抖动）")
    print("| 场景 | 锚 Δ时长 | 桥 Δ时长 | 锚 ΔF0 | 桥 ΔF0 | 锚 f0_std | 桥 f0_std | 基线 f0_std | 桥 Δ抖动 | 可测(桥) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for scene in scenes:
        def scene_rows(g, key):
            return {k: v for k, v in dd[g][key].items() if k[0] == scene}

        base_std = mean([b["f0_std"] for r in rows
                         if group_of(r) == "base" and scene_of(r) == scene
                         for b in [r]])
        bridge_std = mean([r["f0_std"] for r in rows
                           if group_of(r) == "bridge" and scene_of(r) == scene])
        anc_std = mean([r["f0_std"] for r in rows
                        if group_of(r) == "anc" and scene_of(r) == scene])
        bridge_dur = mean([v[0] for v in scene_rows("bridge", "dur").values()])
        anc_dur = mean([v[0] for v in scene_rows("anc", "dur").values()])
        bridge_f0 = mean([v[0] for v in scene_rows("bridge", "f0").values()])
        anc_f0 = mean([v[0] for v in scene_rows("anc", "f0").values()])
        jitter = mean([v[0] for v in scene_rows("bridge", "f0std").values()])
        ok_dur = measurable(scene_rows("bridge", "dur"), "dur", noise.get("dur"))
        ok_f0 = measurable(scene_rows("bridge", "f0"), "f0", noise.get("f0"))
        ok_j = measurable(scene_rows("bridge", "f0std"), "f0std", None)
        summary[scene] = dict(base_std=base_std, anc_std=anc_std, bridge_std=bridge_std,
                              bridge_dur=bridge_dur, anc_dur=anc_dur, bridge_f0=bridge_f0,
                              anc_f0=anc_f0, jitter=jitter, ok_dur=ok_dur, ok_f0=ok_f0,
                              ok_jitter=ok_j)
        print("| %s | %+.2f | %+.2f | %+.1f | %+.1f | %.1f | %.1f | %.1f | %+.1f | %d·%d·%d |"
              % (scene, _n(anc_dur), _n(bridge_dur), _n(anc_f0), _n(bridge_f0),
                 _n(anc_std), _n(bridge_std), _n(base_std), _n(jitter),
                 ok_dur, ok_f0, ok_j))

    print("\n## 快哭（cry）逐句 f0_std：历史对照")
    print("旧 LoRA（用户说更像）= 76.4 / 79.1 / 76.2 Hz；ck500 用 3a 自由描述 = 60.9 / 65.1 / 70.3 Hz")
    print("| 句 | 基线 | 锚 | 桥 | 桥-基线 | 桥-锚 |")
    print("|---|---|---|---|---|---|")
    cry = {}
    for sentence in ("cry1", "cry2", "cry3"):
        b = mean([r["f0_std"] for r in rows if r["sentence"] == sentence and group_of(r) == "base"])
        anc = mean([r["f0_std"] for r in rows if r["sentence"] == sentence and group_of(r) == "anc"])
        br = mean([r["f0_std"] for r in rows if r["sentence"] == sentence and group_of(r) == "bridge"])
        cry[sentence] = dict(base=b, anc=anc, bridge=br)
        print("| %s | %.1f | %.1f | %.1f | %+.1f | %+.1f |"
              % (sentence, _n(b), _n(anc), _n(br), _n((br or 0) - (b or 0)), _n((br or 0) - (anc or 0))))

    print("\n## 音色漂移检查（锚路径 vs 上一轮 ck500：应当复现）")
    print("| 场景 | 本轮 Δ时长/ΔF0 | 上一轮 ck500 Δ时长/ΔF0 |")
    print("|---|---|---|")
    for scene, (pdur, pf0) in PREV_CK500_ANC.items():
        cur = {k: v for k, v in dd["anc"]["dur"].items() if k[0] == scene}
        curf = {k: v for k, v in dd["anc"]["f0"].items() if k[0] == scene}
        print("| %s | %+.2f / %+.1f | %+.2f / %+.1f |"
              % (scene, _n(mean([v[0] for v in cur.values()])), _n(mean([v[0] for v in curf.values()])),
                 pdur, pf0))

    print("\n## 桥描述（每场景一条）")
    seen = {}
    for r in rows:
        if group_of(r) == "bridge":
            seen.setdefault(scene_of(r), r["caption"])
    for scene, cap in sorted(seen.items()):
        print("- %s：%s（%d 字）" % (scene, cap, len(cap)))

    if a.json:
        json.dump(dict(summary=summary, cry=cry, bad=bad, noise=noise,
                       captions=seen), open(a.json, "w", encoding="utf8"),
                  ensure_ascii=False, indent=1)
        print("\n汇总 JSON → %s" % a.json)


def _n(v):
    return 0.0 if v is None else v


if __name__ == "__main__":
    main()
