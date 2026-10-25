#!/usr/bin/env python3
"""阿尔比恩的眼睛 · 采集端（跑在主用电脑 `albion` 上）

只做一件事：**按需截一张屏，返回 JPEG**。别的什么都不做。

为什么这么写：
  - 只用标准库 + Windows 自带的 .NET（`System.Drawing`），**不需要 pip 装任何东西**；
  - 默认**只绑 Tailscale 地址**（不是 0.0.0.0），且**必须带 token**；
  - 采集是"被问才给"（taihou 那台按需来取），不是主动往外推。

启动：
    python capture_agent.py                      # token 随机生成并打印
    python capture_agent.py --port 8790 --max-width 1600 --token-file token.txt
    （也认环境变量 ALBION_EYES_TOKEN；--token <TOKEN> 仍可用，但值会出现在进程命令行里）

接口：
    GET /health              → {"ok": true, "screen": "3840x1080", ...}
    GET /shot + Authorization: Bearer <token> → image/jpeg（多显示器一起截）

自检（不带参数跑一次，跑完退出）：
    python capture_agent.py --selftest
"""
from __future__ import annotations

import argparse
import json
import ipaddress
import re
import secrets
import socket
import subprocess
import sys
import threading
import tempfile
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

IS_WIN = sys.platform.startswith('win')

# 子进程一律不开窗口：PowerShell / ffmpeg 每次抓图都弹一个黑框出来（2026-10-10 实拍）
_NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

PS_SCRIPT = r'''
param([string]$Out, [int]$MaxWidth = 0)
$ErrorActionPreference = 'Stop'
$src = $null
$g = $null
$dst = $null
$g2 = $null
try {
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
# 按物理像素当进程：不做的话 VirtualScreen 会被 DPI 虚化（本机真屏 1200x1920 只截出 960x1536，
# 是缩小图），而且 UI 自动化的窗口坐标对不上截图。（SetProcessDPIAware 编译+调用实测 133ms）
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name Dpi -Namespace Albion
[Albion.Dpi]::SetProcessDPIAware() | Out-Null
$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
if ($vs.Width -le 0 -or $vs.Height -le 0) { throw 'invalid virtual screen bounds' }
$src = New-Object System.Drawing.Bitmap $vs.Width, $vs.Height
$g = [System.Drawing.Graphics]::FromImage($src)
$g.CopyFromScreen($vs.Left, $vs.Top, 0, 0, $src.Size)

# ---- 隐私遮挡：不该让眼睛看到的，涂黑 ----
# 1) 窗口标题或进程名命中关键词 → 整块涂黑（密码管理器、登录页、支付、私聊窗口…）
# 2) UI 自动化报出来的密码输入框 → 单独涂黑（比标题准，全桌面扫一遍约 250ms）
# 想加词改下面这一行即可。注意：普通浏览器页里的密码框标题不一定带关键词，靠第 2 条兜底；
# 做不到的是「看不出是密码框的普通输入」（比如某个 App 自绘的密码输入），那种只能靠标题命中。
$REDACT_WORDS = 'password|passwd|密码|登录|登入|sign in|signin|log in|login|credential|凭据|验证码|两步|2fa|authenticator|keepass|1password|bitwarden|lastpass|钱包|wallet|支付宝|微信支付|银行卡|身份证|密钥|私钥|secret|隐私|无痕|隐身|incognito'
try {
  $procs = @{}
  Get-Process | ForEach-Object { $procs[$_.Id] = $_.ProcessName }
  $desk = [System.Windows.Automation.AutomationElement]::RootElement
  if ($null -eq $desk) { throw 'UI Automation desktop is unavailable' }
  $kids = $desk.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
  if ($null -eq $kids -or $kids.Count -lt 1) { throw 'UI Automation returned no desktop windows' }
  foreach ($el in $kids) {
    $r = $el.Current.BoundingRectangle
    if ($r.Width -lt 40 -or $r.Height -lt 40) { continue }
    $proc = $procs[[int]$el.Current.ProcessId]
    if ("$($el.Current.Name) $proc" -match $REDACT_WORDS) {
      $g.FillRectangle([System.Drawing.Brushes]::Black, $r.X, $r.Y, $r.Width, $r.Height)
    }
  }
  $pwCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsPasswordProperty, $true)
  foreach ($el in $desk.FindAll([System.Windows.Automation.TreeScope]::Descendants, $pwCond)) {
    $r = $el.Current.BoundingRectangle
    if ($r.Width -lt 1 -or $r.Height -lt 1) { continue }
    $g.FillRectangle([System.Drawing.Brushes]::Black, $r.X - 4, $r.Y - 4, $r.Width + 8, $r.Height + 8)
  }
} catch { throw "privacy redaction failed: $($_.Exception.Message)" }
# 坐标不需要换算：进程已是 DPI 感知，截图和 UI 自动化都在同一套物理像素上（2026-10-10 实测 1200x1920 对齐）
# ------------------------------------------
if ($MaxWidth -gt 0 -and $src.Width -gt $MaxWidth) {
  $h = [int]($src.Height * $MaxWidth / $src.Width)
  $dst = New-Object System.Drawing.Bitmap $MaxWidth, $h
  $g2 = [System.Drawing.Graphics]::FromImage($dst)
  $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g2.DrawImage($src, 0, 0, $MaxWidth, $h)
  $dst.Save($Out, [System.Drawing.Imaging.ImageFormat]::Jpeg)
  $g2.Dispose(); $g2 = $null; $dst.Dispose(); $dst = $null
} else {
  $src.Save($Out, [System.Drawing.Imaging.ImageFormat]::Jpeg)
}
$g.Dispose(); $g = $null; $src.Dispose(); $src = $null
if (-not (Test-Path -LiteralPath $Out) -or (Get-Item -LiteralPath $Out).Length -lt 4) { throw 'JPEG output is missing or incomplete' }
Write-Output "REDACTION_OK|$($vs.Width)x$($vs.Height)"
} catch {
  [Console]::Error.WriteLine("capture failed: $($_.Exception.Message)")
  foreach ($resource in @($g2, $dst, $g, $src)) {
    if ($null -ne $resource) { try { $resource.Dispose() } catch {} }
  }
  Remove-Item -LiteralPath $Out -Force -ErrorAction SilentlyContinue
  exit 21
}
'''

_ps_path: Path | None = None


def _ps_file() -> Path:
    """把 PowerShell 脚本落到临时文件（首次写；被 Temp 清理掉就重写）。"""
    global _ps_path
    # 文件没了必须重写，否则截屏会一路 500 到进程重启（2026-10-01 实拍）
    if _ps_path is None or not _ps_path.exists():
        p = Path(tempfile.gettempdir()) / 'albion_capture.ps1'
        p.write_text(PS_SCRIPT, encoding='utf-8-sig')
        _ps_path = p
    return _ps_path


# 同一时刻只允许一次抓取：Windows 的 .NET 截屏 / ffmpeg 抓帧都不能并发跑，
# 两路请求撞一起时后者会 500（2026-09-17 实拍：她取图撞上"采集端 500"）。
# 加锁后第二路排队等待，不再报错——锁是进程级的，同一台的截图与摄像头共用。
_CAPTURE_LOCK = threading.Lock()


def grab_jpeg(max_width: int = 1920) -> tuple[bytes, str]:
    """截一张全屏 JPEG。返回 (bytes, '宽x高')。"""
    # 并发保护：见 _CAPTURE_LOCK
    with _CAPTURE_LOCK:
        if not IS_WIN:
            raise RuntimeError('采集端目前只支持 Windows（借 .NET 截图）')
        out = Path(tempfile.gettempdir()) / f'albion_shot_{os.getpid()}.jpg'
        if out.exists():
            out.unlink()
        try:
            r = subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', str(_ps_file()), '-Out', str(out), '-MaxWidth', str(max_width)],
                capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
            )
            if r.returncode != 0 or not out.exists():
                raise RuntimeError('screen capture unavailable')
            marker = (r.stdout or '').strip()
            if not marker.startswith('REDACTION_OK|'):
                raise RuntimeError('screen capture unavailable')
            size = marker.removeprefix('REDACTION_OK|')
            if not re.fullmatch(r'\d+x\d+', size):
                raise RuntimeError('screen capture unavailable')
            data = out.read_bytes()
            if len(data) < 4 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
                raise RuntimeError('screen capture unavailable')
            return data, size
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('screen capture unavailable') from exc
        finally:
            out.unlink(missing_ok=True)


DEFAULT_FFMPEG = 'D:/ffmpeg-2026-03-22-git-9c63742425-full_build/bin/ffmpeg.exe'
DEFAULT_CAM = 'FHD Camera'
# 摄像头是侧着装的：原始画面里人头朝左（2026-10-17 实拍），本地 4B 视觉模型认成「倒的、白花花一片」。
# 顺时针转 90° 才是正的；摄像头换了朝向就改 --cam-rotate。
DEFAULT_CAM_ROTATE = 90
_CAM_ROTATE_VF = {0: '', 90: 'transpose=1', 180: 'hflip,vflip', 270: 'transpose=2'}


def grab_cam_jpeg(max_width: int = 1920, device: str = DEFAULT_CAM, ffmpeg: str = DEFAULT_FFMPEG,
                  rotate: int = DEFAULT_CAM_ROTATE) -> bytes:
    """用 ffmpeg 抓摄像头的一帧，转正、只缩不放成 JPEG。"""
    # 并发保护：见 _CAPTURE_LOCK
    with _CAPTURE_LOCK:
        if not IS_WIN:
            raise RuntimeError('摄像头采集目前只做了 Windows（DirectShow + ffmpeg）')
        out = Path(tempfile.gettempdir()) / f'albion_cam_{os.getpid()}.jpg'
        if out.exists():
            out.unlink()
        exe = ffmpeg if Path(ffmpeg).exists() else 'ffmpeg'
        # 转完竖着只有 1080 宽：只缩不放（原来 scale=1920:-1 会放大到 1920×3413；max_width=0 = 原尺寸）
        vf = ','.join(f for f in (_CAM_ROTATE_VF[rotate],
                                  f"scale='min(iw,{max_width})':-2" if max_width > 0 else '') if f) or 'null'
        r = subprocess.run(
            [exe, '-hide_banner', '-loglevel', 'error',
             '-f', 'dshow', '-i', f'video={device}',
             '-frames:v', '1', '-vf', vf, '-q:v', '4', '-y', str(out)],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
        )
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f'抓摄像头失败: {(r.stderr or "")[:200]}')
        data = out.read_bytes()
        out.unlink(missing_ok=True)
        return data


def tailscale_ipv4() -> str | None:
    """本机的 Tailscale 地址（绑它，不绑 0.0.0.0）。"""
    try:
        r = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        ip = (r.stdout or '').strip().splitlines()
        return ip[0].strip() if ip else None
    except Exception:
        return None


# 局域网直连（2026-10-15，照 MWB 的做法）：两台在同一个局域网时，Tailscale 若打不了洞（比如一台开着
# 系统 VPN）就只能走海外中继，一张整屏要 35–60 秒；局域网直连 1–3 秒。--lan 时再多绑本机的局域网地址。
# 只认 10/8 与 192.168/16：172.16/12 多半是 WSL/Hyper-V 虚拟网卡或 VPN（PrivadoVPN 就是 172.20.x），
# 100.64/10 是 Tailscale 自己（已经单独绑了），169.254 是没拿到地址的网卡。token 照旧是门。
def lan_ipv4s(candidates=None) -> list[str]:
    """本机的局域网 IPv4（去重保序）。"""
    if candidates is None:
        try:
            candidates = [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            candidates = []
    nets = (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('192.168.0.0/16'))
    out: list[str] = []
    for ip in candidates:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in n for n in nets) and ip not in out:
            out.append(ip)
    return out


def resolve_bind(explicit: str | None, wait_seconds: float, interval: float = 5.0,
                 probe=tailscale_ipv4, log=print) -> str:
    """定绑定地址：显式给了就用；没给就**等** Tailscale 起来。

    为什么等：开机时 Tailscale 常常比采集端晚一步（2026-10-10 实测），一上来取不到就退回
    127.0.0.1，而壳里配的是 tailscale 地址 ⇒ 跨机取图会一直失败，而没有任何东西会来纠正它。
    所以这里宁可等；退回 127.0.0.1 只当兜底（并明确告警）。
    ponytail: 等 10 分钟就放弃 —— 再久也没见 Tailscale 起来的话，重启一次才是对的动作；
              真要更稳就在运行时盯着地址变化重新绑（要拆成线程 + 换 server）。
    """
    if explicit:
        return explicit
    deadline = time.time() + wait_seconds
    waited = False
    while True:
        ip = probe()
        if ip:
            if waited:
                log(f'  等到了 Tailscale 地址 {ip}')
            return ip
        if time.time() >= deadline:
            log(f'  warn: {wait_seconds:.0f}s 内没等到 Tailscale 地址，退回 127.0.0.1（跨机取图会失败）')
            return '127.0.0.1'
        if not waited:
            log('  没拿到 Tailscale 地址，先等它（开机时它常比我们晚）...')
            waited = True
        time.sleep(interval)


TOKEN_ENV = 'ALBION_EYES_TOKEN'


def read_token_file(path: str | None) -> str:
    """第一行非空内容（与启动器 `for /f` 读 token.txt 同口径）；没给/读不到返回空串。"""
    if not path:
        return ''
    try:
        lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    except OSError:
        return ''
    return next((s.strip() for s in lines if s.strip()), '')


def make_handler(token: str, max_width: int, cam_rotate: int = DEFAULT_CAM_ROTATE):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'albion-eyes/1.0'

        def _cors(self):
            # 浏览器跨源取图/健康检查要它；token 才是真正的门
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')

        def _authorized(self) -> bool:
            scheme, sep, got = self.headers.get('Authorization', '').partition(' ')
            if not (sep and scheme.lower() == 'bearer' and got.strip()):
                return False
            try:
                return secrets.compare_digest(got.strip().encode('utf-8'), token.encode('utf-8'))
            except (UnicodeEncodeError, TypeError):
                return False

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self._cors()
            if code == 401:
                # RFC 7235: 401 要说明用哪种凭据；客户端不依赖它，但探针/浏览器会看。
                self.send_header('WWW-Authenticate', 'Bearer')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):  # noqa: N802
            path = urlsplit(self.path).path
            if path == '/health':
                try:
                    _, size = grab_jpeg(320)          # 顺手证明"真能截到"
                    return self._json(200, {'ok': True, 'screen': size, 'maxWidth': max_width})
                except Exception:
                    return self._json(500, {'ok': False, 'error': 'screen capture unavailable'})
            if path in ('/cam', '/cam.jpg'):
                if not self._authorized():
                    return self._json(401, {'error': 'unauthorized'})
                try:
                    data = grab_cam_jpeg(max_width, rotate=cam_rotate)
                except Exception:
                    return self._json(500, {'error': 'camera capture unavailable'})
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path in ('/shot', '/shot.jpg'):
                if not self._authorized():
                    return self._json(401, {'error': 'unauthorized'})
                try:
                    data, _ = grab_jpeg(max_width)
                except Exception:
                    return self._json(500, {'error': 'screen capture unavailable'})
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._json(404, {'error': '只有 /health 和 /shot'})

        def log_message(self, fmt, *args):
            # The request target may contain credentials from stale or malformed clients.
            safe_path = self.path.partition('?')[0]
            status = args[1] if len(args) > 1 else '-'
            size = args[2] if len(args) > 2 else '-'
            print(f'[{self.address_string()}] "{self.command} {safe_path} {self.request_version}" {status} {size}', flush=True)

    return Handler


# ---- 残留清理：只清"上辈子"留下的抓帧文件 -------------------------------------
# 图本身是"取完就发、不存档"的：每次请求结束就 unlink（见 grab_jpeg / grab_cam_jpeg），
# 壳那边也只放内存不落盘。唯一漏网的是**进程被强杀**时留下的那一个 —— 文件名带进程号，
# 换了进程就没人认领了。所以启动时扫一遍就够，不需要定时任务。
STALE_AGE_S = 3600


def sweep_stale(max_age_s: int = STALE_AGE_S) -> int:
    """删掉临时目录里"别人的、且超过 max_age_s 没动过"的抓帧文件，返回删了几个。"""
    mine = f'_{os.getpid()}.jpg'
    now = time.time()
    removed = 0
    for pat in ('albion_shot_*.jpg', 'albion_cam_*.jpg'):
        for p in Path(tempfile.gettempdir()).glob(pat):
            if p.name.endswith(mine):
                continue  # 本进程正在用的那份，不碰
            try:
                if now - p.stat().st_mtime > max_age_s:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description='阿尔比恩的眼睛 · 采集端')
    ap.add_argument('--port', type=int, default=8790)
    ap.add_argument('--token', default=None,
                    help='会出现在进程命令行里；启动器请用 --token-file 或环境变量 ALBION_EYES_TOKEN')
    ap.add_argument('--token-file', default=None, help='从文件第一行非空内容读 token（不进命令行）')
    ap.add_argument('--max-width', type=int, default=1920, help='超过就等比缩小（0 = 原尺寸）')
    ap.add_argument('--bind', default=None, help='默认只绑 Tailscale 地址')
    ap.add_argument('--bind-wait', type=float, default=600.0,
                    help='没给 --bind 时，等 Tailscale 地址出现的秒数（0 = 不等，直接兜底）')
    ap.add_argument('--lan', action='store_true',
                    help='再多绑本机的局域网地址（同一局域网直连，不走 Tailscale 中继；仍要 token）')
    ap.add_argument('--cam-rotate', type=int, default=DEFAULT_CAM_ROTATE, choices=sorted(_CAM_ROTATE_VF),
                    help='摄像头画面顺时针转几度才正（默认 90：这台的摄像头侧装）')
    ap.add_argument('--selftest', action='store_true', help='截一张就退出')
    args = ap.parse_args()

    if args.selftest:
        # 清理自检：造两个假残留（一个很旧、一个刚生成），扫完旧的没了、新的还在
        tmp = Path(tempfile.gettempdir())
        old = tmp / f'albion_shot_{os.getpid() + 1}.jpg'
        fresh = tmp / f'albion_cam_{os.getpid() + 2}.jpg'
        old.write_bytes(b'x')
        fresh.write_bytes(b'x')
        os.utime(old, (time.time() - STALE_AGE_S - 60,) * 2)
        removed = sweep_stale()
        ok = (not old.exists()) and fresh.exists()
        print(f'{"✓" if ok else "✗"} 清理自检：扫掉 {removed} 个（旧残留没了={not old.exists()}，新的留着={fresh.exists()}）')
        old.unlink(missing_ok=True)
        fresh.unlink(missing_ok=True)

        # 绑定地址：显式优先；没给就等 Tailscale；等不到才退 loopback —— 别让开机抢跑把地址定死
        assert resolve_bind('10.0.0.1', 0, probe=lambda: None) == '10.0.0.1'
        assert resolve_bind(None, 0, probe=lambda: '100.64.0.9', log=lambda *_: None) == '100.64.0.9'
        assert resolve_bind(None, 0.05, interval=0.01, probe=lambda: None, log=lambda *_: None) == '127.0.0.1'
        print('✓ 绑定自检通过：显式优先 / 等到就用 / 等不到才退 loopback')
        assert lan_ipv4s(['10.19.75.219', '172.20.152.146', '100.100.1.2', '192.168.5.6', '169.254.1.2', '127.0.0.1', '10.19.75.219', 'x']) == ['10.19.75.219', '192.168.5.6']
        print(f'✓ 局域网地址自检通过：只留 10/8、192.168/16（本机现在是 {lan_ipv4s() or "无"}）')
        tok = tmp / f'albion_tok_{os.getpid()}.txt'
        tok.write_text('\n  -tok-1 \nsecond\n', encoding='utf-8')
        assert read_token_file(str(tok)) == '-tok-1'
        tok.unlink(missing_ok=True)
        assert read_token_file(str(tok)) == '' and read_token_file(None) == ''
        print('✓ token 文件自检通过：取第一行非空、缺文件为空')

        data, size = grab_jpeg(args.max_width)
        print(f'✓ 屏幕自检通过：{size}，JPEG {len(data) / 1024:.0f} KB')
        try:
            cam = grab_cam_jpeg(args.max_width, rotate=args.cam_rotate)
            print(f'✓ 摄像头自检通过：JPEG {len(cam) / 1024:.0f} KB')
        except Exception as e:
            print(f'✗ 摄像头没抓成：{str(e)[:120]}')
        return

    token = args.token or read_token_file(args.token_file) or os.environ.get(TOKEN_ENV, '').strip()
    if args.token_file and not token:
        # 给了文件却读不到：宁可不起，也别悄悄换成随机 token 让壳那边全 401
        raise SystemExit(f'token 文件缺失或为空：{args.token_file}')
    from_file = bool(token) and not args.token
    token = token or secrets.token_urlsafe(18)
    bind = resolve_bind(args.bind, args.bind_wait)
    handler = make_handler(token, args.max_width, args.cam_rotate)
    srv = ThreadingHTTPServer((bind, args.port), handler)
    extra: list[str] = []
    if args.lan:
        for ip in lan_ipv4s():
            if ip == bind:
                continue
            try:
                lan_srv = ThreadingHTTPServer((ip, args.port), handler)
            except OSError as e:
                print(f'  warn: 局域网地址 {ip}:{args.port} 绑不上（{e}），这一路只走 Tailscale')
                continue
            threading.Thread(target=lan_srv.serve_forever, daemon=True).start()
            extra.append(ip)
    if not from_file:
        try:
            (Path(__file__).parent / 'token.txt').write_text(token, encoding='utf-8')
        except Exception:
            pass

    swept = sweep_stale()
    print(f'  清理   启动扫掉 {swept} 个过期残留（取完即删，无定时任务）')
    print('阿尔比恩的眼睛 已启动')
    print(f'  监听   http://{bind}:{args.port}')
    for ip in extra:
        print(f'  局域网 http://{ip}:{args.port}')
    print('  token  已加载（值不写入日志）')
    print(f'  取图   curl -H "Authorization: Bearer <TOKEN>" -o shot.jpg "http://{bind}:{args.port}/shot"')
    print(f'  自检   curl "http://{bind}:{args.port}/health"')
    srv.serve_forever()


if __name__ == '__main__':
    main()
