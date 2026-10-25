"""Explicit online checks. Never emit provider replies, keys or accept dialogue."""
import argparse
import datetime
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request


def routes():
    result = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for route in ('health', 'ready'):
        try:
            with opener.open('http://127.0.0.1:1933/' + route, timeout=15) as response:
                result[route] = response.status
        except urllib.error.HTTPError as exc:
            result[route] = exc.code
        except Exception as exc:
            result[route] = type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--health', action='store_true')
    parser.add_argument('--doctor', action='store_true')
    parser.add_argument('--accept', action='store_true', help='writes production dedicated accept session')
    args = parser.parse_args()
    report = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if args.health:
        report['windows'] = routes()
        source = ('import json,urllib.request,urllib.error\n'
                  'r={}\no=urllib.request.build_opener(urllib.request.ProxyHandler({}))\n'
                  'for p in ("health","ready"):\n'
                  ' try:\n  with o.open("http://127.0.0.1:1933/"+p,timeout=15) as x:r[p]=x.status\n'
                  ' except urllib.error.HTTPError as e:r[p]=e.code\n'
                  ' except Exception as e:r[p]=type(e).__name__\n'
                  'print(json.dumps(r))')
        run = subprocess.run(['wsl.exe', '-d', 'Ubuntu', '--exec', 'python3', '-c', source],
                             capture_output=True, timeout=45)
        report['wsl'] = json.loads(run.stdout.decode('utf-8')) if run.returncode == 0 else {'exit': run.returncode}
    if args.doctor:
        script = Path.home()/'.codex/plugins/cache/openviking/openviking-memory/0.11.11/scripts/ov-memory-doctor.mjs'
        run = subprocess.run(['node', str(script), '--json', '--timeout', '4000'],
                             capture_output=True, encoding='utf-8', errors='replace', timeout=60)
        data = json.loads(run.stdout)
        connection = next((s for s in data.get('sections', []) if s['title'] == 'Connection'), {})
        # Whitelist diagnostics. Raw doctor output can contain masked key fragments.
        report['doctor'] = {'exit': run.returncode,
                            'reachable': data.get('server', {}).get('reachable'),
                            'auth_ok': data.get('server', {}).get('authOk'),
                            'ready': data.get('serverHealth', {}).get('ready'),
                            'connection': [{'level': f['level'], 'title': f['title']}
                                           for f in connection.get('findings', [])
                                           if f['title'].startswith(('server reachable', 'credentials accepted',
                                               'GET /api/v1/system/status', 'GET /api/v1/fs/ls', 'POST /mcp tools/list'))]}
    if args.accept:
        try:
            run = subprocess.run(['wsl.exe', '-d', 'Ubuntu', '--exec', 'bash', '/root/albion-pi/accept.sh'],
                                 capture_output=True, timeout=1200)
            text = run.stdout.decode('utf-8', errors='replace')
            report['accept'] = {'exit': run.returncode,
                                'pass': len(re.findall(r'^\[PASS\]', text, re.M)),
                                'fail': len(re.findall(r'^\[FAIL\]', text, re.M))}
        except subprocess.TimeoutExpired:
            report['accept'] = {'timeout': True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    failures = 0
    if args.health:
        failures += sum(report.get(env, {}).get(route) != 200 for env in ('windows', 'wsl') for route in ('health', 'ready'))
    if args.doctor:
        failures += report['doctor']['exit'] != 0
    if args.accept:
        failures += report['accept'].get('exit') != 0 or report['accept'].get('fail', 1) != 0
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
