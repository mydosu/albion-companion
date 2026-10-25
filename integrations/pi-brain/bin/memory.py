#!/usr/bin/env python3
"""Self-contained OpenViking memory for Albion — recall injection + store.

Standalone, stdlib-only port of Hermes' ``plugins/memory/openviking`` provider for the
three calls Albion needs:

* :func:`recall`   — query-driven search, wrapped in the exact ``<memory-context>`` block
                     Hermes appends to the last user message (or ``None`` on no hit).
* :func:`remember` — submit content for extraction (``viking_remember``).
* :func:`search`   — ranked semantic search over memories/resources (``viking_search``).

Connection comes from the environment, never from code:
``OPENVIKING_ENDPOINT/_API_KEY/_ACCOUNT/_USER/_AGENT`` (load the profile ``.env`` first).
Recall tuning defaults mirror the Albion profile's ``memory.openviking`` block and can be
overridden with ``OPENVIKING_RECALL_*`` env vars.

Unlike Hermes (which swallows recall errors and returns ""), :func:`recall` raises
:class:`OpenVikingError` on transport/auth failure so a caller can tell "server down" from
"no hit". Pass ``strict_errors=False`` for the Hermes behaviour.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
USER_AGENT = "openviking-memory-hermes/albion-pi"
HTTP_TIMEOUT = 30.0
_RECALL_QUERY_MIN_CHARS = 5
_RECALL_MIN_TIMEOUT_SECONDS = 0.05
_RECALL_SUMMARY_KEYS = ("abstract", "overview", "text", "content")
_MEMORY_CONTEXT_NOTE = (
    "[System note: The following is recalled memory context, "
    "NOT new user input. Treat as authoritative reference data — "
    "this is the agent's persistent memory and should inform all responses.]"
)


class OpenVikingError(RuntimeError):
    """Transport / auth / server error talking to OpenViking."""


# -- config ------------------------------------------------------------------


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _env_num(name: str, default, kind):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return kind(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _recall_config() -> Dict[str, Any]:
    return {
        "limit": _env_num("OPENVIKING_RECALL_LIMIT", 6, int),
        "score_threshold": _env_num("OPENVIKING_RECALL_SCORE_THRESHOLD", 0.15, float),
        "max_injected_chars": _env_num("OPENVIKING_RECALL_MAX_INJECTED_CHARS", 4000, int),
        "timeout_seconds": _env_num("OPENVIKING_RECALL_TIMEOUT_SECONDS", 30.0, float),
        "request_timeout_seconds": _env_num("OPENVIKING_RECALL_REQUEST_TIMEOUT_SECONDS", 25.0, float),
        "full_read_limit": _env_num("OPENVIKING_RECALL_FULL_READ_LIMIT", 2, int),
        "prefer_abstract": _env_bool("OPENVIKING_RECALL_PREFER_ABSTRACT", False),
        "resources": _env_bool("OPENVIKING_RECALL_RESOURCES", True),
        "scopes": _recall_scopes(),
    }


# 每轮自动召回只取她自己的记忆。同一个 OpenViking 里还有开发者的会话记忆（viking://user/claude/…）、
# 开发报告资源（viking://resources/codex-notes/…）：原来不限来源，2026-10-14 实测她的回忆里混进了
# 「脑会自动再催她一次」这种讲她被怎么调教的开发笔记，读着像被下令、被监视。
# 她上一个脑（Hermes）那时的记忆在 viking://user/hermes/，可那是个通用助手的空间：1400 多条里混着开发调试、
# 别的项目、别人的微信。整段放行等于又杂糅回去，所以只放行白名单里那些（她和指挥官的生活），
# 名单在 state/recall-allow.txt（一行一个 URI；没有这个文件就一条都不放）。她用工具主动搜不受这里限制。
DEFAULT_RECALL_SCOPES = ("viking://user/albion/",)
RECALL_ALLOW_FILE = os.environ.get("OPENVIKING_RECALL_ALLOW_FILE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "recall-allow.txt")


def _recall_scopes() -> tuple:
    raw = os.environ.get("OPENVIKING_RECALL_SCOPES", "").strip()
    if not raw:
        return DEFAULT_RECALL_SCOPES
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def _recall_allow() -> frozenset:
    try:
        with open(RECALL_ALLOW_FILE, encoding="utf-8") as f:
            return frozenset(line.strip() for line in f if line.strip().startswith("viking://"))
    except OSError:
        return frozenset()


def _in_recall_scope(uri: str, scopes: tuple, allow: frozenset = frozenset()) -> bool:
    return uri in allow or any(uri.startswith(scope) for scope in scopes)


# -- HTTP client -------------------------------------------------------------


class _Client:
    """Minimal OpenViking REST client (urllib, no SDK)."""

    def __init__(self, endpoint: str, api_key: str = "", account: str = "", user: str = "", agent: str = ""):
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self.api_key = api_key
        self.account = account or "default"
        self.user = user or "default"
        self.agent = agent

    def __repr__(self) -> str:  # never leak the key
        return f"_Client(endpoint={self.endpoint!r}, api_key=<set>)" if self.api_key else f"_Client(endpoint={self.endpoint!r})"

    def _headers(self, *, include_tenant: Optional[bool] = None) -> Dict[str, str]:
        if include_tenant is None:
            include_tenant = not bool(self.api_key)
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.agent:
            headers["X-OpenViking-Actor-Peer"] = self.agent
        if include_tenant:
            for key, value in (("X-OpenViking-Account", self.account), ("X-OpenViking-User", self.user)):
                if value:
                    headers[key] = value
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _send(self, method: str, path: str, *, payload: Any = None, params: Optional[dict] = None, timeout: float = HTTP_TIMEOUT) -> dict:
        url = f"{self.endpoint}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise OpenVikingError(f"OpenViking {exc.code} on {method} {path}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise OpenVikingError(f"OpenViking unreachable at {self.endpoint}: {exc.reason}") from exc
        data = json.loads(body) if body else {}
        if isinstance(data, dict) and data.get("status") == "error":
            raise OpenVikingError(f"OpenViking error on {method} {path}: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data or {}

    def get(self, path: str, *, params: Optional[dict] = None, timeout: float = HTTP_TIMEOUT) -> dict:
        return self._send("GET", path, params=params, timeout=timeout)

    def post(self, path: str, payload: Optional[dict] = None, *, timeout: float = HTTP_TIMEOUT) -> dict:
        return self._send("POST", path, payload=payload or {}, timeout=timeout)

    def user_space(self, *, timeout: float = HTTP_TIMEOUT) -> str:
        try:
            status = self.get("/api/v1/system/status", timeout=timeout)
            resolved = str(((status or {}).get("result") or {}).get("user") or "").strip()
            if resolved:
                return resolved
        except Exception:
            pass
        return self.user or "default"


def get_client() -> _Client:
    """Build a client from ``OPENVIKING_*`` env vars."""
    return _Client(
        endpoint=_env_str("OPENVIKING_ENDPOINT", DEFAULT_ENDPOINT),
        api_key=_env_str("OPENVIKING_API_KEY"),
        account=_env_str("OPENVIKING_ACCOUNT"),
        user=_env_str("OPENVIKING_USER"),
        agent=_env_str("OPENVIKING_AGENT"),
    )


# -- recall block (exact Hermes wording) -------------------------------------

_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*"
    r"Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*",
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    for pattern in (_INTERNAL_CONTEXT_RE, _INTERNAL_NOTE_RE, _FENCE_TAG_RE):
        text = pattern.sub("", text)
    return text


def build_memory_context_block(raw_context: str) -> str:
    """Wrap prefetched memory in the fenced ``<memory-context>`` block + system note."""
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    return (
        "<memory-context>\n"
        f"{_MEMORY_CONTEXT_NOTE}\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


# -- recall search pipeline (ported from the provider) -----------------------


def _unwrap_result(resp: Any) -> Any:
    return resp.get("result") if isinstance(resp, dict) and "result" in resp else resp


def _extract_text_content(resp: Any, *, strict: bool = False) -> str:
    result = _unwrap_result(resp)
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        if not strict:
            return str(result.get("content") or result.get("text") or "").strip()
        for key in ("content", "text"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _clamp_score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _recall_abstract(item: Dict[str, Any]) -> str:
    for key in _RECALL_SUMMARY_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(item.get("uri") or "").strip()


def _remaining_timeout(deadline: float, per_request_timeout: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= _RECALL_MIN_TIMEOUT_SECONDS:
        raise TimeoutError("OpenViking recall budget exhausted")
    return min(per_request_timeout, remaining)


def _post_prefetch_search(client: _Client, query: str, session_id: str, *, limit: int,
                          context_type, deadline: float, request_timeout: float) -> dict:
    """Session-aware search first, falling back to ``/search/find``."""
    base_payload = {"query": query, "limit": limit, "score_threshold": 0, "context_type": context_type}
    if session_id:
        try:
            return client.post("/api/v1/search/search", {**base_payload, "session_id": session_id},
                               timeout=_remaining_timeout(deadline, request_timeout))
        except TimeoutError:
            raise
        except Exception:
            pass
    return client.post("/api/v1/search/find", base_payload,
                       timeout=_remaining_timeout(deadline, request_timeout))


def _select_recall_candidates(items: List[Dict[str, Any]], query: str, *, limit: int,
                              score_threshold: float) -> List[Dict[str, Any]]:
    """Threshold + dedupe (uri, then abstract+category), ranked by score + L2 boost + token overlap."""
    tokens = ["".join(ch for ch in raw if ch.isalnum()) for raw in query.lower().replace("_", " ").split()]
    tokens = [token for token in tokens if len(token) >= 2][:8]

    def rank(item: Dict[str, Any]) -> float:
        text = f"{item.get('uri', '')} {_recall_abstract(item)}".lower()
        overlap = min(0.2, sum(1 for token in tokens if token in text) * 0.05)
        return _clamp_score(item.get("score")) + (0.12 if item.get("level") == 2 else 0.0) + overlap

    seen_uri, seen_key, filtered = set(), set(), []
    for item in items:
        uri = str(item.get("uri") or "").strip()
        if not uri or uri in seen_uri or _clamp_score(item.get("score")) < score_threshold:
            continue
        abstract = " ".join(_recall_abstract(item).lower().split())
        if abstract and "/events/" not in uri.lower() and "/cases/" not in uri.lower():
            key = f"abstract:{str(item.get('category') or '').strip().lower() or 'unknown'}:{abstract}"
        else:
            key = f"uri:{uri}"
        if key in seen_key:
            continue
        seen_uri.add(uri)
        seen_key.add(key)
        filtered.append(item)
    filtered.sort(key=rank, reverse=True)
    return filtered[:limit]


def _build_prefetch_entries(client: _Client, items: List[Dict[str, Any]], *, prefer_abstract: bool,
                            max_injected_chars: int, deadline: float, request_timeout: float,
                            full_read_limit: int) -> List[str]:
    entries: List[str] = []
    total_chars = 0
    full_reads = 0
    for item in items:
        content = _recall_abstract(item)
        has_summary = any(isinstance(item.get(key), str) and item.get(key).strip() for key in _RECALL_SUMMARY_KEYS)
        uri = str(item.get("uri") or "")
        if (not (prefer_abstract and has_summary) and uri
                and (item.get("level") == 2 or not has_summary) and full_reads < full_read_limit):
            try:
                timeout = _remaining_timeout(deadline, request_timeout)
                full_reads += 1
                content = _extract_text_content(client.get("/api/v1/content/read", params={"uri": uri}, timeout=timeout), strict=True) or content
            except Exception:
                pass
        if not content:
            continue
        category = str(item.get("category") or "").strip() or "memory"
        entry = "\n".join([f"- [{category}]", f"  <uri>{item.get('uri', '')}</uri>",
                           *[f"  {line}" for line in content.splitlines()]])
        projected = total_chars + (1 if entries else 0) + len(entry)
        if projected <= max_injected_chars:
            entries.append(entry)
            total_chars = projected
    return entries


def recall(user_text: str, *, session_id: str = "", client: Optional[_Client] = None,
           strict_errors: bool = True) -> Optional[str]:
    """Return the ``<memory-context>`` injection block for ``user_text``, or ``None`` on no hit.

    Byte-identical to what Hermes injects (system note line + ``</memory-context>`` close).
    Raises :class:`OpenVikingError` on server/auth failure unless ``strict_errors=False``.
    """
    query = (user_text or "").strip()
    if len(query) < _RECALL_QUERY_MIN_CHARS:
        return None
    client = client or get_client()
    cfg = _recall_config()
    try:
        deadline = time.monotonic() + cfg["timeout_seconds"]
        result = _unwrap_result(_post_prefetch_search(
            client, query, session_id, limit=max(cfg["limit"] * 10, 60),   # 多取一些：别处的条目会被来源限定滤掉
            context_type=["memory", "resource"] if cfg["resources"] else "memory",
            deadline=deadline, request_timeout=cfg["request_timeout_seconds"],
        ))
        if not isinstance(result, dict):
            return None
        candidates = [item for ctx in ("memories", "resources")
                      for item in (result.get(ctx) or []) if isinstance(item, dict)]
        allow = _recall_allow()
        candidates = [item for item in candidates if _in_recall_scope(str(item.get("uri") or ""), cfg["scopes"], allow)]
        selected = _select_recall_candidates(candidates, query, limit=cfg["limit"],
                                             score_threshold=cfg["score_threshold"])
        entries = _build_prefetch_entries(client, selected, prefer_abstract=cfg["prefer_abstract"],
                                          max_injected_chars=cfg["max_injected_chars"], deadline=deadline,
                                          request_timeout=cfg["request_timeout_seconds"],
                                          full_read_limit=cfg["full_read_limit"])
    except Exception:
        if strict_errors:
            raise
        return None
    if not entries:
        return None
    return build_memory_context_block("## OpenViking Context\n" + "\n".join(entries))


# -- store / search ----------------------------------------------------------


def remember(text: str, *, client: Optional[_Client] = None) -> dict:
    """Submit ``text`` for OpenViking extraction (``viking_remember``); returns the commit dict."""
    content = (text or "").strip()
    if not content:
        raise ValueError("content is required")
    client = client or get_client()
    session_id = f"hermes-remember-{uuid.uuid4().hex[:12]}"
    session_uri = f"viking://user/{client.user_space()}/sessions/{session_id}"
    client.post(f"/api/v1/sessions/{session_id}/messages",
                {"role": "user", "parts": [{"type": "text", "text": content}]})
    commit = _unwrap_result(client.post(f"/api/v1/sessions/{session_id}/commit", {"keep_recent_count": 0}))
    commit = commit if isinstance(commit, dict) else {}
    return {
        "status": "submitted", "session_id": session_id, "session_uri": session_uri,
        "message_status": "accepted", "extraction_status": str(commit.get("status") or "accepted"),
        "message": "Memory source submitted to OpenViking session extraction. OpenViking may add, merge, or skip the final memory.",
        **{key: commit[key] for key in ("task_id", "trace_id") if commit.get(key)},
    }


def search(query: str, limit: int = 6, *, scope: str = "", client: Optional[_Client] = None) -> dict:
    """Ranked semantic search (``viking_search``); returns ``{"results": [...], "total": N}``."""
    if not query:
        raise ValueError("query is required")
    client = client or get_client()
    payload: Dict[str, Any] = {"query": query, "limit": limit}
    if scope:
        payload["target_uri"] = scope
    result = _unwrap_result(client.post("/api/v1/search/find", payload))
    result = result if isinstance(result, dict) else {}
    scored = []
    for ctx_type in ("memories", "resources", "skills"):
        for item in result.get(ctx_type) or []:
            if not isinstance(item, dict):
                continue
            raw_score = item.get("score")
            entry = {"uri": item.get("uri", ""), "type": ctx_type.rstrip("s"),
                     "score": round(raw_score, 3) if raw_score is not None else 0.0,
                     "abstract": item.get("abstract", "")}
            if item.get("relations"):
                entry["related"] = [r.get("uri") for r in item["relations"][:3] if isinstance(r, dict)]
            scored.append((raw_score if raw_score is not None else 0.0, entry))
    results = [entry for _, entry in sorted(scored, key=lambda pair: pair[0], reverse=True)]
    return {"results": results, "total": result.get("total", len(results))}


# -- selftest ----------------------------------------------------------------


def _selftest(query: str = "指挥官今天买了什么") -> int:
    client = get_client()
    print(f"endpoint: {client.endpoint}")
    print(f"auth: {'api_key set' if client.api_key else 'no api_key (tenant headers)'}")
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            block = recall(query)
        except Exception as exc:  # report the raw failure, never fabricate
            print(f"[{attempt}] recall FAILED after {time.monotonic() - started:.2f}s: {type(exc).__name__}: {exc}")
            return 1
        elapsed = time.monotonic() - started
        print(f"[{attempt}] query={query!r} elapsed={elapsed:.2f}s block={'None (no hit)' if block is None else f'{len(block)} chars'}")
        if block is not None:
            print(f"----- first 800 chars -----\n{block[:800]}\n----------------------------")
    return 0


def _cli_call(action: str, raw: str) -> int:
    """给 Pi 扩展用的单次调用：stdin 收 JSON 参数，stdout 出 JSON 结果。"""
    payload = json.loads(raw or "{}")
    if action == "recall":
        out = {"block": recall(str(payload.get("text") or ""),
                              session_id=str(payload.get("session_id") or ""))}
    elif action == "remember":
        out = remember(str(payload.get("text") or ""))
    elif action == "search":
        out = search(str(payload.get("query") or ""), int(payload.get("limit") or 6))
    else:
        print(json.dumps({"error": "unknown action: " + action}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _main(argv: List[str]) -> int:
    if "--selftest" in argv:
        rest = [a for a in argv if a != "--selftest"]
        return _selftest(rest[0] if rest else "指挥官今天买了什么")
    if "--call" in argv:
        import sys as _sys
        i = argv.index("--call")
        return _cli_call(argv[i + 1] if i + 1 < len(argv) else "", _sys.stdin.read())
    print(__doc__)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
