"""Opt-in real Electron chatbox -> brain -> TTS probe via existing local CDP.

Requires the running shell (:9335), brain, TTS and websockets. Submits synthetic
messages to the real chat UI; these become real chat history. Audio hooks copy
only audio decoded during this run and are restored on exit. No tokens, request
headers, profile settings or prior chat history are exported.
"""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import time
import urllib.request

import websockets


HOOK = r"""(() => {
 if(window.__chainR1) throw new Error('probe already installed');
 const p = window.__chainR1 = {audio:[],starts:[],errors:[],tts:[]};
 const proto = BaseAudioContext.prototype, orig = proto.decodeAudioData;
 const sp = AudioBufferSourceNode.prototype, start = sp.start;
 const OriginalWS=window.WebSocket;
 window.WebSocket=new Proxy(OriginalWS,{construct(target,args){
   const ws=Reflect.construct(target,args);if(new URL(args[0]).port==='9885'){
     const row={finished:false,closed:false};p.tts.push(row);
     ws.addEventListener('message',e=>{if(typeof e.data==='string'){try{const d=JSON.parse(e.data);if(d.event==='session.finished')row.finished=true;if(d.event==='error')row.error=d.code||'tts_error';}catch{}}});
     ws.addEventListener('close',()=>{row.closed=true;});
   }return ws;
 }});
 p.restore = () => {proto.decodeAudioData=orig;sp.start=start;window.WebSocket=OriginalWS;delete window.__chainR1;};
 proto.decodeAudioData = function(data, ...rest) {
   const bytes=new Uint8Array(data.slice(0));let s='';
   for(let i=0;i<bytes.length;i+=8192)s+=String.fromCharCode(...bytes.subarray(i,i+8192));
   const row={base64:btoa(s),bytes:bytes.length};p.audio.push(row);
   const result=orig.call(this,data,...rest);
   if(result?.then)result.then(b=>{row.seconds=b.duration;row.rate=b.sampleRate;},e=>{row.error=e.name;});
   return result;
 };
 sp.start=function(...args){p.starts.push({seconds:this.buffer?.duration,state:this.context.state});return start.apply(this,args);};
 return true;
})()"""


async def main(args):
    out = Path(args.out).resolve()
    assert 'tests' in out.parts and 'output' in out.parts
    out.mkdir(parents=True, exist_ok=True)
    targets = json.load(urllib.request.urlopen(f'http://127.0.0.1:{args.cdp_port}/json/list', timeout=4))
    leader = next(x for x in targets if 'synced-leader=true#/' in x.get('url', ''))
    chatbox = next(x for x in targets if '#/chatbox' in x.get('url', ''))
    records, requests = [], {}
    ident = 0
    async with websockets.connect(leader['webSocketDebuggerUrl'], max_size=32*1024*1024) as ws:
        async def command(method, params=None):
            nonlocal ident
            ident += 1
            current = ident
            await ws.send(json.dumps({'id': current, 'method': method, 'params': params or {}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if msg.get('id') == current:
                    assert 'error' not in msg, 'CDP command failed'
                    return msg.get('result', {})
                event(msg)

        def event(msg):
            method, p = msg.get('method'), msg.get('params', {})
            if method == 'Network.requestWillBeSent':
                url = p['request']['url']
                if ':8650/v1/chat/completions' in url:
                    requests[p['requestId']] = {'kind': 'brain', 'status': None, 'finished': False}
            elif method == 'Network.responseReceived' and p['requestId'] in requests:
                requests[p['requestId']]['status'] = p['response']['status']
            elif method in ('Network.loadingFinished', 'Network.loadingFailed') and p['requestId'] in requests:
                requests[p['requestId']]['finished'] = True
                if method.endswith('Failed'):
                    requests[p['requestId']]['failed'] = True

        async def evaluate(code):
            result = await command('Runtime.evaluate', {'expression': code, 'returnByValue': True, 'awaitPromise': True})
            assert 'exceptionDetails' not in result, 'renderer evaluation failed'
            return result['result'].get('value')

        await command('Network.enable')
        await evaluate(HOOK)
        try:
            async with websockets.connect(chatbox['webSocketDebuggerUrl']) as chat:
                expression = "(() => {const i=document.querySelector('input');if(!i||!i.form)throw new Error('chatbox missing');if(i.value)throw new Error('user draft present');const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;setter.call(i," + json.dumps(args.text) + ");i.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
                await chat.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': expression, 'returnByValue': True}}))
                response = json.loads(await chat.recv())
                assert 'exceptionDetails' not in response.get('result', {}), 'chatbox input failed'
                # Vue updates the disabled button on its next tick.
                await asyncio.sleep(.15)
                await chat.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate', 'params': {'expression': "document.querySelector('form').requestSubmit()"}}))
                await chat.recv()
            started, finished_at = time.monotonic(), None
            while time.monotonic()-started < args.timeout:
                await asyncio.sleep(1)
                data = await evaluate("({audio:__chainR1.audio.map(x=>({bytes:x.bytes,seconds:x.seconds,error:x.error})),starts:__chainR1.starts,tts:__chainR1.tts})")
                done = bool(requests) and all(x['finished'] for x in requests.values())
                if done and finished_at is None:
                    finished_at = time.monotonic()
                if done and data['starts'] and data['tts'] and all(x['finished'] for x in data['tts']) and time.monotonic()-finished_at > 15:
                    break
                if done and time.monotonic()-finished_at > 75:
                    break
            data = await evaluate("({audio:__chainR1.audio,starts:__chainR1.starts,tts:__chainR1.tts})")
            for i, audio in enumerate(data['audio']):
                raw = base64.b64decode(audio.pop('base64'))
                suffix = '.wav' if raw.startswith(b'RIFF') else '.audio'
                filename = args.name + '-' + str(i+1) + suffix
                (out/filename).write_bytes(raw)
                audio['file'] = filename
            data.update(requests=list(requests.values()), elapsed_s=round(time.monotonic()-started, 2),
                input=args.text, scope='actual production chatbox form and elected leader; no mock provider')
            data['chat_passed'] = bool(requests) and all(x['status'] == 200 and x['finished'] and not x.get('failed') for x in requests.values())
            data['tts_passed'] = bool(data['audio']) and all(x.get('seconds', 0)>0 and not x.get('error') for x in data['audio']) and any(x['state']=='running' for x in data['starts']) and bool(data['tts']) and all(x['finished'] and not x.get('error') for x in data['tts'])
            (out/(args.name+'.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
            print(json.dumps({k:data[k] for k in ['chat_passed','tts_passed','elapsed_s','requests']}, ensure_ascii=False))
            print('audio_samples', len(data['audio']), 'playback_starts', len(data['starts']))
            if not requests:
                # No brain request means the shell never sent the message, usually because no provider is configured
                # (a fresh profile logs "Provider: none" in artistry-bridge). Only run this against a configured shell.
                print('no brain request observed: the shell did not send the message; check its provider configuration')
            return 0 if data['chat_passed'] and data['tts_passed'] else 1
        finally:
            await evaluate('window.__chainR1?.restore()')
            await command('Network.disable')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text', required=True)
    parser.add_argument('--name', default='ui-1')
    parser.add_argument('--out', required=True)
    parser.add_argument('--timeout', type=float, default=180)
    # Default is the production shell; point at a test instance's remote-debugging port to keep production untouched.
    parser.add_argument('--cdp-port', type=int, default=9335)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
