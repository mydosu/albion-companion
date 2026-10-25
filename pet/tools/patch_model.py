#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
阿尔比恩 Live2D 模型补丁：让动画"丝滑"

改两处（都在模型 zip 里的 JSON，不动美术资源）：
1. model3.json 里加一个 **大写 `Idle` 动作组** —— pixi-live2d-display 把空闲组名
   写死成 "Idle"（dist/cubism4.js: `this.groups = { idle: "Idle" }`），
   我们的模型只有小写 `idle` → SDK 找不到空闲动作，永远只循环同一个。
   加进 idle/idle1..idle5 六个变体后，SDK 会**随机**挑一个播 = 手游那种"看不出重复"。
2. 每个动作的 Meta：`FadeInTime`/`FadeOutTime` 从 0.0 改成 1.0 左右 —— 原来全是 0，
   所以每次切换都是**硬切**（这就是"首尾不在同一位置、像两个片段"的直接原因）。
   同时把 idle 的 `Loop` 关掉：内部循环会在接缝处硬跳，改由 SDK 交叉淡化衔接。

用法：python patch_model.py [zip路径]   原地打补丁（可重复运行，幂等）
"""
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ZIP = Path(sys.argv[1] if len(sys.argv) > 1
           else r"D:/AI/Wife/shell/packages/stage-ui/src/assets/live2d/models/albion.zip")

# 空闲变体（模型自带的 6 组 idle）
IDLE_GROUP = ["idle"]   # 只用原版 idle：它本身就是作者做好的无缝循环（手游就这么播）
# 淡入淡出：空闲之间慢一点（更柔），触摸/事件快一点（更跟手）
FADE_IDLE = (0.3, 0.3)   # 只用于从触摸动作回到空闲时的过渡；内部循环不涉及淡入淡出
FADE_TOUCH = (0.4, 0.5)
IDLE_LIKE = re.compile(r"^(touch_)?idle\d*$")


def patch_motion_json(raw: bytes, name: str) -> bytes:
    d = json.loads(raw.decode("utf-8"))
    meta = d.setdefault("Meta", {})
    fin, fout = FADE_IDLE if IDLE_LIKE.match(name) else FADE_TOUCH
    changed = []
    for k, v in (("FadeInTime", fin), ("FadeOutTime", fout)):
        if abs(float(meta.get(k, 0.0)) - v) > 1e-6:
            meta[k] = v
            changed.append(k)
    # 原版 Loop:true —— 作者写好的无缝循环（手游就这么播）。之前我把它关掉改成
    # "多动作轮播 + 交叉淡化"，反而出现倒放感和道具乱入，故显式改回 true。
    if IDLE_LIKE.match(name) and meta.get("Loop") is not True:
        meta["Loop"] = True
        changed.append("Loop=True")

    # 非空闲动作一律改成**一次性**（Loop=False）。
    # 模型里 35 个动作全被标成 Loop=true → 播完永远不会"回到正常"，
    # 带道具的动作（相框/事件）尤其明显（2026-09-15 指挥官：切到相框那个就回不来了）。
    # 手游里触摸/事件动作都是一次性的，播完由状态机切回待机；pixi-live2d-display 同理——
    # 一次性动作播完会自动回到 Idle 组，并带上 Meta 里的淡出时间，衔接就顺了。
    if not IDLE_LIKE.match(name) and meta.get("Loop") is not False:
        meta["Loop"] = False
        changed.append("Loop=False")

    d["Meta"] = meta
    return json.dumps(d, ensure_ascii=False).encode("utf-8"), changed


def main() -> int:
    if not ZIP.exists():
        print(f"[x] 找不到模型: {ZIP}")
        return 1

    zin = zipfile.ZipFile(ZIP)
    items = zin.infolist()
    patched, added_group, motion_names = [], None, []

    # --- 1) 改每个 motion3.json ---
    new_data = {}
    for it in items:
        data = zin.read(it.filename)
        if it.filename.endswith(".motion3.json"):
            name = Path(it.filename).stem.replace(".motion3", "")
            motion_names.append(name)
            data, changed = patch_motion_json(data, name)
            if changed:
                patched.append(f"{name}:{'/'.join(changed)}")
        new_data[it.filename] = data

    # --- 2) model3.json 加 Idle 组 ---
    for fn in list(new_data):
        if not fn.endswith(".model3.json"):
            continue
        d = json.loads(new_data[fn].decode("utf-8"))
        motions = d.setdefault("FileReferences", {}).setdefault("Motions", {})
        want = [{"File": f"motions/{n}.motion3.json"} for n in IDLE_GROUP if n in motion_names]
        if motions.get("Idle") != want:            # 已存在也要覆盖（重跑脚本时收敛到想要的配置）
            motions["Idle"] = want
            added_group = f"{fn} → Idle 组 {[Path(x['File']).stem.replace('.motion3','') for x in want]}"
        new_data[fn] = json.dumps(d, ensure_ascii=False).encode("utf-8")

    # --- 3) 重写 zip（保留原顺序）---
    tmp = ZIP.with_suffix(".patched.zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in items:
            zout.writestr(it, new_data[it.filename])
    shutil.move(str(tmp), str(ZIP))

    print(f"[ok] 模型: {ZIP.name}  动作数: {len(motion_names)}")
    print(f"[ok] {added_group or 'Idle 组已存在（跳过）'}")
    print(f"[ok] 改了淡入淡出的动作: {len(patched)} 个")
    for line in patched[:6]:
        print("     ", line)
    if len(patched) > 6:
        print(f"      … 其余 {len(patched) - 6} 个同类")

    # 自检：读回来确认改动生效
    with zipfile.ZipFile(ZIP) as z:
        m3 = json.loads(z.read([n for n in z.namelist() if n.endswith(".model3.json")][0]).decode("utf-8"))
        idle_ids = [Path(m["File"]).stem.replace(".motion3", "") for m in m3["FileReferences"]["Motions"]["Idle"]]
        assert idle_ids, "Idle 组是空的！"
        one = json.loads(z.read("aerbien_3/motions/idle.motion3.json").decode("utf-8"))["Meta"]
        assert one["FadeInTime"] > 0 and one["Loop"] is True, f"idle 的 Meta 没改成功: {one}"
        touch = json.loads(z.read("aerbien_3/motions/touch_head.motion3.json").decode("utf-8"))["Meta"]
        assert touch["Loop"] is False, f"touch_head 应该是一次性: {touch}"
        print(f"[自检 ✓] Idle 组 = {idle_ids}；idle FadeIn={one['FadeInTime']} Loop={one['Loop']}（无缝循环）"
              f"｜touch_head Loop={touch['Loop']} FadeIn={touch['FadeInTime']}（一次性，播完自动回 Idle）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
