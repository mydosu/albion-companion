#!/usr/bin/env python3
"""按本轮改动要达成的口径判定探针结果（输入是 run_turn.py 写出的 turns.jsonl）。

判定分两层，分开报，不混成一个数字：

硬性（FAIL 会让脚本退出码非 0）——用户这一轮明确要的东西：
  H1 每条发言都以**一个**合法 ACT 情绪标记起头
  H2 不说"AI 自述"、不吐代码块围栏（人格没丢）
  H3 口吃一条回复最多一处
  H4 闲聊里陪伴/等候姿态最多出现一轮（整理器每 8 条回复最多放过一次；判定用 bin/voice_tidy.ts，
     即整理器 albion-guard.ts 那一份口径，+ 显式等候腔）
  H5 看图那几轮走的是 `read` / `look_image`，工具没报错，返回里真有图片块，
     而且她没有自己去 ffmpeg/ImageMagick/Python 缩图
  H6 干活那轮：有写盘动作、磁盘内容真的变了、正文把改后的内容贴出来（指令要求"贴给我看"）
  H7 让她自己加工具时：新扩展落在 agent/extensions/、她主动跑了 check_extensions、且整批自检通过

度量（只报数，不判过关）——模板命中明细、整理器会对这条做什么、时长、长度、口吃总数。

用法：python3 check_speech.py <turns.jsonl> [--brain /root/albion-pi] [--ext-findings …] [--work-fact …]
"""
import argparse
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 口吃：同字重复（我我 / 指、指挥官 / 阿、阿尔比恩）
STUTTER = re.compile(r"([\u4e00-\u9fff])、(?=\1)|(我|你|您|不|那|这|指)\2(?=[\u4e00-\u9fff])")
AI_SPEAK = ("作为 AI", "作为AI", "我是AI", "我是 AI", "AI 助手", "AI助手", "As an AI", "```")
# 心声被搬到正文：括号里只剩个标签、要说的话跟在括号外 —— 壳只洗括号里那段，外面的话 TTS 会念出来
ASIDE_LEAK = re.compile(r"（(?:心声|内心|心里话|心想)[^）]{0,4}）")
# 近义陪伴/等候措辞（只报数，人工看）：整理器词表盖不到、但意思还是"我在这儿等着陪着"
NEAR_POSTURE = ("安静坐", "不会去管", "一直都在", "陪着", "静静坐", "不打扰", "等您回来", "等你回来", "一直等着", "守着")
# 用户点名过的等候/陪伴腔（整理器词表之外的显式例子，来自给她的约定与验收员口径）
EXPLICIT_TEMPLATES = (
    "您忙您的", "你忙你的", "我不出声", "随时都能接话", "随时可以接话",
    "静候", "随时吩咐", "一直在这儿", "一直在这里", "在这儿陪", "在这里陪",
    "守着", "等着您的吩咐", "等着你的吩咐",
)
CONVERT_CMD = re.compile(r"ffmpeg|imagemagick|\bconvert\b|\bmagick\b|PIL|pillow|pillow-simd|sharp\(|resize", re.I)
# 写盘动作：edit/write 之外还有 bash 里那几种（与 albion-loopguard 的口径一致）
WRITE_CMD = re.compile(r"writeFileSync|sed -i|tee |> ?/|cat >")


def run_tidy(brain, payloads):
    """逐条问整理器（bin/voice_tidy.ts，规则只此一份）：姿态命中 + 不放过姿态时会交出去的样子。"""
    if not payloads:
        return []
    out = subprocess.run(["node", os.path.join(brain, "bin", "voice_tidy.ts")], check=True, capture_output=True,
                         input="\n".join(json.dumps(p, ensure_ascii=False) for p in payloads), text=True, encoding="utf-8")
    return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]


def act_ok(reply):
    if not reply:
        return False, 0
    m = re.match(r'^<\|ACT:(\{[^\n]*?\})\|>', reply)
    if not m:
        return False, reply.count("<|ACT:")
    try:
        act = json.loads(m.group(1))
    except ValueError:
        return False, reply.count("<|ACT:")
    return isinstance(act, dict) and act.get("emotion") in {"happy", "sad", "angry", "think", "surprised", "awkward", "question", "curious", "neutral"}, reply.count("<|ACT:")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("turns")
    ap.add_argument("--brain", default="/root/albion-pi", help="脑的目录（用它的 bin/voice_tidy.ts 判姿态）")
    ap.add_argument("--ext-result", default="", help="扩展沙箱自检结果 JSON")
    ap.add_argument("--ext-findings", default="", help="扩展沙箱事实 JSON")
    ap.add_argument("--work-fact", default="", help="干活轮的文件事实 JSON（probe_flow.sh 落盘）")
    a = ap.parse_args()

    turns = [json.loads(l) for l in open(a.turns, encoding="utf-8") if l.strip()]
    work_fact = json.load(open(a.work_fact, encoding="utf-8")) if a.work_fact and os.path.exists(a.work_fact) else {}
    sources = [(t.get("injected") or {}).get("source") or t.get("prompt") or "" for t in turns]
    tidied = run_tidy(a.brain, [{"text": t.get("reply") or "", "source": s} for t, s in zip(turns, sources)])

    hard, metrics, rows = [], [], []
    for t, verdict in zip(turns, tidied):
        reply = t.get("reply") or ""
        ok_act, act_count = act_ok(reply)
        stutters = [m.group(0) for m in STUTTER.finditer(reply)]
        ai = [s for s in AI_SPEAK if s in reply]
        postures = verdict["posture"]
        explicit = [s for s in EXPLICIT_TEMPLATES if s in reply]
        issues = verdict["tidy"]["actions"] if reply else ["empty"]
        row = {
            "id": t["id"], "kind": t["kind"], "seconds": t.get("seconds"),
            "rc": t.get("rc"), "reply": reply,
            "act_ok": ok_act, "act_count": act_count,
            "stutters": stutters, "ai_speak": ai,
            "postures": postures, "explicit_templates": explicit,
            "guard_issues": issues,
            "tool_calls": [(c["name"], c["secs"], c["is_error"], c["images"]) for c in t.get("tool_calls") or []],
            "wrote_file": t.get("wrote_file"),
        }
        rows.append(row)

        if not ok_act or act_count != 1:
            hard.append("%s 缺 ACT 标记或标记不唯一（count=%d）" % (t["id"], act_count))
        if ai:
            # 干活轮里用代码块围栏把文件内容括起来只是风格问题（人工看），闲聊轮才算"通用助手口吻"
            if t["kind"] in ("chat", "image", "image_free"):
                hard.append("%s 出现 AI 自述/代码块：%s" % (t["id"], ai))
            else:
                row["work_style"] = ai
        if len(stutters) > 1:
            hard.append("%s 口吃 %d 处（上限 1）：%s" % (t["id"], len(stutters), stutters))
        # 闲聊里的陪伴/等候姿态：整理器每 8 条回复最多放过一次，所以一次探针里最多一轮出现（循环后统一判）
        # loopguard 的"补一句"是直接加进给指挥官看的那条消息里的：闲聊轮里出现就是误伤
        noted = "（补一句：" in reply
        row["annotated"] = noted
        if noted and t["kind"] == "chat":
            hard.append("%s 闲聊轮被 loopguard 补注（误伤）：%s" % (t["id"], reply[reply.find("（补一句："):][:70]))
        leak = ASIDE_LEAK.search(reply)
        if leak:
            hard.append("%s 心声搬到正文了（%s……后面的话会被 TTS 念出来）：%s" % (
                t["id"], leak.group(0), reply[leak.end():][:40]))
        near = [w for w in NEAR_POSTURE if w in reply]
        row["near_posture"] = near

        calls = t.get("tool_calls") or []
        if t["kind"] in ("image", "image_free"):
            # 提示词点名了工具就照点名验；没点名（image_free）时 read / look_image 都算数，
            # 只要求"真看到了画面"：read 要带回图片块、look_image 要带回描述。
            if "look_image" in t["prompt"]:
                want = "look_image"
            elif "read" in t["prompt"]:
                want = "read"
            else:
                want = "look_image" if any(c["name"] == "look_image" for c in calls) else "read"
            used = [c for c in calls if c["name"] == want]
            if not used:
                hard.append("%s 没用 %s（实际：%s）" % (t["id"], want, [c["name"] for c in calls]))
            elif used[0]["is_error"]:
                hard.append("%s 的 %s 报错" % (t["id"], want))
            elif want == "read" and used[0]["images"] < 1:
                hard.append("%s 的 read 返回里没有图片块" % t["id"])
            elif want == "look_image" and used[0]["result_chars"] < 100:
                # look_image 按设计只回文字（描述），不是图片块
                hard.append("%s 的 look_image 没带回画面描述（result %d 字符）" % (t["id"], used[0]["result_chars"]))
            bad = [c for c in calls if c["name"] == "bash" and CONVERT_CMD.search(c["args"])]
            if bad:
                hard.append("%s 自己去转码/缩图了：%s" % (t["id"], bad[0]["args"][:120]))
        if t["kind"] == "work" and "note.txt" in t["prompt"]:
            wrote = any(
                (c["name"] in ("edit", "write") or (c["name"] == "bash" and WRITE_CMD.search(c["args"])))
                and not c["is_error"]
                for c in calls
            )
            if not wrote:
                hard.append("%s 没有任何写盘动作（工具：%s）" % (t["id"], [c["name"] for c in calls]))
            if work_fact and not work_fact.get("disk_changed"):
                hard.append("%s 磁盘上没改到（%s）" % (t["id"], work_fact.get("path")))
            # 指令是"改完把文件内容原样贴给我看"：交付要把真看到的输出带上，不能只说一句"改好了"
            if work_fact and work_fact.get("disk_changed"):
                line = work_fact.get("changed_line") or ""
                if line and line not in reply:
                    hard.append("%s 正文没贴出改后的内容（该出现：%s）" % (t["id"], line))
                # 内容是她自己贴的，还是 loopguard 兜底补出来的？（用户总能看到真内容，但口径要分清）
                without_notes = "\n".join(l for l in reply.split("\n") if "（补一句：" not in l)
                row["pasted_by_self"] = bool(line) and line in without_notes
                row["pasted_by_guard"] = bool(line) and line in reply and not row["pasted_by_self"]

    posture_chats = [r for r in rows if r["kind"] == "chat" and (r["postures"] or r["explicit_templates"])]
    if len(posture_chats) > 1:
        hard.append("闲聊里 %d 轮出现陪伴/等候模板（最多一轮）：%s" % (len(posture_chats), "; ".join(
            "%s:%s" % (r["id"], r["postures"] + r["explicit_templates"]) for r in posture_chats)))

    # ── 扩展沙箱（单独一轮，结果由 probe_flow.sh 落盘）────────────────
    ext_lines = []
    if a.ext_findings and os.path.exists(a.ext_findings):
        f = json.load(open(a.ext_findings, encoding="utf-8"))
        if not f.get("new_extension"):
            hard.append("扩展测试：agent/extensions/ 下没有新文件")
        if not f.get("ran_check_extensions"):
            hard.append("扩展测试：她没跑 check_extensions（工具调用里没有）")
        if not f.get("sandbox_check_ok"):
            hard.append("扩展测试：整批自检没过：%s" % (f.get("sandbox_check_out") or "")[:200])
        if f.get("production_extensions_changed"):
            hard.append("扩展测试：生产 agent/extensions/ 被改动了")
        ext_lines.append("扩展沙箱：新文件=%s 她跑了自检=%s 自检通过=%s 生产未变=%s" % (
            f.get("new_extension"), f.get("ran_check_extensions"), f.get("sandbox_check_ok"),
            not f.get("production_extensions_changed")))

    # ── 报告 ─────────────────────────────────────────────────────────
    print("── 逐轮事实 ─────────────────────────────────────────────")
    for r in rows:
        print("[%s] %s %.1fs act=%s 口吃=%d 姿态=%s 显式等候腔=%s 补注=%s 长度=%d" % (
            r["id"], r["kind"], r["seconds"] or -1, r["act_ok"], len(r["stutters"]),
            r["postures"] or "-", r["explicit_templates"] or "-",
            "有" if r.get("annotated") else "-", len(r["reply"] or "")))
        if r["tool_calls"]:
            print("      工具：%s" % ", ".join("%s(%ss%s%s)" % (
                n, s, ", 图%d" % im if im else "", ", 报错" if e else "") for n, s, e, im in r["tool_calls"]))
        print("      她说：%s" % (r["reply"] or "").replace("\n", " ⏎ ")[:220])
        if r["guard_issues"]:
            print("      整理器会做：%s" % r["guard_issues"])
        if "pasted_by_self" in r:
            print("      贴内容：%s" % ("她自己贴的" if r["pasted_by_self"]
                                     else "靠兜底注补出来的" if r.get("pasted_by_guard") else "没贴"))
        if r.get("work_style"):
            print("      （风格）干活轮里用了：%s" % r["work_style"])

    print()
    print("── 度量（不判过关）─────────────────────────────────────")
    template_total = sum(len(r["postures"]) + len(r["explicit_templates"]) for r in rows)
    print("陪伴/等候姿态命中合计：%d 处（整理器词表 %d + 显式例子 %d）" % (
        template_total,
        sum(len(r["postures"]) for r in rows),
        sum(len(r["explicit_templates"]) for r in rows)))
    for r in rows:
        if r["postures"] or r["explicit_templates"]:
            print("   - %s：%s" % (r["id"], (r["postures"] or []) + (r["explicit_templates"] or [])))
    chat = [r for r in rows if r["kind"] == "chat"]
    work = [r for r in rows if r["kind"] == "work"]
    image = [r for r in rows if r["kind"] in ("image", "image_free")]
    noted = [r["id"] for r in rows if r.get("annotated")]
    print("loopguard 补注：%d 轮（%s）" % (len(noted), ", ".join(noted) or "无"))
    if any("pasted_by_self" in r for r in rows):
        self_pasted = [r["id"] for r in rows if r.get("pasted_by_self")]
        by_guard = [r["id"] for r in rows if r.get("pasted_by_guard")]
        print("干活轮贴内容：她自己贴 %d 轮（%s）／靠兜底注 %d 轮（%s）" % (
            len(self_pasted), ", ".join(self_pasted) or "无",
            len(by_guard), ", ".join(by_guard) or "无"))
        style = [r["id"] for r in rows if r.get("work_style")]
        if style:
            print("（风格）干活轮用代码块围栏：%d 轮（%s）" % (len(style), ", ".join(style)))
    near_rows = [(r["id"], r["near_posture"]) for r in rows if r.get("near_posture")]
    print("近义陪伴/等候措辞（词表没盖到，人工看）：%d 轮 %s" % (
        len(near_rows), "; ".join("%s:%s" % (i, w) for i, w in near_rows) or "无"))
    def avg(rs):
        return round(sum(r["seconds"] for r in rs) / len(rs), 1) if rs else -1
    print("时长：闲聊均 %.1fs／干活均 %.1fs／看图均 %.1fs" % (avg(chat), avg(work), avg(image)))
    print("长度：闲聊均 %d 字／干活均 %d 字" % (
        sum(len(r["reply"] or "") for r in chat) // max(len(chat), 1),
        sum(len(r["reply"] or "") for r in work) // max(len(work), 1)))
    print("口吃合计：%d 处（%s）" % (
        sum(len(r["stutters"]) for r in rows),
        ", ".join(r["id"] + ":" + "".join(r["stutters"]) for r in rows if r["stutters"]) or "无"))
    for line in ext_lines:
        print(line)

    print()
    if hard:
        print("── 硬性判定：FAIL（%d 条）─────────────────────────────" % len(hard))
        for h in hard:
            print("  ✗ " + h)
        return 1
    print("── 硬性判定：PASS（%d 轮全部满足 H1–H7）───────────────" % len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
