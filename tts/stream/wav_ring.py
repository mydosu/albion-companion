# -*- coding: utf-8 -*-
"""整形后 wav 的现场环形缓存（2026-11-08）。

为什么要有这个：11-08 两次「听着断、日志正常」都卡在同一件事上——9889 渲完就把临时 wav 删了，
事后只能重渲，而重渲出来的不是当时那份（caption 是自由描述，每次不同，时长差 3~6%），拿它给
用户听等于换了个样本、证明不了什么。这里把**真正发给壳的那一份**留在 `logs/tts-recent/`，
只留最近 `KEEP` 个（按 mtime 删最旧），下次直接拿原音复现。

纯标准库、纯函数：失败只返回 None/空列表，由调用方决定记不记日志——保存失败绝不能连累出声。
"""
import os

KEEP = 40                                   # 默认留多少个（约 40 段 ≈ 几条回复）
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


def safe_name(text, limit=80):
    """文件名里只留字母数字和 `-._`：会话号是十六进制、unit 号带冒号，冒号在 Windows 上非法。"""
    s = "".join(ch if ch in _SAFE else "_" for ch in str(text or ""))[:limit]
    return s or "x"


def prune(folder, keep=KEEP, protect=None):
    """只留最近 `keep` 个 `.wav`（按 mtime），返回被删掉的文件名列表；`protect` 那个永不删。"""
    if keep <= 0:
        return []
    try:
        names = [n for n in os.listdir(folder) if n.lower().endswith(".wav")]
    except OSError:
        return []
    items = []
    for n in names:
        p = os.path.join(folder, n)
        try:
            items.append((os.path.getmtime(p), p))
        except OSError:                 # 正好被别的手删了：跳过
            continue
    items.sort(reverse=True)            # 新 → 旧
    guarded = os.path.abspath(protect) if protect else None
    removed = []
    for _, p in items[keep:]:
        if guarded and os.path.abspath(p) == guarded:
            continue
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except OSError:
            pass
    return removed


def save(folder, name, audio, keep=KEEP):
    """把 `audio` 存成 `folder/<safe(name)>.wav` 并按 mtime 裁到 `keep` 个。

    先写 `.tmp` 再 `os.replace`：半截文件不会被当成现场留下（下次 prune 也可能删到它）。
    返回落盘路径；任何失败返回 None（调用方只记日志）。
    """
    try:
        os.makedirs(folder, exist_ok=True)
        base = safe_name(name)
        if not base.lower().endswith(".wav"):
            base += ".wav"
        path = os.path.join(folder, base)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(audio)
        os.replace(tmp, path)
        prune(folder, keep, protect=path)
        return path
    except Exception:       # noqa: BLE001 - 留现场失败绝不影响出声
        return None
