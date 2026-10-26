# -*- coding: utf-8 -*-
"""场景评测集 → 「桥自动写的受控描述」矩阵（阶段 3d 离线评测用）。

输入：`tests/tts/data/emotion_scene_20261026.json`（8 场景 × 3 句 × base/anc/free）
输出：同结构的矩阵，但每个句子的 `free` 条件换成**桥会写的描述** ——
即 `tts/irodori/voice_caption.caption_for()` 对"该场景对应的情绪提示"的输出，
不是人手写的 3a 描述。其余（句子、锚、seed、基线）逐字段照抄，diff 只有 caption 一个变量。

为什么用提示而不是跑真桥：桥的描述纯函数只依赖情绪提示（`scene_of` + `level_of`），
不用起服务、不用花翻译调用；场景标签 → 提示的映射写死在 `SCENE_CUES`，一眼可核。

用法（仓库根）：
    tts/irodori/.venv/Scripts/python.exe -B tests/tts/probes/controlled_matrix.py \\
        > tests/tts/data/emotion_scene_bridge_20261026.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tts", "irodori"))

import voice_caption as VC      # noqa: E402

SRC = os.path.join(ROOT, "tests", "tts", "data", "emotion_scene_20261026.json")

# 场景 → 桥会拿到的情绪提示（跟 `voice_caption.scene_of` 的分支一一对应）。
SCENE_CUES = {
    "sad": {"valence": -0.45, "arousal": 0.30, "intensity": 0.35},
    "whi": {"valence": 0.05, "arousal": 0.30, "intensity": 0.30, "phonation": "whispered"},
    "shy": {"valence": 0.10, "arousal": 0.30, "intensity": 0.30,
            "src": [{"kind": "shy", "negated": False}]},
    "coq": {"valence": 0.20, "arousal": 0.40, "intensity": 0.40, "phonation": "breathy"},
    "cry": {"valence": -0.60, "arousal": 0.35, "intensity": 0.55},
    "exc": {"valence": 0.70, "arousal": 0.80, "intensity": 0.80},
    "ang": {"valence": -0.50, "arousal": 0.85, "intensity": 0.70},
    "gen": {"valence": 0.40, "arousal": 0.30, "intensity": 0.30},
}


def bridge_caption(scene):
    cues = SCENE_CUES[scene]
    cap = VC.caption_for(cues)
    if not cap:                              # 桥会丢掉描述、回锚路径 —— 评测里不能悄悄换一条
        raise SystemExit("场景 %s 没拼出合法描述（%r）" % (scene, cues))
    return cap


def build(raw):
    out = json.loads(json.dumps(raw))         # 深拷贝
    table = {}
    for cond in out["conditions"]:
        scene = cond.get("scene")
        if cond.get("group") == "free":
            if scene not in SCENE_CUES:
                raise SystemExit("条件 %s 的场景未知：%r" % (cond["id"], scene))
            cap = bridge_caption(scene)
            table[scene] = cap
            cond["caption"] = cap
            cond["cfg"] = VC.FREE_CFG
            cond["group"] = "bridge"
            cond["caption_source"] = "voice_caption.bridge"
    out["derived_from"] = "emotion_scene_20261026.json"
    out["bridge_captions"] = table
    out["notes"] = list(out.get("notes") or []) + [
        "本矩阵的 bridge 组 = 桥（voice_caption）对该场景情绪提示写的受控描述，"
        "同一场景的 3 句共用一条（描述只随场景/强度变，不随标点变）；",
        "base/anc 组逐字段照抄 emotion_scene_20261026.json，只换 free 组的 caption/cfg。",
    ]
    return out, table


def main(argv=None):
    import argparse
    import pathlib
    p = argparse.ArgumentParser(description="生成「桥自动写的受控描述」评测矩阵（UTF-8 JSON）")
    p.add_argument("--out", help="写到这个路径（不给就打到 stdout）")
    a = p.parse_args(argv)
    raw = json.loads(open(SRC, encoding="utf8").read())
    out, table = build(raw)
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        pathlib.Path(a.out).write_text(text, encoding="utf8")
    else:
        print(text)
    print("场景描述：%s" % json.dumps(table, ensure_ascii=False), file=sys.stderr)
    print("条件 %d 条 → %s" % (len(out["conditions"]), a.out or "stdout"), file=sys.stderr)


if __name__ == "__main__":
    main()
