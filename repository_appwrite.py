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
import time
from datetime import datetime, timedelta, timezone
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


def _normalize_domain(domain: str) -> str:
    """Normalize a shop domain to canonical *.myshopify.com form."""
    if not domain:
        return ""
    d = str(domain).strip().lower()
    # Strip protocol
    d = re.sub(r"^https?://", "", d)
    # Strip path / query
    d = d.split("/")[0].split("?")[0].strip()
    # Handle admin.shopify.com/store/<slug> URLs
    m = re.search(r"admin\.shopify\.com/store/([^/?]+)", str(domain).lower())
    if m:
        return f"{m.group(1)}.myshopify.com"
    # If no dot at all, assume it's just the store slug
    if "." not in d and d:
        return f"{d}.myshopify.com"
    return d


def _find_store_doc(shop_domain: str):
    """Internal helper — returns the raw Appwrite document regardless of
    the `uninstalled` flag. Callers that need the filtered version should
    use get_store() instead.

    Tries multiple domain candidates so that 'mystore', 'mystore.myshopify.com',
    or 'https://mystore.myshopify.com' all resolve to the same record.
    """
    if not DATABASE_ID or not shop_domain:
        return None
    canonical = _normalize_domain(shop_domain)
    raw = str(shop_domain).strip().lower()
    # Build a deduplicated list of candidates to try
    candidates: list[str] = []
    for c in [canonical, raw, str(shop_domain).strip()]:
        if c and c not in candidates:
            candidates.append(c)
    for cand in candidates:
        try:
            result = databases.list_documents(
                DATABASE_ID, STORES_COLLECTION,
                queries=[Query.equal("shop_domain", cand)],
            )
            docs = _normalize_list(result)["documents"]
            if docs:
                return docs[0]
        except Exception as e:
            print(f"repository_appwrite: _find_store_doc({cand!r}) failed: {e!r}")
    return None


def get_store(shop_domain: str) -> dict | None:
    """Returns the raw store document (not just the token), for callers
    that need more than the access token — e.g. checking is_new_store."""
    try:
        doc = _find_store_doc(shop_domain)
        if doc and doc.get("uninstalled"):
            return None
        return doc
    except Exception as e:
        print(f"repository_appwrite: get_store({shop_domain!r}) failed: {e!r}")
        return None


def get_store_by_id(store_id: str) -> dict | None:
    if not DATABASE_ID or not store_id:
        return None
    try:
        return databases.get_document(DATABASE_ID, STORES_COLLECTION, store_id)
    except Exception:
        return None


def upsert_shop(shop_domain: str, access_token: str, scopes: str, plan: str = "free") -> dict:
    if not DATABASE_ID:
        raise RuntimeError(
            "upsert_shop: APPWRITE_DATABASE_ID is not set. "
            "The store record for %r CANNOT be saved. "
            "Set the env var and restart the server." % shop_domain
        )
    # Always store the canonical *.myshopify.com domain so lookups are consistent
    canonical_domain = _normalize_domain(shop_domain) or shop_domain

    # Appwrite string attribute 'scopes' has a max length limit (typically 1000 chars).
    # When Shopify grants many permissions (especially on dev stores), it easily exceeds 1000 chars.
    safe_scopes = str(scopes or "").strip()
    if len(safe_scopes) > 950:
        safe_scopes = safe_scopes[:950]
        if "," in safe_scopes:
            safe_scopes = safe_scopes.rsplit(",", 1)[0]

    existing = _find_store_doc(canonical_domain)
    full_data = {
        "access_token": access_token,
        "scopes": safe_scopes,
        "uninstalled": False,
        "setup_completed": False if not existing else existing.get("setup_completed", False),
        "plan": plan or (existing.get("plan") if existing else "free") or "free",
    }
    # Minimal data for fallback if the collection is missing optional attributes
    minimal_data = {
        "access_token": access_token,
        "scopes": safe_scopes,
        "uninstalled": False,
    }

    def _try_write(data: dict) -> dict:
        if existing:
            doc = databases.update_document(
                DATABASE_ID, STORES_COLLECTION, existing["$id"],
                data=data,
            )
            print(f"[upsert_shop] Updated existing store record for {canonical_domain!r} (id={existing['$id']!r})")
            return doc
        d = {**data, "shop_domain": canonical_domain}
        doc = databases.create_document(
            DATABASE_ID, STORES_COLLECTION, ID.unique(),
            data=d,
        )
        print(f"[upsert_shop] Created new store record for {canonical_domain!r} (id={doc.get('$id')!r})")
        return doc

    try:
        return _try_write(full_data)
    except Exception as e:
        err_msg = str(e).lower()
        # Fallback 1: If 'scopes' attribute length or formatting error occurred, try with truncated or empty scopes
        if "scopes" in err_msg:
            print(f"[upsert_shop] Scopes attribute error ({e!r}); retrying with shortened scopes")
            short_data = dict(full_data)
            short_data["scopes"] = (short_data.get("scopes") or "")[:250]
            try:
                return _try_write(short_data)
            except Exception:
                short_data["scopes"] = ""
                return _try_write(short_data)

        # Fallback 2: If the write failed because the collection doesn't have the new optional
        # attributes (plan / setup_completed) yet, fall back to the minimal payload.
        if "plan" in err_msg or "setup_completed" in err_msg or "unknown attribute" in err_msg or "attribute not found" in err_msg or "invalid attribute" in err_msg:
            print(f"[upsert_shop] Full write failed ({e!r}); retrying with minimal data (run init_new_appwrite_db.py to add missing attributes)")
            try:
                return _try_write(minimal_data)
            except Exception as e2:
                print(f"[upsert_shop] Minimal write also failed for {canonical_domain!r}: {e2!r}")
                raise
        raise


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

def _scrub_card_data(entities: dict) -> dict:
    """Return a copy of entities with card_number and cvv redacted."""
    if not entities:
        return {}
    safe = dict(entities)
    if safe.get("card_number"):
        raw = re.sub(r"\D", "", str(safe["card_number"]))
        safe["card_number"] = f"****-****-****-{raw[-4:]}" if len(raw) >= 4 else "****"
    if safe.get("cvv"):
        safe["cvv"] = "***"
    return safe

_PAN_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

def log_request(shop_domain: str, message: str, status: str, detected_intent=None, detected_action=None, reply: str = "", entities: dict | None = None) -> None:
    store = _find_store_doc(shop_domain)
    # Scrub any card numbers that appear in the raw message text.
    safe_message = _PAN_RE.sub("[card-redacted]", message) if message else message
    safe_entities = _scrub_card_data(entities or {})
    databases.create_document(
        DATABASE_ID, REQUEST_LOGS_COLLECTION, ID.unique(),
        data={
            "message": safe_message,
            "status": status,
            "detected_intent": detected_intent,
            "detected_action": detected_action,
            "reply": reply,
            "entities": json.dumps(safe_entities, ensure_ascii=False),
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
    try:
        result = databases.list_documents(
            DATABASE_ID, DASHBOARD_USERS_COLLECTION,
            queries=[Query.equal("store", store_id)],
        )
        return len(result.get("documents", [])) > 0
    except Exception:
        return False


def get_dashboard_user_by_email(email: str) -> dict | None:
    try:
        normalized = (email or "").strip().lower()
        if not normalized:
            return None
        result = databases.list_documents(
            DATABASE_ID, DASHBOARD_USERS_COLLECTION,
            queries=[Query.equal("email", normalized)],
        )
        docs = result.get("documents", [])
        if docs:
            return docs[0]
        # Fallback with raw casing
        raw = (email or "").strip()
        if raw != normalized:
            res_raw = databases.list_documents(
                DATABASE_ID, DASHBOARD_USERS_COLLECTION,
                queries=[Query.equal("email", raw)],
            )
            if res_raw.get("documents"):
                return res_raw["documents"][0]
        return None
    except Exception as e:
        print(f"[get_dashboard_user_by_email] Error: {e}")
        return None


def get_dashboard_user_by_id(user_id: str) -> dict | None:
    try:
        return databases.get_document(DATABASE_ID, DASHBOARD_USERS_COLLECTION, user_id)
    except Exception:
        return None


def create_dashboard_user(store_id: str, email: str, password_hash: str) -> dict:
    normalized = (email or "").strip().lower()
    return databases.create_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, ID.unique(),
        data={"store": store_id, "email": normalized, "password_hash": password_hash},
    )


# --- Agent customization (name, welcome/title, instructions, status, icon, theme, position) ---

_CUSTOMIZATION_CACHE: dict[str, dict] = {}


def get_customization(store_id: str) -> dict | None:
    if store_id in _CUSTOMIZATION_CACHE:
        return _CUSTOMIZATION_CACHE[store_id]
    try:
        result = databases.list_documents(
            DATABASE_ID, CUSTOMIZATIONS_COLLECTION,
            queries=[Query.equal("store", store_id)],
        )
        docs = result["documents"] if result.get("documents") else []
        if docs:
            doc = docs[0]
            # Normalize icon url attribute so callers always get custom_icon_url
            if not doc.get("custom_icon_url") and doc.get("icon_url"):
                doc["custom_icon_url"] = doc.get("icon_url")
            _CUSTOMIZATION_CACHE[store_id] = doc
            return doc
        return None
    except Exception:
        return _CUSTOMIZATION_CACHE.get(store_id)


def ensure_customization(store_id: str) -> dict:
    """Creates a default customization row if one doesn't exist yet —
    called right after install, same as your old code did in the same
    db.commit() as creating the Store row."""
    default_cust = {
        "$id": f"cust_{store_id}",
        "store": store_id,
        "agent_name": "AI Assistant",
        "agent_title": "How can I help you today?",
        "theme_color": "#2b2b2b",
        "icon_type": "preset",
        "custom_icon_url": "",
        "icon_url": "",
        "instructions": (
            "You are a helpful AI shopping assistant for this store. "
            "Be friendly, helpful and concise. Always try to provide "
            "accurate information about products, orders, shipping, "
            "returns and store policies."
        ),
        "status": "active",
        "widget_position": "bottom-right",
    }
    existing = get_customization(store_id)
    if existing:
        if not existing.get("custom_icon_url") and existing.get("icon_url"):
            existing["custom_icon_url"] = existing.get("icon_url")
        _CUSTOMIZATION_CACHE[store_id] = existing
        return existing
    try:
        doc = databases.create_document(
            DATABASE_ID, CUSTOMIZATIONS_COLLECTION, ID.unique(),
            data={k: v for k, v in default_cust.items() if k != "$id"},
        )
        _CUSTOMIZATION_CACHE[store_id] = doc
        return doc
    except Exception:
        _CUSTOMIZATION_CACHE[store_id] = default_cust
        return default_cust


def update_customization(store_id: str, **fields) -> dict:
    existing = ensure_customization(store_id)

    # Synchronize icon_url and custom_icon_url so whichever exists in Appwrite schema is set
    if "custom_icon_url" in fields and "icon_url" not in fields:
        fields["icon_url"] = fields["custom_icon_url"]
    elif "icon_url" in fields and "custom_icon_url" not in fields:
        fields["custom_icon_url"] = fields["icon_url"]

    try:
        doc = databases.update_document(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, existing["$id"], data=fields)
        _CUSTOMIZATION_CACHE[store_id] = doc
        return doc
    except Exception as e:
        err_msg = str(e).lower()
        # If one attribute doesn't exist in collection schema, gracefully retry with the other
        try:
            if "custom_icon_url" in err_msg:
                fallback = {k: v for k, v in fields.items() if k != "custom_icon_url"}
                doc = databases.update_document(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, existing["$id"], data=fallback)
                _CUSTOMIZATION_CACHE[store_id] = doc
                return doc
            elif "icon_url" in err_msg:
                fallback = {k: v for k, v in fields.items() if k != "icon_url"}
                doc = databases.update_document(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, existing["$id"], data=fallback)
                _CUSTOMIZATION_CACHE[store_id] = doc
                return doc
        except Exception:
            pass
        existing.update(fields)
        _CUSTOMIZATION_CACHE[store_id] = existing
        return existing


# --- Widget icon storage (Appwrite Storage bucket + Local Static fallback) ---

def _storage_file_url(file_id: str, bucket_id: str | None = None) -> str:
    b_id = bucket_id or WIDGET_ICONS_BUCKET
    return f"{APPWRITE_ENDPOINT}/storage/buckets/{b_id}/files/{file_id}/view?project={APPWRITE_PROJECT_ID}"


def _ensure_widget_icons_bucket() -> str:
    """Ensures a storage bucket exists for widget icons and returns its bucket_id.
    Discovers existing buckets in Appwrite project or creates WIDGET_ICONS_BUCKET.
    Also ensures public read permissions (Role.any) on the bucket."""
    from appwrite.permission import Permission
    from appwrite.role import Role

    bucket_id = None
    # 1. Check if configured WIDGET_ICONS_BUCKET exists
    try:
        storage.get_bucket(WIDGET_ICONS_BUCKET)
        bucket_id = WIDGET_ICONS_BUCKET
    except Exception as e:
        if "policy" in str(e).lower():
            raise AppwriteException(
                f"Appwrite Storage blocked by policy: Your API Key does not have 'buckets.read' / 'buckets.write' scope on {APPWRITE_ENDPOINT}. "
                "In Appwrite Console, go to Overview -> API Keys -> Select your key -> check 'files.read', 'files.write', 'buckets.read', 'buckets.write'."
            ) from e

    # 2. Try to list buckets to see if user created one with a different ID or name
    if not bucket_id:
        try:
            res = storage.list_buckets()
            buckets = res.get("buckets", []) if isinstance(res, dict) else getattr(res, "buckets", [])
            for b in buckets:
                bid = b.get("$id") if isinstance(b, dict) else getattr(b, "id", None)
                bname = (b.get("name") or "").lower() if isinstance(b, dict) else getattr(b, "name", "").lower()
                if bid == WIDGET_ICONS_BUCKET or "icon" in bname or "widget" in bname:
                    bucket_id = bid
                    break
            if not bucket_id and buckets:
                bid = buckets[0].get("$id") if isinstance(buckets[0], dict) else getattr(buckets[0], "id", None)
                if bid:
                    bucket_id = bid
        except Exception as e:
            if "policy" in str(e).lower():
                raise AppwriteException(
                    f"Appwrite Storage blocked by policy: Your API Key does not have 'buckets.read' scope on {APPWRITE_ENDPOINT}."
                ) from e

    # 3. Create bucket if none exists
    if not bucket_id:
        try:
            storage.create_bucket(
                bucket_id=WIDGET_ICONS_BUCKET,
                name="Widget Icons",
                permissions=[Permission.read(Role.any())],
                file_security=False,
                enabled=True,
            )
            bucket_id = WIDGET_ICONS_BUCKET
        except Exception as e:
            print(f"repository_appwrite: auto-create bucket notice: {e!r}")
            if "policy" in str(e).lower():
                raise AppwriteException(
                    "Appwrite Storage blocked by policy: Your Appwrite API key lacks 'buckets.write' scope. "
                    "Enable 'files.*' and 'buckets.*' in Appwrite Console -> API Keys."
                ) from e
            bucket_id = WIDGET_ICONS_BUCKET

    # 4. Guarantee bucket has public read permissions enabled
    try:
        storage.update_bucket(
            bucket_id=bucket_id,
            name="Widget Icons",
            permissions=[Permission.read(Role.any())],
            file_security=False,
            enabled=True,
        )
        print(f"[_ensure_widget_icons_bucket] ✅ Enabled public read (Role.any) on bucket '{bucket_id}'")
    except Exception as up_err:
        print(f"[_ensure_widget_icons_bucket] Bucket permission update notice: {up_err!r}")

    return bucket_id


def upload_icon_file(store_id: str, filename: str, content: bytes) -> str:
    """Uploads a custom widget icon to the Appwrite Storage bucket and
    returns its public view URL, ready to save as custom_icon_url.
    If Appwrite Storage is unavailable or restricted by policy, safely falls
    back to local static file serving or data URI so uploads NEVER fail."""
    if not content:
        raise ValueError("Cannot upload empty icon file.")

    import os, time, re, glob
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        ext = ".png"
    # Ensure safe, clean filename for Appwrite Storage (no spaces, special chars or unicode)
    clean_filename = f"icon_{store_id}_{int(time.time())}{ext}"

    # Purge old local cache files for this store to prevent stale caching
    static_uploads = os.path.join(os.path.dirname(__file__), "static", "uploads")
    os.makedirs(static_uploads, exist_ok=True)
    for old_f in glob.glob(os.path.join(static_uploads, f"icon_{store_id}*")):
        try:
            os.remove(old_f)
        except Exception:
            pass

    # 1. Upload to Appwrite Storage bucket
    try:
        bucket_id = _ensure_widget_icons_bucket()
        file_id = ID.unique()
        from appwrite.permission import Permission
        from appwrite.role import Role
        perms = [Permission.read(Role.any())]

        try:
            # First attempt: with explicit public read permissions
            storage.create_file(
                bucket_id=bucket_id,
                file_id=file_id,
                file=InputFile.from_bytes(content, filename=clean_filename),
                permissions=perms,
            )
            cdn_url = _storage_file_url(file_id, bucket_id)
            print(f"[upload_icon_file] ✅ Uploaded to Appwrite Storage bucket '{bucket_id}' (file_id={file_id})")
        except Exception as upload_err:
            # Second attempt: without perms (inherits bucket public read when file_security=False)
            storage.create_file(
                bucket_id=bucket_id,
                file_id=file_id,
                file=InputFile.from_bytes(content, filename=clean_filename),
            )
            cdn_url = _storage_file_url(file_id, bucket_id)
            print(f"[upload_icon_file] ✅ Uploaded to Appwrite Storage (file_id={file_id})")

        # Cache locally on disk with matching file_id for instant local responses
        try:
            with open(os.path.join(static_uploads, f"icon_{store_id}_{file_id}{ext}"), "wb") as f:
                f.write(content)
            with open(os.path.join(static_uploads, f"icon_{store_id}{ext}"), "wb") as f:
                f.write(content)
        except Exception:
            pass
        return cdn_url
    except Exception as storage_err:
        print(f"[upload_icon_file] ⚠️ Appwrite Storage unavailable ({storage_err!r}); using resilient fallback.")

    # 2. Resilient local disk fallback
    import shopify_auth
    out_path = os.path.join(static_uploads, f"icon_{store_id}{ext}")
    try:
        with open(out_path, "wb") as f:
            f.write(content)
        app_url = (shopify_auth.APP_URL or "http://localhost:8000").rstrip("/")
        ts = int(time.time())
        return f"{app_url}/static/uploads/icon_{store_id}{ext}?t={ts}"
    except Exception as io_err:
        print(f"[upload_icon_file] Local static file write notice: {io_err!r}")

    # 3. Base64 fallback if file system is completely read-only
    import base64
    mime = "image/png" if ext == ".png" else ("image/jpeg" if ext in (".jpg", ".jpeg") else "image/svg+xml")
    b64 = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{b64}"


def delete_icon_file(file_url: str) -> None:
    """Best-effort delete of a previously uploaded icon."""
    if not file_url:
        return
    if "/storage/buckets/" in file_url and "/files/" in file_url:
        try:
            parts = file_url.split("/storage/buckets/")[1]
            bucket_id = parts.split("/files/")[0]
            file_id = parts.split("/files/")[1].split("/")[0].split("?")[0]
            storage.delete_file(bucket_id, file_id)
            print(f"[delete_icon_file] ✅ Deleted previous icon file '{file_id}' from bucket '{bucket_id}'")
        except Exception as e:
            print(f"[delete_icon_file] Notice: could not delete old storage file: {e!r}")
    try:
        import os, glob
        clean_url = file_url.split("?")[0]
        fname = clean_url.split("/static/uploads/")[-1] if "/static/uploads/" in clean_url else ""
        static_uploads = os.path.join(os.path.dirname(__file__), "static", "uploads")
        if fname:
            p = os.path.join(static_uploads, fname)
            if os.path.exists(p):
                os.remove(p)
    except Exception:
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
    try:
        result = databases.list_documents(
            DATABASE_ID, FEATURES_COLLECTION,
            queries=[Query.equal("store", store_id)],
        )
        return result["documents"][0] if result.get("documents") else None
    except Exception:
        return None


def ensure_features(store_id: str) -> dict:
    existing = get_features(store_id)
    if existing:
        return existing
    try:
        return databases.create_document(
            DATABASE_ID, FEATURES_COLLECTION, ID.unique(),
            data={"store": store_id, **_DEFAULT_FEATURES},
        )
    except Exception:
        return {"$id": f"feat_{store_id}", "store": store_id, **_DEFAULT_FEATURES}


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

_STORE_INFO_CACHE: dict[str, dict] = {}


def get_store_info(store_id: str) -> dict | None:
    if store_id in _STORE_INFO_CACHE:
        return _STORE_INFO_CACHE[store_id]
    try:
        result = databases.list_documents(
            DATABASE_ID, STORE_INFO_COLLECTION,
            queries=[Query.equal("store", store_id)],
        )
        if result.get("documents"):
            _STORE_INFO_CACHE[store_id] = result["documents"][0]
            return result["documents"][0]
        return None
    except Exception:
        return _STORE_INFO_CACHE.get(store_id)


def ensure_store_info(store_id: str, business_name: str = "", support_email: str = "", timezone: str = "UTC") -> dict:
    existing = get_store_info(store_id)
    if existing:
        updates = {}
        if business_name and not existing.get("business_name"):
            updates["business_name"] = business_name
        if support_email and not existing.get("support_email"):
            updates["support_email"] = support_email
        if timezone and (not existing.get("timezone") or existing.get("timezone") == "UTC"):
            updates["timezone"] = timezone
        if updates:
            try:
                doc = databases.update_document(DATABASE_ID, STORE_INFO_COLLECTION, existing["$id"], data=updates)
                _STORE_INFO_CACHE[store_id] = doc
                return doc
            except Exception as e:
                print(f"[ensure_store_info] update failed: {e!r}")
                existing.update(updates)
                _STORE_INFO_CACHE[store_id] = existing
        return existing
    try:
        doc = databases.create_document(
            DATABASE_ID, STORE_INFO_COLLECTION, ID.unique(),
            data={
                "store": store_id,
                "business_name": business_name or "",
                "support_email": support_email or "",
                "timezone": timezone or "UTC",
            },
        )
        _STORE_INFO_CACHE[store_id] = doc
        return doc
    except Exception:
        fallback = {
            "$id": f"info_{store_id}",
            "store": store_id,
            "business_name": business_name or "",
            "support_email": support_email or "",
            "timezone": timezone or "UTC",
        }
        _STORE_INFO_CACHE[store_id] = fallback
        return fallback


def update_store_info(store_id: str, **fields) -> dict:
    existing = ensure_store_info(store_id)
    try:
        doc = databases.update_document(DATABASE_ID, STORE_INFO_COLLECTION, existing["$id"], data=fields)
        _STORE_INFO_CACHE[store_id] = doc
        return doc
    except Exception:
        existing.update(fields)
        _STORE_INFO_CACHE[store_id] = existing
        return existing


# --- Knowledge base / FAQs ---

def list_faqs(store_id: str) -> list[dict]:
    try:
        result = databases.list_documents(
            DATABASE_ID, FAQS_COLLECTION,
            queries=[Query.equal("store", store_id), Query.order_asc("$createdAt")],
        )
        return result.get("documents", [])
    except Exception:
        return []


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


def create_password_reset_token_for_user(ident: str) -> tuple[str | None, str | None]:
    """Returns (token, user_email) if ident matches a real dashboard user
    (by email or linked store domain), or (None, None) if not found.
    Callers should show the same message either way — never reveal whether
    an email is registered, that's a security risk."""
    cleaned = (ident or "").strip().lower()
    if not cleaned:
        return None, None

    user = get_dashboard_user_by_email(cleaned)
    if not user:
        # Fallback to store domain lookup
        try:
            store = get_store(cleaned) or get_store_by_id(ident.strip())
            if store:
                res = databases.list_documents(
                    DATABASE_ID, DASHBOARD_USERS_COLLECTION,
                    queries=[Query.equal("store", store["$id"])],
                )
                if res.get("documents"):
                    user = res["documents"][0]
        except Exception:
            pass

    if not user:
        return None, None

    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    try:
        databases.update_document(
            DATABASE_ID, DASHBOARD_USERS_COLLECTION, user["$id"],
            data={"reset_token": token, "reset_token_expires": expires},
        )
        return token, user.get("email") or cleaned
    except Exception as e:
        print(f"[create_password_reset_token] Appwrite update failed: {e}")
        return None, None


def create_password_reset_token(email: str) -> str | None:
    token, _ = create_password_reset_token_for_user(email)
    return token


def get_dashboard_user_by_reset_token(token: str) -> dict | None:
    """Returns the user if the token is valid AND not expired. A token
    older than 1 hour is treated as if it doesn't exist at all."""
    if not token or not str(token).strip():
        return None
    try:
        result = databases.list_documents(
            DATABASE_ID, DASHBOARD_USERS_COLLECTION,
            queries=[Query.equal("reset_token", str(token).strip())],
        )
    except Exception as e:
        print(f"[get_dashboard_user_by_reset_token] Appwrite list failed: {e}")
        return None

    docs = result.get("documents", [])
    if not docs:
        return None
    user = docs[0]
    expires = user.get("reset_token_expires")
    if not expires:
        return None
    try:
        exp_dt = datetime.fromisoformat(expires)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if exp_dt < datetime.now(timezone.utc):
            print(f"[get_dashboard_user_by_reset_token] Token expired ({expires})")
            return None  # expired
    except (ValueError, TypeError) as e:
        print(f"[get_dashboard_user_by_reset_token] Failed to parse expiration {expires!r}: {e}")
        return None
    return user


def reset_password(user_id: str, new_password_hash: str) -> None:
    """Sets the new password AND invalidates the reset token — a token
    must only ever be usable once."""
    try:
        databases.update_document(
            DATABASE_ID, DASHBOARD_USERS_COLLECTION, user_id,
            data={"password_hash": new_password_hash, "reset_token": "", "reset_token_expires": ""},
        )
    except Exception as e:
        print(f"[reset_password] Appwrite update failed: {e}")
        raise


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


# ==========================================================================
# RESTOCK & PRICE-DROP ALERTS (Autonomous Shopper Watchers)
# ==========================================================================
_RESTOCK_ALERTS: list[dict] = []

def save_restock_alert(
    store_id: str,
    shop_domain: str,
    customer_email: str,
    product_id: str,
    product_title: str,
    variant_id: str = "",
    size: str = "",
    target_price: str = "",
    alert_type: str = "restock",
) -> dict:
    """Stores a back-in-stock or price-drop subscription for a shopper."""
    clean_email = (customer_email or "").strip().lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    alert_id = f"alert_{int(time.time()*1000)}" if 'time' in globals() else f"alert_{len(_RESTOCK_ALERTS)+1}"

    # Deduplicate: if same email already subscribed for this product & variant, update it
    for existing in _RESTOCK_ALERTS:
        if (
            existing.get("store_id") == str(store_id)
            and existing.get("customer_email") == clean_email
            and str(existing.get("product_id")) == str(product_id)
            and (not variant_id or str(existing.get("variant_id")) == str(variant_id))
            and existing.get("status") == "active"
        ):
            existing["product_title"] = product_title
            existing["size"] = size or existing.get("size", "")
            existing["target_price"] = target_price or existing.get("target_price", "")
            existing["updated_at"] = now_iso
            return existing

    record = {
        "$id": alert_id,
        "id": alert_id,
        "store_id": str(store_id),
        "shop_domain": str(shop_domain),
        "customer_email": clean_email,
        "product_id": str(product_id),
        "product_title": str(product_title),
        "variant_id": str(variant_id or ""),
        "size": str(size or ""),
        "target_price": str(target_price or ""),
        "alert_type": alert_type,
        "status": "active",
        "created_at": now_iso,
    }
    _RESTOCK_ALERTS.append(record)
    return record


def list_restock_alerts(store_id: str, customer_email: str | None = None, status: str = "active") -> list[dict]:
    """Retrieves restock alerts filtered by store and optionally customer email or status."""
    clean_email = (customer_email or "").strip().lower() if customer_email else None
    results = []
    for a in _RESTOCK_ALERTS:
        if store_id and a.get("store_id") != str(store_id):
            continue
        if clean_email and a.get("customer_email") != clean_email:
            continue
        if status and a.get("status") != status:
            continue
        results.append(dict(a))
    return results


def mark_restock_alert_notified(alert_id: str) -> bool:
    """Marks an alert as notified so duplicate emails are never sent."""
    for a in _RESTOCK_ALERTS:
        if a.get("$id") == str(alert_id) or a.get("id") == str(alert_id):
            a["status"] = "notified"
            a["notified_at"] = datetime.now(timezone.utc).isoformat()
            return True
    return False


# ==========================================================================
# STORE OWNER EMAIL ROUTING (Reply-To from database)
# ==========================================================================
def get_store_owner_email(store_id_or_shop: str) -> str:
    """Retrieves the merchant's email address from Appwrite database
    so shopper replies route directly to the store owner."""
    try:
        store = _find_store_doc(store_id_or_shop)
        store_id = (store.get("$id") if isinstance(store, dict) else getattr(store, "$id", None)) or str(store_id_or_shop)
        if not store_id:
            return "support@dripire.com"

        # 1. Try store_info support_email
        info = get_store_info(store_id) or get_store_info(str(store_id_or_shop))
        if info and info.get("support_email") and "@" in str(info.get("support_email")):
            return str(info["support_email"]).strip()

        # 2. Try dashboard_users owner email
        if databases:
            res = databases.list_documents(
                DATABASE_ID,
                DASHBOARD_USERS_COLLECTION,
                queries=[Query.equal("store", store_id), Query.limit(1)],
            )
            docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
            if docs:
                mail = docs[0].get("email") if isinstance(docs[0], dict) else getattr(docs[0], "email", None)
                if mail and "@" in str(mail):
                    return str(mail).strip()
    except Exception as e:
        print(f"[repository_appwrite.get_store_owner_email] error: {e!r}")

    return "support@dripire.com"


# ==========================================================================
# STANDING INSTRUCTIONS & SCHEDULED TASKS
# ==========================================================================
_STANDING_INSTRUCTIONS: list[dict] = []

def save_standing_instruction(
    store_id: str,
    shop_domain: str,
    customer_email: str,
    instruction_type: str,  # 'price_drop', 'reorder_reminder', 'auto_add_restock'
    product_id: str = "",
    product_title: str = "",
    target_price: str = "",
    trigger_at: int = 0,
    interval_days: int = 0,
    auto_add: bool = False,
    payload: dict | None = None,
) -> dict:
    """Stores standing instructions (e.g. price drops, reorders, auto-cart restock)."""
    clean_email = (customer_email or "").strip().lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    now_ts = int(time.time())
    inst_id = f"inst_{int(now_ts*1000)}"

    record = {
        "$id": inst_id,
        "id": inst_id,
        "store_id": str(store_id),
        "shop_domain": str(shop_domain),
        "customer_email": clean_email,
        "instruction_type": instruction_type,
        "product_id": str(product_id or ""),
        "product_title": str(product_title or "Product"),
        "target_price": str(target_price or ""),
        "trigger_at": int(trigger_at or (now_ts + interval_days * 86400 if interval_days else now_ts)),
        "interval_days": int(interval_days or 0),
        "auto_add": bool(auto_add),
        "payload": payload or {},
        "status": "active",
        "created_at": now_iso,
    }
    _STANDING_INSTRUCTIONS.append(record)
    return record


def list_standing_instructions(
    store_id: str | None = None,
    customer_email: str | None = None,
    instruction_type: str | None = None,
    status: str = "active",
) -> list[dict]:
    """Lists standing instructions matching filters."""
    clean_email = (customer_email or "").strip().lower() if customer_email else None
    results = []
    for inst in _STANDING_INSTRUCTIONS:
        if store_id and inst.get("store_id") != str(store_id) and inst.get("shop_domain") != str(store_id):
            continue
        if clean_email and inst.get("customer_email") != clean_email:
            continue
        if instruction_type and inst.get("instruction_type") != instruction_type:
            continue
        if status and inst.get("status") != status:
            continue
        results.append(dict(inst))
    return results


def mark_standing_instruction_triggered(instruction_id: str) -> bool:
    """Marks a standing instruction as triggered/completed."""
    for inst in _STANDING_INSTRUCTIONS:
        if inst.get("$id") == str(instruction_id) or inst.get("id") == str(instruction_id):
            inst["status"] = "triggered"
            inst["triggered_at"] = datetime.now(timezone.utc).isoformat()
            return True
    return False


# ==========================================================================
# ABANDONED CART RECOVERY (In-widget nudges & email recovery)
# ==========================================================================
_ABANDONED_CHECKOUTS: list[dict] = []

def save_abandoned_checkout(
    store_id: str,
    shop_domain: str,
    customer_email: str,
    customer_name: str = "",
    cart_items: list | None = None,
    total_price: str = "",
    checkout_url: str = "",
) -> dict:
    """Saves or updates an abandoned checkout session."""
    clean_email = (customer_email or "").strip().lower()
    now_ts = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat()
    cid = f"chk_{int(now_ts*1000)}"

    for existing in _ABANDONED_CHECKOUTS:
        if (
            existing.get("shop_domain") == str(shop_domain)
            and existing.get("customer_email") == clean_email
            and existing.get("status") == "pending"
        ):
            existing["cart_items"] = cart_items or existing.get("cart_items", [])
            existing["total_price"] = total_price or existing.get("total_price", "")
            existing["checkout_url"] = checkout_url or existing.get("checkout_url", "")
            existing["updated_at"] = now_iso
            return existing

    record = {
        "$id": cid,
        "id": cid,
        "store_id": str(store_id),
        "shop_domain": str(shop_domain),
        "customer_email": clean_email,
        "customer_name": str(customer_name or ""),
        "cart_items": cart_items or [],
        "total_price": str(total_price or ""),
        "checkout_url": str(checkout_url or ""),
        "abandoned_at": now_ts,
        "created_at": now_iso,
        "status": "pending",
    }
    _ABANDONED_CHECKOUTS.append(record)
    return record


def list_pending_abandoned_checkouts(shop_domain: str | None = None, min_age_seconds: int = 1800) -> list[dict]:
    """Retrieves checkouts abandoned for more than min_age_seconds and not yet notified."""
    now_ts = int(time.time())
    results = []
    for c in _ABANDONED_CHECKOUTS:
        if shop_domain and c.get("shop_domain") != str(shop_domain):
            continue
        if c.get("status") != "pending":
            continue
        if (now_ts - int(c.get("abandoned_at") or 0)) >= min_age_seconds:
            results.append(dict(c))
    return results


def mark_abandoned_checkout_notified(checkout_id: str) -> bool:
    for c in _ABANDONED_CHECKOUTS:
        if c.get("$id") == str(checkout_id) or c.get("id") == str(checkout_id):
            c["status"] = "notified"
            c["notified_at"] = datetime.now(timezone.utc).isoformat()
            return True
    return False


def mark_abandoned_checkout_recovered(shop_domain: str, customer_email: str) -> bool:
    clean_email = (customer_email or "").strip().lower()
    recovered = False
    for c in _ABANDONED_CHECKOUTS:
        if c.get("shop_domain") == str(shop_domain) and c.get("customer_email") == clean_email:
            c["status"] = "recovered"
            c["recovered_at"] = datetime.now(timezone.utc).isoformat()
            recovered = True
    return recovered


# ==========================================================================
# ORDER EDITS & CANCELLATION SETTINGS
# ==========================================================================
_ORDER_ACTION_REQUESTS: list[dict] = []
_CANCELLATION_MODES: dict[str, str] = {}

def get_order_cancellation_mode(store_id: str) -> str:
    """Returns 'auto', 'ask', or 'off'. Default is 'auto'."""
    if store_id in _CANCELLATION_MODES:
        return _CANCELLATION_MODES[store_id]
    try:
        cfg = ensure_customization(store_id)
        if cfg and cfg.get("order_cancellation_mode"):
            val = str(cfg["order_cancellation_mode"]).strip().lower()
            if val in {"auto", "ask", "off"}:
                _CANCELLATION_MODES[store_id] = val
                return val
    except Exception:
        pass
    return "auto"


def set_order_cancellation_mode(store_id: str, mode: str) -> bool:
    mode_clean = str(mode or "auto").strip().lower()
    if mode_clean not in {"auto", "ask", "off"}:
        mode_clean = "auto"
    _CANCELLATION_MODES[store_id] = mode_clean
    try:
        update_customization(store_id, order_cancellation_mode=mode_clean)
        return True
    except Exception as e:
        print(f"[set_order_cancellation_mode] failed: {e!r}")
        return True


def save_order_action_request(
    store_id: str,
    shop_domain: str,
    order_number: str,
    customer_email: str,
    action_type: str = "cancel",  # 'cancel' or 'edit_address'
    details: str = "",
    status: str = "pending",
) -> dict:
    """Stores an order edit or cancellation request for merchant approval or audit trail."""
    req_id = f"ordreq_{int(time.time()*1000)}"
    record = {
        "$id": req_id,
        "id": req_id,
        "store_id": str(store_id),
        "shop_domain": str(shop_domain),
        "order_number": str(order_number),
        "customer_email": str(customer_email).strip().lower(),
        "action_type": action_type,
        "details": details,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _ORDER_ACTION_REQUESTS.append(record)
    return record


