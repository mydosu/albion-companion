"""Single consumer for three bounded OpenViking incident slots (no raw errors)."""
import datetime
from pathlib import Path
import time
import urllib.request

from openviking_runtime import Busy, file_lock, log, maintenance_active, read_state, run_bounded, save_state

LOG_ROOT = Path(r'D:\AI\scripts\logs')
HERMES = str(Path.home() / 'AppData' / 'Local' / 'hermes' / 'bin' / 'hermes.exe')
STATE_NAME = '.openviking-notify-state.json'
THROTTLE = 1800
SEND_TIMEOUT = 30
# 告警直接发到用户本人的 Telegram 私信和微信私信（两个 home channel）。`hermes send`
# 不调模型，退出码即平台回执。两路各自独立发送、独立记日志：一路失败或超时不影响另一路。
TARGETS = ('telegram', 'weixin')


def healthy():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:1933/health', timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def command(message, target):
    return [HERMES, 'send', '--to', target, '--quiet', message]


def delivered_targets(item):
    # 旧状态只记了 telegram 一路（delivered_fail_ts），迁移时视为 telegram 已送达，避免重发。
    if item.get('delivered_to') is not None:
        return dict(item['delivered_to'])
    if item.get('delivered_at') and item.get('delivered_fail_ts'):
        return {'telegram': item['delivered_fail_ts']}
    return {}


def deliver(root=LOG_ROOT, check=healthy, send=run_bounded, clock=time.time):
    logfile = root / 'openviking-notify.log'
    try:
        with file_lock(root / '.openviking-notify.lock'):
            for kind in ('service', 'process', 'extraction'):
                if maintenance_active(root):
                    return 0
                # Probe outside the state lock, then re-read the latest generation.
                is_healthy = check()
                now = clock()
                with file_lock(root / '.openviking-state.lock', timeout=1):
                    path = root / STATE_NAME
                    state = read_state(path)
                    item = state['incidents'].get(kind)
                    if not item or not item['active']:
                        continue
                    if kind == 'extraction' and state['incidents'].get('service', {}).get('active'):
                        continue
                    if kind == 'service' and item.get('error') == 'unreachable' and is_healthy:
                        item.update(active=False, resolved_at=now)
                        save_state(path, state)
                        log(logfile, f'CANCEL recovered incident={item["id"]}')
                        continue
                    if now - item['observed_at'] > 300:
                        continue  # stale sampler data must not be sent as current
                    if item.get('attempt_at') and now - item['attempt_at'] < THROTTLE:
                        continue
                    # 批次级去重按路进行：已送达的一路不重发，只补发仍未送达的那路。
                    done = delivered_targets(item)
                    newest = item.get('newest', 0)
                    if kind in ('extraction', 'process'):
                        pending = [t for t in TARGETS if t not in done or done[t] < newest]
                        if not pending:
                            continue
                    else:
                        pending = list(TARGETS)
                    # Reserve before external work. Even crashes/failures are throttled.
                    item['attempt_at'] = now
                    incident = item['id']
                    at = datetime.datetime.fromtimestamp(item['observed_at'], datetime.timezone.utc).isoformat()
                    summary = ('task-probe ' + item['error']) if kind == 'service' else f'extraction failures={item["count"]}'
                    if kind == 'process':
                        summary = (f'observed process exits={item["count"]}; latest pid={item["pid"]} '
                                   f'exit_code={item["exit_code"]}; health_now={is_healthy}')
                    message = (f'[OpenViking observation {at}] incident={incident}; {summary}. '
                               'This is the status at the observation time; recovery may have occurred.')
                    save_state(path, state)
                results = []
                for target in pending:
                    try:
                        code, timed_out = send(command(message, target), r'D:\AI\scripts', SEND_TIMEOUT)
                        result = 'timeout' if timed_out else ('delivered' if code == 0 else 'failed')
                    except Exception as exc:
                        code, result = -1, 'failed_' + type(exc).__name__
                    results.append((target, result, code))
                with file_lock(root / '.openviking-state.lock', timeout=1):
                    state = read_state(path)
                    current = state['incidents'].get(kind, {})
                    matched = current.get('id') == incident
                    if matched:
                        current['last_result'] = ' '.join(f'{t}={r}' for t, r, _ in results)
                        ok = [t for t, r, _ in results if r == 'delivered']
                        if ok:
                            done.update({t: newest for t in ok})
                            current.update(delivered_at=clock(), delivered_fail_ts=newest, delivered_to=done)
                        save_state(path, state)
                    suffix = '_resolved_during_send' if matched and not current.get('active', False) else ''
                for target, result, code in results:
                    log(logfile, f'{result}{suffix} incident={incident} target={target} exit={code}')
            return 0
    except Busy:
        # Do not write here: another consumer owns the notification log too.
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(deliver())
    except Exception as exc:
        log(LOG_ROOT / 'openviking-notify.log', f'FAIL worker type={type(exc).__name__}')
        raise SystemExit(1)
