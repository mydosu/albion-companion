# -*- coding: utf-8 -*-
"""耳朵（whisper_serve.py）验收：三种输入都要给出正确回应，且不 500。

用法（服务已在 :9882 跑着时）:
  D:/AI/Wife/tts/GPT-sovits/runtime/python.exe check_ear.py

检查项：
  1) 正常中文语音 → 文本非空且包含关键词
  2) 空文件       → 200 + {"text": ""}（空录音=没听见，不该报错）
  3) 垃圾字节     → 200 + {"text": ""}
"""
import io
import json
import pathlib
import sys
import urllib.request

EAR = 'http://127.0.0.1:9882/v1/audio/transcriptions'
ZH_WAV = pathlib.Path(__file__).parent / 'data' / '_zh_test.wav'
failures = []


def post(name: str, payload: bytes, filename: str = 'a.wav', language: str = 'zh') -> tuple[int, dict]:
    boundary = '----albioncheck'
    body = io.BytesIO()
    for field, value in (('model', 'whisper-1'), ('language', language)):
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
               f'Content-Type: application/octet-stream\r\n\r\n'.encode())
    body.write(payload)
    body.write(f'\r\n--{boundary}--\r\n'.encode())
    req = urllib.request.Request(EAR, data=body.getvalue(),
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        return e.code, {'text': e.read().decode('utf-8', 'ignore')[:120]}


def check(name: str, cond: bool, detail: str):
    print(('  ✓ ' if cond else '  ✗ ') + f'{name}：{detail}')
    if not cond:
        failures.append(name)


# 1) 正常中文
if not ZH_WAV.exists():
    print('  ! 缺 _zh_test.wav（用 Windows 中文语音合成一句即可），跳过第 1 项')
else:
    code, data = post('zh', ZH_WAV.read_bytes(), 'zh.wav')
    text = str(data.get('text', ''))
    check('正常中文', code == 200 and '阿尔比恩' in text, f'HTTP {code} → {text!r}')

# 2) 空文件 / 3) 垃圾字节
for name, payload in (('空文件', b''), ('垃圾字节', bytes(range(256)) * 16)):
    code, data = post(name, payload, 'x.bin')
    check(name, code == 200 and data.get('text') == '', f'HTTP {code} → {data!r}')

print('\n结果：' + ('全部通过 ✓' if not failures else f'{len(failures)} 项不过 ✗ {failures}'))
sys.exit(1 if failures else 0)
