"""
Queries a store-specific Dify Knowledge Base for relevant chunks and caches
results in a short-lived in-memory store, so repeated/similar queries within
the TTL window don't re-hit the API.

Multi-tenant design: each store gets its own Dify dataset (knowledge base),
so one store's documents are never visible to another store's queries. This
module is responsible for resolving "which store is asking" -> "which Dify
dataset_id to query" before ever calling Dify.

That mapping is meant to live in Appwrite long-term (a collection of
store_id -> dify_dataset_id). Until that's wired up, STORE_DATASET_MAP below
acts as a stand-in with the exact same shape, so swapping in the real
Appwrite call later is a one-function change, not a rewrite.

Keeps this concern separate from reply_generator.py on purpose:
  - rag_retriever.py decides WHICH store's data to search, WHAT context to
    fetch, and caches it
  - reply_generator.py decides HOW to phrase the answer using that context

Env vars expected:
  DIFY_BASE_URL         e.g. "https://dify.yourdomain.com"
  DIFY_DATASET_API_KEY  the "dataset-..." key from Dify (workspace-scoped —
                         the same key works across all datasets/knowledge
                         bases in your Dify workspace, so this stays a single
                         env var even though dataset_id varies per store)
"""

import os
import time
import hashlib
import requests

try:
    from langfuse import observe, get_client
except ImportError:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    def get_client(*args, **kwargs):
        return None

DIFY_BASE_URL = os.environ.get("DIFY_BASE_URL", "").rstrip("/")
DIFY_DATASET_API_KEY = os.environ.get("DIFY_DATASET_API_KEY", "")

# How long a cached result stays valid, in seconds. Short-lived by design —
# this is meant to smooth out repeated/near-duplicate queries in a single
# conversation burst, not act as a long-term store.
CACHE_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Store -> Dify dataset_id resolution
# ---------------------------------------------------------------------------

# Fallback map used only when Appwrite has no entry for a store.
# Only keep your known production / dev stores here.
STORE_DATASET_MAP: dict[str, str] = {
    "nhtcnc-hs.myshopify.com": "a4d60f13-ff14-4e91-a19f-621cf2e41bb2",
}

# Short-lived cache for store -> dataset_id lookups too, separate from the
# retrieval cache above, since this rarely changes and is worth avoiding a
# DB round-trip for on every single message.
_dataset_id_cache: dict[str, dict] = {}
DATASET_ID_CACHE_TTL_SECONDS = 3600


def _lookup_dataset_id_from_appwrite(store_identifier: str) -> str | None:
    """
    Query the Appwrite store_info collection for a dify_dataset_id field.
    Returns None when Appwrite is unconfigured or the store has no entry.
    """
    try:
        aw_endpoint  = os.environ.get("APPWRITE_ENDPOINT", "").rstrip("/")
        aw_project   = os.environ.get("APPWRITE_PROJECT_ID", "")
        aw_api_key   = os.environ.get("APPWRITE_API_KEY", "")
        aw_db_id     = os.environ.get("APPWRITE_DATABASE_ID", "")
        aw_col       = os.environ.get("APPWRITE_STORE_INFO_COLLECTION_ID", "store_info")
        if not all([aw_endpoint, aw_project, aw_api_key, aw_db_id]):
            return None
        import httpx
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{aw_endpoint}/databases/{aw_db_id}/collections/{aw_col}/documents",
                headers={"X-Appwrite-Project": aw_project, "X-Appwrite-Key": aw_api_key},
                params={"queries[]": f'equal("store","{store_identifier}")', "limit": "1"},
            )
        if resp.status_code == 200:
            docs = resp.json().get("documents", [])
            if docs and docs[0].get("dify_dataset_id"):
                return str(docs[0]["dify_dataset_id"])
    except Exception as e:
        print(f"rag_retriever: Appwrite lookup failed ({e!r}), falling back to STORE_DATASET_MAP")
    return None


def get_dataset_id(store_identifier: str, use_cache: bool = True) -> str | None:
    """
    Resolves a store's unique identifier to its Dify dataset_id.
    Returns None if the store has no dataset mapped anywhere (Appwrite or
    the pseudo dict) — callers should treat that as "no knowledge base
    configured for this store" and skip retrieval rather than error out.
    """
    if not store_identifier:
        return None

    if use_cache:
        now = time.time()
        cached = _dataset_id_cache.get(store_identifier)
        if cached and now - cached["timestamp"] < DATASET_ID_CACHE_TTL_SECONDS:
            return cached["dataset_id"]

    dataset_id = _lookup_dataset_id_from_appwrite(store_identifier)

    if dataset_id is None:
        # Fallback to the pseudo mapping until Appwrite is live.
        dataset_id = STORE_DATASET_MAP.get(store_identifier)

    if dataset_id and use_cache:
        _dataset_id_cache[store_identifier] = {
            "timestamp": time.time(),
            "dataset_id": dataset_id,
        }

    return dataset_id


# ---------------------------------------------------------------------------
# Retrieval (chunk fetching + caching)
# ---------------------------------------------------------------------------

# (store_identifier, query_hash) -> {"timestamp": float, "chunks": list[dict]}
_retrieval_cache: dict[str, dict] = {}


def _cache_key(store_identifier: str, query: str, top_k: int) -> str:
    raw = f"{store_identifier}::{query.strip().lower()}::{top_k}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prune_expired() -> None:
    now = time.time()
    expired = [k for k, v in _retrieval_cache.items() if now - v["timestamp"] > CACHE_TTL_SECONDS]
    for k in expired:
        del _retrieval_cache[k]

@observe(name="rag_retrieve")
def retrieve_context(
    store_identifier: str,
    query: str,
    top_k: int = 3,
    score_threshold: float | None = 0.5,
    use_cache: bool = True,
) -> list[dict]:
    """
    Returns a list of chunks: [{"content": str, "score": float, "document": str}, ...]
    scoped to the requesting store's own dataset only.

    Returns an empty list on any failure, including "store has no dataset
    mapped" — callers should treat that as "no relevant context found"
    rather than crash the reply pipeline.
    """
    if not query or not query.strip():
        return []

    dataset_id = get_dataset_id(store_identifier)
    if not dataset_id:
        print(f"rag_retriever: no dataset mapped for store '{store_identifier}', skipping retrieval")
        return []

    if not (DIFY_BASE_URL and DIFY_DATASET_API_KEY):
        print("rag_retriever: missing DIFY_BASE_URL / DIFY_DATASET_API_KEY, skipping retrieval")
        return []

    key = _cache_key(store_identifier, query, top_k)

    if use_cache:
        _prune_expired()
        cached = _retrieval_cache.get(key)
        if cached:
            return cached["chunks"]

    payload = {
        "query": query,
        "retrieval_model": {
            "search_method": "hybrid_search",
            "reranking_enable": False,
            "top_k": top_k,
            "score_threshold_enabled": score_threshold is not None,
            "score_threshold": score_threshold or 0.0,
        },
    }

    try:
        resp = requests.post(
            f"{DIFY_BASE_URL}/datasets/{dataset_id}/retrieve",
            headers={
                "Authorization": f"Bearer {DIFY_DATASET_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
    except Exception as e:
        print(f"rag_retriever: retrieval failed for store '{store_identifier}' ({e!r}), returning no context")
        return []

    chunks = [
        {
            "content": r.get("segment", {}).get("content", ""),
            "score": r.get("score", 0.0),
            "document": r.get("segment", {}).get("document", {}).get("name", ""),
        }
        for r in records
        if r.get("segment", {}).get("content")
    ]
    # Update Langfuse span with retrieval metadata (guard against missing client)
    try:
        cli = get_client()
        if cli and hasattr(cli, "update_current_span"):
            cli.update_current_span(metadata={"store": store_identifier, "chunks": len(chunks)})
    except Exception:
        pass

    if use_cache:
        _retrieval_cache[key] = {"timestamp": time.time(), "chunks": chunks}

    return chunks


def clear_cache() -> None:
    """Optional manual reset, e.g. between test runs or if a KB was just re-indexed."""
    _retrieval_cache.clear()
    _dataset_id_cache.clear()
