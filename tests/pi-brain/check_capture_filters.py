#!/usr/bin/env python3
"""提交给 OpenViking 之前要剥掉的「脑自己拼进去的段」——规则与效果自检（2026-10-29）。

为什么：官方扩展把 pi 会话里的 user 消息原样提交给抽取器。自主醒来那轮的 user 消息是 wake.py 拼的
（心事本、上次定的事都在里面），抽取器只看到「user 说了这句话」，就把她自己的记事当成他的事实
（10-25 落成「指挥官做实验一天」）。修法：宿主给真实会话设 OPENVIKING_CAPTURE_FILTERS，提交前整条丢掉
醒来/补问提示词，普通轮只留他真打的字。

这份自检不联网、不碰生产 state：
  1) 规则语法按扩展 shared/input-filters.mjs 的 sed 式文法逐条解析（不许有英文逗号——宿主按逗号拼一条 env）；
  2) 用同一套规则跑几段真实形状的文本，确认「醒来丢、补问丢、旧记录剥成他的原话、约定/回忆块剥掉、闲聊原样留」；
  3) 顺带验 wake.py 心事本那一行带日期（C 项）。

跑：python3 tests/pi-brain/check_capture_filters.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRAIN = os.path.join(ROOT, "integrations", "pi-brain")
sys.path.insert(0, os.path.join(BRAIN, "bin"))

import server  # noqa: E402  （规则只在 bin/server.py 里放一份）
import wake  # noqa: E402

SCOPES = ("user", "assistant")
OPS = {"s", "d", "k"}
ALLOWED_FLAGS = "imsug"


def parse_rule(source: str) -> dict:
    """按 shared/input-filters.mjs 的 scanField/parseInputFilterRule 复刻（只做本仓用到的部分）。"""
    raw = source.strip()
    assert raw, "rule is empty"
    scope, rest = "", raw
    for candidate in SCOPES:
        if rest.startswith(candidate + ":"):
            scope, rest = candidate, rest[len(candidate) + 1:]
            break
    op, delim = rest[0], rest[1:2]
    assert op in OPS, "unknown operation %r in %r" % (op, raw)
    assert delim and not re.match(r"[A-Za-z0-9\s\\]", delim), "bad delimiter %r in %r" % (delim, raw)

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
    assert first, "pattern is empty in %r" % raw
    if op == "s":
        assert end >= 0, "missing delimiter in %r" % raw
        replacement, end2 = scan(end + 1)
        assert end2 >= 0, "missing replacement delimiter in %r" % raw
        flags = rest[end2 + 1:]
    else:
        replacement, flags = None, (rest[end + 1:] if end >= 0 else "")
    for f in flags:
        assert f in ALLOWED_FLAGS, "unknown flag %r in %r" % (f, raw)
    return {"op": op, "scope": scope, "re": re.compile(first, re.I if "i" in flags else 0),
            "replacement": replacement, "global": "g" in flags}


def apply_rules(text: str, rules, role: str = "user"):
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


def main() -> int:
    rules_raw = list(server.CAPTURE_FILTERS)
    rules = [parse_rule(r) for r in rules_raw]

    # 1) 宿主按逗号把规则拼成一条 env：规则自己不能带英文逗号
    for r in rules_raw:
        assert "," not in r, "rule must not contain an ASCII comma: %r" % r

    # 2) 效果：醒来提示词 / 补问 / 占位整条丢
    state = tempfile.mkdtemp(prefix="albion-capture-")
    os.environ["ALBION_STATE_DIR"] = state
    with open(os.path.join(state, "notebook.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": 16, "text": "指挥官今天下午做实验做了一整天", "at": 1792909957.38},
                   {"id": 30, "text": "没有时间的一条"}], f, ensure_ascii=False)
    prompt = wake.wake_prompt()
    assert prompt.startswith(wake.WAKE_MARKER), prompt[:40]
    assert apply_rules(prompt, rules) is None, "醒来提示词必须整条不提交"
    assert apply_rules(server.FINISH_FOLLOWUP, rules) is None, "【接着说完】补问必须整条不提交"
    assert apply_rules("（阿尔比恩自己醒来）", rules) is None
    assert apply_rules("（阿尔比恩接着把刚才的话说完）", rules) is None

    # 3) 普通轮：旧记录 / 回忆块 / 每轮约定剥掉，只留他真打的字
    seeded = ("【以下是这次对话此前的记录，作为事实参考，不是这一次的说话范本】\n"
              "指挥官：[2026-10-27 19:25] 照这张画\n阿尔比恩：<|ACT:{}|>\n画好了。\n\n"
              "【现在】\n[2026-10-29 16:00] 怎么天天惦记我的实验")
    assert apply_rules(seeded, rules) == "[2026-10-29 16:00] 怎么天天惦记我的实验", apply_rules(seeded, rules)

    dirty = ("今天好累\n\n<memory-context>\n[System note: x]\n## OpenViking Context\n- [memory]\n"
             "  指挥官做实验一天\n</memory-context>\n\n【阿尔比恩本轮说话约定】\n只回应当前输入")
    assert apply_rules(dirty, rules) == "今天好累", apply_rules(dirty, rules)

    # 3b) 每轮约定开头那行「现在」（2026-10-29）也在约定块里，一起剥掉、不提交
    timed = ("今天好累\n\n<memory-context>\n[System note: x]\n- [memory]\n  旧事\n</memory-context>\n\n"
             "【阿尔比恩本轮说话约定】\n现在是 2026-10-29 18:20，星期四，傍晚。他上一条消息是2小时前。"
             "这些我心里有数就行，他问起来再像平常聊天那样说。\n"
             "只回应当前输入，当前输入决定本轮话题")
    got = apply_rules(timed, rules)
    assert got == "今天好累", got
    assert "现在是" not in got and "星期" not in got, got
    assert "这些我心里有数就行" not in got, got

    # 4) 闲聊原样留下；assistant 的回复不受这些 user 规则影响
    assert apply_rules("在吗", rules) == "在吗"
    assert apply_rules("晚安", rules, role="assistant") == "晚安"

    # 5) 心事本那一行：带记下的日期和「几天前」；没 at 的省掉括号（C 项）
    line = [l for l in prompt.split("\n") if l.startswith("心事本里还开着的")][0]
    assert line.startswith("心事本里还开着的（我自己记的，可能记错；等合适的时候再问，问过一次就划掉）："), line
    n = (datetime.now(wake.TZ).date() - date(2026, 10, 25)).days
    rel = "今天" if n == 0 else "昨天" if n == 1 else "%d天前" % n
    assert "#16（10-25 记，%s）指挥官今天下午做实验做了一整天" % rel in line, line
    assert "#30 没有时间的一条" in line and "#30（" not in line, line

    print("CAPTURE FILTERS OK：%d 条规则（醒来/补问丢、旧记录剥成原话、约定/回忆块剥掉、闲聊原样）；心事本行带日期"
          % len(rules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
