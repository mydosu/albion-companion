# -*- coding: utf-8 -*-
"""场景规划（P3，规格见方案 §3.2）。**纯 CPU、不加载模型、不碰 GPU。**

来源：2026-09-27 从 `irodori_serve.py` 抽取。当前本模块是规划的唯一实现；
后端重复副本已清理，旧端点由 `serve_v2.legacy_plan` 复用这里的判据。
v4/v5 冻结行为由 `tests/tts/` 的 golden JSON 与情绪规划回归测试守住。

为什么要抽出来：场景判定（"这一句该用什么语气"）与"把多少个字合成进一个 WAV"原本绑在同一个
HTTP input 上，导致流式的每个碎片都要重新判定一次整篇语境。抽成纯函数后，规划可以对着
**已准备、已提交**的单元集独立运行，还能在 strict 模式下与旧整篇路径逐段比对。
"""
import os
import re

# ---- 自动风格表（逐字取自 irodori_serve.py）--------------------------------
EMOJI_MAP = [
    (("😳", "😖", "🥺", "///"), "shy"),
    (("😊", "😄", "😆", "✨", "🎉", "♪"), "happy"),
    (("😏", "💕", "♥", "❤", "💗", "💦"), "flirty"),
    (("😭", "😢", "🥲"), "sad"),
    (("😱", "😨", "❗", "⁉"), "surprised"),
]
ONO = ("んっ", "あっ", "はぁ", "ふぅ", "ひゃ", "んぅ", "はあ", "ぁ…", "あ…", "ん…", "ふ…",
       "ちゅ", "ちゅぱ", "ぺろ", "んぐ",
       "うぅ", "うっ", "くぅ", "くっ", "ふっ", "すぅ", "ひぃ", "あぁ", "んぁ", "はふ", "ふぁ")
_ONO_BOUNDARY = set("…‥、。！？!?・」「』〜～ \t\n-—")
MOAN_WORD = ("だめ", "ダメ", "敏感", "そこ", "弱い", "もっと", "奥", "ビク", "とろけ",
             "感じ", "気持ちい", "濡れ", "熱い", "震え", "締ま", "激し", "欲し", "当た",
             "やめ", "いや", "痛い")
SHY = re.compile(r"(恥ずかし|恥ず|照れ|てれ|はにか|うつむ|赤面|もじもじ|顔が熱|何でもない|//)")
HAPPY = re.compile(r"(嬉し|うれし|よかった|良かった|楽しみ|たのし|えへへ|ふふ|やった|すごい|幸せ|にこにこ|わーい)")
FLIRTY = re.compile(r"(わがまま|嫌いじゃない|近い|近くに|色っぽ|誘う|甘え|んふ|あのね"
                    r"|他の子|だけを見て|だけを|離さな|渡さな|そばにいて|乱れ|独り占め|ものに"
                    r"|好き|大好き|会いたい|独占|焦らし|意地悪|あなたのもの|私のもの)")
SAD = re.compile(r"(悲し|かなし|つらい|辛い|寂し|さみし|不安|心配|泣|悔し|情けな|申し訳|落ち込|しんど|すみません|ごめんなさい|不甲斐)")
SURPRISED = re.compile(r"(ええっ|えっ[！?]|まさか|本当に[？?]|！？|⁉|はっ[？?]|びっくり|驚い|うそでしょ|いきなり)")
SOFT = re.compile(r"(ゆっくり|休みましょ|紅茶|おやすみ|ありがとう|お茶|どうぞ|お疲れ|大丈夫|無理しな|そっと|休んで)")
STYLES = {
    "moan": ("息が荒く、甘く喘ぐように、声を漏らして、とろけた声で", 4.5),
    "shy": ("恥ずかしそうに、声を震わせて、小さく照れながら", 3.5),
    "happy": ("嬉しそうに、明るく弾んだ声で、楽しげに", 3.2),
    "flirty": ("甘く誘うように、近い距離感で、色っぽく囁いて", 4.0),
    "sad": ("悲しそうに、声を落として、切なげに", 3.5),
    "surprised": ("驚いたように、息を呑んで、慌てた声で", 3.5),
    "soft": ("柔らかく、優しく、落ち着いた声で", 2.5),
    "tremble": ("息混じりで、声を震わせて、途切れ途切れに、甘く", 4.0),
    "calm": ("落ち着いた自然な声で、丁寧に", 3.0),
}
INTIMATE = {"moan", "flirty", "shy", "tremble"}

FAMILIES = {
    "intimate": ("息を荒くして、甘く喘ぐように、とろけた声で、近い距離で", 4.5),
    "flirty":   ("甘く誘うように、色っぽく囁いて、近い距離感で",            4.0),
    "bright":   ("明るく、弾んだ声で、楽しげに",                            3.2),
    "sad":      ("悲しそうに、声を落として、切なげに",                      3.5),
    "neutral":  ("落ち着いた自然な声で、丁寧に",                            3.0),
}
STYLE_FAMILY = {"moan": "intimate", "tremble": "intimate", "shy": "intimate",
                "flirty": "flirty", "happy": "bright", "surprised": "bright",
                "sad": "sad", "soft": "neutral", "calm": "neutral"}

GAP_SEC = 0.10                      # 行间静音（渲染侧用；与旧实现同值）
PLAN_VERSION = 2


# ---- 判定（逐字取自 irodori_serve.py）--------------------------------------
def _ono_hits(line: str) -> list:
    """命中的拟声词——要求落在词/句边界上（「あったかい」里的「あっ」不算命中）"""
    hits = []
    for k in ONO:
        p = line.find(k)
        while p >= 0:
            nxt = line[p + len(k): p + len(k) + 1]
            if k.endswith("…") or nxt == "" or nxt in _ONO_BOUNDARY or nxt == "っ":
                hits.append(k)
                break
            p = line.find(k, p + 1)
    return hits


def _moanish(line: str) -> bool:
    n_ono = len(_ono_hits(line))
    if n_ono >= 2:
        return True
    return n_ono >= 1 and any(w in line for w in MOAN_WORD)


def _match(text: str):
    """规则瀑布：返回命中的风格名，没命中返回 None（顺序即优先级）"""
    for keys, name in EMOJI_MAP:
        if any(k in text for k in keys):
            return name
    if _moanish(text):
        return "moan"
    if SHY.search(text):
        return "shy"
    if FLIRTY.search(text):
        return "flirty"
    if HAPPY.search(text):
        return "happy"
    if SAD.search(text):
        return "sad"
    if SURPRISED.search(text):
        return "surprised"
    if SOFT.search(text):
        return "soft"
    return None


_NONONO = re.compile(r"[、。！？…‥っッ\s!?─—\-〜～♪♡♥「」『』【】]")


def _is_interjection(line: str) -> bool:
    """纯拟声/气声短句（去掉拟声词与标点后几乎没剩字）——只有这种直接上 moan 档。"""
    t = line
    for k in ONO:
        t = t.replace(k, "")
    return len(_NONONO.sub("", t)) <= 2


def _ctx_style(line: str, ctx: str):
    """本行完全没信号 → 用相邻行窗口兜底，且只认亲密信号（moan/flirty/shy）。"""
    if _match(ctx) not in ("moan", "flirty", "shy"):
        return None
    return "moan" if _is_interjection(line) else "tremble"


def auto_style(line: str, ctx: str = ""):
    """返回 (style_name, caption, cfg_scale_caption)——纯后端判断。"""
    name = _match(line) or (_ctx_style(line, ctx) if ctx else None)
    name = name or "calm"
    return (name,) + STYLES[name]


def _raw_styles(segs: list, extra_ctx: str = "") -> list:
    """逐行原始判定（带相邻行窗口兜底，不含场景粘滞）。"""
    out = []
    for i, s in enumerate(segs):
        ctx = extra_ctx + (segs[i - 1] if i else "") + s + (segs[i + 1] if i + 1 < len(segs) else "")
        out.append(auto_style(s, ctx)[0])
    return out


def _sticky(raw: list) -> list:
    """场景粘滞：只救 calm（①紧邻救援 ②段内被两头夹住的整段救援），显式判出的档一律不动。"""
    out = list(raw)
    n = len(raw)
    for i, name in enumerate(raw):
        if name != "calm":
            continue
        prev = raw[i - 1] if i > 0 else None
        nxt = raw[i + 1] if i + 1 < n else None
        if prev in INTIMATE or nxt in INTIMATE:
            out[i] = "tremble"
    i = 0
    while i < n:
        if raw[i] != "calm":
            i += 1
            continue
        j = i
        while j < n and raw[j] == "calm":
            j += 1
        left = raw[i - 1] if i > 0 else None
        right = raw[j] if j < n else None
        if left in INTIMATE and right in INTIMATE:
            for k in range(i, j):
                out[k] = "tremble"
        i = j
    return out


_SCENE_MARK = re.compile(r"^\s*\[\[.*?\]\]\s*$")


def _segments(text: str):
    """切行 + 识别台本标记。返回 (可念的行列表, 场景边界行号集合)。"""
    segs, breaks = [], set()
    for s in re.split(r"[\n]+", text):
        s = s.strip()
        if not s:
            continue
        if _SCENE_MARK.match(s):
            if segs:
                breaks.add(len(segs))
            continue
        segs.append(s)
    if len(segs) == 1:
        segs = [s.strip() for s in re.split(r"(?<=[。！？!?])", segs[0]) if s.strip()]
        breaks = set()
    return segs, breaks


def _plan_styles(segs: list, styles: list, breaks: set, merge=None):
    """场景识别 → 定基调族 → 段内统一基调。返回 (plan, scene_ids)。

    `merge=None`（默认）＝按**段**投票决定是否做基调合并，与生产整篇路径逐字一致。
    v2 的 online 路径改按**自然句**投票并自行合并，再用 `merge=False` 关掉这里的第二次判断，
    这样同一句软切成几片都不改变语气。等价性测试仍按三参数调用，比的是默认路径。
    """
    plan = [None] * len(segs)
    scene_ids = [0] * len(segs)
    if merge is None and styles and sum(1 for x in styles if STYLE_FAMILY[x] == "intimate") / len(styles) >= 0.40:
        styles = ["tremble" if STYLE_FAMILY[x] == "neutral" else x for x in styles]
        print(f"[irodori/scene] 整篇 intimate 占比 ≥40% → neutral 并入 intimate"
              f"（{sum(1 for x in styles if STYLE_FAMILY[x] == 'neutral')} 行仍是 neutral）", flush=True)
    i = sid = 0
    while i < len(segs):
        fam = STYLE_FAMILY[styles[i]]
        j = i + 1
        while j < len(segs) and STYLE_FAMILY[styles[j]] == fam and j not in breaks:
            j += 1
        cap, cfg = FAMILIES[fam]
        for k in range(i, j):
            plan[k] = (fam, cap, cfg)
            scene_ids[k] = sid
        i, sid = j, sid + 1
    return plan, scene_ids


# ---- v2 入口（纯函数）------------------------------------------------------
def styles_for(segs, extra_ctx: str = "", style_override=None):
    if style_override:
        return [style_override] * len(segs)
    return _sticky(_raw_styles(list(segs), extra_ctx))


def plan_document(text: str, extra_ctx: str = "", style_override=None):
    """strict 模式：对**整篇**文本规划（与旧整篇路径同口径，用于等价性回归）。"""
    segs, breaks = _segments(text)
    styles = styles_for(segs, extra_ctx, style_override)
    plan, scene_ids = _plan_styles(segs, styles, breaks)
    return {"segs": segs, "styles": styles, "plan": plan,
            "scene_ids": scene_ids, "breaks": sorted(breaks)}


def _src_cues_of(u):
    """中文源线索：可以直接给 `src_cues`，也可以随情绪提示一起带在 `emotion["src"]` 里
    （桥就是这么传的 —— 线索和情绪同一次调用回来，不另开通道）。"""
    cues = u.get("src_cues") if isinstance(u, dict) else None
    if not isinstance(cues, list):
        emo = u.get("emotion") if isinstance(u, dict) else None
        cues = (emo or {}).get("src") if isinstance(emo, dict) else None
    if not isinstance(cues, list):
        return None
    return [c for c in cues if isinstance(c, dict) and c.get("kind")][:8] or None


def _voice_caption_in(u):
    """桥写的受控声音描述（阶段 3d）：开关关着时**一律当成没有**。

    两条闸都在这里：① `ALBION_VOICE_CAPTION` 没开 ⇒ 直接 None（生产逐字段不变）；
    ② 开了也要过 `voice_caption.validate_caption` —— 超长/空/词表外实词的描述一律不用，
    这句回原来的码本锚路径，绝不把半截描述送进模型。
    """
    if not isinstance(u, dict) or not VC.voice_caption_on():
        return None
    v = u.get("voice_caption")
    if not isinstance(v, str) or VC.validate_caption(v):
        return None
    return v


def _apply_voice_caption(rec, caption):
    """把受控描述当 caption 发出去（`caption_mode="free"`），其余诊断字段原样保留。

    * `family` 保持规划器定下的族：render_queue 仍要按它校验；描述只换「怎么说」，不换语义族；
    * `cfg` 固定 `voice_caption.FREE_CFG`（3.0）：评测台结论是 cfg 不是强度旋钮，强度写在词里；
    * `caption_mode="free"` 是后端放过码本白名单的唯一钥匙（不带它，自由 caption 会被 400）；
    * `tier/level` 不动（诊断口径一致），`reason` 加一段可归因的尾巴。
    """
    rec["caption"] = caption
    rec["cfg"] = VC.FREE_CFG
    rec["caption_mode"] = "free"
    rec["reason"] = (rec.get("reason") or "") + " caption=voice"


def _norm_units(units):
    """把单元规整成 dict（也接受纯字符串）。

    `advance` = 这是不是**新自然句的第一片**（软切/强切切出的续片为 False）：
    滞回的"无证据计数"只在整句上推进一次，续片不重复衰减（方案 1.4）。
    `src_cues` = 桥从**中文原文**抽出的结构化线索（emoji/括号指令），译文里看不到。
    """
    out = []
    for i, u in enumerate(units):
        if isinstance(u, dict):
            out.append({"text": str(u.get("text") or ""), "unit_id": u.get("unit_id"),
                        "parent_id": u.get("parent_id", i), "complete": bool(u.get("complete", True)),
                        "emotion": u.get("emotion") if isinstance(u.get("emotion"), dict) else None,
                        "advance": (u.get("advance", True) is not False) and bool(u.get("complete", True)),
                        "src_cues": _src_cues_of(u), "voice_caption": _voice_caption_in(u)})
        else:
            out.append({"text": str(u), "unit_id": None, "parent_id": i, "complete": True,
                        "emotion": None, "advance": True, "src_cues": None,
                        "voice_caption": None})
    return out


def tone_vote(us, styles):
    """基调投票的**可观测结果**（按自然句）。

    同句碎片只算一票；闭合以该句末片的 `complete` 为准；未闭合的句子进 `skipped` 不进分母。
    """
    order, seen = [], set()
    for u in us:
        if u["parent_id"] not in seen:
            seen.add(u["parent_id"])
            order.append(u["parent_id"])
    counted, skipped = [], 0
    for pid in order:
        ids = [i for i, u in enumerate(us) if u["parent_id"] == pid]
        if not us[ids[-1]]["complete"]:
            skipped += 1
            continue
        counted.append(any(STYLE_FAMILY[styles[i]] == "intimate" for i in ids))
    return {"counted": len(counted), "intimate": sum(counted), "skipped": skipped,
            "merged": bool(counted) and sum(counted) / len(counted) >= 0.40}


def _merge_by_sentence(us, styles):
    """按自然句投票决定基调合并（v2 相对旧整篇路径的关键不变性：切几片不改变语气）。"""
    vote = tone_vote(us, styles)
    if not vote["merged"]:
        return list(styles)
    print(f"[irodori/scene] 自然句 intimate 占比 ≥40%（{vote['intimate']}/{vote['counted']} 句，"
          f"未闭合 {vote['skipped']} 句不计）→ neutral 并入 intimate", flush=True)
    return ["tremble" if STYLE_FAMILY[s] == "neutral" else s for s in styles]


def plan_units(units, extra_ctx: str = "", style_override=None):
    """online 模式：对**已提交的单元**规划（不再自己切句）。

    `units` 可以是字符串，也可以是 `{"text","unit_id","parent_id","complete"}`：
    `parent_id` = 该单元所属的自然句（同一句被软切成的碎片共用），`complete` = 这句是否已吐完。
    """
    us = _norm_units(units)
    texts = [u["text"] for u in us]
    if not texts:
        return []
    raw = [style_override] * len(texts) if style_override else _raw_styles(texts, extra_ctx)
    styles = list(raw) if style_override else _sticky(raw)
    if not style_override:
        styles = _merge_by_sentence(us, styles)
    plan, scene_ids = _plan_styles(texts, styles, set(), merge=False)
    out = []
    for i in range(len(texts)):
        why = "style=%s" % styles[i]
        if raw[i] != styles[i]:
            why += "（粘滞救援 %s→%s）" % (raw[i], styles[i])
        out.append({"plan_version": PLAN_VERSION, "unit_id": us[i]["unit_id"],
                    "parent_id": us[i]["parent_id"], "sub_index": i, "role": "speech",
                    "family": plan[i][0], "caption": plan[i][1], "cfg": plan[i][2],
                    "scene_id": scene_ids[i], "reason": why})
    return out


class PlanMapError(Exception):
    """strict 模式下单元与整篇切分对不上（契约错误，必须显式报出来，不能悄悄错位）。"""


def _doc_segments(us):
    """按**整篇实际采用的粒度**切分，并记下每段来自哪个单元。

    不能拿每个单元各自切一次的结果去对（复核发现的问题 3）：单元 `"第一句。第二句！"` 自己会切成
    两段，但整篇是多行、按行保留，于是粒度对不上。这里复刻 `_segments` 的整篇规则——含"整篇恰好
    只剩一行时才退化成按句切"这条**整篇一起决定**的退化——并保留来源单元号。
    """
    segs, breaks, prov = [], set(), []
    for i, u in enumerate(us):
        for ln in u["text"].split("\n"):
            s = ln.strip()
            if not s:
                continue
            if _SCENE_MARK.match(s):
                if segs:
                    breaks.add(len(segs))
                continue
            segs.append(s)
            prov.append(i)
    if len(segs) == 1:
        segs = [s.strip() for s in re.split(r"(?<=[。！？!?])", segs[0]) if s.strip()]
        prov = [prov[0]] * len(segs)
        breaks = set()
    return segs, breaks, prov


def strict_records(units, extra_ctx: str = ""):
    """strict 模式：按**整篇**（保留行边界）规划，再把结果映射回单元。

    纯标记单元是场景边界输入、不是发音单元 → 各自返回一条 `role=scene_break`（不产生朗读记录）；
    空单元返回 `role=skipped`。都不静默丢弃。
    """
    us = _norm_units(units)
    doc = plan_document("\n".join(u["text"] for u in us), extra_ctx)
    segs, breaks, prov = _doc_segments(us)
    if segs != doc["segs"] or sorted(breaks) != doc["breaks"]:
        # 两条切分必须一致，否则映射会静默错位；对不上就是契约错误
        raise PlanMapError("整篇切分与来源映射不一致：segs %r vs %r，breaks %r vs %r"
                           % (doc["segs"], segs, doc["breaks"], sorted(breaks)))
    recs = []
    for i, u in enumerate(us):
        base = {"plan_version": PLAN_VERSION, "unit_id": u["unit_id"], "parent_id": u["parent_id"]}
        idxs = [k for k, p in enumerate(prov) if p == i]
        if not idxs:
            role = "scene_break" if _SCENE_MARK.match(u["text"]) else "skipped"
            recs.append(dict(base, role=role))
            continue
        for sub, k in enumerate(idxs):
            recs.append(dict(base, sub_index=sub, role="speech", family=doc["plan"][k][0],
                             caption=doc["plan"][k][1], cfg=doc["plan"][k][2],
                             scene_id=doc["scene_ids"][k], reason="style=%s" % doc["styles"][k]))
    return recs


# ---- v3：训练锚码本 + 强度档 + 滞回（2026-09-28）-----------------------------
# 与 v2 的关系：**规则判定原样保留**（`_raw_styles` 那一套仍是先验），v3 只改"条件怎么编译"，
# 并**去掉**两处经验补丁（场景粘滞 `_sticky`、整篇 40% 投票合并）—— 那两处是"单句判不准"的
# 代偿：前者把没信号的句子硬塞进亲密档，后者更进一步把整篇 neutral 并进 intimate（实测四份
# persona 全部触发），恰恰是"整段一个味"的来源。v3 用**逐句滞回**承担同一职责：
# 有明确信号 → 立即切；没信号 → 继承上一档并逐句衰减（约 3 句后自然落到弱档＝语气回落）。
import delivery_stage as DS
import emotion_codebook as CB
import emotion_fusion as F
import cue_scope as CS
import refusal_guard as RG
import voice_caption as VC

PLAN_VERSION_V3 = 3
PLAN_VERSION_V4 = 4          # astra-3 §5.1：v4 = v3 + codebook v2（neutral 说耳语时借既有耳语锚）
PLAN_VERSION_V5 = 5          # astra 第 11 轮 ②：v5 = v4 + 拒绝/制止/安抚类语义闸门（refusal_guard）
PLAN_VERSION_V6 = 6          # astra 第 12 轮：v6 = v5 + 四缺口（① hint 自述发声等级 ② 跨句指令
                            #   ③ 日语表层舞台提示 ④ 换族后按文本先验重置强度），每项一个独立
                            #   开关、v6 下默认开
POLICY_V4 = "cascade_v2"
POLICY_V5 = "cascade_v3"
POLICY_V6 = "cascade_v4"
_HOLD_DECAY = 0.17          # 一句没信号，强度往下走这么多（≈3 句从强档落回弱档）
_HOLD_MAX = 8               # 连续这么多句都没信号就**释放**继承的族（别让一句亲密粘住整篇）
                            # ponytail: 8 是听感旋钮，不是定律；听测觉得"气氛掉得太快/太慢"就调这个数


def _finite_num(v, lo, hi):
    """挑出"有限数值且在范围内"，其余（布尔/NaN/±Inf/巨大整数/字符串/list）一律 None。

    这是信任边界（`prev` 来自请求体）：坏类型**不能**一路抛到 `/v2/plan` 的宽泛异常 ——
    那会被当成"规划失败"，把整个会话静默降级回 v2（把真问题掩盖成一台坏输入）。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)                      # 巨大整数在这里 OverflowError，属于坏输入而非崩溃点
    except (OverflowError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f if lo <= f <= hi else None


def _clean_prev(prev):
    """只接受"服务端自己发出去过"的状态形状（`/v2/plan` 的 `prev` 来自请求体，是信任边界）。

    不校验的后果：坏类型会一路抛到宽泛异常、静默降到 v2（掩盖真问题），
    或者被人为塞进一个跨会话的族。这里只挑认识的键和范围，其余全丢。
    """
    if not isinstance(prev, dict):
        return {}
    out = {}
    fam = prev.get("family")
    if isinstance(fam, str) and fam in CB.LADDER:          # list/dict 会让 `in` 抛 TypeError
        out["family"] = fam
    lv = _finite_num(prev.get("level"), 0.0, 1.0)
    if lv is not None:
        out["level"] = lv
    sc = _finite_num(prev.get("scene_id"), 0, 10 ** 6)
    if sc is not None:
        out["scene_id"] = int(sc)
    held = _finite_num(prev.get("held"), 0, 10 ** 4)
    if held is not None:
        out["held"] = int(held)
    tier = prev.get("tier")
    if isinstance(tier, str) and tier in ("weak", "strong"):
        out["tier"] = tier
    d = prev.get("delivery")                # §5.2 的 H 组：只认标记为**可信**的前句发声
    if isinstance(d, str) and d in ("soft", "breathy", "whispered"):
        out["delivery"] = d
        if prev.get("delivery_trusted") is True:
            out["delivery_trusted"] = True
    p = prev.get("p")                       # 上一句的族分布（前文先验用）
    if isinstance(p, dict):
        q = {}
        for f in CB.LADDER:
            v = p.get(f)
            if _finite_num(v, 0.0, 1.0) is not None:
                q[f] = float(v)
        if len(q) == len(CB.LADDER):
            out["p"] = q
    ds = prev.get("delivery_scope")         # 生效中的发声指令属于哪个源句（cue 归属用）
    if isinstance(ds, str) and 0 < len(ds) <= 128:
        out["delivery_scope"] = ds
    return out


def _delivery(text, src_cues=None, hint=None):
    """本句目标发声方式：原文直述指令（可靠度最高）→ LLM 自述。返回 (phonation, 可靠度)。

    v3 走这条（行为冻结）；v4 另有 `delivery_stage.gate()` 一层的分组/去重/abstain 门控。
    """
    ph, rel = F.delivery_of(text, src_cues)
    if not ph and isinstance(hint, dict) and hint.get("phonation") in (
            "voiced", "soft", "breathy", "whispered"):
        return hint["phonation"], 0.5
    return ph, rel


def _weak_delivery_on(body):
    """§5.2 阶段的开关：默认随 v4 开；`ALBION_WEAK_DELIVERY=0`（或请求里给布尔值）可单独关掉，
    好让试听能把 §5.1 与 §5.2 的收益分开归因。"""
    v = body.get("weak_delivery") if isinstance(body, dict) else None
    if isinstance(v, bool):
        return v
    return os.environ.get("ALBION_WEAK_DELIVERY", "1") != "0"


def _cue_scope_on(body, default=False):
    """`ALBION_CUE_SCOPE`（v4 默认关；v6 默认开 —— ② 跨句指令以源句身份为前提）。
    请求里给布尔值可覆盖（与 weak_delivery 同形）。"""
    return CS.enabled(body.get("cue_scope") if isinstance(body, dict) else None, default)


def _flag_on(body, key, env, default=True):
    """v6 的四个独立开关的通用读法：请求体显式布尔 > 进程环境（0/false/no/off 关）> 默认值。"""
    v = body.get(key) if isinstance(body, dict) else None
    if isinstance(v, bool):
        return v
    s = (os.environ.get(env) or "").strip().lower()
    if s in ("0", "false", "no", "off"):
        return False
    if s in ("1", "true", "yes", "on"):
        return True
    return default


def _hint_phonation_on(body):
    """① `ALBION_HINT_PHONATION`（v6 默认开）：hint 自述的 whispered/breathy 单独也算证据
    （档位保守：调用方压到弱档；拒绝/制止/安抚类句子只准用它选锚、不准用它选族）。"""
    return _flag_on(body, "hint_phonation", "ALBION_HINT_PHONATION")


def _voice_caption_on(body):
    """阶段 3d：受控声音描述（`ALBION_VOICE_CAPTION`，默认**关**）。

    关着时桥不产 `voice_caption`、这里也不认它 —— 规划器输出与加这段之前逐字段相同。
    """
    return _flag_on(body, "voice_caption", VC.VOICE_CAPTION_ENV, default=False)


def _cross_sentence_on(body):
    """② `ALBION_CROSS_SENTENCE`（v6 默认开）：跨句指令生效（本 unit 内 + 桥按 `source_prefix`
    重放出的前文延续；跨 unit 的续接由桥做，见 `cue_scope.active_directive`）。"""
    return _flag_on(body, "cross_sentence", "ALBION_CROSS_SENTENCE")


def _ja_stage_cue_on(body):
    """③ `ALBION_JA_STAGE_CUE`（v6 默认开）：日语表层舞台提示（`（小声で）`/`息を荒くして`）算证据。"""
    return _flag_on(body, "ja_stage_cue", "ALBION_JA_STAGE_CUE")


def _level_rebase_on(body):
    """④ `ALBION_LEVEL_REBASE`（v6 默认开）：闸门换族后按新族重算 level（留重算前/后）。"""
    return _flag_on(body, "level_rebase", "ALBION_LEVEL_REBASE")


def _refusal_guard_on(body):
    """`ALBION_REFUSAL_GUARD`（默认开，v5 生效）：拒绝/制止/安抚类的语义闸门。

    `ALBION_REFUSAL_GUARD=0`（或请求里给布尔值）可**单独关掉**这一层而仍请求 v5，用于把
    "加了一层闸门"与"版本号本身"分开归因（与 weak_delivery 同形）。
    """
    v = body.get("refusal_guard") if isinstance(body, dict) else None
    if isinstance(v, bool):
        return v
    return os.environ.get("ALBION_REFUSAL_GUARD", "1") != "0"


def _scope_of(u):
    """本子段所属源句的身份（桥在 `emotion.src_scope` 里盖章）。判不出就 None + scope_unknown。

    身份必须**由桥按原文跨度算出来**：规划器自己看日语正文是判不出"这条中文提示属于哪句"的
    （译文里那些词早就没了）—— 拿不到身份就不注入，而不是默认挂到本段。
    """
    emo = u.get("emotion") if isinstance(u, dict) else None
    sc = (emo or {}).get("src_scope") if isinstance(emo, dict) else None
    if isinstance(sc, dict):
        sid = sc.get("id")
        if isinstance(sid, str) and 0 < len(sid) <= 128:
            return sid, str(sc.get("basis") or "span")
    return None, "scope_unknown"


def _cues_in_scope(cues, sid):
    """只留**属于本子段源句**的 cue；身份缺失/对不上的一律不用。返回 (in_scope|None, dropped)。

    这一条是"本句的提示不外溢"的落点：cue 自带 `sid`，本段身份不同 ⇒ 不注入。
    """
    if not cues:
        return cues, 0
    if not sid:
        return None, len(cues)
    keep = [c for c in cues if c.get("sid") == sid]
    return (keep or None), len(cues) - len(keep)


def _hint_level(u):
    """单元里带的情绪提示（上游 LLM 给的，可缺/可坏）：只认 0..1 的强度类数值，其余一概不信。"""
    h = u.get("emotion") if isinstance(u, dict) else None
    if not isinstance(h, dict):
        return None
    for k in ("intensity", "arousal"):
        v = h.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0:
            return float(v)
    return None


def plan_units_v3(units, extra_ctx="", prev=None, style_override=None,
                  plan_version=PLAN_VERSION_V3, allow_neutral_whisper=False,
                  weak_delivery=False, cue_scope=False, refusal_guard=False,
                  hint_phonation=False, cross_sentence=False, ja_stage_cue=False,
                  level_rebase=False, voice_caption=False):
    """返回 (records, state)；`state` 由协调器原样带回下一次调用当作 `prev`。

    输出记录仍是渲染队列认识的那套字段，另外多带 `tier/level`（诊断用，不参与校验）。

    三个版本参数只影响**发出什么条件**，不影响这条代码路径的其余行为：
    `plan_version` 只写进记录（v4 写 4、v5 写 5、v6 写 6），`allow_neutral_whisper` 只放开 §5.1
    那个新组合，`refusal_guard`（v5）只在"高唤醒自动等于 bright"落到拒绝/制止/安抚类句子时换掉
    那个族。默认值 = 旧 v3 行为，旧响应因此逐字节不变。

    v6（astra 第 12 轮）新增四个**互相独立**的开关，默认全 False（v4/v5 因此逐字段不变），
    由端点按 `plan_version>=6` + 各自的环境/请求开关打开：

    ① `hint_phonation`：`delivery_stage` 的证据等级（句面明确 > hint 自述 > 韵律推断）落地。
       hint 自述的 `whispered/breathy` 单独也算证据（`hint_self_report`，trusted=False），
       但**档位压到弱档**；**优先序**：句面明确（中文 cue / 日语舞台提示 / 词面）> hint 自述 >
       韵律推断，且**任何句子**上 hint 自述**只准选锚、不准选族** —— 低可信的"怎么说"不单独
       改变语义族（`fam_h` 摘掉 phonation）：「はい、わかりました。」+hint whispered 仍是
       neutral 族借既有耳语锚，不会变成 intimate；「拒绝不许 bright」也不受影响。

    ② `cross_sentence`：跨句指令（"接下来都小声说"）已由桥按源句复制给后续源句（每个副本盖
       接收句自己的 `sid`），本函数只把它当**本句的 D** 用，并在诊断里记
       `cue_scope.mode=cross_sentence` 与生效区间 `cross={from,to}`；**跨 unit 的续接在桥侧**
       完成（重放壳发的 `source_prefix`，副本 `cross_from=-1`）—— 本层不外推，
       `cross_unit` 如实记桥的实现方式（`CS.CROSS_UNIT`）。

    ③ `ja_stage_cue`：日语表层舞台提示（`（小声で）`/`囁くように`/`息を荒くして`）进 `S` 组，
       与中文 cue 同级（句面明确），可单独决定 delivery。既有中文通路一条不改。

    ④ `level_rebase`：闸门换族后**移除被否决 hint、以文本先验重置强度**（不是"按新族估的
       level"—— 公式读的是**文本**先验 + 发声微调，不读替换后的族；族相关的强度校准**未验收**）。
       闸门否掉的正是"上游 arousal 就是响声证据"这一条，所以重置丢弃该项（`cue_level(text, None)`
       + 发声方式微调），旧族的 level 不再适用；诊断里保留 `level_rebase={before, after, basis}`。

    `refusal_guard=True`（`ALBION_REFUSAL_GUARD`，v5 默认开）时：`refusal_guard.apply()` 先判
    **语义类别**（拒绝/制止/安抚降温/恐惧制止/低落拒绝）与**明确禁止的发声条件**，只在即将采用的
    族是被禁的那个（bright）时换成同一份证据支持的合法族（负效价 → sad，其余 → neutral），并把
    档位压回弱档。玩笑式拒绝（正效价/玩笑标记）、引述、否定、以及证据本来就选成别的族的句子
    **一律不干预** —— 见 `refusal_guard.py` 的模块说明与 `tests/test_emotion_refusal.py`。

    `cue_scope=True`（`ALBION_CUE_SCOPE`，v4 默认关 / v6 默认开）时，**cue 只作用于它所在的源句**：
    身份由桥按中文原文的 code point 跨度算好（`emotion.src_scope.id`），本段只采用身份相符的
    cue；同一个源句的后续子段共享该身份，但**不重复加档、不刷新可信链**；句末即失效
    （下一句身份不同 ⇒ 上一句的局部 D 不会被当成新 D）。判不出身份 ⇒ 不注入 + `scope_unknown`。
    """
    us = _norm_units(units)
    prev = _clean_prev(prev)
    if not us:
        return [], dict(prev)
    texts = [u["text"] for u in us]
    override = style_override if style_override in STYLES else None
    # 本句信号**只看本句**：v2 的 `_raw_styles` 会把邻近行/前文也算进上下文（`_ctx_style`），
    # 那样"没信号的句子"会被上文顶成 tremble，继承计数每句被重置 ⇒ 释放永远不触发。
    # 上文的名分改成下面的 ctx_seed（继承的**起点**），不再参与信号判定。
    raw = [override] * len(texts) if override else [(_match(t) or "calm") for t in texts]
    ctx_seed = STYLE_FAMILY.get(_match(extra_ctx) or "calm")
    if ctx_seed == "neutral":
        ctx_seed = None                       # 上文也只是平静 ⇒ 不算种子（否则平静也会被继承）

    level = prev.get("level")
    fam_prev = prev.get("family")
    tier_prev = prev.get("tier")
    scene = int(prev.get("scene_id") or 1)
    held = int(prev.get("held") or 0)
    recs = []
    cbv = CB.CODEBOOK_VERSION_V2 if plan_version >= PLAN_VERSION_V4 else CB.CODEBOOK_VERSION
    for i, t in enumerate(texts):
        hint = us[i].get("emotion")
        cues, sid, sbasis, sdrop = us[i].get("src_cues"), None, None, 0
        if cue_scope:                                # ① cue 只作用于它所在的那个源句
            sid, sbasis = _scope_of(us[i])
            cues, sdrop = _cues_in_scope(cues, sid)
        # ② 同一条指令在**同一源句的后续子段**上持续有效：不重复加档、不刷新可信来源
        repeat = bool(sid and sid == prev.get("delivery_scope"))
        advance_now = us[i].get("advance", True) is not False and not repeat
        # ② 跨句指令（v6）：副本已经盖了本句的 sid，这里只把它记进诊断（不额外外推）
        cross = None
        if cross_sentence:
            for c in (cues or []):
                if isinstance(c, dict) and c.get("cross_sentence") and isinstance(c.get("cross_from"), int):
                    cross = {"from": c["cross_from"], "to": c.get("cross_to", c["cross_from"])}
                    break
        if weak_delivery:                            # §5.2：先过弱线索闸门，再选族
            g = DS.gate(text=t, src_cues=cues, hint=hint, prev=prev,
                        hint_self_report=hint_phonation, ja_stage_cue=ja_stage_cue)
            delivery, d_rel = g["phonation"], g["reliability"]
            d_log, d_trusted = g["log"], g["trusted"]
            d_tier = g["tier"]                       # 证据等级（①/③ 打开时才有）
            if repeat and delivery:
                # 可信链**不刷新**：这条信任是第一次给的，这里只是原样带走（也续不到下句）
                d_trusted = bool(prev.get("delivery_trusted"))
                if d_log:
                    d_log = dict(d_log, reason="explicit_directive_repeat", repeat_in_scope=True)
        else:
            delivery, d_rel = _delivery(t, cues, hint)
            d_log, d_trusted, d_tier = None, False, None
        # 闸门弃权（没有可信的发声证据）时，hint 的 phonation 一律作废：既不参与选族，也不参与
        # 强度档（否则「小声じゃないよ。」会被念成耳语、「はい。」+breathy 会把弱档推成强档）。
        # —— Sol 第 7/8 轮两条旁路。
        _h = hint
        if weak_delivery and not delivery and isinstance(hint, dict) and "phonation" in hint:
            _h = {k: v for k, v in hint.items() if k != "phonation"}
        # ① 的族隔离（v6）：只有 hint 自述这一路证据时，**发声方式不单独选族** —— 低可信的
        # "怎么说"不该把语义族从 neutral 抬成 intimate（Sol 第 13 轮第五条：`はい、わかりました。`
        # + hint whispered 之前会变成 intimate）。发声照旧只决定 delivery：neutral 族借用码本里
        # 既有的耳语锚（合法组合）。句面明确（D/S/J，d_tier ≠ hint）时不动，照旧参与选族。
        # ⑧ 证据冲突（Sol 第 18 轮）：已采纳的**明确**发声线索（句面 / 日语舞台提示 / 未引述 cue，
        # 即 d_tier=explicit_surface）与 hint 的 phonation 相冲突时，该 hint phonation 从此作废 ——
        # 不选族、也不算 level（与上面 ① 的收窄同口径）。冲突的尺子直接用既有的
        # `F.phonation_conflict`：voiced/soft ↔ whispered/breathy 才算打架，whispered↔breathy
        # 只是同一轴上的强弱（「（小声で）」+ hint breathy 这类不属于冲突，行为不变）。
        # 只在 ①/③ 打开时 d_tier 才有值 ⇒ 这段天然只在 v6 生效，v2/v3/v4/v5 不经过。
        if d_tier == DS.TIER_SURFACE and isinstance(_h, dict) \
                and F.phonation_conflict(_h.get("phonation"), delivery, 1.0):
            _h = {k: v for k, v in _h.items() if k != "phonation"}
        fam_h = _h
        if d_tier == DS.TIER_HINT and isinstance(_h, dict) and _h.get("phonation"):
            fam_h = {k: v for k, v in _h.items() if k != "phonation"}
        rl = CB.cue_level(t, _hint_level(us[i])) + CB.hint_level_bonus(_h)
        rl = max(0.0, min(1.0, rl))
        # 没有上一档可继承（本 turn 的第一片）时先落地一个起点：本句 cue level。
        # 下面各分支的推进都写在 `if advance_now` 里，续片（advance=False）不推进 —— 不初始化的话
        # level 会一路保持 None，到记录处 `round(float(level), 3)` 直接 TypeError（2026-09-30 生产：
        # 首片 soft 切 ⇒ advance=False ⇒ /v2/plan 400 ⇒ 这一片完全没声音）。
        # `step_level(None, rl)` 本来就返回 rl，所以 advance=True 的既有路径逐字不变。
        if level is None:
            level = rl
        hint_fam = CB.family_from_hint(fam_h, cbv)    # 映射规则按码本版本走：v1 保持旧行为
        seed = fam_prev or ctx_seed          # 继承的起点：本会话上一档，其次上文
        if override:
            fam, why = STYLE_FAMILY[override], "override=%s" % override
            level = 1.0
            held = 0
        elif raw[i] != "calm":                       # 关键词命中明确信号：立即切，不粘滞
            fam, why = STYLE_FAMILY[raw[i]], "style=%s" % raw[i]
            # 提示只在两边说不到一起时补充：规则命中"中性"档而提示更明确（弱声/激动/低落）时听提示
            if hint_fam and hint_fam != "neutral" and fam == "neutral":
                fam, why = hint_fam, "style=%s+hint" % raw[i]
            if advance_now:                       # 续片/同源句的重放不重复推进强度（astra-3 §A）
                level = CB.step_level(level, rl)
            held = 0
        elif hint_fam and hint_fam != "neutral":     # 关键词没信号，但 LLM 判出来了 —— 这正是升级点
            fam, why = hint_fam, "hint(%s)" % (hint.get("phonation") or hint_fam)
            if advance_now:                       # 续片/同源句的重放不重复推进强度（astra-3 §A）
                level = CB.step_level(level, rl)
            held = 0
        elif seed and held < _HOLD_MAX:              # 都没信号：继承上一档，强度逐句衰减
            fam = seed
            # 计数与衰减只在**新自然句**上各走一次：软切/强切出来的续片不重复衰减（Sol r2 建议改）
            if advance_now:
                level = max(0.0, (level if level is not None else rl) - _HOLD_DECAY)
                held += 1
            why = "inherit(%s)%d%s" % (seed, held, "" if fam_prev else "+ctx")
        else:                                        # 开头没信号，或没信号太久 ⇒ 释放到平静
            fam, why = "neutral", ("release(无信号%d句)" % held if seed else "style=calm")
            if advance_now:                       # 续片/同源句的重放不重复推进强度（astra-3 §A）
                level = CB.step_level(level, rl)
            held = 0
        # v5（astra 第 11 轮 ②）：拒绝/制止/安抚类的语义闸门。先分类、再按禁令换族 —— 只在
        # 即将采用的族正是被禁的那个（bright，来自"高唤醒自动等于 bright"）时才动手；
        # 换出去的族仍然是码本里训过的锚（sad / neutral），**不新增字族与训练锚**。
        guard = RG.apply(t, fam, _h, explicit_style=bool(override)) if refusal_guard else None
        rebase = None
        if guard and guard["veto"]:
            fam = guard["replacement"]
            why = "%s+guard(%s:%s→%s)" % (why, guard["class"], guard["hit"], fam)
            if level_rebase:
                # ④ 换族后**移除被否决 hint、以文本先验重置强度**（闸门否掉的正是"上游 arousal
                # 就是响声证据"，所以重置丢弃该项：只留文本先验 + 发声方式微调）。
                # 注意：公式读的是**文本**先验，不读替换后的族 —— 族相关的强度校准**未验收**
                # （Sol 第 13 轮第五/六条），字段名按实际做的事写，不叫"按新族重算"。
                before = level
                level = max(0.0, min(1.0, CB.cue_level(t, None) + CB.hint_level_bonus(_h)))
                rebase = {"before": round(float(before if before is not None else 0.0), 3),
                          "after": round(float(level), 3),
                          "basis": "guard_veto_text_prior_reset"}
        cond = CB.compile_condition(fam, level, tier_prev if fam == fam_prev else None)
        if d_tier == DS.TIER_HINT and cond["tier"] != "weak":
            # ① 档位保守：hint 自述单独成立时只给弱档（不因提示的高唤醒/气声跳到强档）
            cond = dict(cond, tier="weak", caption=CB.ladder(cond["family"])[0])
        # 发声闸门（方案 1.4：发声与族分开门控）：本句明说了怎么发声（「（小声）」/「囁く」/
        # 「息を殺して」，其次 LLM 自述）时，不许选一条与它打架的锚 —— 耳语/弱声绝不能被喂
        # 成 `息を荒くして…` 那种粗气息。不匹配时换同族另一条锚，都没有就记 coverage_gap。
        # （发声闸门已在本单元开头算过：先闸门后选族，弃权时不参与选族）
        gap = None
        if delivery:
            gated = F.compile_target(fam, 0.0 if level is None else level,
                                     tier_prev if fam == fam_prev else None, delivery,
                                     allow_neutral_whisper=allow_neutral_whisper,
                                     force_weak=(d_tier == DS.TIER_HINT))
            gap, cond = gated.get("gap"), gated
        if guard and guard["veto"]:
            # 强档一起禁（`FORCE_WEAK`）：上游高 arousal 不是"要更大声"的证据 —— 安抚/拒绝的
            # 强档要么是"声を張り上げて"（bright，已被禁），要么是更"张"的中性锚，与降温相反。
            if guard["force_weak"]:
                cond = dict(cond, tier="weak", caption=CB.ladder(cond["family"])[0])
            if not gap:                              # 如实记缺口：族是**被禁令换掉的**
                gap = RG.GAP
        tier_prev = cond["tier"]
        if fam != fam_prev:
            scene = scene + (1 if fam_prev else 0)
        fam_prev = fam
        recs.append({"plan_version": plan_version, "unit_id": us[i]["unit_id"],
                     "parent_id": us[i]["parent_id"], "sub_index": i, "role": "speech",
                     "family": cond["family"], "caption": cond["caption"], "cfg": cond["cfg"],
                     "tier": cond["tier"], "level": round(float(level), 3),
                     "style": (override or (raw[i] if raw[i] != "calm" else fam)),
                     "scene_id": scene,
                     "reason": "%s level=%.2f tier=%s%s%s" % (
                         why, level, cond["tier"],
                         "" if not delivery else " delivery=%s" % delivery,
                         " repeat_in_scope" if repeat else ""),
                     "diagnostics": {"policy": (
                         POLICY_V6 if (hint_phonation or ja_stage_cue or level_rebase or cross_sentence)
                         else POLICY_V5 if refusal_guard
                         else (POLICY_V4 if allow_neutral_whisper else "cascade")),
                                     "delivery": delivery,
                                     "delivery_rel": d_rel, "gap": gap,
                                     **(dict(delivery_tier=d_tier) if d_tier else {}),
                                     **(dict(level_rebase=rebase) if rebase else {}),
                                     **(dict(guard=guard) if guard else {}),
                                     **(dict(cue_scope={
                                         "id": sid, "basis": sbasis,
                                         "cues": len(cues or []),
                                         "out_of_scope": sdrop,
                                         "repeat": repeat,
                                         **({"mode": ("cross_sentence" if cross else "sentence"),
                                             "cross": cross,
                                             # 跨 unit 的续接在**桥侧**（重放 source_prefix）：本层只消费副本、不外推
                                             "cross_unit": CS.CROSS_UNIT,
                                             "cross_sentence": CS.CROSS_SUPPORTED}
                                            if cross_sentence
                                            else {"cross_sentence": CS.CROSS_SENTENCE})})
                                                 if cue_scope else {}),
                                     **(dict(delivery_stage=d_log) if d_log else {})}})
        if voice_caption and us[i]["voice_caption"]:
            _apply_voice_caption(recs[-1], us[i]["voice_caption"])
    last = recs[-1]
    state = {"family": last["family"], "caption": last["caption"], "level": last["level"],
             "style": last["style"], "scene_id": last["scene_id"], "tier": last["tier"],
             "held": held}
    if weak_delivery:
        # 末句的发声 + 它是否**可信**（由 D 或被接纳的多证据决定）—— 下一句的 H 组只看这个，
        # 弱延续给出去的 trusted 恒为 False，所以不能靠延续给自己滚动续期。
        state["delivery"] = delivery
        state["delivery_trusted"] = bool(d_trusted)
    if cue_scope:
        # 生效中的指令属于哪个源句：下一段身份相同 ⇒ 是**同一条指令的续片**（不重复加档）；
        # 身份不同或本段没有发声指令 ⇒ 局部 D 已随句末失效（下一句只能靠自己/既有的一跳）。
        state["delivery_scope"] = sid if delivery else None
    return recs, state


# ---- 模型真实 token 预算（astra-3 §1.1/§2.3.4；整段那条腿已按指挥官口径停做）--------
TOKEN_LIMIT = 255       # 底模 max_text_len=256（含 BOS）⇒ 正文可用 255（astra-3 §1.1 实测）
_SPLIT_CAND = re.compile(r"(?<=[。！？!?…‥])|(?<=[、，,；;])")


def split_within_budget(text, count, limit=TOKEN_LIMIT):
    """按句界/顿号把文本切到**模型真实 token 预算**内：返回 `[(片段, 是否硬切), …]`。

    `count` 由调用方注入（真 tokenizer 在服务进程里；测试用假计数器）。找不到装得下的
    句界就只能硬切 —— 硬切切在词中、发音会坏，所以标 True 让调用方**显式报告**，不当成功。
    这里是"不许 tokenizer 静默截断"的落点（astra-3 §2.3.4）；不 strip，保证拼接后 **逐字等于原文**。
    """
    out, buf, buf_hard = [], "", False
    for chunk in (c for c in _SPLIT_CAND.split(text or "") if c):
        if buf and count(buf + chunk) > limit:
            out.append((buf, buf_hard))
            buf, buf_hard = "", False
        buf = buf + chunk
        if count(buf) > limit:
            while count(buf) > limit:            # 单个 chunk 自己就超预算：只能在字符上硬切
                lo, hi = 1, len(buf)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if count(buf[:mid]) <= limit:
                        lo = mid
                    else:
                        hi = mid - 1
                out.append((buf[:lo], True))
                buf = buf[lo:]
            buf_hard = True                      # 尾巴也是被硬切下来的，别当软切混过去
    if buf:
        out.append((buf, buf_hard))
    return out


FUSION_POLICY = "fusion_v1"


def plan_units_fusion(units, extra_ctx="", prev=None, style_override=None, voice_caption=False):
    """fusion_v1：五路证据融合 + 低置信不切换（方案第一问；实现见 `emotion_fusion.py`）。

    与 cascade（`plan_units_v3`）的区别：
      * 判定不再"先命中的规则说了算"，而是五路证据加权比较、过了门槛才切；
      * 门槛不过就**继承**，并把"无新证据"计数推进（连续 8 个完整自然句后释放）；
      * 只在**新自然句**上推进计数/衰减（soft/force 切出的子段不重复衰减）；
      * 目标发声与候选锚不相容时换档或记 `coverage_gap`，不硬喂粗气息；
      * 送给模型的条件仍然只是码本里训过的完整日语短句。
    """
    import emotion_fusion as F
    us = _norm_units(units)
    prev = _clean_prev(prev)
    if not us:
        return [], dict(prev)
    override = style_override if style_override in STYLES else None
    p_prev = prev.get("p")
    fam_prev, tier_prev = prev.get("family"), prev.get("tier")
    level = prev.get("level")
    scene = int(prev.get("scene_id") or 1)
    held = int(prev.get("held") or 0)
    recs = []
    for i, u in enumerate(us):
        t = u["text"]
        hint = u.get("emotion") if isinstance(u.get("emotion"), dict) else None
        advance = u.get("advance", True) is not False
        llm_ph = hint.get("phonation") if hint else None
        u_llm, r_llm = F.llm_support(hint)
        u_ja, ph_ja, r_ja = F.ja_support(t)
        u_zh, ph_zh, r_zh = F.zh_support(u.get("src_cues"))
        u_pr, ph_pr, r_pr = F.prosody_support(t)
        ev = [("llm", u_llm, r_llm), ("ja", u_ja, r_ja),
              ("zh", u_zh, r_zh), ("prosody", u_pr, r_pr)]
        eff = F.effective_origins(ev)          # 权重与门槛都按"实际说话的几路"算
        p, Q, conflict = F.fuse(ev, p_prev, weights=F.weights_for(eff))
        d = F.decide(p, Q, fam_prev, tier_prev, q_min=F.quality_bar(eff))
        # 明确直述的发声指令（原文括号提示 / 原文词）优先于 LLM 的自述，其次韵律
        phon = ph_zh or ph_ja or llm_ph or ph_pr
        # 原文直述 vs LLM 自述的发声冲突：暴露出来，但采用原文（可靠度最高）
        direct_ph, direct_rel = (ph_zh, r_zh) if ph_zh else (ph_ja, r_ja)
        conflict = conflict or F.phonation_conflict(llm_ph, direct_ph, direct_rel)
        rl = CB.cue_level(t, _hint_level(u))
        fresh = bool(d.get("switch"))
        if override:
            fam, why, held = STYLE_FAMILY[override], "override=%s" % override, 0
            level, tier_prev = 1.0, None
        elif fresh:
            fam, why, held = d["family"], "fusion:%s(p=%.2f,m=%.2f,Q=%.2f)" % (
                d["reason"], d["confidence"], d["margin"], d["Q"]), 0
        elif fam_prev and held < _HOLD_MAX:
            fam, why = fam_prev, "inherit(%s)%d(%s)" % (fam_prev, held + (1 if advance else 0),
                                                        d["reason"])
            if advance:
                held += 1
        else:
            fam = "neutral"
            why = "release(无信号%d句)" % held if fam_prev else "no_evidence"
            held = 0
        if fresh or override or Q >= F.Q_MIN:
            if us[i].get("advance", True) is not False:   # 续片不重复推进强度（astra-3 §A）
                level = CB.step_level(level, rl)
        elif advance:
            level = max(0.0, (level if level is not None else rl) - _HOLD_DECAY)
        gap = F.coverage_gap(hint)
        cond = F.compile_target(fam, 0.0 if level is None else level,
                                tier_prev if fam == fam_prev else None, phon)
        if fam != fam_prev:
            scene = scene + (1 if fam_prev else 0)
        fam_prev, tier_prev = fam, cond["tier"]
        recs.append({"plan_version": PLAN_VERSION_V3, "unit_id": u["unit_id"],
                     "parent_id": u["parent_id"], "sub_index": i, "role": "speech",
                     "family": cond["family"], "caption": cond["caption"], "cfg": cond["cfg"],
                     "tier": cond["tier"], "level": round(float(level or 0.0), 3),
                     "style": (override or (_match(t) or fam)),
                     "scene_id": scene,
                     "reason": "%s level=%.2f tier=%s" % (why, level or 0.0, cond["tier"]),
                     "diagnostics": {"policy": FUSION_POLICY, "p": _round_p(p), "Q": round(Q, 3),
                                     "margin": d.get("margin"), "conflict": conflict,
                                     "gap": gap or cond.get("gap"), "phonation": phon,
                                     "held": held}})
        if voice_caption and u["voice_caption"]:
            _apply_voice_caption(recs[-1], u["voice_caption"])
    last = recs[-1]
    state = {"family": last["family"], "caption": last["caption"], "level": last["level"],
             "style": last["style"], "scene_id": last["scene_id"], "tier": last["tier"],
             "held": held, "p": _round_p(p)}
    return recs, state


def _round_p(p):
    return None if not p else {f: round(float(v), 3) for f, v in p.items()}


def install(app):
    """把 `/v2/plan` 挂到已有 app 上（纯 CPU；I 与协调器共用同一个进程即可，不新增服务）。"""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.post("/v2/plan")
    async def v2_plan(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "bad_request: %s" % str(e)[:120]}, status_code=400)
        units = body.get("units") or []
        ctx = body.get("context_before", "")
        try:                                   # 只认明确支持的版本，别让 >=3 的怪值走进来
            pv = int(body.get("plan_version") or PLAN_VERSION)
        except (TypeError, ValueError):
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "plan_version 非法：%r" % (body.get("plan_version"),)},
                                status_code=400)
        if pv not in (PLAN_VERSION, PLAN_VERSION_V3, PLAN_VERSION_V4, PLAN_VERSION_V5,
                      PLAN_VERSION_V6):
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "不支持的 plan_version：%r（只支持 %d/%d/%d/%d/%d）"
                                          % (body.get("plan_version"), PLAN_VERSION,
                                             PLAN_VERSION_V3, PLAN_VERSION_V4,
                                             PLAN_VERSION_V5, PLAN_VERSION_V6)},
                                status_code=400)
        policy = str(body.get("emotion_policy") or "cascade")
        if policy not in ("cascade", FUSION_POLICY):
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "不支持的 emotion_policy：%r（支持 cascade/%s）"
                                          % (policy, FUSION_POLICY)}, status_code=400)
        want_cb = CB.CODEBOOK_VERSION_V2 if pv >= PLAN_VERSION_V4 else CB.CODEBOOK_VERSION
        if body.get("codebook_version") not in (None, want_cb):
            # 请求显式报了版本就必须一致：说 v2 而只会产出 v1（或反之）时静默照做，
            # 会让下游按错的版本校验（Sol 第 6 轮第五反例）。
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "codebook_version 不匹配：请求 %r，plan_version=%r 只会产出 %d"
                                          % (body.get("codebook_version"), pv, want_cb)},
                                status_code=400)
        if pv >= PLAN_VERSION_V3:
            v4 = pv >= PLAN_VERSION_V4          # v5/v6 也走 codebook v2（v5 = v4 + 语义闸门）
            v6 = pv >= PLAN_VERSION_V6
            if v4 and policy == FUSION_POLICY:
                # 版本组合不匹配就**明确失败**，不静默降级（astra-3 §5.1 的版本隔离要求）
                return JSONResponse({"status": "failed", "stage": "plan",
                                     "error": "plan_version=4/5/6 只支持 cascade（融合路线未校准）"},
                                    status_code=400)
            fn = plan_units_fusion if policy == FUSION_POLICY else plan_units_v3
            weak = v4 and _weak_delivery_on(body)
            cs = v4 and _cue_scope_on(body, default=v6)   # ① cue 按源句归属（v6 默认开）
            guard = pv >= PLAN_VERSION_V5 and _refusal_guard_on(body)   # ② 拒绝类语义闸门（v5 默认开）
            hp = v6 and _hint_phonation_on(body)          # ① hint 自述发声等级
            xs = v6 and _cross_sentence_on(body)          # ② 跨句指令（本 unit 内）
            jc = v6 and _ja_stage_cue_on(body)            # ③ 日语表层舞台提示
            lr = v6 and _level_rebase_on(body)            # ④ 换族后 level 重算
            vc = _voice_caption_on(body)                  # 阶段 3d：受控声音描述（默认关）
            extra = {} if policy == FUSION_POLICY else {"plan_version": pv,
                                                        "allow_neutral_whisper": v4,
                                                        "weak_delivery": weak,
                                                        "cue_scope": cs,
                                                        "refusal_guard": guard,
                                                        "hint_phonation": hp,
                                                        "cross_sentence": xs,
                                                        "ja_stage_cue": jc,
                                                        "level_rebase": lr}
            extra["voice_caption"] = vc
            try:
                recs, state = fn(units, ctx, prev=body.get("prev"),
                                 style_override=body.get("style_override"), **extra)
            except Exception as e:
                if v4:
                    # v4/v5 的版本隔离是硬要求（astra-3 §5.1）：宁可不说话，也不给一条"报 v5、
                    # 实则 v2"的单元 —— 它的记录不带 codebook_version，render 端照样会拒。
                    # 这条出口必须留痕：报 400 时下游只看到状态码，真因只在这里
                    # （2026-09-30 排障：prod_irodori.log 里一行都没有）。
                    uid = next((u.get("unit_id") for u in units if isinstance(u, dict)), "?")
                    print("[irodori/scene] v%d 规划失败（unit=%s）：%s: %s"
                          % (pv, uid, type(e).__name__, str(e)[:160]), flush=True)
                    return JSONResponse({"status": "failed", "stage": "plan",
                                         "error": "v%d 规划失败：%s: %s"
                                                  % (pv, type(e).__name__, str(e)[:120])},
                                        status_code=400)
                # 安全网：v3 出任何意外都退回 v2 计划 —— 宁可用旧语气，也不能让这句话没声音。
                # 退回会留日志（生产排查看得到），不是静默掩盖。
                print("[irodori/scene] %s 规划失败，退回 v2：%s: %s"
                      % (policy, type(e).__name__, str(e)[:160]), flush=True)
                try:
                    recs = plan_units(units, ctx)
                except Exception as e2:
                    return JSONResponse({"status": "failed", "stage": "plan",
                                         "error": "%s: %s" % (type(e2).__name__, str(e2)[:120])},
                                        status_code=200)
                for r in recs:
                    r["voice"] = body.get("voice")
                return JSONResponse({"plan_version": PLAN_VERSION, "units": recs, "fallback": "v2"})
            for r in recs:
                r["voice"] = body.get("voice")
                if v4:
                    r["codebook_version"] = CB.CODEBOOK_VERSION_V2   # 记录级版本：render 按它校验
            out = {"plan_version": pv, "policy": policy, "units": recs, "state": state}
            if v4:
                out["codebook_version"] = CB.CODEBOOK_VERSION_V2     # 老响应不追加新字段
                out["weak_delivery"] = weak                          # §5.2 是否打开（归因用）
                if guard:
                    out["refusal_guard"] = True                      # ② 闸门是否打开（归因用）
                if cs:
                    out["cue_scope"] = True                          # 开关关时响应形状不变
                if vc:
                    out["voice_caption"] = True                      # 阶段 3d 开关打开（归因用）
                if v6:
                    # v6 的四个开关**逐个**报出来（归因用）：关掉哪个、还剩哪个，一眼可读
                    out["v6_features"] = {"hint_phonation": hp, "cross_sentence": xs,
                                          "ja_stage_cue": jc, "level_rebase": lr}
            return JSONResponse(out)
        try:
            recs = (strict_records(units, ctx) if body.get("mode") == "strict"
                    else plan_units(units, ctx))
        except PlanMapError as e:
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "plan_map_mismatch: %s" % str(e)[:200]}, status_code=200)
        except Exception as e:
            return JSONResponse({"status": "failed", "stage": "plan",
                                 "error": "%s: %s" % (type(e).__name__, str(e)[:120])}, status_code=200)
        for r in recs:
            r["voice"] = body.get("voice")
        return JSONResponse({"plan_version": PLAN_VERSION, "units": recs})
    return app
