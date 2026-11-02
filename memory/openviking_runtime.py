"""Shared local locking, atomic state and bounded notification processes.

No credentials or user payloads belong in state/logs. Windows notification
children are created suspended and assigned to a kill-on-close job before run.
"""
import contextlib
import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


class Busy(RuntimeError):
    pass


def maintenance_active(root):
    try:
        return (Path(root) / '.openviking-maintenance').read_text(encoding='utf-8-sig').strip().lower() == 'active'
    except FileNotFoundError:
        return False
    except OSError:
        return True


@contextlib.contextmanager
def file_lock(path, timeout=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'\0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy('lock held') from None
                time.sleep(.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def read_state(path):
    try:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': 1, 'incidents': {}}
    if obj.get('version') != 1 or not isinstance(obj.get('incidents'), dict):
        raise ValueError('invalid notification state')
    return obj


def save_state(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=True), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def log(path, message):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Single writer per component; bound disk use without moving active logs.
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        os.replace(path, path.with_suffix(path.suffix + '.1'))
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f'{datetime.datetime.now(datetime.timezone.utc).isoformat()} {message}\n')


def run_bounded(argv, cwd, timeout):
    """Return (exit_code, timed_out), discard output and contain descendants."""
    if os.name != 'nt':
        # Portable isolated tests; the deployed implementation uses Windows jobs.
        try:
            r = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=False)
            return r.returncode, False
        except subprocess.TimeoutExpired:
            return 124, True
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32process
    job = win32job.CreateJobObject(None, '')
    process = thread = null = None
    assigned = False
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        security = pywintypes.SECURITY_ATTRIBUTES()
        security.bInheritHandle = True
        null = win32file.CreateFile('NUL', win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                   win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                   security, win32con.OPEN_EXISTING, 0, None)
        startup = win32process.STARTUPINFO()
        startup.dwFlags |= win32con.STARTF_USESTDHANDLES
        startup.hStdInput = startup.hStdOutput = startup.hStdError = null
        process, thread, _, _ = win32process.CreateProcess(
            None, subprocess.list2cmdline(argv), None, None, True,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
            None, str(cwd), startup)
        win32job.AssignProcessToJobObject(job, process)
        assigned = True
        win32process.ResumeThread(thread)
        timed_out = win32event.WaitForSingleObject(process, int(timeout * 1000)) == win32con.WAIT_TIMEOUT
        if timed_out:
            win32job.TerminateJobObject(job, 124)
            win32event.WaitForSingleObject(process, 5000)
        return win32process.GetExitCodeProcess(process), timed_out
    finally:
        # Fail closed: an unassigned suspended child must never be resumed.
        if process is not None and not assigned:
            win32api.TerminateProcess(process, 125)
        job.Close()  # also stops any leftover child after CLI exit or failure
        for handle in (thread, process, null):
            if handle is not None:
                handle.Close()
