"""流式情感 A/B 材料（astra-3 §7 第④步）：按**流式腿的真实顺序**出材料。

逐句调用（`/v2/plan` 带 `prev` 串起来 → `/v2/render` 逐句），三档配置对照：
  v3        plan_version=3（基线，旧行为）
  v4_s1     plan_version=4, weak_delivery=false（只开 §5.1 codebook v2）
  v4_s1s2   plan_version=4, weak_delivery=true（§5.1 + §5.2 弱线索阶段）
同一段里同一句在三个配置下共用**同一个 seed**，所以差异来自条件而不是采样噪声。

中文材料走**真的桥**（:9881 `/v2/prepare`：翻译 + 情绪提示 + 源线索），日语材料原样入规划 ——
两条都是生产里真实存在的输入形状。

用法：python tests/tts/eval/ab_stream_v4.py [--out 目录] [--voice albion] [--only 名字]
产出：<out>/<NN>_<名字>__<配置>.wav（整段顺播）+ manifest.jsonl + README.md
"""
import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import wave

TTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "tts"))
for p in (os.path.dirname(os.path.abspath(__file__)), os.path.join(TTS, "irodori")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ab_emotion as AB      # 复用它的 FOCUS 材料集与 SSE 解码（别抄第二份）

IRODORI = os.environ.get("ALBION_IRODORI_URL", "http://127.0.0.1:9889")
BRIDGE = os.environ.get("ALBION_BRIDGE_URL", "http://127.0.0.1:9881")
CFGS = (("v3", 3, False), ("v4_s1", 4, False), ("v4_s1s2", 4, True), ("v5", 5, True))
# v5（astra 第 11 轮 ②）= v4_s1s2 + 拒绝/制止/安抚类语义闸门（`ALBION_REFUSAL_GUARD` 默认开）。

# 中文材料：走真桥的准备腿（翻译 + 情绪提示 + 源线索），覆盖亲密/耳语/普通拒绝三类
ZH_ITEMS = [
    ("中文-平铺说明", ["今天天气不错，我下午去图书馆还书。", "你要一起吗？", "不急，你忙完再说。"]),
    ("中文-耳语指令", ["（凑到耳边小声说）别出声，跟我来。", "（耳语）就在前面，小心点。", "好了，安全了。"]),
    ("中文-普通拒绝", ["不行，别这样。", "我说了不要，快住手。", "……你先冷静一下。"]),
    ("中文-亲密气声", ["（压低声音）……指挥官，别、别这么用力……", "（气息不稳）啊……那里不行……", "……再、再抱紧一点。"]),
]


def _post(path, body, timeout=180):
    req = urllib.request.Request(path, data=json.dumps(body).encode("utf-8"),
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:                 # 把错误正文带出来，别只留一个 400
        raise RuntimeError("%s → %d %s" % (path.rstrip("/").rsplit("/", 1)[-1], e.code,
                                          e.read().decode("utf-8", "ignore")[:300]))


def prepare(zh, sid):
    """走真桥：中文 → （日语正文, 情绪提示, 源线索）。

    契约（实测）：请求要带 `speech_text`（+`raw_text` 才看得见旁白/括号里的线索），
    响应是 `ja_segments[].text/emotion`，源线索在 `emotion.src` 里。
    **没拿到日语分段就抛** —— 早先这里 `or zh` 的兜底把中文原文当日语喂进了 TTS 腿，
    出来的材料是废的（静默兜底 = 假绿，宁可直接失败）。
    """
    d = _post(BRIDGE + "/v2/prepare",
              {"session_id": sid, "generation": 1, "unit_id": sid,
               "speech_text": zh, "raw_text": zh, "context_before": ""})
    segs = d.get("ja_segments") or []
    if d.get("status") != "ready" or not segs:
        raise RuntimeError("prepare 没给出日语分段：%s"
                           % json.dumps(d, ensure_ascii=False)[:200])
    s = segs[0]
    emo = s.get("emotion") or {}
    return s["text"], emo, (emo.get("src") or None)


def plan_one(text, pv, weak, emotion=None, cues=None, prev=None, ctx=""):
    unit = {"unit_id": "u", "text": text, "advance": True}
    if emotion:
        unit["emotion"] = emotion
    if cues:
        unit["src_cues"] = cues
    body = {"units": [unit], "plan_version": pv, "context_before": ctx,
            "emotion_policy": "cascade"}
    if pv >= 4:
        body["weak_delivery"] = weak
    if prev:
        body["prev"] = prev
    d = _post(IRODORI + "/v2/plan", body)
    if d.get("status") == "failed":
        raise RuntimeError("plan 失败：%s" % d.get("error"))
    return d["units"][0], d.get("state")


def render_one(rec, text, voice, sid, pv, seed):
    u = {"unit_id": "u", "sub_index": 0, "text": text, "family": rec["family"],
         "caption": rec["caption"], "cfg": rec["cfg"], "scene_id": rec.get("scene_id", 1),
         "seed": seed}
    if rec.get("tier") is not None:
        u["tier"] = rec["tier"]
    if rec.get("codebook_version") is not None:
        u["codebook_version"] = rec["codebook_version"]
    blob, ev = AB.post_sse(IRODORI + "/v2/render",
                           {"session_id": sid, "generation": 1, "plan_version": pv,
                            "voice": voice, "format": "wav", "units": [u]}, timeout=300)
    if not ev.get("audio_chunk"):
        raise RuntimeError("没拿到音频：事件=%s" % ev)
    return blob


def concat(blobs, path, gap_s=0.25):
    with wave.open(path, "wb") as out:
        with wave.open(io.BytesIO(blobs[0])) as w0:
            ch, sw, sr = w0.getnchannels(), w0.getsampwidth(), w0.getframerate()
        out.setnchannels(ch)
        out.setsampwidth(sw)
        out.setframerate(sr)
        gap = b"\x00" * int(sr * gap_s) * ch * sw
        for i, b in enumerate(blobs):
            with wave.open(io.BytesIO(b)) as w:
                if i:
                    out.writeframes(gap)
                out.writeframes(w.readframes(w.getnframes()))
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.abspath(os.path.join(TTS, "..", "tests", "output", "tts", "ab-emotion-v4")))
    ap.add_argument("--voice", default="albion")
    ap.add_argument("--only", default="", help="只跑名字里含这个子串的组")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    run_id = "v4ab%04x" % (os.getpid() & 0xFFFF)          # 会话 id 必须每次唯一
    print("[v4ab] run_id=%s → %s" % (run_id, args.out), flush=True)

    # 材料：日语组（原样入规划）+ 中文组（走真桥准备腿，**先按 --only 过滤再准备**，别白花翻译钱）
    items = [(name, [(t, None, None) for t in AB.group_lines(spec)])
             for name, spec in AB.FOCUS if name in ("弱声气声", "亲密长段", "混合对话")]
    items += [(name, None) for name, _ in ZH_ITEMS]
    if args.only:
        items = [(n, r) for n, r in items if args.only in n]
    zh_map = dict(ZH_ITEMS)
    prepared = []
    for name, rows in items:
        if rows is not None:
            prepared.append((name, rows))
            continue
        out = []
        for zi, zh in enumerate(zh_map[name]):
            out.append(prepare(zh, "%s-prep-%s-%d" % (run_id, name, zi)))
        prepared.append((name, out))
    items = prepared

    manifest = []
    for gi, (gname, rows) in enumerate(items):
        made = {}
        for cfg, pv, weak in CFGS:
            prev, blobs, per_unit = None, [], []
            ctx = ""
            for i, (text, emo, cues) in enumerate(rows):
                rec, prev = plan_one(text, pv, weak, emotion=emo, cues=cues, prev=prev, ctx=ctx)
                seed = 7000 + i                      # 同句三档共用 ⇒ 差异来自条件
                blobs.append(render_one(rec, text, args.voice,
                                        "%s-g%d-%s-%d" % (run_id, gi, cfg, i), pv, seed))
                st = (rec.get("diagnostics") or {}).get("delivery_stage") or {}
                per_unit.append({"text": text, "family": rec["family"], "caption": rec["caption"],
                                 "cfg": rec["cfg"], "tier": rec.get("tier"),
                                 "delivery": (rec.get("diagnostics") or {}).get("delivery"),
                                 "gap": (rec.get("diagnostics") or {}).get("gap"),
                                 "reason": st.get("reason")})
                ctx = (ctx + " " + text)[-160:]
            path = os.path.join(args.out, "%02d_%s__%s.wav" % (gi, gname, cfg))
            size = concat(blobs, path)
            made[cfg] = per_unit
            print("%-14s %-9s %d 句 → %s（%d 字节）" % (gname, cfg, len(rows),
                                                        os.path.basename(path), size), flush=True)
        manifest.append({"group": gname, "sentences": [r[0] for r in rows],
                         "arms": {c: made[c] for c, _, _ in CFGS}})

    with open(os.path.join(args.out, "manifest.jsonl"), "w", encoding="utf-8") as f:
        for m in manifest:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(os.path.join(args.out, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 流式情感 A/B 材料（§5.1 / §5.2）\n\n"
                "同名的三个 wav 是**同一段文本、同一批随机种子**，只有条件不同：\n\n"
                "| 配置 | plan_version | 弱线索阶段 | 含义 |\n|---|---|---|---|\n"
                "| `v3` | 3 | — | 基线（本轮改动前的行为） |\n"
                "| `v4_s1` | 4 | 关 | 只开 §5.1：neutral 说耳语时借既有耳语锚 |\n"
                "| `v4_s1s2` | 4 | 开 | §5.1 + §5.2：弱线索分组/去重/不足就 abstain |\n"
                "| `v5` | 5 | 开 | v4_s1s2 + 拒绝/制止/安抚类语义闸门（本轮 ②） |\n\n"
                "听法：先竖着听同一段的三档（哪个更像人说话），再横着听不同段。\n\n"
                "重点看：① 弱声/气声是否自然、语尾收得住 ② **普通拒绝**（中文-普通拒绝组）\n"
                "有没有被念成亲密或\"明亮弹跳/提高嗓门\"（v5 起由 `refusal_guard` 拦掉）\n"
                "③ 相邻句之间音色/语气有没有跳 ④ 该轻的地方是否还硬。\n\n"
                "`manifest.jsonl` 是每句在三档下的 family/caption/cfg/delivery/gap/reason。\n")
    print("\n共 %d 组 → %s" % (len(manifest), os.path.abspath(args.out)))


if __name__ == "__main__":
    main()
