# -*- coding: utf-8 -*-
"""A/B 听测材料生成器：同一段文本，两条臂各渲染一份 WAV。

  A 臂 = 改动前的生产条件（v2：族级一句固定 caption + 固定 cfg，无前文、无滞回）
  B 臂 = v3（训练锚码本 + 弱/强档 + 逐句滞回）

为什么必须"同一段、两条臂"：只改一个变量才归因得动。两臂共用同一音色、
同一采样步数，唯一差别就是喂给模型的条件（caption/cfg）。

用法：
    python tests/tts/eval/ab_emotion.py --out _tmp/ab-emotion
产物：
    <out>/A_*.wav  <out>/B_*.wav      成对音频（同段号）
    <out>/probe.jsonl                 每条的 family/caption/cfg/tier/时长/A 臂 B 臂对照
    <out>/README.txt                  怎么听、听什么
"""
import argparse
import base64
import io
import json
import os
import sys
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TTS = os.path.abspath(os.path.join(HERE, "..", "..", "..", "tts"))
sys.path.insert(0, os.path.join(TTS, "irodori"))

import scene_plan as SP            # noqa: E402

IRODORI = os.environ.get("ALBION_IRODORI_URL", "http://127.0.0.1:9889")

# 每组：("名字", ("lines", [日语行…])) 或 ("名字", ("persona", "文件.txt", [段号…]))
# 精听组的文本是**手写的**：要覆盖的是"判定该不该跟着内容变"，不是某个角色的稿子。
FOCUS = [
    ("混合对话", ("lines", [
        "指揮官、おかえりなさい。",                        # 平静
        "わあ、本当ですか！？うれしい！",                   # 激动
        "すみません……私のせいで。",                        # 低落
        "もう、そんなに見ないでください……",                 # 害羞
        "（小声で）今日は早めに休んでくださいね。",           # 耳语
        "はぁ……んっ……",                                 # 拟声（信号最强）
        "今日は少し寒いですね。",                          # 平铺
        "えっ、それ本当に大丈夫なんですか？",                # 惊讶
    ])),
    ("亲密长段", ("persona", "阿尔比恩.txt", [18, 19, 20, 21, 22])),
    ("弱声气声", ("lines", [
        "……ん、そこ、だめ……",
        "（息を殺して）気づかれないように……",
        "ふふ、少しだけなら……いいですよ。",
        "はぁ……はぁ……もう、無理……",
        "（囁くように）今夜は、離さないでください。",
    ])),
]

# 全量组额外加：每船一段亲密戏（段号取自 Astra 方案 §4.1 的候选，只负责定位）
EXTRA = [
    ("albion-1", ("persona", "阿尔比恩.txt", [4, 5, 6])),
    ("albion-2", ("persona", "阿尔比恩.txt", [14, 15, 16])),
    ("brest", ("persona", "布雷斯特.txt", [39, 40, 41])),
    ("taihou", ("persona", "大凤.txt", [56, 57, 58])),
    ("jinshi", ("persona", "金狮.txt", [53, 54, 55])),
]


def group_lines(spec):
    """把组规格摊平成 [文本…]（顺带做 200 字符截断）。"""
    if spec[0] == "lines":
        return [fit_200(t)[0] for t in spec[1]]
    fname, idxs = spec[1], spec[2]
    with open(os.path.join(TTS, "persona", fname), encoding="utf-8-sig") as f:
        segs, _ = SP._segments(f.read().strip())
    return [fit_200(segs[i])[0] for i in idxs if i < len(segs)]


def fit_200(text, limit=200):
    """渲染队列的硬上限是 200 字符（`render_queue._check_unit`）；超了就在标点处截到上限内。

    两臂用**同一份截断后的文本**，所以比较依然公平；只是长段只听到前半截。
    """
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    for p in ("。", "！", "？", "…", "、"):
        i = cut.rfind(p)
        if i > limit // 3:
            return cut[:i + 1], True
    return cut, True


def post_sse(url, body, timeout=120):
    if url.endswith("/v2/render"):
        base = url[:-len("/v2/render")]
        def lifecycle(action, payload):
            req = urllib.request.Request(base + "/v2/render/" + action,
                data=json.dumps(payload).encode("utf8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        opened = lifecycle("open", {"voice": body["voice"]})
        address = {"session_id": opened["session_id"], "generation": opened["generation"]}
        complete = False
        try:
            result = _post_sse(url, dict(body, **address), timeout)
            complete = bool(result[1].get("done"))
            return result
        finally:
            lifecycle("close" if complete else "cancel", address)
    return _post_sse(url, body, timeout)


def _post_sse(url, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                headers={"Content-Type": "application/json"})
    audio, events = b"", {}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        name = None
        for raw in r:
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if line.startswith("event:"):
                name = line[6:].strip()
                events[name] = events.get(name, 0) + 1
            elif line.startswith("data:") and name == "audio_chunk":
                audio += base64.b64decode(json.loads(line[5:].strip()).get("audio_base64") or "")
    return audio, events


def render(plan_rec, text, voice, session_id, generation, plan_version):
    unit = {"unit_id": "%s:0" % session_id, "sub_index": 0, "text": text,
            "family": plan_rec["family"], "caption": plan_rec["caption"],
            "cfg": plan_rec["cfg"], "scene_id": plan_rec.get("scene_id", 1)}
    blob, ev = post_sse(IRODORI + "/v2/render",
                        {"session_id": session_id, "generation": generation,
                         "plan_version": plan_version, "voice": voice, "format": "wav",
                         "units": [unit]})
    if not ev.get("audio_chunk"):
        raise RuntimeError("没拿到音频：事件=%s" % ev)
    with wave.open(io.BytesIO(blob)) as w:
        secs = round(w.getnframes() / w.getframerate(), 2)
    return blob, secs


def _take_text(texts, limit=200):
    """能一次合成的**整段**：逐句累加，到生产上限（`render_queue` 的 200 字硬校验）为止。

    不拼接、不在句中断——整段就是**一次合成一个条件**，情感得连着听才看得出优劣。
    """
    out, used = "", []
    for t in texts:
        if len(out) + len(t) > limit:
            break
        out += t
        used.append(t)
    return out, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.abspath(os.path.join(TTS, "..", "tests", "output", "tts", "ab-emotion")))
    ap.add_argument("--voice", default="albion")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 组（冒烟用）")
    ap.add_argument("--tag", default="ab", help="会话 id 前缀（每次跑唯一，别复用旧 id）")
    ap.add_argument("--groups", default="focus", choices=("focus", "all"),
                    help="focus=三组精听材料（18 段）；all=再加四船亲密段")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    # 会话 id 必须**每次运行唯一**：渲染队列把 (session_id, generation) 已完成的世代当退休处理，
    # 复用同一个 id 的第二轮会被直接拒（冒烟跑过一次就踩到了）。
    run_id = "%s%04x" % (args.tag, os.getpid() & 0xFFFF)
    print("[ab] run_id=%s" % run_id, flush=True)

    rows = []
    groups = FOCUS + (EXTRA if args.groups == "all" else [])
    if args.limit:
        groups = groups[:args.limit]
    for gname, spec in groups:
        texts = group_lines(spec)
        take, used = _take_text(texts)
        if len(used) < len(texts):
            print("[ab] %s：整段 %d 字超上限，取前 %d/%d 句（%d 字）"
                  % (gname, sum(len(t) for t in texts), len(used), len(texts), len(take)),
                  flush=True)
        # 同一条长段、两臂各**一次**合成：A = 改动前口径，B = v3（这条整段一个条件）
        a_rec = SP.plan_units([{"text": take, "unit_id": "a", "parent_id": 0}])[0]
        b_rec = SP.plan_units_v3([{"text": take, "unit_id": "b", "parent_id": 0}])[0]
        a_blob, a_s = render(a_rec, take, args.voice, "%s-%s-a" % (run_id, gname), 1, 2)
        b_blob, b_s = render(b_rec, take, args.voice, "%s-%s-b" % (run_id, gname), 1, 3)
        for arm, blob in (("A", a_blob), ("B", b_blob)):
            with open(os.path.join(args.out, "%s_%s.wav" % (arm, gname)), "wb") as f:
                f.write(blob)
        rows.append({"tag": gname, "group": gname, "sentences": used, "chars": len(take),
                     "text": take,
                     "A": {"family": a_rec["family"], "caption": a_rec["caption"],
                           "cfg": a_rec["cfg"], "secs": a_s},
                     "B": {"family": b_rec["family"], "caption": b_rec["caption"],
                           "cfg": b_rec["cfg"], "tier": b_rec.get("tier"),
                           "reason": b_rec.get("reason"), "secs": b_s}})
        print("%-10s %3d 字  A=%-26s B=%-26s %.2fs/%.2fs" % (
            gname, len(take), a_rec["caption"][:24], b_rec["caption"][:24], a_s, b_s), flush=True)

    with open(os.path.join(args.out, "probe.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    n_diff = sum(1 for r in rows if r["A"]["caption"] != r["B"]["caption"])
    with open(os.path.join(args.out, "README.txt"), "w", encoding="utf-8") as f:
        f.write("A/B 听测材料\n"
                "A = 改动前的生产条件（族级一句固定 caption + 固定 cfg）\n"
                "B = v3（只用训练集里出现过的 7 句锚，弱/强两档，逐句滞回）\n"
                "同段 A_x.wav / B_x.wav 成对听；x 是段号。\n"
                "每段两次渲染的随机种子不同（采样本身有随机性），所以差异里含少量噪声，\n"
                "听的时候重点看\"同一句话为什么要换一种说法\"，而不是逐帧比对。\n"
                "听什么：① 语气有没有跟着内容走（该轻的轻、该起伏的起伏）② 是否仍在\"念台词\"\n"
                "③ 弱声/气声段：气息是否自然、语尾是否收得住 ④ 相邻句之间音色有没有跳。\n\n"
                "本组统计：共 %d 段，其中 %d 段两臂条件不同（%.0f%%）。\n"
                % (len(rows), n_diff, 100.0 * n_diff / max(1, len(rows))))
        f.write("\nprobe.jsonl 里是每段的 family/caption/cfg/tier 与时长。\n")
    n_wav = len([f for f in os.listdir(args.out) if f.endswith(".wav")]) if os.path.isdir(args.out) else 0
    print("\n共 %d 段 → %s（%d 段两臂条件不同；落盘 %d 个 wav）"
          % (len(rows), os.path.abspath(args.out), n_diff, n_wav))


if __name__ == "__main__":
    main()
