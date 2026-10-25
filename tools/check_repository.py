#!/usr/bin/env python3
"""Read-only layout gate. Run from any cwd; optionally scan staged content with Gitleaks."""
import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gitleaks', help='Path to gitleaks executable; scan staged content with full redaction')
    args = parser.parse_args()
    top = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel']).decode('utf-8').strip()
    if pathlib.Path(top).resolve() != ROOT.resolve():
        print('ERROR: project must have its own Git root; parent repository cannot validate this checkout')
        return 1
    names = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).decode('utf-8').split('\0')
    problems = []
    checked = 0
    old = re.compile(r'(?<![\w/])(?:pet[/\\]tests|tts[/\\]tests|tts[/\\]eval|pet[/\\]tools[/\\]spine-official)(?:[/\\\s`"\']|$)')
    private = re.compile(r'(^|/)(?:backup|her-home|_archive|\.local)(/|$)|(^|/)\.env(?:\.|$)|\.bak|\.obsolete-|agent-keys\.json$|\.token$|(?:^|/)token\.txt$')
    for name in sorted(set(names) - {''}):
        path = ROOT / name
        if not path.is_file():
            continue
        checked += 1
        if private.search(name) and not name.endswith('.example'):
            problems.append({'file': name, 'reason': 'private or backup file would enter repository'})
        if name.startswith(('pet/', 'tts/')) and re.search(r'(^|/)test_[^/]+\.py$', name):
            problems.append({'file': name, 'reason': 'independent test belongs under tests/'})
        if name.startswith('archive/') or name in {'docs/layout-moves-20261002.json', 'tools/check_repository.py'}:
            continue
        if path.suffix.lower() in {'.py', '.ts', '.mjs', '.js', '.md', '.bat', '.ps1', '.vbs', '.sh', '.yaml', '.yml'}:
            data = path.read_bytes()
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = data.decode('gb18030', errors='replace')
            for number, line in enumerate(text.splitlines(), 1):
                if old.search(line):
                    problems.append({'file': name, 'line': number, 'reason': 'reference to relocated directory'})
    print(json.dumps({'files_checked': checked, 'layout_errors': len(problems), 'problems': problems}, ensure_ascii=True, indent=2))
    if problems:
        return 1
    if args.gitleaks:
        # No report file is necessary: redact stdout and stderr before they reach the terminal.
        scan = subprocess.run([args.gitleaks, 'git', str(ROOT), '--staged', '--redact=100', '--no-banner', '--gitleaks-ignore-path', str(ROOT / '.gitleaksignore')], capture_output=True)
        print('gitleaks_staged_exit=' + str(scan.returncode))
        return scan.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
