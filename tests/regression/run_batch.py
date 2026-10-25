#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""批次回归入口（K6）：只跑离线、确定性的检查，结果写进 tests/output/regression/<批次>/。

用法（任何 cwd 都能跑，路径按本文件定位）：
    python tests/regression/run_batch.py --list
    python tests/regression/run_batch.py --suite tts-unit
    python tests/regression/run_batch.py --all
    python tests/regression/run_batch.py --all --python .\tts\irodori\.venv\Scripts\python.exe --out tests/output/regression/my-batch

约束：不启动或重启任何服务，不联网，不写仓库内跟踪文件；每条命令的完整输出落在 <out>/<suite>.log。
`--out` 只能落在 tests/output/ 之内（生成结果只写 tests/output/），越界或建不出来时报错并返回 2。
真实服务、模型、GPU、Electron 页面相关的用例不在这里跑，见同目录 README.md。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_ROOT = ROOT / "tests" / "output"
PY = sys.executable


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_unittest(text: str) -> dict:
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    skip = re.search(r"^OK \((?:.*skipped=(\d+).*)\)", text, re.M)
    failed = re.search(r"^FAILED \((.*)\)", text, re.M)
    out = {"total": int(ran.group(1)) if ran else None, "skipped": int(skip.group(1)) if skip else 0}
    if failed:
        out["failed"] = failed.group(1)
    elif ran:
        out["failed"] = 0
    return out


def parse_bat(text: str) -> dict:
    m = re.search(r"(\d+)/(\d+) 通过", text)
    if not m:
        return {}
    ok, total = int(m.group(1)), int(m.group(2))
    return {"total": total, "failed": total - ok}


def parse_repo(text: str) -> dict:
    m = re.search(r'"layout_errors": (\d+)', text)
    c = re.search(r'"files_checked": (\d+)', text)
    if not m:
        return {}
    return {"files": int(c.group(1)) if c else None, "failed": int(m.group(1))}


def parse_selftest(text: str) -> dict:
    if "SELFTEST OK" in text:
        return {"failed": 0}
    return {"failed": 1}


def parse_vitest(text: str) -> dict:
    failed = re.search(r"Test Files\s+(\d+) failed", text)
    passed = re.search(r"Test Files\s+(\d+) passed", text)
    if failed:
        return {"total": int(failed.group(1)) + (int(passed.group(1)) if passed else 0),
                "failed": int(failed.group(1))}
    if passed:
        return {"total": int(passed.group(1)), "failed": 0}
    return {}


def find_node25(configured: str | None = None) -> tuple[str | None, str]:
    """Use an explicit Node path, then probe PATH; accept only Node 25.x."""
    candidates = [configured or os.environ.get("ALBION_NODE25_PATH"), shutil.which("node")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            probe = subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            continue
        version = decode(probe.stdout).strip()
        if probe.returncode == 0 and re.match(r"^v25\.", version):
            return candidate, version
    where = "显式配置 ALBION_NODE25_PATH/--node 或 PATH"
    return None, "未找到可用的 Node 25（{}）；当前 PATH 的 node={}".format(where, shutil.which("node") or "无")


def shell_vitest_cmd(node: str) -> list[str]:
    vitest = sorted((ROOT / "shell" / "node_modules" / ".pnpm").glob(
        "vitest@*/node_modules/vitest/vitest.mjs"))
    return [node, str(vitest[0]), "run", "--maxWorkers=2"]


SUITES = [
    {
        "name": "repo-layout",
        "what": "仓库分类门禁 check_repository.py",
        "cmd": lambda py: [py, "-I", "tools/check_repository.py"],
        "needs": ["tools/check_repository.py"],
        "parse": parse_repo,
    },
    {
        "name": "tts-unit",
        "what": "tests/tts 全部 unittest（CPU/mock）",
        "cmd": lambda py: [py, "-B", "-m", "unittest", "discover", "-s", "tests/tts", "-p", "test_*.py"],
        "needs": ["tests/tts"],
        "parse": parse_unittest,
    },
    {
        "name": "tts-translate-rules",
        "what": "翻译层规则自检（K5 的 TestCase）",
        "cmd": lambda py: [py, "-I", "tests/tts/test_translate_rules.py"],
        "needs": ["tests/tts/test_translate_rules.py"],
        "parse": parse_unittest,
    },
    {
        "name": "pet-bat",
        "what": "根目录 .bat 的 GBK + CRLF 检查",
        "cmd": lambda py: [py, "-I", "pet/tools/check_bat.py"],
        "needs": ["pet/tools/check_bat.py"],
        "parse": parse_bat,
    },
    {
        "name": "pi-brain-guard",
        "what": "脑端整理器自检 test_guard_ext.ts（纯 TS，无网络）",
        "cmd": lambda py: ["node", "integrations/pi-brain/bin/test_guard_ext.ts"],
        "needs": ["integrations/pi-brain/bin/test_guard_ext.ts"],
        "parse": parse_selftest,
    },
    {
        "name": "pi-brain-shell-tools",
        "what": "壳插件工具桥 shell_tools.py + server.py 接线（本机回环 HTTP，不起 Pi；Windows 需 tzdata）",
        "cmd": lambda py: [py, "-B", "-m", "unittest", "discover", "-s", "tests/pi-brain", "-p", "test_shell_tools.py"],
        "needs": ["tests/pi-brain/test_shell_tools.py"],
        "parse": parse_unittest,
    },
    {
        "name": "pi-brain-shell-tools-ext",
        "what": "albion-shell-tools 代理工具同步（假 Pi 工具表，纯 TS）",
        "cmd": lambda py: ["node", "tests/pi-brain/check_shell_tools_ext.ts"],
        "needs": ["tests/pi-brain/check_shell_tools_ext.ts"],
        "parse": parse_selftest,
    },
    {
        "name": "shell-vitest",
        "what": "壳 Vitest 全套（Node；最多 2 个 worker）",
        "cmd": shell_vitest_cmd,
        "cwd": "shell",
        "needs": ["shell/package.json"],
        "parse": parse_vitest,
    },
]


def suite_by_name(name: str) -> dict:
    for s in SUITES:
        if s["name"] == name:
            return s
    raise SystemExit("unknown suite: " + name)


class OutDirError(Exception):
    """--out 越界或不可用；main 捕获后打印清楚原因并返回 2。"""


def resolve_out_dir(raw) -> pathlib.Path:
    """把 --out 解析成 tests/output/ 下的绝对路径。

    默认 tests/output/regression/<时间戳>；相对路径按仓库根解析；`..` 只会被
    词法收敛，真正是否越界按 resolve() 后的真实路径判断（能挡住符号链接/联接）。
    """
    if raw is None:
        out = OUT_ROOT / "regression" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        out = pathlib.Path(raw).expanduser()
        if not out.is_absolute():
            out = ROOT / out
        out = pathlib.Path(os.path.normpath(str(out)))
    allowed = OUT_ROOT.resolve()
    try:
        real = out.resolve()
    except OSError as exc:
        raise OutDirError("无法解析 --out 路径 {!r}: {}".format(str(raw), exc))
    if allowed not in real.parents:
        raise OutDirError(
            "--out 只能写在 {}/ 之内，收到 {!r}（解析为 {}）".format(
                OUT_ROOT.relative_to(ROOT).as_posix(), str(raw), real))
    return out


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """Stop the timed-out command and all descendants it created."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_one(suite: dict, out_dir: pathlib.Path, python: str, timeout: int, node: str | None = None) -> dict:
    missing = [p for p in suite["needs"] if not (ROOT / p).exists()]
    if suite["name"] == "shell-vitest":
        vitest_files = list((ROOT / "shell" / "node_modules" / ".pnpm").glob("vitest@*/node_modules/vitest/vitest.mjs"))
        if not node:
            _, reason = find_node25()
            missing.append(reason)
        if not vitest_files:
            missing.append("shell Vitest 未安装（缺少 shell/node_modules/.pnpm/vitest@*/.../vitest.mjs）")
    if missing:
        return {"name": suite["name"], "what": suite["what"], "status": "skipped",
                "reason": "缺少 " + ", ".join(missing), "counts": {}}
    cmd = suite["cmd"](python)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    started = dt.datetime.now()
    if suite["name"] == "shell-vitest":
        cmd = suite["cmd"](node)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=ROOT / suite.get("cwd", "."), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, start_new_session=(os.name != "nt"),
                            creationflags=flags)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc)
        stdout, stderr = proc.communicate()
    seconds = round((dt.datetime.now() - started).total_seconds(), 1)
    text = decode(stdout) + decode(stderr)
    log = out_dir / (suite["name"] + ".log")
    log.write_text(text, encoding="utf-8")
    counts = suite["parse"](text) or {}
    ok = not timed_out and proc.returncode == 0 and counts.get("failed") == 0
    if timed_out:
        text = "TIMEOUT: 超过 {} 秒，已终止子进程树。\n".format(timeout) + text
        log.write_text(text, encoding="utf-8")
    return {"name": suite["name"], "what": suite["what"], "status": "timeout" if timed_out else ("pass" if ok else "fail"),
            "cmd": " ".join(cmd), "exit": proc.returncode, "seconds": seconds,
            **({"reason": "超过 {} 秒，已终止子进程树".format(timeout)} if timed_out else {}),
            "counts": counts, "log": log.relative_to(ROOT).as_posix()}


def main() -> int:
    ap = argparse.ArgumentParser(description="批次回归入口（只读、离线）")
    ap.add_argument("--list", action="store_true", help="列出可跑的套件")
    ap.add_argument("--suite", action="append", default=[], help="只跑指定套件，可重复")
    ap.add_argument("--all", action="store_true", help="跑全部套件（默认）")
    ap.add_argument("--python", default=PY, help="跑 Python 套件的解释器（默认当前解释器）")
    ap.add_argument("--out", default=None,
                    help="输出目录，必须落在 tests/output/ 内；默认 tests/output/regression/<时间戳>")
    ap.add_argument("--timeout", type=int, default=1800, help="单条命令超时（秒）")
    ap.add_argument("--node", default=None, help="Node 25 可执行文件路径（也可设 ALBION_NODE25_PATH）")
    args = ap.parse_args()

    if args.list:
        for s in SUITES:
            print("{:20s} {:s}".format(s["name"], s["what"]))
            print("{:20s} 需要: {}".format("", ", ".join(s["needs"])))
        return 0

    names = args.suite or [s["name"] for s in SUITES]
    try:
        out_dir = resolve_out_dir(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
    except OutDirError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("error: 无法创建输出目录 {!r}: {}".format(args.out, exc), file=sys.stderr)
        return 2

    results = []
    node, node_reason = find_node25(args.node)
    for name in names:
        suite = suite_by_name(name)
        print("== " + name + " ...", flush=True)
        result = run_one(suite, out_dir, args.python, args.timeout, node=node)
        results.append(result)
        print("   " + result["status"] + "  " + json.dumps(result.get("counts", {}), ensure_ascii=False), flush=True)

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    summary = {
        "head": head,
        "worktree_dirty": bool(dirty.strip()),
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "python": args.python,
        "suites": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = sum(1 for r in results if r["status"] == "pass")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] == "fail")
    timed_out = sum(1 for r in results if r["status"] == "timeout")
    print("\n通过 {} / 失败 {} / 跳过 {} → {}".format(passed, failed, skipped, out_dir.relative_to(ROOT).as_posix()))
    if not node:
        print("shell-vitest 跳过原因：" + node_reason)
    return 0 if failed == 0 and timed_out == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
