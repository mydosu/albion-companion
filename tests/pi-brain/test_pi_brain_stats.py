# -*- coding: utf-8 -*-
"""pi_brain_stats 的离线自检：造一小份日志，核对计数口径。不碰 WSL、不读生产。"""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))

import pi_brain_stats as s  # noqa: E402

BRAIN = [
    '2026-10-16 10:00:00 prompt {"session": "A", "chars": 5, "images": 0, "woke": true}',
    '2026-10-16 10:00:05 turn {"session": "A", "ms": 5000, "chars": 20}',
    '2026-10-16 10:01:00 prompt {"session": "B", "chars": 5, "images": 0, "woke": false}',
    '2026-10-16 10:01:03 turn {"session": "B", "ms": 3000, "chars": 0}',
    '2026-10-16 10:02:00 request {"path": "/v1/chat/completions"}',
    '2026-10-15 09:00:00 turn {"session": "A", "ms": 9999, "chars": 9}',
]
GUARD = [
    '{"ts":"2026-10-16T10:00:06.000Z","actions":["posture_kept"],"dropped":0,"session":"A"}',
    '{"ts":"2026-10-16T10:01:06.000Z","actions":["act"],"dropped":0,"session":"B"}',
    '{"ts":"2026-10-15T10:00:00.000Z","actions":["posture_dropped"],"dropped":1,"session":"A"}',
]
LOOP = [
    '{"ts":"2026-10-16T10:00:00.000Z","loaded":true,"pid":1}',
    '{"ts":"2026-10-16T10:00:01.000Z","blocked":"bash","seen":3,"lockdown":false}',
    '{"ts":"2026-10-16T10:00:02.000Z","blocked":"bash","seen":4,"lockdown":true}',
    '{"ts":"2026-10-16T10:00:03.000Z","blocked":"bash","lockdown":2,"terminate":false}',
    '{"ts":"2026-10-16T10:00:04.000Z","blocked":"bash","lockdown":3,"terminate":true}',
]


class PiBrainStatsTest(unittest.TestCase):
    def test_counts_since_date(self):
        brain = s.parse_brain(BRAIN, '2026-10-16')
        guard = s.parse_jsonl(GUARD, '2026-10-16')
        loop = s.parse_jsonl(LOOP, '2026-10-16')
        r = s.compute(brain, guard, loop, '2026-10-16')
        self.assertEqual(r['turns'], 2)
        self.assertEqual(r['replies'], 1)
        self.assertEqual(r['zero_replies'], 1)
        self.assertEqual(r['posture'], {'action_rows': 2, 'replies_with_posture': 1, 'rate': 1.0})
        self.assertEqual(r['guard_actions'], {'posture_kept': 1, 'act': 1})
        self.assertEqual(r['repeat_breaker'], {'soft_blocks': 1, 'hard_trips': 1,
                                               'blocked_after_trip': 2, 'terminated': 1})
        self.assertEqual(r['proactive'], {'woke_prompts': 1, 'woke_spoke': 1})
        self.assertEqual(r['turn_ms']['n'], 1)
        self.assertEqual(r['turn_ms']['p50'], 5000)

    def test_no_since_keeps_everything(self):
        brain = s.parse_brain(BRAIN, None)
        self.assertEqual(sum(1 for ev, _ in brain if ev == 'turn'), 3)

    def test_empty_inputs_do_not_divide_by_zero(self):
        r = s.compute([], [], [], None)
        self.assertIsNone(r['posture']['rate'])
        self.assertIsNone(r['turn_ms']['p50'])

    def test_legacy_guard_rows_give_no_posture_rate(self):
        brain = s.parse_brain(BRAIN, None)
        legacy = ['{"ts":"2026-10-07T07:49:34.058Z","draft":0,"issues":["act"]}']
        r = s.compute(brain, s.parse_jsonl(legacy, None), [], None)
        self.assertEqual(r['posture']['action_rows'], 0)
        self.assertIsNone(r['posture']['rate'])

    def test_local_dir_reads_copies(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / 'brain.log').write_text('\n'.join(BRAIN) + '\n', encoding='utf-8')
            args = type('A', (), {'local': d, 'distro': 'Ubuntu'})
            self.assertEqual(len(s.read_lines('brain.log', args)), len(BRAIN))
            self.assertEqual(s.read_lines('guard.log', args), [])


if __name__ == '__main__':
    unittest.main()
