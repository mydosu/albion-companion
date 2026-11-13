"""Single consumer for three bounded OpenViking incident slots (no raw errors)."""
import datetime
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from openviking_runtime import Busy, file_lock, log, maintenance_active, read_state, save_state

LOG_ROOT = Path(r'D:\AI\scripts\logs')
STATE_NAME = '.openviking-notify-state.json'
THROTTLE = 1800
SEND_TIMEOUT = 30
# 2026-11-13: 微信撤掉（iLink 令牌只随用户入站消息续期，长期 session not ready / prepare failed），
# 告警只走 Discord webhook：不走 hermes，直接 POST，2xx 即送达，HTTP 状态码即失败码。
TARGETS = ('discord',)
# webhook URL（含 token）只放本机，不进仓库、不进日志、不进状态。
WEBHOOK = Path(r'D:\AI\scripts\.ov-alert-discord-webhook')
DISCORD_USERNAME = 'OpenViking 运维'


def healthy():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:1933/health', timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def webhook_url():
    """Webhook 只从本机文件读：文件缺失或为空返回空串，这一路跳过。"""
    try:
        return WEBHOOK.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def post_webhook(url, message):
    # 代理沿用 urllib 默认解析：HTTPS_PROXY/ALL_PROXY 等环境变量优先，其次 Windows 系统代理，
    # 都没有就直连。Hermes 网关自身没有 proxy 配置键，本机靠 Clash 分流，两边走同一套 socket 路径。
    # 异常文本可能带 URL/token，所以只记异常类型和 HTTP 状态码，不记 exc 内容。
    body = json.dumps({'content': message, 'username': DISCORD_USERNAME,
                       'allowed_mentions': {'parse': []}}).encode('utf-8')
    # urllib 默认 UA 是 Python-urllib/x.y，Discord 前面的 Cloudflare 对它直接 403（error 1010），
    # 所以固定一个自己的 UA。
    request = urllib.request.Request(url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'User-Agent': 'OpenViking-notify/1.0'})
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as response:
        return response.status


def delivered_targets(item):
    # 旧状态只记了 telegram 一路（delivered_fail_ts），迁移时视为 telegram 已送达，避免重发。
    if item.get('delivered_to') is not None:
        return dict(item['delivered_to'])
    if item.get('delivered_at') and item.get('delivered_fail_ts'):
        return {'telegram': item['delivered_fail_ts']}
    return {}


def deliver(root=LOG_ROOT, check=healthy, post=post_webhook, clock=time.time):
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
                    # 批次级去重按目标进行：已送达的批次不重发，只补发还没送达的。
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
                for target in pending:   # TARGETS 只剩 discord
                    url = webhook_url()
                    if not url:
                        # 没配 webhook 就跳过，不记送达。
                        results.append((target, 'skipped', 0))
                        continue
                    try:
                        status = post(url, message)
                        ok = 200 <= status < 300
                        results.append((target, 'delivered' if ok else 'failed', 0 if ok else status))
                    except urllib.error.HTTPError as exc:
                        results.append((target, 'failed', exc.code))
                    except Exception as exc:
                        results.append((target, 'failed_' + type(exc).__name__, -1))
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
                    if result == 'skipped':
                        log(logfile, f'skipped{suffix} incident={incident} target={target} reason=no_webhook')
                    else:
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
