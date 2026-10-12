"""
repository_appwrite.py
------------------------
Same function names and signatures as the SQLAlchemy repository.py, so
main.py's route handlers don't need to change — only the import line
does: `import repository_appwrite as repo` instead of `import repository`.

NEW IN THIS VERSION (to support the rich "RenderLink" dashboard UI):
  - ensure_customization() now also seeds `instructions`, `status`,
    and `widget_position` alongside the existing agent_name/agent_title/
    icon_type/theme_color/custom_icon_url fields.
  - get_dashboard_user_by_id() — needed because the dashboard now shows
    the logged-in owner's email in the sidebar/topbar.
  - Features, Store Info, FAQs, and Feedback: new collections + CRUD
    functions, mirroring the existing ensure_customization/
    update_customization pattern.

>>> ACTION NEEDED IN appwrite_client.py <<<
Add these four new collection ID constants (create the matching
collections in your Appwrite console first) and import them below:

    FEATURES_COLLECTION       — one doc per store. Boolean attributes:
                                 product_search, recommendations,
                                 product_filtering, warranty,
                                 cart_editing, returns, track_orders.
                                 Plus a `store` relationship/string attr.
    STORE_INFO_COLLECTION     — one doc per store. String attributes:
                                 business_name, support_email, timezone.
                                 Plus a `store` relationship/string attr.
    FAQS_COLLECTION           — many docs per store. String attributes:
                                 question, answer. Plus `store`.
    FEEDBACK_COLLECTION       — many docs per store (write-mostly).
                                 String attribute: message. Plus `store`.

Also add `instructions` (string, large), `status` (string, default
"active"), and `widget_position` (string, default "bottom-right") as
attributes on your existing CUSTOMIZATIONS_COLLECTION.
"""

import json
import re
from datetime import datetime, timedelta
try:
    from appwrite.query import Query
    from appwrite.id import ID
    from appwrite.input_file import InputFile
    from appwrite.exception import AppwriteException
except ImportError:
    class Query:
        @classmethod
        def equal(cls, *a): return str(a)
        @classmethod
        def order_desc(cls, *a): return str(a)
        @classmethod
        def limit(cls, *a): return str(a)
        @classmethod
        def greater_than(cls, *a): return str(a)
        @classmethod
        def less_than(cls, *a): return str(a)
    class ID:
        @classmethod
        def unique(cls): return "mock_unique_id"
    class InputFile:
        pass
    class AppwriteException(Exception):
        pass
from appwrite_client import (
    databases, storage, DATABASE_ID, STORES_COLLECTION, FLOWS_COLLECTION,
    REQUEST_LOGS_COLLECTION, CUSTOMIZATIONS_COLLECTION, DASHBOARD_USERS_COLLECTION,
    FEATURES_COLLECTION, STORE_INFO_COLLECTION, FAQS_COLLECTION, FEEDBACK_COLLECTION,
    APPWRITE_ENDPOINT, APPWRITE_PROJECT_ID, WIDGET_ICONS_BUCKET,
    NormalizedDocument, NormalizedDocumentList, _normalize_doc, _normalize_list,
)


def _find_store_doc(shop_domain: str):
    result = databases.list_documents(
        DATABASE_ID, STORES_COLLECTION,
        queries=[Query.equal("shop_domain", shop_domain)],
    )
    docs = _normalize_list(result)["documents"]
    return docs[0] if docs else None


def get_store(shop_domain: str) -> dict | None:
    """Returns the raw store document (not just the token), for callers
    that need more than the access token — e.g. checking is_new_store."""
    doc = _find_store_doc(shop_domain)
    if doc and doc.get("uninstalled"):
        return None
    return doc


def get_store_by_id(store_id: str) -> dict | None:
    try:
        return databases.get_document(DATABASE_ID, STORES_COLLECTION, store_id)
    except AppwriteException:
        return None


def upsert_shop(shop_domain: str, access_token: str, scopes: str) -> dict:
    existing = _find_store_doc(shop_domain)
    if existing:
        return databases.update_document(
            DATABASE_ID, STORES_COLLECTION, existing["$id"],
            data={"access_token": access_token, "scopes": scopes, "uninstalled": False},
        )
    return databases.create_document(
        DATABASE_ID, STORES_COLLECTION, ID.unique(),
        data={"shop_domain": shop_domain, "access_token": access_token, "scopes": scopes, "uninstalled": False},
    )


def get_shop_token(shop_domain: str) -> str | None:
    doc = _find_store_doc(shop_domain)
    if not doc or doc.get("uninstalled"):
        return None
    return doc["access_token"]


def mark_shop_uninstalled(shop_domain: str) -> None:
    doc = _find_store_doc(shop_domain)
    if doc:
        databases.update_document(
            DATABASE_ID, STORES_COLLECTION, doc["$id"],
            data={"uninstalled": True, "access_token": ""},
        )


def delete_shop(shop_domain: str) -> None:
    """Used by the shop/redact GDPR webhook — permanently removes the
    store and, since Appwrite relationship attributes can cascade,
    optionally its related documents too (configure on_delete when
    creating the relationship if you want automatic cascade)."""
    doc = _find_store_doc(shop_domain)
    if doc:
        databases.delete_document(DATABASE_ID, STORES_COLLECTION, doc["$id"])


# --- Flows ---

def save_flow(shop_domain: str, intent: str, action: str, url: str, steps: list) -> dict:
    store = _find_store_doc(shop_domain)
    if not store:
        raise ValueError(f"No store record for {shop_domain} — must complete OAuth install first")

    existing = databases.list_documents(
        DATABASE_ID, FLOWS_COLLECTION,
        queries=[Query.equal("intent", intent), Query.equal("action", action), Query.equal("store", store["$id"])],
    )
    steps_json = json.dumps(steps)

    if existing["documents"]:
        return databases.update_document(
            DATABASE_ID, FLOWS_COLLECTION, existing["documents"][0]["$id"],
            data={"url": url, "steps_json": steps_json},
        )
    return databases.create_document(
        DATABASE_ID, FLOWS_COLLECTION, ID.unique(),
        data={"intent": intent, "action": action, "url": url, "steps_json": steps_json, "store": store["$id"]},
    )


def get_flow(shop_domain: str, intent: str, action: str) -> dict | None:
    store = _find_store_doc(shop_domain)
    if not store:
        return None
    result = databases.list_documents(
        DATABASE_ID, FLOWS_COLLECTION,
        queries=[Query.equal("intent", intent), Query.equal("action", action), Query.equal("store", store["$id"])],
    )
    if not result["documents"]:
        return None
    doc = result["documents"][0]
    return {"url": doc["url"], "steps": json.loads(doc["steps_json"])}


# --- Request logs ---

def log_request(shop_domain: str, message: str, status: str, detected_intent=None, detected_action=None, reply: str = "", entities: dict | None = None) -> None:
    store = _find_store_doc(shop_domain)
    databases.create_document(
        DATABASE_ID, REQUEST_LOGS_COLLECTION, ID.unique(),
        data={
            "message": message,
            "status": status,
            "detected_intent": detected_intent,
            "detected_action": detected_action,
            "reply": reply,
            "entities": json.dumps(entities or {}, ensure_ascii=False),
            "store": store["$id"] if store else None,
        },
    )


# --- Agent analytics (dashboard "Insights" section) -------------------
#
# NOTE: this reads recent REQUEST_LOGS_COLLECTION rows and aggregates them
# in Python rather than in the database. That's fine at the volumes a
# single store will produce, but if a store's chat volume grows large,
# this should move to a scheduled aggregation job (or Appwrite Function)
# instead of scanning raw logs on every dashboard load.
#
# "Sales" caveat: the agent hands cart mutations off to the store's own
# Shopify checkout (see shopify_actions.py) — it never itself completes a
# purchase or sees a final order total. So there's no true revenue figure
# available from chat logs alone. The closest honest proxy is successful
# agent-assisted "add to cart" events, which is what get_agent_conversions_by_day
# below counts. If/when real Shopify order data (e.g. via an orders
# webhook) becomes available elsewhere in the app, that would be a more
# accurate source for an actual sales/revenue graph.

def get_recent_logs(store_id: str, limit: int = 2000) -> list[dict]:
    result = databases.list_documents(
        DATABASE_ID, REQUEST_LOGS_COLLECTION,
        queries=[Query.equal("store", store_id), Query.order_desc("$createdAt"), Query.limit(limit)],
    )
    return result["documents"]


def get_agent_conversions_by_day(store_id: str, days: int = 60) -> list[dict]:
    """Returns [{"date": "2026-08-20", "count": 3}, ...], one entry per day
    (oldest first, missing days filled with 0), counting successful
    agent-assisted 'add to cart' events as the best available proxy for
    an agent-driven conversion."""
    logs = get_recent_logs(store_id)
    cutoff = datetime.utcnow() - timedelta(days=days)
    buckets: dict[str, int] = {}
    for doc in logs:
        created = doc.get("$createdAt", "")
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        if dt < cutoff:
            continue
        if doc.get("detected_intent") == "cart_management" and doc.get("detected_action") == "add_item" and doc.get("status") == "done":
            key = dt.strftime("%Y-%m-%d")
            buckets[key] = buckets.get(key, 0) + 1

    out = []
    for i in range(days - 1, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": day, "count": buckets.get(day, 0)})
    return out


_NON_PRODUCT_PATTERNS = re.compile(
    r"\b(?:more|less)\s+(?:then|than)\s+\$?\d+\b|"
    r"^(?:give me|show me|find|search|filter|under|above|over)\s+(?:more\s+|less\s+)?(?:than\s+|then\s+)?\$?\d+|"
    r"^[\d\s\$,\.\+\-]+$",
    re.IGNORECASE,
)


def _is_plausible_product_name(text: str) -> bool:
    if not text or len(str(text).strip()) < 2:
        return False
    if _NON_PRODUCT_PATTERNS.search(str(text).strip()):
        return False
    return True


def _entity_text(entities: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = entities.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def get_top_searched_products(store_id: str, limit: int = 8) -> list[tuple[str, int]]:
    """Tallies product names/queries from product_search and
    cart_management log entries — prioritizing genuine product names."""
    logs = get_recent_logs(store_id)
    counts: dict[str, int] = {}
    for doc in logs:
        if doc.get("detected_intent") not in ("product_search", "cart_management"):
            continue
        try:
            entities = json.loads(doc.get("entities") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        name = _entity_text(entities, ("matched_product", "product_name_or_id", "product_name", "query"))
        if not name or not _is_plausible_product_name(name):
            continue
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


INTENT_LABELS = {
    "registration_login": ("Account & Login", "person"),
    "order_tracking": ("Order Tracking", "local_shipping"),
    "cart_management": ("Cart Management", "shopping_cart"),
    "warranty_claim": ("Warranty & Returns", "check_circle"),
    "product_search": ("Product Search", "search"),
    "policy_query": ("Store Policy Questions", "menu_book"),
    "fallback": ("Unclear / Other", "help_outline"),
}


def get_top_features_used(store_id: str, limit: int = 8) -> list[tuple[str, int]]:
    """Tallies how many logged interactions matched each intent — i.e.
    which agent capability shoppers actually use most."""
    logs = get_recent_logs(store_id)
    counts: dict[str, int] = {}
    for doc in logs:
        intent = doc.get("detected_intent")
        if not intent:
            continue
        counts[intent] = counts.get(intent, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


# --- Dashboard users (store owner's login) ---

def has_dashboard_user(store_id: str) -> bool:
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return len(result["documents"]) > 0


def get_dashboard_user_by_email(email: str) -> dict | None:
    normalized = (email or "").strip().lower()
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("email", normalized)],
    )
    return result["documents"][0] if result["documents"] else None


def get_dashboard_user_by_id(user_id: str) -> dict | None:
    try:
        return databases.get_document(DATABASE_ID, DASHBOARD_USERS_COLLECTION, user_id)
    except AppwriteException:
        return None


def create_dashboard_user(store_id: str, email: str, password_hash: str) -> dict:
    normalized = (email or "").strip().lower()
    return databases.create_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, ID.unique(),
        data={"store": store_id, "email": normalized, "password_hash": password_hash},
    )


# --- Agent customization (name, welcome/title, instructions, status, icon, theme, position) ---

def get_customization(store_id: str) -> dict | None:
    result = databases.list_documents(
        DATABASE_ID, CUSTOMIZATIONS_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return result["documents"][0] if result["documents"] else None


def ensure_customization(store_id: str) -> dict:
    """Creates a default customization row if one doesn't exist yet —
    called right after install, same as your old code did in the same
    db.commit() as creating the Store row."""
    existing = get_customization(store_id)
    if existing:
        return existing
    return databases.create_document(
        DATABASE_ID, CUSTOMIZATIONS_COLLECTION, ID.unique(),
        data={
            "store": store_id,
            "agent_name": "AI Assistant",
            "agent_title": "How can I help you today?",
            "theme_color": "#2b2b2b",
            "icon_type": "preset",
            "instructions": (
                "You are a helpful AI shopping assistant for this store. "
                "Be friendly, helpful and concise. Always try to provide "
                "accurate information about products, orders, shipping, "
                "returns and store policies."
            ),
            "status": "active",
            "widget_position": "bottom-right",
        },
    )


def update_customization(store_id: str, **fields) -> dict:
    existing = ensure_customization(store_id)
    return databases.update_document(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, existing["$id"], data=fields)


# --- Widget icon storage (Appwrite Storage bucket) ---------------------
#
# Uploaded icons live in Appwrite Storage rather than local disk, since
# Render's filesystem is ephemeral and wipes anything written there on
# every restart/redeploy. This gives back a stable, publicly-loadable
# URL that survives independently of the app server.

def _storage_file_url(file_id: str) -> str:
    return f"{APPWRITE_ENDPOINT}/storage/buckets/{WIDGET_ICONS_BUCKET}/files/{file_id}/view?project={APPWRITE_PROJECT_ID}"


def upload_icon_file(store_id: str, filename: str, content: bytes) -> str:
    """Uploads a custom widget icon to the Appwrite Storage bucket and
    returns its public view URL, ready to save as custom_icon_url."""
    file_id = ID.unique()
    storage.create_file(
        bucket_id=WIDGET_ICONS_BUCKET,
        file_id=file_id,
        file=InputFile.from_bytes(content, filename=filename),
    )
    return _storage_file_url(file_id)


def delete_icon_file(file_url: str) -> None:
    """Best-effort delete of a previously uploaded icon, given the URL
    stored in custom_icon_url. Silently no-ops if the URL doesn't look
    like one of our Storage file URLs (e.g. a leftover local-disk path
    from before this bucket existed) or if the file's already gone."""
    if not file_url or "/storage/buckets/" not in file_url:
        return
    try:
        file_id = file_url.split("/files/")[1].split("/")[0]
    except IndexError:
        return
    try:
        storage.delete_file(WIDGET_ICONS_BUCKET, file_id)
    except AppwriteException:
        pass


# --- Features (per-store toggles for what the AI agent can do) ---

_DEFAULT_FEATURES = {
    "product_search": True,
    "recommendations": True,
    "product_filtering": True,
    "warranty": True,
    "cart_editing": True,
    "returns": False,
    "track_orders": True,
}


def get_features(store_id: str) -> dict | None:
    result = databases.list_documents(
        DATABASE_ID, FEATURES_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return result["documents"][0] if result["documents"] else None


def ensure_features(store_id: str) -> dict:
    existing = get_features(store_id)
    if existing:
        return existing
    return databases.create_document(
        DATABASE_ID, FEATURES_COLLECTION, ID.unique(),
        data={"store": store_id, **_DEFAULT_FEATURES},
    )


def update_features(store_id: str, **fields) -> dict:
    existing = ensure_features(store_id)
    return databases.update_document(DATABASE_ID, FEATURES_COLLECTION, existing["$id"], data=fields)


def is_feature_enabled(store_id: str, feature_name: str) -> bool:
    try:
        features = get_features(store_id)
        if features and feature_name in features:
            return bool(features[feature_name])
    except Exception:
        pass
    return _DEFAULT_FEATURES.get(feature_name, True)


# --- Store info (business name, support email, timezone) ---

def get_store_info(store_id: str) -> dict | None:
    result = databases.list_documents(
        DATABASE_ID, STORE_INFO_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return result["documents"][0] if result["documents"] else None


def ensure_store_info(store_id: str) -> dict:
    existing = get_store_info(store_id)
    if existing:
        return existing
    return databases.create_document(
        DATABASE_ID, STORE_INFO_COLLECTION, ID.unique(),
        data={"store": store_id, "business_name": "", "support_email": "", "timezone": "UTC"},
    )


def update_store_info(store_id: str, **fields) -> dict:
    existing = ensure_store_info(store_id)
    return databases.update_document(DATABASE_ID, STORE_INFO_COLLECTION, existing["$id"], data=fields)


# --- Knowledge base / FAQs ---

def list_faqs(store_id: str) -> list[dict]:
    result = databases.list_documents(
        DATABASE_ID, FAQS_COLLECTION,
        queries=[Query.equal("store", store_id), Query.order_asc("$createdAt")],
    )
    return result["documents"]


def add_faq(store_id: str, question: str, answer: str) -> dict:
    return databases.create_document(
        DATABASE_ID, FAQS_COLLECTION, ID.unique(),
        data={"store": store_id, "question": question, "answer": answer},
    )


def delete_faq(store_id: str, faq_id: str) -> bool:
    """Deletes a FAQ only if it actually belongs to this store, so one
    store owner can't delete another store's FAQ by guessing an id."""
    try:
        doc = databases.get_document(DATABASE_ID, FAQS_COLLECTION, faq_id)
    except AppwriteException:
        return False
    owner_id = doc["store"]["$id"] if isinstance(doc.get("store"), dict) else doc.get("store")
    if owner_id != store_id:
        return False
    databases.delete_document(DATABASE_ID, FAQS_COLLECTION, faq_id)
    return True


# --- Feedback ---

def submit_feedback(store_id: str, message: str) -> dict:
    return databases.create_document(
        DATABASE_ID, FEEDBACK_COLLECTION, ID.unique(),
        data={"store": store_id, "message": message},
    )
"""
ADDITIONS FOR repository_appwrite.py
-------------------------------------
Append these functions to your existing repository_appwrite.py — they
assume the same `databases`, `DATABASE_ID`, `STORES_COLLECTION`,
`DASHBOARD_USERS_COLLECTION`, and `Query`/`ID` imports your file
already has.

REQUIRED NEW APPWRITE ATTRIBUTES (add these in the Appwrite console,
or via setup_collections.py if you're re-running it):

  On the `stores` collection:
    - setup_completed   (boolean, default: false)

  On the `dashboard_users` collection:
    - reset_token           (string, size 255, not required)
    - reset_token_expires   (string, size 64, not required)  -- stored
                              as an ISO datetime string
"""

import secrets
from datetime import datetime, timedelta, timezone


def is_setup_complete(store: dict) -> bool:
    return bool(store.get("setup_completed"))


def mark_setup_complete(store_id: str) -> dict:
    return databases.update_document(
        DATABASE_ID, STORES_COLLECTION, store_id, data={"setup_completed": True}
    )


def create_password_reset_token(email: str) -> str | None:
    """Returns a fresh reset token if the email matches a real dashboard
    user, or None if it doesn't. Callers should show the SAME message
    either way ("if that email exists, we sent a link") — never reveal
    whether an email is registered, that's a real security leak."""
    user = get_dashboard_user_by_email(email)
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    databases.update_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, user["$id"],
        data={"reset_token": token, "reset_token_expires": expires},
    )
    return token


def get_dashboard_user_by_reset_token(token: str) -> dict | None:
    """Returns the user if the token is valid AND not expired. A token
    older than 1 hour is treated as if it doesn't exist at all."""
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("reset_token", token)],
    )
    docs = result["documents"]
    if not docs:
        return None
    user = docs[0]
    expires = user.get("reset_token_expires")
    if not expires:
        return None
    try:
        if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
            return None  # expired
    except ValueError:
        return None
    return user


def reset_password(user_id: str, new_password_hash: str) -> None:
    """Sets the new password AND invalidates the reset token — a token
    must only ever be usable once."""
    databases.update_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, user_id,
        data={"password_hash": new_password_hash, "reset_token": None, "reset_token_expires": None},
    )


# ====================================================================
# ANALYTICS ADDITIONS  (append-only — nothing above this line was edited)
# --------------------------------------------------------------------
# Feeds the dashboard's /dashboard/analytics page. Two real data sources:
#
#  1) REQUEST_LOGS_COLLECTION (chat logs) — every logged shopper message
#     is one row, so message counts, per-day usage, cart adds per product
#     are all real. SESSIONS / UNIQUE USERS are only computed if your
#     log rows carry an id: a top-level `session_id` / `visitor_id`
#     attribute, OR the same keys inside the `entities` dict you already
#     pass to log_request(). Without an id they are reported as
#     "not available" — never guessed.
#
#  2) Shopify Admin API (orders / products) — real sales trend, purchased
#     products and category popularity. Needs the `read_orders` scope
#     (and `read_products` for categories) on the store's stored token.
#
# Storefront PAGE-VIEW tracking does not exist in this app (the assistant
# only sees what shoppers ask in chat), so product views are reported as
# not available until a storefront pixel is added.
# ====================================================================
import os as _os
import time as _time
from collections import defaultdict as _defaultdict


class AnalyticsUnavailable(Exception):
    """Raised when a metric can't be produced; `user_message` is shown
    to the merchant in place of the chart."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


_LOG_CACHE: dict = {}
_LOG_CACHE_TTL = 30  # seconds — several metrics share one log fetch


def _parse_created(doc: dict):
    try:
        return datetime.fromisoformat((doc.get("$createdAt") or "").replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _log_entities(doc: dict) -> dict:
    try:
        ents = json.loads(doc.get("entities") or "{}")
        return ents if isinstance(ents, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _get_logs_since(store_id: str, days: int, max_logs: int = 10000) -> list[dict]:
    """All of this store's chat logs from the last `days` days (paged, so
    longer ranges aren't cut off at the first page)."""
    key = (store_id, days)
    hit = _LOG_CACHE.get(key)
    if hit and _time.time() - hit[0] < _LOG_CACHE_TTL:
        return hit[1]

    cutoff = datetime.utcnow() - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    logs: list[dict] = []
    try:
        cursor = None
        while len(logs) < max_logs:
            queries = [
                Query.equal("store", store_id),
                Query.greater_than_equal("$createdAt", cutoff_iso),
                Query.order_desc("$createdAt"),
                Query.limit(500),
            ]
            if cursor:
                queries.append(Query.cursor_after(cursor))
            docs = databases.list_documents(DATABASE_ID, REQUEST_LOGS_COLLECTION, queries=queries)["documents"]
            if not docs:
                break
            logs.extend(docs)
            cursor = docs[-1]["$id"]
    except AppwriteException:
        # Fall back to the existing helper + Python-side date filtering.
        logs = [d for d in get_recent_logs(store_id) if (_parse_created(d) or datetime.min) >= cutoff]

    _LOG_CACHE[key] = (_time.time(), logs)
    return logs


_SESSION_KEYS = ("session_id", "sessionId", "conversation_id", "chat_id")
_VISITOR_KEYS = ("visitor_id", "visitorId", "user_id", "client_id", "anonymous_id")


def _first_id(doc: dict, ents: dict, keys: tuple):
    for k in keys:
        v = doc.get(k) or ents.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    return None


def get_chat_usage_by_day(store_id: str, days: int = 60) -> list[dict]:
    """[{"date", "messages", ["sessions"], ["unique_users"]}, ...] oldest
    first. sessions/unique_users appear only if the logs carry ids."""
    msgs: dict = _defaultdict(int)
    sess: dict = _defaultdict(set)
    users: dict = _defaultdict(set)
    have_s = have_u = False
    for doc in _get_logs_since(store_id, days):
        dt = _parse_created(doc)
        if not dt:
            continue
        day = dt.strftime("%Y-%m-%d")
        msgs[day] += 1
        ents = _log_entities(doc)
        s = _first_id(doc, ents, _SESSION_KEYS)
        if s:
            sess[day].add(s)
            have_s = True
        u = _first_id(doc, ents, _VISITOR_KEYS)
        if u:
            users[day].add(u)
            have_u = True

    out = []
    for i in range(days - 1, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        row = {"date": day, "messages": msgs.get(day, 0)}
        if have_s:
            row["sessions"] = len(sess.get(day, ()))
        if have_u:
            row["unique_users"] = len(users.get(day, ()))
        out.append(row)
    return out


def get_chat_summary(store_id: str, days: int = 60) -> dict:
    """Totals for the period. Keys with value None = not trackable yet."""
    all_s, all_u, total = set(), set(), 0
    for doc in _get_logs_since(store_id, days):
        total += 1
        ents = _log_entities(doc)
        s = _first_id(doc, ents, _SESSION_KEYS)
        u = _first_id(doc, ents, _VISITOR_KEYS)
        if s:
            all_s.add(s)
        if u:
            all_u.add(u)
    return {
        "messages": total,
        "sessions": len(all_s) if all_s else None,
        "unique_users": len(all_u) if all_u else None,
        "visitors": len(all_u) if all_u else None,
    }


def get_top_cart_added_products(store_id: str, days: int = 60, limit: int = 8) -> list[tuple[str, int]]:
    """Products shoppers successfully added to cart through the assistant."""
    counts: dict[str, int] = {}
    for doc in _get_logs_since(store_id, days):
        if not (doc.get("detected_intent") == "cart_management"
                and doc.get("detected_action") == "add_item" and doc.get("status") == "done"):
            continue
        name = _entity_text(_log_entities(doc), ("product_name_or_id", "query"))
        if name:
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def get_top_viewed_products(store_id: str, days: int = 60, limit: int = 8):
    fn = globals().get("events_top_viewed")
    if callable(fn):
        return fn(store_id, days, limit)
    return []


# ---- Shopify Admin API (real orders) -------------------------------
_SHOPIFY_API_VERSION = _os.environ.get("SHOPIFY_API_VERSION", "2024-10")
_ORDERS_CACHE: dict = {}
_ORDERS_CACHE_TTL = 60


def _shopify_store(store_id: str, scope: str) -> dict:
    store = get_store_by_id(store_id)
    if not store or store.get("uninstalled") or not store.get("access_token"):
        raise AnalyticsUnavailable("Your Shopify connection isn't active. Reinstall the app to enable sales analytics.")
    granted = {s.strip() for s in (store.get("scopes") or "").split(",") if s.strip()}
    if scope not in granted:
        raise AnalyticsUnavailable(
            f"Sales analytics needs the Shopify '{scope}' permission. Add it to your app's scopes "
            "and re-authorise the app, then refresh."
        )
    return store


def _shopify_paged(store: dict, path: str, params: dict, max_pages: int = 8) -> list[dict]:
    import httpx  # local import: only needed for sales analytics

    url = f"https://{store['shop_domain']}/admin/api/{_SHOPIFY_API_VERSION}/{path}"
    headers = {"X-Shopify-Access-Token": store["access_token"]}
    key = path.split(".")[0]
    items: list[dict] = []
    for _ in range(max_pages):
        resp = httpx.get(url, headers=headers, params=params, timeout=15.0)
        if resp.status_code in (401, 403):
            raise AnalyticsUnavailable("Shopify refused the request — re-authorise the app with order access.")
        if resp.status_code == 404:
            raise AnalyticsUnavailable(
                "Shopify returned 404 for this store — check the stored shop domain and that the app is still installed."
            )
        resp.raise_for_status()
        items.extend(resp.json().get(key, []))
        nxt = resp.links.get("next", {}).get("url")
        if not nxt:
            break
        url, params = nxt, None  # the next-page URL already carries its cursor
    return items


def _fetch_orders(store_id: str, days: int) -> list[dict]:
    key = (store_id, days)
    hit = _ORDERS_CACHE.get(key)
    if hit and _time.time() - hit[0] < _ORDERS_CACHE_TTL:
        return hit[1]
    store = _shopify_store(store_id, "read_orders")
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders = _shopify_paged(store, "orders.json", {
        "status": "any", "limit": 250, "created_at_min": since,
        "fields": "id,created_at,cancelled_at,total_price,currency,line_items",
    })
    orders = [o for o in orders if not o.get("cancelled_at")]
    _ORDERS_CACHE[key] = (_time.time(), orders)
    return orders


def get_sales_by_day(store_id: str, days: int = 60) -> list[dict]:
    """[{"date", "orders", "revenue", "currency"}, ...] from real Shopify orders or assistant events."""
    try:
        orders = _fetch_orders(store_id, days)
    except AnalyticsUnavailable:
        fn = globals().get("events_sales_by_day")
        if callable(fn):
            ev_sales = fn(store_id, days)
            if any(row.get("orders", 0) > 0 for row in ev_sales):
                return ev_sales
        raise
    buckets: dict = {}
    currency = ""
    for o in orders:
        day = (o.get("created_at") or "")[:10]
        if not day:
            continue
        b = buckets.setdefault(day, {"orders": 0, "revenue": 0.0})
        b["orders"] += 1
        try:
            b["revenue"] += float(o.get("total_price") or 0)
        except ValueError:
            pass
        currency = currency or (o.get("currency") or "")
    out = []
    for i in range(days - 1, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        b = buckets.get(day, {"orders": 0, "revenue": 0.0})
        out.append({"date": day, "orders": b["orders"], "revenue": round(b["revenue"], 2), "currency": currency})
    return out


def get_top_purchased_products(store_id: str, days: int = 60, limit: int = 8) -> list[tuple[str, int]]:
    try:
        orders = _fetch_orders(store_id, days)
    except AnalyticsUnavailable:
        fn = globals().get("events_top_purchases")
        if callable(fn):
            ev_purchases = fn(store_id, days, limit)
            if ev_purchases:
                return ev_purchases
        raise
    counts: dict[str, int] = {}
    for o in orders:
        for li in o.get("line_items", []):
            title = (li.get("title") or "").strip()
            if title:
                counts[title] = counts.get(title, 0) + int(li.get("quantity") or 0)
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def get_category_popularity(store_id: str, days: int = 60, limit: int = 8) -> list[tuple[str, int]]:
    """Units sold per Shopify product type (needs read_orders + read_products)."""
    try:
        orders = _fetch_orders(store_id, days)
        store = _shopify_store(store_id, "read_products")
        types = {
            p["id"]: (p.get("product_type") or "").strip() or "Uncategorized"
            for p in _shopify_paged(store, "products.json", {"limit": 250, "fields": "id,product_type"})
        }
        counts: dict[str, int] = {}
        for o in orders:
            for li in o.get("line_items", []):
                cat = types.get(li.get("product_id"), "Uncategorized")
                counts[cat] = counts.get(cat, 0) + int(li.get("quantity") or 0)
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    except AnalyticsUnavailable:
        fn = globals().get("events_category_popularity")
        if callable(fn):
            ev_cats = fn(store_id, days, limit)
            if ev_cats:
                return ev_cats
        raise


# ====================================================================
# ANALYTICS EVENTS TABLE  (append-only — nothing above this line was edited)
# --------------------------------------------------------------------
# One generic, platform-neutral table (`analytics_events`) that stores the
# raw facts the dashboard charts are built from, for ANY kind of store
# (Shopify, WooCommerce, custom...). Nothing in it is Shopify-specific:
#
#   event_type    what happened. Use these (or your own):
#                   "search"        shopper searched for something
#                   "cart_add"      item added to cart
#                   "purchase"      one row per product line bought (value = quantity)
#                   "order"         one row per order (value = order total)
#                   "product_view"  product page viewed
#                   "chat_message"  one chat message
#   name          product / search term
#   category      product category / type (optional)
#   value         quantity, or money amount for "order" rows
#   currency      e.g. "USD" (optional)
#   platform      "shopify", "woocommerce", ... (just a label)
#   session_id / visitor_id   anonymous random ids (optional)
#   external_id   the platform's own id (e.g. order id) so a retried
#                 webhook can never be counted twice (optional)
#
# Data is kept for EVENT_RETENTION_DAYS (60) and then removed by
# purge_old_events(). Every write helper here swallows its own errors and
# returns False, so analytics can never break a shopper's chat.
#
# SETUP (once): run  python -c "import repository_appwrite as r; print(r.setup_analytics_events_collection())"
# The collection id defaults to "analytics_events"; override with the
# ANALYTICS_EVENTS_COLLECTION_ID environment variable if you named it differently.
# ====================================================================
ANALYTICS_EVENTS_COLLECTION = _os.environ.get("ANALYTICS_EVENTS_COLLECTION_ID", "analytics_events")
EVENT_RETENTION_DAYS = 60

EVENT_SEARCH = "search"
EVENT_CART_ADD = "cart_add"
EVENT_PURCHASE = "purchase"
EVENT_ORDER = "order"
EVENT_PRODUCT_VIEW = "product_view"
EVENT_CHAT = "chat_message"


def setup_analytics_events_collection() -> dict:
    """Creates the collection, its attributes and indexes in Appwrite.
    Safe to run more than once: anything that already exists is reported
    as 'skipped'. Returns a {step: result} report."""
    report: dict = {}

    def step(label, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            report[label] = "ok"
        except AppwriteException as e:
            report[label] = f"skipped: {getattr(e, 'message', e)}"

    col = ANALYTICS_EVENTS_COLLECTION
    step("collection", databases.create_collection, DATABASE_ID, col, "analytics_events",
         permissions=[], document_security=False)

    strings = [
        ("store", 64, True), ("event_type", 32, True), ("platform", 32, False),
        ("name", 255, False), ("category", 255, False), ("currency", 8, False),
        ("session_id", 100, False), ("visitor_id", 100, False), ("external_id", 100, False),
    ]
    for key, size, required in strings:
        step(f"attr:{key}", databases.create_string_attribute, DATABASE_ID, col, key, size, required)
    step("attr:value", databases.create_float_attribute, DATABASE_ID, col, "value", False, default=1.0)

    # Attributes are created asynchronously; indexes need them "available".
    for _ in range(30):
        try:
            attrs = databases.list_attributes(DATABASE_ID, col)["attributes"]
            if attrs and all(a.get("status") == "available" for a in attrs):
                break
        except AppwriteException:
            pass
        _time.sleep(1)

    step("index:store", databases.create_index, DATABASE_ID, col, "idx_store", "key", ["store"])
    step("index:store_type", databases.create_index, DATABASE_ID, col, "idx_store_type", "key", ["store", "event_type"])
    step("index:external", databases.create_index, DATABASE_ID, col, "idx_external", "key", ["store", "event_type", "external_id"])
    return report


def _ev_clip(value, size: int) -> str:
    return str(value or "").strip()[:size]


def record_event(store_id: str, event_type: str, name: str = "", value: float = 1, *,
                 platform: str = "shopify", category: str = "", currency: str = "",
                 session_id: str = "", visitor_id: str = "", external_id: str = "") -> bool:
    """Stores one analytics event. Returns True if written, False if it was
    a duplicate (same external_id) or failed. Never raises."""
    try:
        if external_id:
            dup = databases.list_documents(
                DATABASE_ID, ANALYTICS_EVENTS_COLLECTION,
                queries=[Query.equal("store", store_id), Query.equal("event_type", event_type),
                         Query.equal("external_id", _ev_clip(external_id, 100)), Query.limit(1)],
            )
            if dup["documents"]:
                return False
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = 1.0
        databases.create_document(
            DATABASE_ID, ANALYTICS_EVENTS_COLLECTION, ID.unique(),
            data={
                "store": store_id,
                "event_type": _ev_clip(event_type, 32),
                "platform": _ev_clip(platform, 32),
                "name": _ev_clip(name, 255),
                "category": _ev_clip(category, 255),
                "value": amount,
                "currency": _ev_clip(currency, 8).upper(),
                "session_id": _ev_clip(session_id, 100),
                "visitor_id": _ev_clip(visitor_id, 100),
                "external_id": _ev_clip(external_id, 100),
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 — analytics must never break the caller
        print(f"repository_appwrite: record_event failed: {e!r}")
        return False


def purge_old_events(days: int = EVENT_RETENTION_DAYS, max_delete: int = 20000) -> int:
    """Deletes events older than `days` (default 60). Returns how many were
    removed. Call it on a schedule (e.g. once a day)."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    deleted = 0
    while deleted < max_delete:
        try:
            docs = databases.list_documents(
                DATABASE_ID, ANALYTICS_EVENTS_COLLECTION,
                queries=[Query.less_than("$createdAt", cutoff_iso), Query.limit(100)],
            )["documents"]
        except AppwriteException:
            break
        if not docs:
            break
        for doc in docs:
            try:
                databases.delete_document(DATABASE_ID, ANALYTICS_EVENTS_COLLECTION, doc["$id"])
                deleted += 1
            except AppwriteException:
                pass
    return deleted


# ---- reading (platform-neutral) ------------------------------------
_EV_CACHE: dict = {}
_EV_CACHE_TTL = 30  # seconds


def _events_since(store_id: str, days: int, event_types: tuple, max_rows: int = 10000) -> list[dict]:
    key = (store_id, days, event_types)
    hit = _EV_CACHE.get(key)
    if hit and _time.time() - hit[0] < _EV_CACHE_TTL:
        return hit[1]
    cutoff = datetime.utcnow() - timedelta(days=min(days, EVENT_RETENTION_DAYS))
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    rows: list[dict] = []
    cursor = None
    while len(rows) < max_rows:
        queries = [
            Query.equal("store", store_id),
            Query.equal("event_type", list(event_types)),
            Query.greater_than_equal("$createdAt", cutoff_iso),
            Query.order_desc("$createdAt"),
            Query.limit(500),
        ]
        if cursor:
            queries.append(Query.cursor_after(cursor))
        docs = databases.list_documents(DATABASE_ID, ANALYTICS_EVENTS_COLLECTION, queries=queries)["documents"]
        if not docs:
            break
        rows.extend(docs)
        cursor = docs[-1]["$id"]
    _EV_CACHE[key] = (_time.time(), rows)
    return rows


def _ev_num(v):
    return int(v) if float(v) == int(v) else round(float(v), 2)


def _ev_ranked(store_id: str, days: int, limit: int, event_type: str, field: str = "name", weighted: bool = False):
    counts: dict = {}
    for doc in _events_since(store_id, days, (event_type,)):
        label = (doc.get(field) or "").strip() or ("Uncategorized" if field == "category" else "")
        if not label:
            continue
        if event_type == EVENT_SEARCH and not _is_plausible_product_name(label):
            continue
        try:
            amount = float(doc.get("value") or 1) if weighted else 1
        except (TypeError, ValueError):
            amount = 1
        counts[label] = counts.get(label, 0) + amount
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(n, _ev_num(c)) for n, c in ranked]


def events_top_searches(store_id: str, days: int = 60, limit: int = 8):
    return _ev_ranked(store_id, days, limit, EVENT_SEARCH)


def events_top_cart_adds(store_id: str, days: int = 60, limit: int = 8):
    return _ev_ranked(store_id, days, limit, EVENT_CART_ADD)


def events_top_purchases(store_id: str, days: int = 60, limit: int = 8):
    return _ev_ranked(store_id, days, limit, EVENT_PURCHASE, weighted=True)


def events_top_viewed(store_id: str, days: int = 60, limit: int = 8):
    return _ev_ranked(store_id, days, limit, EVENT_PRODUCT_VIEW)


def events_category_popularity(store_id: str, days: int = 60, limit: int = 8):
    return _ev_ranked(store_id, days, limit, EVENT_PURCHASE, field="category", weighted=True)


def _ev_day_list(days: int) -> list[str]:
    return [(datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def events_chat_usage_by_day(store_id: str, days: int = 60) -> list[dict]:
    msgs: dict = _defaultdict(int)
    sess: dict = _defaultdict(set)
    users: dict = _defaultdict(set)
    have_s = have_u = False
    for doc in _events_since(store_id, days, (EVENT_CHAT,)):
        dt = _parse_created(doc)
        if not dt:
            continue
        day = dt.strftime("%Y-%m-%d")
        msgs[day] += 1
        if (doc.get("session_id") or "").strip():
            sess[day].add(doc["session_id"])
            have_s = True
        if (doc.get("visitor_id") or "").strip():
            users[day].add(doc["visitor_id"])
            have_u = True
    out = []
    for day in _ev_day_list(days):
        row = {"date": day, "messages": msgs.get(day, 0)}
        if have_s:
            row["sessions"] = len(sess.get(day, ()))
        if have_u:
            row["unique_users"] = len(users.get(day, ()))
        out.append(row)
    return out


def events_chat_summary(store_id: str, days: int = 60) -> dict:
    all_s, all_u, total = set(), set(), 0
    for doc in _events_since(store_id, days, (EVENT_CHAT,)):
        total += 1
        if (doc.get("session_id") or "").strip():
            all_s.add(doc["session_id"])
        if (doc.get("visitor_id") or "").strip():
            all_u.add(doc["visitor_id"])
    return {
        "messages": total,
        "sessions": len(all_s) if all_s else None,
        "unique_users": len(all_u) if all_u else None,
        "visitors": len(all_u) if all_u else None,
    }


def events_cart_adds_by_day(store_id: str, days: int = 60) -> list[dict]:
    buckets: dict = _defaultdict(int)
    for doc in _events_since(store_id, days, (EVENT_CART_ADD,)):
        dt = _parse_created(doc)
        if dt:
            buckets[dt.strftime("%Y-%m-%d")] += 1
    return [{"date": day, "count": buckets.get(day, 0)} for day in _ev_day_list(days)]


def events_sales_by_day(store_id: str, days: int = 60) -> list[dict]:
    """From "order" events: value = order total."""
    buckets: dict = {}
    currency = ""
    for doc in _events_since(store_id, days, (EVENT_ORDER,)):
        dt = _parse_created(doc)
        if not dt:
            continue
        b = buckets.setdefault(dt.strftime("%Y-%m-%d"), {"orders": 0, "revenue": 0.0})
        b["orders"] += 1
        try:
            b["revenue"] += float(doc.get("value") or 0)
        except (TypeError, ValueError):
            pass
        currency = currency or (doc.get("currency") or "")
    out = []
    for day in _ev_day_list(days):
        b = buckets.get(day, {"orders": 0, "revenue": 0.0})
        out.append({"date": day, "orders": b["orders"], "revenue": round(b["revenue"], 2), "currency": currency})
    return out


# Metric names are the ones dashboard_nicegui.py already uses.
EVENT_PROVIDERS = {
    "chat_summary": events_chat_summary,
    "chat_usage_daily": events_chat_usage_by_day,
    "product_views": events_top_viewed,
    "product_searches": events_top_searches,
    "product_purchases": events_top_purchases,
    "sales_daily": events_sales_by_day,
    "category_popularity": events_category_popularity,
    "agent_cart_adds": events_cart_adds_by_day,
    "product_cart_adds": events_top_cart_adds,
}


def register_event_providers(register_fn, platform: str = "generic", metrics=None) -> None:
    """Plugs the events table into the dashboard for one platform, using
    the dashboard's own register_analytics_provider as `register_fn`:

        from dashboard_nicegui import register_analytics_provider
        repo.register_event_providers(register_analytics_provider, platform="woocommerce")

    Pass `metrics=["product_searches", "product_views"]` to register only
    some metrics (e.g. keep Shopify's live order data but take searches
    from this table)."""
    for metric, fn in EVENT_PROVIDERS.items():
        if metrics is None or metric in metrics:
            register_fn(platform, metric, fn)
