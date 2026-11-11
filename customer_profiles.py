"""
customer_profiles.py
---------------------
Everything needed to recognise shoppers safely and store comprehensive customer profiles:
  1. verify_app_proxy()   - proves a request really came from Shopify's App Proxy.
  2. make/read_session_token() - signed token handed to the widget so /chat calls are verified.
  3. ensure_customer_profiles_collection() - automatic schema check and migration.
  4. save_customer_profile() - stores complete personal details, order history, active orders,
                               total spent, tags, and preferences.
  5. append_chat_turn() - stores the chat conversation history done by customers, updates
                          last_message, total_chats count, and last_seen.
  6. get_profile() / list_customer_profiles() / delete_profile() - retrieval and GDPR erasure.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

import httpx

try:
    from appwrite_client import (
        databases,
        DATABASE_ID,
        APPWRITE_ENDPOINT,
        APPWRITE_PROJECT_ID,
        APPWRITE_API_KEY,
    )
    from appwrite.exception import AppwriteException
    from appwrite.query import Query
except ImportError:
    databases = None
    DATABASE_ID = os.environ.get("APPWRITE_DATABASE_ID", "")
    APPWRITE_ENDPOINT = os.environ.get("APPWRITE_ENDPOINT", "").rstrip("/")
    APPWRITE_PROJECT_ID = os.environ.get("APPWRITE_PROJECT_ID", "")
    APPWRITE_API_KEY = os.environ.get("APPWRITE_API_KEY", "")
    AppwriteException = Exception
    Query = None

SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or SHOPIFY_API_SECRET
SESSION_TTL_SECONDS = 2 * 60 * 60
PROXY_MAX_AGE_SECONDS = 10 * 60

CUSTOMERS_COLLECTION_ID = os.environ.get("APPWRITE_CUSTOMERS_COLLECTION_ID", "customer_profiles")

_JSON_FIELDS = (
    "purchased_product_ids",
    "top_types",
    "top_tags",
    "recent_searches",
    "favorite_colors",
    "recent_orders",
    "orders_data",
    "active_orders",
    "chat_history",
)
_INT_FIELDS = (
    "order_count",
    "orders_count",
    "synced_at",
    "last_seen",
    "budget_min",
    "budget_max",
    "total_chats",
)

_KNOWN_ATTRS_CACHE: set[str] | None = None
_KNOWN_ATTRS_TS: float = 0


# ---------------------------------------------------------------------
# 1. App Proxy signature check
# ---------------------------------------------------------------------
def verify_app_proxy(query_items: list[tuple[str, str]]) -> bool:
    if not SHOPIFY_API_SECRET:
        print("customer_profiles: SHOPIFY_API_SECRET not set - cannot verify app proxy requests")
        return False

    provided = None
    grouped: dict[str, list[str]] = {}
    for key, value in query_items:
        if key == "signature":
            provided = value
            continue
        grouped.setdefault(key, []).append(value)
    if not provided:
        return False

    message = "".join(f"{k}={','.join(grouped[k])}" for k in sorted(grouped))
    expected = hmac.new(SHOPIFY_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        return False

    try:
        age = abs(time.time() - int(grouped.get("timestamp", ["0"])[0]))
    except ValueError:
        return False
    return age <= PROXY_MAX_AGE_SECONDS


# ---------------------------------------------------------------------
# 2. Signed session tokens (widget -> /chat)
# ---------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session_token(shop: str, customer_id: str) -> str:
    payload = json.dumps(
        {"shop": shop, "cid": str(customer_id), "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read_session_token(token: str | None, shop: str) -> str | None:
    if not token or not SESSION_SECRET or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig_b64)):
            return None
        data = json.loads(payload)
    except Exception:
        return None
    if data.get("shop") != shop or int(data.get("exp", 0)) < time.time():
        return None
    return str(data.get("cid") or "") or None


# ---------------------------------------------------------------------
# 3. Schema Management & Automatic Migration
# ---------------------------------------------------------------------
def _configured() -> bool:
    return all([APPWRITE_ENDPOINT, APPWRITE_PROJECT_ID, APPWRITE_API_KEY, DATABASE_ID])


def _base_rest() -> str:
    return f"{APPWRITE_ENDPOINT.rstrip('/')}/databases/{DATABASE_ID}/collections/{CUSTOMERS_COLLECTION_ID}/documents"


def _hdr_rest() -> dict:
    return {
        "X-Appwrite-Project": APPWRITE_PROJECT_ID,
        "X-Appwrite-Key": APPWRITE_API_KEY,
        "Content-Type": "application/json",
    }


def ensure_customer_profiles_collection():
    """Ensures collection and all required attributes exist in Appwrite."""
    if not _configured() or not databases:
        return

    # 1. Collection check / create
    try:
        databases.get_collection(DATABASE_ID, CUSTOMERS_COLLECTION_ID)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or getattr(e, "code", None) == 404:
            try:
                databases.create_collection(
                    DATABASE_ID,
                    CUSTOMERS_COLLECTION_ID,
                    "Customer Profiles",
                )
                print(f"[customer_profiles] Created collection '{CUSTOMERS_COLLECTION_ID}'")
            except Exception as ce:
                print(f"[customer_profiles] Failed creating collection: {ce!r}")
        else:
            print(f"[customer_profiles] Notice checking collection: {e!r}")

    # 2. Desired attributes
    string_attrs = [
        ("shop", 255),
        ("store", 255),
        ("customer_id", 128),
        ("email", 255),
        ("first_name", 100),
        ("last_name", 100),
        ("name", 255),
        ("phone", 60),
        ("total_spent", 60),
        ("recent_orders", 65535),
        ("orders_data", 65535),
        ("active_orders", 10000),
        ("purchased_product_ids", 65535),
        ("top_types", 4096),
        ("top_tags", 4096),
        ("recent_searches", 4096),
        ("favorite_colors", 1024),
        ("chat_history", 65535),
        ("last_message", 1000),
        ("preferences", 4000),
        ("tags", 1000),
        ("currency", 20),
        ("addresses", 2000),
    ]

    integer_attrs = [
        ("order_count", 0),
        ("orders_count", 0),
        ("total_chats", 0),
        ("budget_min", 0),
        ("budget_max", 0),
        ("synced_at", 0),
        ("last_seen", 0),
    ]

    for key, size in string_attrs:
        try:
            databases.create_string_attribute(
                DATABASE_ID,
                CUSTOMERS_COLLECTION_ID,
                key,
                size,
                required=False,
            )
        except Exception as e:
            if getattr(e, "code", None) != 409 and "already exists" not in str(e).lower():
                pass

    for key, default_val in integer_attrs:
        try:
            databases.create_integer_attribute(
                DATABASE_ID,
                CUSTOMERS_COLLECTION_ID,
                key,
                required=False,
                default=default_val,
            )
        except Exception as e:
            if getattr(e, "code", None) != 409 and "already exists" not in str(e).lower():
                pass

    # 3. Create indexes
    try:
        from appwrite.enums.databases_index_type import DatabasesIndexType
        idx_type = DatabasesIndexType.KEY
    except Exception:
        idx_type = "key"

    for idx_name, cols in [
        ("idx_shop", ["shop"]),
        ("idx_customer_id", ["customer_id"]),
        ("idx_email", ["email"]),
    ]:
        try:
            databases.create_index(
                DATABASE_ID,
                CUSTOMERS_COLLECTION_ID,
                idx_name,
                idx_type,
                cols,
            )
        except Exception:
            pass


def _get_allowed_attrs() -> set[str] | None:
    global _KNOWN_ATTRS_CACHE, _KNOWN_ATTRS_TS
    now = time.time()
    if _KNOWN_ATTRS_CACHE and (now - _KNOWN_ATTRS_TS < 300):
        return _KNOWN_ATTRS_CACHE
    if not _configured() or not databases:
        return None
    try:
        coll = databases.get_collection(DATABASE_ID, CUSTOMERS_COLLECTION_ID)
        raw_attrs = coll.get("attributes", []) if isinstance(coll, dict) else getattr(coll, "attributes", [])
        avail = {
            (a.get("key") if isinstance(a, dict) else getattr(a, "key", None))
            for a in raw_attrs
            if (a.get("status") if isinstance(a, dict) else getattr(a, "status", None)) in ("available", None)
        }
        avail.discard(None)
        if avail:
            _KNOWN_ATTRS_CACHE = avail
            _KNOWN_ATTRS_TS = now
            return _KNOWN_ATTRS_CACHE
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------
# 4. Storage Helpers: Doc IDs, Encode & Decode
# ---------------------------------------------------------------------
def _doc_id(shop: str, customer_id: str | None = None, email: str | None = None) -> str:
    """Deterministic document ID (max 36 chars alphanumeric/dash/underscore)."""
    shop_clean = str(shop or "").strip().lower()
    cid_clean = str(customer_id or "").strip()
    if cid_clean:
        return hashlib.sha1(f"{shop_clean}:{cid_clean}".encode()).hexdigest()[:36]
    email_clean = str(email or "").strip().lower()
    if email_clean:
        return hashlib.sha1(f"{shop_clean}:email:{email_clean}".encode()).hexdigest()[:36]
    return hashlib.sha1(f"{shop_clean}:anon".encode()).hexdigest()[:36]


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if v is None:
            continue
        if k in _JSON_FIELDS:
            out[k] = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        elif k in _INT_FIELDS:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = 0
        else:
            out[k] = str(v)
    return out


def _decode(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if not k.startswith("$")}
    for k in _JSON_FIELDS:
        val = out.get(k)
        if isinstance(val, str):
            try:
                out[k] = json.loads(val)
            except Exception:
                out[k] = [] if k != "preferences" else {}
        elif val is None:
            out[k] = [] if k != "preferences" else {}
    for k in _INT_FIELDS:
        try:
            out[k] = int(out.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0

    # Unpack extra attributes if preserved in preferences JSON
    pref = out.get("preferences")
    if isinstance(pref, str) and pref.startswith("{"):
        try:
            pref_dict = json.loads(pref)
            if isinstance(pref_dict, dict):
                for pk, pv in pref_dict.items():
                    if pk not in out or not out[pk]:
                        out[pk] = pv
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------
# 5. Core Appwrite Read / Write / Query
# ---------------------------------------------------------------------
async def get_profile(shop: str, customer_id: str | None = None, email: str | None = None) -> dict | None:
    """Fetches customer profile by customer_id or email."""
    if not _configured():
        return None
    shop_clean = str(shop or "").strip().lower()
    cid_clean = str(customer_id or "").strip()
    email_clean = str(email or "").strip().lower()

    if not cid_clean and not email_clean:
        return None

    # 1. Try direct ID lookup
    target_id = _doc_id(shop_clean, cid_clean, email_clean)
    doc = None
    if databases:
        try:
            doc = databases.get_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, target_id)
        except Exception:
            doc = None

    if doc:
        return _decode(doc)

    # 2. Try REST direct lookup fallback
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(f"{_base_rest()}/{target_id}", headers=_hdr_rest())
            if resp.status_code == 200:
                return _decode(resp.json())
    except Exception:
        pass

    # 3. If queried with email or ID, search collection via Query
    if databases and Query:
        queries = [Query.equal("shop", shop_clean)]
        if cid_clean:
            queries.append(Query.equal("customer_id", cid_clean))
        elif email_clean:
            queries.append(Query.equal("email", email_clean))
        try:
            res = databases.list_documents(DATABASE_ID, CUSTOMERS_COLLECTION_ID, queries=queries + [Query.limit(1)])
            docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
            if docs:
                return _decode(docs[0])
        except Exception:
            pass

    return None


async def save_profile(shop: str, customer_id: str | None = None, fields: dict | None = None, email: str | None = None) -> bool:
    """Upserts customer profile, safely handling schema and attribute evolution."""
    if not _configured() or not fields:
        return False
    shop_clean = str(shop or "").strip().lower()
    cid_clean = str(customer_id or "").strip()
    email_clean = str(email or (fields or {}).get("email") or "").strip().lower()

    if not cid_clean and not email_clean:
        return False

    doc_id = _doc_id(shop_clean, cid_clean, email_clean)
    payload = {
        **fields,
        "shop": shop_clean,
        "store": shop_clean,
    }
    if cid_clean:
        payload["customer_id"] = cid_clean
    if email_clean:
        payload["email"] = email_clean

    data = _encode(payload)

    # Filter to known available attributes if discovered, pack the rest into 'preferences'
    allowed = _get_allowed_attrs()
    if allowed:
        safe_data = {}
        overflow = {}
        for k, v in data.items():
            if k in allowed:
                safe_data[k] = v
            else:
                overflow[k] = v
        if overflow:
            current_pref = safe_data.get("preferences") or "{}"
            try:
                p_dict = json.loads(current_pref) if isinstance(current_pref, str) else dict(current_pref)
            except Exception:
                p_dict = {}
            p_dict.update(overflow)
            if "preferences" in allowed:
                safe_data["preferences"] = json.dumps(p_dict, ensure_ascii=False)[:3990]
        data = safe_data

    # Attempt 1: via databases SDK
    if databases:
        try:
            # Check if document already exists
            existing = None
            try:
                existing = databases.get_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, doc_id)
            except Exception:
                existing = None

            if existing:
                databases.update_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, doc_id, data=data)
            else:
                databases.create_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, doc_id, data=data)
            return True
        except Exception as e:
            err_msg = str(e)
            # If an unknown attribute was rejected, strip it and retry
            m = re.findall(r'Attribute\s*["\']?([a-zA-Z0-9_]+)["\']?\s*not found', err_msg, re.IGNORECASE)
            if m:
                for bad_key in m:
                    data.pop(bad_key, None)
                try:
                    databases.create_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, doc_id, data=data)
                    return True
                except Exception:
                    try:
                        databases.update_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, doc_id, data=data)
                        return True
                    except Exception:
                        pass
            print(f"[customer_profiles] databases SDK save failed: {e!r}")

    # Attempt 2: REST fallback
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.patch(f"{_base_rest()}/{doc_id}", headers=_hdr_rest(), json={"data": data})
            if resp.status_code == 404:
                resp = await client.post(_base_rest(), headers=_hdr_rest(), json={"documentId": doc_id, "data": data})
            if resp.status_code in (200, 201):
                return True
            print(f"[customer_profiles] REST save -> {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[customer_profiles] save_profile REST failed: {e!r}")

    return False


async def save_customer_profile(
    shop: str,
    customer_id: str | None = None,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    name: str | None = None,
    phone: str | None = None,
    order_count: int | None = None,
    total_spent: float | str | None = None,
    recent_orders: list | None = None,
    active_orders: list | None = None,
    purchased_product_ids: list | None = None,
    top_types: list | None = None,
    top_tags: list | None = None,
    addresses: str | None = None,
    tags: str | list | None = None,
    currency: str | None = None,
    preferences: dict | str | None = None,
) -> bool:
    """Comprehensive helper: saves complete shopper profile including orders and personal info."""
    if not shop:
        return False

    existing = await get_profile(shop, customer_id=customer_id, email=email) or {}

    fields: dict[str, Any] = {
        "synced_at": int(time.time()),
        "last_seen": int(time.time()),
    }

    if customer_id:
        fields["customer_id"] = str(customer_id)
    if email:
        fields["email"] = str(email).strip().lower()
    if first_name:
        fields["first_name"] = str(first_name).strip()
    if last_name:
        fields["last_name"] = str(last_name).strip()
    if name:
        fields["name"] = str(name).strip()
    elif first_name or last_name:
        fields["name"] = f"{first_name or ''} {last_name or ''}".strip()
    if phone:
        fields["phone"] = str(phone).strip()
    if order_count is not None:
        fields["order_count"] = int(order_count)
        fields["orders_count"] = int(order_count)
    if total_spent is not None:
        fields["total_spent"] = str(total_spent)
    if recent_orders is not None:
        fields["recent_orders"] = recent_orders
        fields["orders_data"] = recent_orders
    if active_orders is not None:
        fields["active_orders"] = active_orders
    if purchased_product_ids is not None:
        fields["purchased_product_ids"] = purchased_product_ids
    if top_types is not None:
        fields["top_types"] = top_types
    if top_tags is not None:
        fields["top_tags"] = top_tags
    if addresses:
        fields["addresses"] = str(addresses)
    if tags:
        fields["tags"] = tags if isinstance(tags, str) else json.dumps(tags)
    if currency:
        fields["currency"] = str(currency)
    if preferences:
        fields["preferences"] = preferences

    # Preserve existing chats and searches if not passed in this call
    for preserve_key in ("chat_history", "last_message", "total_chats", "recent_searches", "favorite_colors"):
        if preserve_key not in fields and existing.get(preserve_key):
            fields[preserve_key] = existing[preserve_key]

    return await save_profile(shop, customer_id=customer_id or existing.get("customer_id"), email=email or existing.get("email"), fields=fields)


async def append_chat_turn(
    shop: str,
    customer_id: str | None = None,
    email: str | None = None,
    name: str | None = None,
    user_message: str = "",
    bot_reply: str = "",
    session_id: str = "",
    visitor_id: str = "",
) -> bool:
    """Appends a customer's conversation message and AI reply to their profile in Appwrite."""
    if not shop or (not customer_id and not email):
        return False

    existing = await get_profile(shop, customer_id=customer_id, email=email) or {}

    history_list = existing.get("chat_history") or []
    if isinstance(history_list, str):
        try:
            history_list = json.loads(history_list)
        except Exception:
            history_list = []
    elif not isinstance(history_list, list):
        history_list = []

    now_ts = int(time.time())
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_ts))

    if user_message:
        history_list.append({
            "role": "user",
            "message": user_message[:1000],
            "time": now_iso,
            "timestamp": now_ts,
            "session_id": session_id or "",
        })
    if bot_reply:
        history_list.append({
            "role": "assistant",
            "message": bot_reply[:2000],
            "time": now_iso,
            "timestamp": now_ts,
            "session_id": session_id or "",
        })

    # Keep last 50 turns to fit cleanly within string attribute constraints
    history_list = history_list[-50:]

    fields: dict[str, Any] = {
        "chat_history": history_list,
        "last_message": (user_message or bot_reply)[:1000],
        "total_chats": int(existing.get("total_chats") or 0) + (1 if user_message else 0),
        "last_seen": now_ts,
    }
    if name and not existing.get("name"):
        fields["name"] = name
    if email and not existing.get("email"):
        fields["email"] = email
    if customer_id and not existing.get("customer_id"):
        fields["customer_id"] = str(customer_id)

    return await save_profile(
        shop,
        customer_id=customer_id or existing.get("customer_id"),
        email=email or existing.get("email"),
        fields=fields,
    )


async def list_customer_profiles(shop: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """Retrieves all customer profiles for a given store, sorted by most recently active."""
    if not _configured():
        return []
    shop_clean = str(shop or "").strip().lower()

    if databases and Query:
        try:
            res = databases.list_documents(
                DATABASE_ID,
                CUSTOMERS_COLLECTION_ID,
                queries=[
                    Query.equal("shop", shop_clean),
                    Query.order_desc("last_seen"),
                    Query.limit(limit),
                    Query.offset(offset),
                ],
            )
            raw_docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
            return [_decode(d) for d in raw_docs]
        except Exception as e:
            # Try secondary query with "store" attribute if "shop" index not yet ready
            try:
                res = databases.list_documents(
                    DATABASE_ID,
                    CUSTOMERS_COLLECTION_ID,
                    queries=[
                        Query.equal("store", shop_clean),
                        Query.limit(limit),
                        Query.offset(offset),
                    ],
                )
                raw_docs = res.get("documents", []) if isinstance(res, dict) else getattr(res, "documents", [])
                return [_decode(d) for d in raw_docs]
            except Exception:
                print(f"[customer_profiles] list_customer_profiles failed: {e!r}")

    return []


async def delete_profile(shop: str, customer_id: str | None = None, email: str | None = None) -> bool:
    """GDPR privacy deletion for customers/redact and shop/redact."""
    if not _configured():
        return False
    doc_id = _doc_id(shop, customer_id, email)
    if databases:
        try:
            databases.delete_document(DATABASE_ID, CUSTOMERS_COLLECTION_ID, doc_id)
            return True
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.delete(f"{_base_rest()}/{doc_id}", headers=_hdr_rest())
            return resp.status_code in (200, 204, 404)
    except Exception:
        return False


# ---------------------------------------------------------------------
# 6. Recommendation Signals
# ---------------------------------------------------------------------
def _merge_recent(old, new, cap):
    out = [x for x in new if x]
    for x in old or []:
        if x not in out:
            out.append(x)
    return out[:cap]


async def record_interaction(shop: str, customer_id: str, entities: dict, existing: dict | None = None) -> bool:
    """Remembers what a shopper searched for, so recommendations can use it."""
    try:
        if not customer_id or not entities:
            return False
        profile = existing if existing is not None else (await get_profile(shop, customer_id) or {})
        fields: dict = {"last_seen": int(time.time())}

        terms = [str(entities[k]).strip().lower() for k in ("query", "category") if entities.get(k)]
        if terms:
            fields["recent_searches"] = _merge_recent(profile.get("recent_searches"), terms, 20)
        if entities.get("color"):
            fields["favorite_colors"] = _merge_recent(
                profile.get("favorite_colors"), [str(entities["color"]).strip().lower()], 10
            )
        for src, dst in (("price_min", "budget_min"), ("price_max", "budget_max")):
            try:
                if entities.get(src) not in (None, ""):
                    fields[dst] = int(float(entities[src]))
            except (TypeError, ValueError):
                pass
        if len(fields) == 1:
            return False
        return await save_profile(shop, customer_id=customer_id, fields=fields)
    except Exception as e:
        print(f"customer_profiles.record_interaction failed: {e!r}")
        return False
