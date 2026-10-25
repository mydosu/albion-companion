#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查 .bat 是否还是 GBK + CRLF（编辑器/脚本改坏换行会让 cmd 整份报"不是内部或外部命令"）。

用法：python check_bat.py [目录或文件...]        默认检查本目录下所有 .bat
"""
import sys
from pathlib import Path


def check(path: Path) -> list[str]:
    raw = path.read_bytes()
    bad = []
    crlf = raw.count(b"\r\n")
    bare = raw.count(b"\n") - crlf
    if bare:
        bad.append(f"{bare} 个裸 LF（cmd 会把这些行当坏命令）")
    try:
        raw.decode("gbk")
    except UnicodeDecodeError as e:
        bad.append(f"不是 GBK（{e}）")
    if not crlf:
        bad.append("没有任何 CRLF")
    return bad


def main() -> int:
    args = sys.argv[1:] or ["."]
    files, failed = [], 0
    for a in args:
        p = Path(a)
        files += sorted(p.glob("*.bat")) if p.is_dir() else [p]
    for f in files:
        bad = check(f)
        if bad:
            failed += 1
            print(f"[✗] {f.name}: {'; '.join(bad)}")
        else:
            print(f"[ok] {f.name}: GBK + CRLF")
    print(f"\n{len(files) - failed}/{len(files)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
