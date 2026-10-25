"""Opt-in two-key Command Code free-model probe (Windows Python); reads existing keys, never changes them.

key#1 = TTS bridge key file, key#2 = hermes .env DAILY_2 (same sources as tts/irodori/bridge.py);
the brain's models.json key is only compared by sha256[:8]. Per model and key: one Responses-API
translation (as the TTS bridge `_ask`) and one chat-completions tool call (as Pi `openai-completions`).
Output never contains keys; error bodies are redacted.

  python -I tests/pi-brain/probe_free_models_keys.py --out tests/output/free-model-check/probe-3.json
  python -I tests/pi-brain/probe_free_models_keys.py --models a/b,c/d --out tests/output/x/probe.json  (only these ids)
"""
import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = 'https://api.commandcode.ai/provider/v1'
MUST = ['minimax/minimax-m3.1-flash:free', 'reflection/beam:free']
SOURCE = '今天也一起把家里的事做好吧。'
TOOL = {'type': 'function', 'function': {'name': 'probe_echo', 'description': 'Return the supplied word.',
        'parameters': {'type': 'object', 'properties': {'word': {'type': 'string'}}, 'required': ['word']}}}


def sha8(k):
    return hashlib.sha256(k.encode()).hexdigest()[:8]


def read_keys():
    k1 = Path(r'D:\AI\_secrets\command code.txt').read_text(encoding='utf-8-sig').strip()
    k2 = ''
    for line in (Path.home() / 'AppData' / 'Local' / 'hermes' / '.env').read_text(encoding='utf-8', errors='ignore').splitlines():
        if line.startswith('HERMES_CUSTOM_COMMAND_CODE_DAILY_2_API_KEY='):
            k2 = line.split('=', 1)[1].strip().strip('"').strip("'")
    agent = Path(r'\\wsl.localhost\Ubuntu\root\albion-pi\agent')
    ref = json.loads((agent / 'models.json').read_text(encoding='utf-8'))['providers']['commandcode']['apiKey']
    env = dict(l.split('=', 1) for l in (agent / '.env').read_text(encoding='utf-8').splitlines() if '=' in l and not l.startswith('#'))
    brain = env.get(ref.lstrip('$'), ref).strip().strip('"').strip("'")
    return k1, k2, brain


def call(key, keys, path, body=None):
    req = urllib.request.Request(f'{BASE}/{path}', data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'curl/8.5.0',
                                          'Content-Type': 'application/json'})
    t = time.monotonic()
    def scrub(s):
        for k in keys:
            s = s.replace(k, '<REDACTED>')
        return s
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return {'status': r.status, 's': round(time.monotonic() - t, 2), 'body': json.load(r)}
    except urllib.error.HTTPError as e:
        return {'status': e.code, 's': round(time.monotonic() - t, 2), 'error': scrub(e.read().decode(errors='replace'))[:500]}
    except Exception as e:  # noqa: BLE001
        return {'status': 0, 's': round(time.monotonic() - t, 2), 'error': scrub(f'{type(e).__name__}: {e}')[:300]}


def translate(key, keys, model):
    r = call(key, keys, 'responses', {'model': model, 'store': False, 'reasoning': {'effort': 'none'},
             'input': [{'role': 'system', 'content': '把用户给的中文翻译成自然的日语，只输出译文。'},
                       {'role': 'user', 'content': SOURCE}]})
    txt = ''
    for item in (r.get('body') or {}).get('output') or []:
        if item.get('type') == 'message':
            txt += ''.join(c.get('text') or '' for c in item.get('content') or [] if c.get('type') in ('output_text', 'text'))
    r['text'] = txt.strip()
    r['usage'] = (r.pop('body', None) or {}).get('usage')
    return r


def tool_call(key, keys, model):
    r = call(key, keys, 'chat/completions', {'model': model, 'max_tokens': 512, 'tools': [TOOL], 'tool_choice': 'auto',
             'messages': [{'role': 'user', 'content': '请调用 probe_echo 工具，word 必须是早安。不要仅输出文字。'}]})
    body = r.pop('body', None) or {}
    msg = (body.get('choices') or [{}])[0].get('message') or {}
    calls = msg.get('tool_calls') or []
    try:
        r['valid'] = len(calls) == 1 and calls[0]['function']['name'] == 'probe_echo' \
            and json.loads(calls[0]['function']['arguments']) == {'word': '早安'}
    except (KeyError, ValueError, TypeError):
        r['valid'] = False
    r['n_calls'] = len(calls)
    r['content'] = (msg.get('content') or '')[:120]
    r['usage'] = body.get('usage')
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--models', default='', help='comma-separated model ids; skips free-model selection')
    args = ap.parse_args()
    k1, k2, brain = read_keys()
    assert k1 and k2, 'key#1 or key#2 missing'
    keys = [k for k in (k1, k2, brain) if k]
    out = {'key_sha8': {'key#1': sha8(k1), 'key#2': sha8(k2), 'brain': sha8(brain) if brain else None}}
    cat = call(k1, keys, 'models')
    assert cat['status'] == 200, cat
    models = cat['body']['data']
    def is_free(m):
        p = m.get('pricing') or {}
        return 'free' in m['id'].lower() or (p and all(str(p.get(k, '')) in ('0', '0.0', '0.00') for k in ('prompt', 'completion')))
    free = [m for m in models if is_free(m)]
    out['catalog'] = {'total': len(models), 'pricing_field': any('pricing' in m for m in models),
                      'sample_keys': sorted(models[0].keys()) if models else [], 'free': free}
    ids = [s.strip() for s in args.models.split(',') if s.strip()] if args.models else \
        [m['id'] for m in free] + [m for m in MUST if m not in {f['id'] for f in free}]
    out['account'] = {name: {p: call(k, keys, p).get('status') for p in ('usage', 'credits', 'me')}
                      for name, k in (('key#1', k1), ('key#2', k2))}
    out['probes'] = []
    for mid in ids:
        for name, k in (('key#1', k1), ('key#2', k2)):
            row = {'model': mid, 'key': name, 'in_catalog': mid in {f['id'] for f in free},
                   'translate': translate(k, keys, mid), 'tool': tool_call(k, keys, mid)}
            out['probes'].append(row)
            print(json.dumps({'model': mid, 'key': name, 'tr': [row['translate']['status'], row['translate']['s'], row['translate']['text'][:40]],
                              'tool': [row['tool']['status'], row['tool']['s'], row['tool']['valid']]}, ensure_ascii=False), flush=True)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
