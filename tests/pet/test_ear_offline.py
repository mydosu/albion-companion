# -*- coding: utf-8 -*-
"""耳朵（pet/whisper_serve.py）离线单测：mock 模型，不加载 funasr/whisper。

用法（GSV runtime 自带 fastapi + httpx）:
  D:/AI/Wife/tts/GPT-sovits/runtime/python.exe tests/pet/test_ear_offline.py -v

覆盖：加载失败回 503 且 /health 带出错误；加载成功后正常识别；
并发只加载一次；推理期间 /health 不被卡住。
"""
import importlib.util
import os
import pathlib
import threading
import time
import unittest

from fastapi.testclient import TestClient

os.environ['ALBION_STT_ENGINE'] = 'paraformer'
_SRC = pathlib.Path(__file__).resolve().parents[2] / 'pet' / 'whisper_serve.py'
_spec = importlib.util.spec_from_file_location('whisper_serve', _SRC)
ear = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ear)


class FakeModel:
    def __init__(self, gate=None, entered=None):
        self.gate, self.entered = gate, entered

    def generate(self, input, **kw):
        if self.entered:
            self.entered.set()
        if self.gate:
            self.gate.wait(10)
        return [{'text': '你好阿尔比恩'}]


def post(client):
    return client.post('/v1/audio/transcriptions', files={'file': ('a.wav', b'RIFFxxxx')}, data={'model': 'whisper-1'})


class EarTest(unittest.TestCase):
    def setUp(self):
        ear._model, ear._load_error = None, None
        self._orig = ear._load_model

    def tearDown(self):
        ear._load_model = self._orig
        ear._model, ear._load_error = None, None

    def test_load_failure_returns_503_then_recovers(self):
        def boom():
            raise RuntimeError('model dir missing')
        ear._load_model = boom
        c = TestClient(ear.app)
        r = post(c)
        self.assertEqual(r.status_code, 503)
        self.assertIn('model dir missing', r.json()['detail'])
        h = c.get('/health').json()
        self.assertFalse(h['loaded'])
        self.assertIn('model dir missing', h['error'])

        ear._load_model = FakeModel   # 下一次请求重试加载
        r = post(c)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'text': '你好阿尔比恩'})
        h = c.get('/health').json()
        self.assertTrue(h['loaded'])
        self.assertIsNone(h['error'])

    def test_concurrent_load_happens_once(self):
        calls = []

        def slow_load():
            calls.append(1)
            time.sleep(0.2)
            return FakeModel()
        ear._load_model = slow_load
        ts = [threading.Thread(target=ear.get_model) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(calls), 1)

    def test_health_not_blocked_during_inference(self):
        gate, entered = threading.Event(), threading.Event()
        ear._model = FakeModel(gate, entered)
        with TestClient(ear.app) as c:   # 同一个事件循环
            result = {}
            a = threading.Thread(target=lambda: result.setdefault('post', post(c)))
            a.start()
            self.assertTrue(entered.wait(5))
            b = threading.Thread(target=lambda: result.setdefault('health', c.get('/health')))
            b.start()
            b.join(5)
            alive = b.is_alive()
            gate.set()
            a.join(10)
            b.join(5)
        self.assertFalse(alive, '/health 被推理卡住了')
        self.assertEqual(result['health'].status_code, 200)
        self.assertEqual(result['post'].json(), {'text': '你好阿尔比恩'})


if __name__ == '__main__':
    unittest.main()
