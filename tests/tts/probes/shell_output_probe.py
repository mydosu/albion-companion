"""A: 只读读取生产壳渲染页的输出端参数（任务 46cbd1a6）。

在现有主渲染页里新建一个临时 AudioContext 读 sampleRate/sinkId/baseLatency/outputLatency，
列 audiooutput / audioinput 设备名，并把 localStorage 里与音量/增益/语音相关的键打印出来。
不注入钩子、不改任何东西，读完即 close 这个临时 context。
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

import websockets

JS = r"""
(async () => {
  const out = { ua: navigator.userAgent.slice(-48) };
  const ctx = new AudioContext();
  out.sampleRate = ctx.sampleRate;
  out.state = ctx.state;
  out.baseLatency = ctx.baseLatency;
  out.outputLatency = ctx.outputLatency;
  out.sinkId = String(ctx.sinkId);
  out.destinationMaxChannels = ctx.destination.maxChannelCount;
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    out.outputs = devs.filter(d => d.kind === 'audiooutput').map(d => d.label);
    out.inputs = devs.filter(d => d.kind === 'audioinput').map(d => d.label);
  } catch (e) { out.devicesError = String(e); }
  out.ls = {};
  for (const [k, v] of Object.entries(localStorage)) {
    if (/vol|gain|speech|tts|audio|mute|speaker|output|hearing|listen/i.test(k))
      out.ls[k] = String(v).slice(0, 400);
  }
  await ctx.close();
  return out;
})()
"""


async def main() -> int:
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9335/json/list", timeout=3))
    pages = [x for x in targets if x.get("type") == "page" and "synced-leader=true#/" in x.get("url", "")]
    assert len(pages) == 1, f"need exactly one main renderer, found {len(pages)}"
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps(dict(id=1, method="Runtime.evaluate",
                                      params=dict(expression=JS, awaitPromise=True, returnByValue=True))))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if resp.get("id") == 1:
                print(json.dumps(resp["result"], ensure_ascii=False, indent=1)[:4000])
                return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
