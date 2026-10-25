# -*- coding: utf-8 -*-
"""I 侧 v2 接线（P3 第三部分）：**新老端点全部**排进同一条单 GPU 队列。

为什么必须有这个文件：`render_queue.py` 只让新端点走了队列，旧的 `/tts`、`/tts_stream` 仍直接调
`synth_stream`/`synth_auto` → 新旧混合请求时同时会有两段在推理，"全局单 GPU"是假的。这里让两个旧
端点保持**请求/响应契约一字不改**，只把推理换成排队跑。

落地（P7 维护窗口）：`iq = RenderQueue(); serve_v2.install(app, iq)`。
规划统一复用 scene_plan（_segments → _raw_styles → _sticky → _plan_styles）。
后端已删除被本模块路由替换的旧合成副本；回归测试位于 tests/tts/。
"""
import asyncio
import base64
import json

import scene_plan as SP
from render_queue import RenderQueue, _mount

GAP_SEC = 0.10        # 行间静音，与 irodori_serve 同值


def _seg_unit(i, seg, plan, styles, scene_ids, ship):
    """一段 → 队列能吃的单元（旧 SSE 事件的字段都从这一份里取，保证两处一致）。"""
    fam, cap, cfg = plan[i]
    return {"unit_id": "s%d" % i, "sub_index": i, "text": seg, "caption": cap, "cfg": cfg,
            "family": fam, "voice": ship, "style": styles[i], "scene_id": scene_ids[i] + 1}


def legacy_plan(text, style_override=None, ctx=""):
    """旧 `synth_stream()` 那套规划，返回 (segs, plan, scene_ids, styles)。"""
    segs, breaks = SP._segments(text)
    if style_override in SP.STYLES:
        plan = [("override",) + SP.STYLES[style_override]] * len(segs)
        return segs, plan, [0] * len(segs), [style_override] * len(segs)
    raw = SP._sticky(SP._raw_styles(segs, ctx))
    plan, scene_ids = SP._plan_styles(segs, raw, breaks)
    return segs, plan, scene_ids, [p[0] for p in plan]      # 旧实现里非强制档的 style 就是族名


def _concat(a, b, gap_samples):
    """拼接两段波形 + 中间静音。真实路径是 numpy，离线冒烟没有 numpy 时退回 list。"""
    try:
        import numpy as np
        return np.concatenate([np.asarray(a, dtype="float32"),
                               np.zeros(gap_samples, dtype="float32"),
                               np.asarray(b, dtype="float32")])
    except ImportError:
        return list(a) + [0.0] * gap_samples + list(b)


def _ship(body, all_ships, default):
    ship = (body.get("voice") or default).strip().lower().replace("-irodori", "")
    return ship if ship in all_ships else default


def install(app, queue=None, all_ships=("albion",), default_ship="albion"):
    """把 `/tts`、`/tts_stream`（契��不变，推理入队）挂到 app 上；`/v2/*` 另用 install 挂。"""
    from fastapi import Request, Response
    from fastapi.responses import StreamingResponse

    q = queue or RenderQueue()

    async def tts(request: Request):
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return Response(status_code=400, content="missing text")
        override = (body.get("style") or body.get("emotion") or "").strip().lower() or None
        ship = _ship(body, all_ships, default_ship)
        ctx = (body.get("ctx") or "").strip()
        segs, plan, scene_ids, styles = legacy_plan(text, override, ctx)
        if not segs:
            return Response(status_code=500, content="irodori failed: RuntimeError: no audio")
        try:
            session_id, generation = q.open(ship)
        except OverflowError:
            return Response(status_code=429, content="render_session_capacity")
        got, done = [], False
        try:
            for i, seg in enumerate(segs):
                # 逐段排队：段与段之间把 GPU 让出去，别让一个整篇请求独占到结束
                res = await q.submit(session_id, generation, _seg_unit(i, seg, plan, styles, scene_ids, ship))
                if res["status"] != "ok":
                    raise RuntimeError(res.get("error") or res["status"])
                got.append((res["sr"], res["wave"]))
            sr = got[0][0]
            merged = got[0][1]
            for sr_i, wave in got[1:]:
                merged = _concat(merged, wave, int(GAP_SEC * sr_i))
            blob = await asyncio.to_thread(q.encode, merged, sr, "wav")   # 编码可能等 ffmpeg，别占着事件循环
            done = True
            return Response(content=blob, media_type="audio/wav",
                            headers={"X-Irodori-Styles": str(list(styles)),
                                     "X-Irodori-Ship": ship})
        except Exception as e:
            return Response(status_code=500,
                            content="irodori failed: %s: %s" % (type(e).__name__, str(e)[:300]))
        finally:
            if not done:
                q.cancel(session_id, 1)         # 出错/断线：没跑的段别再占 GPU
            else:
                q.close(session_id, 1)          # 成功也清理去重账本。

    async def tts_stream(request: Request):
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return Response(status_code=400, content="missing text")
        override = (body.get("style") or body.get("emotion") or "").strip().lower() or None
        ship = _ship(body, all_ships, default_ship)
        fmt = (body.get("format") or "mp3").strip().lower()
        ctx = (body.get("ctx") or "").strip()

        async def gen():
            try:
                session_id, generation = q.open(ship)
            except OverflowError:
                yield 'event: error\ndata: {"error":"render_session_capacity"}\n\n'
                return
            segs, plan, scene_ids, styles = legacy_plan(text, override, ctx)
            chunks, finished = 0, False
            try:
                for i, seg in enumerate(segs):
                    unit = _seg_unit(i, seg, plan, styles, scene_ids, ship)
                    res = await q.submit(session_id, 1, unit)
                    if res["status"] != "ok":
                        yield ("event: error\ndata: %s\n\n" % json.dumps(
                            {"error": res.get("error") or res["status"]}, ensure_ascii=False))
                        return
                    sr, wave = res["sr"], res["wave"]
                    if i:
                        wave = _prepend_gap(wave, sr)
                    blob = await asyncio.to_thread(q.encode, wave, sr, fmt)
                    payload = {"seg": seg[:40], "style": styles[i], "family": unit["family"],
                               "scene": unit["scene_id"], "cfg": unit["cfg"],
                               "dur": round(len(wave) / sr, 2), "index": i, "ship": ship, "format": fmt,
                               "audio_base64": base64.b64encode(blob).decode()}
                    yield "event: audio_chunk\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
                    chunks += 1
                yield ("event: done\ndata: %s\n\n" % json.dumps(
                    {"chunks": chunks, "ship": ship, "format": fmt}))
                finished = True
            except Exception as e:
                yield ("event: error\ndata: %s\n\n" % json.dumps(
                    {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}, ensure_ascii=False))
            finally:
                if not finished:
                    # 断线 / 中途出错：未起跑的段出队、在跑的段结果丢弃（与 v2 同一套取消语义）
                    q.cancel(session_id, 1)
                else:
                    q.close(session_id, 1)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Irodori-Ship": ship})

    # 先删同路径旧路由再挂：生产 app 里这两个路径已经有旧处理函数，不删就永远不会走到这里
    _mount(app, "/tts", tts, ["POST"])
    _mount(app, "/tts_stream", tts_stream, ["POST"])
    return app


def _prepend_gap(wave, sr):
    """旧实现：段间 GAP 挂在后段头部（客户端首尾相接即等于整篇）。"""
    try:
        import numpy as np
        return np.concatenate([np.zeros(int(GAP_SEC * sr), dtype="float32"),
                               np.asarray(wave, dtype="float32")])
    except ImportError:
        return [0.0] * int(GAP_SEC * sr) + list(wave)
