# -*- coding: utf-8 -*-
"""Denia 后端启动器（包住上游 GPT-SoVITS 的 api_v2.py）。

为什么需要它：GSV 便携 runtime 用 `runtime/python39._pth` 钉死搜索路径，_pth 模式下
PYTHONPATH 会被忽略，直接跑 api_v2.py 会 `ModuleNotFoundError: No module named 'tools'`。
这里先把 **GSV 安装目录** 加进 sys.path，再从那里执行 api_v2.py。

GSV 安装目录（含 api_v2.py、GPT_SoVITS/、runtime/、权重目录）：
  环境变量 ALBION_GSV_ROOT；没设就用 <仓库根>/tts/GPT-sovits（整目录忽略，不入库）。
本文件只负责「接上」，上游代码与权重都不在仓库里；见 tts/gsv/README.md。

设备：环境变量 `ALBION_GSV_DEVICE`（默认 cuda）覆盖推理配置里的 `custom.device`，
`is_half` 跟着走（cuda → true，cpu → false）。**入库的 tts_infer_denia.yaml 一个字节都不改**：
override 只写进系统临时目录里的一份副本，`-c` 换成那份副本的路径。上游 api_v2 的
`TTS_Config` 只读 `configs["custom"]`，所以只改 custom 段（其余版本段原样保留）。

用法（相对配置路径按 GSV 根解析，所以这里会 chdir 过去）：
    <GSV_ROOT>\\runtime\\python.exe <仓库>\\tts\\gsv\\denia_launch.py -c <仓库>\\tts\\gsv\\tts_infer_denia.yaml -a 127.0.0.1 -p 9880
"""
import os
import re
import runpy
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GSV_ROOT = Path(os.environ.get("ALBION_GSV_ROOT") or (REPO_ROOT / "tts" / "GPT-sovits")).resolve()

DEVICE = (os.environ.get("ALBION_GSV_DEVICE") or "cuda").strip() or "cuda"
# 上游的 TTS_Config 自己也会在 cpu+is_half 时打警告并把 is_half 压回 False；
# 这里提前按设备算好，配置文件和日志里看到的就是实际生效的值。
IS_HALF = DEVICE.strip().lower() != "cpu"


def override_device(text, device, is_half):
    """把 custom 段的 device/is_half 换掉，返回新的 yaml 文本（纯函数，方便离线测试）。

    只动 `custom:` 块里、行首缩进的那两行；块外的 device/is_half（v1…v4 段）原样保留。
    缺 is_half 时补一行，缺 device 时插在 custom 段开头。
    """
    out, in_custom, done = [], False, {"device": False, "is_half": False}
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):] or "\n"
        stripped = body.strip()
        top_level = bool(stripped) and not body[:1].isspace() and not stripped.startswith("#")
        if top_level:
            in_custom = stripped.rstrip() == "custom:"
        if in_custom and not top_level:
            m = re.match(r"^(\s+)device\s*:", body)
            if m:
                out.append("%sdevice: %s%s" % (m.group(1), device, eol))
                done["device"] = True
                continue
            m = re.match(r"^(\s+)is_half\s*:", body)
            if m:
                out.append("%sis_half: %s%s" % (m.group(1), "true" if is_half else "false", eol))
                done["is_half"] = True
                continue
        out.append(line)
    text = "".join(out)
    if not done["device"]:
        print("[denia] 配置里没有 custom.device，按原样使用（ALBION_GSV_DEVICE 未生效）")
        return text
    if not done["is_half"]:
        # custom 段存在但没有 is_half：紧跟 device 补一行
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if re.match(r"^\s+device\s*:", line):
                indent = re.match(r"^(\s+)", line).group(1)
                eol = "\r\n" if line.endswith("\r\n") else "\n"
                lines.insert(i + 1, "%sis_half: %s%s"
                             % (indent, "true" if is_half else "false", eol))
                break
        text = "".join(lines)
    return text


def config_path_from_argv(argv):
    """找 `-c` / `--tts_config` 的值（没给返回 None）。"""
    for i, arg in enumerate(argv):
        if arg in ("-c", "--tts_config") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--tts_config="):
            return arg.split("=", 1)[1]
    return None


def prepare_config(src, device, is_half):
    """把 override 后的配置写到临时文件，返回新路径（入库的 yaml 不动）。"""
    try:
        text = Path(src).read_text(encoding="utf-8")
    except OSError as e:
        print("[denia] 读不到推理配置 %s（%s），设备按上游默认" % (src, e), flush=True)
        return src
    patched = override_device(text, device, is_half)
    if patched == text:
        return src
    # 固定名字（按设备区分）覆盖写：反复重启不留一堆临时文件
    tag = re.sub(r"[^0-9A-Za-z]+", "_", device) or "cuda"
    dst = Path(tempfile.gettempdir()) / ("albion_denia_%s.yaml" % tag)
    dst.write_text(patched, encoding="utf-8")
    return str(dst)


def main():
    if not (GSV_ROOT / "api_v2.py").is_file():
        sys.exit(
            "[denia] 找不到 %s\\api_v2.py。把 ALBION_GSV_ROOT 指到 GPT-SoVITS 安装目录"
            "（默认 <仓库>\\tts\\GPT-sovits），或照 tts/gsv/README.md 装一份。" % GSV_ROOT
        )

    # `-c` 可能在 chdir 之后才被上游按相对路径打开：先按调用方的 cwd 解析成绝对路径
    cfg_arg = config_path_from_argv(sys.argv)
    if cfg_arg:
        cfg_path = str(Path(cfg_arg).resolve())
        patched = prepare_config(cfg_path, DEVICE, IS_HALF)
        if patched != cfg_path:
            for i, arg in enumerate(sys.argv):
                if arg in ("-c", "--tts_config") and i + 1 < len(sys.argv):
                    sys.argv[i + 1] = patched
                elif arg.startswith("--tts_config="):
                    sys.argv[i] = "--tts_config=" + patched
            print("[denia] device=%s is_half=%s（ALBION_GSV_DEVICE 覆盖 -> %s；入库 yaml 不动）"
                  % (DEVICE, IS_HALF, patched), flush=True)
        else:
            print("[denia] 沿用原配置 %s（没读到 custom 段或读文件失败，见上一行）" % cfg_path, flush=True)
    else:
        print("[denia] 没给 -c，设备由上游默认配置决定（ALBION_GSV_DEVICE 未生效）", flush=True)

    os.chdir(GSV_ROOT)
    if str(GSV_ROOT) not in sys.path:
        sys.path.insert(0, str(GSV_ROOT))

    TARGET = str(GSV_ROOT / "api_v2.py")
    sys.argv = [TARGET] + sys.argv[1:]
    runpy.run_path(TARGET, run_name="__main__")


if __name__ == "__main__":
    main()
