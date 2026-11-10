"""
shopify_auth.py
----------------
The Shopify OAuth "install app" flow — same OAuth logic as before
(make_state/verify_state/verify_hmac unchanged), now backed by Appwrite
instead of SQLAlchemy. Access tokens live in the Appwrite `stores`
collection rather than a Postgres table or an in-memory dict.

Three routes, same as before:

GET /                Shopify sends merchants here. Reads ?shop=xxx. If we
                      already have a valid token, skip to dashboard.
                      Otherwise start OAuth.
GET /auth             Redirects to Shopify's permission-grant screen.
GET /auth/callback    Shopify redirects back with a temporary `code`;
                      verify state + HMAC, exchange for an access token,
                      save it, then route to signup (new store) or
                      login (returning store).
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse

from types import SimpleNamespace
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, PlainTextResponse, HTMLResponse

import repository_appwrite as repo
import shopify_actions

router = APIRouter(tags=["shopify-auth"])

SHOPIFY_API_KEY = (os.environ.get("SHOPIFY_API_KEY") or "").strip()
SHOPIFY_API_SECRET = (os.environ.get("SHOPIFY_API_SECRET") or "").strip()
SHOPIFY_SCOPES = (
    os.environ.get("SHOPIFY_SCOPES")
    or os.environ.get("SCOPES")
    or "read_products,read_orders,write_orders,read_customers"
).strip()

# >>> ADDED (analytics & widget installation): always ask Shopify for the permissions the
# dashboard's Analytics page needs (orders + products) and widget script installation (script_tags + themes).
for _needed_scope in ("read_products", "read_orders", "write_script_tags", "read_script_tags", "write_themes", "read_themes"):
    if _needed_scope not in [_s.strip() for _s in SHOPIFY_SCOPES.split(",")]:
        SHOPIFY_SCOPES = f"{SHOPIFY_SCOPES},{_needed_scope}" if SHOPIFY_SCOPES else _needed_scope
# <<< END ADDED
APP_URL = (os.environ.get("APP_URL") or os.environ.get("HOST") or "http://localhost:8000").strip().rstrip("/")

STATE_MAX_AGE_SECONDS = 600


# --------------------------------------------------------------------
# Carried over unchanged from the version we debugged together.
# --------------------------------------------------------------------
def make_state(shop: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{shop}:{timestamp}"
    signature = hmac.new(SHOPIFY_API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_state(state: str, shop: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        shop_in_state, timestamp, signature = raw.split(":")
    except Exception as e:
        print(f"[state check] FAILED to decode state: {e}. Raw state received: {state!r}")
        return False
    if shop_in_state != shop:
        print(f"[state check] SHOP MISMATCH. In state: {shop_in_state!r}, from query: {shop!r}")
        return False
    expected_payload = f"{shop_in_state}:{timestamp}"
    expected_signature = hmac.new(SHOPIFY_API_SECRET.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        print(f"[state check] SIGNATURE MISMATCH. Expected {expected_signature}, got {signature}")
        return False
    age = int(time.time()) - int(timestamp)
    if age > STATE_MAX_AGE_SECONDS:
        print(f"[state check] EXPIRED. Age was {age} seconds (max {STATE_MAX_AGE_SECONDS})")
        return False
    print(f"[state check] OK for shop {shop}")
    return True


def verify_hmac(params: dict) -> bool:
    hmac_value = params.get("hmac")
    if not hmac_value:
        return False
    rest = {k: v for k, v in params.items() if k != "hmac"}
    message = "&".join(f"{k}={v}" for k, v in sorted(rest.items()))
    generated_hash = hmac.new(SHOPIFY_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(generated_hash, hmac_value)


# --------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------
@router.get("/")
async def app_entry(request: Request):
    shop = request.query_params.get("shop")
    if not shop:
        return {
            "status": "running",
            "message": "Shopify AI Agent backend is running.",
            "env_check": {
                "SHOPIFY_API_KEY_set": bool(SHOPIFY_API_KEY),
                "SHOPIFY_API_SECRET_set": bool(SHOPIFY_API_SECRET),
                "APP_URL_set": bool(APP_URL),
                "APPWRITE_DATABASE_ID_set": bool(repo.DATABASE_ID),
                "APPWRITE_PROJECT_ID_set": bool(repo.APPWRITE_PROJECT_ID),
            },
        }

    try:
        store = repo.get_store(shop)
    except Exception as e:
        print(f"shopify_auth: store lookup failed for {shop}: {e!r}")
        store = None

    if store and store.get("access_token"):
        return RedirectResponse(f"/dashboard?shop={urllib.parse.quote(shop)}")

    # If Shopify OAuth credentials are not configured yet, direct to Dashboard Setup
    if not SHOPIFY_API_KEY or not SHOPIFY_API_SECRET:
        return RedirectResponse(f"/dashboard/setup?shop={urllib.parse.quote(shop)}")

    return RedirectResponse(f"/auth?shop={urllib.parse.quote(shop)}")


@router.get("/health")
def health():
    return {"status": "healthy"}


@router.get("/debug/store")
async def debug_store(shop: str = ""):
    """Diagnostic endpoint — shows what the DB says about a shop domain.
    Usage: GET /debug/store?shop=your-store.myshopify.com
    Remove or secure this route once debugging is complete.
    """
    import appwrite_client as _awc
    result: dict = {
        "query_domain": shop,
        "normalized": repo._normalize_domain(shop) if shop else None,
        "appwrite_endpoint": _awc.APPWRITE_ENDPOINT,
        "appwrite_project_id": _awc.APPWRITE_PROJECT_ID,
        "database_id": _awc.DATABASE_ID,
        "stores_collection": _awc.STORES_COLLECTION,
    }
    if shop:
        try:
            store = repo.get_store(shop)
            result["store_found"] = store is not None
            if store:
                result["store_id"] = store.get("$id")
                result["store_domain"] = store.get("shop_domain")
                result["has_token"] = bool(store.get("access_token"))
                result["uninstalled"] = store.get("uninstalled")
                result["plan"] = store.get("plan")
                result["setup_completed"] = store.get("setup_completed")
        except Exception as e:
            result["store_lookup_error"] = str(e)
        # Also try listing first 5 stores
        try:
            from appwrite_client import databases, DATABASE_ID, STORES_COLLECTION
            from appwrite.query import Query
            from appwrite_client import _normalize_list
            docs_res = databases.list_documents(DATABASE_ID, STORES_COLLECTION, queries=[Query.limit(5)])
            result["total_stores_in_db"] = docs_res.get("total", "unknown") if isinstance(docs_res, dict) else "unknown"
            result["sample_domains"] = [d.get("shop_domain") for d in _normalize_list(docs_res).get("documents", [])]
        except Exception as e:
            result["store_list_error"] = str(e)
    return result


# --------------------------------------------------------------------
# Step 1: redirect to Shopify's permission screen
# --------------------------------------------------------------------
@router.get("/auth")
def start_auth(shop: str = ""):
    if not shop:
        return RedirectResponse("/dashboard")

    if not SHOPIFY_API_KEY or not SHOPIFY_API_SECRET or not APP_URL:
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Configuration Required — RenderLink AI</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8fafc; margin: 0; padding: 40px 16px; display: flex; justify-content: center; align-items: center; min-height: 100vh; box-sizing: border-box; }}
        .card {{ background: white; border-radius: 16px; padding: 32px; max-width: 520px; width: 100%; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        h1 {{ font-size: 20px; font-weight: 700; color: #0f172a; margin-top: 0; }}
        p {{ font-size: 14px; color: #475569; line-height: 1.5; }}
        .badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; background: #fef3c7; color: #92400e; margin-bottom: 16px; }}
        .env-list {{ background: #f1f5f9; border-radius: 8px; padding: 12px 16px; font-family: monospace; font-size: 13px; color: #334155; margin: 16px 0; }}
        .btn {{ display: block; text-align: center; background: #111827; color: white; text-decoration: none; padding: 12px 20px; border-radius: 10px; font-weight: 600; font-size: 14px; margin-top: 20px; }}
        .btn:hover {{ background: #1f2937; }}
    </style>
</head>
<body>
    <div class="card">
        <span class="badge">Store Configuration</span>
        <h1>Shopify App Setup Required</h1>
        <p>To connect seamlessly with <strong>{shop}</strong> via OAuth, set the following environment variables on your server (or in <code>.env</code>):</p>
        <div class="env-list">
            <div>SHOPIFY_API_KEY: {'[Configured]' if SHOPIFY_API_KEY else '[Missing]'}</div>
            <div>SHOPIFY_API_SECRET: {'[Configured]' if SHOPIFY_API_SECRET else '[Missing]'}</div>
            <div>APP_URL: {'[Configured]' if APP_URL else '[Missing]'}</div>
        </div>
        <p>You can also proceed directly to the store owner dashboard:</p>
        <a class="btn" href="/dashboard/setup?shop={urllib.parse.quote(shop)}">Open Store Dashboard Setup</a>
    </div>
</body>
</html>""",
            status_code=200,
        )

    state = make_state(shop)
    print(f"[auth start] shop={shop} generated state={state!r}")
    redirect_uri = f"{APP_URL}/auth/callback"
    query = urllib.parse.urlencode(
        {
            "client_id": SHOPIFY_API_KEY,
            "scope": SHOPIFY_SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    install_url = f"https://{shop}/admin/oauth/authorize?{query}"
    return RedirectResponse(install_url)


# --------------------------------------------------------------------
# Step 2: Shopify redirects back with a temporary `code`
# --------------------------------------------------------------------
@router.get("/auth/callback")
async def auth_callback(request: Request):
    params = dict(request.query_params)
    shop = params.get("shop")
    code = params.get("code")
    state = params.get("state")

    if not shop or not code:
        return PlainTextResponse("Missing shop or code in callback", status_code=400)

    print(f"[callback] shop={shop!r} state received={state!r}")

    if not state or not verify_state(state, shop):
        return PlainTextResponse("Invalid state parameter — possible CSRF attempt", status_code=403)

    if not verify_hmac(params):
        return PlainTextResponse("HMAC validation failed", status_code=403)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": SHOPIFY_API_KEY,
                "client_secret": SHOPIFY_API_SECRET,
                "code": code,
            },
        )

    if response.status_code != 200:
        return PlainTextResponse(f"Failed to exchange code for access token: {response.text}", status_code=500)

    data = response.json()
    access_token = data.get("access_token")
    granted_scopes = data.get("scope", "")
    print(f"Installed on {shop}. Scopes: {granted_scopes}. Token starts with: {access_token[:10]}...")

    # Fetch store information from Shopify Admin API (shop.json)
    store_obj = SimpleNamespace(shop_domain=shop, access_token=access_token, id="")
    shop_info = await shopify_actions.fetch_shop_details(store_obj)
    plan_name = shop_info.get("plan_name", "free")
    business_name = shop_info.get("name", "")
    support_email = shop_info.get("customer_email") or shop_info.get("email") or ""
    timezone_val = shop_info.get("timezone", "UTC")

    # Upsert store document with plan, setup_completed, uninstalled, etc.
    store = repo.upsert_shop(shop, access_token, granted_scopes, plan=plan_name)
    store_obj.id = store["$id"]

    repo.ensure_customization(store["$id"])
    repo.ensure_features(store["$id"])
    repo.ensure_store_info(
        store["$id"],
        business_name=business_name,
        support_email=support_email,
        timezone=timezone_val,
    )
    is_new_store = not repo.has_dashboard_user(store["$id"])

    # Directly add widget script to the store upon app install
    try:
        install_res = await shopify_actions.install_widget_script(store_obj, app_url=APP_URL)
        print(f"[install callback] Auto-installed widget script on {shop}: {install_res}")
    except Exception as e:
        print(f"[install callback] Widget script auto-install failed on {shop}: {e!r}")

    destination = (
        f"/dashboard/setup?shop={urllib.parse.quote(shop)}"
        if is_new_store
        else f"/dashboard/login?shop={urllib.parse.quote(shop)}"
    )
    return RedirectResponse(destination)
