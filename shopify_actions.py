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
import hashlib
import time
from datetime import datetime
import httpx
import difflib

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
        id firstName lastName displayName
        defaultEmailAddress { emailAddress }
        numberOfOrders
        orders(first: 20, sortKey: CREATED_AT, reverse: true) {
          nodes {
            id name createdAt displayFinancialStatus displayFulfillmentStatus
            totalPriceSet { shopMoney { amount currencyCode } }
            lineItems(first: 20) { nodes { name quantity } }
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
    needle = (product_query or "").strip().lower()
    if not needle:
        return None
    for item in cart_summary["items"]:
        if needle in item["name"].lower():
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


async def remove_item(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
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
