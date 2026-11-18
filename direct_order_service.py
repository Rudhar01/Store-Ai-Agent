"""
direct_order_service.py
-----------------------
Dedicated service for autonomous direct ordering and accurate real-time inventory checking.

Solves:
1. Accurate Stock Checking: Prevents in-stock products from being falsely reported as out of stock
   when Shopify REST API omits inventory_quantity or when inventory is unmanaged / continuing sales.
2. Direct Store Order Creation: When a shopper asks the agent to order a product for them
   (e.g. "order a silence tshirt for me", "buy silence tshirt for me", "place an order for ..."),
   the agent creates a store order / draft order pre-filled with the customer's personal details
   and shipping address, and redirects the customer directly to the payment page to pay.
"""

import json
import logging
import re
from urllib.parse import quote, urlencode

import httpx

import customer_profiles
import repository_appwrite as repo

logger = logging.getLogger("direct_order_service")

# -----------------------------------------------------------------------------
# 1. ACCURATE REAL-TIME STOCK RESOLUTION
# -----------------------------------------------------------------------------

def is_variant_in_stock(variant: dict | None, product: dict | None = None) -> bool:
    """Accurately checks whether a product variant is in stock.
    Avoids falsely flagging in-stock items as out of stock when Shopify REST API
    omits inventory_quantity or when store policies allow continuous sales."""
    if not variant:
        return False

    # 1. Native GraphQL / Storefront availability
    avail_sale = variant.get("availableForSale")
    if avail_sale is not None:
        return bool(avail_sale)

    # 2. Storefront REST availability
    avail = variant.get("available")
    if avail is not None:
        return bool(avail)

    # 3. Inventory policy: 'continue' means shoppers can buy even when stock count is 0
    policy = (variant.get("inventory_policy") or "").lower()
    if policy == "continue":
        return True

    # 4. Inventory management: if unmanaged or not 'shopify', quantity is untracked
    is_managed = (variant.get("inventory_management") or "").lower() == "shopify"
    if not is_managed:
        return True

    # 5. Quantity tracking when provided
    qty = variant.get("inventory_quantity")
    if qty is not None:
        try:
            return int(qty) > 0
        except (ValueError, TypeError):
            pass

    # 6. Active catalog products with omitted quantity in REST payloads default to in-stock
    if product and (product.get("status") or "active").lower() == "active":
        return True

    return True


def is_product_in_stock(product: dict | None) -> bool:
    """Returns True if the product or any of its variants is currently in stock."""
    if not product:
        return False

    variants = product.get("variants") or []
    if variants:
        return any(is_variant_in_stock(v, product) for v in variants)

    # If cards format (pre-formatted card dictionary)
    if "in_stock" in product or "available" in product:
        return bool(product.get("in_stock", True) or product.get("available", True))

    return (product.get("status") or "active").lower() == "active"


# -----------------------------------------------------------------------------
# 2. DIRECT ORDER REQUEST DETECTION & PARSING
# -----------------------------------------------------------------------------

_DIRECT_ORDER_RE = re.compile(
    r"^(?:can you\s+)?(?:could you\s+)?(?:please\s+)?(?:i\s+want\s+to\s+)?"
    r"(?:order|buy|purchase|place\s+(?:an?\s+)?order\s*(?:for|of)?|create\s+(?:an?\s+)?order\s*(?:for|of)?)"
    r"(?:\s+(.+?))?(?:\s+for\s+me)?$",
    re.IGNORECASE,
)

_EXCLUSION_WORDS = [
    "track", "where is", "status", "cancel", "return", "refund",
    "history", "past order", "previous order", "eligibility",
    "policy", "how to", "can i return", "whether",
]


def is_direct_order_request(message: str) -> tuple[bool, str, int]:
    """Detects if the shopper wants the agent to order a product for them directly.
    Returns: (is_direct_order, raw_product_name, quantity)
    """
    msg = (message or "").strip()
    msg_low = msg.lower()

    if not msg:
        return False, "", 1

    # Exclude tracking, returns, cancellations, and policy questions
    if any(w in msg_low for w in _EXCLUSION_WORDS):
        return False, "", 1

    # Exclude cart management queries ("add ... to cart", "put in my cart", "view cart")
    if re.search(r"\b(to|in|into)\s+(?:my\s+|the\s+)?(?:cart|bag|basket)\b", msg_low):
        return False, "", 1
    if re.search(r"\b(view|see|check|clear|empty|remove|delete)\s+(?:my\s+|the\s+)?(?:cart|bag)\b", msg_low):
        return False, "", 1

    # Exclude order numbers (e.g. "order #1001")
    if bool(re.search(r"#\d{3,}", msg)):
        return False, "", 1

    m = _DIRECT_ORDER_RE.match(msg)
    if not m:
        return False, "", 1

    raw_item = (m.group(1) or "").strip()
    if not raw_item or raw_item.lower() in ("me", "for me", "it", "this", "that", "the item", "product", "the product", "order", "an order", "the order"):
        return True, "", 1

    # Parse quantity and clean product name
    qty = 1
    m_qty = re.match(r"^\s*(\d+)\s*(?:x|units?|pieces?|pcs?|items?|of)?\s+(.+)$", raw_item, re.IGNORECASE)
    if m_qty:
        qty = int(m_qty.group(1))
        prod_name = m_qty.group(2).strip()
    else:
        m_art = re.match(r"^(?:a|an|the|one)\s+(.+)$", raw_item, re.IGNORECASE)
        prod_name = m_art.group(1).strip() if m_art else raw_item

    # Strip trailing punctuation or fillers
    prod_name = re.sub(r"\s+for\s+me$", "", prod_name, flags=re.IGNORECASE).strip(" .,!?")
    if prod_name.lower() in ("me", "for me", "it", "this", "that", "the item", "product", "the product", "order", "an order", "the order"):
        prod_name = ""

    return True, prod_name, max(1, qty)


# -----------------------------------------------------------------------------
# 3. CUSTOMER DETAILS RESOLUTION
# -----------------------------------------------------------------------------

async def resolve_customer_order_details(
    store,
    shop: str,
    customer_id: str | None = None,
    customer_email: str | None = None,
    customer_name: str | None = None,
) -> dict:
    """Gathers all available customer details (name, email, phone, shipping address)
    from Appwrite customer profiles and Shopify customer records so orders can be
    pre-filled without manual effort by the customer."""
    details = {
        "email": (customer_email or "").strip().lower(),
        "first_name": "",
        "last_name": "",
        "name": (customer_name or "").strip(),
        "phone": "",
        "shipping_address": None,
        "customer_id": customer_id,
    }

    if customer_name:
        parts = customer_name.strip().split()
        details["first_name"] = parts[0]
        details["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else ""

    # 1. Check Appwrite customer profile
    profile = None
    try:
        profile = await customer_profiles.get_profile(shop, customer_id=customer_id, email=customer_email)
    except Exception as e:
        logger.debug("Failed fetching customer profile from Appwrite: %s", e)

    if profile:
        if not details["email"] and profile.get("email"):
            details["email"] = profile["email"].strip().lower()
        if not details["first_name"] and profile.get("first_name"):
            details["first_name"] = profile["first_name"].strip()
        if not details["last_name"] and profile.get("last_name"):
            details["last_name"] = profile["last_name"].strip()
        if not details["name"] and profile.get("name"):
            details["name"] = profile["name"].strip()
        if not details["phone"] and profile.get("phone"):
            details["phone"] = profile["phone"].strip()

        # Check saved addresses
        raw_addr = profile.get("addresses")
        if raw_addr:
            if isinstance(raw_addr, str):
                try:
                    parsed_addr = json.loads(raw_addr)
                    if isinstance(parsed_addr, dict):
                        details["shipping_address"] = parsed_addr
                except Exception:
                    # Stored as comma-separated string
                    details["shipping_address"] = {
                        "address1": raw_addr,
                        "first_name": details["first_name"],
                        "last_name": details["last_name"],
                    }
            elif isinstance(raw_addr, dict):
                details["shipping_address"] = raw_addr

    # 2. Check Shopify Admin API customer record if store connection is active
    if customer_id and store:
        try:
            import shopify_actions
            cust_resp = await shopify_actions._get(store, f"customers/{customer_id}.json")
            if cust_resp.status_code == 200:
                cust_data = cust_resp.json().get("customer", {})
                if not details["email"] and cust_data.get("email"):
                    details["email"] = cust_data["email"].strip().lower()
                if not details["first_name"] and cust_data.get("first_name"):
                    details["first_name"] = cust_data["first_name"].strip()
                if not details["last_name"] and cust_data.get("last_name"):
                    details["last_name"] = cust_data["last_name"].strip()
                if not details["phone"] and cust_data.get("phone"):
                    details["phone"] = cust_data["phone"].strip()

                def_addr = cust_data.get("default_address") or {}
                if def_addr:
                    details["shipping_address"] = {
                        "first_name": def_addr.get("first_name") or details["first_name"],
                        "last_name": def_addr.get("last_name") or details["last_name"],
                        "address1": def_addr.get("address1", ""),
                        "address2": def_addr.get("address2", ""),
                        "city": def_addr.get("city", ""),
                        "province": def_addr.get("province", ""),
                        "zip": def_addr.get("zip", ""),
                        "country": def_addr.get("country", ""),
                        "phone": def_addr.get("phone") or details["phone"],
                    }
        except Exception as e:
            logger.debug("Shopify customer REST lookup failed: %s", e)

    if not details["name"] and (details["first_name"] or details["last_name"]):
        details["name"] = f"{details['first_name']} {details['last_name']}".strip()

    return details


# -----------------------------------------------------------------------------
# 4. ORDER CREATION (DRAFT ORDER API & PRE-FILLED CHECKOUT PERMALINK)
# -----------------------------------------------------------------------------

async def create_shopify_draft_order(
    store,
    variant_id: int,
    quantity: int,
    customer_details: dict,
    shop: str,
) -> tuple[str | None, str | None, str | None, int | None]:
    """Creates a Draft Order in Shopify with line items and customer details pre-filled.
    Returns: (invoice_url, total_price, currency, draft_order_id)
    """
    import shopify_actions

    payload = {
        "draft_order": {
            "line_items": [
                {
                    "variant_id": int(variant_id),
                    "quantity": int(quantity),
                }
            ],
            "use_customer_default_address": True,
            "tags": "ai-agent-direct-order, prefilled-checkout",
            "note": "Created by AI Store Assistant on customer direct order request",
        }
    }

    cid = customer_details.get("customer_id")
    if cid and str(cid).isdigit():
        payload["draft_order"]["customer"] = {"id": int(cid)}

    if customer_details.get("email"):
        payload["draft_order"]["email"] = customer_details["email"]

    addr = customer_details.get("shipping_address")
    if addr and isinstance(addr, dict) and addr.get("address1"):
        payload["draft_order"]["shipping_address"] = addr
        payload["draft_order"]["billing_address"] = addr

    try:
        resp = await shopify_actions._post(store, "draft_orders.json", payload)
        if resp.status_code in (200, 201):
            draft = resp.json().get("draft_order", {})
            invoice_url = draft.get("invoice_url")
            total = str(draft.get("total_price") or "")
            curr = str(draft.get("currency") or "INR")
            draft_id = draft.get("id")
            if invoice_url:
                return invoice_url, total, curr, draft_id
    except Exception as e:
        logger.info("Draft order API creation note: %s — using pre-filled checkout permalink", e)

    return None, None, None, None


def build_prefilled_checkout_url(
    shop: str,
    variant_id: int,
    quantity: int,
    customer_details: dict,
) -> str:
    """Builds Shopify's official pre-filled checkout permalink so the customer
    lands on checkout with the product and their personal/shipping details pre-populated."""
    params = {}
    if customer_details.get("email"):
        params["checkout[email]"] = customer_details["email"]

    addr = customer_details.get("shipping_address")
    if isinstance(addr, dict):
        if addr.get("first_name"):
            params["checkout[shipping_address][first_name]"] = addr["first_name"]
        if addr.get("last_name"):
            params["checkout[shipping_address][last_name]"] = addr["last_name"]
        if addr.get("address1"):
            params["checkout[shipping_address][address1]"] = addr["address1"]
        if addr.get("city"):
            params["checkout[shipping_address][city]"] = addr["city"]
        if addr.get("province"):
            params["checkout[shipping_address][province]"] = addr["province"]
        if addr.get("zip"):
            params["checkout[shipping_address][zip]"] = addr["zip"]
        if addr.get("country"):
            params["checkout[shipping_address][country]"] = addr["country"]
        if addr.get("phone"):
            params["checkout[shipping_address][phone]"] = addr["phone"]
    else:
        if customer_details.get("first_name"):
            params["checkout[shipping_address][first_name]"] = customer_details["first_name"]
        if customer_details.get("last_name"):
            params["checkout[shipping_address][last_name]"] = customer_details["last_name"]
        if customer_details.get("phone"):
            params["checkout[shipping_address][phone]"] = customer_details["phone"]

    query = f"?{urlencode(params)}" if params else ""
    return f"https://{shop}/cart/{variant_id}:{quantity}{query}"


# -----------------------------------------------------------------------------
# 5. CORE DISPATCH ENTRY POINT
# -----------------------------------------------------------------------------

async def handle_direct_order_request(
    store,
    message: str,
    session_id: str,
    customer_id: str | None = None,
    customer_email: str | None = None,
    customer_name: str | None = None,
    last_product: dict | None = None,
    cart_data: dict | None = None,
) -> dict:
    """Processes a direct product order request:
    1. Identifies the requested product and variant.
    2. Validates real-time inventory to ensure in-stock items are processed.
    3. Resolves customer details and pre-fills the order.
    4. Creates the order and returns a direct payment checkout redirect URL.
    """
    import shopify_actions

    shop = getattr(store, "shop_domain", None) or getattr(store, "shop", "")
    is_req, prod_query, qty = is_direct_order_request(message)
    if not is_req:
        return {}

    # If no specific product mentioned in "place an order":
    if not prod_query:
        # 1. If cart has items, proceed directly to checkout to pay!
        cart_items = []
        if isinstance(cart_data, dict):
            cart_items = cart_data.get("items") or []
        if cart_items:
            item_count = sum(it.get("quantity", 1) for it in cart_items)
            item_text = f"{item_count} item" if item_count == 1 else f"{item_count} items"
            checkout_url = f"/checkout?checkout[email]={quote(customer_email)}" if customer_email else "/checkout"
            reply_msg = f"Taking you directly to the payment page to complete your order with {item_text}."
            return {
                "status": "checkout_redirect",
                "reply": reply_msg,
                "checkout_url": checkout_url,
                "widget_action": {
                    "type": "checkout",
                    "url": checkout_url,
                },
                "message_type": "action",
                "is_action": True,
            }
        # 2. If cart is empty, check if shopper previously viewed/asked about a product
        if last_product and (last_product.get("name") or last_product.get("title")):
            prod_query = last_product.get("name") or last_product.get("title")
        else:
            return {
                "status": "cart_empty",
                "reply": "Your cart is currently empty. Which product would you like me to order for you?",
                "message_type": "normal",
            }

    # Check for pronoun / referential product
    if prod_query.lower() in ("this", "this product", "it", "that", "the item", "the product") and last_product:
        prod_query = last_product.get("name") or last_product.get("title") or prod_query

    # Resolve product and variant from store catalog
    prod, variant, in_stock, _ = await shopify_actions._find_store_product(store, prod_query)
    if not prod:
        search_res = await shopify_actions.search_products(store, {"query": prod_query}, session_id)
        cards = (search_res.get("results") or []) if isinstance(search_res, dict) else []
        if cards and not search_res.get("no_exact_match"):
            top_card = cards[0]
            prod_query = top_card.get("name") or prod_query
            prod, variant, in_stock, _ = await shopify_actions._find_store_product(store, prod_query)

    if not prod or not variant:
        return {
            "status": "not_found",
            "reply": f"I couldn't find \"{prod_query}\" in our store catalog. Please check the product name or ask me to search our collection.",
            "message_type": "normal",
        }

    # Verify inventory accurately
    accurate_stock = is_variant_in_stock(variant, prod)
    if not accurate_stock:
        prod_title = prod.get("title") or prod_query
        return {
            "status": "out_of_stock",
            "reply": f"I checked our inventory for '{prod_title}', but it is currently out of stock at the moment. Would you like me to notify you as soon as fresh stock arrives?",
            "message_type": "normal",
            "product_title": prod_title,
            "in_stock": False,
        }

    # Extract variant ID, title, and price
    raw_vid = variant.get("id")
    variant_id = int(str(raw_vid).split("/")[-1]) if raw_vid else None
    if not variant_id:
        return {
            "status": "error",
            "reply": "I couldn't resolve the variant details for that product. Please try selecting it from the catalog.",
        }

    prod_title = prod.get("title") or prod_query
    price_val = variant.get("price") or (prod.get("variants", [{}])[0].get("price") if prod.get("variants") else "0.00")
    try:
        unit_price = float(str(price_val).replace("₹", "").strip())
        total_price = f"{unit_price * qty:.2f}"
    except Exception:
        total_price = str(price_val)

    # Resolve customer details
    cust_details = await resolve_customer_order_details(
        store=store,
        shop=shop,
        customer_id=customer_id,
        customer_email=customer_email,
        customer_name=customer_name,
    )

    # 1. Try Shopify Draft Order API
    invoice_url, draft_total, draft_curr, draft_id = await create_shopify_draft_order(
        store=store,
        variant_id=variant_id,
        quantity=qty,
        customer_details=cust_details,
        shop=shop,
    )

    # 2. If Draft Order URL is ready, use it; otherwise use the pre-filled checkout permalink
    if invoice_url:
        payment_url = invoice_url
        if draft_total:
            total_price = draft_total
    else:
        payment_url = build_prefilled_checkout_url(
            shop=shop,
            variant_id=variant_id,
            quantity=qty,
            customer_details=cust_details,
        )

    # Format user reply
    prefill_notes = []
    if cust_details.get("email"):
        prefill_notes.append(f"email ({cust_details['email']})")
    if cust_details.get("shipping_address"):
        prefill_notes.append("saved delivery address")

    note_str = f" with your {', '.join(prefill_notes)} already filled in" if prefill_notes else " with your order details pre-filled"
    reply_msg = (
        f"I've created your order for {qty}x '{prod_title}' (Total: ₹{total_price}){note_str}! "
        f"Adding it to your cart and taking you directly to the payment page to complete your purchase..."
    )

    return {
        "status": "order_created_redirect",
        "reply": reply_msg,
        "product_title": prod_title,
        "quantity": qty,
        "total_price": total_price,
        "checkout_url": payment_url,
        "widget_action": {
            "type": "checkout",
            "url": payment_url,
            "variant_id": variant_id,
            "quantity": qty,
        },
        "customer_prefilled": bool(cust_details.get("email") or cust_details.get("shipping_address")),
        "message_type": "action",
        "is_action": True,
    }
