# -*- coding: utf-8 -*-
"""请求体 → dict：按 Content-Type 的 charset 解码，失败回退 gb18030，再失败抛 ValueError。

2026-10-30：`await request.json()` 遇到非 UTF-8 的请求体（比如 Windows 客户端用 GBK 编码的中文）
直接抛 `UnicodeDecodeError` → FastAPI 返回 500。排查时被它误导成「服务挂了」。
现在先取 body bytes：按声明的 charset（缺省 utf-8）解码，解不开再试 gb18030（GBK 的超集），
还解不开就抛 `ValueError`，由调用方回 400。

只改「读请求体」这一件事；JSON 语法错误照旧是 400（不拿 gb18030 去猜一个本来就能解码的 UTF-8 体）。
"""
import json

DEFAULT_CHARSET = "utf-8"
FALLBACK_CHARSETS = ("gb18030",)


def declared_charset(request):
    """Content-Type 里的 charset=…（没有就 None）。"""
    ctype = request.headers.get("content-type") or ""
    for part in ctype.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip("\"'")
    return None


async def read_json_body(request):
    """解析 JSON 请求体；空 body 当 `{}`（与 FastAPI 的 422 不同：旧代码也是 400 缺字段）。"""
    raw = await request.body()
    if not raw:
        return {}
    charsets = []
    declared = declared_charset(request)
    for cs in ([declared] if declared else []) + [DEFAULT_CHARSET] + list(FALLBACK_CHARSETS):
        if cs.lower() not in [c.lower() for c in charsets]:
            charsets.append(cs)
    decoded = None
    last = None
    for cs in charsets:
        try:
            decoded = raw.decode(cs)
            break
        except (LookupError, UnicodeDecodeError) as e:      # 未知编码名 / 该编码下字节非法
            last = e
    if decoded is None:
        raise ValueError("请求体无法解码（charset=%s）: %s" % ("/".join(charsets),
                                                              type(last).__name__))
    try:
        return json.loads(decoded)
    except ValueError as e:
        raise ValueError("请求体不是合法 JSON: %s" % str(e)[:120]) from e
