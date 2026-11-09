# -*- coding: utf-8 -*-
"""V / TV 复测（2026-11-09）：用**改过的代码路径**重渲 A/B 两轮 + 主脑改写文本的两轮。

V  = A/B 两轮的日志 ja（同 seed 20261109、同样预热），走新的桥（亲密锚标记 + 拟声去「！」）
     + 新的规划器（`intimate_anchor` 过闸门）+ 新的气口（`ALBION_GAP_INTIMATE`）。
TV = `rewrite_T.txt` 两轮：日文直接给（跳过翻译腿），情绪按桥既有规则从日语拟声推
     （有拟声 → breathy+0.9），其余与 V 相同；块按「……」边界攒到 ≤60 朗读字。

只本地跑：9881/9889 都是现有接口，不重启任何服务；9889 只用来 /v2/render（与本次改动无关）。
plan 用本工作树里的 scene_plan（纯 CPU），因为 #2 改的就是它。
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tts", "irodori"))
sys.path.insert(0, os.path.join(ROOT, "tts", "stream"))

import bridge_v2 as B2      # noqa: E402
import emotion_codebook as CB  # noqa: E402
import gap_shaper as GS     # noqa: E402
import scene_plan as SP     # noqa: E402

OUT = r"D:\AI\Wife\tests\output\intimate-flow-20261109"
IRODORI = "http://127.0.0.1:9889"
SEED = 20261109
VOICE = "albion"
CTX_CHARS = 400
GAP_X = 0.10              # 壳固定的段间停顿（9885 自己不加静音）
ROUNDS = {                # 与 f2dd2f36 样张同一批 unit（zh 用于 source_range/source_prefix）
    "A": {"session": "s22f8c329be5e", "units": [
        ("s22f8c329be5e:1:0", "哈啊……！"),
        ("s22f8c329be5e:1:1", "舒、舒服就别忍着了……"),
        ("s22f8c329be5e:1:2", "嗯……全都射进来……一滴都别留……"),
        ("s22f8c329be5e:1:3", "指挥官……哈啊……快给我……全部射给我呀……！")]},
    "B": {"session": "se6222405884d", "units": [
        ("se6222405884d:1:0", "哈啊……！"),
        ("se6222405884d:1:1", "啊……！"),
        ("se6222405884d:1:2", "身子深处……全都被热热地浇透了……"),
        ("se6222405884d:1:3", "呼……全都接到了，肚子里又胀又烫的……指挥官这次……真的被榨干了吧……"),
        ("se6222405884d:1:4", "快抽纸擦一擦……不准再胡闹了，赶紧闭上眼睛去睡啦。")]},
}


# ---------------- HTTP（只有 /v2/render 与对照用的 /v2/plan） ----------------
def _post(path, body, base=IRODORI, timeout=900):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s → %d %s" % (path, e.code, e.read().decode("utf-8", "ignore")[:300]))


def sse_render(sid, gen, unit, timeout=900):
    body = {"session_id": sid, "generation": gen, "plan_version": 4, "voice": VOICE,
            "format": "wav", "units": [unit]}
    req = urllib.request.Request(IRODORI + "/v2/render", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    audio, name, done = b"", None, False
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[5:].strip() or "{}")
                if name == "audio_chunk":
                    audio += base64.b64decode(payload.get("audio_base64") or "")
                elif name == "unit.failed":
                    raise RuntimeError("unit.failed: %s" % payload.get("error"))
                elif name == "done":
                    done = True
                    if payload.get("failed"):
                        raise RuntimeError("done failed=%s" % payload["failed"])
    if not audio or not done:
        raise RuntimeError("没拿到音频")
    return audio, time.monotonic() - t0


# ---------------- 本工作树的 plan（与 /v2/plan 的 pv=4 同口径） ----------------
PLAN_FLAGS = dict(plan_version=4, allow_neutral_whisper=True, weak_delivery=True,
                  cue_scope=False, refusal_guard=False, hint_phonation=False,
                  cross_sentence=False, ja_stage_cue=False, level_rebase=False,
                  voice_caption=True)


def plan_one(text, uid, ctx, prev, emotion, voice_caption=None):
    units = [{"unit_id": uid, "text": text, "advance": True, "emotion": emotion}]
    if voice_caption:
        units[0]["voice_caption"] = voice_caption      # 桥写的自由描述（与 X 逐字相同）
    t0 = time.monotonic()
    recs, state = SP.plan_units_v3(units, ctx, prev=prev, **PLAN_FLAGS)
    rec = recs[0]
    rec["codebook_version"] = CB.CODEBOOK_VERSION_V2      # /v2/plan 处理器补的，v4 渲染必填
    return rec, state, time.monotonic() - t0


def render_unit(uid, sub_index, text, rec):
    u = {"unit_id": uid, "sub_index": sub_index, "text": text,
         "family": rec["family"], "caption": rec["caption"], "cfg": rec["cfg"],
         "scene_id": rec.get("scene_id", 1), "codebook_version": CB.CODEBOOK_VERSION_V2,
         "seed": SEED}
    for k in ("tier", "level"):
        if rec.get(k) is not None:
            u[k] = rec[k]
    if rec.get("caption_mode"):
        u["caption_mode"] = rec["caption_mode"]
    return u


# ---------------- 音频 ----------------
def wav_read(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        p = w.getparams()
        raw = w.readframes(w.getnframes())
    return p, np.frombuffer(raw, dtype="<i2").reshape(-1, p.nchannels).mean(axis=1) / 32768.0


def wav_stats(blob):
    p, mono = wav_read(blob)
    sr = p.framerate
    peak = float(np.abs(mono).max()) if len(mono) else 0.0
    ve, sil = GS.tail_profile(blob)
    lead = -1.0
    if peak > 0:
        thr = max(peak * 10 ** (-GS.VOICED_DB / 20.0), 10 ** (GS.FLOOR_DB / 20.0))
        win = max(1, int(0.01 * sr))
        fr = len(mono) // win
        if fr:
            loud = mono[:fr * win].reshape(fr, win).max(axis=1) > thr
            idx = np.flatnonzero(loud)
            if len(idx):
                lead = float(idx[0] * win) / sr
    return {"dur_s": round(len(mono) / float(sr), 3), "lead_s": round(lead, 3),
            "voiced_end_s": ve, "tail_sil_s": sil, "peak": round(peak, 4)}


def wav_concat(blobs, gap_s, path):
    params = None
    with wave.open(path, "wb") as out:
        for i, b in enumerate(blobs):
            with wave.open(io.BytesIO(b), "rb") as w:
                cur = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = cur
                    out.setnchannels(cur[0]); out.setsampwidth(cur[1]); out.setframerate(cur[2])
                if i and gap_s > 0:
                    out.writeframes(b"\x00" * int(params[2] * gap_s) * params[0] * params[1])
                out.writeframes(w.readframes(w.getnframes()))
    return path


def silence_gaps(blob, min_s=0.20):
    p, mono = wav_read(blob)
    sr = p.framerate
    peak = float(np.abs(mono).max()) if len(mono) else 0.0
    if peak <= 0:
        return []
    thr = max(peak * 10 ** (-GS.VOICED_DB / 20.0), 10 ** (GS.FLOOR_DB / 20.0))
    win = max(1, int(0.01 * sr))
    fr = len(mono) // win
    loud = mono[:fr * win].reshape(fr, win).max(axis=1) > thr
    gaps, i = [], 0
    while i < fr:
        if loud[i]:
            i += 1
            continue
        j = i
        while j < fr and not loud[j]:
            j += 1
        if (j - i) * win / float(sr) >= min_s:
            gaps.append({"at_s": round(i * win / float(sr), 2),
                         "len_s": round((j - i) * win / float(sr), 2)})
        i = j
    return gaps


def spoken_chars(text):
    return sum(1 for c in text if not unicodedata.category(c).startswith(("P", "Z"))
               and not c.isspace())


# ---------------- V ----------------
def boot_service():
    return B2.PrepareService(translate=lambda s: s, translate_ex=lambda s: (s, None))


def v_units(round_id, runs):
    """V 的输入 = X 那一版的原样材料（日志 ja + 当时 prepare 给的情绪/自由描述）。

    这样 V 与 X 只差三处改动：句尾气口、闸门认锚、拟声去「！」。锚判定与去叹号**直接用本树的
    桥函数**（X 那时也是同一个函数判的，只是现在多了标记/去叹号）。
    """
    units_x = runs["rounds"][round_id]["units"]
    B2._last_intimate_at = time.monotonic()      # 预热：生产里这两轮之前刚说过亲密句
    out, prefix = [], ""
    for u in units_x:
        text = u["ja_log"]
        seg0 = (u.get("rsegs") or [{}])[0]
        emo = dict(seg0.get("emotion") or {}) or None
        vc = seg0.get("voice_caption")
        anchor = B2.intimate_codebook_anchor(emo, text, u["zh"],
                                             context_before=(prefix or "")[-CTX_CHARS:])
        seg = {"sub_index": 0, "text": text}
        if anchor:
            if anchor["hint"]:
                emo.update(anchor["hint"])
            emo["intimate_anchor"] = anchor["tier"]
            seg["emotion"] = emo
            seg["intimate"] = True
            seg["text"] = B2.strip_nonclimax_bang(text)
        else:
            if emo:
                seg["emotion"] = emo
            if vc:
                seg["voice_caption"] = vc        # 自由描述与 X 逐字相同
        out.append({"unit_id": u["unit_id"], "zh": u["zh"], "ja_in": text, "segs": [seg],
                    "anchor_why": (anchor or {}).get("why")})
        prefix += u["zh"]
    return out


# ---------------- TV ----------------
_SPLIT_BY_PAUSE = re.compile(r"(?<=……)")


def tv_blocks(line, limit=60):
    """按「……」边界攒块：每块 ≤limit 朗读字（单个碎片自己超限就单独成块）。"""
    blocks, buf = [], ""
    for piece in (p for p in _SPLIT_BY_PAUSE.split(line) if p):
        if buf and spoken_chars(buf + piece) > limit:
            blocks.append(buf)
            buf = ""
        buf += piece
        while spoken_chars(buf) > limit:      # 单个碎片自己就超：按字符硬切（记录里标出来）
            cut = max(1, int(limit / max(1e-6, spoken_chars(buf)) * len(buf)))
            blocks.append(buf[:cut])
            buf = buf[cut:]
    if buf.strip():
        blocks.append(buf)
    return [b for b in blocks if b.strip()]


def read_t_rounds(path):
    rounds, key = {}, None
    for line in open(path, encoding="utf-8-sig"):
        line = line.rstrip("\n")
        m = re.match(r"^\s*\[([0-9A-Za-z_:-]+)\]\s*$", line)
        if m:
            key = m.group(1)
            continue
        if key and line.strip() and not line.lstrip().startswith("#"):
            rounds[key] = line.strip()
            key = None
    return rounds


def tv_units(session, line):
    """TV：日文给定 ⇒ 没有翻译腿。情绪按桥既有规则从日语拟声推（有拟声 → breathy+0.9）。"""
    svc = boot_service()
    out, prefix = [], ""
    for i, text in enumerate(tv_blocks(line)):
        emo = dict(B2.INTIMATE_STRONG_HINT) if B2.moan_cues(text) else None
        req = B2.parse_request({"session_id": session, "generation": 1, "unit_id": "tv%d" % i,
                                "voice": VOICE, "source_range": [0, 0], "speech_text": text,
                                "raw_text": "", "source_prefix": prefix, "plan_version": 4})
        anchor = B2.intimate_codebook_anchor(emo, text, "",
                                             context_before=(prefix or "")[-CTX_CHARS:])
        seg = {"sub_index": 0, "text": text}
        if anchor:
            if anchor["hint"]:
                emo.update(anchor["hint"])
            emo["intimate_anchor"] = anchor["tier"]
            seg["emotion"] = emo
            seg["intimate"] = True
            seg["text"] = B2.strip_nonclimax_bang(text)
        # 没有锚就不带情绪：TV 没有翻译腿，凭空给 hint 会让闸门判出别的族（与 V 的口径不同）
        out.append({"unit_id": "tv%d" % i, "zh": "", "ja_in": text, "segs": [seg],
                    "anchor_why": (anchor or {}).get("why")})
        prefix += text
    return out


# ---------------- 一轮渲染 ----------------
def render_round(tag, units, out_path, with_latency=True):
    opened = _post("/v2/render/open", {"voice": VOICE})
    sid, gen = opened["session_id"], opened["generation"]
    blocks, blobs, ctx, prev = [], [], "", None
    t_all, first_audio_s, prepare_s = time.monotonic(), None, 0.0
    try:
        for u in units:
            last_i = len(u["segs"]) - 1
            for s in u["segs"]:
                i = s.get("sub_index", 0)
                uid = "%s:%d" % (u["unit_id"], i)
                intimate = bool(s.get("intimate"))
                t0 = time.monotonic()
                rec, state, pms = plan_one(s["text"], uid, ctx, prev, s.get("emotion"),
                                           s.get("voice_caption"))
                body = render_unit(uid, i, s["text"], rec)
                raw, rms = sse_render(sid, gen, body)
                kind = GS.shape_kind(u["zh"], "", intimate=intimate, last=(i == last_i))
                shaped = GS.shape(raw, kind)
                if first_audio_s is None:
                    first_audio_s = time.monotonic() - t0
                blocks.append({
                    "index": len(blocks), "unit_id": uid, "unit_ids": [u["unit_id"]],
                    "text": s["text"], "text_in": u["ja_in"] if len(u["segs"]) == 1 else None,
                    "intimate": intimate, "tail_kind": kind,
                    "interjection": bool(SP._is_interjection(s["text"])),
                    "plan": rec, "render_unit": body,
                    "plan_ms": round(pms * 1000), "render_ms": round(rms * 1000),
                    "stats_raw": wav_stats(raw), "stats_shaped": wav_stats(shaped),
                    "anchor_why": u.get("anchor_why")})
                blobs.append(shaped)
                ctx = (ctx + " " + s["text"])[-CTX_CHARS:]
                prev = state or prev
    finally:
        _post("/v2/render/close", {"session_id": sid, "generation": gen})
    wav_concat(blobs, GAP_X, out_path)
    return {"blocks": blocks, "gap_s": GAP_X,
            "gap_note": "同一套壳 0.10s；句尾气口由新代码决定（亲密 0.3 / 其余按标点）",
            "wall_s": round(time.monotonic() - t_all, 1),
            "first_audio_s": round(first_audio_s or 0, 2),
            "first_audio_note": "只含 plan+render+shape（文本已给定，没有翻译腿）",
            "file": out_path, "stats_total": wav_stats(open(out_path, "rb").read())}


# ---------------- index.md 追加 ----------------
def fmt_rows(d):
    rows = []
    for b in d["blocks"]:
        st = b["stats_shaped"]
        rows.append("| %d | %s | %.2f | %s/%s cfg=%s | %s | %.3f | %.2f | %.2f | %s |"
                    % (b["index"] + 1,
                       "%s:%s" % (b["unit_ids"][0].split(":")[-1], b["unit_id"].split(":")[-1]),
                       st["dur_s"], b["plan"].get("family"), b["plan"].get("tier") or "-",
                       b["plan"].get("cfg"), b["tail_kind"], st["tail_sil_s"],
                       b["stats_raw"]["tail_sil_s"], b["stats_raw"]["dur_s"],
                       "是" if b["intimate"] else "否"))
    return rows


def gap_list(path):
    if not path or not os.path.isfile(path):
        return "—"
    return "、".join("%.2fs" % g["len_s"] for g in silence_gaps(open(path, "rb").read())) or "无"


def brief(d, first=None):
    """一行摘要：块数 / 总长 / 首声 / 块间静音 / 每块 族-档-cfg(尾静音)。"""
    per = " ".join("%s/%s/%s(%.2f)" % (b["plan"].get("family"), b["plan"].get("tier") or "-",
                                       b["plan"].get("cfg"),
                                       (b["stats_shaped"] or b["stats_raw"])["tail_sil_s"])
                   for b in d["blocks"])
    return ("| %d | %.2fs | %s | %s | %s |"
            % (len(d["blocks"]), d["stats_total"]["dur_s"],
               first if first is not None else "%.2fs" % (d.get("first_audio_s") or 0),
               gap_list(d.get("file")), per))


def append_index(runs, res):
    path = os.path.join(OUT, "index.md")
    L = ["", "---", "", "# V / TV 复测（三处改动后，2026-11-09）", "",
         "V = A/B 两轮按 X 的同一份材料（日志 ja + 当时的情绪/自由描述）重渲，只差三处改动；",
         "TV = `rewrite_T.txt` 两轮（日文直接给，跳过翻译腿，情绪按桥既有规则从日语拟声推）。",
         "块间仍是壳的 0.10s；“首声”只含 plan+render+shape（文本已给定，没有翻译腿），"
         "X 那一列含真翻译（约 +2.5–5s）。", "",
         "## X / V 并排（每块 族/档/cfg(尾静音s)）", "",
         "| 轮 | 版 | 块数 | 总长 | 首声 | 块间静音 | 每块 |", "|---|---|---|---|---|---|---|"]
    for rid in ("A", "B"):
        lat = [x for x in runs.get("latency") or []
               if x.get("round") == rid and x.get("version") == "X"]
        x_first = ("%.2f–%.2fs（含翻译）" % (min(x["first_audio_s"] for x in lat),
                                           max(x["first_audio_s"] for x in lat))) if lat else "—"
        for ver in ("X", "V"):
            d = runs["rounds"].get(rid, {}).get(ver)
            if d:
                if ver == "X":
                    L.append("| %s | %s %s" % (rid, ver, brief(d, x_first)))
                else:
                    L.append("| %s | %s %s" % (rid, ver, brief(d, "%.2fs" % (d.get("first_audio_s") or 0))))
    for tag in ("AT", "BT"):
        d = runs["rounds"].get("TV", {}).get(tag)
        if d:
            L.append("| %s | TV %s" % (tag, brief(d, "%.2fs" % (d.get("first_audio_s") or 0))))
    L += ["", "三处改动的客观差（A/B）：亲密块尾静音 0.76–0.80 → 0.30–0.49，"
          "紧跟拟声的可听间隔 0.91/0.93 → 0.57/0.53；B 的 unit 1 由 bright/strong cfg 3.2 "
          "→ intimate/strong 4.5；拟声块渲染文本去掉末尾「！」；非亲密块（free/bright cfg 3.0）"
          "逐字不变。TV 两轮全部落在 intimate/strong 4.5，尾静音 0.30–0.36。", ""]
    for tag in ("A", "B", "AT", "BT"):
        d = res.get(tag)
        if not d:
            continue
        L += ["## %s（%d 块，总长 %.2fs）" % (tag, len(d["blocks"]), d["stats_total"]["dur_s"]), "",
              "| # | unit | 时长s | 族/档 cfg | 气口 | 尾静音s | 原尾静音s | 原时长s | 亲密 |",
              "|---|---|---|---|---|---|---|---|---|"]
        L += fmt_rows(d)
        L += ["", "块间静音（≥0.20s）：%s" % gap_list(d.get("file")), ""]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="V,TV")
    ap.add_argument("--index-only", action="store_true")
    args = ap.parse_args()
    os.environ["ALBION_VOICE_CAPTION"] = "1"     # 生产桥同值；锚判定在这条开关里
    os.environ["ALBION_VOICE_EMOJI"] = "0"       # 关掉：V 的文本必须与日志 ja 逐字相同
    runs_path = os.path.join(OUT, "runs.json")
    runs = json.load(open(runs_path, encoding="utf-8"))
    if args.index_only:
        res = {}
        for rid in ("A", "B"):
            if "V" in runs["rounds"].get(rid, {}):
                res[rid] = runs["rounds"][rid]["V"]
        for tag in ("AT", "BT"):
            if tag in runs["rounds"].get("TV", {}):
                res[tag] = runs["rounds"]["TV"][tag]
        append_index(runs, res)
        return

    cache = json.load(open(os.path.join(OUT, "prepare_cache.json"), encoding="utf-8"))
    if "V" in args.only:
        for rid in ("A", "B"):
            units = v_units(rid, runs)
            d = render_round(rid, units, os.path.join(OUT, "%s_V.wav" % rid))
            d["units"] = [{"unit_id": u["unit_id"], "ja_in": u["ja_in"],
                           "segs": [{k: v for k, v in s.items()}
                                    for s in u["segs"]]} for u in units]
            runs["rounds"].setdefault(rid, {})["V"] = d
            print("[V] %s %d 块 %.2fs 首声 %.2fs" % (rid, len(d["blocks"]),
                                                    d["stats_total"]["dur_s"], d["first_audio_s"]))
        # 口径核对：非亲密单元的本树 plan 应与 9889 的 /v2/plan 完全一致
        try:
            unit = runs["rounds"]["B"]["V"]["blocks"][4]
            body = {"units": [{"unit_id": "cmp", "text": unit["text"], "advance": True,
                               "emotion": None}], "context_before": "", "plan_version": 4,
                    "emotion_policy": "cascade"}
            live = _post("/v2/plan", body, base="http://127.0.0.1:9889")["units"][0]
            mine, _st, _ms = plan_one(unit["text"], "cmp", "", None, None)
            keys = ("family", "caption", "cfg", "tier", "level")
            print("[cmp] 9889 vs 本树 plan：%s"
                  % ("一致" if all(live.get(k) == mine.get(k) for k in keys)
                     else "不一致 %s vs %s" % ({k: live.get(k) for k in keys},
                                               {k: mine.get(k) for k in keys})))
        except Exception as e:                       # noqa: BLE001 - 只是核对，失败不影响样张
            print("[cmp] 跳过：%s: %s" % (type(e).__name__, e))

    if "TV" in args.only:
        tpath = os.path.join(OUT, "rewrite_T.txt")
        rounds = read_t_rounds(tpath)
        for sess, tag in (("s22f8c329be5e", "AT"), ("se6222405884d", "BT")):
            line = rounds.get(sess)
            if not line:
                print("[TV] 找不到 %s 的整轮，跳过" % sess)
                continue
            units = tv_units(sess, line)
            d = render_round(tag, units, os.path.join(OUT, "%s_TV.wav" % tag))
            d["units"] = [{"unit_id": u["unit_id"], "ja_in": u["ja_in"], "anchor_why": u["anchor_why"]}
                          for u in units]
            runs["rounds"].setdefault("TV", {})[tag] = d
            print("[TV] %s %d 块 %.2fs 首声 %.2fs" % (tag, len(d["blocks"]),
                                                     d["stats_total"]["dur_s"], d["first_audio_s"]))
    json.dump(runs, open(runs_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    res = {}
    for rid in ("A", "B"):
        if "V" in runs["rounds"].get(rid, {}):
            res[rid] = runs["rounds"][rid]["V"]
    for tag in ("AT", "BT"):
        if tag in runs["rounds"].get("TV", {}):
            res[tag] = runs["rounds"]["TV"][tag]
    append_index(runs, res)
    print("[done]")


if __name__ == "__main__":
    main()
