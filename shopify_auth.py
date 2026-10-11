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

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, PlainTextResponse

import repository_appwrite as repo

router = APIRouter(tags=["shopify-auth"])

SHOPIFY_API_KEY = (os.environ.get("SHOPIFY_API_KEY") or "").strip()
SHOPIFY_API_SECRET = (os.environ.get("SHOPIFY_API_SECRET") or "").strip()
SHOPIFY_SCOPES = (
    os.environ.get("SHOPIFY_SCOPES")
    or os.environ.get("SCOPES")
    or "read_products,read_orders,write_orders,read_customers"
).strip()

# >>> ADDED (analytics): always ask Shopify for the permissions the
# dashboard's Analytics page needs (orders + products), even if the
# SHOPIFY_SCOPES / SCOPES environment variable on Render lists fewer.
for _needed_scope in ("read_products", "read_orders"):
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
            },
        }

    store = repo.get_store(shop)
    if store and store.get("access_token"):
        return RedirectResponse(f"/dashboard?shop={urllib.parse.quote(shop)}")

    return RedirectResponse(f"/auth?shop={urllib.parse.quote(shop)}")


@router.get("/health")
def health():
    return {"status": "healthy"}


# --------------------------------------------------------------------
# Step 1: redirect to Shopify's permission screen
# --------------------------------------------------------------------
@router.get("/auth")
def start_auth(shop: str):
    if not SHOPIFY_API_KEY or not SHOPIFY_API_SECRET or not APP_URL:
        return PlainTextResponse(
            "Missing required environment variables. Check SHOPIFY_API_KEY, "
            "SHOPIFY_API_SECRET, and APP_URL (or HOST) on Render.",
            status_code=500,
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

    store = repo.upsert_shop(shop, access_token, granted_scopes)
    repo.ensure_customization(store["$id"])
    is_new_store = not repo.has_dashboard_user(store["$id"])

    destination = (
        f"/dashboard/signup?shop={urllib.parse.quote(shop)}"
        if is_new_store
        else f"/dashboard/login?shop={urllib.parse.quote(shop)}"
    )
    return RedirectResponse(destination)
