"""
customer_profiles.py
---------------------
Everything needed to recognise a LOGGED-IN shopper safely and to remember
them between visits:

  1. verify_app_proxy()   - proves a request really came from Shopify's
                            App Proxy (HMAC signature) and therefore that
                            `logged_in_customer_id` is genuine.
  2. make/read_session_token() - a short-lived signed token handed to the
                            widget so every /chat call can be tied to the
                            verified customer. The browser can't forge it.
  3. get/save/delete_profile() - a small per-customer record in Appwrite
                            (name, email, order count, purchases, favourite
                            types/tags, recent searches, colours, budget).
  4. record_interaction() - remembers what a logged-in shopper searched for
                            so recommendations can use it.

Env vars:
  SHOPIFY_API_SECRET             your app's Client Secret (signs app-proxy requests)
  SESSION_SECRET                 any long random string (signs chat session tokens);
                                 falls back to SHOPIFY_API_SECRET if unset
  APPWRITE_ENDPOINT              e.g. https://cloud.appwrite.io/v1
  APPWRITE_PROJECT_ID
  APPWRITE_API_KEY               server key with databases.read/write
  APPWRITE_DATABASE_ID
  APPWRITE_CUSTOMERS_COLLECTION_ID   default: customer_profiles

Appwrite collection `customer_profiles` attributes needed:
  shop (string), customer_id (string), first_name (string), last_name (string),
  email (string), order_count (int), synced_at (int),
  purchased_product_ids (string, large), top_types (string), top_tags (string),
  -- new --
  recent_searches (string 4096), favorite_colors (string 1024),
  last_seen (int), budget_min (int), budget_max (int)
"""

import base64
import hashlib
import hmac
import json
import os
import time

import httpx

SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or SHOPIFY_API_SECRET
SESSION_TTL_SECONDS = 2 * 60 * 60
PROXY_MAX_AGE_SECONDS = 10 * 60

APPWRITE_ENDPOINT = os.environ.get("APPWRITE_ENDPOINT", "").rstrip("/")
APPWRITE_PROJECT_ID = os.environ.get("APPWRITE_PROJECT_ID", "")
APPWRITE_API_KEY = os.environ.get("APPWRITE_API_KEY", "")
APPWRITE_DATABASE_ID = os.environ.get("APPWRITE_DATABASE_ID", "")
CUSTOMERS_COLLECTION_ID = os.environ.get("APPWRITE_CUSTOMERS_COLLECTION_ID", "customer_profiles")

_LIST_FIELDS = ("purchased_product_ids", "top_types", "top_tags", "recent_searches", "favorite_colors")
_INT_FIELDS = ("order_count", "synced_at", "last_seen", "budget_min", "budget_max")


# ---------------------------------------------------------------------
# 1. App Proxy signature check
# ---------------------------------------------------------------------
def verify_app_proxy(query_items: list[tuple[str, str]]) -> bool:
    """query_items: request.query_params.multi_items(). Follows Shopify's
    documented algorithm: drop `signature`, sort the rest by key, build
    "key=value" (repeated keys joined with commas), concatenate with NO
    separator, HMAC-SHA256 with the app's client secret."""
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
    """Returns the verified customer id, or None if the token is missing,
    forged, expired, or issued for a different shop."""
    if not token or not SESSION_SECRET or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig_b64)):
            return None
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    if data.get("shop") != shop or int(data.get("exp", 0)) < time.time():
        return None
    return str(data.get("cid") or "") or None


# ---------------------------------------------------------------------
# 3. Appwrite storage (REST)
# ---------------------------------------------------------------------
def _configured() -> bool:
    return all([APPWRITE_ENDPOINT, APPWRITE_PROJECT_ID, APPWRITE_API_KEY, APPWRITE_DATABASE_ID])


def _base() -> str:
    return f"{APPWRITE_ENDPOINT}/databases/{APPWRITE_DATABASE_ID}/collections/{CUSTOMERS_COLLECTION_ID}/documents"


def _hdr() -> dict:
    return {
        "X-Appwrite-Project": APPWRITE_PROJECT_ID,
        "X-Appwrite-Key": APPWRITE_API_KEY,
        "Content-Type": "application/json",
    }


def _doc_id(shop: str, customer_id: str) -> str:
    """Deterministic id, so we can GET/PATCH by id with no query/index.
    (Appwrite ids may be at most 36 chars of [a-zA-Z0-9_.-].)"""
    return hashlib.sha1(f"{shop}:{customer_id}".encode()).hexdigest()[:36]


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if v is None:
            continue
        out[k] = json.dumps(v, ensure_ascii=False) if k in _LIST_FIELDS else v
    return out


def _decode(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if not k.startswith("$")}
    for k in _LIST_FIELDS:
        try:
            out[k] = json.loads(out.get(k) or "[]")
        except (TypeError, ValueError):
            out[k] = []
    for k in _INT_FIELDS:
        try:
            out[k] = int(out.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


async def get_profile(shop: str, customer_id: str) -> dict | None:
    if not _configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_base()}/{_doc_id(shop, customer_id)}", headers=_hdr())
    except Exception as e:  # noqa: BLE001
        print(f"customer_profiles.get_profile failed: {e!r}")
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        print(f"customer_profiles.get_profile -> {resp.status_code} {resp.text[:300]}")
        return None
    return _decode(resp.json())


async def save_profile(shop: str, customer_id: str, fields: dict) -> bool:
    """Create-or-update. Only the fields passed are changed."""
    if not _configured():
        return False
    doc_id = _doc_id(shop, customer_id)
    data = _encode({**fields, "shop": shop, "customer_id": str(customer_id)})
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(f"{_base()}/{doc_id}", headers=_hdr(), json={"data": data})
            if resp.status_code == 404:
                resp = await client.post(_base(), headers=_hdr(), json={"documentId": doc_id, "data": data})
    except Exception as e:  # noqa: BLE001
        print(f"customer_profiles.save_profile failed: {e!r}")
        return False
    if resp.status_code not in (200, 201):
        print(f"customer_profiles.save_profile -> {resp.status_code} {resp.text[:300]}")
        return False
    return True


async def delete_profile(shop: str, customer_id: str) -> bool:
    """Call this from Shopify's customers/redact (and shop/redact) privacy
    webhooks - stored customer data must be erasable on request."""
    if not _configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{_base()}/{_doc_id(shop, customer_id)}", headers=_hdr())
    except Exception as e:  # noqa: BLE001
        print(f"customer_profiles.delete_profile failed: {e!r}")
        return False
    return resp.status_code in (200, 204, 404)


# ---------------------------------------------------------------------
# 4. Recommendation signals
# ---------------------------------------------------------------------
def _merge_recent(old, new, cap):
    out = [x for x in new if x]
    for x in old or []:
        if x not in out:
            out.append(x)
    return out[:cap]


async def record_interaction(shop: str, customer_id: str, entities: dict, existing: dict | None = None) -> bool:
    """Remembers what a logged-in shopper searched for, so recommendations
    can use it. Best-effort: never raises."""
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
        if len(fields) == 1:  # nothing useful beyond last_seen
            return False
        return await save_profile(shop, customer_id, fields)
    except Exception as e:  # noqa: BLE001
        print(f"customer_profiles.record_interaction failed: {e!r}")
        return False