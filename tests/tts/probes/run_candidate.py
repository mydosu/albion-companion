"""Isolated ports with explicit warmup; reuse local assets read-only, never load a second GPU model."""
import argparse
import os
from pathlib import Path
import sys
import uvicorn

ROOT = Path(__file__).resolve().parents[3]

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("service", choices=["backend", "bridge", "stream"])
    p.add_argument("--assets", required=True, type=Path)
    p.add_argument("--port", type=int)
    a = p.parse_args()
    sys.path[:0] = [str(ROOT/"tts/irodori"), str(ROOT/"tts/stream")]
    # infer.py / irodori_tts come from tracked candidate source, weights from local assets.
    if a.service == "backend":
        import irodori_serve as m
        m.ROOT = str(a.assets.resolve())
        m.CKPT = str(a.assets.resolve()/"checkpoints/model.safetensors")
        m.SHIPS_JSON = str(a.assets.resolve()/"ships.json")
        os.chdir(a.assets.resolve())
        m._warmup()
        app, port = m.app, 19889
    elif a.service == "bridge":
        import bridge as m
        m.BACKEND = "http://127.0.0.1:19889/tts"
        m.BACKEND_STREAM = "http://127.0.0.1:19889/tts_stream"
        app, port = m.app, 19881
    else:
        import stream_server as m
        app = m.create_app(m.Config(bridge_url="http://127.0.0.1:19881", irodori_url="http://127.0.0.1:19889"))
        port = 19885
    uvicorn.run(app, host="127.0.0.1", port=a.port or port, log_level="warning")
