"""采一份「眼槽 → 脸图」的几何（setup pose），给 spine_face_eye_masks.py 用。

必须在跑着的壳上采（CDP 9335，leader 页）。产物写到模型素材目录旁边的 albion-eye-geometry.json
（本地文件，不入库）。只读，不改壳的状态。

用法：
  python pet/tools/spine_face_eye_geometry_dump.py
  python pet/tools/spine_face_eye_geometry_dump.py --port 9335 --out D:/AI/Wife/shell/.../albion-eye-geometry.json
"""
import argparse
import asyncio
import json
import urllib.request
from pathlib import Path

import websockets

JS = """(() => {
  const m = globalThis.__albionSpine38, sk = m.skeleton, sd = m.spineData
  const faces = []
  const faceIdx = sd.slots.findIndex(s => s.name === '1')
  const bag = sd.skins[0].attachments[faceIdx]
  for (const name of ['1','2','3','4','5','6','7','8','9','10','11']) {
    const att = bag[name]
    if (!att) continue
    const n = att.worldVerticesLength || (att.vertices ? att.vertices.length : 0)
    const w = new Array(n)
    att.computeWorldVertices(sk.slots[faceIdx], 0, n, w, 0, 2)
    faces.push({ name, world: w, regionUVs: Array.from(att.uvs || []), triangles: Array.from(att.triangles || []),
                 region: { u: att.region.u, v: att.region.v, u2: att.region.u2, v2: att.region.v2,
                           w: att.region.width, h: att.region.height, rotate: !!att.region.rotate, page: att.region.page.name } })
  }
  const eyes = []
  for (const slot of sk.slots) {
    const nm = String(slot.data.name)
    if (!/^(?:you|zuo)yan-\d$/.test(nm)) continue
    const a = slot.getAttachment()
    if (!a) { eyes.push({ slot: nm, world: null }); continue }
    const n = a.worldVerticesLength || (a.vertices ? a.vertices.length : 0)
    const w = n ? new Array(n) : []
    if (n) a.computeWorldVertices(slot, 0, n, w, 0, 2)
    eyes.push({ slot: nm, world: w })
  }
  return { faces, eyes }
})()"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9335)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2]
                                         / "shell/packages/stage-ui/src/assets/spine/models/albion-eye-geometry.json"))
    args = ap.parse_args()
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/list", timeout=5))
    leader = next(t for t in targets if "synced-leader=true#/" in t.get("url", ""))
    async with websockets.connect(leader["webSocketDebuggerUrl"], max_size=64 << 20) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": JS, "returnByValue": True}}))
        while True:
            msg = json.loads(await ws.recv(), strict=False) if False else json.loads(await ws.recv())
            if msg.get("id") == 1:
                break
    if "exceptionDetails" in msg["result"]:
        raise SystemExit(json.dumps(msg["result"]["exceptionDetails"], ensure_ascii=False)[:500])
    geo = msg["result"]["result"]["value"]
    geo["note"] = "setup-pose geometry dumped from the running shell (CDP). Faces: 1..11, eyes: youyan/zuoyan slots."
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")
    print(f"faces {len(geo['faces'])} eyes {len(geo['eyes'])} → {out}")


if __name__ == "__main__":
    asyncio.run(main())
