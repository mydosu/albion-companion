#!/usr/bin/env python3
"""验 `read` 读大图时交出去的是不是**原字节**（settings.json 里 `images.autoResize: false`）。

"不卡"只是现象，能证明改动真的生效的是：工具返回的 image 块解出来跟磁盘上的文件**逐字节相同**。
缩过、转过码的对不上。

用法：python3 verify_image_bytes.py <turn.events.jsonl> <原图路径>
退出码 0 = 原字节一致；1 = 不一致或没拿到图片块。
"""
import base64
import hashlib
import json
import os
import sys


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    dump, img = sys.argv[1], sys.argv[2]
    raw = open(img, "rb").read()
    want = hashlib.sha256(raw).hexdigest()

    blocks = []
    for line in open(dump, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "tool_execution_end":
            continue
        content = (e.get("result") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                blocks.append(b)

    print("原图 %s：%d 字节 sha256=%s" % (img, len(raw), want[:16]))
    if not blocks:
        print("[FAIL] 这一轮的工具返回里没有 image 块——图没交到模型手上")
        return 1
    ok = True
    for i, b in enumerate(blocks):
        data = b.get("data") or ""
        try:
            decoded = base64.b64decode(data)
        except Exception as exc:
            print("[FAIL] 第 %d 块 base64 解不开：%s" % (i, exc))
            ok = False
            continue
        got = hashlib.sha256(decoded).hexdigest()
        print("第 %d 块：base64 %d 字符 → %d 字节 mimeType=%s sha256=%s" % (
            i, len(data), len(decoded), b.get("mimeType"), got[:16]))
        if got != want:
            print("[FAIL] 与原图不一致：本机动过像素（或换过编码）")
            ok = False
    if ok:
        print("[PASS] 交出去的就是原字节（逐字节一致）")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
