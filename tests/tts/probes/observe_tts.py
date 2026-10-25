"""Opt-in 30 minute / 50 real render-round observation. Stops on the first failed round."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import time

import httpx
from verify_tts_live import sse, wav_info


async def main(args):
    out = Path(args.out).resolve()
    assert "tests" in out.parts and "output" in out.parts
    out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    summary = dict(requested=args.rounds, passed=0, failed=0, duration_requested_s=args.duration, rounds=[])
    async with httpx.AsyncClient(timeout=100) as c:
        async def post(path, body):
            r = await c.post(args.backend+path, json=body)
            r.raise_for_status()
            return r.json()
        try:
            for i in range(args.rounds):
                at = start + args.duration*i/args.rounds
                while time.monotonic() < at:
                    await asyncio.sleep(min(1, at-time.monotonic()))
                t0 = time.monotonic()
                plan = await post('/v2/plan', dict(plan_version=4, emotion_policy='cascade',
                    units=[dict(unit_id='u', text='こんにちは。')]))
                opened = await post('/v2/render/open', dict(voice='albion'))
                address = dict(session_id=opened['session_id'], generation=opened['generation'])
                complete, blobs, first_audio = False, [], None
                try:
                    async with c.stream('POST', args.backend+'/v2/render', json=dict(address,
                            voice='albion', format='wav', plan_version=4,
                            units=[dict(plan['units'][0], text='こんにちは。', unit_id='u', seed=12345)])) as response:
                        response.raise_for_status()
                        async for event, p in sse(response):
                            assert event not in ('unit.failed', 'render.cancelled')
                            if event == 'audio_chunk':
                                blobs.append(base64.b64decode(p['audio_base64'], validate=True))
                                first_audio = first_audio or time.monotonic()-t0
                            elif event == 'done':
                                assert p['status'] == 'complete' and p['failed'] == 0
                                complete = True
                    assert complete and len(blobs) == 1
                finally:
                    await post('/v2/render/'+('close' if complete else 'cancel'), address)
                info = wav_info(blobs[0])
                (out/('%02d.wav' % i)).write_bytes(blobs[0])
                r = await c.get(args.backend+'/v2/render/stats')
                r.raise_for_status()
                resources = r.json()
                assert resources['active_sessions'] <= 128 and resources['retired_entries'] <= 128
                health = {}
                for port, path in [(9881,'/v2/health'),(9885,'/health'),(9889,'/health')]:
                    r = await c.get('http://127.0.0.1:%d%s' % (port,path))
                    r.raise_for_status()
                    h = r.json()
                    assert h['ok']
                    health[port] = h
                cfg = health[9885]['config']
                assert [cfg[k] for k in ('plan_version','emotion_mode','emotion_policy','prepare_timeout','unit_budget','render_timeout')] == [4,'v3','cascade',60.,110.,90.]
                idle = not health[9885]['sessions'] and not resources['queued'] and not resources['running']
                if idle:
                    assert all(resources[k] == 0 for k in ('active_sessions','closing_sessions','retired_entries','watchers','inflight_units','done_units','dropped_units'))
                row = dict(round=i+1, elapsed_s=round(time.monotonic()-start,3),
                           first_audio_s=round(first_audio,3), audio=info, idle=idle, resources=resources)
                summary['rounds'].append(row)
                summary['passed'] += 1
                (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
                print(json.dumps({k:row[k] for k in ('round','elapsed_s','first_audio_s','idle')}),flush=True)
            while time.monotonic() - start < args.duration:
                await asyncio.sleep(min(1, args.duration-(time.monotonic()-start)))
        except Exception as e:
            summary['failed'] += 1
            summary['error_type'] = type(e).__name__
        summary['elapsed_s'] = round(time.monotonic()-start,3)
        summary['complete'] = summary['passed'] == args.rounds and summary['elapsed_s'] >= args.duration
        (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    return not summary['complete']


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend', default='http://127.0.0.1:9889')
    p.add_argument('--duration', type=float, default=1800)
    p.add_argument('--rounds', type=int, default=50)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    assert a.rounds > 0 and a.duration >= 0
    raise SystemExit(asyncio.run(main(a)))
