# -*- coding: utf-8 -*-
"""情感评测台：固定句集 × 固定 seed × 可描述条件（caption 锚 + 正文 emoji 位置），
每条量 时长 / F0 / 能量动态 / 字速 / 发声块 / 坏音，可选说话人相似度。

只读写生产后端 :9889（`/v2/render` 白名单内），不改生产代码、不起第二个模型实例。
条件矩阵可以从 JSON 读，也可以直接用内置的 emoji 位置实验（默认）。
结果写 `--out`（必须是 tests/output/ 下），自动生成一页 README（表格 + 「先听这几条」）。

条件可以带 `caption` / `cfg` / `caption_mode: "free"`（阶段 2）：自由描述是训练集外的 caption，
**只有带 `caption_mode=free` 时后端才放过码本白名单**（`render_queue._check_unit` 的实验路径）。
free 条件直接打 `/v2/render`，不走 `/v2/plan`（规划层仍然只认码本内的锚）。
固定矩阵见 `tests/tts/data/emotion_free_20261026.json`：`--matrix emotion_free_20261026` 就能跑。

命令行（前置：:9889 在跑；用 irodori 的 venv，脚本只用 httpx/numpy）：

    python -B tests/tts/probes/emotion_bench.py --out tests/output/emoji-pos-20261026

    python -B tests/tts/probes/emotion_bench.py --dump-matrix _tmp/matrix.json      # 只看条件表
    python -B tests/tts/probes/emotion_bench.py --matrix _tmp/matrix.json --out ...  # 自定义条件
    python -B tests/tts/probes/emotion_bench.py --report-only --out ...              # 用已有 metrics.json 重出 README

口径（与 2026-10-26 摸底一致，见 tests/output/tts-emotion-survey-20261026/SURVEY.md）：
- 时长由预测器决定、与 seed 无关，所以时长差就是条件差；
- F0 用自相关，同一个人的不同条件可以比，绝对值不能跨人比；
- 判断「可测」要同时超过「同句同锚换 seed 的 spread」和最小可辨阈值（时长 0.15 s /
  F0 10 Hz / 动态 2 dB / 字速 0.3 字每秒），且各 seed 方向一致；
- 比这小的差异一律当噪声，不写进结论。
"""
import argparse
import asyncio
import base64
import io
import json
import pathlib
import statistics
import time
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_BACKEND = "http://127.0.0.1:9889"
VOICE = "albion"
PLAN_VERSION = 4
CODEBOOK_VERSION = 2

# ---- 固定材料：6 句自编日语（长短、陈述/疑问都有；不用真实聊天原文）----------------

SENTENCES = [
    dict(id="s1", kind="陈述", note="长句、逗号在中间",
         text="今日は少しだけ遠回りして、いつもの喫茶店に寄ってから帰ろうと思うんだ。"),
    dict(id="s2", kind="疑问", note="短问句",
         text="ねえ、本当にそれでいいの？"),
    dict(id="s3", kind="陈述", note="中句、逗号靠前",
         text="雨の音が、なんだか落ち着くよね。"),
    dict(id="s4", kind="疑问", note="中长问句、逗号在中前",
         text="明日の朝は早いのに、こんな時間まで起きてて平気なの？"),
    dict(id="s5", kind="陈述", note="最短句",
         text="ありがとう。"),
    dict(id="s6", kind="陈述", note="中句、两个逗号",
         text="紅茶と、それから焼き菓子も、少しだけ頂戴。"),
]

SENTENCES_BY_ID = {s["id"]: s for s in SENTENCES}

# 生产码本里合法的一条 (family, caption, cfg) —— 实验统一用中性锚，只让 emoji 一个变量动。
ANCHORS = {
    "quiet": dict(family="neutral", caption="落ち着いた、静かな声で", cfg=3.0, tier="weak"),
}

STYLE_EMOJIS = [
    ("heart_hands", "🫶", "優しく"),
    ("sleepy", "😪", "眠そうに、気だるげに"),
    ("turtle", "🐢", "ゆっくりと"),
    ("fast", "⏩", "早口、一気に"),
    ("panic", "😰", "慌てて、動揺"),
    ("tease", "😏", "からかうように"),
    ("tremble", "🥺", "声を震わせながら"),
    ("shy", "🫣", "恥ずかしそうに"),
    ("happy", "😊", "楽しげに、嬉しそうに"),
    ("angry", "😠", "怒り、不満げに"),
    ("worry", "😟", "心配そうに"),
]

NONVERBAL_EMOJIS = [
    ("giggle", "🤭", "笑い（くすくす）"),
    ("sigh", "😮‍💨", "吐息、溜息"),
    ("gasp", "😮", "息をのむ"),
    ("yawn", "🥱", "あくび"),
    ("nod", "👌", "相槌、頷く音"),
    ("cry", "😭", "嗚咽、泣き声"),
]

ALL_EMOJIS = STYLE_EMOJIS + NONVERBAL_EMOJIS
EMOJI_BY_ID = {k: e for k, e, _ in ALL_EMOJIS}

POSITIONS = [("head", "句首"), ("mid", "句中（逗号后）"), ("end", "句末")]

MAIN_CARRIER = "s1"       # 主句：11×3 全网格，2 个 seed
REPLICA_CARRIER = "s4"    # 复现句：5 个代表 emoji，2 个 seed
REPLICA_EMOJIS = ["happy", "angry", "tremble", "fast", "sigh"]
STACK_EMOJIS = ["happy", "tremble", "cry"]   # 单个 vs 叠两个
PAIR = ("s3", "s2")       # 两句连写，只在第一句放 emoji（看串句）

DEFAULT_SEEDS = [12345, 777]
NOISE_SEEDS = [1, 2, 3, 12345, 777]   # 只给主句基线，量噪声底

# 最小可辨阈值（超过噪声 spread 之后还要过这一关）
MIN_DELTA = dict(dur=0.15, f0=10.0, dyn=2.0, rate=0.30)

# 坏音判据
BAD_CLIP_RATIO = 0.001     # >0.1% 样本贴顶
BAD_SILENT_RATIO = 0.60    # 静音占比 >60%
BAD_PEAK_DB = -30.0        # 峰值太低（疑似没出声）
BAD_DUR_S = (0.40, 25.0)   # 太短像截断/太长像跑飞
BAD_F0_COVER = 0.15        # 周期性帧太少（不是人声）


class BenchError(RuntimeError):
    pass


# ---- 纯函数：插入 / 计数 -------------------------------------------------------

def is_spoken_char(ch):
    """能念出来的字：平假名/片假名/汉字/拉丁字母/数字。emoji、标点、空白都不算。"""
    o = ord(ch)
    return (
        0x3041 <= o <= 0x309F or      # 平假名
        0x30A1 <= o <= 0x30FC or      # 片假名 + 长音符 ー
        0x4E00 <= o <= 0x9FFF or      # 汉字（常用）
        0x3400 <= o <= 0x4DBF or      # 汉字（扩展 A）
        0xFF10 <= o <= 0xFF19 or      # 全角数字
        0xFF21 <= o <= 0xFF3A or      # 全角大写
        0xFF41 <= o <= 0xFF5A or      # 全角小写
        (ch.isascii() and ch.isalnum())
    )


def spoken_chars(text):
    """计入字速的字数（去掉标点、空白、emoji）。"""
    return sum(1 for c in text if is_spoken_char(c))


def _first_comma(text):
    """第一个「句中」逗号的下标（没有返回 -1；末尾标点不算句中）。"""
    for i, ch in enumerate(text):
        if ch in "、，" and i < len(text) - 1:
            return i
    return -1


def insert_emoji(text, emoji, pos, repeat=1):
    """把 emoji 放到句首 / 句中（第一个逗号后）/ 句末，返回新文本。

    - head：接在最前面；
    - mid：第一个逗号后面；没有逗号就按「可念字数」的中点切开（不切断代理对）；
    - end：接在整句后面（含原有句末标点之后，与 2026-10-26 摸底口径一致）；
    - end1：接在**第一句**的句末标点之后（只有两句连写时才和 end 不同）；
    - none：原样返回。
    """
    if pos == "none":
        return text
    if repeat < 1:
        raise ValueError("repeat 必须 >= 1")
    if not emoji:
        raise ValueError("emoji 不能为空")
    mark = emoji * int(repeat)
    if pos == "head":
        return mark + text
    if pos == "end":
        return text + mark
    if pos == "end1":
        for i, ch in enumerate(text):
            if ch in "。！？!?":
                return text[:i + 1] + mark + text[i + 1:]
        return text + mark
    if pos != "mid":
        raise ValueError("pos 只能是 none/head/mid/end/end1：%r" % (pos,))
    ci = _first_comma(text)
    if ci >= 0:
        return text[:ci + 1] + mark + text[ci + 1:]
    spoken = [i for i, c in enumerate(text) if is_spoken_char(c)]
    if not spoken:
        return text + mark
    cut = spoken[(len(spoken) + 1) // 2]
    return text[:cut] + mark + text[cut:]


# ---- 纯函数：指标 -------------------------------------------------------------

def f0_track(a, sr, lo_hz=70.0, hi_hz=450.0, win_s=0.04, hop_s=0.01, voicing=0.5):
    """自相关 F0（40 ms 窗 / 10 ms 步，归一化峰值 >0.5 才算有声）。返回 Hz 列表。"""
    a = np.asarray(a, dtype="float64")
    fl, hop = max(2, int(win_s * sr)), max(1, int(hop_s * sr))
    lo, hi = int(sr / hi_hz), int(sr / lo_hz)
    out = []
    for i in range(0, max(0, len(a) - fl), hop):
        fr = a[i:i + fl]
        if len(fr) < fl or np.sqrt((fr ** 2).mean()) < 1e-3:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, mode="full")[fl - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        if not len(seg):
            continue
        k = int(np.argmax(seg))
        if seg[k] < voicing:
            continue
        out.append(sr / float(lo + k))
    return out


def speech_blocks(samples, sr, frame_s=0.01, drop_db=40.0, floor_db=-55.0, gap_s=0.15):
    """按 10 ms 包络切「发声块」，块间静音超过 150 ms 算断开。

    阈值 = max(峰值−40 dB, −55 dBFS)；返回 [(起秒, 止秒), …]，止秒是最后一个有声帧的末端。
    """
    win = max(1, int(frame_s * sr))
    n = len(samples) // win
    if n <= 0:
        return []
    env = np.abs(samples[:n * win].reshape(n, win)).max(1)
    peak = float(env.max())
    if peak <= 0:
        return []
    thr = max(peak * 10 ** (-drop_db / 20.0), 10 ** (floor_db / 20.0))
    gap_frames = int(round(gap_s / frame_s))
    blocks, start, last = [], None, None
    for i, act in enumerate(env > thr):
        if act:
            if start is None:
                start = i
            last = i
        elif start is not None and (i - last) > gap_frames:
            blocks.append((start, last + 1))
            start = last = None
    if start is not None:
        blocks.append((start, last + 1))
    return [(round(s * frame_s, 3), round(e * frame_s, 3)) for s, e in blocks]


def _db(x):
    return float(20 * np.log10(max(float(x), 1e-9)))


def _dyn_db(voiced, sr, frame_s=0.02):
    fl = max(1, int(frame_s * sr))
    frames = [_db(np.sqrt((np.asarray(voiced[i:i + fl], dtype="float64") ** 2).mean()))
              for i in range(0, max(1, len(voiced) - fl), fl)]
    return float(np.std(frames)) if frames else 0.0


def window_metrics(samples, sr, t0, t1):
    """时间窗内的 F0 均值 / 动态 / RMS（判「作用范围」和「有没有串句」）。"""
    i0, i1 = max(0, int(t0 * sr)), min(len(samples), int(t1 * sr))
    seg = np.asarray(samples[i0:i1], dtype="float32")
    if len(seg) < int(0.10 * sr):
        return dict(f0_mean=None, dyn_db=None, rms_db=None)
    f0 = f0_track(seg, sr)
    return dict(
        f0_mean=round(statistics.fmean(f0), 1) if f0 else None,
        dyn_db=round(_dyn_db(seg, sr), 1),
        rms_db=round(_db(np.sqrt((seg.astype("float64") ** 2).mean())), 1),
    )


def analyze(samples, sr, text, blocks=None):
    """一条 wav → 指标 dict。samples 单声道 float，-1..1。"""
    samples = np.asarray(samples, dtype="float32")
    dur = len(samples) / float(sr) if sr else 0.0
    blocks = speech_blocks(samples, sr) if blocks is None else blocks
    v0 = blocks[0][0] if blocks else 0.0
    v1 = blocks[-1][1] if blocks else 0.0
    voiced = samples[int(v0 * sr):int(v1 * sr)] if v1 > v0 else samples
    voiced_s = max(0.0, v1 - v0)
    chars = spoken_chars(text)
    f0 = f0_track(voiced, sr) if len(voiced) else []
    peak = float(np.abs(samples).max()) if len(samples) else 0.0
    rms = float(np.sqrt((np.asarray(voiced, dtype="float64") ** 2).mean())) if len(voiced) else 0.0
    return dict(
        sr=sr, dur=round(dur, 3), voiced_s=round(voiced_s, 3),
        lead_s=round(v0, 3), tail_s=round(max(0.0, dur - v1), 3), chars=chars,
        peak_db=round(_db(peak), 1), rms_db=round(_db(rms), 1),
        dyn_db=round(_dyn_db(voiced, sr), 1) if len(voiced) else 0.0,
        f0_mean=round(statistics.fmean(f0), 1) if f0 else None,
        f0_std=round(statistics.pstdev(f0), 1) if len(f0) > 1 else None,
        f0_p10=round(float(np.percentile(f0, 10)), 1) if f0 else None,
        f0_p90=round(float(np.percentile(f0, 90)), 1) if f0 else None,
        f0_cover=round(len(f0) * 0.01 / voiced_s, 2) if voiced_s > 0 else 0.0,
        rate_cps=round(chars / voiced_s, 2) if voiced_s > 0 else None,
        clip_ratio=round(float((np.abs(samples) > 0.999).sum()) / max(1, len(samples)), 5),
        silent_ratio=round(1.0 - voiced_s / dur, 3) if dur > 0 else 1.0,
        n_blocks=len(blocks), blocks=[list(b) for b in blocks],
        head2=window_metrics(samples, sr, v0, v0 + 2.0),
        tail2=window_metrics(samples, sr, max(v0, v1 - 2.0), v1),
    )


def bad_audio(m):
    """坏音判据，返回原因列表（空列表 = 没坏）。"""
    why = []
    dur = m.get("dur") or 0.0
    if dur < BAD_DUR_S[0]:
        why.append("时长<%.1fs（空/截断）" % BAD_DUR_S[0])
    if dur > BAD_DUR_S[1]:
        why.append("时长>%.0fs（跑飞）" % BAD_DUR_S[1])
    if (m.get("clip_ratio") or 0.0) > BAD_CLIP_RATIO:
        why.append("削波样本>%.1f%%" % (BAD_CLIP_RATIO * 100))
    if (m.get("silent_ratio") or 0.0) > BAD_SILENT_RATIO:
        why.append("静音占比>%.0f%%" % (BAD_SILENT_RATIO * 100))
    if (m.get("peak_db") if m.get("peak_db") is not None else 0.0) < BAD_PEAK_DB:
        why.append("峰值<-30dBFS")
    if (m.get("f0_cover") or 0.0) < BAD_F0_COVER:
        why.append("周期性帧<%.0f%%" % (BAD_F0_COVER * 100))
    if not m.get("n_blocks"):
        why.append("无发声块")
    return why


# ---- 条件矩阵 ----------------------------------------------------------------

def build_matrix(seeds=None, noise_seeds=None):
    """内置实验：emoji 位置（风格类 / 非语言类 / 叠加 / 两句连写）+ 每句基线。"""
    seeds = list(seeds or DEFAULT_SEEDS)
    noise_seeds = list(noise_seeds or NOISE_SEEDS)
    sentences = [dict(s) for s in SENTENCES]
    conds = []

    def add(**kw):
        kw.setdefault("anchor", "quiet")
        kw.setdefault("repeat", 1)
        kw.setdefault("pos", "none")
        kw.setdefault("emoji", "")
        kw.setdefault("emoji_id", "")
        kw.setdefault("group", "base")
        conds.append(kw)

    for sid in [s["id"] for s in sentences]:
        for seed in (noise_seeds if sid == MAIN_CARRIER else seeds):
            add(id="%s-base-%d" % (sid, seed), sentence=sid, seed=seed, group="base")

    for eid, emoji, _ in STYLE_EMOJIS:
        for pos, _label in POSITIONS:
            for seed in seeds:
                add(id="%s-style-%s-%s-%d" % (MAIN_CARRIER, eid, pos, seed), sentence=MAIN_CARRIER,
                    emoji=emoji, emoji_id=eid, pos=pos, seed=seed, group="style")

    for eid, emoji, _ in NONVERBAL_EMOJIS:
        for pos, _label in POSITIONS:
            for seed in seeds:
                add(id="%s-verb-%s-%s-%d" % (MAIN_CARRIER, eid, pos, seed), sentence=MAIN_CARRIER,
                    emoji=emoji, emoji_id=eid, pos=pos, seed=seed, group="nonverbal")

    for eid in REPLICA_EMOJIS:
        for pos, _label in POSITIONS:
            for seed in seeds:
                add(id="%s-repl-%s-%s-%d" % (REPLICA_CARRIER, eid, pos, seed),
                    sentence=REPLICA_CARRIER, emoji=EMOJI_BY_ID[eid], emoji_id=eid, pos=pos,
                    seed=seed, group="replica")

    for eid in STACK_EMOJIS:
        for pos in ("mid", "end"):
            for seed in seeds:
                add(id="%s-stack2-%s-%s-%d" % (MAIN_CARRIER, eid, pos, seed), sentence=MAIN_CARRIER,
                    emoji=EMOJI_BY_ID[eid], emoji_id=eid, pos=pos, repeat=2, seed=seed, group="stack")

    sentences.append(dict(id="pair", kind="两句连写", note="%s + %s" % PAIR,
                          text=SENTENCES_BY_ID[PAIR[0]]["text"] + SENTENCES_BY_ID[PAIR[1]]["text"]))
    for seed in seeds:
        add(id="pair-base-%d" % seed, sentence="pair", seed=seed, group="pair-base")
        for pos in ("mid", "end1"):
            add(id="pair-style-happy-%s-%d" % (pos, seed), sentence="pair", emoji="😊",
                emoji_id="happy", pos=pos, seed=seed, group="pair")
            add(id="pair-verb-cry-%s-%d" % (pos, seed), sentence="pair", emoji="😭",
                emoji_id="cry", pos=pos, seed=seed, group="pair")

    return dict(voice=VOICE, plan_version=PLAN_VERSION, codebook_version=CODEBOOK_VERSION,
                anchors=ANCHORS, seeds=seeds, noise_seeds=noise_seeds,
                sentences=sentences, conditions=conds)


def normalize_matrix(raw):
    """校验矩阵并补默认值；返回 (matrix, {句 id: 文本})。"""
    import copy
    if not isinstance(raw, dict):
        raise BenchError("矩阵必须是对象")
    raw = copy.deepcopy(raw)
    sentences = raw.get("sentences") or []
    if not sentences:
        raise BenchError("矩阵缺 sentences")
    texts, seen = {}, set()
    for s in sentences:
        if not isinstance(s, dict) or not s.get("id") or not s.get("text"):
            raise BenchError("sentences 每项要有 id 和 text：%r" % (s,))
        if s["id"] in seen:
            raise BenchError("句子 id 重复：%s" % s["id"])
        seen.add(s["id"])
        texts[s["id"]] = s["text"]
    anchors = raw.get("anchors") or ANCHORS
    conds, ids = [], set()
    for c in raw.get("conditions") or []:
        cid = c.get("id") or ""
        if not cid:
            raise BenchError("条件缺 id：%r" % (c,))
        if cid in ids:
            raise BenchError("条件 id 重复：%s" % cid)
        ids.add(cid)
        if c.get("sentence") not in texts:
            raise BenchError("条件 %s 的 sentence 不在句集里：%r" % (cid, c.get("sentence")))
        anchor = c.get("anchor", "quiet")
        if anchor not in anchors:
            raise BenchError("条件 %s 的 anchor 未知：%r" % (cid, anchor))
        if int(c.get("repeat", 1)) < 1:
            raise BenchError("条件 %s 的 repeat 必须 >= 1" % cid)
        pos = c.get("pos", "none")
        if pos not in [p for p, _ in POSITIONS] + ["none", "end1"]:
            raise BenchError("条件 %s 的 pos 未知：%r" % (cid, pos))
        if c.get("emoji") and pos == "none":
            raise BenchError("条件 %s 有 emoji 但 pos=none" % cid)
        if not c.get("emoji") and pos != "none":
            raise BenchError("条件 %s 的 pos=%s 但没有 emoji" % (cid, pos))
        if c.get("caption") is not None:
            if not isinstance(c["caption"], str) or not c["caption"].strip():
                raise BenchError("条件 %s 的 caption 必须是非空字符串" % cid)
        if c.get("cfg") is not None:
            if isinstance(c["cfg"], bool) or not isinstance(c["cfg"], (int, float)):
                raise BenchError("条件 %s 的 cfg 必须是数值：%r" % (cid, c["cfg"]))
        mode = c.get("caption_mode")
        if mode is not None and mode != "free":
            raise BenchError("条件 %s 的 caption_mode 只支持 \"free\"：%r" % (cid, mode))
        if mode == "free" and c.get("caption") is None:
            raise BenchError("条件 %s 是 free 模式但没有给 caption" % cid)
        base_anchor = c.get("baseline_anchor")
        if base_anchor is not None and base_anchor not in anchors:
            raise BenchError("条件 %s 的 baseline_anchor 未知：%r" % (cid, base_anchor))
        conds.append(dict(c, anchor=anchor, repeat=int(c.get("repeat", 1)), pos=pos))
    if not conds:
        raise BenchError("矩阵没有 conditions")
    if raw.get("noise_reference") is not None and raw["noise_reference"] not in anchors:
        raise BenchError("noise_reference 未知：%r" % (raw["noise_reference"],))
    raw.update(anchors=anchors, conditions=conds, seeds=list(raw.get("seeds") or DEFAULT_SEEDS))
    return raw, texts


def condition_text(cond, texts):
    return insert_emoji(texts[cond["sentence"]], cond.get("emoji") or "",
                        cond.get("pos", "none"), cond.get("repeat", 1))


def unit_spec(cond, anchors):
    """条件 → 送进 `/v2/render` 的单元条件（锚 + 每个条件的覆盖）。

    覆盖键：`caption`（自由描述）、`cfg`、`tier`、`caption_mode`（"free" 才让后端放过码本白名单）。
    换了 caption 的条件不再带锚的 `tier`（那是码本档位，自由描述没有档位，别往日志里写假信息）。
    """
    a = dict(anchors[cond.get("anchor", "quiet")])
    for k in ("caption", "cfg", "tier", "caption_mode"):
        if cond.get(k) is not None:
            a[k] = cond[k]
    if cond.get("caption") is not None:
        a.pop("tier", None)
    return a


# ---- 指标比较 ----------------------------------------------------------------

def noise_floor(rows, reference=None):
    """同一句**同一锚**换 seed 的 spread（取各句最大），作为「噪声」上限。

    只算参考锚（默认 `quiet`）的行：多锚矩阵里别的锚是**被测对象**，混进去会把噪声底撑爆。
    """
    reference = reference or "quiet"
    per = {}
    for r in rows:
        if r.get("group") != "base" or r.get("anchor") != reference:
            continue
        b = per.setdefault(r["sentence"], dict(dur=[], f0=[], dyn=[], rate=[]))
        b["dur"].append(r["dur"])
        if r.get("f0_mean") is not None:
            b["f0"].append(r["f0_mean"])
        b["dyn"].append(r["dyn_db"])
        if r.get("rate_cps") is not None:
            b["rate"].append(r["rate_cps"])
    if not per:
        # 参考锚没对上（自定义矩阵没设 noise_reference）：退回第一条 base 行用的锚，别静默出空表
        for r in rows:
            if r.get("group") == "base":
                return noise_floor(rows, r.get("anchor"))
    floor, detail = {}, {}
    for key, b in per.items():
        d = dict(dur=round(max(b["dur"]) - min(b["dur"]), 3),
                 f0=round(max(b["f0"]) - min(b["f0"]), 1) if len(b["f0"]) > 1 else 0.0,
                 dyn=round(max(b["dyn"]) - min(b["dyn"]), 1),
                 rate=round(max(b["rate"]) - min(b["rate"]), 2) if len(b["rate"]) > 1 else 0.0)
        detail[key] = dict(spread=d, n_seeds=len(b["dur"]),
                           seeds=dict(dur=[min(b["dur"]), max(b["dur"])],
                                      f0=[min(b["f0"]), max(b["f0"])] if b["f0"] else None,
                                      dyn=[min(b["dyn"]), max(b["dyn"])],
                                      rate=[min(b["rate"]), max(b["rate"])]))
        for k, v in d.items():
            floor[k] = max(floor.get(k, 0.0), v)
    return floor, detail


def deltas(row, base):
    """条件相对同句同 seed 基线的差（基线缺失返回 None）。"""
    if not base:
        return None
    out = dict(dur=round(row["dur"] - base["dur"], 3),
               voiced=round(row["voiced_s"] - base["voiced_s"], 3),
               dyn=round(row["dyn_db"] - base["dyn_db"], 1),
               lead=round(row["lead_s"] - base["lead_s"], 3),
               head_f0=_diff(row, base, "head2", "f0_mean", 1),
               tail_f0=_diff(row, base, "tail2", "f0_mean", 1),
               head_dyn=_diff(row, base, "head2", "dyn_db", 1),
               tail_dyn=_diff(row, base, "tail2", "dyn_db", 1))
    out["f0"] = (_diff(row, base, None, "f0_mean", 1))
    out["rate"] = (_diff(row, base, None, "rate_cps", 2))
    return out


def _diff(row, base, window, key, nd):
    a = (row.get(window) or {}).get(key) if window else row.get(key)
    b = (base.get(window) or {}).get(key) if window else base.get(key)
    if a is None or b is None:
        return None
    return round(a - b, nd)


def judge(values, floor, key):
    """values: 各 seed 的 Δ；floor: 噪声 spread；返回 (均值, 是否可测)。

    可测 = |均值| 超过 max(噪声 spread, 最小可辨阈值)，且各 seed 同号（不互换）。
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return None, False
    mean = statistics.fmean(vals)
    thr = max(floor.get(key, 0.0), MIN_DELTA[key])
    agree = all(v > 0 for v in vals) or all(v < 0 for v in vals)
    return round(mean, 2), bool(abs(mean) > thr and agree)


# ---- 渲染 --------------------------------------------------------------------

def decode_wav(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    return sr, (a.reshape(-1, ch).mean(1) if ch > 1 else a)


def load_wav(path):
    return decode_wav(pathlib.Path(path).read_bytes())


async def render_unit(client, backend, text, anchor, seed, voice=VOICE):
    """一次 /v2/render（单 unit）。返回 (wav bytes, None) 或 (None, 错误文本)。

    `anchor` 是**解析后**的单元条件（见 `unit_spec`）：family/caption/cfg/tier，可带
    `caption_mode="free"` —— 那是后端的实验开关（训练集外的 caption 要它才放行）。
    """
    opened = (await client.post(backend + "/v2/render/open", json={"voice": voice})).json()
    addr = {"session_id": opened["session_id"], "generation": opened["generation"]}
    unit = dict(unit_id="u0", sub_index=0, text=text, family=anchor["family"],
                caption=anchor["caption"], cfg=anchor["cfg"], scene_id=1,
                codebook_version=CODEBOOK_VERSION)
    if anchor.get("tier"):
        unit["tier"] = anchor["tier"]
    if anchor.get("caption_mode"):
        unit["caption_mode"] = anchor["caption_mode"]
    if seed is not None:
        unit["seed"] = int(seed)
    body = dict(addr, plan_version=PLAN_VERSION, voice=voice, format="wav", units=[unit])
    blob, done, err = b"", False, None
    try:
        async with client.stream("POST", backend + "/v2/render", json=body, timeout=180) as r:
            if r.status_code != 200:
                return None, "HTTP %d: %s" % (r.status_code,
                                              (await r.aread()).decode("utf8", "ignore")[:300])
            ev = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    pay = json.loads(line[5:].strip())
                    if ev == "audio_chunk":
                        blob += base64.b64decode(pay["audio_base64"])
                    elif ev == "done":
                        done = pay.get("status") == "complete"
                    elif ev in ("unit.failed", "render.cancelled", "error"):
                        err = pay
    finally:
        try:
            await client.post(backend + "/v2/render/" + ("close" if done else "cancel"), json=addr)
        except Exception:
            pass
    if not done or not blob:
        return None, "render incomplete: %r" % (err,)
    return blob, None


DATA_DIR = ROOT / "tests" / "tts" / "data"


def resolve_matrix_path(name):
    """`--matrix` 的解析：先按给的路径，再按 `tests/tts/data/<名字>[_可选 .json]`（固定材料放那儿）。"""
    given = pathlib.Path(name)
    if given.exists():
        return given
    for cand in (DATA_DIR / given.name, DATA_DIR / (given.name + ".json")):
        if cand.exists():
            return cand
    raise BenchError("矩阵文件找不到：%s（也试过 %s）" % (name, DATA_DIR))


def backend_health(backend):
    try:
        import httpx
        r = httpx.get(backend + "/health", timeout=5.0)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


# ---- 报告 --------------------------------------------------------------------

def fmt(v, spec="%.2f", dash="—"):
    return dash if v is None else spec % v


def _cell(mean, ok, spec="%+.2f"):
    if mean is None:
        return "—"
    return (spec + "*") % mean if ok else (spec % mean)


def _pick(rows, group, eid=None, pos=None, seed=None):
    out = []
    for r in rows:
        if r.get("group") != group:
            continue
        if eid is not None and r.get("emoji_id") != eid:
            continue
        if pos is not None and r.get("pos") != pos:
            continue
        if seed is not None and r.get("seed") != seed:
            continue
        out.append(r)
    return out


def _mean_delta(rows, key, nd=2):
    vals = [(r.get("delta") or {}).get(key) for r in rows]
    vals = [v for v in vals if v is not None]
    return round(statistics.fmean(vals), nd) if vals else None


def _mean_field(rows, key, nd=3):
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    return round(statistics.fmean(vals), nd) if vals else None


def _blocks_note(rows):
    return "、".join("%s→%s" % (r.get("base_blocks"), r["n_blocks"]) for r in rows)


def _bad_note(rows):
    bad = ["、".join(r["bad"]) for r in rows if r.get("bad")]
    return ";".join(bad) if bad else "ok"


def span2(row):
    """第二句跨度：最后一块的结束 − 倒数第二块的开始（两句连写时才用）。"""
    b = row.get("blocks") or []
    if len(b) < 2:
        return None
    return round(b[-1][1] - b[-2][0], 3)


def _win_delta(row, base, window, key, nd=1):
    a = (row.get(window) or {}).get(key)
    b = (base.get(window) or {}).get(key)
    if a is None or b is None:
        return None
    return round(a - b, nd)


def render_readme(run_name, matrix, rows, failures, floor, floor_detail, has_speaker=False):
    L = []
    add = L.append
    sentences = {s["id"]: s for s in matrix["sentences"]}
    bad_rows = [r for r in rows if r.get("bad")]
    seeds = sorted({r["seed"] for r in rows})
    anchors = matrix.get("anchors") or ANCHORS
    ref_anchor = matrix.get("noise_reference") or "quiet"
    if not any(r.get("anchor") == ref_anchor for r in rows):
        # 矩阵没声明参考锚：跟 noise_floor 一样退回第一条 base 行的锚
        ref_anchor = next((r["anchor"] for r in rows if r.get("group") == "base"), ref_anchor)

    def fl_for(sid):
        """同句换 seed 的 spread（每句自己的）；没有就退回全局上限（保守）。"""
        return (floor_detail.get(sid) or {}).get("spread") or floor

    fl_main, fl_repl = fl_for(MAIN_CARRIER), fl_for(REPLICA_CARRIER)

    add("# 情感评测台：%s" % run_name)
    add("")
    add("由 `tests/tts/probes/emotion_bench.py` 自动生成。共 **%d** 条 wav，**%d** 条坏音，失败 **%d** 条。"
        % (len(rows), len(bad_rows), len(failures)))
    add("")
    add("后端 `%s`（`/v2/render`，voice=%s，plan_version=%s，码本 v%s）。seed=%s。"
        % (matrix.get("backend", DEFAULT_BACKEND), matrix.get("voice", VOICE),
           matrix.get("plan_version", PLAN_VERSION), matrix.get("codebook_version", CODEBOOK_VERSION),
           ", ".join(str(s) for s in seeds)))
    add("")
    add("锚：" + "；".join("`%s`=%s／%s／cfg %s" % (aid, a.get("family"), a.get("caption"), a.get("cfg"))
                          for aid, a in anchors.items()) + "。")
    n_free = len({c["id"] for c in matrix["conditions"] if c.get("caption_mode") == "free"})
    if n_free:
        n_sweep = len({c["id"] for c in matrix["conditions"] if c.get("group") == "free_cfg"})
        add("")
        add("其中 **%d 条条件**（%d 条自由描述%s）走 `caption_mode=free`：caption 是训练集外的"
            "日语描述，刻意绕过码本白名单（只为量现有 LoRA 能不能听懂；生产网关不带这个字段）。"
            % (n_free, n_free - n_sweep,
               " + %d 条同描述换 cfg" % n_sweep if n_sweep else ""))
    elif len(anchors) == 1:
        add("")
        add("caption 固定为上面的锚，**只让正文 emoji 一个变量动**。")
    add("")
    add("句集：" + "；".join("%s「%s」" % (s["id"], s["text"]) for s in matrix["sentences"]
                          if s["id"] != "pair"))
    add("")
    add("## 口径与噪声底")
    add("")
    add("时长由预测器决定、与 seed 无关，所以**时长差就是条件差**；F0 用自相关（只有部分帧可信，"
        "只能同一个人比）。判「可测」要同时满足：超过**该句自己**换 seed 的 spread **且** 超过最小可辨阈值"
        "（时长 %.2f s / F0 %.0f Hz / 动态 %.1f dB / 字速 %.2f），并且每个 seed 方向一致——"
        "表里带 `*` 的是可测。" % (MIN_DELTA["dur"], MIN_DELTA["f0"], MIN_DELTA["dyn"],
                                MIN_DELTA["rate"]))
    add("")
    add("两条读表须知：**(1)** Δ时长 是确定性的（同文本同 seed 不换、跟 seed 无关），可以当真；"
        "Δ发声 / Δ首静音 会受「某条渲染开头很低、低于峰值−40 dB」影响，±0.9 s 内别当结论。")
    if any(r.get("emoji") for r in rows):
        add("**(2)** 字速 = 字数 ÷ 发声时长，emoji 多出来的非语言声会算进分母，所以句末/非语言类的"
            "负 Δ字速 **不代表念得更慢**；要判主体语速只看句首/句中的 Δ字速。")
    else:
        add("**(2)** 字速 = 字数 ÷ 发声时长：同一条文本里，时长变长而字数不变，字速必然一起降，"
            "两个指标不要当两条独立证据读。")
    add("")
    if floor_detail:
        add("| 句 | seed 数 | 时长范围 s | F0 范围 Hz | 动态范围 dB | 字速范围 |")
        add("|---|---|---|---|---|---|")
        for sid in sorted(floor_detail):
            d = floor_detail[sid]
            sp = d["seeds"]
            add("| %s | %d | %.2f–%.2f | %s | %.1f–%.1f | %s |" % (
                sid, d["n_seeds"], sp["dur"][0], sp["dur"][1],
                "%.0f–%.0f" % tuple(sp["f0"]) if sp["f0"] else "—",
                sp["dyn"][0], sp["dyn"][1],
                "%.2f–%.2f" % tuple(sp["rate"]) if sp["rate"] else "—"))
        add("")
        add("噪声上限：时长 ±%.2f s、F0 ±%.0f Hz、动态 ±%.1f dB、字速 ±%.2f。"
            % (floor.get("dur", 0), floor.get("f0", 0), floor.get("dyn", 0), floor.get("rate", 0)))
        add("")

    # --- 码本锚基线（相对参考锚）---
    base_rows = [r for r in rows if r.get("group") == "base" and r.get("anchor") != ref_anchor]
    if base_rows:
        add("## 码本锚基线（各自相对参考锚 `%s`）" % ref_anchor)
        add("")
        add("| 锚 | family | caption | cfg | 句 | Δ时长 s | ΔF0 Hz | Δ动态 dB | Δ字速 | 块数 |")
        add("|---|---|---|---|---|---|---|---|---|---|")
        for aid, anc in anchors.items():
            if aid == ref_anchor:
                continue
            for sid in sorted({r["sentence"] for r in base_rows if r["anchor"] == aid}):
                sel = [r for r in base_rows if r["anchor"] == aid and r["sentence"] == sid]
                if not sel:
                    continue
                fl = fl_for(sid)
                g = lambda k: [(r.get("delta") or {}).get(k) for r in sel]
                dm, dok = judge(g("dur"), fl, "dur")
                fm, fok = judge(g("f0"), fl, "f0")
                ym, yok = judge(g("dyn"), fl, "dyn")
                rm, rok = judge(g("rate"), fl, "rate")
                add("| `%s` | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    aid, anc.get("family"), anc.get("caption"), anc.get("cfg"), sid,
                    _cell(dm, dok), _cell(fm, fok, "%+.1f"), _cell(ym, yok, "%+.1f"),
                    _cell(rm, rok, "%+.2f"), _blocks_note(sel)))
        add("")

    # --- 自由描述（caption_mode=free）---
    free = [r for r in rows if r.get("group") == "free" and r["pos"] == "none"]
    if free:
        add("## 自由描述（`caption_mode=free`）相对参考锚 `%s`" % ref_anchor)
        add("")
        add("训练集外的描述，逐条比同句同 seed 的参考锚。可测标记同前（超过该句 spread 与最小可辨阈值、"
            "各 seed 同号）——**没有 `*` 就是「听不出差别」**，不是「更好」也不是「更差」。")
        add("")
        add("| 描述 | cfg | 句 | Δ时长 s | ΔF0 Hz | Δ动态 dB | Δ字速 | 块数 | 坏音 |")
        add("|---|---|---|---|---|---|---|---|---|")
        marks = dict(dur=0, f0=0, dyn=0, rate=0, n=0)
        seen = []
        for r in free:
            key = (r.get("caption"), r.get("cfg"))
            if key in seen:
                continue
            seen.append(key)
            for sid in sorted({x["sentence"] for x in free
                               if (x.get("caption"), x.get("cfg")) == key}):
                sel = [x for x in free if (x.get("caption"), x.get("cfg")) == key
                       and x["sentence"] == sid]
                fl = fl_for(sid)
                g = lambda k: [(x.get("delta") or {}).get(k) for x in sel]
                dm, dok = judge(g("dur"), fl, "dur")
                fm, fok = judge(g("f0"), fl, "f0")
                ym, yok = judge(g("dyn"), fl, "dyn")
                rm, rok = judge(g("rate"), fl, "rate")
                marks["n"] += 1
                for k, ok in (("dur", dok), ("f0", fok), ("dyn", yok), ("rate", rok)):
                    marks[k] += int(bool(ok))
                add("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    key[0], key[1], sid, _cell(dm, dok), _cell(fm, fok, "%+.1f"),
                    _cell(ym, yok, "%+.1f"), _cell(rm, rok, "%+.2f"), _blocks_note(sel),
                    _bad_note(sel)))
        add("")
        if marks["n"]:
            add("合计 %d 个（描述 × 句）单元里：时长可测 **%d**、F0 可测 **%d**、动态可测 **%d**、"
                "字速可测 **%d** —— 自由描述能不能真的推动声音，看这几个数。"
                % (marks["n"], marks["dur"], marks["f0"], marks["dyn"], marks["rate"]))
            add("")

    # --- 同一描述换 cfg ---
    sweep = [r for r in rows if r.get("group") == "free_cfg" and r["pos"] == "none"]
    if sweep:
        add("## 同一描述换 cfg（2 / 4 / 6）：强度能不能连续调")
        add("")
        add("| 描述 | cfg | 句 | Δ时长 s | ΔF0 Hz | Δ动态 dB | Δ字速 | 可测(时长/F0/动态/字速) |")
        add("|---|---|---|---|---|---|---|---|")
        cfgs = sorted({r.get("cfg") for r in sweep if r.get("cfg") is not None})
        caps = []
        for r in sweep:
            if r.get("caption") not in caps:
                caps.append(r.get("caption"))
        for cap in caps:
            for cfg in cfgs:
                for sid in sorted({x["sentence"] for x in sweep
                                   if x.get("caption") == cap and x.get("cfg") == cfg}):
                    sel = [r for r in sweep if r.get("caption") == cap
                           and r.get("cfg") == cfg and r["sentence"] == sid]
                    if not sel:
                        continue
                    fl = fl_for(sid)
                    g = lambda k: [(x.get("delta") or {}).get(k) for x in sel]
                    dm, dok = judge(g("dur"), fl, "dur")
                    fm, fok = judge(g("f0"), fl, "f0")
                    ym, yok = judge(g("dyn"), fl, "dyn")
                    rm, rok = judge(g("rate"), fl, "rate")
                    add("| %s | %s | %s | %s | %s | %s | %s | %s/%s/%s/%s |" % (
                        cap, cfg, sid, _cell(dm, dok), _cell(fm, fok, "%+.1f"),
                        _cell(ym, yok, "%+.1f"), _cell(rm, rok, "%+.2f"),
                        "Y" if dok else "n", "Y" if fok else "n", "Y" if yok else "n",
                        "Y" if rok else "n"))
        add("")

    # --- 风格类 ---
    style = [r for r in rows if r.get("group") == "style"]
    if style:
        add("## 风格类 emoji：句首 / 句中（逗号后）/ 句末（句 %s）" % MAIN_CARRIER)
        add("")
        add("「作用范围」看首 2 s / 末 2 s 的音高：emoji 放句首而首 2 s 没变 → 作用不到后面整句；"
            "放句中而首 2 s（逗号前那一段）没变 → 只影响它之后。")
        add("")
        add("| emoji | 位置 | Δ时长 s | Δ发声 s | ΔF0 Hz | Δ动态 dB | Δ字速 | 首2s ΔF0 | 末2s ΔF0 "
            "| 块数(基线→本) | 新增尾段 s | 坏音 |")
        add("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for eid, emoji, _ in STYLE_EMOJIS:
            for pos, label in POSITIONS:
                sel = _pick(style, "style", eid, pos)
                if not sel:
                    continue
                g = lambda k: [(r.get("delta") or {}).get(k) for r in sel]
                dm, dok = judge(g("dur"), fl_main, "dur")
                f0m, f0ok = judge(g("f0"), fl_main, "f0")
                dym, dyok = judge(g("dyn"), fl_main, "dyn")
                rtm, rtok = judge(g("rate"), fl_main, "rate")
                hm, hok = judge(g("head_f0"), fl_main, "f0")
                tm, tok = judge(g("tail_f0"), fl_main, "f0")
                add("| %s %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    emoji, eid, label, _cell(dm, dok), fmt(_mean_delta(sel, "voiced")),
                    _cell(f0m, f0ok, "%+.1f"), _cell(dym, dyok, "%+.1f"), _cell(rtm, rtok, "%+.2f"),
                    _cell(hm, hok, "%+.1f"), _cell(tm, tok, "%+.1f"), _blocks_note(sel),
                    fmt(_mean_field(sel, "tail_block_s")), _bad_note(sel)))
        add("")

    # --- 复现句 ---
    repl = [r for r in rows if r.get("group") == "replica"]
    if repl:
        add("## 复现：换一句（%s，疑问句）把同样的三个位置再跑一遍" % REPLICA_CARRIER)
        add("")
        add("| emoji | 位置 | Δ时长 s | ΔF0 Hz | Δ动态 dB | 时长/F0/动态 是否可测 |")
        add("|---|---|---|---|---|---|")
        for eid in REPLICA_EMOJIS:
            emoji = EMOJI_BY_ID[eid]
            for pos, label in POSITIONS:
                sel = _pick(repl, "replica", eid, pos)
                if not sel:
                    continue
                g = lambda k: [(r.get("delta") or {}).get(k) for r in sel]
                dm, dok = judge(g("dur"), fl_repl, "dur")
                f0m, f0ok = judge(g("f0"), fl_repl, "f0")
                dym, dyok = judge(g("dyn"), fl_repl, "dyn")
                add("| %s %s | %s | %s | %s | %s | %s |" % (
                    emoji, eid, label, _cell(dm, dok), _cell(f0m, f0ok, "%+.1f"),
                    _cell(dym, dyok, "%+.1f"),
                    "%s/%s/%s" % ("Y" if dok else "n", "Y" if f0ok else "n", "Y" if dyok else "n")))
        add("")

    # --- 非语言类 ---
    verb = [r for r in rows if r.get("group") == "nonverbal"]
    if verb:
        add("## 非语言类 emoji：多出来的是不是只在那一段（句 %s）" % MAIN_CARRIER)
        add("")
        add("| emoji | 位置 | Δ时长 s | Δ发声 s | ΔF0 Hz | 块数(基线→本) | 首块起点Δ s | 新增尾段 s "
            "| 末2s Δ动态 | 坏音 |")
        add("|---|---|---|---|---|---|---|---|---|---|")
        for eid, emoji, _ in NONVERBAL_EMOJIS:
            for pos, label in POSITIONS:
                sel = _pick(verb, "nonverbal", eid, pos)
                if not sel:
                    continue
                f0m, f0ok = judge([(r.get("delta") or {}).get("f0") for r in sel], fl_main, "f0")
                tdm, tdok = judge([(r.get("delta") or {}).get("tail_dyn") for r in sel], fl_main, "dyn")
                add("| %s %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    emoji, eid, label, fmt(_mean_delta(sel, "dur")), fmt(_mean_delta(sel, "voiced")),
                    _cell(f0m, f0ok, "%+.1f"), _blocks_note(sel), fmt(_mean_delta(sel, "lead")),
                    fmt(_mean_field(sel, "tail_block_s")), _cell(tdm, tdok, "%+.1f"), _bad_note(sel)))
        add("")

    # --- 叠加 ---
    stack = [r for r in rows if r.get("group") == "stack"]
    if stack:
        add("## 单个 vs 叠两个")
        add("")
        add("| emoji | 位置 | 单个 Δ时长 | 叠两个 Δ时长 | 单个 ΔF0 | 叠两个 ΔF0 | 块数 单个 "
            "| 块数 叠两个 | 叠两个新增尾段 s |")
        add("|---|---|---|---|---|---|---|---|")
        for eid in STACK_EMOJIS:
            emoji = EMOJI_BY_ID[eid]
            for pos, label in POSITIONS:
                if pos == "head":
                    continue
                one = _pick(style, "style", eid, pos) or _pick(verb, "nonverbal", eid, pos)
                two = _pick(stack, "stack", eid, pos)
                if not one or not two:
                    continue
                add("| %s %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    emoji, eid, label, fmt(_mean_delta(one, "dur")), fmt(_mean_delta(two, "dur")),
                    fmt(_mean_delta(one, "f0"), "%+.1f"), fmt(_mean_delta(two, "f0"), "%+.1f"),
                    _blocks_note(one), _blocks_note(two), fmt(_mean_field(two, "tail_block_s"))))
        add("")

    # --- 串句 ---
    pairs = [r for r in rows if r.get("group") in ("pair", "pair-base")]
    if pairs:
        add("## 两句连写、只在第一句放 emoji（%s + %s）" % PAIR)
        add("")
        add("第二句 = 最后两个发声块那一段（跨度 = 末块结束 − 倒数第二块开始）；另比最后 2 s 的 F0 均值 / 动态。"
            "判定同样要求超过噪声、且每个 seed 同号；只有一项超噪声时只是「边界」，得去听。")
        add("")
        add("| 条件 | Δ时长 s | 第二句跨度 Δ s | 末2s F0 Δ | 末2s 动态 Δ | 判定 |")
        add("|---|---|---|---|---|---|")
        bases = {r["seed"]: r for r in pairs if r.get("group") == "pair-base"}
        pos_label = dict(POSITIONS)
        pos_label["end1"] = "第一句句末"
        seen = []
        for r in pairs:
            if r.get("group") != "pair":
                continue
            k = (r.get("emoji_id"), r.get("pos"))
            if k in seen:
                continue
            seen.append(k)
            sel = [x for x in pairs if x.get("group") == "pair"
                   and (x.get("emoji_id"), x.get("pos")) == k]
            dur, span, f0, dyn = [], [], [], []
            for x in sel:
                b = bases.get(x["seed"])
                if not b:
                    continue
                dur.append(round(x["dur"] - b["dur"], 3))
                span.append(round(span2(x) - span2(b), 3)
                            if span2(x) is not None and span2(b) is not None else None)
                f0.append(_win_delta(x, b, "tail2", "f0_mean"))
                dyn.append(_win_delta(x, b, "tail2", "dyn_db"))
            judge(dur, {}, "dur")
            sm, sok = judge(span, {}, "dur")
            _, fok = judge(f0, floor, "f0")
            _, yok = judge(dyn, floor, "dyn")
            if fok and yok:
                verdict = "**可能串了**（末 2 s 音高和能量都超噪声）"
            elif fok or yok:
                verdict = ("边界：末 2 s 只有%s超噪声，第二句跨度 %s s —— 得听"
                           % ("音高" if fok else "能量", fmt(sm, "%+.2f")))
            elif sok:
                verdict = "音高/能量没动，但第二句跨度 %+.2f s" % sm
            else:
                verdict = "没动"
            add("| %s %s · %s | %s | %s | %s | %s | %s |" % (
                EMOJI_BY_ID[k[0]], k[0], pos_label.get(k[1], k[1]),
                fmt(statistics.fmean(dur) if dur else None),
                fmt(statistics.fmean(span) if span and None not in span else None),
                fmt(statistics.fmean([v for v in f0 if v is not None]) if any(f0) else None, "%+.1f"),
                fmt(statistics.fmean([v for v in dyn if v is not None]) if any(dyn) else None, "%+.1f"),
                verdict))
        add("")

    add("## 说话人相似度")
    add("")
    if has_speaker:
        add("（本机没有可用编码器，见 README）")
    else:
        add("跳过：本机没有能从任意 wav 提说话人向量的编码器（`tts/irodori` 的 speaker_inversion 是"
            "**训练出来的嵌入目标**，不是编码器；resemblyzer/speechbrain/pyannote 都没装，不为此装新依赖）。")
    add("")

    add("## 坏音")
    add("")
    if bad_rows:
        for r in bad_rows:
            add("- `%s.wav`：%s" % (r["id"], "、".join(r["bad"])))
    else:
        add("全部 %d 条都能听：无空文件、无连续削波、时长 %.2f–%.2f s、静音占比 %.0f%%–%.0f%%。"
            % (len(rows), min(r["dur"] for r in rows), max(r["dur"] for r in rows),
               100 * min(r["silent_ratio"] for r in rows), 100 * max(r["silent_ratio"] for r in rows)))
    add("")

    add("## 先听这几条")
    add("")
    picks, taken = [], set()

    def pick(row, why):
        key = (row["sentence"], row.get("emoji_id"), row.get("pos"))
        if key in taken or row.get("bad"):
            return
        taken.add(key)
        picks.append((row, why))

    for r in sorted([x for x in rows if (x.get("delta") or {}).get("dur") is not None],
                    key=lambda x: -abs(x["delta"]["dur"]))[:2]:
        pick(r, "时长变化最大 %+.2f s" % r["delta"]["dur"])
    for r in sorted([x for x in rows if (x.get("delta") or {}).get("f0") is not None],
                    key=lambda x: -abs(x["delta"]["f0"]))[:2]:
        pick(r, "F0 偏得最多 %+.0f Hz" % r["delta"]["f0"])
    for r in [x for x in verb if x.get("tail_block_s") and not x.get("bad")][:1]:
        pick(r, "多出一段 %.2f s 的非语言声" % r["tail_block_s"])
    for r in [x for x in pairs if x.get("group") == "pair" and x["pos"] == "end1"][:1]:
        pick(r, "两句连写、emoji 在第一句句末：听第二句有没有被带跑")
    if not picks:
        add("（没有可推荐的条目）")
    for i, (r, why) in enumerate(picks[:6], 1):
        add("%d. `wav/%s.wav` —— %s；%s，%d 个发声块。" % (
            i, r["id"], why, (sentences.get(r["sentence"], {}).get("text") or "")[:24] + "…",
            r["n_blocks"]))
    if MAIN_CARRIER + "-base-%d" % seeds[0] in {r["id"] for r in rows}:
        add("")
        add("基线对照：`wav/%s-base-%d.wav`。" % (MAIN_CARRIER, seeds[0]))
    add("")
    add("## 复现")
    add("")
    add("```")
    add("python -B tests/tts/probes/emotion_bench.py --out <同一目录> --name %s" % run_name)
    add("```")
    add("")
    return "\n".join(L)


# ---- 主流程 ------------------------------------------------------------------

def attach_baselines(rows):
    """给每条条件挂基线指标与差值，并算坏音 / 多出的尾块。

    基线 = 同句、同 seed、**同锚**的那条 base 行；条件可用 `baseline_anchor` 指向别的锚
    （7 锚基线就是「各自相对 neutral/quiet」）。自由描述条件默认跟自己用的锚比。
    """
    bases = {(r["sentence"], r["seed"], r["anchor"]): r for r in rows
             if r.get("group") in ("base", "pair-base") and not r.get("emoji")}
    for r in rows:
        want = r.get("baseline_anchor") or r["anchor"]
        base = bases.get((r["sentence"], r["seed"], want))
        r["base_id"] = base["id"] if base else None
        r["base_blocks"] = base["n_blocks"] if base else None
        r["delta"] = deltas(r, base) if (base and base is not r) else None
        r["tail_block_s"] = None
        if base and base is not r and r["n_blocks"] > base["n_blocks"]:
            last_end = base["blocks"][-1][1] if base["blocks"] else 0.0
            new = [b for b in r["blocks"] if b[0] >= last_end - 0.15]
            if new:
                r["tail_block_s"] = round(sum(b[1] - b[0] for b in new), 3)
        r["bad"] = bad_audio(r)
    return rows


async def run(matrix, texts, out, backend, only=None, reuse=False, log=print):
    import httpx
    out.mkdir(parents=True, exist_ok=True)
    (out / "wav").mkdir(exist_ok=True)
    conds = list(matrix["conditions"])
    if only:
        conds = [c for c in conds if any(k in c["id"] for k in only)]
    if not conds:
        raise BenchError("没有匹配的条件")
    rows, failures = [], []
    t0 = time.time()
    async with httpx.AsyncClient() as client:
        for i, cond in enumerate(conds, 1):
            text = condition_text(cond, texts)
            wav_path = out / "wav" / (cond["id"] + ".wav")
            blob, err = (wav_path.read_bytes(), None) if (reuse and wav_path.exists()) else (None, None)
            if blob is None:
                for _attempt in (0, 1):
                    blob, err = await render_unit(client, backend, text,
                                                  unit_spec(cond, matrix["anchors"]),
                                                  cond.get("seed"),
                                                  voice=matrix.get("voice", VOICE))
                    if blob or "HTTP 4" in (err or ""):
                        break
                    await asyncio.sleep(2.0)
                if blob:
                    wav_path.write_bytes(blob)
            if not blob:
                failures.append(dict(id=cond["id"], text=text, seed=cond.get("seed"), error=err))
                log("FAIL %-34s %s" % (cond["id"], (err or "")[:110]), flush=True)
                continue
            sr, a = decode_wav(blob)
            m = analyze(a, sr, text)
            spec = unit_spec(cond, matrix["anchors"])
            m.update(id=cond["id"], sentence=cond["sentence"], seed=cond.get("seed"),
                     emoji=cond.get("emoji", ""), emoji_id=cond.get("emoji_id", ""),
                     pos=cond.get("pos", "none"), repeat=cond.get("repeat", 1),
                     group=cond.get("group", "base"), anchor=cond["anchor"], text=text,
                     family=spec.get("family"), caption=spec.get("caption"),
                     cfg=spec.get("cfg"), tier=spec.get("tier"),
                     caption_mode=spec.get("caption_mode"),
                     baseline_anchor=cond.get("baseline_anchor"), bytes=len(blob))
            rows.append(m)
            log("[%3d/%3d] %-34s dur=%5.2fs f0=%6s dyn=%5.1f rate=%5s blocks=%d %s"
                % (i, len(conds), cond["id"], m["dur"], fmt(m["f0_mean"], "%.0f"), m["dyn_db"],
                   fmt(m["rate_cps"], "%.2f"), m["n_blocks"],
                   " ".join("%.2f-%.2f" % tuple(b) for b in m["blocks"][:5])), flush=True)
    log("渲染 %d 条，失败 %d，用时 %.1fs" % (len(rows), len(failures), time.time() - t0), flush=True)
    return rows, failures


def write_outputs(out, run_name, matrix, rows, failures):
    # 「相对哪个锚」是条件上的信息：老 metrics.json 没存过它，这里按 id 从矩阵补回来，
    # 否则 --report-only 会把多锚矩阵的锚基线差值全算成空（2026-10-26 实测踩过）。
    by_id = {c.get("id"): c for c in matrix.get("conditions") or []}
    for r in rows:
        if r.get("baseline_anchor") is None:
            c = by_id.get(r.get("id"))
            if c:
                r["baseline_anchor"] = c.get("baseline_anchor")
    rows = attach_baselines(rows)
    floor, floor_detail = noise_floor(rows, matrix.get("noise_reference") or "quiet")
    payload = dict(run=run_name, created=time.strftime("%Y-%m-%dT%H:%M:%S"), matrix=matrix,
                   noise=dict(floor=floor, detail=floor_detail), rows=rows, failures=failures)
    (out / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf8")
    (out / "README.md").write_text(
        render_readme(run_name, matrix, rows, failures, floor, floor_detail), encoding="utf8")
    return floor, floor_detail


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", help="输出目录（必须在 tests/output/ 下；默认 tests/output/emotion-bench-<时间>）")
    p.add_argument("--name", help="运行名（默认取 --out 的目录名）")
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    p.add_argument("--matrix", help="条件矩阵 JSON（默认内置 emoji 位置实验；"
                                    "可只给 tests/tts/data 下的名字，如 emotion_free_20261026）")
    p.add_argument("--seeds", help="逗号分隔，覆盖矩阵里的 seed")
    p.add_argument("--only", action="append", default=[], help="只跑 id 含该子串的条件（可多次）")
    p.add_argument("--reuse", action="store_true", help="已有 wav 就复用，只重算指标")
    p.add_argument("--report-only", action="store_true", help="只读已有 metrics.json 重出 README")
    p.add_argument("--dump-matrix", help="把矩阵写到该路径后退出（不发请求）")
    p.add_argument("--skip-health", action="store_true", help="不先探 /health")
    a = p.parse_args(argv)

    raw = json.loads(resolve_matrix_path(a.matrix).read_text(encoding="utf8")) if a.matrix \
        else build_matrix()
    if a.seeds:
        raw["seeds"] = [int(s) for s in a.seeds.split(",") if s.strip()]
    matrix, texts = normalize_matrix(raw)

    if a.dump_matrix:
        dst = pathlib.Path(a.dump_matrix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(matrix, ensure_ascii=False, indent=1), encoding="utf8")
        print("条件 %d 条 → %s" % (len(matrix["conditions"]), dst))
        return 0

    out = pathlib.Path(a.out).resolve() if a.out else (
        ROOT / "tests" / "output" / ("emotion-bench-" + time.strftime("%Y%m%d-%H%M")))
    if "tests" not in out.parts or "output" not in out.parts:
        raise SystemExit("输出目录必须在 tests/output/ 下：%s" % out)
    run_name = a.name or out.name
    matrix["backend"] = a.backend

    if a.report_only:
        payload = json.loads((out / "metrics.json").read_text(encoding="utf8"))
        write_outputs(out, run_name, payload["matrix"], payload["rows"], payload.get("failures") or [])
        print("README.md 已重出：%s" % out)
        return 0

    if not a.skip_health and not backend_health(a.backend):
        raise SystemExit("后端 %s 不可用（/health 不通）——先确认 :9889 在跑" % a.backend)
    print("矩阵：%d 条条件，%d 句，seed=%s" % (len(matrix["conditions"]), len(matrix["sentences"]),
                                            matrix["seeds"]))
    rows, failures = asyncio.run(run(matrix, texts, out, a.backend, only=a.only, reuse=a.reuse))
    floor, _ = write_outputs(out, run_name, matrix, rows, failures)
    print("噪声上限：%s" % json.dumps(floor, ensure_ascii=False))
    print("结果：%s（%d 条 wav）" % (out, len(rows)))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
