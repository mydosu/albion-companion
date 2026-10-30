"""Spine38 部署验收探针（生产壳 CDP 9335，leader 页）。

只走页面已有的调试出口：`window.__albionPet`（emotion / poke）、`globalThis.__albionAuthor`
（tapHitArea 是否存在）、`globalThis.__albionSpine38`（骨架，取 zui 骨 scaleX）。
不写任何用户设置，不点菜单外的按钮；只读 + 回忆情绪。

产物目录：tests/output/promo-face-20261030/deploy/
  emo_<情绪>.png                  9 个情绪，各等 1.2s 后截图
  poke_<部位>_<秒>.png             poke 后 0.5/1.5/3/5 秒
  poke_<部位>.json                气泡文字、tapHitArea、状态采样
  zui_trace.json                  换脸前后逐帧 zui.scaleX（前 0.1s ~ 后 1s）
  zui_trace_long.json             补充：后 2.5s（看被推迟的换脸有没有落在 1s 之外）
  summary.json                    汇总数字

用法：<runtime-python> tests/shell/spine38-deploy-acceptance.py
"""
import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

import websockets

# 气泡里是作者的中文台词，控制台默认 GBK 会崩，统一走 UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDP = 9335
OUT = Path(__file__).resolve().parents[2] / "tests" / "output" / "promo-face-20261030" / "deploy"
EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "awkward", "question", "think", "curious"]

# 页面里的公共取值：气泡/菜单按 Spine38.vue 里的内联样式认，不依赖 class 名。
JS_HELPERS = r"""
globalThis.__acc = {
  spine() {
    return globalThis.__albionSpine38 || globalThis.__albionModel || null
  },
  zui() {
    const m = globalThis.__albionSpine38
    if (m && m.skeleton) {
      const b = m.skeleton.bones.find(x => x.data && x.data.name === 'zui')
      return b || null
    }
    const l = globalThis.__albionModel
    if (l && l.internalModel && l.internalModel.coreModel) {
      const cm = l.internalModel.coreModel
      const idx = cm.getParameterIndex && cm.getParameterIndex('ParamMouthOpenY')
      if (idx >= 0) return { scaleX: cm.getParameterValueById('ParamMouthOpenY') }
    }
    return null
  },
  bubbles() {
    return [...document.querySelectorAll('div')]
      .filter(d => /rgba\(0,\s*0,\s*0,\s*0\.62\)/.test(d.getAttribute('style') || ''))
      .map(d => (d.textContent || '').trim())
      .filter(Boolean)
  },
  menus() {
    return [...document.querySelectorAll('div')]
      .filter(d => /rgba\(0,\s*0,\s*0,\s*0\.72\)/.test(d.getAttribute('style') || '')).length
  },
  poseTrack() {
    const m = globalThis.__albionSpine38
    if (!m) return null
    const e = m.state.getCurrent(2)
    return e ? e.animation.name : null
  },
  probe() {
    return {
      hasPet: !!window.__albionPet,
      petKeys: Object.keys(window.__albionPet || {}),
      hasAuthor: !!globalThis.__albionAuthor,
      tapHitArea: typeof (globalThis.__albionAuthor || {}).tapHitArea,
      skeletonSource: globalThis.__albionSpine38 ? '__albionSpine38' : (globalThis.__albionModel ? '__albionModel' : null),
      zui: (() => { const b = this.zui(); return b ? +b.scaleX.toFixed(5) : null })(),
      bubbles: this.bubbles(),
      menus: this.menus(),
      pose: this.poseTrack(),
    }
  },
  idle() {
    return { bubbles: this.bubbles().length, menus: this.menus(), pose: this.poseTrack() }
  },
}
true
"""


async def connect():
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=5))
    t = next(t for t in targets if "synced-leader=true#/" in t.get("url", ""))
    ws = await websockets.connect(t["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
    p = Page(ws)
    p.reader = asyncio.create_task(p._read())
    return p


class Page:
    def __init__(self, ws):
        self.ws, self.ident, self.fut = ws, 0, {}

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.fut:
                self.fut.pop(msg["id"]).set_result(msg)

    async def cmd(self, method, params=None, timeout=60):
        self.ident += 1
        i = self.ident
        self.fut[i] = asyncio.get_running_loop().create_future()
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(self.fut[i], timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg.get("result", {})

    async def ev(self, expr, timeout=90):
        r = await self.cmd("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:800])
        return r["result"].get("value")

    async def shot(self, name):
        r = await self.cmd("Page.captureScreenshot", {"format": "png"}, 30)
        (OUT / name).write_bytes(base64.b64decode(r["data"]))
        return name


IDLE_JS = "(() => { const s = globalThis.__acc.idle(); return s.bubbles === 0 && s.menus === 0 && !s.pose })()"


async def wait_idle(p, max_s=25.0, need=2):
    """连续 need 次采样都是「无气泡 + 无菜单 + 姿势轨空」即认为反应结束。"""
    ok = 0
    waited = 0.0
    while waited < max_s:
        if await p.ev(IDLE_JS):
            ok += 1
            if ok >= need:
                return True, waited
        else:
            ok = 0
        await asyncio.sleep(0.5)
        waited += 0.5
    return False, waited


TRACE_JS = r"""
(async (emotion, preMs, postMs) => {
  const b = globalThis.__acc.zui()
  if (!b) return { error: 'no zui bone' }
  const rec = []
  let running = true
  const t0 = performance.now()
  const loop = () => {
    if (!running) return
    rec.push([+((performance.now() - t0) / 1000).toFixed(4), +b.scaleX.toFixed(5)])
    requestAnimationFrame(loop)
  }
  requestAnimationFrame(loop)
  await new Promise(r => setTimeout(r, preMs))
  const swapAt = +((performance.now() - t0) / 1000).toFixed(4)
  window.__albionPet.emotion(emotion)
  await new Promise(r => setTimeout(r, postMs))
  running = false
  await new Promise(r => requestAnimationFrame(() => r()))
  return { emotion, swapAt, rec }
})
"""


def max_step(frames):
    """相邻帧（相邻 rAF 采样）最大变化量及其时刻。"""
    best = {"delta": 0.0, "from_t": None, "to_t": None, "from": None, "to": None}
    for (t1, v1), (t2, v2) in zip(frames, frames[1:]):
        d = abs(v2 - v1)
        if d > best["delta"]:
            best = {"delta": round(d, 5), "from_t": t1, "to_t": t2, "from": v1, "to": v2}
    return best


async def poke_round(p, part):
    before = await p.ev("globalThis.__acc.probe()")
    r = await p.ev("(() => { try { window.__albionPet.poke('%s'); return 'ok' } catch (e) { return 'ERR ' + e } })()" % part)
    print(f"  poke({part}) -> {r}", flush=True)
    t0 = asyncio.get_running_loop().time()
    shots, texts = [], []
    plan = [0.5, 1.5, 3.0, 5.0]
    for target in plan:
        delay = target - (asyncio.get_running_loop().time() - t0)
        if delay > 0:
            await asyncio.sleep(delay)
        name = f"poke_{part}_{target:g}.png"
        await p.shot(name)
        shots.append(name)
        b = await p.ev("globalThis.__acc.bubbles()")
        texts.append({"t": target, "bubbles": b})
        print(f"  {name} bubbles={b}", flush=True)
    settled, waited = await wait_idle(p)
    return {
        "part": part,
        "before": before,
        "poke_return": r,
        "shots": shots,
        "bubble_samples": texts,
        "bubbles": [x for s in texts for x in s["bubbles"]],
        "settled": settled,
        "settle_wait_s": round(waited, 1),
        "after": await p.ev("globalThis.__acc.probe()"),
    }


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    p = await connect()
    summary = {}

    await p.ev(JS_HELPERS)
    # 上一轮可能被打断，先复位到 neutral 并等反应结束
    await p.ev("window.__albionPet.emotion('neutral')")
    await wait_idle(p, max_s=25)
    probe0 = await p.ev("globalThis.__acc.probe()")
    print("probe:", json.dumps(probe0, ensure_ascii=False), flush=True)
    summary["probe"] = probe0
    summary["tapHitArea_exists"] = probe0.get("tapHitArea") == "function"

    # 4a 九个情绪
    emo_shots = []
    for e in EMOTIONS:
        await p.ev(f"window.__albionPet.emotion('{e}')")
        await asyncio.sleep(1.2)
        emo_shots.append(await p.shot(f"emo_{e}.png"))
        st = await p.ev("globalThis.__acc.probe()")
        print(f"emo {e}: zui={st['zui']} menus={st['menus']}", flush=True)
    await p.ev("window.__albionPet.emotion('neutral')")
    summary["emo_shots"] = emo_shots

    # 4b 两次 poke
    await asyncio.sleep(3)
    summary["poke_waist"] = await poke_round(p, "waist")
    summary["poke_head"] = await poke_round(p, "head")

    # 4c 换脸嘴骨逐帧
    for tag, post in (("zui_trace.json", 1000), ("zui_trace_long.json", 2500)):
        traces = []
        for emotion in ("happy", "neutral"):
            tr = await p.ev(f"{TRACE_JS}('{emotion}', 100, {post})")
            if "error" in tr:
                traces.append({"emotion": emotion, "error": tr["error"]})
                continue
            # 只保留换脸前 0.1s 起的数据
            rec = [f for f in tr["rec"] if f[0] >= tr["swapAt"] - 0.12]
            stand = [f for f in rec if f[0] < tr["swapAt"]]
            after = [f for f in rec if f[0] >= tr["swapAt"]]
            traces.append({
                "emotion": emotion,
                "swapAt": tr["swapAt"],
                "frames": len(rec),
                "before_last": stand[-1][1] if stand else None,
                "after_first": after[0][1] if after else None,
                "after_last": after[-1][1] if after else None,
                "max_step_all": max_step(rec),
                "max_step_after": max_step(after),
                "rec": rec,
            })
            print(f"{tag} {emotion}: frames={len(rec)} before={stand[-1][1] if stand else None} "
                  f"last={after[-1][1] if after else None} maxStepAfter={max_step(after)['delta']}", flush=True)
            await asyncio.sleep(0.8)
        (OUT / tag).write_text(json.dumps(traces, ensure_ascii=False, indent=1), encoding="utf-8")
        summary[tag] = [{k: v for k, v in t.items() if k != "rec"} for t in traces]

    # 复位
    await p.ev("window.__albionPet.emotion('neutral')")
    await asyncio.sleep(1.5)
    final = await p.ev("globalThis.__acc.probe()")
    final["idle"] = await p.ev("globalThis.__acc.idle()")
    summary["final"] = final
    await p.shot("reset_neutral.png")
    print("final:", json.dumps(final, ensure_ascii=False), flush=True)

    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for part in ("waist", "head"):
        v = summary[f"poke_{part}"]
        (OUT / f"poke_{part}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
