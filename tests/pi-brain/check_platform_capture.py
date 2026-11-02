#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台通知（TG / Discord）进长期记忆时只留「（渠道）谁：原话」。

2026-11-02：`CAPTURE_FILTERS` 里原来有一条 `user:d|^【平台消息】|`，把桥叫醒她的整条通知丢掉，
于是他在 TG/Discord 说的话永远进不了记忆。现在改成一组 s 规则剥壳，这个测试守住它。

做法：**不手写假通知**——用 `chat_bridge.dm_notice()` / `group_notice()` 真生成（含 R≤3、R=0 的额度句、
带上下文、多个渠道），再送进**扩展真实的过滤引擎** `shared/input-filters.mjs` 的 `applyInputFilters`
（用 node 跑，WSL 里的生产副本），断言剥完只剩消息行。谁改通知文案，这里就会红。

前置：WSL 里能跑 node，且 `/root/albion-pi/agent/openviking/shared/input-filters.mjs` 存在
（可用 `OPENVIKING_INPUT_FILTERS` 指定别的路径）。缺前置退出码 2，不假装通过。

跑：python tests/pi-brain/check_platform_capture.py    （Windows 或 WSL 都行）
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "brain", "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import chat_bridge                      # noqa: E402
import server                           # noqa: E402

INPUT_FILTERS = os.environ.get("OPENVIKING_INPUT_FILTERS") or \
    "/root/albion-pi/agent/openviking/shared/input-filters.mjs"

HELPER = r"""
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
const mod = await import(pathToFileURL(process.argv[2]).href);
const job = JSON.parse(readFileSync(0, "utf-8"));
const compiled = mod.compileInputFilters(job.rules);
if (compiled.errors.length) {
  console.log(JSON.stringify({ error: compiled.errors }));
  process.exit(0);
}
const out = [];
for (const text of job.texts) {
  out.push(mod.applyInputFilters(text, compiled.rules, { role: "user" }));
}
console.log(JSON.stringify({ results: out }));
"""

# 说明性的字样：剥完一个都不许留
FORBIDDEN = ("【平台消息】", "写给自己看的", "可畏也在群里", "刚才群里还在聊的", "这些你可以回",
             "群里你们俩这一轮", "这一轮群里说得够多", "send_message", "已经说了", "还能再说",
             "Discord 群「", "上给我发了消息")


def _run_wsl(cmd):
    return subprocess.run(["wsl", "-e", "bash", "-lc", cmd], capture_output=True, timeout=120)


def _write_helper():
    """把 helper 写进 WSL 的 /tmp（base64 传，避免编码/引号问题）。"""
    b64 = base64.b64encode(HELPER.encode("utf-8")).decode("ascii")
    path = "/tmp/albion_apply_filters_%d.mjs" % os.getpid()
    r = _run_wsl("printf %%s '%s' | base64 -d > %s" % (b64, path))
    if r.returncode != 0:
        raise RuntimeError("写 helper 失败：%s" % r.stderr.decode("utf-8", "replace")[:200])
    return path


def apply_with_real_engine(texts, rules):
    helper = _write_helper()
    payload = json.dumps({"rules": list(rules), "texts": list(texts)}, ensure_ascii=False)
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    cmd = "printf %%s '%s' | base64 -d | node %s %s" % (b64, helper, INPUT_FILTERS)
    r = _run_wsl(cmd)
    if r.returncode != 0:
        raise RuntimeError("node 跑过滤引擎失败：%s" % r.stderr.decode("utf-8", "replace")[:400])
    data = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    if data.get("error"):
        raise RuntimeError("规则编译失败：%s" % data["error"])
    return [item["text"] for item in data["results"]]


def notices():
    """真实通知：两个私信渠道、群（带上下文 + R=3）、群（R=0）、群（不提醒）。"""
    now = 1762000000.0                      # 固定时间，样例稳定
    items = [
        {"id": 12, "channel": "telegram", "at": now, "text": "在忙吗？"},
        {"id": 13, "channel": "telegram", "at": now + 60, "text": "晚饭吃了吗"},
    ]
    ctx = [{"id": 9, "at": now - 120, "author": "机器人甲", "text": "我先睡了"},
           {"id": 10, "at": now - 60, "author": "Formidable", "text": "晚安"}]
    group_items = [{"id": 14, "at": now, "author": "OwnerNick", "text": "你们俩聊什么呢"},
                   {"id": 15, "at": now + 30, "author": "Formidable", "text": "随便聊聊"}]
    return [
        ("TG 私信", chat_bridge.dm_notice("telegram", items[:1])),
        ("Discord 私信", chat_bridge.dm_notice("discord", items[1:])),
        ("群 + 上下文 + R=3",
         chat_bridge.group_notice("常规", group_items, ctx, {"remaining": 3, "used": 9})),
        ("群 R=0", chat_bridge.group_notice("常规", group_items, None, {"remaining": 0, "used": 12})),
        ("群 额度充足", chat_bridge.group_notice("常规", group_items, None, {"remaining": 8, "used": 4})),
    ]


def main():
    rules = list(server.CAPTURE_FILTERS)
    cases = notices()
    texts = [text for _, text in cases]
    outs = apply_with_real_engine(texts, rules)

    problems = []
    for (name, _), out in zip(cases, outs):
        for bad in FORBIDDEN:
            if bad in out:
                problems.append("%s：剥完还留着 %r" % (name, bad))
        if not out.strip():
            problems.append("%s：剥完什么都没有了（消息行被一起删掉）" % name)
        for line in out.splitlines():
            if not line.strip():
                continue
            if not line.startswith("（"):
                problems.append("%s：留下的行不是消息行：%r" % (name, line[:60]))

    # 具体形状（谁说了什么）
    tg = outs[0]
    if "（Telegram）指挥官：在忙吗？" not in tg:
        problems.append("TG 私信没变成「（Telegram）指挥官：原话」：%r" % tg)
    dm = outs[1]
    if "（Discord 私信）指挥官：晚饭吃了吗" not in dm:
        problems.append("Discord 私信没变成「（Discord 私信）指挥官：原话」：%r" % dm)
    grp = outs[2]
    for want in ("（Discord 群）机器人甲：我先睡了", "（Discord 群）Formidable：晚安",
                 "（Discord 群）OwnerNick：你们俩聊什么呢", "（Discord 群）Formidable：随便聊聊"):
        if want not in grp:
            problems.append("群通知缺上下文/消息行：%r" % want)
    zero = outs[3]
    if "（Discord 群）OwnerNick：你们俩聊什么呢" not in zero:
        problems.append("R=0 那条的消息行没了：%r" % zero)

    # 与 tools/reextract_albion_memory.py 的 Python 引擎逐条对拍（同一份规则、同义实现）
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import reextract_albion_memory as rex    # noqa: E402
    compiled = [rex.parse_rule(r) for r in rules]
    py_out = [rex.apply_rules(t, compiled, "user") or "" for t in texts]
    for (name, _), node_text, py_text in zip(cases, outs, py_out):
        if node_text.strip() != py_text.strip():
            problems.append("%s：node 与 reextract 的 Python 引擎结果不一致" % name)

    # 醒来提示/别的会话不受影响：应当保留原样（这里只验平台通知这一路）
    if not outs[4].startswith("（"):
        problems.append("群通知（额度充足）也应当只剩消息行")

    if problems:
        print("平台通知捕获过滤没过：")
        for p in problems:
            print(" -", p)
        print("\n样例：")
        for (name, _), out in zip(cases, outs):
            print("== %s ==\n%s\n" % (name, out))
        return 1
    print("平台通知捕获过滤 OK（%d 条真实通知，node 引擎 + reextract 对拍）" % len(cases))
    for (name, _), out in zip(cases, outs):
        print("-- %s --" % name)
        for line in out.splitlines():
            print("   " + line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError as exc:
        print("缺少前置（node / 扩展文件）：%s" % exc)
        sys.exit(2)
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(2)
