# -*- coding: utf-8 -*-
"""一次性：向 TTS 桥请求 albion 音色的日语语音（UTF-8 安全）。"""
import json, os, sys, time, urllib.request
from pathlib import Path

TEXT = sys.argv[1] if len(sys.argv) > 1 else "今の気持ちは、指揮官に会えたうれしさでいっぱいです。アルビオン、ずっとここで待っていましたから。"
OUT_DIR = str(Path(__file__).resolve().parents[2] / "output" / "tts" / "mood")
os.makedirs(OUT_DIR, exist_ok=True)
out = os.path.join(OUT_DIR, "tts_albion_mood_%s.wav" % time.strftime("%Y%m%d_%H%M%S"))

payload = json.dumps({"input": TEXT, "voice": "albion"}).encode("utf-8")
req = urllib.request.Request("http://127.0.0.1:9881/v1/audio/speech", data=payload,
                             headers={"Content-Type": "application/json; charset=utf-8"})
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
        styles = r.headers.get("X-Irodori-Styles", "")
    with open(out, "wb") as f:
        f.write(data)
    print("OK", out, len(data), "styles=", styles)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode("utf-8", "ignore")[:400])
