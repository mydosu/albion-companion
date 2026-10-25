#!/usr/bin/env python3
"""给 Windows 桌面摆件（pet/widget/albion_status_widget.pyw）定时写最近活动快照。

宿主每 INTERVAL 秒把 state/state.db 的只读查询结果原子写到 state/widget.json；
摆件只读这份 JSON，不再自己起 wsl + python 轮询。
文件动作只记路径与时间，不记内容。

用法：python3 bin/widget_snapshot.py [--selftest]   （不带参数＝写一次并打印）
"""
import datetime
import json
import os
import re
import sqlite3
import time

INTERVAL = 5
SESSION = "albion-desktop"
HOME_ROOT = "/root/家"
FILE_NAME = "widget.json"


def _state_dir():
    return os.environ.get("ALBION_STATE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")


def snapshot(db_path, root=HOME_ROOT, sid=SESSION):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        session = conn.execute(
            "SELECT last_activity_at,message_count,tool_call_count FROM sessions WHERE id=?",
            (sid,)).fetchone()
        tool = conn.execute(
            "SELECT tool_name,timestamp FROM messages WHERE session_id=? AND role='tool' "
            "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        assistant = conn.execute(
            "SELECT content,tool_calls,timestamp FROM messages WHERE session_id=? "
            "AND role='assistant' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_messages = conn.execute(
            "SELECT count(*) FROM messages WHERE session_id=? AND timestamp>=?",
            (sid, midnight)).fetchone()[0]
        today_tools = conn.execute(
            "SELECT count(*) FROM messages WHERE session_id=? AND role='tool' AND timestamp>=?",
            (sid, midnight)).fetchone()[0]
        writes = conn.execute(
            "SELECT content,timestamp FROM messages WHERE session_id=? AND role='tool' "
            "AND (tool_name IN ('write_file','patch','edit_file','apply_patch','read_file') "
            "OR (tool_name='terminal' AND content LIKE '%files_modified%')) "
            "ORDER BY id DESC LIMIT 100", (sid,)).fetchall()
    finally:
        conn.close()
    action = ""
    emotion = "neutral"
    if assistant:
        match = re.match(r'<\|ACT:({[^\n]{1,1000}?})\|>', assistant[0] or "")
        if match:
            try:
                emotion = str(json.loads(match.group(1)).get("emotion") or "neutral")
            except (ValueError, TypeError, AttributeError):
                pass
        if assistant[1]:
            try:
                calls = json.loads(assistant[1])
                if isinstance(calls, list) and calls:
                    call = calls[0]
                    action = str(call.get("function", {}).get("name") or call.get("name") or "")
            except (ValueError, TypeError, AttributeError):
                pass
    work = None
    if os.path.isdir(root):
        newest = (0, "")
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
            for name in files:
                path = os.path.join(base, name)
                try:
                    if os.path.islink(path):
                        continue
                    stamp = os.stat(path, follow_symlinks=False).st_mtime
                    if stamp > newest[0]:
                        newest = (stamp, os.path.relpath(path, root))
                except OSError:
                    pass
        if newest[1]:
            work = {"relative_path": newest[1], "mtime": newest[0]}
    # 工具成功结果能把路径归因到她本人；不对整个 /root 或 /mnt 做无主文件扫描。
    tool_work = None
    for content, stamp in writes:
        if not isinstance(stamp, (int, float)):
            continue
        try:
            result = json.loads(content or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or result.get("success") is False or result.get("verified") is False:
            continue
        paths = result.get("files_modified") or []
        if not isinstance(paths, list):
            paths = []
        paths.append(result.get("resolved_path"))
        for path in paths:
            if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
                continue
            path = os.path.normpath(path)
            shown = os.path.relpath(path, root) if path.startswith(root + "/") else path
            tool_work = {"relative_path": shown, "mtime": stamp}
            break
        if tool_work is not None:
            break
    if tool_work is not None and (work is None or tool_work["mtime"] >= work["mtime"]):
        work = tool_work
    name = str(tool[0] or "工具") if tool else ""
    now = time.time()
    return {
        "generated_at": now,
        "last_tool": {"tool_name": name, "summary": ("使用 " + name)[:40], "ts": tool[1]} if tool else None,
        "last_action": action,
        "work": work,
        "emotion": emotion,
        "idle_seconds": max(0, int(now - session[0])) if session and session[0] else None,
        "counts": {"message_count": session[1] or 0, "tool_call_count": session[2] or 0} if session else {"message_count": 0, "tool_call_count": 0},
        "today_counts": {"message_count": today_messages, "tool_call_count": today_tools},
        "last_activity_at": session[0] if session else None,
    }


def write_once(state_dir=None, root=HOME_ROOT):
    state_dir = state_dir or _state_dir()
    data = snapshot(os.path.join(state_dir, "state.db"), root)
    out = os.path.join(state_dir, FILE_NAME)
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(out + ".tmp", out)
    return data


def loop(log=None):
    """宿主的后台线程：出错只记一次，恢复后接着写；摆件看 generated_at 判断陈旧。"""
    failing = False
    while True:
        try:
            write_once()
            failing = False
        except Exception as exc:
            if not failing and log:
                log("widget snapshot failed", error=str(exc)[:120])
            failing = True
        time.sleep(INTERVAL)


def _selftest():
    import tempfile
    d = tempfile.mkdtemp(prefix="widget-snap-")
    root = os.path.join(d, "家")
    os.makedirs(os.path.join(root, "游戏"))
    with open(os.path.join(root, "游戏", "读着走.txt"), "w") as f:
        f.write("x")
    db = sqlite3.connect(os.path.join(d, "state.db"))
    now = time.time()
    db.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY, last_activity_at REAL, message_count INT, tool_call_count INT);"
                     "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                     " tool_calls TEXT, tool_name TEXT, timestamp REAL);")
    db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (SESSION, now - 30, 9, 2))
    db.executemany("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp) VALUES (?,?,?,?,?,?)", [
        (SESSION, "assistant", '<|ACT:{"emotion":"curious"}|>看看', '[{"function":{"name":"read_file"}}]', None, now - 40),
        (SESSION, "tool", json.dumps({"resolved_path": root + "/书/新书.md"}), None, "read_file", now + 5),
        ("albion-accept", "tool", "{}", None, "terminal", now + 9),
    ])
    db.commit()
    db.close()
    data = write_once(d, root)
    with open(os.path.join(d, FILE_NAME), encoding="utf-8") as f:
        assert json.load(f) == data
    assert not os.path.exists(os.path.join(d, FILE_NAME + ".tmp"))
    assert data["emotion"] == "curious" and data["last_action"] == "read_file", data
    assert data["last_tool"]["tool_name"] == "read_file", "别的会话不算"
    assert data["work"]["relative_path"] == "书/新书.md", "工具记录比文件 mtime 新时以工具为准"
    assert data["counts"] == {"message_count": 9, "tool_call_count": 2}
    assert data["today_counts"]["message_count"] >= 2 and abs(data["generated_at"] - time.time()) < 5
    print("WIDGET SNAPSHOT SELFTEST OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(json.dumps(write_once(), ensure_ascii=False))
