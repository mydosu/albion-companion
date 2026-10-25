# -*- coding: utf-8 -*-
"""阿尔比恩桌宠 · 八项自检（start_all.bat 收尾会调它）

用法:
  D:/AI/Wife/tts/GPT-sovits/runtime/python.exe check_all.py [等待秒数] [--json]

默认最多等 90 秒（Irodori 后端要加载模型，起得慢）。每 5 秒轮询一次，全绿就提前结束。
--json：只在 stdout 输出一份 JSON（ok / ready / total / checks[{name, ok, detail}]），不打印表格；退出码不变。
脑的探活带 key（不带 key 会在网关日志里留 "rejected invalid API key"，见 docs/history/pet-ROADMAP.md 17g）。
"""
import json
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.request

try:
    import locale
    sys.stdout.reconfigure(encoding=locale.getpreferredencoding(False) or 'utf-8', errors='replace')
except Exception:
    pass

HERE = pathlib.Path(__file__).parent
AS_JSON = '--json' in sys.argv[1:]
_args = [a for a in sys.argv[1:] if a != '--json']
WAIT_S = int(_args[0]) if _args else 90
SHELL_ENV = HERE.parent / 'shell' / 'apps' / 'stage-tamagotchi' / '.env.local'


def vision_base():
    # 视觉地址和壳共用一份本机配置（不进仓库），环境变量优先
    import os
    v = os.environ.get('VITE_ALBION_VISION_BASE', '')
    if not v and SHELL_ENV.exists():
        m = re.search(r'^VITE_ALBION_VISION_BASE=(.+)$', SHELL_ENV.read_text(encoding='utf-8', errors='ignore'), re.M)
        v = m.group(1).strip() if m else ''
    return v.rstrip('/')


def tcp_open(port, host='127.0.0.1', timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http(url, headers=None, timeout=8.0):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, ''
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def api_key():
    # 凭据在 WSL 的 /root/albion-pi/agent/.env（Windows 侧 Hermes 时代的 .env 已不存在，不再读）
    try:
        out = subprocess.run(
            ['wsl', '-d', 'Ubuntu', '-e', 'bash', '-lc',
             "grep -m1 '^API_SERVER_KEY=' /root/albion-pi/agent/.env | cut -d= -f2-"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return (out.stdout or '').strip().strip('"').strip("'")
    except Exception:
        return ''


KEY = api_key()


def stream_plan_version_row():
    """流式 TTS :9885 的 /health 必须带 config.plan_version；缺了说明跑的是不认版本的旧服务。"""
    st, body = http('http://127.0.0.1:9885/health')
    if st != 200:
        return ('流式TTS :9885', False, f'HTTP {st}' if st else f'连不上 {body}')
    try:
        pv = json.loads(body).get('config', {}).get('plan_version')
    except Exception:
        pv = None
    if pv is None:
        return ('流式TTS :9885', False, '/health 没有 config.plan_version（旧版服务？重启 start_stream.bat 后再查）')
    return ('流式TTS :9885', True, f'plan_version={pv}')


def collect():
    rows = []

    ok = tcp_open(9889)
    rows.append(('Irodori 后端 :9889', ok, 'TCP 可连' if ok else '连不上'))

    st, body = http('http://127.0.0.1:9881/v1/models')
    models = ''
    if st == 200:
        try:
            models = '、'.join(m.get('id', '') for m in json.loads(body).get('data', [])[:6])
        except Exception:
            pass
    rows.append(('嘴·TTS 桥 :9881', st == 200, f'HTTP {st} {models}'.strip()))

    rows.append(stream_plan_version_row())

    st, body = http('http://127.0.0.1:9882/health')
    model = ''
    if st == 200:
        try:
            d = json.loads(body)
            model = f"{pathlib.Path(d.get('model', '')).name} loaded={d.get('loaded')}"
        except Exception:
            pass
    rows.append(('耳·STT :9882', st == 200, f'HTTP {st} {model}'.strip()))

    st, _ = http('http://127.0.0.1:8650/v1/models', {'Authorization': f'Bearer {KEY}'})
    rows.append(('脑·pi :8650', st == 200, f'HTTP {st}' + ('' if KEY else '（没找到 API_SERVER_KEY）')))

    vb = vision_base()
    st, _ = http(f'{vb}/models', timeout=8) if vb else (0, '')
    rows.append(('眼·视觉模型', st == 200, (f'HTTP {st}' + ('' if st == 200 else '（视觉那台没开？）')) if vb else f'没配地址（{SHELL_ENV.name} 里的 VITE_ALBION_VISION_BASE）'))

    ok = tcp_open(8790)
    rows.append(('眼·截图/摄像头采集端 :8790', ok, 'TCP 可连' if ok else '连不上（一键启动会拉起）'))

    st, body = http('http://127.0.0.1:9335/json/version', timeout=5)
    browser = ''
    if st == 200:
        m = re.search(r'"Browser"\s*:\s*"([^"]+)"', body)
        browser = m.group(1) if m else ''
    rows.append(('桌宠壳 :9335', st == 200, browser or f'HTTP {st}'))

    return rows


deadline = time.time() + WAIT_S
rows = collect()
while not all(r[1] for r in rows) and time.time() < deadline:
    time.sleep(5)
    rows = collect()

if AS_JSON:
    ready = sum(1 for r in rows if r[1])
    print(json.dumps({'ok': ready == len(rows), 'ready': ready, 'total': len(rows),
                      'checks': [{'name': n, 'ok': bool(g), 'detail': d} for n, g, d in rows]},
                     ensure_ascii=True, indent=2))
    sys.exit(0 if ready == len(rows) else 1)

print('--------------------------------------------------')
ok = 0
for name, good, detail in rows:
    print(('[OK] ' if good else '[--] ') + name.ljust(22) + detail)
    ok += 1 if good else 0
print('--------------------------------------------------')
print(f'  {ok}/{len(rows)} 就绪' + ('' if ok == len(rows) else '（缺的看对应窗口的日志）'))
sys.exit(0 if ok == len(rows) else 1)
