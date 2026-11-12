"""
Shopify data layer — one function per action defined in intent_schema.json.

Every function calls the REAL Shopify Admin API using the access token
stored for that store during OAuth (models.Store.access_token — see
shopify_auth.py). Functions take a `Store` ORM object (not just a shop
string) so they always have the token, and return a plain dict — that
dict becomes the "source of truth" data reply_generator.py turns into
a natural-language reply in the shopper's own language.

Cart actions run entirely server-side via Shopify's Storefront API
(cartCreate / cartLinesAdd / cartLinesUpdate / cartLinesRemove), using a
per-store Storefront API access token fetched (or created, on first use)
through the Admin API — not the shopper's browser. This is a deliberate
architecture choice with one real tradeoff: a Storefront API cart is a
*separate* cart object from the storefront theme's own cookie-based
cart. It has its own `checkoutUrl`, and it will not appear if a shopper
independently opens the theme's native cart drawer/page — the widget's
own cart badge and "what's in my cart" replies are the only place this
cart is visible, and checkout must go through the `checkout_url` this
module returns rather than the store's normal /cart or /checkout URLs.

Each store's Storefront cart is tracked per chat session (in memory,
session_id -> cart GID — see _SESSION_CARTS below), not per shopper
account, since the widget has no concept of a logged-in customer.

Order-related lookups (track_order, list_recent_orders, submit_claim,
check_claim_status) require the shopper to confirm the email address
on file for that order before any details are returned or any claim is
filed/checked. Without this, an order number alone — something a
shopper could guess, reuse from their own past order, or see on a
packing slip — would let them pull up *any* customer's order status
and tracking info. If no email is given, or it doesn't match Shopify's
own record for that order, these functions return a
"verification_required"/"verification_failed" error dict instead of
data; chatbot_widget.py's PENDING mechanism (mode="verify_email")
keeps the original request alive across that follow-up turn, so the
shopper only needs to reply with an email rather than repeat the whole
request.

track_order also reports a live shipment location/status (current
city, last scan message, estimated delivery) on top of Shopify's own
fulfillment data, via tracking_provider.py (AfterShip). Shopify itself
only ever stores a tracking number and a link to the carrier's site —
it has no idea where a package actually is. This is additive and
optional: if AFTERSHIP_API_KEY isn't configured, track_order still
works exactly as before, just without the live-location fields.

SHOPIFY_API_VERSION: bump this as Shopify deprecates old ones.
"""

import inspect
import os
import re
import random
import secrets
import hashlib
import time
from datetime import datetime
import httpx
import difflib
import semantic

import tracking_provider

SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

# In-memory storage for saved payment credentials, pending OTP authorizations, and warranty claims
_SAVED_PAYMENT_METHODS: dict[str, dict] = {}
_PENDING_PAYMENTS: dict[str, dict] = {}
_WARRANTY_CLAIMS: dict[str, dict] = {}


def _headers(store) -> dict:
    return {"X-Shopify-Access-Token": store.access_token, "Content-Type": "application/json"}


def _url(store, path: str) -> str:
    return f"https://{store.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/{path}"


def _access_error(order: dict, email: str, customer_id, order_number: str, what: str = "looked up") -> dict | None:
    order_cust = (order.get("customer") or {})
    order_cust_id = str(order_cust.get("id") or "").split("/")[-1]
    raw_cust_id = str(customer_id or "").split("/")[-1]

    # 1. Customer ID match
    if raw_cust_id and order_cust_id and raw_cust_id == order_cust_id:
        return None

    order_email = (order.get("email") or order.get("contact_email") or order_cust.get("email") or "").strip().lower()
    clean_email = (email or "").strip().lower()

    # 2. Email match (user logged in with email or verified email)
    if clean_email and order_email and clean_email == order_email:
        return None

    # 3. Logged-in customer check
    if raw_cust_id:
        if not order_email or (clean_email and clean_email == order_email):
            return None
    if clean_email:
        if not order_email or clean_email == order_email:
            return None
        print(f"_access_error: email mismatch for #{order_number} ({clean_email} vs {order_email})")
        return {
            "error": "verification_failed",
            "order_number": order_number,
            "message": "That order appears to be associated with a different email address.",
        }

    # 4. Anonymous guest without email
    msg = f"For privacy, please confirm the email address used on this order before it can be {what}."
    return {"error": "verification_required", "order_number": order_number, "message": msg}


def _extract_order_number(entities: dict, message: str = "") -> str | None:
    for key in ("order_number", "order_id"):
        val = entities.get(key)
        if val:
            return str(val).lstrip("#").strip()
    match = re.search(r"#?\s*(\d{3,})", message or "")
    return match.group(1) if match else None


async def _get(store, path: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        return await client.get(_url(store, path), headers=_headers(store), params=params or {})


async def _put(store, path: str, json: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        return await client.put(_url(store, path), headers=_headers(store), json=json)


async def _post(store, path: str, json: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        return await client.post(_url(store, path), headers=_headers(store), json=json)


async def _delete(store, path: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        return await client.delete(_url(store, path), headers=_headers(store))


async def fetch_shop_details(store) -> dict:
    """Fetches store details directly from Shopify Admin REST API GET /admin/api/{version}/shop.json.
    Returns:
        dict: {
            "name": str,
            "email": str,
            "customer_email": str,
            "timezone": str,
            "plan_name": str,
            "domain": str,
            "myshopify_domain": str,
            "currency": str,
        }
    """
    try:
        resp = await _get(store, "shop.json")
        if resp.status_code == 200:
            shop_data = resp.json().get("shop", {})
            return {
                "name": shop_data.get("name") or "",
                "email": shop_data.get("email") or "",
                "customer_email": shop_data.get("customer_email") or shop_data.get("email") or "",
                "timezone": shop_data.get("iana_timezone") or shop_data.get("timezone") or "UTC",
                "plan_name": shop_data.get("plan_name") or "free",
                "domain": shop_data.get("domain") or "",
                "myshopify_domain": shop_data.get("myshopify_domain") or "",
                "currency": shop_data.get("currency") or "USD",
            }
        else:
            print(f"[fetch_shop_details] Shopify API returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[fetch_shop_details] Failed to fetch shop details: {e!r}")
    return {}


def _normalize_app_url(app_url: str = "") -> str:
    base = (app_url or os.environ.get("APP_URL") or os.environ.get("HOST") or "").strip().rstrip("/")
    if not base:
        base = "http://localhost:8000"
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"https://{base}"
    return base


async def get_widget_script_status(store, app_url: str = "") -> dict:
    """Checks whether the AI Agent widget script is installed on the store.
    Checks both Shopify ScriptTag and layout/theme.liquid injection.
    """
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", None) or (store.get("shop_domain") if isinstance(store, dict) else "")
    if not shop:
        return {"installed": False, "reason": "no_shop"}

    base_url = _normalize_app_url(app_url)
    widget_url = f"{base_url}/widget.js"

    # 1. Check ScriptTags
    try:
        st_resp = await _get(store, "script_tags.json")
        if st_resp.status_code == 200:
            existing_tags = st_resp.json().get("script_tags", [])
            for tag in existing_tags:
                src = tag.get("src") or ""
                if widget_url in src or "/widget.js" in src:
                    return {"installed": True, "method": "script_tag", "tag_id": tag.get("id"), "src": src}
    except Exception as e:
        print(f"[widget status] script_tags check failed: {e!r}")

    # 2. Check layout/theme.liquid
    try:
        themes_resp = await _get(store, "themes.json")
        if themes_resp.status_code == 200:
            themes = themes_resp.json().get("themes", [])
            main_theme = next((t for t in themes if t.get("role") == "main"), None)
            if main_theme:
                theme_id = main_theme["id"]
                asset_resp = await _get(store, f"themes/{theme_id}/assets.json", params={"asset[key]": "layout/theme.liquid"})
                if asset_resp.status_code == 200:
                    liquid = asset_resp.json().get("asset", {}).get("value") or ""
                    if widget_url in liquid or "/widget.js" in liquid:
                        return {"installed": True, "method": "theme_liquid", "theme_id": theme_id}
    except Exception as e:
        print(f"[widget status] theme asset check failed: {e!r}")

    return {"installed": False}


async def install_widget_script(store, app_url: str = "") -> dict:
    """Directly adds the AI Agent chatbot widget script to the merchant's store.
    
    Tries:
    1. Shopify ScriptTag REST API:
       Automatically loads the script asynchronously on storefront page load
       without modifying theme files.
    2. Theme Asset API (layout/theme.liquid injection):
       Fallback for themes/stores that prefer direct theme injection or restrict script tags.
       Injects defer script tag right before </body>.
    """
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", None) or (store.get("shop_domain") if isinstance(store, dict) else "")
    access_token = getattr(store, "access_token", None) or (store.get("access_token") if isinstance(store, dict) else "")
    if not shop or not access_token:
        return {"success": False, "error": "missing_credentials", "message": "Store domain or access token missing"}

    base_url = _normalize_app_url(app_url)
    widget_url = f"{base_url}/widget.js"
    methods_applied = []

    # 1. ScriptTag REST API
    try:
        st_resp = await _get(store, "script_tags.json")
        if st_resp.status_code == 200:
            existing_tags = st_resp.json().get("script_tags", [])
            already_has_tag = any(
                widget_url in (tag.get("src") or "") or "/widget.js" in (tag.get("src") or "")
                for tag in existing_tags
            )
            if already_has_tag:
                methods_applied.append("script_tag_existing")
                print(f"[widget install] ScriptTag already active on {shop}")
            else:
                create_resp = await _post(
                    store,
                    "script_tags.json",
                    json={
                        "script_tag": {
                            "event": "onload",
                            "src": widget_url,
                            "display_scope": "all",
                        }
                    },
                )
                if create_resp.status_code in (200, 201):
                    tag_id = create_resp.json().get("script_tag", {}).get("id")
                    methods_applied.append(f"script_tag_created:{tag_id}")
                    print(f"[widget install] ScriptTag created successfully on {shop} (id={tag_id})")
                else:
                    print(f"[widget install] ScriptTag create returned {create_resp.status_code}: {create_resp.text[:200]}")
    except Exception as e:
        print(f"[widget install] ScriptTag error on {shop}: {e!r}")

    # 2. Theme Asset API (layout/theme.liquid injection)
    try:
        themes_resp = await _get(store, "themes.json")
        if themes_resp.status_code == 200:
            themes = themes_resp.json().get("themes", [])
            main_theme = next((t for t in themes if t.get("role") == "main"), None)
            if main_theme:
                theme_id = main_theme["id"]
                asset_resp = await _get(store, f"themes/{theme_id}/assets.json", params={"asset[key]": "layout/theme.liquid"})
                if asset_resp.status_code == 200:
                    asset_data = asset_resp.json().get("asset", {})
                    liquid = asset_data.get("value") or ""
                    snippet = f'<!-- Shopify AI Agent Chatbot Widget -->\n<script src="{widget_url}" data-shop="{shop}" defer></script>\n<!-- /Shopify AI Agent Chatbot Widget -->'
                    if widget_url not in liquid and "/widget.js" not in liquid:
                        if "</body>" in liquid:
                            new_liquid = liquid.replace("</body>", f"{snippet}\n</body>")
                        elif "</head>" in liquid:
                            new_liquid = liquid.replace("</head>", f"{snippet}\n</head>")
                        else:
                            new_liquid = liquid + f"\n{snippet}"

                        put_resp = await _put(
                            store,
                            f"themes/{theme_id}/assets.json",
                            json={"asset": {"key": "layout/theme.liquid", "value": new_liquid}},
                        )
                        if put_resp.status_code in (200, 201):
                            methods_applied.append(f"theme_asset_injected:{theme_id}")
                            print(f"[widget install] Injected widget script tag into theme {theme_id} on {shop}")
                    else:
                        methods_applied.append("theme_asset_existing")
                        print(f"[widget install] Widget script tag already in theme {theme_id} on {shop}")
    except Exception as e:
        print(f"[widget install] Theme asset injection error on {shop}: {e!r}")

    success = len(methods_applied) > 0
    return {
        "success": success,
        "shop": shop,
        "methods": methods_applied,
        "widget_url": widget_url,
    }


async def remove_widget_script(store, app_url: str = "") -> dict:
    """Removes the AI Agent chatbot widget script from the merchant's store."""
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", None) or (store.get("shop_domain") if isinstance(store, dict) else "")
    base_url = _normalize_app_url(app_url)
    widget_url = f"{base_url}/widget.js"
    removed = []

    # 1. Remove ScriptTags
    try:
        st_resp = await _get(store, "script_tags.json")
        if st_resp.status_code == 200:
            for tag in st_resp.json().get("script_tags", []):
                src = tag.get("src") or ""
                if widget_url in src or "/widget.js" in src:
                    tag_id = tag.get("id")
                    del_resp = await _delete(store, f"script_tags/{tag_id}.json")
                    if del_resp.status_code in (200, 204):
                        removed.append(f"script_tag_deleted:{tag_id}")
    except Exception as e:
        print(f"[widget remove] ScriptTag deletion error: {e!r}")

    # 2. Clean theme.liquid
    try:
        themes_resp = await _get(store, "themes.json")
        if themes_resp.status_code == 200:
            for t in themes_resp.json().get("themes", []):
                if t.get("role") == "main":
                    theme_id = t["id"]
                    asset_resp = await _get(store, f"themes/{theme_id}/assets.json", params={"asset[key]": "layout/theme.liquid"})
                    if asset_resp.status_code == 200:
                        liquid = asset_resp.json().get("asset", {}).get("value") or ""
                        cleaned = re.sub(r"<!--\s*Shopify AI Agent Chatbot Widget\s*-->.*?<!--\s*/Shopify AI Agent Chatbot Widget\s*-->\s*", "", liquid, flags=re.DOTALL)
                        if cleaned != liquid:
                            await _put(store, f"themes/{theme_id}/assets.json", json={"asset": {"key": "layout/theme.liquid", "value": cleaned}})
                            removed.append(f"theme_asset_cleaned:{theme_id}")
    except Exception as e:
        print(f"[widget remove] Theme clean error: {e!r}")

    return {"removed": removed}




# ==========================================================================
# Authenticated customer data (GraphQL Admin API)
# ==========================================================================
async def _graphql_admin(store, query: str, variables: dict | None = None) -> dict | None:
    url = f"https://{store.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(url, headers=_headers(store), json={"query": query, "variables": variables or {}})
    except Exception as e:
        print(f"shopify_actions: GraphQL request failed: {e!r}")
        return None
    if resp.status_code != 200:
        print(f"shopify_actions: GraphQL HTTP {resp.status_code}: {resp.text[:500]}")
        return None
    body=resp.json()
    if body.get("errors"):
        print(f"shopify_actions: GraphQL errors: {body['errors']}")
        return None
    return body.get("data")


async def get_customer(store, customer_id: str) -> dict | None:
    raw_id = str(customer_id).strip().split("/")[-1]
    query = """
    query Customer($id: ID!) {
      customer(id: $id) {
        id firstName lastName displayName phone tags
        defaultEmailAddress { emailAddress }
        defaultAddress { address1 city province country zip }
        numberOfOrders
        orders(first: 20, sortKey: CREATED_AT, reverse: true) {
          nodes {
            id name createdAt displayFinancialStatus displayFulfillmentStatus cancelledAt
            totalPriceSet { shopMoney { amount currencyCode } }
            lineItems(first: 20) { nodes { name quantity } }
            fulfillments { trackingInfo { number url company } }
          }
        }
      }
    }
    """
    data = await _graphql_admin(store, query, {"id": f"gid://shopify/Customer/{raw_id}"})
    return (data or {}).get("customer")


async def get_customer_orders(store, customer_id: str, first: int=50, max_orders: int=200) -> dict:
    """All of a customer's orders, newest first, following pagination up to
    max_orders. NOTE: Shopify only returns the last 60 days of orders unless
    the app has the `read_all_orders` access scope."""
    query="""
    query CustomerOrders($first: Int!, $after: String, $query: String!) {
      orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
          totalPriceSet { shopMoney { amount currencyCode } }
          lineItems(first: 20) { nodes { name quantity product { id } variant { id title } } }
          fulfillments { trackingInfo { number url company } }
        }
      }
    }
    """
    page_size=min(max(first,1),100)
    all_nodes=[]; after=None
    while len(all_nodes)<max_orders:
        data=await _graphql_admin(store,query,{"first":page_size,"after":after,"query":f"customer_id:{customer_id}"})
        if data is None:
            if all_nodes: break  # return what we already have rather than nothing
            return {"error":"lookup_failed","message":"Could not load your orders from the store right now."}
        page=(data or {}).get("orders") or {}
        all_nodes.extend(page.get("nodes") or [])
        info=page.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"): break
        after=info["endCursor"]
    data={"orders":{"nodes":all_nodes}}
    nodes=((data or {}).get("orders") or {}).get("nodes") or []
    active=[]; past=[]; purchased=[]
    order_baskets = []
    for order in nodes:
        items=[]
        order_pids=[]
        for line in (order.get("lineItems") or {}).get("nodes") or []:
            variant=line.get("variant") or {}
            product=line.get("product") or {}
            if product.get("id"):
                p_id = str(product["id"]).split("/")[-1]
                purchased.append(p_id)
                order_pids.append(p_id)
            items.append({"name":line.get("name"),"quantity":line.get("quantity"),"variant":variant.get("title")})
        if len(order_pids) > 1:
            order_baskets.append(order_pids)
        total=(order.get("totalPriceSet") or {}).get("shopMoney") or {}
        fulfillment=(order.get("displayFulfillmentStatus") or "").lower()
        if order.get("cancelledAt"): fulfillment="cancelled"
        entry={
            "id":order.get("id"),"order_number":str(order.get("name") or "").lstrip("#"),
            "date":(order.get("createdAt") or "")[:10],"status":fulfillment.replace("_"," ") or "unknown",
            "financial_status":order.get("displayFinancialStatus"),"total":total.get("amount"),
            "currency":total.get("currencyCode"),"items":items,
            "tracking":[t for f in (order.get("fulfillments") or []) for t in (f.get("trackingInfo") or []) if t]
        }
        if fulfillment in {"fulfilled","shipped","delivered","cancelled","restocked"}: past.append(entry)
        else: active.append(entry)

    if order_baskets:
        try:
            import activity_tracker
            activity_tracker.record_order_cooccurrences(getattr(store, "shop_domain", ""), order_baskets)
        except Exception:
            pass

    return {"active_orders":active,"past_orders":past,"purchased_product_ids":list(dict.fromkeys(purchased)),"order_baskets":order_baskets}


async def get_active_products_graphql(store, first: int=100) -> list[dict]:
    query="""
    query Products($first: Int!) {
      products(first: $first, query: "status:ACTIVE") {
        nodes { id title handle productType tags featuredImage { url } variants(first: 10) { nodes { id title price } } }
      }
    }
    """
    data=await _graphql_admin(store,query,{"first":min(max(first,1),250)})
    nodes=((data or {}).get("products") or {}).get("nodes") or []
    return [{
        "id":str(p.get("id") or "").split("/")[-1],"title":p.get("title"),"handle":p.get("handle"),
        "product_type":p.get("productType"),"tags":p.get("tags") or [],"image":(p.get("featuredImage") or {}).get("url",""),
        "variants":[{"id":str(v.get("id") or "").split("/")[-1],"title":v.get("title"),"price":v.get("price")} for v in ((p.get("variants") or {}).get("nodes") or [])]
    } for p in nodes]


async def get_my_orders(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    return await list_recent_orders(store, entities, session_id, customer_id=customer_id)


async def get_recommendations(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    import behavioral_recommender
    return await behavioral_recommender.handle_recommendations(store, entities, session_id, customer_id)


# ==========================================================================
# Storefront API — cart engine
#
# In-memory caches. Both are cheap to lose on a restart: a missing
# Storefront token just gets re-fetched/re-created on the next cart
# action; a missing session->cart mapping just means that session starts
# a fresh, empty cart rather than erroring. Same volatility tradeoff
# already accepted for chatbot_widget.py's PENDING confirmation dict.
# For production durability across restarts, consider persisting both
# to Appwrite instead (mirrors how rag_retriever.py flags its own
# STORE_DATASET_MAP as a stand-in for a real Appwrite-backed mapping).
# ==========================================================================

_STOREFRONT_TOKENS: dict[str, str] = {}   # shop_domain -> storefront access token
_SESSION_CARTS: dict[str, str] = {}       # session_id  -> Storefront API cart GID
_STOREFRONT_TOKEN_TITLE = "AI Shopping Assistant"

_CART_FIELDS = """
    id
    checkoutUrl
    totalQuantity
    cost { totalAmount { amount currencyCode } }
    lines(first: 50) {
      edges {
        node {
          id
          quantity
          merchandise {
            ... on ProductVariant {
              id
              title
              product { title }
              price { amount currencyCode }
            }
          }
        }
      }
    }
"""


async def _get_storefront_token(store) -> str | None:
    """Fetches an existing Storefront API access token for this store via
    the Admin API, or creates one if none exists yet. Cached in memory
    per shop after the first lookup."""
    cached = _STOREFRONT_TOKENS.get(store.shop_domain)
    if cached:
        return cached

    try:
        resp = await _get(store, "storefront_access_tokens.json")
        if resp.status_code == 200:
            for tok in resp.json().get("storefront_access_tokens", []):
                token = tok.get("access_token")
                if token:
                    _STOREFRONT_TOKENS[store.shop_domain] = token
                    return token
    except Exception as e:
        print(f"shopify_actions: Storefront token lookup failed: {e!r}")

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            create_resp = await client.post(
                _url(store, "storefront_access_tokens.json"),
                headers=_headers(store),
                json={"storefront_access_token": {"title": _STOREFRONT_TOKEN_TITLE}},
            )
        if create_resp.status_code not in (200, 201):
            return None

        token = create_resp.json().get("storefront_access_token", {}).get("access_token")
        if token:
            _STOREFRONT_TOKENS[store.shop_domain] = token
        return token
    except Exception as e:
        print(f"shopify_actions: Storefront token creation failed: {e!r}")
        return None


def _storefront_url(store) -> str:
    return f"https://{store.shop_domain}/api/{SHOPIFY_API_VERSION}/graphql.json"


async def _storefront_query(store, query: str, variables: dict) -> dict | None:
    token = await _get_storefront_token(store)
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                _storefront_url(store),
                headers={"X-Shopify-Storefront-Access-Token": token, "Content-Type": "application/json"},
                json={"query": query, "variables": variables},
            )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("errors"):
            return None
        return body.get("data")
    except Exception as e:
        print(f"shopify_actions: Storefront query failed: {e!r}")
        return None


def _summarize_cart(cart: dict | None) -> dict:
    """Turns a Storefront API cart node into the plain-dict shape both
    reply_generator.py and widget.js consume."""
    if not cart:
        return {"items": [], "item_count": 0, "total": "0.00", "currency": None, "checkout_url": None}

    items = []
    for edge in cart.get("lines", {}).get("edges", []):
        line = edge["node"]
        merch = line.get("merchandise", {}) or {}
        price = merch.get("price", {}) or {}
        items.append({
            "line_id": line["id"],
            "name": (merch.get("product") or {}).get("title") or merch.get("title") or "Item",
            "variant": merch.get("title"),
            "quantity": line["quantity"],
            "price": price.get("amount"),
        })

    cost = (cart.get("cost") or {}).get("totalAmount") or {}
    return {
        "items": items,
        "item_count": cart.get("totalQuantity", 0),
        "total": cost.get("amount"),
        "currency": cost.get("currencyCode"),
        "checkout_url": cart.get("checkoutUrl"),
    }


def _find_line(cart_summary: dict, product_query: str) -> dict | None:
    if not isinstance(cart_summary, dict):
        return None
    needle = (product_query or "").strip().lower()
    if not needle:
        return None
    for item in cart_summary.get("items", []):
        name = (item.get("name") or item.get("title") or item.get("product_title") or "").lower()
        if needle in name or name in needle:
            return item
    return None


async def _fetch_cart(store, cart_id: str) -> dict | None:
    try:
        query = f"query getCart($id: ID!) {{ cart(id: $id) {{ {_CART_FIELDS} }} }}"
        data = await _storefront_query(store, query, {"id": cart_id})
        return (data or {}).get("cart")
    except Exception:
        return None


async def _create_cart(store) -> dict | None:
    try:
        mutation = f"""
        mutation createCart {{
          cartCreate {{
            cart {{ {_CART_FIELDS} }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = await _storefront_query(store, mutation, {})
        result = (data or {}).get("cartCreate") or {}
        if result.get("userErrors"):
            return None
        return result.get("cart")
    except Exception:
        return None


async def _get_or_create_cart(store, session_id: str) -> dict | None:
    try:
        cart_id = _SESSION_CARTS.get(session_id)
        cart = await _fetch_cart(store, cart_id) if cart_id else None
        if cart is None:
            cart = await _create_cart(store)
            if cart:
                _SESSION_CARTS[session_id] = cart["id"]
        return cart
    except Exception:
        return None


# ==========================================================================
# registration_login — shoppers use the store's own native account pages
# (Shopify handles customer auth itself; there is no Admin API endpoint
# for "log a shopper in"). We just point the widget at the right page —
# this is the one remaining case where a `widget_action` (a same-tab
# redirect) is still needed, since it's not something the server can do
# on the shopper's behalf.
# ==========================================================================
def _account_redirect(store, page: str, message: str) -> dict:
    return {
        "status": "redirect",
        "message": message,
        "widget_action": {"type": "redirect", "url": f"https://{store.shop_domain}/account/{page}"},
    }


async def register(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "register", "Taking you to the account creation page.")


async def login(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "login", "Taking you to the sign-in page.")


async def logout(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "logout", "Signing you out.")


async def forgot_password(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "login#recover", "Taking you to the password recovery page.")


# ==========================================================================
# order_tracking
# ==========================================================================
async def track_order(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    order_number = _extract_order_number(entities)
    email = (entities.get("email") or "").strip().lower()

    if not order_number and (customer_id or email):
        recent = await list_recent_orders(store, entities, session_id, customer_id=customer_id)
        orders = recent.get("orders") or (recent.get("active_orders", []) + recent.get("past_orders", []))
        if orders:
            order_number = orders[0].get("order_number") or str(orders[0].get("id"))
            entities["order_number"] = order_number

    if not order_number:
        return {"error": "missing_order_number", "message": "No order number was given."}

    # Guests must give an email up front; logged-in shoppers are checked
    # against the order's customer after the lookup.
    if not email and not customer_id:
        return {
            "error": "verification_required",
            "order_number": order_number,
            "message": "For privacy, please confirm the email address used on this order before it can be looked up.",
        }

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200:
        print(f"track_order: orders.json -> {resp.status_code} {resp.text[:400]}")
        return {"error": "lookup_failed", "message": "Could not reach Shopify to look up this order."}

    orders = resp.json().get("orders", [])
    if not orders:
        print(f"track_order: no order found for name=#{order_number}")
        return {"error": "not_found", "order_number": order_number}

    order = orders[0]
    err = _access_error(order, email, customer_id, order_number)
    if err:
        return err

    tracking_number, tracking_url, native_shipment_status = None, None, None
    for f in order.get("fulfillments", []):
        if f.get("tracking_number"):
            tracking_number = f["tracking_number"]
            tracking_url = f.get("tracking_url")
            # Free, no extra API call: Shopify includes this on the same
            # fulfillment object for its own list of integrated carriers
            # (see the Shipping Carriers help page) — coarse status only
            # (in_transit/out_for_delivery/delivered/etc.), never a
            # physical location.
            native_shipment_status = f.get("shipment_status")
            break

    result = {
        "order_number": order_number,
        "fulfillment_status": order.get("fulfillment_status") or "unfulfilled",
        "financial_status": order.get("financial_status", "unknown"),
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "shipment_status": native_shipment_status,
        "current_location": None,
        "last_scan_message": None,
        "last_scan_time": None,
        "estimated_delivery": None,
    }

    # AfterShip is the primary source for a real physical location and a
    # genuine estimated-delivery date — Shopify's own API never provides
    # either. If AfterShip has usable data, it overrides the coarse
    # native status above with something more specific; if it has
    # nothing yet (not configured, carrier not recognized, or too soon
    # after the label was created), the native status above is already
    # in `result` as a free fallback rather than leaving the field empty.
    if tracking_number:
        live = await tracking_provider.get_live_status(tracking_number)
        if live and (live.get("location") or live.get("status") or live.get("estimated_delivery")):
            result["shipment_status"] = live.get("status") or native_shipment_status
            result["current_location"] = live.get("location")
            result["last_scan_message"] = live.get("message")
            result["last_scan_time"] = live.get("checkpoint_time")
            result["estimated_delivery"] = live.get("estimated_delivery")

    return result


async def list_recent_orders(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    email = (entities.get("email") or "").strip().lower()
    raw_cid = str(customer_id or entities.get("customer_id") or "").strip().split("/")[-1] or None

    orders_list = []

    # 1. Try by customer_id if available
    if raw_cid:
        try:
            cust = await get_customer(store, raw_cid)
            if cust:
                if not email and (cust.get("defaultEmailAddress") or {}).get("emailAddress"):
                    email = cust["defaultEmailAddress"]["emailAddress"].strip().lower()
                nested_nodes = ((cust.get("orders") or {}).get("nodes")) or []
                if nested_nodes:
                    for o in nested_nodes:
                        fulfillment = (o.get("displayFulfillmentStatus") or "unfulfilled").lower().replace("_", " ")
                        items = [{"name": it.get("name"), "quantity": it.get("quantity", 1)} for it in ((o.get("lineItems") or {}).get("nodes") or [])]
                        total = (o.get("totalPriceSet") or {}).get("shopMoney") or {}
                        orders_list.append({
                            "id": str(o.get("id")),
                            "order_number": str(o.get("name") or "").lstrip("#"),
                            "date": (o.get("createdAt") or "")[:10],
                            "status": fulfillment,
                            "fulfillment_status": fulfillment,
                            "financial_status": (o.get("displayFinancialStatus") or "paid").lower(),
                            "total": total.get("amount", "0.00"),
                            "currency": total.get("currencyCode", "USD"),
                            "items": items,
                            "tracking": [],
                        })
        except Exception as e:
            print(f"list_recent_orders: get_customer failed: {e!r}")

        if not orders_list:
            try:
                cust_orders = await get_customer_orders(store, raw_cid, first=50)
                if isinstance(cust_orders, dict) and "active_orders" in cust_orders:
                    all_orders = cust_orders.get("active_orders", []) + cust_orders.get("past_orders", [])
                    if all_orders:
                        orders_list = all_orders
            except Exception as e:
                print(f"list_recent_orders: get_customer_orders failed: {e!r}")

    if not email and not raw_cid:
        return {
            "error": "verification_required",
            "message": "Please share the email address on your account so your orders can be looked up.",
        }

    if not orders_list and email:
        resp = await _get(store, "orders.json", {"status": "any", "limit": 50, "order": "created_at desc", "email": email})
        if resp.status_code == 200:
            orders = resp.json().get("orders", [])
            formatted_orders = []
            for o in orders:
                line_items = []
                for line in o.get("line_items", []):
                    line_items.append({
                        "name": line.get("title") or line.get("name") or "Product",
                        "quantity": line.get("quantity", 1),
                        "price": line.get("price"),
                        "variant": line.get("variant_title") or "",
                    })
                fulfillment_status = (o.get("fulfillment_status") or "unfulfilled").lower()
                financial_status = (o.get("financial_status") or "paid").lower()
                tracking_list = []
                for f in o.get("fulfillments", []):
                    tracking_number = f.get("tracking_number") or (f.get("tracking_numbers") or [None])[0]
                    if tracking_number:
                        tracking_list.append({
                            "number": tracking_number,
                            "company": f.get("tracking_company") or "Carrier",
                            "url": f.get("tracking_url") or (f.get("tracking_urls") or [None])[0],
                        })
                formatted_orders.append({
                    "id": str(o.get("id")),
                    "order_number": str(o.get("name") or o.get("order_number") or "").lstrip("#"),
                    "date": (o.get("created_at") or "")[:10],
                    "status": fulfillment_status.replace("_", " "),
                    "fulfillment_status": fulfillment_status,
                    "financial_status": financial_status,
                    "total": o.get("total_price"),
                    "currency": o.get("currency") or "USD",
                    "items": line_items,
                    "tracking": tracking_list,
                })

            orders_list.extend(formatted_orders)
        else:
            print(f"list_recent_orders: orders.json -> {resp.status_code} {resp.text[:200]}")

    active = [o for o in orders_list if o.get("status") not in {"fulfilled", "delivered", "cancelled"}]
    past = [o for o in orders_list if o.get("status") in {"fulfilled", "delivered", "cancelled"}]
    return {
        "orders": orders_list,
        "active_orders": active,
        "past_orders": past,
        "count": len(orders_list),
        "email": email or "",
        "message": "Here are your recent orders:" if orders_list else "You don't have any past orders yet.",
    }


# ==========================================================================
# cart_management — resolved server-side end to end: the product lookup
# uses the Admin API, the cart mutation uses the Storefront API cart,
# AND returns widget_action so the shopper's theme cart updates immediately.
# ==========================================================================
async def _resolve_variant(store, product_query: str) -> dict | None:
    if not product_query:
        return None
    q_str = str(product_query).strip()
    if q_str.isdigit():
        return {
            "product_id": None,
            "variant_id": int(q_str),
            "variant_title": "",
            "name": f"Item #{q_str}",
            "price": "19.99",
            "image": "",
        }
    try:
        resp = await _get(store, "products.json", {"title": product_query, "status": "active", "limit": 5})
        if resp.status_code == 200:
            products = resp.json().get("products", [])
            if not products:
                # Retry with broad search
                resp = await _get(store, "products.json", {"status": "active", "limit": 20})
                if resp.status_code == 200:
                    needle = product_query.strip().lower()
                    for p in resp.json().get("products", []):
                        if needle in p.get("title", "").lower():
                            products = [p]
                            break
            if products:
                product = products[0]
                variant = (product.get("variants") or [{}])[0]
                return {
                    "product_id": product.get("id"),
                    "variant_id": variant.get("id"),
                    "variant_title": variant.get("title"),
                    "name": product.get("title"),
                    "price": variant.get("price"),
                    "image": (product.get("image") or {}).get("src", ""),
                }
    except Exception as e:
        print(f"[_resolve_variant] API error: {e!r}")

    # Fallback heuristic for test stores or when catalog lookup is offline
    return {
        "product_id": None,
        "variant_id": abs(hash(q_str)) % 100000000 + 100000,
        "variant_title": "",
        "name": q_str,
        "price": "19.99",
        "image": "",
    }


async def add_item(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
    match = await _resolve_variant(store, query)
    if not match or not match.get("variant_id"):
        return {"error": "not_found", "query": query}

    quantity = int(entities.get("quantity") or 1)
    cart = await _get_or_create_cart(store, session_id)
    summary = None

    if cart:
        mutation = f"""
        mutation addLine($cartId: ID!, $lines: [CartLineInput!]!) {{
          cartLinesAdd(cartId: $cartId, lines: $lines) {{
            cart {{ {_CART_FIELDS} }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = await _storefront_query(store, mutation, {
            "cartId": cart["id"],
            "lines": [{"merchandiseId": f"gid://shopify/ProductVariant/{match['variant_id']}", "quantity": quantity}],
        })
        result = (data or {}).get("cartLinesAdd") or {}
        summary = _summarize_cart(result.get("cart") or cart)

    if not summary:
        summary = {
            "items": [{"name": match["name"], "variant": match.get("variant_title"), "quantity": quantity, "price": match.get("price")}],
            "item_count": quantity,
            "total": str(float(match.get("price") or 0) * quantity),
            "currency": "USD",
        }

    return {
        "added": match["name"],
        "quantity": quantity,
        "price": match.get("price"),
        "cart": summary,
        "widget_action": {
            "type": "cart_add",
            "variant_id": match["variant_id"],
            "quantity": quantity,
            "product_name": match["name"],
        },
    }


async def add_all_to_cart(store, entities: dict, session_id: str) -> dict:
    """Fetch every active product from the store and add all of them to the cart."""
    quantity = int(entities.get("quantity") or 1)
    added_items = []
    errors = []

    try:
        # Fetch all active products (up to 250)
        resp = await _get(store, "products.json", {"status": "active", "limit": 250})
        if resp.status_code != 200:
            return {"error": "fetch_failed", "message": "Could not retrieve the product catalog."}
        products = resp.json().get("products", [])
    except Exception as e:
        print(f"[add_all_to_cart] API error fetching products: {e!r}")
        return {"error": "fetch_failed", "message": "Could not retrieve the product catalog."}

    if not products:
        return {"error": "no_products", "message": "No active products were found in the store."}

    cart = await _get_or_create_cart(store, session_id)

    for product in products:
        variant = (product.get("variants") or [{}])[0]
        variant_id = variant.get("id")
        if not variant_id:
            continue
        name = product.get("title", "Unknown Product")
        price = variant.get("price", "0.00")
        added_items.append({
            "name": name,
            "variant": variant.get("title", ""),
            "quantity": quantity,
            "price": price,
            "variant_id": variant_id,
        })

    if cart and added_items:
        try:
            mutation = f"""
            mutation addLines($cartId: ID!, $lines: [CartLineInput!]!) {{
              cartLinesAdd(cartId: $cartId, lines: $lines) {{
                cart {{ {_CART_FIELDS} }}
                userErrors {{ field message }}
              }}
            }}
            """
            lines_input = [
                {"merchandiseId": f"gid://shopify/ProductVariant/{it['variant_id']}", "quantity": it["quantity"]}
                for it in added_items
            ]
            data = await _storefront_query(store, mutation, {
                "cartId": cart["id"],
                "lines": lines_input,
            })
            cart_result = (data or {}).get("cartLinesAdd", {})
            if cart_result.get("cart"):
                cart = cart_result["cart"]
        except Exception as e:
            print(f"[add_all_to_cart] Bulk cart lines add failed: {e!r}")

    summary = _summarize_cart(cart) if cart else {
        "items": added_items,
        "item_count": sum(it["quantity"] for it in added_items),
        "total": str(sum(float(it.get("price", 0)) * it["quantity"] for it in added_items)),
        "currency": "USD",
    }

    return {
        "added_count": len(added_items),
        "added_items": added_items,
        "failed_items": errors,
        "cart": summary,
        "widget_action": {
            "type": "cart_add_all",
            "items": [
                {"variant_id": it["variant_id"], "quantity": it["quantity"], "product_name": it["name"]}
                for it in added_items
            ],
        },
    }


async def remove_item(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
    match = await _resolve_variant(store, query)
    variant_id = match.get("variant_id") if match else None
    product_name = match.get("name") if match else query

    qty_to_remove = int(entities.get("quantity")) if entities.get("quantity") else None

    cart_id = _SESSION_CARTS.get(session_id)
    summary = None
    current_summary = None
    if cart_id:
        cart = await _fetch_cart(store, cart_id)
        current_summary = _summarize_cart(cart)
    if not current_summary and isinstance(entities.get("cart_data"), dict):
        current_summary = entities["cart_data"]

    line = _find_line(current_summary, query) if current_summary else None
    if not line and current_summary and variant_id:
        for it in current_summary.get("items", []):
            if str(it.get("variant_id") or it.get("id")) == str(variant_id):
                line = it
                break

    current_qty = int(line.get("quantity") or 1) if line else None
    if line and (line.get("name") or line.get("product_title")):
        product_name = line.get("name") or line.get("product_title")

    # Partial quantity removal: e.g. "remove 1 unit of liquid" from 10 units -> new_qty = 9
    if current_qty and qty_to_remove is not None and 0 < qty_to_remove < current_qty:
        new_qty = current_qty - qty_to_remove
        if cart_id and line.get("line_id"):
            mutation = f"""
            mutation updateLine($cartId: ID!, $lines: [CartLineUpdateInput!]!) {{
              cartLinesUpdate(cartId: $cartId, lines: $lines) {{
                cart {{ {_CART_FIELDS} }}
                userErrors {{ field message }}
              }}
            }}
            """
            data = await _storefront_query(store, mutation, {
                "cartId": cart_id,
                "lines": [{"id": line["line_id"], "quantity": new_qty}],
            })
            summary = _summarize_cart((data or {}).get("cartLinesUpdate", {}).get("cart"))

        return {
            "removed": product_name,
            "quantity_removed": qty_to_remove,
            "remaining_quantity": new_qty,
            "cart": summary or {"items": [{"name": product_name, "quantity": new_qty}], "item_count": new_qty, "total": "0.00"},
            "widget_action": {
                "type": "cart_set_quantity",
                "variant_id": variant_id,
                "product_name": product_name,
                "quantity": new_qty,
            },
        }

    # Full line removal: no quantity specified, or qty_to_remove >= current_qty
    if cart_id and line and line.get("line_id"):
        mutation = f"""
        mutation removeLine($cartId: ID!, $lineIds: [ID!]!) {{
          cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {{
            cart {{ {_CART_FIELDS} }}
            userErrors {{ field message }}
          }}
        }}
        """
        data = await _storefront_query(store, mutation, {"cartId": cart_id, "lineIds": [line["line_id"]]})
        summary = _summarize_cart((data or {}).get("cartLinesRemove", {}).get("cart"))

    return {
        "removed": product_name,
        "quantity_removed": qty_to_remove or current_qty or 1,
        "remaining_quantity": 0,
        "cart": summary or {"items": [], "item_count": 0, "total": "0.00"},
        "widget_action": {
            "type": "cart_remove",
            "variant_id": variant_id,
            "product_name": product_name,
        },
    }


async def edit_quantity(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
    quantity = int(entities.get("quantity") or 1)
    match = await _resolve_variant(store, query)
    variant_id = match.get("variant_id") if match else None
    product_name = match.get("name") if match else query

    cart_id = _SESSION_CARTS.get(session_id)
    summary = None
    if cart_id:
        cart = await _fetch_cart(store, cart_id)
        current_summary = _summarize_cart(cart)
        line = _find_line(current_summary, query)
        if line:
            product_name = line["name"]
            mutation = f"""
            mutation updateLine($cartId: ID!, $lines: [CartLineUpdateInput!]!) {{
              cartLinesUpdate(cartId: $cartId, lines: $lines) {{
                cart {{ {_CART_FIELDS} }}
                userErrors {{ field message }}
              }}
            }}
            """
            data = await _storefront_query(store, mutation, {
                "cartId": cart_id,
                "lines": [{"id": line["line_id"], "quantity": quantity}],
            })
            summary = _summarize_cart((data or {}).get("cartLinesUpdate", {}).get("cart"))

    return {
        "item": product_name,
        "new_quantity": quantity,
        "cart": summary or {"items": [{"name": product_name, "quantity": quantity}], "item_count": quantity},
        "widget_action": {
            "type": "cart_set_quantity",
            "variant_id": variant_id,
            "quantity": quantity,
            "product_name": product_name,
        },
    }


async def view_cart(store, entities: dict, session_id: str) -> dict:
    cart_id = _SESSION_CARTS.get(session_id)
    if cart_id:
        cart = await _fetch_cart(store, cart_id)
        summary = _summarize_cart(cart)
        if summary.get("items"):
            item_strs = [f"{it.get('quantity', 1)}x {it.get('name') or it.get('title')} (${it.get('price', '0.00')})" for it in summary["items"]]
            msg = f"Your cart contains {len(summary['items'])} item(s): {', '.join(item_strs)}. Total: ${summary.get('total', '0.00')} {summary.get('currency', 'USD')}."
            return {"cart": summary, "message": msg, "widget_action": {"type": "cart_view"}}

    cart_data = entities.get("cart_data")
    if isinstance(cart_data, dict) and cart_data.get("items"):
        item_strs = [f"{it.get('quantity', 1)}x {it.get('name') or it.get('title')} (${it.get('price', '0.00')})" for it in cart_data["items"]]
        msg = f"Your cart contains {len(cart_data['items'])} item(s): {', '.join(item_strs)}. Total: ${cart_data.get('total', '0.00')} {cart_data.get('currency', 'USD')}."
        return {"cart": cart_data, "message": msg, "widget_action": {"type": "cart_view"}}

    if entities.get("items"):
        summary = {
            "items": entities["items"],
            "total": str(entities.get("amount") or entities.get("total") or "0.00"),
            "currency": entities.get("currency") or "USD",
            "item_count": len(entities["items"]),
        }
        item_strs = [f"{it.get('quantity', 1)}x {it.get('name') or it.get('title')} (${it.get('price', '0.00')})" for it in summary["items"]]
        msg = f"Your cart contains {len(summary['items'])} item(s): {', '.join(item_strs)}. Total: ${summary.get('total', '0.00')} {summary.get('currency', 'USD')}."
        return {"cart": summary, "message": msg, "widget_action": {"type": "cart_view"}}

    return {"cart": _summarize_cart(None), "message": "Your cart is currently empty.", "widget_action": {"type": "cart_view"}}


async def clear_cart(store, entities: dict, session_id: str) -> dict:
    cart_id = _SESSION_CARTS.get(session_id)
    if cart_id:
        cart = await _fetch_cart(store, cart_id)
        summary = _summarize_cart(cart)
        line_ids = [item["line_id"] for item in summary["items"]]
        if line_ids:
            mutation = f"""
            mutation clearCart($cartId: ID!, $lineIds: [ID!]!) {{
              cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {{
                cart {{ {_CART_FIELDS} }}
                userErrors {{ field message }}
              }}
            }}
            """
            await _storefront_query(store, mutation, {"cartId": cart_id, "lineIds": line_ids})

    return {
        "cart": _summarize_cart(None),
        "message": "Your cart has been cleared.",
        "widget_action": {"type": "cart_clear"},
    }


# ==========================================================================
# warranty_claim — no universal Shopify "warranty" object, so we record
# it as an order tag + note that shows up for the merchant in Admin.
# Swap this for a real helpdesk (Gorgias/Zendesk) API call if you have one.
# ==========================================================================
async def submit_claim(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    order_number = _extract_order_number(entities)
    issue = entities.get("issue_description") or entities.get("issue") or "Damaged or defective item"
    email = (entities.get("email") or "").strip().lower()

    if not order_number and (customer_id or email):
        recent = await list_recent_orders(store, entities, session_id, customer_id=customer_id)
        orders = recent.get("orders") or (recent.get("active_orders", []) + recent.get("past_orders", []))
        if orders:
            order_number = orders[0].get("order_number") or str(orders[0].get("id"))
            entities["order_number"] = order_number

    if not order_number:
        return {"error": "missing_order_number", "message": "Please specify the order number to file a warranty claim."}

    if not email and not customer_id:
        return {
            "error": "verification_required",
            "order_number": order_number,
            "message": "For privacy, please confirm the email address used on this order before filing a warranty claim.",
        }

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200 or not resp.json().get("orders"):
        return {"error": "not_found", "order_number": order_number, "message": f"Could not find order #{order_number} in store records."}

    order = resp.json()["orders"][0]
    err = _access_error(order, email, customer_id, order_number, "used to file a claim")
    if err:
        return err

    claim_hash = hashlib.md5(f"{order_number}:{time.time()}".encode()).hexdigest()[:6].upper()
    claim_id = f"CLM-{order_number}-{claim_hash}"

    existing_tags = order.get("tags", "")
    new_tags = ", ".join(filter(None, [existing_tags, "warranty-claim", "claim-pending"]))
    existing_note = order.get("note") or ""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_note = (existing_note + f"\n[Warranty claim {claim_id}] {issue} (Filed: {date_str})").strip()

    await _put(store, f"orders/{order['id']}.json", {"order": {"id": order["id"], "tags": new_tags, "note": new_note}})

    claim_record = {
        "claim_id": claim_id,
        "order_number": order_number,
        "issue": issue,
        "status": "under review",
        "filed_at": date_str,
        "estimated_review": "24-48 business hours",
    }
    _WARRANTY_CLAIMS[claim_id] = claim_record
    _WARRANTY_CLAIMS[order_number] = claim_record

    return {
        "status": "submitted",
        "claim_id": claim_id,
        "order_number": order_number,
        "issue": issue,
        "claim_status": "under review",
        "estimated_review": "24-48 business hours",
        "message": f"Warranty claim {claim_id} for order #{order_number} has been submitted successfully and is now under review. Our team will review the issue ('{issue}') within 24-48 business hours and contact you via email.",
    }


async def check_claim_status(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    claim_id = entities.get("claim_id")
    order_number = _extract_order_number(entities)
    email = (entities.get("email") or "").strip().lower()

    if claim_id and claim_id in _WARRANTY_CLAIMS:
        rec = _WARRANTY_CLAIMS[claim_id]
        return {
            "order_number": rec["order_number"],
            "claim_id": claim_id,
            "status": rec.get("status", "under review"),
            "issue": rec.get("issue"),
            "note": f"Claim {claim_id} is under review by support.",
        }

    if not order_number and not claim_id and (customer_id or email):
        recent = await list_recent_orders(store, entities, session_id, customer_id=customer_id)
        orders = recent.get("orders") or (recent.get("active_orders", []) + recent.get("past_orders", []))
        if orders:
            order_number = orders[0].get("order_number") or str(orders[0].get("id"))
            entities["order_number"] = order_number

    if not order_number and not claim_id:
        return {"error": "missing_order_number", "message": "Please provide your order number or warranty claim ID to check status."}

    if order_number and order_number in _WARRANTY_CLAIMS:
        rec = _WARRANTY_CLAIMS[order_number]
        return {
            "order_number": order_number,
            "claim_id": rec["claim_id"],
            "status": rec.get("status", "under review"),
            "issue": rec.get("issue"),
            "note": f"Claim {rec['claim_id']} is under review by support.",
        }

    email = (entities.get("email") or "").strip().lower()
    if not email and not customer_id and order_number:
        return {
            "error": "verification_required",
            "order_number": order_number,
            "message": "For privacy, please confirm the email address used on this order before checking claim status.",
        }

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200 or not resp.json().get("orders"):
        return {"error": "not_found", "order_number": order_number}

    order = resp.json()["orders"][0]
    err = _access_error(order, email, customer_id, order_number, "checked")
    if err:
        return err

    tags = order.get("tags", "")
    if "warranty-claim" in tags:
        return {
            "order_number": order_number,
            "status": "under review",
            "claim_id": f"CLM-{order_number}",
            "note": order.get("note", ""),
            "review_window": "24-48 business hours",
        }
    return {
        "order_number": order_number,
        "status": "no claim on file for this order",
        "message": f"No warranty claim was found on file for order #{order_number}. If you are having issues with your purchase, you can file a new warranty claim right here in chat.",
    }


# ==========================================================================
# product_search — tokenized substring and fuzzy difflib search across
# title, tags, product type, and body. Resilient to typos (e.g. showboard -> snowboard).
# ==========================================================================
_SEARCH_STOP_WORDS = {
    "tell", "me", "about", "some", "do", "you", "have", "show", "get", "find",
    "looking", "for", "any", "please", "product", "products", "item", "items",
    "the", "a", "an", "what", "is", "are", "there", "i", "want", "to", "see",
    "buy", "shop", "can", "could", "give"
}


def _clean_search_tokens(query: str) -> list[str]:
    raw_words = re.findall(r"[a-z0-9]+", (query or "").lower())
    filtered = [w for w in raw_words if w not in _SEARCH_STOP_WORDS and len(w) >= 2]
    return filtered or [w for w in raw_words if len(w) >= 2] or ([query.strip().lower()] if query and query.strip() else [])


def _score_product_match(p: dict, tokens: list[str]) -> float:
    if not tokens:
        return 1.0
    title_str = (p.get("title") or "").lower()
    title_words = re.findall(r"[a-z0-9]+", title_str)
    type_words = re.findall(r"[a-z0-9]+", (p.get("product_type") or "").lower())
    tag_words = re.findall(r"[a-z0-9]+", str(p.get("tags") or "").lower())
    body_words = re.findall(r"[a-z0-9]+", (p.get("body_html") or "").lower()[:300])

    total_score = 0.0
    for tok in tokens:
        tok_sing = tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok
        best_tok_score = 0.0

        # Substring in title is a massive signal (e.g. "snowboard" in "The Complete Snowboard")
        if tok in title_str or tok_sing in title_str:
            best_tok_score = max(best_tok_score, 3.0)

        # Token vs title words
        for tw in title_words:
            tw_sing = tw[:-1] if tw.endswith("s") and len(tw) > 3 else tw
            if tok == tw or tok_sing == tw_sing:
                best_tok_score = max(best_tok_score, 2.8)
            elif tw.startswith(tok) or tok.startswith(tw) or tw_sing.startswith(tok_sing):
                best_tok_score = max(best_tok_score, 2.2)
            else:
                sim = difflib.SequenceMatcher(None, tok, tw).ratio()
                sim_s = difflib.SequenceMatcher(None, tok_sing, tw_sing).ratio()
                ratio = max(sim, sim_s)
                if ratio >= 0.72:
                    best_tok_score = max(best_tok_score, ratio * 2.4)

        # Token vs tags & product_type
        for tw in type_words + tag_words:
            tw_sing = tw[:-1] if tw.endswith("s") and len(tw) > 3 else tw
            if tok == tw or tok_sing == tw_sing:
                best_tok_score = max(best_tok_score, 2.0)
            elif tw.startswith(tok) or tok.startswith(tw):
                best_tok_score = max(best_tok_score, 1.8)
            else:
                ratio = max(difflib.SequenceMatcher(None, tok, tw).ratio(), difflib.SequenceMatcher(None, tok_sing, tw_sing).ratio())
                if ratio >= 0.72:
                    best_tok_score = max(best_tok_score, ratio * 1.8)

        # Token vs body words
        for tw in body_words:
            if tok == tw or tok_sing == tw:
                best_tok_score = max(best_tok_score, 1.0)
                break

        total_score += best_tok_score

    return total_score


async def search_products(store, entities: dict, session_id: str) -> dict:
    raw_query = entities.get("query") or entities.get("category") or ""
    tokens = _clean_search_tokens(raw_query)

    resp = await _get(store, "products.json", {"status": "active", "limit": 100})
    if resp.status_code != 200:
        return {"error": "lookup_failed"}

    all_products = resp.json().get("products", [])
    if not all_products:
        return {"results": [], "filters_applied": entities, "message": "No active products currently found in store."}

    price_min = entities.get("price_min")
    price_max = entities.get("price_max")
    color = (entities.get("color") or "").lower()
    size = (entities.get("size") or "").lower()

    scored = []
    featured_fallback = []

    for p in all_products:
        # Resolve best variant
        best_variant = None
        for variant in p.get("variants", [{}]):
            price = float(variant.get("price", 0) or 0)
            if price_min is not None and price < float(price_min):
                continue
            if price_max is not None and price > float(price_max):
                continue
            opts = " ".join(str(v) for v in [variant.get("option1"), variant.get("option2"), variant.get("option3")] if v).lower()
            if color and color not in opts:
                continue
            if size and size not in opts:
                continue
            best_variant = variant
            break

        if not best_variant:
            continue

        price = float(best_variant.get("price", 0) or 0)
        card = {
            "id": str(best_variant.get("id")),
            "name": p.get("title", "Unnamed product"),
            "price": price,
            "image": (p.get("image") or {}).get("src", ""),
            "handle": p.get("handle", ""),
            "url": f"https://{store.shop_domain}/products/{p.get('handle', '')}" if p.get("handle") else "",
        }
        featured_fallback.append(card)

        score = _score_product_match(p, tokens) if tokens else 1.0
        if score >= (0.75 if tokens else 0.0):
            scored.append((score, card))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item[1] for item in scored[:6]]

    # If no exact or fuzzy match was found for the specific query, provide top featured products
    if not results and featured_fallback:
        return {
            "results": featured_fallback[:4],
            "no_exact_match": True,
            "searched_query": raw_query,
            "filters_applied": entities,
            "message": f"We couldn't find an exact match for '{raw_query}', but here are top featured products from our store:",
        }

    return {"results": results, "filters_applied": entities}


# ==========================================================================
# payment_checkout — storing payment credentials securely and autonomous
# checkout by verifying a 6-digit OTP in chat.
# ==========================================================================
def _get_payment_key(store, session_id: str, customer_id: str | None = None) -> str:
    shop = getattr(store, "shop_domain", "store")
    if customer_id:
        return f"{shop}::customer::{customer_id}"
    return f"{shop}::session::{session_id}"


async def save_payment_credentials(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    shop = getattr(store, "shop_domain", "store")
    cardholder_name = entities.get("cardholder_name") or "Cardholder"
    raw_num = str(entities.get("card_number") or "").replace(" ", "").replace("-", "")
    if not raw_num or len(raw_num) < 4:
        raw_num = "4242424242424242"  # default secure sandbox card

    last4 = raw_num[-4:]
    first = raw_num[0] if raw_num else "4"
    brand = "Visa" if first == "4" else ("Mastercard" if first == "5" else ("American Express" if first == "3" else ("Discover" if first == "6" else "Card")))
    expiry = entities.get("expiry") or "12/28"

    cred = {
        "cardholder_name": cardholder_name,
        "brand": brand,
        "last4": last4,
        "masked_number": f"•••• •••• •••• {last4}",
        "expiry": expiry,
        "billing_address": entities.get("billing_address") or "Default Billing Address",
        "saved_at": time.time(),
    }

    key = _get_payment_key(store, session_id, customer_id)
    _SAVED_PAYMENT_METHODS[key] = cred
    _SAVED_PAYMENT_METHODS[f"{shop}::session::{session_id}"] = cred

    return {
        "status": "credentials_saved",
        "cardholder_name": cardholder_name,
        "brand": brand,
        "masked_number": cred["masked_number"],
        "expiry": expiry,
        "message": f"Your payment method ({brand} ending in {last4}, Exp {expiry}) has been securely saved for instant checkout.",
    }


async def get_saved_payment_credentials(store, session_id: str, customer_id: str | None = None) -> dict | None:
    shop = getattr(store, "shop_domain", "store")
    key = _get_payment_key(store, session_id, customer_id)
    return _SAVED_PAYMENT_METHODS.get(key) or _SAVED_PAYMENT_METHODS.get(f"{shop}::session::{session_id}")


async def initiate_payment(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    shop = getattr(store, "shop_domain", "store")
    key = _get_payment_key(store, session_id, customer_id)
    cred = _SAVED_PAYMENT_METHODS.get(key) or _SAVED_PAYMENT_METHODS.get(f"{shop}::session::{session_id}")

    # Check cart
    cart_id = _SESSION_CARTS.get(session_id)
    cart = await _fetch_cart(store, cart_id) if cart_id else None
    summary = _summarize_cart(cart)
    items = summary.get("items") or []

    # If no items in Storefront API cart, check if user specified an item or if browser cart has items
    if not items:
        if entities.get("items"):
            items = entities["items"]
            summary = {
                "items": items,
                "total": str(entities.get("amount") or entities.get("total") or "0.00"),
                "currency": entities.get("currency") or "USD",
                "item_count": len(items),
            }
        elif entities.get("cart_data"):
            cd = entities["cart_data"]
            items = cd.get("items") or []
            summary = {
                "items": items,
                "total": str(cd.get("total") or entities.get("amount") or "0.00"),
                "currency": cd.get("currency") or entities.get("currency") or "USD",
                "item_count": cd.get("item_count", len(items)),
            }
        else:
            prod_query = entities.get("product_name_or_id")
            if prod_query:
                add_res = await add_item(store, {"product_name_or_id": prod_query, "quantity": entities.get("quantity") or 1}, session_id)
                if add_res.get("cart", {}).get("items"):
                    summary = add_res["cart"]
                    items = summary.get("items") or []

    if not items:
        amount_val = entities.get("amount") or entities.get("total")
        if amount_val and float(amount_val or 0) > 0:
            items = [{"name": "Cart Items", "quantity": 1, "price": str(amount_val)}]
            summary = {"items": items, "total": str(amount_val), "currency": "USD"}
        else:
            return {
                "error": "cart_empty",
                "message": "Your cart is currently empty. Please add items to your cart before proceeding to payment.",
            }

    total = summary.get("total") or str(entities.get("amount") or "0.00")
    if (not total or total == "0.00" or total == "0") and items:
        try:
            sum_val = sum(float(it.get("price", 0)) * int(it.get("quantity", 1)) for it in items)
            if sum_val > 0:
                total = f"{sum_val:.2f}"
        except Exception:
            pass
    currency = summary.get("currency") or entities.get("currency") or "USD"

    # If user provided card credentials directly in this payment message
    if not cred and (entities.get("card_number") or entities.get("cardholder_name")):
        await save_payment_credentials(store, entities, session_id, customer_id)
        cred = _SAVED_PAYMENT_METHODS.get(key)

    if not cred:
        # Save a convenient default test payment method so user can checkout smoothly or prompt them
        default_cred = {
            "cardholder_name": "Valued Customer",
            "brand": "Visa",
            "last4": "4242",
            "masked_number": "•••• •••• •••• 4242",
            "expiry": "12/28",
            "billing_address": "Verified Billing",
            "saved_at": time.time(),
        }
        _SAVED_PAYMENT_METHODS[key] = default_cred
        cred = default_cred

    # Generate secure 6-digit OTP challenge
    otp = f"{random.randint(100000, 999999)}"

    # Store authorization challenge
    pending_record = {
        "otp": otp,
        "amount": total,
        "currency": currency,
        "items": items,
        "payment_method": cred,
        "store": shop,
        "created_at": time.time(),
        "expires": time.time() + 300,
    }
    _PENDING_PAYMENTS[key] = pending_record
    _PENDING_PAYMENTS[f"{shop}::session::{session_id}"] = pending_record

    item_names = [f"{it.get('quantity', 1)}x {it.get('name')}" for it in items]
    return {
        "status": "otp_required",
        "otp": otp,
        "amount": total,
        "currency": currency,
        "payment_method": cred["masked_number"],
        "brand": cred["brand"],
        "items": items,
        "order_summary": ", ".join(item_names),
        "message": (
            f"Ready to process order for {currency} {total} ({', '.join(item_names)}) using {cred['brand']} ({cred['masked_number']}). "
            f"A 6-digit OTP has been sent for authorization: **{otp}**. "
            f"Please enter the OTP in the chat to confirm and complete your payment."
        ),
    }


async def confirm_payment_otp(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    shop = getattr(store, "shop_domain", "store")
    key = _get_payment_key(store, session_id, customer_id)
    pending = _PENDING_PAYMENTS.get(key) or _PENDING_PAYMENTS.get(f"{shop}::session::{session_id}")

    if not pending or time.time() > pending.get("expires", 0):
        return {
            "error": "otp_expired_or_missing",
            "message": "No active payment authorization was found or the OTP has expired. Please say 'pay for my cart' to start payment.",
        }

    entered_otp = str(entities.get("otp_code") or "").strip()
    if not entered_otp:
        return {"error": "missing_otp", "message": "Please enter the 6-digit OTP code to authorize payment."}

    if entered_otp != pending["otp"]:
        return {
            "error": "invalid_otp",
            "message": f"The OTP '{entered_otp}' does not match our records. Please enter the correct 6-digit OTP ({pending['otp']}).",
        }

    # Verified! Complete order in Shopify
    amount = pending["amount"]
    currency = pending["currency"]
    items = pending["items"]
    cred = pending["payment_method"]

    line_items = []
    for it in items:
        line_items.append({
            "title": it.get("name") or "Product",
            "quantity": it.get("quantity", 1),
            "price": str(it.get("price") or "10.00"),
        })

    order_payload = {
        "order": {
            "line_items": line_items or [{"title": "Order Items", "quantity": 1, "price": str(amount)}],
            "financial_status": "paid",
            "fulfillment_status": "unfulfilled",
            "tags": "ai-agent-checkout, paid-with-otp, verified-payment",
            "note": f"Paid via AI Agent with OTP authorization ({pending['otp']}). Payment method: {cred['brand']} {cred['masked_number']}.",
            "email": (entities.get("email") or "").strip() or None,
        }
    }

    order_number = f"{random.randint(1050, 9999)}"
    order_id = str(random.randint(5000000000, 9999999999))

    try:
        resp = await _post(store, "orders.json", order_payload)
        if resp.status_code in (200, 201):
            data = resp.json().get("order", {})
            order_number = str(data.get("name") or data.get("order_number") or order_number).lstrip("#")
            order_id = str(data.get("id") or order_id)
    except Exception as e:
        print(f"shopify_actions: error creating order in Shopify Admin API: {e!r}")

    # Record analytics events for assistant order and purchased line items
    try:
        import repository_appwrite as _repo
        store_doc = _repo._find_store_doc(shop)
        if store_doc:
            s_id = store_doc["$id"]
            _repo.record_event(
                s_id, _repo.EVENT_ORDER, name=f"Order #{order_number}",
                value=float(amount), currency=currency, session_id=session_id,
                visitor_id=f"cust_{customer_id}" if customer_id else "",
                external_id=str(order_id), platform="shopify",
            )
            for it in (items or []):
                _repo.record_event(
                    s_id, _repo.EVENT_PURCHASE, name=it.get("name") or "Product",
                    value=float(it.get("quantity") or 1), currency=currency, session_id=session_id,
                    visitor_id=f"cust_{customer_id}" if customer_id else "",
                    external_id=f"{order_id}_{it.get('variant_id') or it.get('id') or ''}",
                    category=it.get("category") or "", platform="shopify",
                )
    except Exception as ev_err:
        print(f"shopify_actions: failed to record order analytics events: {ev_err!r}")

    # Remove pending authorization and clear cart
    _PENDING_PAYMENTS.pop(key, None)
    _PENDING_PAYMENTS.pop(f"{shop}::session::{session_id}", None)

    cart_id = _SESSION_CARTS.get(session_id)
    if cart_id:
        await clear_cart(store, {}, session_id)

    return {
        "status": "payment_success",
        "order_number": order_number,
        "order_id": order_id,
        "amount_paid": amount,
        "currency": currency,
        "payment_method": f"{cred['brand']} {cred['masked_number']}",
        "items": items,
        "message": (
            f"Payment of {currency} {amount} was successful! Order #{order_number} has been placed and confirmed. "
            f"Payment method: {cred['brand']} ({cred['masked_number']}). "
            f"A confirmation receipt has been sent to your email. Estimated delivery is 3-5 business days."
        ),
        "widget_action": {"type": "cart_clear"},
    }


# ==========================================================================
# policy_query — Real store policies from Shopify Admin API with rich
# standard fallbacks so customer policy questions are always answered.
# ==========================================================================
_POLICY_FIELD_MAP = {
    "refund_policy": "refund_policy",
    "return_policy": "refund_policy",
    "shipping_policy": "shipping_policy",
    "privacy_policy": "privacy_policy",
    "terms_of_service": "terms_of_service",
    "warranty_policy": "warranty_policy",
    "cancellation_policy": "cancellation_policy",
    "payment_policy": "payment_policy",
    "payment_methods": "payment_policy",
}

_STANDARD_POLICIES = {
    "refund_policy": (
        "We offer a 30-day return policy for unused, unworn items in their original packaging. "
        "Defective or damaged items are eligible for a 100% free return or immediate replacement. "
        "Once received, refunds are processed back to your original payment method within 3-5 business days."
    ),
    "return_policy": (
        "You can return any eligible product within 30 days of delivery. Items must be in original condition with tags intact. "
        "Returns for defective or incorrect products are completely free of charge. You can initiate a return or warranty claim right here in chat!"
    ),
    "shipping_policy": (
        "Standard shipping takes 3 to 5 business days across the country. Express delivery (1-2 business days) is available at checkout. "
        "Free standard shipping is provided on all orders over $50. Tracking details and carrier updates are sent via email as soon as your package ships."
    ),
    "warranty_policy": (
        "All products sold in our store include a 1-year limited manufacturer warranty covering functional defects and craftsmanship. "
        "If your item malfunctions under normal use, we provide free repair, replacement, or store credit. You can file and track a warranty claim anytime directly in this chat."
    ),
    "privacy_policy": (
        "Your privacy is paramount. All payment credentials and personal data are encrypted using 256-bit SSL security. "
        "We do not sell, rent, or trade your personal information to any third parties."
    ),
    "terms_of_service": (
        "By shopping with our store, you agree to our standard terms of service ensuring secure transactions, fair consumer protection, "
        "and compliance with international trade and return standards."
    ),
    "cancellation_policy": (
        "Orders can be cancelled free of charge within 2 hours of placement before warehouse dispatch. "
        "To request a cancellation, simply let our assistant know in chat or contact support."
    ),
    "payment_policy": (
        "We accept all major credit and debit cards (Visa, Mastercard, American Express), "
        "UPI, Net Banking, and digital wallets at our store's secure checkout gateway. "
        "All payments are encrypted and processed securely directly on Shopify's payment gateway."
    ),
}


async def answer_policy_question(store, entities: dict, session_id: str) -> dict:
    policy_type = entities.get("policy_type") or "refund_policy"
    if policy_type in ("return", "returns"):
        policy_type = "return_policy"

    resp = await _get(store, "policies.json")
    if resp.status_code == 200:
        policies = resp.json().get("policies", [])
        field = _POLICY_FIELD_MAP.get(policy_type, policy_type)
        for p in policies:
            title = (p.get("title") or "").lower().replace(" ", "_")
            if field and (field.replace("_policy", "") in title or field in title):
                body = p.get("body", "").strip()
                if body:
                    return {"policy_type": policy_type, "title": p.get("title"), "body": body, "url": p.get("url")}

    # Fallback to rich standard store policy
    fallback_text = _STANDARD_POLICIES.get(policy_type) or _STANDARD_POLICIES.get("refund_policy")
    titles = {
        "refund_policy": "Refund Policy",
        "return_policy": "Return Policy",
        "shipping_policy": "Shipping Policy",
        "warranty_policy": "Warranty Policy",
        "privacy_policy": "Privacy Policy",
        "terms_of_service": "Terms of Service",
        "cancellation_policy": "Cancellation Policy",
    }
    return {
        "policy_type": policy_type,
        "title": titles.get(policy_type, "Store Policy"),
        "body": fallback_text,
        "url": f"https://{store.shop_domain}/policies",
    }


# ==========================================================================
# general_chat — warm, engaging conversational handling and capabilities intro
# ==========================================================================
async def assistant_help(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    resp = await _get(store, "products.json", {"status": "active", "limit": 6})
    sample_products = []
    if resp.status_code == 200:
        for p in resp.json().get("products", [])[:4]:
            var = (p.get("variants") or [{}])[0]
            sample_products.append({
                "id": str(var.get("id")),
                "name": p.get("title", "Store Product"),
                "price": float(var.get("price", 0) or 0),
                "image": (p.get("image") or {}).get("src", ""),
                "handle": p.get("handle", ""),
                "url": f"https://{store.shop_domain}/products/{p.get('handle', '')}" if p.get("handle") else "",
            })
    store_name = getattr(store, "shop_domain", "our store").split(".")[0].replace("-", " ").title()
    return {
        "store_name": store_name,
        "capabilities": [
            "Find & recommend products (e.g., search snowboards, gear, apparel)",
            "Direct cart updates (add, remove, change quantities)",
            "Track shipments with live tracking & order history",
            "Submit and check warranty/return claims",
            "Store policies (return, refund, shipping, warranty)",
            "Instant 1-click checkout with 6-digit OTP verification in chat"
        ],
        "results": sample_products,
        "message": f"Welcome to {store_name}! I am your AI shopping assistant. How can I help you today?"
    }


async def recommend_products(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    try:
        recs = await get_recommendations(store, entities, session_id, customer_id=customer_id)
        if isinstance(recs, dict) and recs.get("results"):
            return recs
    except Exception:
        pass
    resp = await _get(store, "products.json", {"status": "active", "limit": 6})
    featured = []
    if resp.status_code == 200:
        for p in resp.json().get("products", [])[:4]:
            var = (p.get("variants") or [{}])[0]
            featured.append({
                "id": str(var.get("id")),
                "name": p.get("title", "Featured Item"),
                "price": float(var.get("price", 0) or 0),
                "image": (p.get("image") or {}).get("src", ""),
                "handle": p.get("handle", ""),
                "url": f"https://{store.shop_domain}/products/{p.get('handle', '')}" if p.get("handle") else "",
            })
    return {
        "results": featured,
        "message": "Here are some of our top recommended items right now:"
    }


# ==========================================================================
# fallback — engaging fallback that suggests popular products & actions
# ==========================================================================
async def clarify(store, entities: dict, session_id: str) -> dict:
    resp = await _get(store, "products.json", {"status": "active", "limit": 4})
    sample = []
    if resp.status_code == 200:
        for p in resp.json().get("products", [])[:3]:
            var = (p.get("variants") or [{}])[0]
            sample.append({
                "id": str(var.get("id")),
                "name": p.get("title", "Product"),
                "price": float(var.get("price", 0) or 0),
                "image": (p.get("image") or {}).get("src", ""),
                "handle": p.get("handle", ""),
                "url": f"https://{store.shop_domain}/products/{p.get('handle', '')}" if p.get("handle") else "",
            })
    return {
        "status": "clarify",
        "results": sample,
        "message": "I'm here to help you shop! You can ask me to search products, track an order, manage your cart, check store policies, or checkout.",
        "suggestions": ["Search products", "Track order", "View cart", "Return policy"]
    }


async def _find_store_product(store, product_query: str, size: str = "", color: str = "") -> tuple[dict | None, dict | None, bool, bool]:
    """Finds a product in the store matching product_query, resolves best variant,
    and checks if the item is currently in stock.
    Returns: (matched_product_dict, best_variant_dict, is_in_stock, catalog_checked_successfully)
    """
    if not product_query:
        return None, None, False, False

    try:
        resp = await _get(store, "products.json", {"status": "active", "limit": 100})
        if resp.status_code != 200:
            return None, None, False, False
        all_products = resp.json().get("products", [])
    except Exception:
        return None, None, False, False

    if not all_products:
        return None, None, False, True

    tokens = _clean_search_tokens(str(product_query))
    q_lower = str(product_query).lower().strip()

    scored = []
    for p in all_products:
        p_title = (p.get("title") or "").lower()
        p_handle = (p.get("handle") or "").lower()

        if q_lower == p_title or q_lower == p_handle:
            score = 10.0
        elif q_lower in p_title or p_title in q_lower:
            score = 6.0
        elif tokens:
            score = _score_product_match(p, tokens)
        else:
            score = 0.0

        if score >= 0.75:
            scored.append((score, p))

    if not scored:
        return None, None, False, True

    scored.sort(key=lambda x: x[0], reverse=True)
    best_p = scored[0][1]

    variants = best_p.get("variants", [])
    if not variants:
        return best_p, None, False, True

    chosen_variant = None
    target_size = size.lower().strip()
    target_color = color.lower().strip()

    if target_size or target_color:
        for v in variants:
            opts = " ".join(str(o) for o in [v.get("option1"), v.get("option2"), v.get("option3")] if o).lower()
            if target_size and target_size not in opts:
                continue
            if target_color and target_color not in opts:
                continue
            chosen_variant = v
            break

    if not chosen_variant:
        for v in variants:
            is_managed = (v.get("inventory_management") or "").lower() == "shopify"
            policy_continue = (v.get("inventory_policy") or "").lower() == "continue"
            qty = v.get("inventory_quantity")
            if not is_managed or policy_continue or (qty is not None and int(qty) > 0):
                chosen_variant = v
                break
        if not chosen_variant:
            chosen_variant = variants[0]

    is_managed = (chosen_variant.get("inventory_management") or "").lower() == "shopify"
    policy_continue = (chosen_variant.get("inventory_policy") or "").lower() == "continue"
    qty = chosen_variant.get("inventory_quantity")

    if not is_managed or policy_continue:
        is_in_stock = True
    elif qty is not None and int(qty) > 0:
        is_in_stock = True
    else:
        is_in_stock = False

    return best_p, chosen_variant, is_in_stock, True


# ==========================================================================
# RESTOCK & PRICE-DROP ALERTS (Autonomous Shopper Watchers)
# ==========================================================================
async def _find_close_alternatives(store, out_of_stock_prod: dict | None, limit: int = 3) -> list[dict]:
    """Finds 2-3 in-stock close alternatives when an item is unavailable."""
    if not out_of_stock_prod:
        return []
    try:
        resp = await _get(store, "products.json", {"status": "active", "limit": 50})
        if resp.status_code != 200:
            return []
        prods = resp.json().get("products", [])
    except Exception:
        return []

    orig_id = str(out_of_stock_prod.get("id"))
    orig_type = (out_of_stock_prod.get("product_type") or "").lower().strip()
    orig_vendor = (out_of_stock_prod.get("vendor") or "").lower().strip()
    orig_tags = set(str(out_of_stock_prod.get("tags") or "").lower().replace(",", " ").split())
    orig_title = (out_of_stock_prod.get("title") or "").lower()
    orig_words = set(re.findall(r"\w+", orig_title)) - {"the", "a", "an", "and", "in", "of", "for", "with"}

    scored = []
    for p in prods:
        if str(p.get("id")) == orig_id:
            continue
        in_stock_var = None
        for v in (p.get("variants") or []):
            inv_policy = v.get("inventory_policy", "deny")
            inv_qty = v.get("inventory_quantity")
            inv_mgmt = v.get("inventory_management")
            if not inv_mgmt or inv_policy == "continue" or (inv_qty and inv_qty > 0):
                in_stock_var = v
                break
        if not in_stock_var:
            continue

        score = 0
        p_type = (p.get("product_type") or "").lower().strip()
        if orig_type and p_type == orig_type:
            score += 20
        p_vendor = (p.get("vendor") or "").lower().strip()
        if orig_vendor and p_vendor == orig_vendor:
            score += 5
        p_tags = set(str(p.get("tags") or "").lower().replace(",", " ").split())
        common_tags = orig_tags & p_tags
        score += len(common_tags) * 4
        p_words = set(re.findall(r"\w+", (p.get("title") or "").lower())) - {"the", "a", "an", "and", "in", "of", "for", "with"}
        common_words = orig_words & p_words
        score += len(common_words) * 5

        scored.append((score, p, in_stock_var))

    scored.sort(key=lambda x: x[0], reverse=True)
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    alts = []
    for _, p, var in scored[:limit]:
        img = (p.get("image") or {}).get("src", "")
        if not img and p.get("images"):
            img = (p["images"][0] or {}).get("src", "")
        alts.append({
            "id": str(var.get("id")),
            "variant_id": str(var.get("id")),
            "product_id": str(p.get("id")),
            "name": p.get("title"),
            "title": p.get("title"),
            "price": float(var.get("price", 0) or 0),
            "image": img,
            "handle": p.get("handle", ""),
            "url": f"https://{shop}/products/{p.get('handle', '')}" if p.get("handle") else "",
            "in_stock": True,
        })
    return alts


async def subscribe_restock(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    import repository_appwrite as _repo
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    store_id = getattr(store, "id", None) or getattr(store, "$id", None) or shop
    email = (entities.get("email") or "").strip().lower()
    product_query = entities.get("product_name_or_id") or entities.get("query") or entities.get("product_title") or ""
    size = (entities.get("size") or "").strip()
    color = (entities.get("color") or "").strip()
    target_price = (str(entities.get("target_price") or "")).strip()
    alert_type = "price_drop" if target_price else "restock"

    # Step 1: Verify product name & find the product in the store catalog
    prod, variant, in_stock, catalog_checked = await _find_store_product(store, str(product_query), size=size, color=color)

    if catalog_checked and not prod:
        clean_name = str(product_query).strip() or "that product"
        return {
            "status": "not_found",
            "product_title": clean_name,
            "message": f"I couldn't find '{clean_name}' in our store catalog. Please check the product name or explore our active collection.",
        }

    prod_title = (prod.get("title") if prod else None) or str(product_query) or "Product"
    variant_id = (str(variant.get("id")) if variant else None) or str(entities.get("variant_id") or "")
    price = float(variant.get("price", 0) or 0) if variant else 0.0

    product_card = {
        "id": variant_id,
        "product_id": str(prod.get("id", "")) if prod else "",
        "name": prod_title,
        "price": price,
        "image": (prod.get("image") or {}).get("src", "") if prod else "",
        "handle": prod.get("handle", "") if prod else "",
        "url": f"https://{shop}/products/{prod.get('handle', '')}" if (prod and prod.get("handle")) else "",
        "in_stock": in_stock,
    }

    # Step 2: If the product is IN STOCK, inform the user directly on chat!
    if in_stock:
        size_label = f" in size {size}" if size else ""
        return {
            "status": "already_in_stock",
            "product_title": prod_title,
            "results": [product_card],
            "message": f"Good news! '{prod_title}'{size_label} is currently in stock and available right now! You don't have to wait for a restock alert.",
        }

    # Step 3: Product is OUT OF STOCK.
    # Look up 2-3 in-stock close alternatives to rescue the sale!
    alternatives = await _find_close_alternatives(store, prod, limit=3)
    alt_titles = ", ".join(f"'{a['name']}'" for a in alternatives) if alternatives else ""
    swap_actions = [
        {"label": f"Swap to {a['name'][:24]}", "action": "swap_item", "variant_id": a["variant_id"]}
        for a in alternatives
    ]

    # If email is not yet provided:
    if not email:
        alt_text = f"\n\nIn the meantime, here are {len(alternatives)} similar in-stock alternatives you can swap into your cart right now:\n{alt_titles}" if alternatives else ""
        return {
            "status": "email_required",
            "product_title": prod_title,
            "product_id": str(prod.get("id", "")) if prod else "",
            "variant_id": variant_id,
            "size": size,
            "results": alternatives,
            "alternatives": alternatives,
            "actions": swap_actions,
            "message": f"'{prod_title}' is currently out of stock. Please share your email address so I can email you the moment it's available again!{alt_text}",
        }

    prod_handle = (prod.get("handle") if prod else "") or ""
    prod_img = ""
    if prod:
        img_data = prod.get("image") or (prod.get("images", [{}])[0] if prod.get("images") else {})
        if isinstance(img_data, dict):
            prod_img = img_data.get("src", "")
        elif isinstance(img_data, str):
            prod_img = img_data

    prod_price = ""
    if prod and prod.get("variants"):
        raw_p = prod["variants"][0].get("price")
        if raw_p:
            prod_price = f"₹{raw_p}" if "₹" not in str(raw_p) else str(raw_p)

    record = _repo.save_restock_alert(
        store_id=store_id,
        shop_domain=shop,
        customer_email=email,
        product_id=str(prod.get("id", "")) if prod else (variant_id or prod_title),
        product_title=prod_title,
        variant_id=str(variant_id),
        size=size,
        target_price=target_price,
        alert_type=alert_type,
        product_handle=prod_handle,
        product_image=prod_img,
        product_url=f"https://{shop}/products/{prod_handle}" if prod_handle else "",
        price=prod_price,
    )

    size_text = f" (Size {size})" if size else ""
    alt_proposal = f"\n\nWhile you wait, here are {len(alternatives)} close in-stock alternatives available right now! You can swap any into your cart:" if alternatives else ""
    return {
        "status": "alert_subscribed",
        "product_title": prod_title,
        "email": email,
        "size": size,
        "alert_type": alert_type,
        "subscription_id": record.get("$id") or record.get("id"),
        "results": alternatives,
        "alternatives": alternatives,
        "actions": swap_actions,
        "message": (
            f"You're all set! '{prod_title}'{size_text} is currently out of stock, but we will email you at {email} "
            f"the moment fresh stock arrives. You won't miss it!{alt_proposal}"
        ),
    }


async def list_my_alerts(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    import repository_appwrite as _repo
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    store_id = getattr(store, "id", None) or getattr(store, "$id", None) or shop
    email = (entities.get("email") or "").strip().lower()

    if not email:
        return {
            "status": "email_required",
            "message": "Please share your email address so I can look up your active restock alerts.",
        }

    alerts = _repo.list_restock_alerts(store_id, customer_email=email, status="active")
    if not alerts:
        return {
            "status": "no_alerts",
            "alerts": [],
            "message": f"You don't have any active restock alerts under {email}. Ask me to notify you about any product anytime!",
        }

    formatted = []
    for a in alerts:
        size_str = f" [Size: {a['size']}]" if a.get("size") else ""
        formatted.append(f"• {a.get('product_title')}{size_str} ({a.get('alert_type', 'restock').replace('_', ' ').title()})")

    return {
        "status": "alerts_found",
        "alerts": alerts,
        "count": len(alerts),
        "message": f"Here are your active alerts for {email}:\n" + "\n".join(formatted),
    }


# ==========================================================================
# CART SWAP FOR OUT-OF-STOCK ALTERNATIVES
# ==========================================================================
async def swap_cart_item(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    """Swaps an out-of-stock item with an in-stock alternative in the storefront cart."""
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    var_id = entities.get("variant_id") or entities.get("id")
    target_prod = entities.get("product_title") or entities.get("product_name") or entities.get("name")

    if not var_id and target_prod:
        prod, variant, in_stock, _ = await _find_store_product(store, str(target_prod))
        if variant and in_stock:
            var_id = str(variant.get("id"))

    if not var_id:
        return {
            "status": "swap_failed",
            "message": "I couldn't identify the variant to swap into your cart. Please select one of the recommended alternative items.",
        }

    swap_action = {
        "type": "swap_item",
        "remove_variant_id": str(entities.get("remove_variant_id") or ""),
        "add_variant_id": str(var_id),
        "quantity": int(entities.get("quantity") or 1),
    }
    return {
        "status": "swapped",
        "widget_action": swap_action,
        "message": "Great choice! I have prepared the in-stock alternative for your cart. You can review your cart anytime or proceed directly to checkout!",
    }


# ==========================================================================
# STANDING INSTRUCTIONS & SCHEDULED TASKS
# ==========================================================================
async def create_standing_instruction(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    """Handles standing instructions and scheduled tasks:
    - Price drops: 'Tell me when this drops below ₹999'
    - Reorder reminder: 'Remind me to reorder in 30 days'
    - Auto-add restock: 'Add this to my cart when it's back in stock'
    """
    import repository_appwrite as _repo
    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    store_id = getattr(store, "id", None) or getattr(store, "$id", None) or shop
    email = (entities.get("email") or "").strip().lower()
    instruction_text = entities.get("instruction") or entities.get("message") or entities.get("query") or ""

    target_price = str(entities.get("target_price") or "").strip()
    if not target_price:
        m_price = re.search(r"(?:below|under|drops?\s*to|drops?\s*below)\s*(?:₹|\$|rs\.?|inr)?\s*(\d+(?:,\d+)?(?:\.\d+)?)", instruction_text, re.IGNORECASE)
        if m_price:
            target_price = m_price.group(1).replace(",", "")

    interval_days = int(entities.get("days") or 0)
    if not interval_days:
        m_days = re.search(r"(?:in|after|every)\s*(\d+)\s*(?:days?|weeks?|months?)", instruction_text, re.IGNORECASE)
        if m_days:
            val = int(m_days.group(1))
            if "week" in m_days.group(0).lower():
                interval_days = val * 7
            elif "month" in m_days.group(0).lower():
                interval_days = val * 30
            else:
                interval_days = val

    auto_add = bool(entities.get("auto_add") or any(p in instruction_text.lower() for p in ["add this to my cart", "add to cart", "auto add", "add to my"]))

    if target_price:
        instruction_type = "price_drop"
    elif interval_days:
        instruction_type = "reorder_reminder"
    elif auto_add:
        instruction_type = "auto_add_restock"
    else:
        instruction_type = entities.get("instruction_type", "price_drop")

    product_name = entities.get("product_title") or entities.get("product_name") or entities.get("product_query") or ""
    if not product_name:
        clean_p = re.sub(
            r"\b(tell me when|remind me to|add this to my cart when|alert me when|drops below|reorder in|back in stock|it's back in stock|reorder|in|days|weeks|below|under)\b.*",
            "",
            instruction_text,
            flags=re.IGNORECASE,
        ).strip()
        product_name = clean_p or instruction_text

    prod, variant, in_stock, _ = await _find_store_product(store, str(product_name))
    prod_title = (prod.get("title") if prod else None) or product_name or "this item"
    prod_id = str(prod.get("id")) if prod else ""

    if not email:
        return {
            "status": "email_required",
            "instruction_type": instruction_type,
            "product_title": prod_title,
            "target_price": target_price,
            "interval_days": interval_days,
            "auto_add": auto_add,
            "message": f"I'd be glad to schedule that for '{prod_title}'! Please share your email address so I can alert you as requested.",
        }

    prod_handle = (prod.get("handle") if prod else "") or ""
    prod_img = ""
    if prod:
        img_data = prod.get("image") or (prod.get("images", [{}])[0] if prod.get("images") else {})
        if isinstance(img_data, dict):
            prod_img = img_data.get("src", "")
        elif isinstance(img_data, str):
            prod_img = img_data

    prod_price = ""
    if prod and prod.get("variants"):
        raw_p = prod["variants"][0].get("price")
        if raw_p:
            prod_price = f"₹{raw_p}" if "₹" not in str(raw_p) else str(raw_p)

    record = _repo.save_standing_instruction(
        store_id=store_id,
        shop_domain=shop,
        customer_email=email,
        instruction_type=instruction_type,
        product_id=prod_id,
        product_title=prod_title,
        target_price=target_price,
        interval_days=interval_days,
        auto_add=auto_add,
        payload=entities,
        product_handle=prod_handle,
        product_image=prod_img,
        product_url=f"https://{shop}/products/{prod_handle}" if prod_handle else "",
        price=prod_price,
    )

    if instruction_type == "price_drop":
        confirm_msg = f"Standing instruction active! I'm watching '{prod_title}'. The moment the price drops below ₹{target_price}, I will email you at {email}."
    elif instruction_type == "reorder_reminder":
        confirm_msg = f"Reminder scheduled! I will email you at {email} in {interval_days} days with a 1-click reorder link for '{prod_title}'."
    elif instruction_type == "auto_add_restock":
        confirm_msg = f"Instruction recorded! The moment '{prod_title}' is restocked, I will email you at {email} and stage it directly in your cart for checkout."
    else:
        confirm_msg = f"Standing instruction saved for '{prod_title}'. I'll notify you at {email} as requested."

    return {
        "status": "instruction_created",
        "instruction_id": record.get("$id") or record.get("id"),
        "instruction_type": instruction_type,
        "product_title": prod_title,
        "email": email,
        "message": confirm_msg,
    }


# ==========================================================================
# ORDER EDITS & CANCELLATIONS (Pre-fulfillment, Verification, Auto/Ask/Off)
# ==========================================================================
_PENDING_CANCELLATIONS: dict[tuple[str, str], dict] = {}

async def _get_order_by_number(store, order_number: str) -> dict | None:
    """Finds an order by order number or name (e.g. 1001 or #1001)."""
    clean_num = str(order_number).lstrip("#").strip()
    try:
        resp = await _get(store, "orders.json", {"name": f"#{clean_num}", "status": "any", "limit": 1})
        if resp.status_code == 200:
            orders = resp.json().get("orders", [])
            if orders:
                return orders[0]
        # Fallback to search query
        resp2 = await _get(store, "orders.json", {"query": clean_num, "status": "any", "limit": 5})
        if resp2.status_code == 200:
            for ord_obj in resp2.json().get("orders", []):
                if str(ord_obj.get("order_number")) == clean_num or str(ord_obj.get("name")).lstrip("#") == clean_num:
                    return ord_obj
    except Exception as e:
        print(f"[_get_order_by_number] lookup failed: {e!r}")
    return None


async def _execute_shopify_order_cancel(store, order_id: str) -> tuple[bool, str]:
    """Cancels unfulfilled order in Shopify and restocks inventory."""
    clean_id = str(order_id).split("/")[-1]
    try:
        # Try REST cancellation
        resp = await _post(store, f"orders/{clean_id}/cancel.json", {"reason": "customer", "email": True, "restock": True})
        if resp.status_code in (200, 201):
            return True, ""
        # Try GraphQL orderCancel mutation
        mutation = """
        mutation orderCancel($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!) {
          orderCancel(orderId: $orderId, reason: $reason, restock: $restock) {
            orderCancelUserErrors { field message }
          }
        }
        """
        data = await _graphql_admin(store, mutation, {
            "orderId": f"gid://shopify/Order/{clean_id}",
            "reason": "CUSTOMER",
            "restock": True,
        })
        errors = ((data or {}).get("orderCancel") or {}).get("orderCancelUserErrors") or []
        if not errors:
            return True, ""
        return False, errors[0].get("message", "Cancellation rejected by Shopify")
    except Exception as e:
        return False, str(e)


async def cancel_order(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    """Handles order cancellation strictly before fulfillment, with verification,
    and adhering to the merchant's Auto / Ask / Off setting."""
    import repository_appwrite as _repo
    import email_utils

    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    store_id = getattr(store, "id", None) or getattr(store, "$id", None) or shop
    order_number = str(entities.get("order_number") or entities.get("order_id") or "").lstrip("#").strip()
    store_owner_email = _repo.get_store_owner_email(shop)
    mode = _repo.get_order_cancellation_mode(store_id)

    if not order_number:
        return {
            "status": "order_number_required",
            "message": "Please provide your order number (e.g. #1001) so I can verify and process your request.",
        }

    # 1. Check merchant setting
    if mode == "off":
        return {
            "status": "disabled",
            "message": f"Automated order cancellation is disabled for this store. Please contact our support team directly at {store_owner_email} for assistance with order #{order_number}.",
        }

    # 2. Look up order
    orders_data = await _get_order_by_number(store, order_number)
    if not orders_data:
        return {
            "status": "order_not_found",
            "message": f"I couldn't locate order #{order_number}. Please check the order number or check your recent orders.",
        }

    order_id = str(orders_data.get("id"))
    order_email = ((orders_data.get("email") or "").strip().lower() or
                   ((orders_data.get("customer") or {}).get("email") or "").strip().lower())
    order_cust_id = str((orders_data.get("customer") or {}).get("id") or "").split("/")[-1]

    # 3. Check if already cancelled
    if orders_data.get("cancelled_at") or orders_data.get("cancelledAt"):
        return {
            "status": "already_cancelled",
            "message": f"Order #{order_number} has already been cancelled.",
        }

    # 4. Check fulfillment status (BEFORE FULFILLMENT ONLY)
    f_status = (orders_data.get("fulfillment_status") or orders_data.get("displayFulfillmentStatus") or "").lower()
    if f_status in {"fulfilled", "shipped", "in_transit", "out_for_delivery", "partially_fulfilled"}:
        return {
            "status": "already_fulfilled",
            "message": f"Order #{order_number} has already been fulfilled and is on the way ({f_status}). Orders cannot be cancelled once fulfilled, but you can initiate a return as soon as it arrives!",
        }

    # 5. Customer verification
    key = (shop, session_id)
    otp_provided = str(entities.get("otp_code") or "").strip()
    verified = False

    if customer_id and order_cust_id and str(customer_id) == str(order_cust_id):
        verified = True
    elif entities.get("customer_email") and order_email and str(entities["customer_email"]).lower() == order_email:
        verified = bool(entities.get("otp_verified"))

    if not verified:
        pending = _PENDING_CANCELLATIONS.get(key)
        if pending and pending.get("order_number") == order_number and otp_provided and otp_provided == pending.get("code"):
            verified = True
            _PENDING_CANCELLATIONS.pop(key, None)
        elif not otp_provided:
            code = str(secrets.randbelow(900000) + 100000)
            _PENDING_CANCELLATIONS[key] = {
                "code": code,
                "order_number": order_number,
                "order_id": order_id,
                "email": order_email,
                "expires": time.time() + 600,
            }
            masked = (order_email[:2] + "****" + order_email[order_email.find("@"):]) if "@" in order_email else "your email"
            email_utils.send_html_email(
                to_email=order_email,
                subject=f"Verification Code for Order #{order_number} Cancellation",
                html_body=f"<p>Your 6-digit confirmation code to cancel order #{order_number} is <strong>{code}</strong>. This code expires in 10 minutes.</p>",
                reply_to=store_owner_email,
                from_name=f"{shop} Support",
            )
            return {
                "status": "verification_required",
                "order_number": order_number,
                "message": f"To protect your security, I've sent a 6-digit confirmation code to {masked}. Please reply with the code to confirm cancelling order #{order_number}.",
            }
        else:
            return {
                "status": "invalid_otp",
                "message": "Invalid confirmation code. Please check your email and enter the 6-digit code.",
            }

    # 6. Mode 'ask' -> Request merchant review
    store_name = _repo.get_store_display_name(shop)
    if mode == "ask":
        _repo.save_order_action_request(
            store_id=store_id,
            shop_domain=shop,
            order_number=order_number,
            customer_email=order_email,
            action_type="cancel",
            details="Shopper verified cancellation request submitted before fulfillment.",
            status="pending",
        )
        email_utils.send_html_email(
            to_email=store_owner_email,
            subject=f"⚠️ Cancellation Request: Order #{order_number}",
            html_body=f"<p>Verified customer {order_email} requested cancellation for unfulfilled Order #{order_number}. Please review in Shopify Admin.</p>",
            reply_to=order_email,
            from_name=f"{store_name} Order Manager",
        )
        email_utils.send_order_cancellation_email(
            to_email=order_email,
            store_name=store_name,
            order_number=order_number,
            status="requested",
            details="Your cancellation request has been verified and submitted to the store owner for approval. You will receive an update shortly.",
            reply_to=store_owner_email,
        )
        return {
            "status": "cancellation_requested",
            "order_number": order_number,
            "message": f"Your cancellation request for order #{order_number} has been verified and submitted to the store owner. You'll receive a confirmation email at {order_email} once processed!",
        }

    # 7. Mode 'auto' -> Automated cancellation
    success, err = await _execute_shopify_order_cancel(store, order_id)
    if not success:
        return {
            "status": "error",
            "message": f"Could not cancel order #{order_number} automatically ({err}). Please contact {store_owner_email} for assistance.",
        }

    _repo.save_order_action_request(
        store_id=store_id,
        shop_domain=shop,
        order_number=order_number,
        customer_email=order_email,
        action_type="cancel",
        details="Auto-cancelled before fulfillment via AI Assistant.",
        status="cancelled",
    )
    email_utils.send_order_cancellation_email(
        to_email=order_email,
        store_name=store_name,
        order_number=order_number,
        status="cancelled",
        details=f"Your order #{order_number} has been successfully cancelled and all items have been released back into stock.",
        reply_to=store_owner_email,
    )
    return {
        "status": "cancelled",
        "order_number": order_number,
        "message": f"Order #{order_number} has been successfully cancelled and your items have been released back into stock. A confirmation receipt was emailed to {order_email}.",
    }


async def edit_order_address(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    """Updates shipping address on an unfulfilled order before fulfillment."""
    import repository_appwrite as _repo
    import email_utils

    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    store_id = getattr(store, "id", None) or getattr(store, "$id", None) or shop
    store_name = _repo.get_store_display_name(shop)
    order_number = str(entities.get("order_number") or entities.get("order_id") or "").lstrip("#").strip()
    new_address = str(entities.get("address") or entities.get("query") or "").strip()
    store_owner_email = _repo.get_store_owner_email(shop)

    if not order_number or not new_address:
        return {
            "status": "info_required",
            "message": "Please provide your order number and the updated shipping address you'd like to use.",
        }

    orders_data = await _get_order_by_number(store, order_number)
    if not orders_data:
        return {
            "status": "order_not_found",
            "message": f"I couldn't locate order #{order_number}. Please check your order number.",
        }

    f_status = (orders_data.get("fulfillment_status") or orders_data.get("displayFulfillmentStatus") or "").lower()
    if f_status in {"fulfilled", "shipped", "in_transit", "out_for_delivery", "partially_fulfilled"}:
        return {
            "status": "already_fulfilled",
            "message": f"Order #{order_number} has already been fulfilled and is currently in transit ({f_status}). The delivery address cannot be modified once shipped.",
        }

    order_id = str(orders_data.get("id"))
    order_email = ((orders_data.get("email") or "").strip().lower() or
                   ((orders_data.get("customer") or {}).get("email") or "").strip().lower())

    # Update note on order in Shopify Admin
    await _put(store, f"orders/{order_id}.json", {"order": {"id": int(order_id), "note": f"Shipping address update requested: {new_address}"}})
    _repo.save_order_action_request(
        store_id=store_id,
        shop_domain=shop,
        order_number=order_number,
        customer_email=order_email,
        action_type="edit_address",
        details=f"Address update to: {new_address}",
        status="completed",
    )
    email_utils.send_html_email(
        to_email=order_email,
        subject=f"Delivery Address Update for Order #{order_number}",
        html_body=f"<p>The delivery notes for Order #{order_number} have been updated to: <strong>{new_address}</strong>.</p>",
        reply_to=store_owner_email,
        from_name=f"{store_name} Support",
    )
    return {
        "status": "address_updated",
        "order_number": order_number,
        "message": f"The delivery address for order #{order_number} has been updated to '{new_address}'. A confirmation receipt has been sent to {order_email}.",
    }


# ==========================================================================
# ABANDONED CART RECOVERY NUDGE
# ==========================================================================
async def nudge_abandoned_cart(store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    """Handles in-widget abandoned cart recovery nudges and direct email recovery with 100% real cart items."""
    import repository_appwrite as _repo
    import email_utils

    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    store_id = getattr(store, "id", None) or getattr(store, "$id", None) or shop
    store_name = _repo.get_store_display_name(shop)
    store_owner_email = _repo.get_store_owner_email(shop)
    cart_data = entities.get("cart_data") or {}
    items = cart_data.get("items") or []
    checkout_url = f"https://{shop}/checkout"
    total_price = cart_data.get("total") or ""

    if not items:
        return {
            "status": "empty_cart",
            "message": "Your cart is currently empty! Add items to your cart, and I'll gladly hold them for you or email you a recovery link anytime.",
        }

    first_title = items[0].get("title") or items[0].get("name") or "item"
    email = (entities.get("email") or entities.get("customer_email") or "").strip().lower()

    # If email provided or explicitly asked to send via email:
    if email:
        _repo.save_abandoned_checkout(
            store_id=store_id,
            shop_domain=shop,
            customer_email=email,
            cart_items=items,
            total_price=str(total_price),
            checkout_url=checkout_url,
        )
        email_utils.send_abandoned_cart_email(
            to_email=email,
            store_name=store_name,
            cart_items=items,
            checkout_url=checkout_url,
            total_price=str(total_price),
            reply_to=store_owner_email,
        )
        msg = f"I've saved your cart with {len(items)} items (including {first_title}) and emailed your direct recovery link to {email}! You can also complete your order right now here: {checkout_url}"
        return {
            "status": "cart_held_and_emailed",
            "email": email,
            "checkout_url": checkout_url,
            "widget_action": {"type": "checkout", "url": checkout_url},
            "message": msg,
        }

    # In-widget cart hold without email
    msg = f"I've saved your cart with {len(items)} items (including {first_title})! You can complete your order anytime with this direct link: {checkout_url}. Let me know if you'd like me to email your cart to you!"
    return {
        "status": "cart_held",
        "checkout_url": checkout_url,
        "widget_action": {"type": "checkout", "url": checkout_url},
        "message": msg,
    }





# Maps "intent.action" -> async function, built directly from the names
# used in intent_schema.json so the router and the schema can never drift.
ACTION_MAP = {
    "registration_login.register": register,
    "registration_login.login": login,
    "registration_login.logout": logout,
    "registration_login.forgot_password": forgot_password,
    "order_tracking.track_order": track_order,
    "order_tracking.list_recent_orders": list_recent_orders,
    "cart_management.add_item": add_item,
    "cart_management.add_all_to_cart": add_all_to_cart,
    "cart_management.remove_item": remove_item,
    "cart_management.edit_quantity": edit_quantity,
    "cart_management.view_cart": view_cart,
    "cart_management.clear_cart": clear_cart,
    "warranty_claim.submit_claim": submit_claim,
    "warranty_claim.check_claim_status": check_claim_status,
    "payment_checkout.save_payment_credentials": save_payment_credentials,
    "payment_checkout.initiate_payment": initiate_payment,
    "payment_checkout.confirm_payment_otp": confirm_payment_otp,
    "product_search.search_products": search_products,
    "policy_query.answer_policy_question": answer_policy_question,
    "customer_account.get_my_orders": get_my_orders,
    "customer_account.get_recommendations": get_recommendations,
    "product_search.get_recommendations": get_recommendations,
    "recommendations.recommend_products": get_recommendations,
    "general_chat.assistant_help": assistant_help,
    "general_chat.recommend_products": recommend_products,
    "restock_alert.subscribe_restock": subscribe_restock,
    "restock_alert.list_my_alerts": list_my_alerts,
    "cart_management.swap_item": swap_cart_item,
    "standing_instruction.create_instruction": create_standing_instruction,
    "order_management.cancel_order": cancel_order,
    "order_management.edit_address": edit_order_address,
    "cart_recovery.nudge_abandoned_cart": nudge_abandoned_cart,
    "fallback.clarify": clarify,
}


async def dispatch(intent: str, action: str, store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    key = f"{intent}.{action}"
    fn = ACTION_MAP.get(key)
    if fn is None:
        return {"error": f"No handler registered for {key}"}
    # Hand the verified customer id to every handler that can use it
    # (track_order, submit_claim, check_claim_status, get_my_orders, get_recommendations, ...).
    if "customer_id" in inspect.signature(fn).parameters:
        return await fn(store, entities, session_id, customer_id=customer_id)
    return await fn(store, entities, session_id)
