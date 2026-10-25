"""Opt-in real Command Code catalog/tool probe; reads existing keys, never changes them.

Run with WSL Python on the production host (see README). Only synthetic messages
are sent. Output contains catalog metadata and synthetic responses, never keys.
"""
import argparse
import json
from pathlib import Path
import socket
import signal
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brain', default='/root/albion-pi')
    parser.add_argument('--out', required=True)
    parser.add_argument('--only', help='Probe only this catalog model ID')
    parser.add_argument('--key-env', help='Use this existing .env variable instead of the provider reference')
    args = parser.parse_args()
    agent = Path(args.brain) / 'agent'
    env = {}
    for line in (agent / '.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    provider = json.loads((agent / 'models.json').read_text())['providers']['commandcode']
    reference = args.key_env or provider['apiKey']
    secret = env.get(reference.lstrip('$'), reference)
    if args.key_env and args.key_env not in env:
        raise SystemExit('requested existing key variable is absent')
    # Same IPv4 route as albion-relay; this host's AAAA lookup can take >10s.
    original = socket.getaddrinfo
    socket.getaddrinfo = lambda h, p, family=0, type=0, proto=0, flags=0: original(
        h, p, socket.AF_INET, type, proto, flags)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def deadline(_signum, _frame):
        raise TimeoutError('request deadline')
    signal.signal(signal.SIGALRM, deadline)

    def call(path, body=None):
        request = urllib.request.Request('https://api.commandcode.ai/provider/v1/' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + secret, 'User-Agent': 'curl/8.5.0',
                     'Content-Type': 'application/json'})
        started = time.monotonic()
        signal.alarm(45)
        try:
            with opener.open(request, timeout=60) as response:
                result = json.load(response)
            return {'status': 200, 'seconds': round(time.monotonic()-started, 3), 'body': result}
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors='replace').replace(secret, '<REDACTED>')
            return {'status': error.code, 'seconds': round(time.monotonic()-started, 3), 'error': text[:700]}
        except Exception as error:
            return {'status': 0, 'seconds': round(time.monotonic()-started, 3), 'error': type(error).__name__}
        finally:
            signal.alarm(0)

    catalog = call('models')
    assert catalog['status'] == 200, 'model catalog unavailable'
    models = catalog['body']['data']
    free = [m for m in models if 'free' in m['id'].lower()
            or all(str(m.get('pricing', {}).get(k, '')) in ('0', '0.0')
                   for k in ('prompt', 'completion'))]
    if args.only:
        free = [m for m in free if m['id'] == args.only]
        if not free:
            raise SystemExit('requested model is absent from the free catalog')
    result = {'total_models': len(models), 'free_catalog': free, 'probes': []}
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        result['probes'] = json.loads(output.read_text())['probes']
    tool = {'type': 'function', 'function': {'name': 'probe_echo', 'description': 'Return the supplied word.',
        'parameters': {'type': 'object', 'properties': {'word': {'type': 'string'}}, 'required': ['word']}}}
    for model in free:
        model_id = model['id']
        if any(row['model'] == model_id for row in result['probes']):
            continue
        chat = call('chat/completions', {'model': model_id, 'max_tokens': 256,
            'messages': [{'role': 'user', 'content': '请用中文说一句简短的早安，不要调用工具。'}]})
        tools = call('chat/completions', {'model': model_id, 'max_tokens': 512,
            'messages': [{'role': 'user', 'content': '请调用 probe_echo 工具，word 必须是早安。不要仅输出文字。'}],
            'tools': [tool], 'tool_choice': 'auto'})
        message = (tools.get('body', {}).get('choices') or [{}])[0].get('message', {})
        calls = message.get('tool_calls') or []
        valid = False
        try:
            valid = (len(calls) == 1 and calls[0]['function']['name'] == 'probe_echo'
                     and json.loads(calls[0]['function']['arguments']) == {'word': '早安'})
        except (KeyError, ValueError, TypeError):
            pass
        row = {'model': model_id, 'chat': chat, 'tool': tools, 'valid_tool_call': valid}
        result['probes'].append(row)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({'model': model_id, 'chat_status': chat['status'], 'chat_s': chat['seconds'],
            'tool_status': tools['status'], 'tool_s': tools['seconds'], 'valid_tool_call': valid}), flush=True)


if __name__ == '__main__':
    main()
