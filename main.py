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
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
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

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production"),
    same_site="lax",
    https_only=True,
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
            await self.asgi_app(scope, receive, send)
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

        await self.asgi_app(scope, receive, send_wrapper)


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
             "agent_cart_adds", "product_cart_adds"],
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
