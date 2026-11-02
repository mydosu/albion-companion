#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 albion 的历史会话「按宿主真实过滤后的纯对话」重灌进 OpenViking，重跑记忆提取。

背景（2026-11-02 复盘）：线上会话里混进了脑自己拼的段（【平台消息】/【自主醒来】/旧记录块），
抽取器判成「没有值得记的」，于是 10-30 之后 albion 的记忆一直 0 条。原始归档不能直接重灌——
那里面就是被污染的内容；这个脚本先按 `brain/bin/server.py` 的 CAPTURE_FILTERS（宿主真实规则，
一字不改地 import）把每条消息过一遍，只把「他真打的字 + 她真说的话」灌进新会话。

安全默认：**不加 `--apply` 就是 dry-run**，只打印会灌哪些消息、各多少字，不连网、不写 OV。
加了 `--apply` 才会：建 `reextract-<原会话id>` 新会话 → 逐条 POST messages → commit → 等 task 完成 →
打印 memory_diff 的 adds/updates/deletes。原会话和归档全程只读。

用法：
  python3 tools/reextract_albion_memory.py --dry-run                 # 默认区间（10-30 12:26 起）
  python3 tools/reextract_albion_memory.py --since "2026-10-30 12:26" --dry-run
  python3 tools/reextract_albion_memory.py --session pi-01a19bcb-a61f-745c-b370-e9d8db18a6c8 --dry-run
  python3 tools/reextract_albion_memory.py --session <id> --apply     # 真的重灌 + 提取（花钱/花时间）

环境：OV 端点与 key 从 /root/albion-pi/agent/.env 读（只读，不打印）；走回环，不走代理。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "brain", "bin"))

import server  # noqa: E402  （CAPTURE_FILTERS 只在这里放一份，脚本原样复用）

DATA_ROOT = os.environ.get("OPENVIKING_DATA", "/mnt/d/AI/openviking/data/viking/default")
AGENT_ENV = os.environ.get("ALBION_AGENT_ENV", "/root/albion-pi/agent/.env")
CST = timezone(timedelta(hours=8))
DEFAULT_SINCE = "2026-10-30 12:26"

SCOPES = ("user", "assistant")
OPS = {"s", "d", "k"}
ALLOWED_FLAGS = "imsug"


# ── 规则求值：与扩展 shared/input-filters.mjs 的 sed 子集同义，与 tests/pi-brain/check_capture_filters.py 一致 ──
def parse_rule(source: str) -> dict:
    raw = source.strip()
    if not raw:
        raise ValueError("empty rule")
    scope, rest = "", raw
    for candidate in SCOPES:
        if rest.startswith(candidate + ":"):
            scope, rest = candidate, rest[len(candidate) + 1:]
            break
    op, delim = rest[0], rest[1:2]
    if op not in OPS or not delim or re.match(r"[A-Za-z0-9\s\\]", delim):
        raise ValueError("bad rule %r" % raw)

    def scan(start: int):
        out, i = "", start
        while i < len(rest):
            c = rest[i]
            if c == "\\":
                nxt = rest[i + 1]
                out += (nxt if nxt == delim else "\\" + (nxt or ""))
                i += 2
                continue
            if c == delim:
                return out, i
            out += c
            i += 1
        return out, -1

    first, end = scan(2)
    if not first:
        raise ValueError("empty pattern in %r" % raw)
    replacement, flags = None, ""
    if op == "s":
        if end < 0:
            raise ValueError("missing delimiter in %r" % raw)
        replacement, end2 = scan(end + 1)
        if end2 < 0:
            raise ValueError("missing replacement delimiter in %r" % raw)
        flags = rest[end2 + 1:]
    else:
        flags = rest[end + 1:] if end >= 0 else ""
    for f in flags:
        if f not in ALLOWED_FLAGS:
            raise ValueError("unknown flag %r in %r" % (f, raw))
    return {"op": op, "scope": scope, "re": re.compile(first, re.I if "i" in flags else 0),
            "replacement": replacement, "global": "g" in flags}


def apply_rules(text: str, rules, role: str = "user"):
    """返回过滤后的文本；None = 整条丢弃。"""
    current = text
    for rule in rules:
        if rule["scope"] and rule["scope"] != role:
            continue
        if rule["op"] == "s":
            current = rule["re"].sub(rule["replacement"], current, count=0 if rule["global"] else 1)
            continue
        matched = bool(rule["re"].search(current))
        if (rule["op"] == "d" and matched) or (rule["op"] == "k" and not matched):
            return None
    return current


# ── OV 侧 ──
def env_value(path: str, key: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        return ""
    return ""


class OV:
    def __init__(self) -> None:
        self.base = (env_value(AGENT_ENV, "OPENVIKING_ENDPOINT") or "http://127.0.0.1:1933").rstrip("/")
        self.key = env_value(AGENT_ENV, "OPENVIKING_API_KEY")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 回环不走代理

    def call(self, method: str, path: str, payload=None, timeout: float = 120.0):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
            req.add_header("X-API-Key", self.key)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300]
        except Exception as e:  # noqa: BLE001
            return -1, repr(e)[:200]


# ── 会话发现与过滤 ──
def parse_ts(value: str):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(CST)
    except Exception:  # noqa: BLE001
        return None


def discover(user: str, since: datetime) -> list:
    base = os.path.join(DATA_ROOT, "user", user, "sessions")
    out = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        meta_path = os.path.join(base, name, ".meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        created = parse_ts(meta.get("created_at"))
        if not created or created < since:
            continue
        out.append({
            "id": name,
            "created": created,
            "commit_count": int(meta.get("commit_count") or 0),
            "memories": (meta.get("memories_extracted") or {}).get("total") or 0,
            "dir": os.path.join(base, name),
        })
    return out


def messages_path(session_dir: str) -> str:
    history = os.path.join(session_dir, "history")
    if os.path.isdir(history):
        archives = sorted(a for a in os.listdir(history) if a.startswith("archive_"))
        if archives:
            return os.path.join(history, archives[-1], "messages.jsonl")
    return os.path.join(session_dir, "messages.jsonl")


def filter_transcript(records: list, rules) -> tuple:
    """只留文字：丢掉 tool part、丢掉被规则整条剔除的消息、丢掉过滤后没字的消息。"""
    kept, dropped, raw_chars, kept_chars = [], 0, 0, 0
    for rec in records:
        role = rec.get("role") or ""
        text_parts = [p.get("text") or "" for p in (rec.get("parts") or []) if p.get("type") == "text"]
        text = "\n\n".join(t for t in text_parts if t)
        raw_chars += len(text)
        filtered = apply_rules(text, rules, role) if text else None
        if filtered is None or not filtered.strip():
            dropped += 1
            continue
        kept_chars += len(filtered)
        kept.append({"role": role, "created_at": rec.get("created_at"),
                     "parts": [{"type": "text", "text": filtered}]})
    return kept, dropped, raw_chars, kept_chars


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 albion 历史会话按宿主过滤规则重灌进 OV 并重跑提取")
    ap.add_argument("--user", default="albion")
    ap.add_argument("--since", default=DEFAULT_SINCE, help='北京时间 "YYYY-MM-DD HH:MM"')
    ap.add_argument("--session", action="append", default=[], help="只处理这些会话 id（可重复）")
    ap.add_argument("--apply", action="store_true", help="真的写 OV（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为，保留给脚本可读性）")
    ap.add_argument("--poll-seconds", type=float, default=300.0)
    args = ap.parse_args(argv)

    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    rules = [parse_rule(r) for r in server.CAPTURE_FILTERS]

    sessions = discover(args.user, since)
    if args.session:
        wanted = set(args.session)
        sessions = [s for s in sessions if s["id"] in wanted]
        missing = wanted - {s["id"] for s in sessions}
        for m in sorted(missing):
            print("!! 未找到会话（或早于 --since）：", m)
    if not sessions:
        print("没有要处理的会话")
        return 0

    ov = OV() if args.apply else None
    totals = {"sessions": 0, "raw": 0, "kept_chars": 0, "messages": 0, "dropped": 0}
    report = []
    for s in sessions:
        path = messages_path(s["dir"])
        if not os.path.isfile(path):
            print("!! 没有消息文件：", s["id"])
            continue
        records = [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]
        kept, dropped, raw_chars, kept_chars = filter_transcript(records, rules)
        totals["sessions"] += 1
        totals["raw"] += raw_chars
        totals["kept_chars"] += kept_chars
        totals["messages"] += len(kept)
        totals["dropped"] += dropped
        line = "%-42s %-12s 消息 %2d→%-2d 丢 %2d 字 %5d→%-5d commits=%d memories=%d" % (
            s["id"], s["created"].strftime("%m-%d %H:%M"), len(records), len(kept), dropped,
            raw_chars, kept_chars, s["commit_count"], s["memories"])
        print(line)
        report.append({"session": s["id"], "created": s["created"].isoformat(), "raw_chars": raw_chars,
                       "kept_chars": kept_chars, "messages_in": len(records), "messages_out": len(kept),
                       "dropped": dropped})
        if not args.apply or not kept:
            continue

        new_id = "reextract-" + s["id"]
        for msg in kept:
            st, resp = ov.call("POST", "/api/v1/sessions/%s/messages" % new_id, msg)
            if st != 200:
                print("   !! 写入失败", st, str(resp)[:160])
                break
        else:
            st, resp = ov.call("POST", "/api/v1/sessions/%s/commit" % new_id, {"keep_recent_count": 0})
            result = (resp.get("result") if isinstance(resp, dict) else None) or {}
            task_id = result.get("task_id")
            print("   commit", st, "status=", result.get("status"), "task=", task_id)
            deadline = time.time() + args.poll_seconds
            while task_id and time.time() < deadline:
                time.sleep(5)
                st2, t = ov.call("GET", "/api/v1/tasks/%s" % task_id)
                r = (t.get("result") if isinstance(t, dict) else None) or {}
                if r.get("status") in ("completed", "failed", "error"):
                    print("   task", r.get("status"), "error=", r.get("error"))
                    break
            diff_path = os.path.join(DATA_ROOT, "user", args.user, "sessions", new_id,
                                     "history", "archive_001", "memory_diff.json")
            if os.path.isfile(diff_path):
                d = json.load(open(diff_path, encoding="utf-8"))
                ops = d.get("operations") or {}
                print("   memory_diff adds=%d updates=%d deletes=%d" % (
                    len(ops.get("adds") or []), len(ops.get("updates") or []), len(ops.get("deletes") or [])))

    mode = "apply" if args.apply else "dry-run"
    print("\n[%s] sessions=%d messages=%d dropped=%d chars %d→%d"
          % (mode, totals["sessions"], totals["messages"], totals["dropped"],
             totals["raw"], totals["kept_chars"]))
    if args.apply:
        out = os.path.join(ROOT, "_tmp", "reextract-report-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("report ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
