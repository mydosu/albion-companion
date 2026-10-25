#!/usr/bin/env python3
"""Verify or restore the private weights-v1 release using the checked-in asset manifest.

Default is read-only validation. --download downloads to ignored .local/downloads;
--install copies missing verified weights/configs, refusing to replace different files.
"""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--cache', type=pathlib.Path, default=ROOT / '.local/downloads/weights-v1')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'config/rebuild/assets.json').read_text(encoding='utf-8'))
    if args.download:
        args.cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(['gh', 'release', 'download', manifest['release'], '--repo', manifest['repository'], '--pattern', '*.safetensors', '--dir', str(args.cache), '--skip-existing'], check=True)
    valid = missing = bad = installed = 0
    for item in manifest['assets']:
        target = ROOT / item['destination']
        assert target.resolve().is_relative_to(ROOT.resolve()), 'Destination outside project'
        source = target if target.is_file() else args.cache / item['asset']
        if not source.is_file():
            print('MISSING ' + item['destination']); missing += 1; continue
        if source.stat().st_size != item['size'] or digest(source) != item['sha256']:
            print('HASH_MISMATCH ' + item['destination']); bad += 1; continue
        valid += 1
        if args.install:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target); installed += 1
            if item.get('adapter_config'):
                config_source = ROOT / item['adapter_config']
                config_target = target.with_name('adapter_config.json')
                if config_target.exists() and config_target.read_bytes() != config_source.read_bytes():
                    print('CONFIG_MISMATCH ' + str(config_target.relative_to(ROOT))); bad += 1
                elif not config_target.exists():
                    shutil.copy2(config_source, config_target)
    local_missing = local_bad = 0
    for item in manifest['local_assets']:
        path = ROOT / item['path']
        if not path.is_file():
            print('EXTERNAL_MISSING ' + item['path']); local_missing += 1
        elif path.stat().st_size != item['size'] or digest(path) != item['sha256']:
            print('EXTERNAL_HASH_MISMATCH ' + item['path']); local_bad += 1
    print(json.dumps({'release_verified': valid, 'release_missing': missing, 'release_mismatch': bad, 'weights_installed': installed, 'external_missing': local_missing, 'external_mismatch': local_bad}))
    return 1 if missing or bad or local_missing or local_bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
