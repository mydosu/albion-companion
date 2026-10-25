# -*- coding: utf-8 -*-
"""复现「漏句」：服务端 credit 看门狗把长段当成卡死，砍掉单元 ⇒ 后面整句都不出声。

关键在 ack 时机：壳只在**一段播完**时才 ack。所以一段 10s+ 的音频在播的这段时间里，
服务端看到的是「一直没有播放进度」；若此刻它正要发下一段（待播余额超窗口），
等满 credit_wait(5s) 就判 StalledCredit 砍单元。

跑法： tts/irodori/.venv/Scripts/python.exe -B tests/tts/support/credit_repro.py
退出码：0 = 全单元都收到（正常）；1 = 被砍（复现成功）
"""
import asyncio
import json
import struct
import sys
import time

import websockets

URL = "ws://127.0.0.1:9885/api/v1/audio/speech/ws?token=albion-local&tts_trigger=manual&tts_source=probe"
PV = 2
TEXT = ("很短一篇。开头三样：燕子去了会再回来，杨柳枯了会再青，桃花。"
        "他算自己那八千多个日子，像针尖上的一滴水掉进海里，没声音，也没影子。"
        "太阳是有脚的：洗手的时候日子从水盆里过去，吃饭的时候日子从饭碗里过去。"
        "末了问一句——为什么偏要白白走这一遭呢。")


def wav_secs(blob):
    """一份 WAV 的可听秒数 = 数据字节 ÷ (声道 × 位深/8) ÷ 采样率。

    ⚠️ 别只除以采样率（更别用 fmt[3]=byte_rate）：16bit 单声道下会**正好多算一倍** ——
    我第一版就这么错，把服务端的 3.16s 段报成 6.32s，还拿它当"服务端低估"的证据（codex 复核抓的口径错）。
    """
    fr = wav_frames(blob)
    return data_secs(fr)


def data_secs(frames):
    return sum(d / float(max(1, ch) * max(1, bits // 8)) / float(sr) for d, sr, ch, bits in frames)


def wav_frames(blob):
    """按**子段**逐份解析（服务端一个 `sentence` 可能由多份 WAV 拼成）→ [(数据字节数, 采样率, 声道, 位深)]。"""
    import struct
    out, i = [], 0
    while i + 12 <= len(blob) and blob[i:i + 4] == b"RIFF" and blob[i + 8:i + 12] == b"WAVE":
        j, fmt, data = i + 12, None, None
        while j + 8 <= len(blob):
            cid, size = blob[j:j + 4], int.from_bytes(blob[j + 4:j + 8], "little")
            if cid == b"fmt " and len(blob) >= j + 24:
                fmt = struct.unpack("<HHIIHH", blob[j + 8:j + 24])
            elif cid == b"data":
                data = size
                break
            j += 8 + size + (size & 1)
        if not fmt or data is None:
            break
        out.append((data, fmt[2], fmt[0], fmt[5]))
        i = j + 8 + data + (data & 1)
    return out


def blob_secs(blob):
    """整个 payload 的真实可听秒数与子段数（逐份求和，与服务端口径一致）。"""
    fr = wav_frames(blob)
    return data_secs(fr), len(fr)


async def main():
    t0 = time.monotonic()
    got, events = [], []
    play_end = [0.0]        # 排到的播放终点（秒，1x 时间轴：只用来决定「什么时候该 ack」）
    audio_done = [0.0]      # 累计**已播完的音频秒数** = 壳报的 seconds 口径（音频内进度，不含空档）
    done = [0]              # 累计已播完的段数 = 壳报的 units 口径
    lock = asyncio.Lock()
    muted = [False]

    async def ack_later(ws, uid, secs):
        """按 1x 播放模拟：收到即排队，这段**播完**才 ack（壳就是这么做的）。

        ack 的 `seconds` 必须是**累计音频秒数**（壳的播放时钟只算音频内进度，不含渲染空档）。
        第一版我报的是「播放时间轴位置」，把空档算进去 = 虚报进度（codex 复核抓到的口径错）。
        """
        async with lock:
            start = max(play_end[0], time.monotonic() - t0)
            play_end[0] = start + secs
            send_at = t0 + play_end[0]
            audio_done[0] += secs
            done[0] += 1
            secs_total, units_total = audio_done[0], done[0]
        wait = send_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        if muted[0]:
            return
        await ws.send(json.dumps({"event": "playback.ack", "seconds": round(secs_total, 3),
                                  "units": units_total, "unit_id": uid}))
        print("  ack 音频 %.2fs / %d 段（%s）" % (secs_total, units_total, uid))

    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({"event": "start", "voice": "albion", "response_format": "wav",
                                  "extra_body": {"protocol_version": PV, "generation": 1}}))
        await ws.send(json.dumps({"event": "text", "text": TEXT}))
        await ws.send(json.dumps({"event": "finish"}))
        buf = b""
        tasks = []
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=240)
                if isinstance(raw, bytes):
                    buf += raw
                    continue
                ev = json.loads(raw)
                name, p = ev.get("event"), ev.get("payload") or {}
                if name == "sentence.start":
                    buf = b""
                elif name == "sentence.end":
                    secs = wav_secs(buf)
                    true_s, nfr = blob_secs(buf)
                    uid = p.get("unit_id")
                    got.append((uid, round(true_s, 2)))
                    print("  收到 %s  单头 %.2fs / 逐子段 %.2fs（%d 份）  (共 %d 段)"
                          % (uid, secs, true_s, nfr, len(got)))
                    tasks.append(asyncio.ensure_future(ack_later(ws, uid, true_s)))
                elif name == "unit.failed":
                    events.append(("failed", p.get("stage"), p.get("reason") or p.get("error")))
                    print("  !! unit.failed stage=%s %s" % (p.get("stage"), (p.get("reason") or p.get("error"))))
                elif name == "session.finished":
                    events.append(("finished", ev.get("status"), ev.get("units"), ev.get("audio"),
                                   ev.get("no_audio"), ev.get("failed")))
                    print("  session.finished status=%s units=%s audio=%s no_audio=%s failed=%s"
                          % (ev.get("status"), ev.get("units"), ev.get("audio"), ev.get("no_audio"), ev.get("failed")))
                    break
        except (websockets.ConnectionClosed, asyncio.TimeoutError) as e:
            muted[0] = True
            print("  !! 连接断了：%s" % type(e).__name__)
        finally:
            muted[0] = True
            for t in tasks:
                t.cancel()

    ok = any(e[0] == "finished" and e[1] == "complete" for e in events)
    print("收到段数 %d；段长 %s" % (len(got), [s for _, s in got]))
    print("判定：%s" % ("未复现（全段到达）" if ok else "复现成功——被 credit 看门狗砍掉，后面整句没了"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
