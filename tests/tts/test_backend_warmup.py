"""离线检查启动预热：生产规划不漂移，成功和失败都清理临时 WAV。"""
import ast
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

IRODORI = Path(__file__).resolve().parents[2] / "tts" / "irodori"
sys.path.insert(0, str(IRODORI))
import serve_v2
import render_queue
from fastapi.testclient import TestClient


class BackendWarmupTests(unittest.TestCase):
    def test_backend_registers_production_routes_once_without_loading_model(self):
        class Runtime:
            from_key = staticmethod(lambda key: None)

        spec = importlib.util.spec_from_file_location("offline_irodori_backend", IRODORI / "irodori_serve.py")
        backend = importlib.util.module_from_spec(spec)
        cwd, paths = os.getcwd(), sys.path[:]
        try:
            with patch.dict(sys.modules, {"infer": SimpleNamespace(InferenceRuntime=Runtime)}), \
                 patch.object(render_queue, "TOKEN_COUNT", None), patch.dict(os.environ):
                spec.loader.exec_module(backend)
        finally:
            os.chdir(cwd)
            sys.path[:] = paths
        for path in ("/tts", "/tts_stream", "/v2/render", "/v2/plan", "/health"):
            routes = [r for r in backend.app.routes if r.path == path]
            self.assertEqual(len(routes), 1, path)
            if path in ("/tts", "/tts_stream"):
                self.assertEqual(routes[0].endpoint.__module__, "serve_v2")
        voices = []

        def synth(unit, output):
            voices.append(unit["voice"])
            return 10, [0.0] * 10

        backend._queue._synth = synth
        backend._queue._encode = lambda wave, sr, fmt: b"offline audio"
        with TestClient(backend.app) as client:
            for path in ("/tts", "/tts_stream"):
                response = client.post(path, json={"text": "こんにちは。", "voice": "taihou"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["x-irodori-ship"], "taihou")
        self.assertEqual(voices, ["taihou", "taihou"])

    def run_warmup(self, fail=False):
        # 只执行预热函数；不导入模型、不访问服务或本地角色资产。
        tree = ast.parse((IRODORI / "irodori_serve.py").read_text(encoding="utf-8"))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_warmup")
        outputs = []

        def synth(unit, output, infer, ship):
            outputs.append((unit, output, ship))
            Path(output).write_bytes(b"fake wav")
            if fail:
                raise RuntimeError("synthesis failed")

        ns = {"os": os, "tempfile": tempfile, "_sv2": serve_v2,
              "_rq": SimpleNamespace(run_infer_synth=synth), "_run_infer": object(),
              "DEFAULT_SHIP": "albion"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "warmup", "exec"), ns)
        if fail:
            with self.assertRaisesRegex(RuntimeError, "synthesis failed"):
                ns["_warmup"]()
        else:
            ns["_warmup"]()
        self.assertEqual(len(outputs), 1)
        unit, output, ship = outputs[0]
        self.assertEqual((unit["text"], unit["family"], unit["cfg"], ship),
                         ("こんにちは。", "neutral", 3.0, "albion"))
        self.assertEqual(unit["caption"], "落ち着いた自然な声で、丁寧に")
        self.assertFalse(Path(output).parent.exists())

    def test_warmup_uses_production_plan_and_cleans_output(self):
        self.run_warmup()

    def test_failed_warmup_cleans_output_and_fails_startup(self):
        self.run_warmup(fail=True)


if __name__ == "__main__":
    unittest.main()
