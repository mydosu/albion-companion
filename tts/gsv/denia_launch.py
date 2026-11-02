# -*- coding: utf-8 -*-
"""Denia 后端启动器（包住上游 GPT-SoVITS 的 api_v2.py）。

为什么需要它：GSV 便携 runtime 用 `runtime/python39._pth` 钉死搜索路径，_pth 模式下
PYTHONPATH 会被忽略，直接跑 api_v2.py 会 `ModuleNotFoundError: No module named 'tools'`。
这里先把 **GSV 安装目录** 加进 sys.path，再从那里执行 api_v2.py。

GSV 安装目录（含 api_v2.py、GPT_SoVITS/、runtime/、权重目录）：
  环境变量 ALBION_GSV_ROOT；没设就用 <仓库根>/tts/GPT-sovits（整目录忽略，不入库）。
本文件只负责「接上」，上游代码与权重都不在仓库里；见 tts/gsv/README.md。

用法（相对配置路径按 GSV 根解析，所以这里会 chdir 过去）：
    <GSV_ROOT>\\runtime\\python.exe <仓库>\\tts\\gsv\\denia_launch.py -c <仓库>\\tts\\gsv\\tts_infer_denia.yaml -a 127.0.0.1 -p 9880
"""
import os
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GSV_ROOT = Path(os.environ.get("ALBION_GSV_ROOT") or (REPO_ROOT / "tts" / "GPT-sovits")).resolve()

if not (GSV_ROOT / "api_v2.py").is_file():
    sys.exit(
        "[denia] 找不到 %s\\api_v2.py。把 ALBION_GSV_ROOT 指到 GPT-SoVITS 安装目录"
        "（默认 <仓库>\\tts\\GPT-sovits），或照 tts/gsv/README.md 装一份。" % GSV_ROOT
    )

os.chdir(GSV_ROOT)
if str(GSV_ROOT) not in sys.path:
    sys.path.insert(0, str(GSV_ROOT))

TARGET = str(GSV_ROOT / "api_v2.py")
sys.argv = [TARGET] + sys.argv[1:]
runpy.run_path(TARGET, run_name="__main__")
