"""Offline regression tests. Only temporary files and test-owned processes."""
import datetime
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import openviking_runtime as runtime


def load(name, filename):
    loader = SourceFileLoader(name, str(ROOT / 'tools' / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


health = load('ov_health', 'openviking-memory-health.py')
notify = load('ov_notify', 'openviking-notify.py')
guard = load('ov_guard', 'tailscale-guard-conpty.pyw')


class GuardTests(unittest.TestCase):
    def setUp(self):
        output = Path('D:/AI/Wife/tests/output/ov-leftovers/isolated') if os.name == 'nt' else ROOT/'tests/output/openviking'
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.root = Path(self.temp.name)
        self.now = 2000000000

    def tearDown(self):
        self.temp.cleanup()

    def clock(self):
        return self.now

    def state(self):
        return runtime.read_state(self.root / health.STATE_NAME)

    def sample_fail(self, exc=None):
        def fetch(*_):
            raise exc or urllib.error.URLError('synthetic secret must not be logged')
        return health.sample(self.root, fetch, self.clock)

    def deliver(self, send, check=lambda: False):
        return notify.deliver(self.root, check, send, self.clock)

    def test_sampler_never_starts_hermes(self):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('external process')):
            self.assertEqual(self.sample_fail(), 0)
        self.assertEqual(self.state()['incidents']['service']['error'], 'unreachable')
        self.assertNotIn('synthetic secret', (self.root / 'openviking-health.log').read_text())

    def test_sampling_uses_one_shared_deadline(self):
        calls = []
        def fetch(status, limit, deadline):
            calls.append((status, deadline))
            return []
        health.sample(self.root, fetch, self.clock)
        self.assertEqual([x[0] for x in calls], ['failed', 'completed'])
        self.assertEqual(calls[0][1], calls[1][1])

    def test_http_auth_does_not_claim_unreachable(self):
        self.sample_fail(urllib.error.HTTPError('unused', 401, 'private', {}, None))
        self.assertEqual(self.state()['incidents']['service']['error'], 'auth')
        sent = []
        self.deliver(lambda *args: (sent.append(args) or (0, False)), check=lambda: True)
        self.assertEqual(len(sent), len(notify.TARGETS))  # unauthenticated /health is not auth proof

    def test_recovered_pending_service_is_cancelled(self):
        self.sample_fail()
        self.deliver(lambda *_: self.fail('stale send'), check=lambda: True)
        self.assertFalse(self.state()['incidents']['service']['active'])

    def observer_exit(self, **fields):
        event = dict(event='exit', image='python.exe', pid=123, exit_code=-1,
                     utc=datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).isoformat())
        event.update(fields)
        path = self.root / 'openviking-recovery/process-exits.jsonl'
        path.parent.mkdir(exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event) + '\n')
        return path

    def test_process_exit_is_sent_after_service_recovers(self):
        self.observer_exit(private_payload='synthetic secret')
        health.sample(self.root, lambda *_: [], self.clock)
        calls = []
        self.deliver(lambda *args: (calls.append(args[0]) or (0, False)), check=lambda: True)
        self.assertEqual(len(calls), len(notify.TARGETS))
        self.assertEqual({c[c.index('--to') + 1] for c in calls}, set(notify.TARGETS))
        self.assertIn('observed process exits=1', calls[0][-1])
        self.assertIn('health_now=True', calls[0][-1])
        self.assertNotIn('synthetic secret', json.dumps(self.state()))
        self.assertGreater(self.state()['incidents']['process']['delivered_at'], 0)

    def test_delivered_process_exit_is_not_repeated(self):
        self.observer_exit()
        health.sample(self.root, lambda *_: [], self.clock)
        self.deliver(lambda *_: (0, False), check=lambda: True)
        self.now += 1
        health.sample(self.root, lambda *_: [], self.clock)
        self.deliver(lambda *_: self.fail('exit already delivered'), check=lambda: True)

    def test_process_exit_expiry_and_partial_append(self):
        path = self.observer_exit()
        with path.open('a') as handle:
            handle.write('{"event":')
        self.assertEqual(len(health.observed_exits(self.root, self.now)), 1)
        self.now += 301
        health.sample(self.root, lambda *_: [], self.clock)
        self.assertNotIn('process', self.state()['incidents'])

    def test_process_exit_ignores_untrusted_shapes(self):
        for fields in [dict(image='conhost.exe'), dict(pid='secret'), dict(exit_code='secret'),
                       dict(utc='bad'), dict(event='heartbeat'), dict(utc=None)]:
            self.observer_exit(**fields)
        path = self.root / 'openviking-recovery/process-exits.jsonl'
        with path.open('a') as handle:
            handle.write('[]\nnull\n')
        self.assertEqual(health.observed_exits(self.root, self.now), [])

    def test_process_exit_rotation_deduplicates(self):
        path = self.observer_exit()
        path.with_name(path.name + '.1').write_bytes(path.read_bytes())
        self.assertEqual(len(health.observed_exits(self.root, self.now)), 1)

    def test_process_exit_and_unavailable_use_bounded_separate_slots(self):
        self.observer_exit()
        self.sample_fail()
        self.assertEqual(set(self.state()['incidents']), {'process', 'service'})
        calls = []
        self.deliver(lambda *args: (calls.append(args[0]) or (1, False)))
        self.assertEqual(len(calls), 2 * len(notify.TARGETS))
        self.deliver(lambda *_: self.fail('failed process alert repeated'))

    def test_nonzero_delivery_is_failed_and_throttled(self):
        self.sample_fail()
        sent = []
        send = lambda *_: (sent.append(1) or (7, False))
        self.deliver(send)
        self.deliver(send)
        item = self.state()['incidents']['service']
        self.assertEqual(item['last_result'], 'telegram=failed weixin=failed')
        self.assertEqual(item['delivered_at'], 0)
        self.assertEqual(len(sent), len(notify.TARGETS))

    def test_timeout_and_exception_are_throttled(self):
        self.sample_fail()
        self.deliver(lambda *_: (124, True))
        self.assertEqual(self.state()['incidents']['service']['last_result'], 'telegram=timeout weixin=timeout')
        self.deliver(lambda *_: self.fail('timeout repeated'))
        self.now += 1801
        self.sample_fail()
        def fail(*_):
            raise RuntimeError('synthetic secret')
        self.deliver(fail)
        self.assertEqual(self.state()['incidents']['service']['last_result'],
                         'telegram=failed_RuntimeError weixin=failed_RuntimeError')

    def test_success_does_not_repeat_same_extraction_batch(self):
        task = {'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now}
        health.sample(self.root, lambda status, *_: [task] if status == 'failed' else [], self.clock)
        calls = []
        self.deliver(lambda *_: (calls.append(1) or (0, False)), check=lambda: True)
        self.now += 1801
        health.sample(self.root, lambda status, *_: [task] if status == 'failed' else [], self.clock)
        self.deliver(lambda *_: (calls.append(1) or (0, False)), check=lambda: True)
        self.assertEqual(len(calls), len(notify.TARGETS))

    def test_new_extraction_failure_after_throttle_can_send(self):
        for _ in range(2):
            task = {'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now}
            health.sample(self.root, lambda status, *_: [task] if status == 'failed' else [], self.clock)
            self.deliver(lambda *_: (0, False), check=lambda: True)
            self.assertEqual(self.state()['incidents']['extraction']['delivered_fail_ts'], self.now)
            self.now += 1801

    def test_resolved_during_send_keeps_resolution(self):
        self.sample_fail()
        def send(*_):
            health.sample(self.root, lambda *_: [], self.clock)
            return 0, False
        self.deliver(send)
        self.assertFalse(self.state()['incidents']['service']['active'])
        self.assertIn('resolved_during_send', (self.root / 'openviking-notify.log').read_text())

    def test_new_incident_during_send_does_not_receive_old_success(self):
        self.sample_fail()
        first = self.state()['incidents']['service']['id']
        def send(*_):
            health.sample(self.root, lambda *_: [], self.clock)
            self.sample_fail()
            return 0, False
        self.deliver(send)
        item = self.state()['incidents']['service']
        self.assertNotEqual(item['id'], first)
        self.assertEqual(item['delivered_at'], 0)

    def test_stale_sample_does_not_send(self):
        self.sample_fail()
        self.now += 301
        self.deliver(lambda *_: self.fail('stale send'))

    def test_extraction_is_suppressed_when_task_sampling_is_down(self):
        task = {'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now}
        health.sample(self.root, lambda *_: [task], self.clock)
        self.sample_fail()
        calls = []
        self.deliver(lambda *args: (calls.append(args[0]) or (0, False)))
        self.assertEqual(len(calls), len(notify.TARGETS))
        self.assertIn('task-probe', calls[0][-1])

    def test_maintenance_stops_sampling_and_notification(self):
        self.sample_fail()
        (self.root / '.openviking-maintenance').write_text('active')
        health.sample(self.root, lambda *_: self.fail('sample in maintenance'), self.clock)
        self.deliver(lambda *_: self.fail('send in maintenance'))

    def test_three_incident_slots_bound_outbox(self):
        self.observer_exit()
        task = {'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now}
        health.sample(self.root, lambda *_: [task], self.clock)
        for _ in range(20):
            self.sample_fail()
            self.now += 1
        self.assertEqual(set(self.state()['incidents']), {'process', 'service', 'extraction'})
        self.assertLess((self.root / health.STATE_NAME).stat().st_size, 4096)

    def test_legacy_throttle_migrates(self):
        (self.root / '.openviking-unavailable-alert').write_text(str(self.now - 30))
        self.sample_fail()
        self.deliver(lambda *_: self.fail('legacy throttle lost'))

    def test_corrupt_state_fails_closed(self):
        (self.root / health.STATE_NAME).write_text('invalid JSON')
        with self.assertRaises(ValueError):
            self.sample_fail()
        with self.assertRaises(ValueError):
            self.deliver(lambda *_: self.fail('corrupt state send'))

    def test_last_success_filters_historical_failures(self):
        tasks = [{'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now-1},
                 {'task_type': 'session_commit', 'status': 'completed', 'created_at': self.now}]
        self.assertEqual(health.task_summary(tasks, self.now), (0, 0))

    def test_notification_command_pushes_directly_without_a_model(self):
        # The alert must reach channels the user actually reads, and must not
        # burn a model call: an exhausted provider must not silence the guard.
        self.assertEqual(set(notify.TARGETS), {'telegram', 'weixin'})
        for target in notify.TARGETS:
            argv = notify.command('synthetic message', target)
            self.assertEqual(argv[1], 'send')
            self.assertEqual(argv[argv.index('--to')+1], target)
            self.assertNotIn('--model', argv)
            self.assertNotIn('--resume', argv)
            self.assertEqual(argv[-1], 'synthetic message')

    def test_one_channel_failure_does_not_block_the_other(self):
        task = {'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now}
        extraction = lambda status, *_: [task] if status == 'failed' else []
        health.sample(self.root, extraction, self.clock)
        sent = []
        def send(argv, *_):
            target = argv[argv.index('--to')+1]
            sent.append(target)
            if target == 'weixin':
                raise RuntimeError('synthetic secret')
            return 0, False
        self.deliver(send, check=lambda: True)
        self.assertEqual(sent, ['telegram', 'weixin'])
        log = (self.root / 'openviking-notify.log').read_text()
        self.assertIn('delivered incident=', log)
        self.assertIn('target=telegram exit=0', log)
        self.assertIn('failed_RuntimeError incident=', log)
        self.assertIn('target=weixin exit=-1', log)
        self.assertNotIn('synthetic secret', log)
        # The next batch round retries only the channel that has not received it.
        self.now += 1801
        health.sample(self.root, extraction, self.clock)
        self.deliver(lambda argv, *_: (sent.append(argv[argv.index('--to')+1]) or (0, False)), check=lambda: True)
        self.assertEqual(sent[2:], ['weixin'])

    def test_legacy_telegram_only_state_does_not_resend_telegram(self):
        task = {'task_type': 'session_commit', 'status': 'failed', 'created_at': self.now}
        health.sample(self.root, lambda status, *_: [task] if status == 'failed' else [], self.clock)
        state = self.state()
        state['incidents']['extraction'].update(delivered_at=self.now, delivered_fail_ts=self.now)
        runtime.save_state(self.root / health.STATE_NAME, state)
        sent = []
        self.deliver(lambda argv, *_: (sent.append(argv[argv.index('--to')+1]) or (0, False)), check=lambda: True)
        self.assertEqual(sent, ['weixin'])

    def test_worker_concurrency_only_one_send(self):
        self.sample_fail()
        entered, release = threading.Event(), threading.Event()
        errors, calls = [], []
        def send(*_):
            calls.append(1); entered.set(); release.wait(3)
            return 0, False
        def first():
            try:
                self.deliver(send)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.deliver(lambda *_: self.fail('second consumer'))
        finally:
            release.set(); thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), len(notify.TARGETS))

    def test_guard_lock_covers_actual_work(self):
        with patch.object(guard, 'ROOT', self.root), patch.object(guard, 'LOG', self.root/'guard.log'):
            with runtime.file_lock(self.root/'logs/.tailscale-guard.lock'):
                with patch.object(guard, 'run_guard', side_effect=AssertionError('overlap')):
                    self.assertEqual(guard.main(), 0)
            with patch.object(guard, 'run_guard', return_value=0) as run:
                self.assertEqual(guard.main(), 0)
                run.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited stdout integration')
    def test_guard_finishes_while_test_child_holds_stdout(self):
        # Match recovery's long-lived descendant inheriting the guard output.
        # Only this test's parent/child processes are created or cleaned up.
        child_pid = self.root / 'test-child.pid'
        script = self.root / 'guard.cmd'
        child = 'import time;time.sleep(30)'
        parent = ('import subprocess,sys,pathlib;'
                  f'p=subprocess.Popen([getattr(sys,"_base_executable",sys.executable),"-c",{child!r}]);'
                  f'pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))')
        parent_path = self.root / 'parent.py'
        parent_path.write_text(parent)
        script.write_text(f'@"{sys.executable}" "{parent_path}"\n@echo test-owned output\n')
        (self.root / 'logs').mkdir()
        try:
            started = time.monotonic()
            with patch.object(guard, 'ROOT', self.root), patch.object(guard, 'GUARD', script), \
                 patch.object(guard, 'LOG', self.root/'guard.log'), patch.object(guard, 'start_background'):
                self.assertEqual(guard.run_guard(), 0)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn('test-owned output', (self.root/'guard.log').read_text())
            import win32api, win32con, win32process
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, int(child_pid.read_text()))
            try:
                self.assertEqual(win32process.GetExitCodeProcess(handle), 259)
            finally:
                handle.Close()
        finally:
            if child_pid.exists():
                import win32api, win32con, win32event
                handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE, False, int(child_pid.read_text()))
                try:
                    win32api.TerminateProcess(handle, 0)
                    win32event.WaitForSingleObject(handle, 5000)
                finally:
                    handle.Close()

    def test_blocked_notifier_does_not_hold_guard_or_sampler_lock(self):
        self.sample_fail()
        entered, release = threading.Event(), threading.Event()
        def send(*_):
            entered.set(); release.wait(3)
            return 0, False
        thread = threading.Thread(target=lambda: self.deliver(send))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            started = time.monotonic()
            self.assertEqual(health.sample(self.root, lambda *_: [], self.clock), 0)
            with patch.object(guard, 'ROOT', self.root), patch.object(guard, 'LOG', self.root/'guard.log'), patch.object(guard, 'run_guard', return_value=0):
                self.assertEqual(guard.main(), 0)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set(); thread.join(4)

    def test_lock_releases_after_process_exit(self):
        lock = self.root/'cross-process.lock'
        ready = self.root/'ready'
        code = ('import sys,time;sys.path.insert(0,sys.argv[1]);from openviking_runtime import file_lock;'
                '\nwith file_lock(sys.argv[2]):\n open(sys.argv[3],"w").close();time.sleep(.6)')
        proc = subprocess.Popen([sys.executable, '-c', code, str(ROOT/'tools'), str(lock), str(ready)])
        try:
            deadline = time.monotonic()+4
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            with self.assertRaises(runtime.Busy):
                with runtime.file_lock(lock):
                    pass
            self.assertEqual(proc.wait(4), 0)
            with runtime.file_lock(lock):
                pass
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()

    def test_bounded_process_exit_status(self):
        self.assertEqual(runtime.run_bounded([sys.executable, '-c', 'raise SystemExit(7)'], self.root, 3), (7, False))

    @unittest.skipUnless(os.name == 'nt', 'Windows Job containment')
    def test_timeout_cleans_test_owned_descendants(self):
        marker = self.root/'child-finished'
        pidfile = self.root/'child-pid'
        child = f'import time,pathlib;time.sleep(2);pathlib.Path({str(marker)!r}).touch()'
        parent = ('import subprocess,sys,pathlib,time;'
                  f'p=subprocess.Popen([sys.executable,"-c",{child!r}]);'
                  f'pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(30)')
        code, timeout = runtime.run_bounded([sys.executable, '-c', parent], self.root, .7)
        self.assertTrue(pidfile.exists())
        self.assertEqual((code, timeout), (124, True))
        import win32api, win32con, win32event
        try:
            handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(pidfile.read_text()))
        except Exception:
            handle = None  # no such process means it has already gone
        if handle is not None:
            try:
                self.assertEqual(win32event.WaitForSingleObject(handle, 1000), win32con.WAIT_OBJECT_0)
            finally:
                handle.Close()
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == 'nt', 'CMD integration')
    def test_actual_cmd_disabled_llama_and_recovery_before_health(self):
        source = (ROOT/'tools/tailscale-guard.cmd').read_bytes().decode('utf-8')
        source = source.replace('tailscale status --json', 'echo {"BackendState": "Running"}')
        source = source.replace('netstat -ano | findstr ":9800.*LISTENING" >nul', 'ver >nul')
        lines = []
        for line in source.splitlines():
            if line.startswith('powershell.exe '):
                line = 'echo recovery>>"%~dp0trace.txt"'
            elif 'python.exe" "D:' in line:
                line = 'echo sample>>"%~dp0trace.txt"'
            elif 'start "" /b wscript.exe' in line:
                line = 'rem no curator in isolated test'
            lines.append(line)
        (self.root/'patch-openviking-plugin.cmd').write_text('@exit /b 0\n')
        # A reappearing legacy startup file must not cause an invocation.
        startup = self.root/'Microsoft/Windows/Start Menu/Programs/Startup'
        startup.mkdir(parents=True)
        (startup/'Hermes-llama-embedding.vbs').write_text('WScript.Quit 17')
        script = self.root/'guard.cmd'
        script.write_bytes(('\r\n'.join(lines)+'\r\n').encode('utf-8'))
        env = dict(os.environ, APPDATA=str(self.root))
        result = subprocess.run(['cmd.exe','/d','/c',str(script)], env=env,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.root/'trace.txt').read_text().splitlines(), ['recovery', 'sample'])
        self.assertNotIn(b'8082 down', result.stdout)
        self.assertNotIn(b'8082/health', script.read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
