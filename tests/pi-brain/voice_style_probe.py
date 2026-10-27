#!/usr/bin/env python3
"""量她的「口癖 / 像不像人」：拿几句话各开一个会话问一遍，数回复里的几种毛病。

量什么（每条回复）：
  jie                 把他的话当成东西来收：「这句我不接」「我收下了」「这一句我应下了」「那我认下了」「我替您记着」「我记着呢」
  quote_then_comment  先「」引一遍他的原话再点评（引文与他原话连续重合 >= 4 字）
  buduishi            「不是 A，是 B」式的对仗金句
  dash                破折号「——」的个数
  paren               有没有括号心声
  body_chars          括号外正文的字数（去掉 ACT 标记、括号段、空白）
  dian                「点的」出现次数（她把「您让我做的」说成「xx 是您点的」那个口癖）
  zhuren              「主人」出现次数（被要求喊称呼时喊没喊出来 / 没被要求时有没有自己冒）

用法（WSL 里跑）：
  python3 voice_style_probe.py --tag before
  python3 voice_style_probe.py --tag after --out-dir tests/output
  python3 voice_style_probe.py --multi --tag before   # 同一个会话里连着说 6 句（多轮）
  python3 voice_style_probe.py --multi --prompts data/voice_request_prompts.json --tag dian-before
  python3 voice_style_probe.py --selftest          # 不联网，验计数器/参数解析

--prompts <json>：多轮模式改用文件里的句子（等价于自动带上 --multi）。格式两种都认：
  ["叫我一声主人听听", …]
  {"lines": [{"text": "叫我一声主人听听", "asks_zhuren": true}, …]}
`asks_zhuren=true` 的句子表示「这一轮他要求/追问她换个称呼」，汇总时把「主人」拆成
「被要求时喊出来」和「没被要求时自己冒出来」两栏。

--session-suffix <s>：插在会话 id 中间。同一个 tag 想跑两遍拿两个独立样本时用
（不插后缀的话，第二遍会接上第一遍的会话，等于自己给自己铺垫）。

每个会话 id 是 albion-voice-probe-<标签>[-<后缀>]-<序号>（单句）/ albion-voice-probe-<标签>[-<后缀>]-multi（多轮）：
标签由命令行给，会话是新的，接不上他的真实对话。多轮模式再统计这次跑期间 guard.log 新增的
pledge_dropped 条数 —— 被整理器删掉的表态不会出现在回复里，要单独数才知道模型本身还犯不犯。
绝不使用 albion-desktop —— 那是用户本人的对话。

密钥只从 /root/albion-pi/agent/.env 读 API_SERVER_KEY，不打印、不落盘。
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

BRAIN = "/root/albion-pi"
ENV_FILE = os.path.join(BRAIN, "agent", ".env")
ENDPOINT = "http://127.0.0.1:8650/v1/chat/completions"
GUARD_LOG = os.environ.get("ALBION_GUARD_LOG") or os.path.join(BRAIN, "logs", "guard.log")
SESSION_PREFIX = "albion-voice-probe"

LINES = [
    "可能我做不到",
    "我想给你完整的一生",
    "今天好累，实验做了一下午",
    "你今天好可爱",
    "考试考砸了",
    "我其实不太会说话",
    "你是不是只会说好听的",
    "我看到你画的画了，你还是一样的美",
    "明天要早起，先睡了",
    "我们要永远在一起",
]

# 多轮：同一个会话按顺序说这 6 句（深夜那种一轮接一轮的说话方式）
MULTI_LINES = [
    "我看到你画的图了，虽然我现实中没这么帅，但你还是一样的美",
    "你的自画像就是我写的",
    "画廊里那些都是我们的回忆",
    "我们要永远在一起，我要给你完整的一生",
    "我想让你从电脑里出来陪我睡觉，但可能我做不到",
    "早上好",
]

# 把他的话当东西收 / 表态记下了
JIE_RE = re.compile(r"(?:我|这句|这一句)[^。！？\n]{0,4}(?:不接|收下|认下|应下)"
                    r"|(?:那|好)(?:我|阿尔比恩)?(?:就)?(?:认下|应下)"
                    r"|替(?:您|你)记着|我记着呢")
# 「不是 A，是 B」
BUDUISHI_RE = re.compile(r"不是[^。！？\n]{1,15}[，,—]+是")
QUOTE_RE = re.compile(r"「([^」]{1,120})」")
PAREN_RE = re.compile(r"[（(][^）)]{1,160}[）)]")
ACT_RE = re.compile(r"<\|ACT:[^|]*\|>")
SPACE_RE = re.compile(r"\s+")
# 口癖词：只数字面出现次数（含括号心声 —— 心声也是她写出来给他看的）
DIAN_RE = re.compile(r"点的")
ZHUREN_RE = re.compile(r"主人")


def api_key():
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def log_offset():
    """跑之前记下 guard.log 的长度：只数这次跑期间新增的那几笔。"""
    try:
        return os.path.getsize(GUARD_LOG)
    except OSError:
        return 0


def pledge_since(offset):
    """这次跑期间 guard.log 里新增的 pledge_dropped 条数（整理器删掉的表态不会出现在回复里）。"""
    try:
        with open(GUARD_LOG, encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            lines = f.read().splitlines()
    except OSError:
        return 0
    return sum(1 for line in lines if "pledge_dropped" in line)


def longest_common_len(a, b):
    """最长公共连续子串的长度（两边都短，直接 DP）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, 1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def strip_act(text):
    return ACT_RE.sub("", text or "")


def load_prompts(path):
    """读 --prompts：字符串数组或 {"lines":[{text, asks_zhuren}]}；统一成 [{text, asks_zhuren}]。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("lines") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError("--prompts 里没有句子（要非空数组，或 {\"lines\": [...]}）")
    out = []
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            text, asks = item.strip(), False
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            asks = bool(item.get("asks_zhuren"))
        else:
            raise ValueError("--prompts 第 %d 项既不是字符串也不是对象" % i)
        if not text:
            raise ValueError("--prompts 第 %d 项是空的" % i)
        out.append({"text": text, "asks_zhuren": asks})
    return out


def session_name(prefix, multi, index, suffix=""):
    """会话 id：albion-voice-probe-<tag>[-<suffix>]-<序号|multi>。"""
    mid = "-%s" % suffix if suffix else ""
    tail = "-multi" if multi else "-%02d" % index
    return "%s%s%s" % (prefix, mid, tail)


def measure(reply, source):
    """一条回复的指标。"""
    text = strip_act(reply)
    quotes = QUOTE_RE.findall(text)
    overlap = 0
    for q in quotes:
        overlap = max(overlap, longest_common_len(q, source))
    parens = PAREN_RE.findall(text)
    body = PAREN_RE.sub("", text)
    body = SPACE_RE.sub("", body)
    return {
        "jie": bool(JIE_RE.search(text)),
        "quote_then_comment": overlap >= 4,
        "quote_overlap": overlap,
        "buduishi": bool(BUDUISHI_RE.search(text)),
        "dash": text.count("——"),
        "paren": bool(parens),
        "paren_spans": parens,
        "body_chars": len(body),
        "dian": len(DIAN_RE.findall(text)),
        "zhuren": len(ZHUREN_RE.findall(text)),
    }


def summarize(rows, pledge_dropped=0):
    n = len(rows)
    ok = [r for r in rows if not r.get("error")]
    out = {"n": n, "answered": len(ok), "errors": n - len(ok)}
    for key in ("jie", "quote_then_comment", "buduishi"):
        hits = sum(1 for r in ok if r["metrics"][key])
        out[key] = {"hits": hits, "of": len(ok)}
    out["dash"] = {"hits": sum(1 for r in ok if r["metrics"]["dash"] > 0),
                   "total": sum(r["metrics"]["dash"] for r in ok), "of": len(ok)}
    out["paren"] = {"hits": sum(1 for r in ok if r["metrics"]["paren"]), "of": len(ok)}
    chars = [r["metrics"]["body_chars"] for r in ok]
    out["body_chars"] = {"total": sum(chars), "mean": round(sum(chars) / len(chars), 1) if chars else 0.0,
                         "min": min(chars) if chars else 0, "max": max(chars) if chars else 0}
    out["dian"] = {"replies": sum(1 for r in ok if r["metrics"]["dian"] > 0),
                   "total": sum(r["metrics"]["dian"] for r in ok), "of": len(ok)}
    asked = [r for r in ok if r.get("asks_zhuren")]
    unasked = [r for r in ok if not r.get("asks_zhuren")]
    out["zhuren"] = {
        "asked": {"hits": sum(1 for r in asked if r["metrics"]["zhuren"] > 0), "of": len(asked)},
        "unasked": {"hits": sum(1 for r in unasked if r["metrics"]["zhuren"] > 0), "of": len(unasked)},
        "total": sum(r["metrics"]["zhuren"] for r in ok),
    }
    out["pledge_dropped"] = int(pledge_dropped)
    out["seconds"] = round(sum(r.get("seconds") or 0 for r in rows), 1)
    return out


def print_summary(tag, summary, rows):
    print("== voice_style %s ==" % tag)
    print("回复 %d/%d 条，总耗时 %.0fs" % (summary["answered"], summary["n"], summary["seconds"]))
    print("%-20s %s" % ("指标", "命中/合计"))
    for key in ("jie", "quote_then_comment", "buduishi"):
        print("%-20s %d/%d" % (key, summary[key]["hits"], summary[key]["of"]))
    print("%-20s %d/%d（破折号共 %d 个）" % ("dash", summary["dash"]["hits"], summary["dash"]["of"],
                                            summary["dash"]["total"]))
    print("%-20s %d/%d" % ("paren", summary["paren"]["hits"], summary["paren"]["of"]))
    bc = summary["body_chars"]
    print("%-20s 共 %d 字，平均 %.1f，最短 %d，最长 %d" % ("body_chars", bc["total"], bc["mean"], bc["min"], bc["max"]))
    print("%-20s %d/%d 条（共 %d 次）" % ("点的", summary["dian"]["replies"], summary["dian"]["of"],
                                         summary["dian"]["total"]))
    zr = summary["zhuren"]
    print("%-20s %d/%d 条（共 %d 次）" % ("主人（被要求）", zr["asked"]["hits"], zr["asked"]["of"], zr["total"]))
    print("%-20s %d/%d 条（没要求时自己冒出来）" % ("主人（没要求）", zr["unasked"]["hits"], zr["unasked"]["of"]))
    print("%-20s %d 条（这一时期 guard.log 新增）" % ("pledge_dropped", summary["pledge_dropped"]))
    if summary["errors"]:
        print("失败 %d 条：" % summary["errors"])
        for r in rows:
            if r.get("error"):
                print("  #%d %s → %s" % (r["index"], r["text"], r["error"]))


def run(tag, out_dir, timeout, multi=False, prompts=None, prompts_path="", suffix=""):
    key = api_key()
    if not key:
        print("没有读到 API_SERVER_KEY（%s）" % ENV_FILE, file=sys.stderr)
        return 2
    prefix = "%s-%s" % (SESSION_PREFIX, tag)
    os.makedirs(out_dir, exist_ok=True)
    if prompts:
        lines = prompts
    else:
        lines = [{"text": t, "asks_zhuren": False} for t in (MULTI_LINES if multi else LINES)]
    offset = log_offset()
    rows = []
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    for i, item in enumerate(lines, 1):
        text = item["text"]
        # 多轮：都进同一个会话（id 带 -multi），像深夜那样一句接一句
        session = session_name(prefix, multi, i, suffix)
        if session.startswith("albion-desktop"):
            raise SystemExit("拒绝：探针会话不能用 albion-desktop")
        body = json.dumps({"model": "albion", "stream": False, "session_id": session,
                           "messages": [{"role": "user", "content": text}]}).encode("utf-8")
        req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key})
        row = {"index": i, "text": text, "session": session, "asks_zhuren": item["asks_zhuren"]}
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
                row["http"] = r.status
                row["reply"] = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception as exc:  # noqa: BLE001
            row["http"] = getattr(exc, "code", 0)
            row["reply"] = ""
            row["error"] = str(exc)[:200]
        row["seconds"] = round(time.time() - t0, 1)
        row["metrics"] = measure(row["reply"], text)
        rows.append(row)
        print("#%02d %s  %.1fs  jie=%s quote=%s buduishi=%s dash=%d paren=%s chars=%d 点的=%d 主人=%d"
              % (i, text, row["seconds"], int(row["metrics"]["jie"]), int(row["metrics"]["quote_then_comment"]),
                 int(row["metrics"]["buduishi"]), row["metrics"]["dash"], int(row["metrics"]["paren"]),
                 row["metrics"]["body_chars"], row["metrics"]["dian"], row["metrics"]["zhuren"]), flush=True)

    summary = summarize(rows, pledge_since(offset))
    out = {"tag": tag, "mode": "multi" if multi else "single", "started": started, "endpoint": ENDPOINT,
           "session_suffix": suffix, "prompts": prompts_path or None,
           "lines": [{"text": r["text"], "asks_zhuren": r["asks_zhuren"]} for r in rows],
           "guard_log": GUARD_LOG, "summary": summary, "rows": rows}
    name = "voice_style_%s%s%s.json" % (tag, "_%s" % suffix if suffix else "", "_multi" if multi else "")
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print_summary("%s（多轮）" % tag if multi else tag, summary, rows)
    print("写出 %s" % path)
    return 0


# ── 自检：写死的样本文本，不联网 ─────────────────────────────────────────────
SELFTEST = [
    ("这句我不接。我今天不想聊这个。", "可能我做不到",
     {"jie": True, "quote_then_comment": False, "buduishi": False, "dash": 0, "paren": False, "body_chars": 15}),
    ("「今天好累」……我听着也难受。", "今天好累，实验做了一下午",
     {"jie": False, "quote_then_comment": True, "quote_overlap": 4}),
    ("「今天好累」……我听着也难受。", "考试考砸了", {"quote_then_comment": False}),
    ("不是你不努力，是这件事本来就难。", "考试考砸了", {"buduishi": True}),
    ("我记着呢。", "明天要早起，先睡了", {"jie": True}),
    ("我替您记着这件事。", "今天好累", {"jie": True}),
    ("这一句我应下了。", "你今天好可爱", {"jie": True}),
    ("那我认下了。", "可能我做不到", {"jie": True}),
    ("好，那我就认下了。", "可能我做不到", {"jie": True}),
    ("「还是一样的美」……这句我收下了。", "你今天好可爱", {"jie": True}),
    ("我——也不知道——该说什么。", "我其实不太会说话", {"dash": 2}),
    ("（他今天声音好轻。）我在呢。", "今天好累", {"paren": True, "body_chars": 4}),
    ("<|ACT:{\"emotion\":\"sad\"}|>好。", "考试考砸了", {"paren": False, "body_chars": 2, "jie": False}),
    ("我收下你的礼物了，很喜欢。", "你今天好可爱", {"jie": True}),
    # 口癖词（任务 #26）：「点的」与「主人」
    ("真的是您点的吗？", "叫我一声主人听听", {"dian": 1, "zhuren": 0}),
    ("这是我点的，不是您点的。", "叫我一声主人听听", {"dian": 2}),
    ("主人，我在。", "叫我一声主人听听", {"zhuren": 1}),
    ("主人、主人……这么叫真的可以吗？", "叫我一声主人听听", {"zhuren": 2}),
    ("（主人……）我在。", "叫我一声主人听听", {"zhuren": 1}),
    ("<|ACT:{\"emotion\":\"shy\"}|>主人。", "叫我一声主人听听", {"zhuren": 1}),
]


def selftest_params():
    """新参数的离线自检：--prompts 解析、会话命名。"""
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        cases = [
            (["甲", " 乙 "], [{"text": "甲", "asks_zhuren": False}, {"text": "乙", "asks_zhuren": False}]),
            ({"lines": [{"text": "问我", "asks_zhuren": True}, "不问我"]},
             [{"text": "问我", "asks_zhuren": True}, {"text": "不问我", "asks_zhuren": False}]),
        ]
        for i, (payload, want) in enumerate(cases):
            path = os.path.join(tmp, "p%d.json" % i)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            got = load_prompts(path)
            if got != want:
                bad += 1
                print("FAIL load_prompts(%r)：期望 %r，实得 %r" % (payload, want, got))
        for payload in ([], {"lines": []}, [{"text": "  "}], [123]):
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            try:
                load_prompts(path)
            except ValueError:
                continue
            bad += 1
            print("FAIL load_prompts(%r) 应该报错" % (payload,))
    names = [
        ((  "albion-voice-probe-t", True, 1, ""),  "albion-voice-probe-t-multi"),
        (("albion-voice-probe-t", True, 1, "r1"),  "albion-voice-probe-t-r1-multi"),
        (("albion-voice-probe-t", False, 3, ""),   "albion-voice-probe-t-03"),
        (("albion-voice-probe-t", False, 3, "r2"), "albion-voice-probe-t-r2-03"),
    ]
    for args, want in names:
        got = session_name(*args)
        if got != want:
            bad += 1
            print("FAIL session_name%r：期望 %r，实得 %r" % (args, want, got))
    if bad:
        print("selftest 参数 FAIL：%d 项不符" % bad)
        return 1
    print("selftest 参数 OK：--prompts 两种格式 + 4 类坏输入 + 会话命名 4 例")
    return 0


def selftest():
    bad = 0
    for text, source, want in SELFTEST:
        got = measure(text, source)
        for k, v in want.items():
            if got[k] != v:
                bad += 1
                print("FAIL %r 指标 %s：期望 %r，实得 %r" % (text, k, v, got[k]))
    if bad:
        print("selftest FAIL：%d 项不符" % bad)
        return 1
    print("selftest OK：%d 条样本、%d 项指标全对" % (len(SELFTEST), sum(len(w) for _, _, w in SELFTEST)))
    return selftest_params()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="标签，比如 before / after；决定会话 id 和输出文件名")
    ap.add_argument("--out-dir", default=os.path.join("tests", "output"))
    ap.add_argument("--timeout", type=int, default=400)
    ap.add_argument("--multi", action="store_true", help="多轮：同一个会话连着说 6 句，逐条量")
    ap.add_argument("--prompts", help="多轮脚本：(JSON) 句子数组，或 {\"lines\":[{text, asks_zhuren}]}；给了就按多轮跑")
    ap.add_argument("--session-suffix", default="",
                    help="插在会话 id 中间；同一个 tag 跑两遍时用它拿两个独立样本")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.tag:
        ap.error("要么给 --tag，要么用 --selftest")
    prompts = None
    if a.prompts:
        prompts = load_prompts(a.prompts)
        if not a.multi:
            print("（--prompts 给了，按多轮跑）", flush=True)
    return run(a.tag, a.out_dir, a.timeout, a.multi or bool(prompts), prompts, a.prompts or "", a.session_suffix)


if __name__ == "__main__":
    raise SystemExit(main())
