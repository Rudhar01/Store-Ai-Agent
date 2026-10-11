"""
semantic.py
-----------
Optional "understand the meaning, not the exact words" layer, built on text
embeddings (vectors where similar meanings sit close together).

It adds two abilities and is completely optional - if no embedding backend is
configured, enabled() is False and the app behaves exactly as before.

  1. route_intent(message)       Semantic safety net for the intent classifier.
                                 Used only when the LLM classifier gives up
                                 ("fallback"): the message is compared with the
                                 example phrases in intent_examples.py (English,
                                 Hinglish, Hindi, Punjabi) and the closest
                                 action is used if it is clearly the best match.

  2. rank_products(shop, products, query)
                                 Semantic product search. "something cosy for
                                 the cold season" finds hoodies even though no
                                 product title contains those words.

Pick ONE embedding backend with env vars:

  A) Any OpenAI-compatible /v1/embeddings server (OpenAI, vLLM, Ollama, ...)
       EMBEDDINGS_BASE_URL   e.g. https://api.openai.com/v1  or  http://localhost:8001/v1
       EMBEDDINGS_MODEL      e.g. text-embedding-3-small  or  BAAI/bge-m3
       EMBEDDINGS_API_KEY    (if the server needs one)
  B) A local sentence-transformers model (pip install sentence-transformers)
       EMBEDDINGS_LOCAL_MODEL   e.g. paraphrase-multilingual-MiniLM-L12-v2

Use a MULTILINGUAL model - your shoppers write Hindi/Punjabi/Hinglish too.
Requires numpy.

Tuning (env, optional):
  SEMANTIC_ROUTER_MIN_SCORE   default 0.60  minimum similarity to trust the router
  SEMANTIC_ROUTER_MIN_MARGIN  default 0.03  best action must beat the runner-up by this
  SEMANTIC_PRODUCT_MIN_SCORE  default 0.30  minimum similarity for a product match
Similarity scales differ between models: if matches are too strict/loose,
adjust these (log a few real queries and look at the scores).
"""

import asyncio
import hashlib
import os
import re

import httpx

try:
    import numpy as np
except ImportError:  # semantic features simply stay off
    np = None

BASE_URL = os.environ.get("EMBEDDINGS_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("EMBEDDINGS_API_KEY", "")
API_MODEL = os.environ.get("EMBEDDINGS_MODEL", "")
LOCAL_MODEL = os.environ.get("EMBEDDINGS_LOCAL_MODEL", "")

ROUTER_MIN_SCORE = float(os.environ.get("SEMANTIC_ROUTER_MIN_SCORE", "0.60"))
ROUTER_MIN_MARGIN = float(os.environ.get("SEMANTIC_ROUTER_MIN_MARGIN", "0.03"))
PRODUCT_MIN_SCORE = float(os.environ.get("SEMANTIC_PRODUCT_MIN_SCORE", "0.30"))


def enabled() -> bool:
    return np is not None and (bool(BASE_URL and API_MODEL) or bool(LOCAL_MODEL))


# ---------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------
_local = None


def _load_local():
    global _local
    if _local is None:
        from sentence_transformers import SentenceTransformer
        _local = SentenceTransformer(LOCAL_MODEL)
    return _local


async def embed(texts: list[str]):
    """Returns an (n, dim) array of unit-length vectors, or None on any failure."""
    if not enabled() or not texts:
        return None
    try:
        if BASE_URL and API_MODEL:
            rows = []
            headers = {"Content-Type": "application/json"}
            if API_KEY:
                headers["Authorization"] = f"Bearer {API_KEY}"
            async with httpx.AsyncClient(timeout=30) as client:
                for i in range(0, len(texts), 64):
                    resp = await client.post(
                        f"{BASE_URL}/embeddings", headers=headers,
                        json={"model": API_MODEL, "input": texts[i:i + 64]},
                    )
                    if resp.status_code != 200:
                        print(f"semantic.embed -> {resp.status_code} {resp.text[:300]}")
                        return None
                    data = sorted(resp.json()["data"], key=lambda r: r["index"])
                    rows.extend(r["embedding"] for r in data)
            arr = np.asarray(rows, dtype="float32")
        else:
            model = await asyncio.to_thread(_load_local)
            arr = await asyncio.to_thread(lambda: np.asarray(model.encode(texts, normalize_embeddings=True), dtype="float32"))
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return arr / norms
    except Exception as e:  # noqa: BLE001
        print(f"semantic.embed failed: {e!r}")
        return None


# ---------------------------------------------------------------------
# 1. Intent router
# ---------------------------------------------------------------------
_router = {"vecs": None, "labels": []}
_router_lock = None


async def _ensure_router() -> bool:
    global _router_lock
    if _router["vecs"] is not None:
        return True
    if _router_lock is None:
        _router_lock = asyncio.Lock()
    async with _router_lock:
        if _router["vecs"] is not None:
            return True
        from intent_examples import EXAMPLES
        labels, texts = [], []
        for label, examples in EXAMPLES.items():
            for text in examples:
                labels.append(label)
                texts.append(text)
        vecs = await embed(texts)
        if vecs is None:
            return False
        _router["vecs"], _router["labels"] = vecs, labels
    return True


async def route_intent(message: str) -> dict | None:
    """Closest (intent, action) by meaning, or None if nothing is clearly closest.
    Result: {"intent", "action", "score", "margin"}."""
    if not enabled() or not (message or "").strip():
        return None
    if not await _ensure_router():
        return None
    q = await embed([message])
    if q is None or q.shape[1] != _router["vecs"].shape[1]:
        return None

    sims = _router["vecs"] @ q[0]
    best: dict = {}
    for label, s in zip(_router["labels"], sims):
        best[label] = max(best.get(label, -1.0), float(s))
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    (label, score) = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score - runner_up
    if score < ROUTER_MIN_SCORE or margin < ROUTER_MIN_MARGIN:
        return None
    return {"intent": label[0], "action": label[1], "score": score, "margin": margin}


# ---------------------------------------------------------------------
# 2. Semantic product search
# ---------------------------------------------------------------------
_catalog: dict = {}  # shop -> {"sig", "ids", "vecs"}


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def product_text(p: dict) -> str:
    tags = p.get("tags") or ""
    if isinstance(tags, list):
        tags = ", ".join(tags)
    return ". ".join(x for x in (p.get("title"), p.get("product_type"), tags, _strip_html(p.get("body_html"))[:300]) if x)


async def rank_products(shop: str, products: list[dict], query: str, limit: int = 6) -> list[dict] | None:
    """Products ranked by meaning, best first ([] = nothing close enough,
    None = semantic search unavailable). The catalog's vectors are cached per
    shop and rebuilt automatically when products are added or edited."""
    if not enabled() or not products or not (query or "").strip():
        return None

    sig = hashlib.sha1("|".join(f"{p.get('id')}:{p.get('updated_at')}" for p in products).encode()).hexdigest()
    entry = _catalog.get(shop)
    if not entry or entry["sig"] != sig:
        vecs = await embed([product_text(p) for p in products])
        if vecs is None:
            return None
        entry = {"sig": sig, "ids": [p.get("id") for p in products], "vecs": vecs}
        _catalog[shop] = entry

    q = await embed([query])
    if q is None or q.shape[1] != entry["vecs"].shape[1]:
        return None

    sims = entry["vecs"] @ q[0]
    order = sims.argsort()[::-1]
    best = float(sims[order[0]])
    if best < PRODUCT_MIN_SCORE:
        return []
    cutoff = max(PRODUCT_MIN_SCORE, best - 0.10)
    by_id = {p.get("id"): p for p in products}
    out = []
    for i in order[: limit * 2]:
        if float(sims[i]) < cutoff:
            break
        out.append(by_id[entry["ids"][i]])
    return out[:limit]
