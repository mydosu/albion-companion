# -*- coding: utf-8 -*-
"""denia 后端启动器（GSV api_v2）。

为什么要它：这套 runtime 是便携版 Python，`runtime/python39._pth` 钉死了搜索路径
（且 ._pth 模式下 PYTHONPATH 会被忽略），所以直接跑 api_v2.py 会
`ModuleNotFoundError: No module named 'tools'`。这里显式把仓库根加入 sys.path 再运行。
用法与 api_v2.py 完全一致，例如：
    .\\runtime\\python.exe denia_launch.py -c GPT_SoVITS/configs/tts_infer_denia.yaml -a 127.0.0.1 -p 9880
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TARGET = os.path.join(ROOT, "api_v2.py")
sys.argv = [TARGET] + sys.argv[1:]
runpy.run_path(TARGET, run_name="__main__")
