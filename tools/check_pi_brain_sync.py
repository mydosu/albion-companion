#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pi-brain 仓库与 WSL 生产副本同步核对（只读）。

比较 integrations/pi-brain 下入库的 bin/、agent/extensions/、agent/APPEND_SYSTEM.md、accept.sh
与 WSL 里 /root/albion-pi/ 同相对路径的文件。比较前去掉 CR（工作树可能是 CRLF，生产是 LF）。
只在 WSL 里跑 sha256 和 find，不写生产、不重启、不改 profile。

用法：python tools/check_pi_brain_sync.py [--distro Ubuntu] [--prod /root/albion-pi]
退出码：0 全部一致；1 有内容差异或仓库文件在生产缺失；2 取不到 WSL 数据。
生产独有的文件只列出来，不计入失败。
"""
import argparse
import hashlib
import pathlib
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFIX = 'integrations/pi-brain/'
SCOPE = ('bin/', 'agent/extensions/', 'agent/APPEND_SYSTEM.md', 'accept.sh')


def tracked():
    out = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z', '--', PREFIX])
    rels = [p[len(PREFIX):] for p in out.decode('utf-8').split('\0') if p]
    return sorted(r for r in rels if r.startswith(SCOPE))


def local_hash(rel):
    raw = (ROOT / PREFIX / rel).read_bytes().replace(b'\r', b'')
    return hashlib.sha256(raw).hexdigest()


def prod_state(distro, prod, rels):
    # 只读脚本经 stdin 送进 bash -s，不经 PowerShell 管道，避免 CRLF 混入
    lines = [f'cd {shlex.quote(prod)} || exit 3']
    lines.append('for f in ' + ' '.join(shlex.quote(r) for r in rels) + '; do')
    lines.append('  if [ -f "$f" ]; then printf "%s\\t%s\\n" "$f" "$(tr -d "\\r" < "$f" | sha256sum | cut -c1-64)";')
    lines.append('  else printf "%s\\tMISSING\\n" "$f"; fi')
    lines.append('done')
    lines.append('find bin agent/extensions -type f -not -path "*/__pycache__/*" -not -path "*/node_modules/*" -not -name "*.bak*" 2>/dev/null | while read -r f; do printf "LIST\\t%s\\n" "$f"; done')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    r = subprocess.run(['wsl', '-d', distro, '--', 'bash', '-s'], input='\n'.join(lines).encode('utf-8'),
                       capture_output=True, timeout=120, creationflags=flags)
    if r.returncode != 0:
        print(f'ERROR: WSL 读取失败（退出码 {r.returncode}）：{r.stderr.decode("utf-8", "replace").strip()[:200]}')
        sys.exit(2)
    hashes, listed = {}, set()
    for line in r.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split('\t')
        if parts[0] == 'LIST' and len(parts) == 2:
            listed.add(parts[1])
        elif len(parts) == 2:
            hashes[parts[0]] = parts[1]
    return hashes, listed


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--distro', default='Ubuntu')
    parser.add_argument('--prod', default='/root/albion-pi')
    args = parser.parse_args()

    rels = tracked()
    prod, listed = prod_state(args.distro, args.prod, rels)
    same, differ, missing = [], [], []
    for rel in rels:
        if prod.get(rel) in (None, 'MISSING'):
            missing.append(rel)
        elif prod[rel] == local_hash(rel):
            same.append(rel)
        else:
            differ.append(rel)
    tracked_set = set(rels)
    prod_only = sorted(p for p in listed if p not in tracked_set and p.startswith(SCOPE))

    for rel in differ:
        print(f'[差异] {PREFIX}{rel}')
    for rel in missing:
        print(f'[生产缺失] {PREFIX}{rel}')
    for rel in prod_only:
        print(f'[仅生产有] {rel}')
    print('--------------------------------------------------')
    print(f'  一致 {len(same)}／{len(rels)}；内容差异 {len(differ)}；生产缺失 {len(missing)}；仅生产有 {len(prod_only)}')
    return 1 if (differ or missing) else 0


if __name__ == '__main__':
    raise SystemExit(main())
