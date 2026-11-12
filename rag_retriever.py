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
import json
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

# Toggle flag: True = Send requests to separately deployed RAG service (Qdrant).
# False = Use internal local policy documents and uploaded store documents directly.
USE_REMOTE_RAG: bool = os.environ.get("USE_REMOTE_RAG", "true").lower() in ("true", "1", "yes")

def set_use_remote_rag(enabled: bool) -> bool:
    """Dynamically toggle between remote Qdrant RAG and internal local knowledge base."""
    global USE_REMOTE_RAG
    USE_REMOTE_RAG = bool(enabled)
    print(f"rag_retriever: USE_REMOTE_RAG set to {USE_REMOTE_RAG}")
    clear_cache()
    return USE_REMOTE_RAG

def is_remote_rag_enabled() -> bool:
    """Returns True if the remote Qdrant RAG service is currently active."""
    return bool(USE_REMOTE_RAG)

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

# ----------------------------------------------------------------------------- Local Knowledge Base (Fallback / Standalone)
LOCAL_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_knowledge_cache.json")
_local_store_lock = threading.Lock()
_LOCAL_STORE_DOCUMENTS: dict[str, list[dict]] = {}

def _load_local_store():
    global _LOCAL_STORE_DOCUMENTS
    if os.path.exists(LOCAL_STORE_PATH):
        try:
            with open(LOCAL_STORE_PATH, "r", encoding="utf-8") as f:
                _LOCAL_STORE_DOCUMENTS = json.load(f)
        except Exception as e:
            print(f"rag_retriever: note - could not load local_knowledge_cache: {e}")

def _save_local_store():
    try:
        with open(LOCAL_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(_LOCAL_STORE_DOCUMENTS, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"rag_retriever: note - could not save local_knowledge_cache: {e}")

_load_local_store()

DEFAULT_STORE_POLICIES = [
    {
        "doc_id": "policy:refund_policy",
        "title": "Refund and Return Policy",
        "kind": "policy",
        "policy_type": "refund_policy",
        "content": (
            "# Refund and Return Policy\n\n"
            "## 30-Day Return Guarantee\n"
            "We offer a 30-day return window from the day your package is marked as delivered by the carrier. "
            "If 30 days have passed since your purchase was delivered, we unfortunately cannot offer you a full refund or exchange.\n\n"
            "## Eligibility Criteria\n"
            "To be eligible for a return:\n"
            "- Items must be unused, unwashed, and in the same condition that you received them.\n"
            "- Items must be in the original packaging with all manufacturer tags and protective seals intact.\n"
            "- A valid order number or proof of purchase is required for all returns.\n\n"
            "## Non-Returnable Items\n"
            "Certain goods are exempt from being returned:\n"
            "- Perishable goods (food, flowers, consumables).\n"
            "- Personalized, engraved, or custom-made products.\n"
            "- Gift cards and downloadable software products.\n"
            "- Health, beauty, and personal care items with broken hygiene seals.\n\n"
            "## How to Initiate a Return\n"
            "1. Contact our customer support team with your order number and reason for return.\n"
            "2. If eligible, our team will provide a prepaid return shipping label and instructions.\n"
            "3. Package the item securely and drop it off at the designated carrier location.\n\n"
            "## Refund Processing Timeline\n"
            "Once your return is received and inspected by our warehouse (usually within 2-3 business days of arrival), "
            "we will send you an email notifying you of the approval or rejection of your refund. "
            "If approved, your refund will be processed to your original method of payment within 5 to 7 business days."
        ),
    },
    {
        "doc_id": "policy:shipping_policy",
        "title": "Shipping and Delivery Policy",
        "kind": "policy",
        "policy_type": "shipping_policy",
        "content": (
            "# Shipping and Delivery Policy\n\n"
            "## Order Processing Times\n"
            "All orders are processed and packed within 1 to 2 business days (excluding weekends and major holidays). "
            "You will receive an automated tracking confirmation email as soon as your order has been dispatched.\n\n"
            "## Domestic Shipping Rates and Delivery Estimates\n"
            "- Standard Shipping (3-5 business days): $4.99 flat rate, or FREE for all orders over $50.\n"
            "- Express / Expedited Shipping (1-2 business days): $12.99 flat rate.\n"
            "- Overnight Priority Delivery: Available at checkout based on destination carrier rates.\n\n"
            "## International Shipping\n"
            "We ship worldwide to most countries. International delivery typically takes 7 to 14 business days. "
            "Customs duties, import taxes, and VAT may be assessed by your local customs authorities and are the responsibility of the recipient.\n\n"
            "## Order Tracking\n"
            "Every shipped order includes an official carrier tracking link. You can track your shipment anytime by clicking the link in your dispatch email "
            "or by entering your order number and email into our order tracking assistant."
        ),
    },
    {
        "doc_id": "policy:cancellation_policy",
        "title": "Order Cancellation and Modification Policy",
        "kind": "policy",
        "policy_type": "cancellation_policy",
        "content": (
            "# Order Cancellation and Modification Policy\n\n"
            "## Order Modifications\n"
            "We process orders quickly to ensure rapid delivery. If you need to change your shipping address, item size, or color, "
            "please contact us immediately within 2 hours of placing your order.\n\n"
            "## Cancellation Window\n"
            "- Orders can be cancelled for a 100% full refund if requested before the order enters fulfillment (typically within 2 hours of placement).\n"
            "- Once an order has entered our automated warehouse fulfillment queue or has been dispatched, it cannot be cancelled. "
            "In that case, you may simply return the unopened package upon delivery according to our standard 30-day return policy."
        ),
    },
    {
        "doc_id": "policy:privacy_policy",
        "title": "Privacy Policy and Customer Data Security",
        "kind": "policy",
        "policy_type": "privacy_policy",
        "content": (
            "# Privacy Policy and Data Security\n\n"
            "## Data We Collect\n"
            "We collect customer details necessary to fulfill orders, including your name, shipping address, billing address, "
            "email address, and phone number for delivery notifications.\n\n"
            "## Payment Security\n"
            "All payment transactions are encrypted using bank-grade 256-bit SSL encryption. We never store or view your full credit card "
            "or debit card numbers. All payments are processed through PCI-DSS Level 1 certified gateways including Shopify Payments, PayPal, Apple Pay, and Shop Pay.\n\n"
            "## We Respect Your Privacy\n"
            "We do not sell, rent, or trade your personal information to third parties. Your data is used exclusively to process orders, "
            "provide customer support, and communicate essential updates about your purchases."
        ),
    },
    {
        "doc_id": "policy:warranty_policy",
        "title": "Warranty and Guarantee Policy",
        "kind": "policy",
        "policy_type": "warranty_policy",
        "content": (
            "# Warranty and Quality Guarantee\n\n"
            "## 1-Year Limited Manufacturer Warranty\n"
            "All our products come with a 1-year limited warranty against manufacturing defects and material flaws under normal consumer use.\n\n"
            "## What Is Covered\n"
            "- Defective seams, zippers, or hardware.\n"
            "- Material peeling or failure under normal care.\n"
            "- Electronic/hardware defects present at time of receipt.\n\n"
            "## What Is Not Covered\n"
            "- Normal wear and tear, accidents, or misuse.\n"
            "- Damage resulting from improper washing, alterations, or unauthorized repair.\n\n"
            "## Warranty Claims\n"
            "To file a warranty claim, contact our support team with your order number and clear photos showing the defect. "
            "We will repair, replace, or refund qualifying items free of charge."
        ),
    },
    {
        "doc_id": "faq:general_store_faq",
        "title": "Frequently Asked Questions (FAQs)",
        "kind": "faq",
        "policy_type": "faq",
        "content": (
            "# Frequently Asked Questions (FAQs)\n\n"
            "## What payment methods do you accept?\n"
            "We accept all major credit and debit cards (Visa, MasterCard, American Express, Discover), PayPal, Apple Pay, Google Pay, and Shop Pay.\n\n"
            "## How do I check the status of my order?\n"
            "You can ask our AI Shopping Assistant anytime by providing your order number or email, or click the tracking link in your confirmation email.\n\n"
            "## Do you offer exchanges?\n"
            "Yes! The fastest way to exchange an item for a different size or color is to return the original item for a refund and place a new order for the desired item.\n\n"
            "## How can I contact customer support?\n"
            "Our customer support team is available Monday through Friday from 9:00 AM to 6:00 PM. "
            "You can chat with our 24/7 AI Shopping Assistant right here on the store, or email us anytime."
        ),
    },
]

_STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by",
    "can", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing",
    "don't", "down", "during", "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself",
    "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is",
    "isn't", "it", "it's", "its", "itself", "let's", "me", "more", "most", "mustn't", "my", "myself",
    "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's", "should", "shouldn't", "so",
    "some", "such", "than", "that", "that's", "the", "their", "theirs", "them", "themselves", "then",
    "there", "there's", "these", "they", "they'd", "they'll", "they're", "they've", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're",
    "we've", "were", "weren't", "what", "what's", "when", "when's", "where", "where's", "which", "while",
    "who", "who's", "whom", "why", "why's", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll",
    "you're", "you've", "your", "yours", "yourself", "yourselves"
}

def _add_local_document(tenant: str, doc_id: str, text: str, title: str = "", kind: str = "document", **fields) -> None:
    tenant = (tenant or "default").strip().lower()
    with _local_store_lock:
        if tenant not in _LOCAL_STORE_DOCUMENTS:
            _LOCAL_STORE_DOCUMENTS[tenant] = []
        existing = [d for d in _LOCAL_STORE_DOCUMENTS[tenant] if d.get("doc_id") == doc_id]
        entry = {
            "doc_id": doc_id,
            "title": (title or doc_id).strip(),
            "content": text.strip(),
            "kind": kind,
            "updated_at": int(time.time()),
            **fields,
        }
        if existing:
            existing[0].update(entry)
        else:
            _LOCAL_STORE_DOCUMENTS[tenant].append(entry)
        _save_local_store()

def _remove_local_document(tenant: str, doc_id: str) -> bool:
    tenant = (tenant or "default").strip().lower()
    with _local_store_lock:
        if tenant in _LOCAL_STORE_DOCUMENTS:
            before = len(_LOCAL_STORE_DOCUMENTS[tenant])
            _LOCAL_STORE_DOCUMENTS[tenant] = [d for d in _LOCAL_STORE_DOCUMENTS[tenant] if d.get("doc_id") != doc_id]
            if len(_LOCAL_STORE_DOCUMENTS[tenant]) != before:
                _save_local_store()
                return True
    return False

def _list_local_documents(tenant: str) -> list[dict]:
    tenant = (tenant or "default").strip().lower()
    results = []
    # 1. Store-uploaded documents
    with _local_store_lock:
        for d in _LOCAL_STORE_DOCUMENTS.get(tenant, []):
            results.append({
                "doc_id": d.get("doc_id"),
                "title": d.get("title", d.get("doc_id")),
                "kind": d.get("kind", "document"),
                "source": "uploaded_store_document",
                "n_chunks": max(1, len(d.get("content", "")) // 500),
                "updated_at": d.get("updated_at", int(time.time())),
            })
    # 2. Built-in default sample policies
    for p in DEFAULT_STORE_POLICIES:
        results.append({
            "doc_id": p["doc_id"],
            "title": p["title"],
            "kind": p["kind"],
            "source": "built_in_sample_policy",
            "n_chunks": max(1, len(p["content"]) // 500),
            "updated_at": int(time.time()),
        })
    return results

def _local_retrieve(tenant: str, query: str, top_k: int = 3, hints: dict | None = None) -> list[dict]:
    """Scores local policy documents and uploaded store documents against the query."""
    q_lower = query.lower().strip()
    q_tokens = [w for w in re.findall(r"\w+", q_lower) if len(w) > 2 and w not in _STOPWORDS]
    target_policy = (hints or {}).get("policy_type", "").lower()

    candidates = []
    # 1. Custom uploaded documents for this tenant
    with _local_store_lock:
        custom_docs = list(_LOCAL_STORE_DOCUMENTS.get(tenant, []))
    for d in custom_docs:
        candidates.append({
            "doc_id": d.get("doc_id", "custom"),
            "title": d.get("title", "Custom Document"),
            "content": d.get("content", ""),
            "kind": d.get("kind", "document"),
            "policy_type": d.get("policy_type", ""),
            "is_custom": True,
        })
    # 2. Built-in sample policies
    for p in DEFAULT_STORE_POLICIES:
        candidates.append({
            "doc_id": p["doc_id"],
            "title": p["title"],
            "content": p["content"],
            "kind": p["kind"],
            "policy_type": p.get("policy_type", ""),
            "is_custom": False,
        })

    scored = []
    for cand in candidates:
        text = cand["content"]
        title = cand["title"]
        ptype = cand.get("policy_type", "")
        text_lower = text.lower()
        title_lower = title.lower()

        score = 0.0
        # Title matches: strong weight
        for tok in q_tokens:
            if tok in title_lower:
                score += 3.5
            if tok in text_lower:
                score += 1.0

        # Exact policy type hint matching
        if target_policy and (target_policy in cand["doc_id"] or target_policy in ptype):
            score += 6.0
        elif target_policy and target_policy.replace("_", " ") in title_lower:
            score += 5.0

        # Priority boost for custom uploaded store documents
        if cand.get("is_custom") and score > 0:
            score += 1.5

        if score > 0:
            norm_score = min(0.96, 0.70 + (score / 25.0) * 0.25)
            scored.append({
                "content": text,
                "score": round(norm_score, 3),
                "document": title,
                "title": title,
                "kind": cand.get("kind", "document"),
                "doc_id": cand.get("doc_id"),
                "source": "local_knowledge_base",
            })

    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored:
        for p in DEFAULT_STORE_POLICIES[:top_k]:
            scored.append({
                "content": p["content"],
                "score": 0.65,
                "document": p["title"],
                "title": p["title"],
                "kind": p["kind"],
                "doc_id": p["doc_id"],
                "source": "local_knowledge_base",
            })

    return scored[:top_k]


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
    """Final documents for one question, scoped to one store."""
    if not query or not query.strip() or not store_identifier:
        return []
    tenant, hints = _tenant(store_identifier), _auto_hints(query, hints)

    # 1. When remote RAG is turned off, retrieve directly from internal local knowledge base
    if not USE_REMOTE_RAG:
        return _local_retrieve(tenant, query, top_k=top_k, hints=hints)

    # 2. If remote RAG is enabled, verify readiness; if unconfigured/breaker open, fall back to local
    if not _ready(store_identifier, query):
        print(f"rag_retriever: remote RAG not configured or cooling down, using local knowledge fallback for '{tenant}'")
        return _local_retrieve(tenant, query, top_k=top_k, hints=hints)

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
        return _finish(tenant, data, score_threshold, key, use_cache)
    except Exception as e:
        _record(False)
        print(f"rag_retriever: remote retrieval failed for '{tenant}' ({e!r}), falling back to local knowledge base")
        return _local_retrieve(tenant, query, top_k=top_k, hints=hints)


async def aretrieve_context(store_identifier: str, query: str, top_k: int = 3, score_threshold: float | None = None,
                            use_cache: bool = True, hints: dict | None = None) -> list[dict]:
    """Async twin for async callers: does not block the event loop."""
    if not query or not query.strip() or not store_identifier:
        return []
    tenant, hints = _tenant(store_identifier), _auto_hints(query, hints)

    # 1. When remote RAG is turned off, retrieve directly from internal local knowledge base
    if not USE_REMOTE_RAG:
        return _local_retrieve(tenant, query, top_k=top_k, hints=hints)

    # 2. If remote RAG is enabled, verify readiness; if unconfigured/breaker open, fall back to local
    if not _ready(store_identifier, query):
        print(f"rag_retriever: remote RAG not configured or cooling down, using local knowledge fallback for '{tenant}'")
        return _local_retrieve(tenant, query, top_k=top_k, hints=hints)

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
        return _finish(tenant, data, score_threshold, key, use_cache)
    except Exception as e:
        _record(False)
        print(f"rag_retriever: remote retrieval failed for '{tenant}' ({e!r}), falling back to local knowledge base")
        return _local_retrieve(tenant, query, top_k=top_k, hints=hints)


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
    tenant = _tenant(store)
    # Always index into local knowledge base
    _add_local_document(tenant, doc_id, text, title=title, **fields)

    # Ingest into remote RAG if enabled and configured
    if USE_REMOTE_RAG and RAG_SERVICE_URL and RAG_API_KEY:
        try:
            return _admin("POST", "/v1/documents", json={"tenant": tenant, "doc_id": doc_id, "text": text,
                                                        "title": title, **fields})
        except Exception as e:
            print(f"rag_retriever: remote ingest failed ({e!r}), saved into local store documents.")
            return {"status": "ok", "doc_id": doc_id, "chunks": 1, "local_fallback": True}
    return {"status": "ok", "doc_id": doc_id, "chunks": 1, "local_fallback": True}


def delete_document(store: str, doc_id: str) -> dict:
    tenant = _tenant(store)
    _remove_local_document(tenant, doc_id)
    if USE_REMOTE_RAG and RAG_SERVICE_URL and RAG_API_KEY:
        try:
            return _admin("DELETE", "/v1/documents", json={"tenant": tenant, "doc_id": doc_id})
        except Exception:
            pass
    return {"status": "deleted", "doc_id": doc_id}


def list_documents(store: str) -> list[dict]:
    tenant = _tenant(store)
    if USE_REMOTE_RAG and RAG_SERVICE_URL and RAG_API_KEY:
        try:
            return _admin("GET", "/v1/documents", params={"tenant": tenant})["documents"]
        except Exception:
            pass
    return _list_local_documents(tenant)


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


def extract_text_from_file_bytes(filename: str, content: bytes) -> str:
    """Extracts plain text / markdown from uploaded document bytes.
    Supports .txt, .md, .markdown, .html, .htm, .json, .csv, and .pdf.
    """
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext == "pdf":
        try:
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages = []
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(f"## Page {i + 1}\n\n{page_text.strip()}")
            return "\n\n".join(pages).strip()
        except Exception as e:
            raise ValueError(f"Failed to parse PDF '{filename}': {e}")
    else:
        # Text-based formats: .txt, .md, .html, .csv, .json, etc.
        for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
            try:
                return content.decode(enc).strip()
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace").strip()


def ingest_file_document(store: str, filename: str, content: bytes, doc_title: str = "", doc_kind: str = "document") -> dict:
    """Extracts text from uploaded file bytes and sends it to the remote RAG service."""
    text = extract_text_from_file_bytes(filename, content)
    if not text:
        raise ValueError(f"File '{filename}' contains no extractable text.")
    clean_name = re.sub(r"[^\w\-.]", "_", filename).strip("_")
    doc_id = f"file:{clean_name}"
    title = (doc_title or "").strip() or filename
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    fmt = "html" if ext in ("html", "htm") else "markdown"
    return ingest_text(store, doc_id=doc_id, text=text, title=title, kind=doc_kind, format=fmt)
