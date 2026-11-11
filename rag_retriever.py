"""
Backend-side RAG client. Drop-in replacement for the old rag_retriever.py.

reply_generator.py keeps calling  retrieve_context(store_identifier, query)  and gets the same shape back:
    [{"content": str, "score": float, "document": str, ...extra fields}, ...]

All retrieval logic (hybrid search, translation, reranking, cutoffs) lives in the remote RAG service; this file only
asks it and returns the final documents. Any failure returns [] so a RAG outage never breaks a reply.

Env vars (backend):
  RAG_SERVICE_URL   e.g. https://rag.yourdomain.com
  RAG_API_KEY       the same key the service was started with
  RAG_TIMEOUT_S     total time allowed for one retrieval (default 6)
"""
import hashlib
import os
import re
import threading
import time

import httpx

if (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip() and (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip():
    try:
        from langfuse import observe, get_client
    except ImportError:
        def observe(*args, **kwargs):
            def decorator(f):
                return f
            return decorator

        def get_client(*args, **kwargs):
            return None
else:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator

    def get_client(*args, **kwargs):
        return None

RAG_SERVICE_URL = os.environ.get("RAG_SERVICE_URL", "").rstrip("/")
RAG_API_KEY = os.environ.get("RAG_API_KEY", "")
RAG_TIMEOUT_S = float(os.environ.get("RAG_TIMEOUT_S", "6"))
CACHE_TTL_SECONDS = 300

# chatbot_widget builds policy queries as "refund policy: <message>". Those prefixes become a metadata hint.
KNOWN_POLICY_TYPES = set(filter(None, os.environ.get(
    "RAG_POLICY_TYPES", "refund_policy,shipping_policy,privacy_policy,terms_of_service,warranty_policy").split(",")))
_PREFIX = re.compile(r"^\s*([A-Za-z_ ]{3,40}?)\s*:\s")

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_http: httpx.Client | None = None

# circuit breaker: after 3 failures in a row, skip the service for 30 s instead of adding a timeout to every chat
_fail_count, _open_until = 0, 0.0
_BREAKER_FAILS, _BREAKER_SECONDS = 3, 30.0


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=RAG_TIMEOUT_S)
    return _http


def _headers() -> dict:
    return {"Authorization": f"Bearer {RAG_API_KEY}"}


def _tenant(store_identifier: str) -> str:
    return (store_identifier or "").strip().lower()


def _auto_hints(query: str, hints: dict | None) -> dict:
    out = dict(hints or {})
    if "policy_type" not in out:
        m = _PREFIX.match(query)
        if m:
            candidate = m.group(1).strip().lower().replace(" ", "_")
            if candidate in KNOWN_POLICY_TYPES:
                out["policy_type"] = candidate
    return out


def _breaker_open() -> bool:
    return time.time() < _open_until


def _record(ok: bool) -> None:
    global _fail_count, _open_until
    if ok:
        _fail_count = 0
        return
    _fail_count += 1
    if _fail_count >= _BREAKER_FAILS:
        _open_until = time.time() + _BREAKER_SECONDS
        _fail_count = 0
        print(f"rag_retriever: RAG service failing, pausing retrieval for {int(_BREAKER_SECONDS)}s")


def _cache_key(tenant: str, query: str, top_k: int, threshold, hints: dict) -> str:
    raw = f"{tenant}::{query.strip().lower()}::{top_k}::{threshold}::{sorted(hints.items())}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prune_expired() -> None:
    now = time.time()
    with _cache_lock:
        for k in [k for k, v in _cache.items() if now - v["timestamp"] > CACHE_TTL_SECONDS]:
            _cache.pop(k, None)


def _payload(tenant, query, top_k, hints) -> dict:
    return {"tenant": tenant, "query": query, "top_k": top_k, "hints": hints or None,
            "deadline_ms": int(max(RAG_TIMEOUT_S - 1.0, 1.0) * 1000)}


def _finish(tenant, data, threshold, key, use_cache) -> list[dict]:
    chunks = [r for r in data.get("results", []) if r.get("content")]
    if threshold is not None:     # optional extra cutoff; the service already applies its own
        chunks = [r for r in chunks if r.get("score", 0.0) >= threshold]
    try:
        cli = get_client()
        if cli and hasattr(cli, "update_current_span"):
            cli.update_current_span(metadata={"store": tenant, "chunks": len(chunks), "rag_meta": data.get("meta")})
    except Exception:
        pass
    if use_cache:
        with _cache_lock:
            _cache[key] = {"timestamp": time.time(), "chunks": chunks}
    return chunks


def _ready(store_identifier: str, query: str) -> bool:
    if not query or not query.strip() or not store_identifier:
        return False
    if not (RAG_SERVICE_URL and RAG_API_KEY):
        print("rag_retriever: RAG_SERVICE_URL / RAG_API_KEY not set, skipping retrieval")
        return False
    return not _breaker_open()


@observe(name="rag_retrieve")
def retrieve_context(
    store_identifier: str,
    query: str,
    top_k: int = 3,
    score_threshold: float | None = None,
    use_cache: bool = True,
    hints: dict | None = None,
) -> list[dict]:
    """Final documents for one question, scoped to one store. Returns [] on any failure."""
    if not _ready(store_identifier, query):
        return []
    tenant, hints = _tenant(store_identifier), _auto_hints(query, hints)
    key = _cache_key(tenant, query, top_k, score_threshold, hints)
    if use_cache:
        _prune_expired()
        with _cache_lock:
            hit = _cache.get(key)
        if hit:
            return hit["chunks"]
    try:
        r = _client().post(f"{RAG_SERVICE_URL}/v1/retrieve", headers=_headers(), json=_payload(tenant, query, top_k, hints))
        r.raise_for_status()
        data = r.json()
        _record(True)
    except Exception as e:
        _record(False)
        print(f"rag_retriever: retrieval failed for '{tenant}' ({e!r}), returning no context")
        return []
    return _finish(tenant, data, score_threshold, key, use_cache)


async def aretrieve_context(store_identifier: str, query: str, top_k: int = 3, score_threshold: float | None = None,
                            use_cache: bool = True, hints: dict | None = None) -> list[dict]:
    """Async twin for async callers: does not block the event loop."""
    if not _ready(store_identifier, query):
        return []
    tenant, hints = _tenant(store_identifier), _auto_hints(query, hints)
    key = _cache_key(tenant, query, top_k, score_threshold, hints)
    if use_cache:
        _prune_expired()
        with _cache_lock:
            hit = _cache.get(key)
        if hit:
            return hit["chunks"]
    try:
        async with httpx.AsyncClient(timeout=RAG_TIMEOUT_S) as c:
            r = await c.post(f"{RAG_SERVICE_URL}/v1/retrieve", headers=_headers(), json=_payload(tenant, query, top_k, hints))
        r.raise_for_status()
        data = r.json()
        _record(True)
    except Exception as e:
        _record(False)
        print(f"rag_retriever: retrieval failed for '{tenant}' ({e!r}), returning no context")
        return []
    return _finish(tenant, data, score_threshold, key, use_cache)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ----------------------------------------------------------------------------- admin helpers
# Used by dashboards, install / uninstall webhooks and scripts. They raise on failure (unlike retrieval).
def _admin(method: str, path: str, **kw) -> dict:
    if not (RAG_SERVICE_URL and RAG_API_KEY):
        raise RuntimeError("RAG_SERVICE_URL / RAG_API_KEY not set")
    r = httpx.request(method, f"{RAG_SERVICE_URL}{path}", headers=_headers(), timeout=120, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"RAG service {r.status_code}: {r.text[:300]}")
    clear_cache()           # content changed: drop cached answers
    return r.json()


def ingest_text(store: str, doc_id: str, text: str, title: str = "", **fields) -> dict:
    """fields: kind, policy_type, url, format ('markdown'|'html'|'text')."""
    return _admin("POST", "/v1/documents", json={"tenant": _tenant(store), "doc_id": doc_id, "text": text,
                                                "title": title, **fields})


def delete_document(store: str, doc_id: str) -> dict:
    return _admin("DELETE", "/v1/documents", json={"tenant": _tenant(store), "doc_id": doc_id})


def list_documents(store: str) -> list[dict]:
    return _admin("GET", "/v1/documents", params={"tenant": _tenant(store)})["documents"]


def crawl_site(store: str, url: str, limit: int = 25, include_paths: list[str] | None = None,
               exclude_paths: list[str] | None = None) -> dict:
    return _admin("POST", "/v1/crawl", json={"tenant": _tenant(store), "url": url, "limit": limit,
                                            "include_paths": include_paths, "exclude_paths": exclude_paths})


def scrape_page(store: str, url: str) -> dict:
    return _admin("POST", "/v1/scrape", json={"tenant": _tenant(store), "url": url})


def job_status(job_id: str) -> dict:
    return _admin("GET", f"/v1/crawl/{job_id}")


def store_stats(store: str) -> dict:
    return _admin("GET", f"/v1/tenants/{_tenant(store)}/stats")


def delete_store(store: str) -> dict:
    """Call from your uninstall and shop/redact webhooks."""
    return _admin("DELETE", f"/v1/tenants/{_tenant(store)}")