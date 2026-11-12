"""
main.py
-------
App entrypoint. No more Base.metadata.create_all() — Appwrite collections
are created once via setup_collections.py, not on every app startup.

Run locally:
    uvicorn main:app --reload --port 8000

Run setup_collections.py once, the first time you connect a new Appwrite
project, before starting the app.
"""
import os
import re
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------
# Startup env-var validation — fail loudly at boot rather than
# silently during the first merchant install.
# --------------------------------------------------------------------
_REQUIRED_VARS = {
    "APPWRITE_ENDPOINT":    "Appwrite cloud/self-hosted URL  (e.g. https://cloud.appwrite.io/v1)",
    "APPWRITE_PROJECT_ID":  "Appwrite Project ID             (from your Appwrite console)",
    "APPWRITE_API_KEY":     "Appwrite server-side API key    (databases.* AND files.*, buckets.* scopes required for icons)",
    "APPWRITE_DATABASE_ID": "Appwrite Database ID            (the DB you created for this app)",
    "SHOPIFY_API_KEY":      "Shopify App API Key / Client ID (from Partners Dashboard)",
    "SHOPIFY_API_SECRET":   "Shopify App API Secret          (from Partners Dashboard)",
    "APP_URL":              "Public HTTPS URL of this server (e.g. https://myapp.onrender.com)",
}

_missing = [f"  {var}  — {desc}" for var, desc in _REQUIRED_VARS.items() if not os.environ.get(var)]
if _missing:
    import sys
    print("\n" + "=" * 70)
    print("STARTUP ERROR — the following required environment variables are NOT SET:")
    print("\n".join(_missing))
    print("=" * 70)
    print("Set them in your .env file (local) or in your hosting platform's")
    print("environment settings (Render / Railway / Fly.io etc.) and restart.\n")
    # Warn but don't crash — lets the server run so the / health endpoint
    # can still respond and the error is visible in platform logs.
    # Change `print` to `sys.exit(1)` above if you prefer a hard stop.

# --------------------------------------------------------------------
# Appwrite connectivity probe — runs once at startup, prints clear
# PASS/FAIL so you know immediately if the database connection works.
# --------------------------------------------------------------------
def _probe_appwrite():
    try:
        from appwrite_client import databases, DATABASE_ID, STORES_COLLECTION
        if not DATABASE_ID:
            print("[Appwrite probe] SKIP — APPWRITE_DATABASE_ID is not set")
            return
        # list_documents with limit=1 is a lightweight read-only ping
        from appwrite.query import Query
        result = databases.list_documents(
            DATABASE_ID, STORES_COLLECTION,
            queries=[Query.limit(1)],
        )
        total = result.get("total", "?") if isinstance(result, dict) else getattr(result, "total", "?")
        print(f"[Appwrite probe] ✅  Connected — 'stores' collection reachable "
              f"({total} document(s) found)")
    except Exception as e:
        print(f"[Appwrite probe] ❌  FAILED: {e!r}")
        print("[Appwrite probe]    App installs will NOT be saved until this is fixed.")
        print("[Appwrite probe]    Run: python test_appwrite_connection.py  for a full diagnosis.")


def _probe_appwrite_storage():
    try:
        from appwrite_client import storage, WIDGET_ICONS_BUCKET
        storage.get_bucket(WIDGET_ICONS_BUCKET)
        print(f"[Appwrite Storage probe] ✅  Connected — Storage bucket '{WIDGET_ICONS_BUCKET}' reachable.")
    except Exception as e:
        if "policy" in str(e).lower():
            print(f"[Appwrite Storage probe] ⚠️  Storage scopes not enabled on API key. Enable 'files.*' and 'buckets.*' in Appwrite Console -> API Keys for permanent custom icons.")
        else:
            print(f"[Appwrite Storage probe] ℹ️  Storage bucket check notice: {e!r}")


def _probe_appwrite_customer_profiles():
    try:
        import customer_profiles
        customer_profiles.ensure_customer_profiles_collection()
        print("[Appwrite Customer Profiles] ✅  Schema checked and initialized.")
    except Exception as e:
        print(f"[Appwrite Customer Profiles] ℹ️  Notice: {e!r}")


_probe_appwrite()
_probe_appwrite_storage()
_probe_appwrite_customer_profiles()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from nicegui import ui

import shopify_auth
import webhooks
import dashboard_nicegui  # noqa: F401 — importing this registers all @ui.page routes below
import chatbot_widget  # NOTE: still needs migrating off SQLAlchemy — see below
import activity_tracker

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Shopify Chatbot Plugin")

# CORS — must come before/alongside your other middleware. The widget
# is embedded on arbitrary Shopify storefront domains (a different
# origin than this backend), so the browser requires this to allow the
# cross-origin POST /chat, /confirm, and GET /widget.js, /widget-config
# calls it makes. allow_credentials=False is correct here since these
# widget endpoints don't use cookies (shop is passed explicitly in the
# request body/query) — only your dashboard needs cookies, and that's
# loaded same-origin inside Shopify's iframe, so it's unaffected by this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

_app_url = os.environ.get("APP_URL", "")
_is_https = _app_url.startswith("https://") or os.environ.get("FORCE_HTTPS", "").lower() in ("1", "true") or os.environ.get("ENVIRONMENT") == "production"

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production"),
    same_site="lax",
    https_only=_is_https,
)

os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(shopify_auth.router)
app.include_router(webhooks.router)
app.include_router(chatbot_widget.router)  # add back once migrated
app.include_router(activity_tracker.router)


# --------------------------------------------------------------------
# NiceGUI mount — must come LAST among routers/mounts above.
# This attaches dashboard_nicegui.py's @ui.page("/dashboard/...") routes
# (plus NiceGUI's own internal static/socket.io routes) onto `app`.
# --------------------------------------------------------------------
ui.run_with(app, storage_secret=os.environ.get("SESSION_SECRET", "change-me-in-production"))


# --------------------------------------------------------------------
# Cookie fix for Shopify's embedded iframe
# --------------------------------------------------------------------
# Your dashboard loads inside an <iframe> in Shopify Admin, on a
# different domain than your app. Browsers treat any cookie your app
# sets in that context as a THIRD-PARTY cookie, and by default refuse
# to send it back on the next request unless it's explicitly marked
# `SameSite=None; Secure`. Without this, login "silently" fails —
# the session cookie gets set, but the browser drops it, so the very
# next page load looks logged-out again.
#
# Neither Starlette's SessionMiddleware nor NiceGUI's internal storage
# middleware expose a public option to set this, so this rewrites every
# outgoing Set-Cookie header directly at the ASGI level, after
# everything else (including NiceGUI's own middleware) has run.
#
# Must be added AFTER ui.run_with(...) above, so it wraps the outside
# of everything and can rewrite cookies set by any layer, including
# NiceGUI's.
#
# Requires HTTPS in production — `Secure` cookies are refused entirely
# over plain HTTP. If you're testing this locally over http://, this
# middleware will still add the flag, but the browser will discard the
# cookie anyway; that's expected and not a bug in this middleware.
class ForceCookieSameSiteNone:
    # NOTE: Starlette's add_middleware() always instantiates middleware as
    # cls(app=app, **options) — the parameter MUST be named `app`, not
    # something else, or you get "unexpected keyword argument 'app'".
    def __init__(self, app):
        self.asgi_app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            try:
                await self.asgi_app(scope, receive, send)
            except KeyError as e:
                if "Session is disconnected" in str(e):
                    return
                raise
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                new_headers = []
                for key, value in message.get("headers", []):
                    if key.lower() == b"set-cookie":
                        cookie = value.decode("latin-1")
                        cookie = re.sub(r"(?i);?\s*samesite=\w+", "", cookie)  # strip any existing SameSite
                        cookie += "; SameSite=None"
                        if "secure" not in cookie.lower():
                            cookie += "; Secure"
                        value = cookie.encode("latin-1")
                    new_headers.append((key, value))
                message["headers"] = new_headers
            await send(message)

        try:
            await self.asgi_app(scope, receive, send_wrapper)
        except KeyError as e:
            if "Session is disconnected" in str(e):
                response = Response(
                    content=b'{"code": 1, "message": "Session is disconnected"}',
                    status_code=400,
                    media_type="application/json",
                )
                await response(scope, receive, send)
                return
            raise


app.add_middleware(ForceCookieSameSiteNone)


# --------------------------------------------------------------------
# Analytics events table (added)
# --------------------------------------------------------------------
# 1) Dashboard charts for chat usage, searches and cart adds now read the
#    separate `analytics_events` collection. Orders / revenue / purchased
#    products / categories are NOT listed here, so they keep coming live
#    from Shopify. For a non-Shopify platform, drop the `metrics=` list to
#    use the events table for everything.
import threading
import time
import repository_appwrite as _repo

_repo.register_event_providers(
    dashboard_nicegui.register_analytics_provider,
    platform="shopify",
    metrics=["chat_summary", "chat_usage_daily", "product_searches",
             "agent_cart_adds", "product_cart_adds", "product_views"],
)


# 2) Delete analytics events older than 60 days, once a day. A plain
#    background thread is used so it works regardless of how the app is
#    started. First run is 60 seconds after startup.
def _events_retention_loop():
    time.sleep(60)
    while True:
        try:
            removed = _repo.purge_old_events()
            print(f"analytics retention: removed {removed} events older than {_repo.EVENT_RETENTION_DAYS} days")
        except Exception as e:  # noqa: BLE001 — never let this thread die
            print(f"analytics retention failed: {e!r}")
        time.sleep(24 * 60 * 60)


threading.Thread(target=_events_retention_loop, daemon=True, name="events-retention").start()
