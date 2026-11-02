"""Read-only task sampling. Notification delivery is a separate worker.

Production entry stays D:/AI/scripts/openviking-memory-health.py. The existing
server credential is read privately from ov.conf, never embedded or printed.
"""
import datetime
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

from openviking_runtime import Busy, file_lock, log, maintenance_active, read_state, run_bounded, save_state

LOG_ROOT = Path(r'D:\AI\scripts\logs')
SERVER_CONFIG = Path(r'D:\AI\openviking\ov.conf')
ENDPOINT = 'http://127.0.0.1:1933'
STATE_NAME = '.openviking-notify-state.json'
TOTAL_BUDGET = 8
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_tasks(status, limit, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('sampling deadline')
    conf = json.loads(SERVER_CONFIG.read_text(encoding='utf-8-sig'))
    key = conf['server']['root_api_key']
    req = urllib.request.Request(f'{ENDPOINT}/api/v1/tasks?status={status}&limit={limit}',
                                 headers={'X-API-Key': key})
    with OPENER.open(req, timeout=min(4, remaining)) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError('oversize task response')
    data = json.loads(body)
    result = data.get('result', data) if isinstance(data, dict) else data
    if isinstance(result, dict):
        result = result.get('tasks', [])
    if not isinstance(result, list):
        raise ValueError('invalid task list')
    return result


def task_summary(tasks, now):
    def stamp(task):
        value = task.get('created_at') or task.get('updated_at') or 0
        return float(value) if isinstance(value, (int, float)) else 0
    relevant = [t for t in tasks if isinstance(t, dict) and
                any(s in str(t.get('task_type', '')) for s in ('commit', 'extract'))]
    last_ok = max((stamp(t) for t in relevant if t.get('status') == 'completed'), default=0)
    failed = [t for t in relevant if t.get('status') == 'failed'
              and stamp(t) >= now - 86400 and stamp(t) >= last_ok]
    return len(failed), max((stamp(t) for t in failed), default=0)


def update_incident(state, kind, active, now, **fields):
    item = state['incidents'].get(kind)
    if not active:
        if item and item['active']:
            item.update(active=False, resolved_at=now)
        return
    if not item or not item['active']:
        item = {'id': uuid.uuid4().hex, 'active': True, 'first_at': now,
                'attempt_at': 0, 'delivered_at': 0, 'delivered_fail_ts': 0}
        state['incidents'][kind] = item
    item.update(observed_at=now, **fields)


def error_kind(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return 'auth' if exc.code in (401, 403) else 'http_' + str(exc.code)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return 'unreachable'
    return 'invalid_response_or_config'


def observed_exits(root, now):
    """Read only bounded observer logs; never copy arbitrary event fields to state."""
    exits = []
    path = root / 'openviking-recovery/process-exits.jsonl'
    for source in (path.with_name(path.name + '.1'), path):
        try:
            with source.open('rb') as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 5 * 1024 * 1024))
                if size > 5 * 1024 * 1024:
                    handle.readline()  # discard a partial first line
                lines = handle.read(5 * 1024 * 1024).decode('utf-8-sig', errors='replace').splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('event') != 'exit' or event.get('image') not in ('python.exe', 'openviking-server.exe'):
                    continue
                at = datetime.datetime.fromisoformat(event['utc'].replace('Z', '+00:00'))
                if at.tzinfo is None:
                    continue
                stamp = at.timestamp()
                pid, code = event['pid'], event['exit_code']
                if type(pid) is not int or pid <= 0 or type(code) is not int:
                    continue
                if now - 300 <= stamp <= now:
                    exits.append((stamp, pid, code))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue  # a concurrent append may leave the final line incomplete
    return sorted(set(exits))


def sample(root=LOG_ROOT, fetch=fetch_tasks, clock=time.time):
    if maintenance_active(root):
        log(root / 'openviking-health.log', 'SKIP maintenance')
        return 0
    # Serialize samplers as well as guard invocations, including manual entry.
    try:
        with file_lock(root / '.openviking-sampler.lock'):
            now = clock()
            exits = observed_exits(root, now)
            deadline = time.monotonic() + TOTAL_BUDGET
            try:
                tasks = fetch('failed', 100, deadline) + fetch('completed', 20, deadline)
                count, newest = task_summary(tasks, now)
                failure = None
            except Exception as exc:
                failure = error_kind(exc)
            with file_lock(root / '.openviking-state.lock', timeout=1):
                path = root / STATE_NAME
                fresh = not path.exists()
                state = read_state(path)
                if exits:
                    stamp, pid, code = exits[-1]
                    update_incident(state, 'process', True, now, count=len(exits),
                                    newest=stamp, pid=pid, exit_code=code)
                else:
                    update_incident(state, 'process', False, now)
                if failure:
                    update_incident(state, 'service', True, now, error=failure)
                    # Extraction status is unknown while sampling fails. No stale send.
                else:
                    update_incident(state, 'service', False, now)
                    update_incident(state, 'extraction', bool(count), now, count=count, newest=newest)
                if fresh:
                    # Carry recent legacy delivery/attempt times across deployment.
                    for kind, name in [('service', '.openviking-unavailable-alert'),
                                       ('extraction', '.openviking-health-alert')]:
                        item = state['incidents'].get(kind)
                        if item:
                            try:
                                old_at = float((root / name).read_text(encoding='utf-8').split(',')[0])
                                item['attempt_at'] = min(now, old_at)
                            except (OSError, ValueError):
                                pass
                save_state(path, state)
            message = f'WARN task probe error={failure}' if failure else f'OK sampled failed={count}'
            log(root / 'openviking-health.log', message)
            return 0
    except Busy:
        log(root / 'openviking-health.log', 'SKIP sampler/state lock busy')
        return 0


if __name__ == '__main__':
    try:
        if '--sample-internal' in sys.argv:
            raise SystemExit(sample())
        # Socket timeouts alone don't bound a response which trickles bytes.
        # Contain only this read-only sampler, never the recovery helper/CMD.
        code, timed_out = run_bounded([sys.executable, str(Path(__file__).resolve()),
                                      '--sample-internal'], Path(__file__).resolve().parent, 10)
        if timed_out:
            log(LOG_ROOT / 'openviking-health.log', 'FAIL sampling hard deadline 10s')
        raise SystemExit(code)
    except Exception as exc:
        # Exception strings can contain URLs, headers or provider responses.
        log(LOG_ROOT / 'openviking-health.log', f'FAIL sampler type={type(exc).__name__}')
        raise SystemExit(1)
