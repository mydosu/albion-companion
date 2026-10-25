#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pi-brain 生产日志计数（只读，只出数字，不输出任何回复正文或预览文本）。

统计：回复数与零字回复、姿态句命中率、复读断路（软拦截／硬断路／断路后仍被拦）、
主动开口（自醒来的回合里真开口的次数）、整轮耗时分位数。
数据来自 WSL 的 /root/albion-pi/logs/（brain.log、guard.log、loopguard.log），只读 cat。

注意：耗时是 brain.log 的 turn.ms，是整轮耗时，没有首块埋点，只能当上界用，不能当首块延迟。

用法：
  python tools/pi_brain_stats.py [--since 2026-10-01] [--out tests/output/x.json]
  python tools/pi_brain_stats.py --local <目录>      # 读已拷出的日志副本（自检用）
"""
import argparse
import json
import pathlib
import re
import statistics
import subprocess
import sys
from collections import Counter

LOGS = ('brain.log', 'guard.log', 'loopguard.log')
PROD_LOGS = '/root/albion-pi/logs'
POSTURE = {'posture_kept', 'posture_dropped'}
BRAIN_LINE = re.compile(r'^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2} (\w+)(?: (.*))?$')


def read_lines(name, args):
    if args.local:
        path = pathlib.Path(args.local) / name
        return path.read_bytes().decode('utf-8', 'replace').splitlines() if path.exists() else []
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    r = subprocess.run(['wsl', '-d', args.distro, '--', 'cat', f'{PROD_LOGS}/{name}'],
                       capture_output=True, timeout=120, creationflags=flags)
    if r.returncode != 0:
        print(f'ERROR: 读取 {name} 失败（退出码 {r.returncode}）', file=sys.stderr)
        raise SystemExit(2)
    return r.stdout.decode('utf-8', 'replace').splitlines()


def parse_brain(lines, since):
    """brain.log 行形如「2026-10-16 11:07:40 turn {json}」，返回 (事件名, 字段) 列表。"""
    events = []
    for line in lines:
        m = BRAIN_LINE.match(line)
        if not m or (since and m.group(1) < since):
            continue
        try:
            fields = json.loads(m.group(3) or '{}')
        except json.JSONDecodeError:
            continue
        events.append((m.group(2), fields))
    return events


def parse_jsonl(lines, since):
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and str(row.get('ts', ''))[:10] < since:
            continue
        rows.append(row)
    return rows


def quantile(values, q):
    if len(values) < 2:
        return values[0] if values else None
    return statistics.quantiles(values, n=100, method='inclusive')[q - 1]


def compute(brain, guard, loop, since):
    turns = [f for ev, f in brain if ev == 'turn']
    replies = [f for f in turns if f.get('chars', 0) > 0]

    # 主动开口：prompt 带 woke 的回合，之后同一会话的下一个 turn 才算开口
    pending, woke_prompts, woke_spoke = set(), 0, 0
    for ev, f in brain:
        session = f.get('session')
        if ev == 'prompt' and f.get('woke'):
            woke_prompts += 1
            pending.add(session)
        elif ev == 'turn' and session in pending:
            pending.discard(session)
            if f.get('chars', 0) > 0:
                woke_spoke += 1

    # guard.log 只在整理器动过手时写一行；旧格式行没有 actions 字段，不计入姿态样本
    action_rows = [g for g in guard if 'actions' in g]
    posture_hits = sum(1 for g in action_rows if POSTURE & set(g.get('actions') or []))
    actions = Counter(a for g in action_rows for a in (g.get('actions') or []))
    soft = sum(1 for r in loop if 'seen' in r and not r.get('lockdown'))
    hard = sum(1 for r in loop if 'seen' in r and r.get('lockdown'))
    after_trip = sum(1 for r in loop if 'blocked' in r and 'seen' not in r)
    terminated = sum(1 for r in loop if r.get('terminate'))
    ms = [f['ms'] for f in replies if isinstance(f.get('ms'), (int, float))]

    return {
        'since': since or None,
        'turns': len(turns),
        'replies': len(replies),
        'zero_replies': len(turns) - len(replies),
        'posture': {
            'action_rows': len(action_rows),
            'replies_with_posture': posture_hits,
            # 没有带 actions 的行时无样本可算，报 null，不能报 0
            'rate': round(posture_hits / len(replies), 4) if replies and action_rows else None,
        },
        'guard_actions': dict(actions),
        'repeat_breaker': {
            'soft_blocks': soft,
            'hard_trips': hard,
            'blocked_after_trip': after_trip,
            'terminated': terminated,
        },
        'proactive': {'woke_prompts': woke_prompts, 'woke_spoke': woke_spoke},
        'turn_ms': {
            'n': len(ms),
            'p50': quantile(ms, 50),
            'p90': quantile(ms, 90),
            'note': '整轮耗时（brain.log turn.ms），无首块埋点，只作上界',
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--since', help='只统计此日期（含）之后的日志，格式 YYYY-MM-DD')
    parser.add_argument('--distro', default='Ubuntu')
    parser.add_argument('--local', help='读取已拷出的日志目录，不走 WSL')
    parser.add_argument('--out', help='把 JSON 写到此文件（目录会自动创建）')
    args = parser.parse_args()

    brain = parse_brain(read_lines('brain.log', args), args.since)
    guard = parse_jsonl(read_lines('guard.log', args), args.since)
    loop = parse_jsonl(read_lines('loopguard.log', args), args.since)
    result = compute(brain, guard, loop, args.since)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
