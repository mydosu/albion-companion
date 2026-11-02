"""Hidden guard runner. Own the lock for the actual CMD lifetime.

Notification and observation have independent single-instance protection and
never inherit the captured guard pipes. No service process is killed here.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from openviking_runtime import Busy, file_lock, log

ROOT = Path(r'D:\AI\scripts')
GUARD = ROOT / 'tailscale-guard.cmd'
LOG = ROOT / 'logs/tailscale-guard.log'
OV_PYTHON = r'D:\AI\openviking\venv\Scripts\pythonw.exe'


def start_background(argv):
    return subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                            close_fds=True)


def run_guard():
    started = time.monotonic()
    log(LOG, f'BEGIN worker={os.getpid()}')
    # Exit observation is independent of the recovery mutex and service lifetime.
    try:
        start_background(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                          '-File', str(ROOT / 'openviking-observe.ps1')])
    except OSError as exc:
        log(LOG, f'WARN observer launch type={type(exc).__name__}')
    # A recovered long-lived server can inherit a duplicate of CMD's stdout.
    # communicate() then waits for EOF after CMD has already exited, holding the
    # guard lock for the server's whole lifetime. A temporary disk handle lets
    # us wait only for CMD; never terminate the recovered service to unblock EOF.
    with tempfile.TemporaryFile(dir=ROOT / 'logs') as output, subprocess.Popen(
            ['cmd.exe', '/d', '/c', str(GUARD)], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW) as process:
        try:
            process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            # CMD may own a recovery launch. Killing its tree could kill :1933.
            # Retain the lock until it actually ends; don't release into overlap.
            log(LOG, f'WARN guard exceeded 600s cmd_pid={process.pid}; retaining lock')
            process.wait()
        output.seek(0)
        body = output.read(1024 * 1024)
        if body:
            log(LOG, body.decode('utf-8', errors='replace').strip())
        code = process.returncode
    try:
        start_background([OV_PYTHON, str(ROOT / 'openviking-notify.py')])
    except OSError as exc:
        log(LOG, f'WARN notifier launch type={type(exc).__name__}')
    log(LOG, f'END worker={os.getpid()} exit={code} elapsed_ms={int((time.monotonic()-started)*1000)}')
    return code


def main():
    try:
        with file_lock(ROOT / 'logs/.tailscale-guard.lock'):
            return run_guard()
    except Busy:
        log(LOG, f'SKIP overlap worker={os.getpid()}')
        return 0
    except Exception as exc:
        log(LOG, f'FAIL worker type={type(exc).__name__}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
