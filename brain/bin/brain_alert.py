"""脑哑了的告警（2026-11-15）：连续两轮没回出来就在 Discord 喊一声，回魂时补一条恢复。
门铃被拒（GET /albion/wake 回 401）也归这里：持续满 10 分钟喊一声，通过时补恢复（2026-11-16）。

只告警、不自动换线路——换线路由用户定（2026-11-14 脑哑了 27 小时，每一轮都 403 却没人知道）。
写法和 memory/openviking-notify.py 里那套已验证能送达的 webhook 一致，但这边独立一份：
脑是 Pi 在 WSL 里起的长进程，不去 import 别的项目的运维代码。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# webhook URL（含 token）只放本机，不进仓库、不进日志、不进状态。
# 首选 Windows 那份（和 openviking-notify 共用）；这台 WSL 关了 automount（/etc/wsl.conf enabled=false），
# /mnt/d 根本不存在，所以再留一份脑自己目录下的（agent/.alert-discord-webhook）给它兜着。
WEBHOOK = Path(os.environ.get("ALBION_ALERT_WEBHOOK_FILE", "/mnt/d/AI/scripts/.ov-alert-discord-webhook"))
LOCAL_WEBHOOK = Path(__file__).resolve().parent.parent / "agent" / ".alert-discord-webhook"
DISCORD_USERNAME = "阿尔比恩的脑"
USER_AGENT = "albion-brain-alert/1.0"      # urllib 默认 UA 会被 Discord 前面的 Cloudflare 403（error 1010）
SEND_TIMEOUT = 10
FAIL_ALERT_AT = 2                          # 连续第 2 轮失败就喊
REPEAT_AFTER_S = 3 * 3600                  # 还一直坏着，隔 3 小时再喊一次
WAKE_ALERT_AFTER_S = 600                   # 门铃持续被拒（401）满 10 分钟就喊
WAKE_KEY = "__wake__"                      # _state 里门铃那格，和会话 id 不会撞
ERROR_MAX_CHARS = 200
# 出外网只能走代理（WSL 直连 DNS 超时），变量和默认值跟 chat_bridge 的 ALBION_CHAT_PROXY 一致；空串则直连。
PROXY = os.environ.get("ALBION_CHAT_PROXY", "http://127.0.0.1:7897").strip()
LOGS = Path(os.environ.get("ALBION_LOGS", str(Path(__file__).resolve().parent.parent / "logs")))

_lock = threading.Lock()
# shortcut: 计数只在内存，宿主重启清零；要跨重启再落盘
_state = {}    # session_id -> {'fails', 'alerted_at', 'since', 'error', 'model'}


def log(msg, **fields):
    """一行日志写进 brain.log（和宿主同一个文件）；写不进去也不能影响这一轮。"""
    line = "%s %s%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg,
                        (" " + json.dumps(fields, ensure_ascii=False)) if fields else "")
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "brain.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                      # noqa: BLE001
        pass


def scrub(text):
    """错误正文常带 key/token：Bearer、sk-、URL 里的 key=/token= 都抹成 ***，再截 200 字。"""
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", str(text or ""))
    text = re.sub(r"sk-[A-Za-z0-9._\-]+", "sk-***", text)
    text = re.sub(r"(?i)([?&](?:key|token)=)[^&\s\"']+", r"\1***", text)
    return " ".join(text.split())[:ERROR_MAX_CHARS]


def webhook_url(path=None):
    """Webhook 只从本机文件读：文件缺失或为空返回空串，这一路跳过（不算发过）。"""
    for candidate in ([path] if path else [WEBHOOK, LOCAL_WEBHOOK]):
        try:
            url = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if url:
            return url
    return ""


def proxy_opener(proxy=None):
    """外网请求用的 opener：显式挂 ProxyHandler，空地址就是直连（不建 opener 默认那套）。"""
    proxy = PROXY if proxy is None else proxy
    if not proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def post_webhook(url, message):
    # 不能靠 urllib 默认代理解析：WSL 直连 DNS 超时，宿主进程环境里又没有 HTTPS_PROXY/HTTP_PROXY，
    # 默认解析等于直连，生产上必然是 status=0。这里显式走 ALBION_CHAT_PROXY（默认 127.0.0.1:7897）。
    # 异常文本可能带 URL/token，所以只记异常类型和 HTTP 状态码，不记 exc 内容。
    body = json.dumps({"content": message, "username": DISCORD_USERNAME,
                       "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
    with proxy_opener().open(request, timeout=SEND_TIMEOUT) as response:
        return response.status


def _send(message, url, post):
    """后台线程发：失败只记一行，不往外抛，不能影响这一轮。"""
    def run():
        try:
            log("brain alert sent status=%s" % post(url, message))
        except urllib.error.HTTPError as exc:
            log("brain alert failed status=%s" % exc.code)
        except Exception as exc:           # noqa: BLE001
            log("brain alert failed status=0 error=%s" % type(exc).__name__)
    threading.Thread(target=run, daemon=True).start()


def _alert_text(item):
    return ("阿尔比恩的脑连续 %d 轮没回出来（会话 %s，模型 %s）。最近一次错误：%s"
            % (item["fails"], item.get("session_id") or "?", item.get("model") or "未知",
               item.get("error") or "（无）"))


def _recover_text(item, now):
    minutes = max(0, int((now - (item.get("since") or now)) / 60))
    return ("阿尔比恩的脑回魂了（会话 %s）：哑了 %d 分钟，这期间失败 %d 轮。"
            % (item.get("session_id") or "?", minutes, item["fails"]))


def record_failure(session_id, error_text, model=None, *, clock=time.time, post=None):
    """一轮失败；连续第 2 轮喊一次，之后还坏着就隔 3 小时再喊。返回这次是否发了告警。"""
    now, url = clock(), webhook_url()
    with _lock:
        item = _state.setdefault(session_id, {"fails": 0, "alerted_at": None, "since": None})
        item.update(session_id=session_id, model=model, error=scrub(error_text))
        item["fails"] += 1
        if item["since"] is None:
            item["since"] = now
        due = item["fails"] >= FAIL_ALERT_AT and (
            item["alerted_at"] is None or now - item["alerted_at"] >= REPEAT_AFTER_S)
        if not due:
            return False
        # 没 webhook 就不算发过：不记 alerted_at，下一次失败还会再试。
        if not url:
            log("brain alert skipped reason=no_webhook")
            return False
        item["alerted_at"] = now
        text = _alert_text(item)
    _send(text, url, post or post_webhook)
    return True


def record_success(session_id, *, clock=time.time, post=None):
    """一轮回了话：告警过的补一条恢复，然后清零；没告警过的只清零。返回这次是否发了恢复。"""
    now = clock()
    with _lock:
        item = _state.pop(session_id, None)
        if not item or item.get("alerted_at") is None:
            return False          # 没告警过：只清零，不读文件、不发东西
        text = _recover_text(item, now)
    url = webhook_url()
    if not url:
        log("brain alert skipped reason=no_webhook")
        return False
    _send(text, url, post or post_webhook)
    return True


# ── 门铃被拒（2026-11-16）────────────────────────────────────────────────────
# 壳每 30 秒问一次 GET /albion/wake；壳带的 key 和脑对不上就一直是 401，她醒不来，
# 而 401 在两边日志里都不留痕（2026-11-15 静了 16 小时才发现）。
# 这一段被拒的第一次记一行日志；持续满 10 分钟喊一次，之后还一直被拒就隔 3 小时再喊。
def _wake_alert_text(item, now):
    minutes = max(0, int((now - (item.get("since") or now)) / 60))
    return ("阿尔比恩的门铃被拒了 %d 分钟（%d 次 401）：壳带的 key 和脑对不上，她醒不来。"
            "多半是壳打包时没带 shell/apps/stage-tamagotchi/.env.local。"
            % (minutes, int(item.get("denied") or 0)))


def _wake_recover_text(item, now):
    minutes = max(0, int((now - (item.get("since") or now)) / 60))
    return "阿尔比恩的门铃恢复了：被拒了 %d 分钟。" % minutes


def record_wake_denied(*, clock=time.time, post=None):
    """门铃回了一次 401（HTTP 线程里调，不能往外抛）。返回这次是否发了告警。"""
    now, url = clock(), webhook_url()
    with _lock:
        item = _state.setdefault(WAKE_KEY, {"denied": 0, "since": None,
                                            "alerted_at": None, "logged": False})
        item["denied"] += 1
        if item["since"] is None:
            item["since"] = now
        if not item["logged"]:
            # 每 30 秒一次 401，只在开头记一行：同类故障至少日志里有痕迹
            item["logged"] = True
            log("wake doorbell denied status=401")
        due = (now - item["since"] >= WAKE_ALERT_AFTER_S and
               (item["alerted_at"] is None or now - item["alerted_at"] >= REPEAT_AFTER_S))
        if not due:
            return False
        # 没 webhook 就不算发过：不记 alerted_at，下一次被拒还会再试。
        if not url:
            log("brain alert skipped reason=no_webhook")
            return False
        item["alerted_at"] = now
        text = _wake_alert_text(item, now)
    _send(text, url, post or post_webhook)
    return True


def record_wake_ok(*, clock=time.time, post=None):
    """门铃通过了（鉴权过就算，不管 due 真假）：告警过的补恢复，然后清零；没告警过的只清零。"""
    now = clock()
    with _lock:
        item = _state.pop(WAKE_KEY, None)
        if not item or item.get("alerted_at") is None:
            return False          # 没告警过：只清零，不读文件、不发东西
        text = _wake_recover_text(item, now)
    url = webhook_url()
    if not url:
        log("brain alert skipped reason=no_webhook")
        return False
    _send(text, url, post or post_webhook)
    return True
