"""
chatbot_widget.py
------------------
Multi-tenant AI Agent backend & embeddable widget for Shopify stores.
Supports complete eCommerce AI agent capabilities:
  - Product search, filtering, and personalized recommendations
  - Showing all customer orders (active & past, via login or email verification)
  - Live shipment tracking with carrier checkpoints and estimated delivery
  - Warranty, return, and replacement claim filing with unique Claim IDs
  - Comprehensive store policies (return, refund, shipping, warranty, privacy, etc.)
  - Secure payment credentials storage and autonomous checkout with 6-digit OTP verification in chat
  - Direct cart management from user natural language messages (add, remove, update quantity, view, clear)

Routes:
  GET  /widget.js                -> Embeddable widget script
  GET  /widget-config             -> Store configuration (name, title, icon, colors)
  POST /chat                     -> Main AI agent conversation endpoint
  POST /confirm                  -> Confirmation for sensitive actions (yes/no)
  POST /save-payment-credentials -> Direct payment card save endpoint
  POST /initiate-payment         -> Initiate cart checkout and generate OTP
  POST /verify-otp               -> Verify 6-digit OTP and complete paid order
  GET  /customer-session         -> Shopify App Proxy authenticated session exchange
"""

import re
import time
import os
import asyncio
import difflib
from types import SimpleNamespace
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

try:
    from langfuse import observe, get_client
except ImportError:
    def observe(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    def get_client(*args, **kwargs):
        return None


def _span_error(msg: str) -> None:
    try:
        cli = get_client()
        if cli and hasattr(cli, "update_current_span"):
            cli.update_current_span(level="ERROR", status_message=msg)
    except Exception:
        pass

import repository_appwrite as repo
from intent_classifier import classify_intent, load_schema
from reply_generator import generate_reply
import shopify_actions
import customer_profiles
import semantic

router = APIRouter(tags=["chatbot-widget"])

SCHEMA = load_schema()

# ---------------------------------------------------------------------
# Pending-state cache. Keyed by shop + session_id (the widget's random
# per-tab id). Bridges multi-turn follow-ups (confirmations, email lookups,
# OTP verification, warranty details, cart queries).
# ---------------------------------------------------------------------
PENDING: dict[str, dict] = {}
PENDING_TTL_SECONDS = 300

_AFFIRMATIVE = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "confirm", "confirmed",
                "si", "sí", "oui", "haan", "ha", "theek hai"}
_NEGATIVE = {"no", "n", "nope", "cancel", "nah", "non", "nahi"}

_FEATURE_FOR_ACTION = {
    ("product_search", "search_products"): "product_search",
    ("cart_management", "add_item"): "cart_editing",
    ("cart_management", "remove_item"): "cart_editing",
    ("cart_management", "edit_quantity"): "cart_editing",
    ("cart_management", "clear_cart"): "cart_editing",
    ("payment_checkout", "save_payment_credentials"): "cart_editing",
    ("payment_checkout", "initiate_payment"): "cart_editing",
    ("payment_checkout", "confirm_payment_otp"): "cart_editing",
    ("warranty_claim", "submit_claim"): "warranty",
    ("warranty_claim", "check_claim_status"): "warranty",
    ("order_tracking", "track_order"): "track_orders",
    ("order_tracking", "list_recent_orders"): "track_orders",
    ("customer_account", "get_my_orders"): "track_orders",
    ("customer_account", "get_recommendations"): "product_search",
}
_FILTER_ENTITIES = ("price_min", "price_max", "color", "size")

_RESCUABLE = {
    ("customer_account", "get_my_orders"),
    ("customer_account", "get_recommendations"),
    ("product_search", "search_products"),
    ("order_tracking", "track_order"),
    ("order_tracking", "list_recent_orders"),
    ("cart_management", "view_cart"),
    ("policy_query", "answer_policy_question"),
    ("payment_checkout", "initiate_payment"),
}


def _rescue_entities(intent: str, action: str, message: str) -> dict:
    if action == "search_products":
        return {"query": extract_search_term(message) or message.strip()}
    return {}


def _pkey(shop: str, session_id: str) -> str:
    return f"{shop}::{session_id}"


HISTORY: dict[str, dict] = {}
HISTORY_TTL_SECONDS = 1800
HISTORY_MAX_MESSAGES = 6


def _remember_turn(key: str, user_text: str, bot_reply: str) -> None:
    now = time.time()
    for k in [k for k, v in HISTORY.items() if v["ts"] < now - HISTORY_TTL_SECONDS]:
        HISTORY.pop(k, None)
    entry = HISTORY.setdefault(key, {"ts": now, "turns": []})
    entry["ts"] = now
    entry["turns"] += [("user", user_text[:300]), ("assistant", (bot_reply or "")[:300])]
    entry["turns"] = entry["turns"][-HISTORY_MAX_MESSAGES:]


def _history_for(key: str) -> list:
    entry = HISTORY.get(key)
    return list(entry["turns"]) if entry and entry["ts"] > time.time() - HISTORY_TTL_SECONDS else []


def _set_pending(key: str, **fields) -> None:
    PENDING[key] = {**fields, "expires": time.time() + PENDING_TTL_SECONDS}


def _prune_pending():
    now = time.time()
    for k in [k for k, v in PENDING.items() if v["expires"] < now]:
        PENDING.pop(k, None)


def _split_widget_action(data: dict) -> tuple[dict, dict | None]:
    if not isinstance(data, dict):
        return data, None
    action = data.get("widget_action")
    if action is None:
        return data, None
    clean = {k: v for k, v in data.items() if k != "widget_action"}
    return clean, action


class ChatRequest(BaseModel):
    message: str = ""
    session_id: str = "anonymous"
    shop: str
    customer_token: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    visitor_id: str | None = None
    otp_code: str | None = None
    payment_data: dict | None = None
    cart_data: dict | None = None


class ConfirmRequest(BaseModel):
    shop: str
    session_id: str = "anonymous"
    confirmed: bool
    customer_token: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    visitor_id: str | None = None


class PaymentCredentialsRequest(BaseModel):
    shop: str
    session_id: str = "anonymous"
    cardholder_name: str | None = None
    card_number: str | None = None
    expiry: str | None = None
    cvv: str | None = None
    customer_token: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None


class InitiatePaymentRequest(BaseModel):
    shop: str
    session_id: str = "anonymous"
    customer_token: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    cart_data: dict | None = None


class VerifyOtpRequest(BaseModel):
    shop: str
    session_id: str = "anonymous"
    otp_code: str
    customer_token: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None


_CUSTOMER_SESSIONS: dict[tuple[str, str], str] = {}
_CUSTOMER_EMAILS: dict[tuple[str, str], str] = {}
_CUSTOMER_NAMES: dict[tuple[str, str], str] = {}


def _remember_customer(shop: str, session_id: str, customer_id: str | None, email: str | None = None, name: str | None = None) -> None:
    key = (shop, session_id)
    if customer_id:
        _CUSTOMER_SESSIONS[key] = str(customer_id)
    elif customer_id is None and not email:
        _CUSTOMER_SESSIONS.pop(key, None)
    if email:
        _CUSTOMER_EMAILS[key] = str(email).strip().lower()
    if name:
        _CUSTOMER_NAMES[key] = str(name).strip()


def _session_customer(shop: str, session_id: str) -> str | None:
    return _CUSTOMER_SESSIONS.get((shop, session_id))


def _session_customer_email(shop: str, session_id: str) -> str | None:
    return _CUSTOMER_EMAILS.get((shop, session_id))


def _session_customer_name(shop: str, session_id: str) -> str | None:
    return _CUSTOMER_NAMES.get((shop, session_id))


def _get_store(shop: str) -> SimpleNamespace | None:
    doc = repo.get_store(shop)
    if not doc:
        return None
    shop_domain = doc.get("shop_domain") if isinstance(doc, dict) else getattr(doc, "shop_domain", shop)
    access_token = doc.get("access_token") if isinstance(doc, dict) else getattr(doc, "access_token", "")
    store_id = doc.get("$id") if isinstance(doc, dict) else getattr(doc, "id", None)
    return SimpleNamespace(shop_domain=shop_domain or shop, access_token=access_token, id=store_id)


import contextvars
_CURRENT_SESSION_ID = contextvars.ContextVar("analytics_session_id", default=None)
_CURRENT_VISITOR_ID = contextvars.ContextVar("analytics_visitor_id", default=None)

from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
_EVENT_POOL = _ThreadPoolExecutor(max_workers=2)
_EVENT_STORE_IDS: dict[str, str] = {}


def _store_id_for_events(shop: str) -> str | None:
    cached = _EVENT_STORE_IDS.get(shop)
    if cached:
        return cached
    doc = repo._find_store_doc(shop)
    if doc:
        _EVENT_STORE_IDS[shop] = doc["$id"]
        return doc["$id"]
    return None


def _write_events(shop: str, events: list) -> None:
    try:
        store_id = _store_id_for_events(shop)
        if not store_id:
            return
        for ev in events:
            repo.record_event(store_id, **ev)
    except Exception as e:
        print(f"chatbot_widget: analytics events failed: {str(e)[:100]}")


_INVALID_SEARCH_TERM_RE = re.compile(
    r"\b(?:more|less)\s+(?:then|than)\s+\$?\d+\b|"
    r"^(?:give me|show me|find|search|filter|under|above|over)\s+(?:more\s+|less\s+)?(?:than\s+|then\s+)?\$?\d+|"
    r"^[\d\s\$,\.\+\-]+$",
    re.IGNORECASE,
)


def _is_valid_product_search_term(text: str) -> bool:
    if not text or len(str(text).strip()) < 2:
        return False
    if _INVALID_SEARCH_TERM_RE.search(str(text).strip()):
        return False
    return True


def _build_events(status: str, intent, action, entities, session_id, visitor_id) -> list:
    ents = entities if isinstance(entities, dict) else {}
    base = {"platform": "shopify", "session_id": session_id or "", "visitor_id": visitor_id or ""}
    events = [{**base, "event_type": "chat_message", "name": str(intent or "")}]
    if status == "done" and (intent, action) == ("product_search", "search_products"):
        prod_name = ents.get("matched_product") or ents.get("product_name")
        if not prod_name:
            term = ents.get("query") or ents.get("category")
            if isinstance(term, str) and term.strip() and _is_valid_product_search_term(term):
                prod_name = term.strip()
        if prod_name:
            events.append({**base, "event_type": "search", "name": prod_name,
                           "category": ents.get("category") if isinstance(ents.get("category"), str) else ""})
            events.append({**base, "event_type": "product_view", "name": prod_name,
                           "category": ents.get("category") if isinstance(ents.get("category"), str) else ""})
    if status == "done" and (intent, action) == ("cart_management", "add_item"):
        product = ents.get("product_name_or_id") or ents.get("query")
        if isinstance(product, str) and product.strip():
            events.append({**base, "event_type": "cart_add", "name": product.strip()})
    return events


def _log(shop: str, message: str, status: str, intent=None, action=None, entities=None, reply: str = "") -> None:
    try:
        _sid, _vid = _CURRENT_SESSION_ID.get(), _CURRENT_VISITOR_ID.get()
        _extra = {}
        if _sid and _sid != "anonymous":
            _extra["session_id"] = str(_sid)[:80]
        if _vid:
            _extra["visitor_id"] = str(_vid)[:80]
        if _extra:
            entities = {**(entities if isinstance(entities, dict) else {}), **_extra}
        repo.log_request(shop, message, status, detected_intent=intent, detected_action=action, reply=reply, entities=entities)
    except Exception as e:
        print(f"chatbot_widget: log_request failed: {str(e)[:100]}")

    try:
        _sid2 = _CURRENT_SESSION_ID.get()
        _sid2 = None if _sid2 == "anonymous" else (str(_sid2)[:80] if _sid2 else None)
        _vid2 = _CURRENT_VISITOR_ID.get()
        _vid2 = str(_vid2)[:80] if _vid2 else None
        _EVENT_POOL.submit(_write_events, shop, _build_events(status, intent, action, entities, _sid2, _vid2))
    except Exception as e:
        print(f"chatbot_widget: analytics events failed: {str(e)[:100]}")


def _feature_enabled(store_id: str, intent: str, action: str, entities: dict) -> bool:
    toggle_name = _FEATURE_FOR_ACTION.get((intent, action))
    if not toggle_name:
        return True
    fn = getattr(repo, "is_feature_enabled", None)
    if callable(fn):
        try:
            return fn(store_id, toggle_name)
        except Exception:
            return True
    return True


def _safe_reply(action: str, data: dict, language: str, original_message: str, fallback: str, **extra) -> str:
    try:
        return generate_reply(action, data, language, original_message, **extra)
    except Exception as e:
        print(f"chatbot_widget: generate_reply failed: {e!r}")
        return fallback


_SEARCH_LEAD_IN = re.compile(
    r"^(?:can you\s+)?(?:please\s+)?(?:show\s+me|find(?:\s+me)?|search(?:\s+for)?|look(?:\s+up|\s+for)?|"
    r"get(?:\s+me)?|i want(?:\s+to\s+see)?|i'm looking for|i am looking for|do you have(?:\s+any|\s+some)?|"
    r"tell me about(?:\s+any|\s+some)?|are there(?:\s+any)?|browse|where can i find|what(?:'s|\s+is) your cheapest)\s+",
    re.IGNORECASE,
)
_SEARCH_FILLER = re.compile(r"\b(products?|items?|please|for me|any|around|got|add|remove|delete|to my cart|from my cart|my cart|cart|some|few|available|your|the|best|all)\b", re.IGNORECASE)
_LEADING_ARTICLE = re.compile(r"^\s*(a|an|the)\s+", re.IGNORECASE)
_CHECKOUT_RE = re.compile(r"\b(check ?out|perform checkout|checkout for me|place (my|the|an?) order|buy (it )?now|purchase now)\b", re.IGNORECASE)
_VIEW_CART_RE = re.compile(
    r"\b(view (my )?cart|show (me )?(my )?cart|what('s| is) in my cart|check (my )?cart|cart (items?|contents?)|my cart contains?|what do i have in my cart|see (my )?cart)\b",
    re.IGNORECASE,
)
_ORDER_NO_RE = re.compile(r"#?\s*(\d{3,})")
_MY_ORDERS_RE = re.compile(
    r"\b(my|all|past|previous|recent|active|current|pending|open)\b[^.?!]{0,30}\borders\b"
    r"|\blist of (my |the )?orders\b"
    r"|\border (history|list)\b"
    r"|\bwhat (have|did) i (order|ordered|bought|purchased)\b"
    r"|\b(show|list|see|view|track|where are|check|what are) (me )?(my |the |all )?orders\b"
    r"|\bmere saare orders dikhao\b|\bmere orders\b",
    re.IGNORECASE)
_RECOMMEND_RE = re.compile(
    r"\brecommend|\bsuggest|\bsomething for me\b|\bwhat (should|can|do) i (buy|get)\b|\bpick(s)? for me\b|\bwhat would i like\b|\bsomething i (would|might|could) like\b",
    re.IGNORECASE)
_POPULAR_RE = re.compile(
    r"\b(what('s| is) (popular|trending|best)|what do you recommend|show me (popular|trending) items|best sellers|what do you sell)\b",
    re.IGNORECASE)
_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|hey there|howdy|hola|namaste|who are you|what can you do|what do you do|help me|tell me about (?:your|this) store|what is this store)\b",
    re.IGNORECASE)
_PAY_RE = re.compile(r"\b(pay(ment)?|pay now|pay for (my )?(cart|order)|complete payment|proceed to pay(ment)?|buy now with card)\b", re.IGNORECASE)
_OTP_RE = re.compile(r"\b(?:otp\s*(?:is|code)?\s*[:=]?\s*)?(\d{6})\b", re.IGNORECASE)
_WARRANTY_RE = re.compile(r"\b(warranty|guarantee|claim|replace|broken|damaged|defective)\b", re.IGNORECASE)

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
_VERIFY_ERRORS = {"verification_required", "verification_failed"}


def _parse_storefront_cart(cart_data: dict | None) -> dict | None:
    if not isinstance(cart_data, dict):
        return None
    raw_items = cart_data.get("items") or []
    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        title = it.get("product_title") or it.get("title") or it.get("name") or "Product"
        qty = int(it.get("quantity") or 1)
        raw_price = it.get("final_line_price") if "final_line_price" in it else it.get("price", 0)
        if isinstance(raw_price, int):
            price_str = f"{raw_price / 100:.2f}"
        else:
            try:
                price_str = f"{float(raw_price):.2f}"
            except Exception:
                price_str = "0.00"
        items.append({
            "name": title,
            "title": title,
            "quantity": qty,
            "price": price_str,
            "variant_id": it.get("variant_id") or it.get("id"),
        })

    raw_total = cart_data.get("total_price") if "total_price" in cart_data else cart_data.get("total", 0)
    if isinstance(raw_total, int):
        total_str = f"{raw_total / 100:.2f}"
    else:
        try:
            total_str = f"{float(raw_total):.2f}"
        except Exception:
            total_str = "0.00"

    if (total_str == "0.00" or total_str == "0") and items:
        try:
            sum_val = sum(float(it.get("price", "0")) * it.get("quantity", 1) for it in items)
            if sum_val > 0:
                total_str = f"{sum_val:.2f}"
        except Exception:
            pass

    currency = cart_data.get("currency") or "USD"
    item_count = cart_data.get("item_count") if cart_data.get("item_count") is not None else sum(it["quantity"] for it in items)
    return {
        "items": items,
        "item_count": item_count,
        "total": total_str,
        "currency": currency,
    }


def extract_search_term(message: str) -> str:
    m = _SEARCH_LEAD_IN.sub("", (message or "").lower().strip())
    m = _SEARCH_FILLER.sub("", m)
    m = _LEADING_ARTICLE.sub("", m)
    return re.sub(r"\s+", " ", m).strip(" .,!?")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _term_variants(word: str) -> set:
    singular = word[:-1] if word.endswith("s") and len(word) > 3 else word
    return {word, singular}


def _product_card(p: dict, store) -> dict:
    variant = (p.get("variants") or [{}])[0]
    price = variant.get("price")
    try:
        price = float(price)
        price = int(price) if price == int(price) else price
    except (TypeError, ValueError):
        pass
    card = {
        "id": str(variant.get("id")),
        "name": p.get("title", "Unnamed product"),
        "price": price,
        "image": (p.get("image") or {}).get("src", ""),
    }
    if p.get("handle"):
        card["url"] = f"https://{store.shop_domain}/products/{p['handle']}"
    return card


async def _catalog_search_ex(store, term: str, limit: int = 6):
    cleaned = extract_search_term(term) or term
    words = [w for w in _words(cleaned) if len(w) >= 2]
    if not words:
        return None, False
    try:
        resp = await shopify_actions._get(store, "products.json", {"status": "active", "limit": 250})
    except Exception as e:
        print(f"chatbot_widget: catalog fetch failed: {e!r}")
        return None, False
    if resp.status_code != 200:
        return None, False

    products = resp.json().get("products", [])
    scored = []
    for p in products:
        hay = _words(p.get("title")) + _words(p.get("product_type")) + _words(p.get("tags"))
        score = 0.0
        for w in words:
            matched = False
            for v in _term_variants(w):
                if any(h.startswith(v) or v.startswith(h) for h in hay):
                    score += 2.0
                    matched = True
                    break
            if not matched:
                for h in hay:
                    sim = difflib.SequenceMatcher(None, w, h).ratio()
                    if sim >= 0.72:
                        score += sim * 1.8
                        break
        if score > 0:
            scored.append((score, p))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [_product_card(p, store) for _, p in scored[:limit]], False

    try:
        ranked = await semantic.rank_products(store.shop_domain, products, cleaned, limit)
    except Exception as e:
        print(f"chatbot_widget: semantic search failed: {e!r}")
        ranked = None
    if ranked:
        return [_product_card(p, store) for p in ranked], True
    return [], False


async def _catalog_search(store, term: str, limit: int = 6) -> list[dict] | None:
    cards, _ = await _catalog_search_ex(store, term, limit)
    return cards


async def _resolve_product(store, text: str):
    found, by_meaning = await _catalog_search_ex(store, text)
    if not found:
        return None, None
    wanted = (text or "").strip().lower()
    for p in found:
        if p["name"].lower() == wanted:
            return p, None
    if len(found) == 1 and not by_meaning:
        return found[0], None
    return None, found


def _apply_price_filters(cards: list[dict], entities: dict) -> list[dict]:
    lo, hi = entities.get("price_min"), entities.get("price_max")
    out = []
    for c in cards:
        try:
            price = float(c.get("price"))
        except (TypeError, ValueError):
            out.append(c)
            continue
        if lo is not None and price < float(lo):
            continue
        if hi is not None and price > float(hi):
            continue
        out.append(c)
    return out


def _llm_view(data):
    if isinstance(data, dict) and "purchased_product_ids" in data:
        data = {k: v for k, v in data.items() if k != "purchased_product_ids"}
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return data
    slim = [{"name": r.get("name"), "price": r.get("price")} for r in data["results"]]
    return {**data, "results": slim}


def _as_variant_id(value):
    s = str(value)
    return int(s) if s.isdigit() else value


# =======================================================================
# Execution
# =======================================================================
@observe(name="execute_and_reply")
async def _execute_and_reply(store: SimpleNamespace, intent: str, action: str, entities: dict, language: str, original_message: str, session_id: str, customer_id: str | None = None) -> dict:
    shop = store.shop_domain
    if not customer_id:
        customer_id = _session_customer(shop, session_id)
    cached_email = _session_customer_email(shop, session_id)
    if cached_email and not entities.get("email"):
        entities["email"] = cached_email
    cached_name = _session_customer_name(shop, session_id)
    if cached_name and not entities.get("customer_name"):
        entities["customer_name"] = cached_name

    raw = await shopify_actions.dispatch(intent, action, store, entities, session_id, customer_id=customer_id)
    data, widget_action = _split_widget_action(raw)

    if action in {"search_products", "get_recommendations", "recommend_products"} and isinstance(data, dict):
        if not data.get("results"):
            query = entities.get("query") or entities.get("category") or extract_search_term(original_message)
            if query:
                alt = await _catalog_search(store, str(query))
                if alt:
                    alt = _apply_price_filters(alt, entities)
                    if alt:
                        data["results"] = alt
                        data.pop("error", None)
            if not data.get("results"):
                try:
                    resp = await shopify_actions._get(store, "products.json", {"status": "active", "limit": 6})
                    if resp.status_code == 200:
                        raw_prods = resp.json().get("products", [])
                        if raw_prods:
                            data["results"] = [_product_card(p, store) for p in raw_prods[:6]]
                            data.pop("error", None)
                except Exception as e:
                    print(f"chatbot_widget: fallback products fetch failed: {e!r}")

    products = data.get("results") if action in {"search_products", "get_recommendations", "recommend_products", "assistant_help", "clarify"} and isinstance(data, dict) else None
    if products and isinstance(products, list) and len(products) > 0:
        first_prod = products[0]
        if isinstance(first_prod, dict) and first_prod.get("name"):
            entities["matched_product"] = first_prod["name"]
    orders = None
    if action in {"list_recent_orders", "get_my_orders"} and isinstance(data, dict):
        if isinstance(data.get("orders"), list):
            orders = data["orders"]
        elif isinstance(data.get("active_orders"), list):
            orders = [{**o, "group": "active"} for o in (data.get("active_orders") or [])] + [{**o, "group": "past"} for o in (data.get("past_orders") or [])]

    rag_query = original_message
    if action == "answer_policy_question" and entities.get("policy_type"):
        rag_query = f"{entities['policy_type'].replace('_', ' ')}: {original_message}"

    fallback = (
        data.get("message")
        if isinstance(data, dict) and data.get("message")
        else ("Here's what I found:" if products else ("Done." if widget_action else "Sorry, I couldn't complete that."))
    )
    reply = _safe_reply(
        action, _llm_view(data), language, original_message, fallback,
        store_identifier=store.shop_domain,
        needs_rag=(action == "answer_policy_question" or action == "search_products"),
        rag_query=rag_query,
    )
    out = {"status": data.get("status", "done") if isinstance(data, dict) else "done",
           "reply": reply, "language": language, "intent": intent, "action": action}
    if products:
        out["products"] = products
    if orders:
        out["orders"] = orders
    if widget_action:
        out["widget_action"] = widget_action
    if isinstance(data, dict) and "cart" in data:
        out["cart"] = data["cart"]
    if isinstance(data, dict) and data.get("error") in _VERIFY_ERRORS:
        out["action_error"] = data["error"]
    if isinstance(data, dict):
        for k in ("otp", "claim_id", "order_number", "amount_paid", "currency", "payment_method", "claim_status"):
            if k in data:
                out[k] = data[k]
    return out


def _arm_verification_if_needed(key: str, intent: str, action: str, entities: dict, language: str, result: dict) -> None:
    if result.get("action_error") not in _VERIFY_ERRORS:
        return
    _set_pending(key, type="verify_email", intent=intent, action=action, entities=entities, language=language)


def _add_to_cart_result(product: dict, quantity: int, language: str, message: str) -> dict:
    reply = _safe_reply(
        "add_item", {"added": product["name"], "quantity": quantity}, language, message,
        fallback=f"Added {product['name']} to your cart!",
    )
    return {
        "status": "done", "reply": reply, "language": language,
        "intent": "cart_management", "action": "add_item",
        "widget_action": {"type": "cart_add", "variant_id": _as_variant_id(product["id"]), "quantity": quantity, "product_name": product["name"]},
    }


def _ask(key: str, ptype: str, instruction: str, language: str, message: str, fallback: str, **extra) -> dict:
    _set_pending(key, type=ptype, language=language, **extra)
    reply = _safe_reply("clarify", {"instruction": instruction}, language, message, fallback)
    return {"status": "done", "reply": reply, "language": language}


async def _handle_followup(store, shop: str, key: str, pending: dict, message: str, session_id: str) -> dict | None:
    ptype = pending["type"]
    language = pending.get("language", "en")
    asked = pending.get("asked", 1)
    customer_id = _session_customer(shop, session_id)

    # 1. Email response for order history
    if ptype == "await_order_email":
        email_match = EMAIL_RE.search(message)
        if email_match:
            email = email_match.group(0).lower()
            result = await _execute_and_reply(store, "order_tracking", "list_recent_orders", {"email": email}, language, message, session_id, customer_id=customer_id)
            _log(shop, message, "done", "order_tracking", "list_recent_orders", {"email": email}, result["reply"])
            return result
        return None

    # 2. OTP code for payment
    if ptype == "await_payment_otp":
        otp_match = _OTP_RE.search(message) or re.search(r"\b(\d{6})\b", message)
        if otp_match:
            otp_code = otp_match.group(1)
            result = await _execute_and_reply(store, "payment_checkout", "confirm_payment_otp", {"otp_code": otp_code}, language, message, session_id, customer_id=customer_id)
            _log(shop, message, result.get("status", "done"), "payment_checkout", "confirm_payment_otp", {"otp_code": otp_code}, result.get("reply", ""))
            return result
        return None

    # 3. Warranty claim details
    if ptype == "await_warranty_order":
        m = _ORDER_NO_RE.search(message)
        if m:
            order_no = m.group(1)
            issue = pending.get("entities", {}).get("issue_description")
            if not issue:
                out = _ask(key, "await_warranty_issue", "Ask the shopper what problem or damage they experienced with this product.",
                           language, message, "Got it. What issue are you experiencing with this item?",
                           entities={"order_number": order_no})
                return out
            entities = {"order_number": order_no, "issue_description": issue}
            result = await _execute_and_reply(store, "warranty_claim", "submit_claim", entities, language, message, session_id, customer_id=customer_id)
            return result
        return None

    if ptype == "await_warranty_issue":
        issue = message.strip()
        entities = {**pending.get("entities", {}), "issue_description": issue}
        result = await _execute_and_reply(store, "warranty_claim", "submit_claim", entities, language, message, session_id, customer_id=customer_id)
        return result

    # 4. Standard verify_email
    if ptype == "verify_email":
        email_match = EMAIL_RE.search(message)
        if not email_match:
            return None
        entities = {**pending["entities"], "email": email_match.group(0)}
        result = await _execute_and_reply(store, pending["intent"], pending["action"], entities, language, message, session_id, customer_id=customer_id)
        _arm_verification_if_needed(key, pending["intent"], pending["action"], entities, language, result)
        _log(shop, message, result.get("status", "done"), pending["intent"], pending["action"], entities, result.get("reply", ""))
        return result

    # 5. Search item
    if ptype == "await_search_item":
        term = extract_search_term(message) or message.strip()
        if customer_id:
            asyncio.create_task(customer_profiles.record_interaction(shop, customer_id, {"query": term}))
        result = await _execute_and_reply(store, "product_search", "search_products", {"query": term}, language, message, session_id, customer_id=customer_id)
        _log(shop, message, "done", "product_search", "search_products", {"query": term}, result["reply"])
        return result

    # 6. Order number for tracking
    if ptype == "await_order_number":
        m = _ORDER_NO_RE.search(message)
        if m:
            entities = {"order_number": m.group(1)}
            result = await _execute_and_reply(store, "order_tracking", "track_order", entities, language, message, session_id, customer_id=customer_id)
            _arm_verification_if_needed(key, "order_tracking", "track_order", entities, language, result)
            _log(shop, message, result.get("status", "done"), "order_tracking", "track_order", entities, result["reply"])
            return result
        return None

    # 7. Cart item add / remove
    is_remove = ptype == "await_remove_item"
    intent, action = "cart_management", ("remove_item" if is_remove else "add_item")

    if is_remove:
        name = extract_search_term(message) or message.strip()
        result = await _execute_and_reply(store, intent, action, {"product_name_or_id": name}, language, message, session_id, customer_id=customer_id)
        _log(shop, message, "done", intent, action, {"product_name_or_id": name}, result["reply"])
        return result

    product, options = await _resolve_product(store, message)
    if product:
        quantity = int((pending.get("entities") or {}).get("quantity") or 1)
        result = _add_to_cart_result(product, quantity, language, message)
        _log(shop, message, "done", intent, action, {"product_name_or_id": product["name"]}, result["reply"])
        return result
    if options:
        names = [p["name"] for p in options]
        out = _ask(key, ptype, f"Tell the shopper several products match ({', '.join(names)}) and ask which one they mean.",
                   language, message, f"I found a few matches — did you mean {', '.join(names)}?", asked=asked)
        _log(shop, message, "ambiguous", intent, action, {"product_name_or_id": message.strip()}, out["reply"])
        return out

    if asked < 2:
        out = _ask(key, ptype, "Say you didn't catch an item name and ask the shopper to type the name of the item they'd like to add to their cart.",
                   language, message, "Sorry, I didn't catch an item name. Please enter the name of the item you'd like to add to your cart.",
                   asked=asked + 1)
        _log(shop, message, "awaiting_item", intent, action, None, out["reply"])
        return out
    reply = _safe_reply("add_item", {"error": "not_found", "query": message.strip(),
                                     "instruction": "Say you couldn't find that item and suggest trying the exact product name."},
                        language, message, f'I couldn\'t find "{message.strip()}" — could you try the exact product name?')
    _log(shop, message, "not_found", intent, action, {"product_name_or_id": message.strip()}, reply)
    return {"status": "done", "reply": reply, "language": language}


# =======================================================================
# Routes
# =======================================================================
@router.post("/chat")
@observe(name="chat_request")
async def chat(req: ChatRequest):
    _CURRENT_SESSION_ID.set(req.session_id)
    _CURRENT_VISITOR_ID.set(req.visitor_id)
    result = await _chat_impl(req)
    try:
        text = (req.message or "").strip()
        if text and isinstance(result, dict):
            _remember_turn(_pkey(req.shop, req.session_id), text, result.get("reply") or "")
    except Exception as e:
        print(f"chatbot_widget: history update failed: {e!r}")
    return result


async def _chat_impl(req: ChatRequest):
    message = (req.message or "").strip()
    if not message and not req.otp_code:
        return {"reply": "Could you type or say something first?"}

    store = _get_store(req.shop)
    if not store:
        return {"reply": "Sorry, I couldn't verify this store. Please reload the page and try again."}

    cfg = repo.ensure_customization(store.id)
    if cfg.get("status", "active") == "inactive":
        return {"reply": "This assistant isn't available right now."}

    customer_id = (
        customer_profiles.read_session_token(req.customer_token, req.shop)
        or req.customer_id
        or _session_customer(req.shop, req.session_id)
    )
    customer_email = (
        (req.customer_email or "").strip().lower()
        or _session_customer_email(req.shop, req.session_id)
    )
    customer_name = (
        (req.customer_name or "").strip()
        or _session_customer_name(req.shop, req.session_id)
    )

    if req.customer_token and not customer_id:
        return {"reply": "", "session_expired": True}

    if customer_id and not customer_email:
        profile = await customer_profiles.get_profile(req.shop, customer_id)
        if profile and profile.get("email"):
            customer_email = profile["email"].strip().lower()
        if profile and profile.get("name") and not customer_name:
            customer_name = profile["name"].strip()

    _remember_customer(req.shop, req.session_id, customer_id, customer_email, customer_name)
    customer_profile = await customer_profiles.get_profile(req.shop, customer_id) if customer_id else None

    _prune_pending()
    key = _pkey(req.shop, req.session_id)
    pending = PENDING.get(key)

    if pending:
        PENDING.pop(key, None)
        if pending.get("type", "confirm") != "confirm":
            try:
                followup = await _handle_followup(store, req.shop, key, pending, message, req.session_id)
            except Exception as e:
                _span_error(str(e))
                print(f"chatbot_widget: follow-up error: {e!r}")
                _log(req.shop, message, "error")
                return {"reply": "Sorry, something went wrong completing that. Please try again."}
            if followup is not None:
                return followup
            pending = None

    if pending is not None:
        text = message.lower().strip(" .!")
        if text in _AFFIRMATIVE:
            result = await _execute_and_reply(store, pending["intent"], pending["action"], pending["entities"], pending["language"], message, req.session_id, customer_id=customer_id)
            _arm_verification_if_needed(key, pending["intent"], pending["action"], pending["entities"], pending["language"], result)
            _log(req.shop, message, result.get("status", "done"), pending["intent"], pending["action"], pending["entities"], result.get("reply", ""))
            return result
        if text in _NEGATIVE:
            cancel_reply = _safe_reply("cancelled", {"message": "The shopper decided not to proceed."}, pending["language"], message,
                                       fallback="No problem — that request was not submitted.")
            _log(req.shop, message, "cancelled", pending["intent"], pending["action"], pending["entities"], cancel_reply)
            return {"status": "cancelled", "reply": cancel_reply, "language": pending["language"]}

    # Fast-path for OTP in messages
    otp_match = _OTP_RE.search(message) or (re.search(r"\b(\d{6})\b", message) if len(message.strip()) == 6 else None)
    if (otp_match or req.otp_code) and _pkey(req.shop, req.session_id) in shopify_actions._PENDING_PAYMENTS:
        otp_to_verify = req.otp_code or (otp_match.group(1) if otp_match else message.strip())
        result = await _execute_and_reply(store, "payment_checkout", "confirm_payment_otp", {"otp_code": otp_to_verify}, "en", message, req.session_id, customer_id=customer_id)
        _log(req.shop, message, result.get("status", "done"), "payment_checkout", "confirm_payment_otp", {"otp_code": otp_to_verify}, result.get("reply", ""))
        return result

    # 1. Fast-path pattern overrides (avoids LLM latency, outages, or token errors)
    if _PAY_RE.search(message) or _CHECKOUT_RE.search(message):
        intent, action = "payment_checkout", "initiate_payment"
        entities = {}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    elif _VIEW_CART_RE.search(message):
        intent, action = "cart_management", "view_cart"
        entities = {}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    elif _MY_ORDERS_RE.search(message):
        intent, action = "order_tracking", "list_recent_orders"
        entities = {}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    elif _SEARCH_LEAD_IN.search(message):
        intent, action = "product_search", "search_products"
        entities = {"query": extract_search_term(message)}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    elif re.search(r"\b(snowboard|snowboards|ski|skis|board|boards|boot|boots|jacket|hoodie|shirt|tee)\b", message, re.IGNORECASE) and not _MY_ORDERS_RE.search(message):
        intent, action = "product_search", "search_products"
        entities = {"query": extract_search_term(message)}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    elif _GREETING_RE.search(message.strip()):
        intent, action = "general_chat", "assistant_help"
        entities = {}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    elif _POPULAR_RE.search(message.strip()):
        intent, action = "general_chat", "recommend_products"
        entities = {}
        language = "en"
        classification = {"intent": intent, "action": action, "entities": entities, "language": language, "requires_confirmation": False}
    else:
        # Nuanced intent classification via schema & LLM
        try:
            classification = classify_intent(message, SCHEMA, history=_history_for(key))
        except Exception as e:
            _span_error(str(e))
            print(f"chatbot_widget: classify_intent error: {e!r}")
            classification = {"intent": "fallback", "action": "clarify", "entities": {}, "language": "en", "requires_confirmation": False}

        if classification.get("intent") == "fallback" and semantic.enabled():
            try:
                routed = await semantic.route_intent(message)
            except Exception as e:
                print(f"chatbot_widget: semantic router failed: {e!r}")
                routed = None
            if routed and (routed["intent"], routed["action"]) in _RESCUABLE:
                classification = {
                    **classification,
                    "intent": routed["intent"], "action": routed["action"],
                    "entities": _rescue_entities(routed["intent"], routed["action"], message),
                    "confidence": routed["score"], "requires_confirmation": False,
                }

        intent = classification["intent"]
        action = classification["action"]
        entities = classification.get("entities") or {}
        language = classification.get("language", "en")

    if intent == "order_tracking" and not (entities.get("order_number") or entities.get("order_id")):
        m = _ORDER_NO_RE.search(message)
        if m:
            entities["order_number"] = m.group(1)

    if (intent, action) == ("product_search", "search_products") and not entities.get("query"):
        extracted = extract_search_term(message)
        if extracted:
            entities["query"] = extracted

    try:
        if customer_email and not entities.get("email"):
            entities["email"] = customer_email
        if customer_id and not entities.get("customer_id"):
            entities["customer_id"] = str(customer_id)
        if customer_name and not entities.get("customer_name"):
            entities["customer_name"] = str(customer_name)

        parsed_cart = _parse_storefront_cart(req.cart_data)
        if parsed_cart:
            entities["cart_data"] = parsed_cart
            if parsed_cart.get("items") and not entities.get("items"):
                entities["items"] = parsed_cart["items"]
            if parsed_cart.get("total") and (not entities.get("amount") or entities.get("amount") == "0.00"):
                entities["amount"] = parsed_cart["total"]
            if parsed_cart.get("currency") and not entities.get("currency"):
                entities["currency"] = parsed_cart["currency"]

        # 1. Order History: Show all orders
        if (intent, action) in (("order_tracking", "list_recent_orders"), ("customer_account", "get_my_orders")):
            if not customer_id and not entities.get("email") and not customer_email:
                email_found = EMAIL_RE.search(message)
                if email_found:
                    entities["email"] = email_found.group(0).lower()
                else:
                    out = _ask(key, "await_order_email", "Ask the shopper for the email address used on their account so their orders can be listed.",
                               language, message, "Please provide the email address used for your orders so I can display your order history.")
                    _log(req.shop, message, "awaiting_email", intent, action, None, out["reply"])
                    return out

        # 2. Track Order
        if intent == "order_tracking" and action == "track_order":
            has_number = bool(entities.get("order_number") or entities.get("order_id") or _ORDER_NO_RE.search(message))
            if not has_number and not customer_id and not customer_email and not entities.get("email"):
                out = _ask(key, "await_order_number", "Ask the shopper for their order number (for example #1001).",
                           language, message, "Sure — what's your order number?", asked=1)
                _log(req.shop, message, "awaiting_order", intent, action, None, out["reply"])
                return out

        # 3. Direct Cart Management
        if (intent, action) == ("cart_management", "add_item"):
            term = entities.get("product_name_or_id")
            if not term:
                term = extract_search_term(message)
            if not term:
                out = _ask(key, "await_item", "Ask the shopper which item they would like to add to their cart.",
                           language, message, "Sure — which item would you like to add to your cart?", asked=1)
                _log(req.shop, message, "awaiting_item", intent, action, None, out["reply"])
                return out
            product, options = await _resolve_product(store, str(term))
            quantity = int(entities.get("quantity") or 1)
            if product:
                result = _add_to_cart_result(product, quantity, language, message)
                _log(req.shop, message, "done", intent, action, {**entities, "product_name_or_id": product["name"]}, result["reply"])
                return result
            if options:
                names = [p["name"] for p in options]
                out = _ask(key, "await_item", f"Tell the shopper several products match ({', '.join(names)}) and ask which one they mean.",
                           language, message, f"I found a few matches — did you mean {', '.join(names)}?", asked=1)
                _log(req.shop, message, "ambiguous", intent, action, entities, out["reply"])
                return out

        if (intent, action) == ("cart_management", "remove_item") and not entities.get("product_name_or_id"):
            term = extract_search_term(message)
            if not term:
                out = _ask(key, "await_remove_item", "Ask the shopper which item they would like to remove from their cart.",
                           language, message, "Sure — which item would you like to remove from your cart?", asked=1)
                _log(req.shop, message, "awaiting_item", intent, action, None, out["reply"])
                return out
            entities["product_name_or_id"] = term

        # 4. Warranty Claim
        if intent == "warranty_claim" and action == "submit_claim":
            m = _ORDER_NO_RE.search(message)
            if m:
                entities["order_number"] = m.group(1)
            if not entities.get("order_number"):
                out = _ask(key, "await_warranty_order", "Ask the shopper for their order number to file the warranty claim.",
                           language, message, "What is the order number for the item you'd like to file a warranty claim for?")
                return out
            if not entities.get("issue_description"):
                cleaned = re.sub(r"#?\s*\d{3,}", "", message).strip()
                if len(cleaned) > 10:
                    entities["issue_description"] = cleaned
                else:
                    out = _ask(key, "await_warranty_issue", "Ask the shopper to describe the defect or issue with their product.",
                               language, message, "Could you describe the issue or damage with the item?",
                               entities={"order_number": entities["order_number"]})
                    return out

        # Feature checks
        if not _feature_enabled(store.id, intent, action, entities):
            reply = _safe_reply("feature_disabled", {"message": "This store has turned this feature off."}, language, message,
                                fallback="Sorry, I can't help with that on this store.")
            _log(req.shop, message, "feature_disabled", intent, action, entities, reply)
            return {"status": "done", "reply": reply, "language": language}

    except Exception as e:
        _span_error(str(e))
        print(f"chatbot_widget: pre-dispatch error: {e!r}")
        _log(req.shop, message, "error", intent, action, entities)
        return {"reply": "Sorry, something went wrong completing that. Please try again."}

    if classification.get("requires_confirmation"):
        _set_pending(key, type="confirm", intent=intent, action=action, entities=entities, language=language)
        confirm_reply = _safe_reply(
            action,
            {"pending_action": action, "details": entities,
             "instruction": "Ask the shopper to reply yes to confirm or no to cancel before this action is taken."},
            language, message, fallback="Should I go ahead? Please reply yes to confirm or no to cancel.",
        )
        _log(req.shop, message, "confirmation_required", intent, action, entities, confirm_reply)
        return {"status": "confirmation_required", "reply": confirm_reply, "language": language}

    try:
        entities["visitor_id"] = req.visitor_id
        result = await _execute_and_reply(store, intent, action, entities, language, message, req.session_id, customer_id=customer_id)
        _arm_verification_if_needed(key, intent, action, entities, language, result)

        if action == "initiate_payment" and result.get("status") == "otp_required":
            _set_pending(key, type="await_payment_otp", intent="payment_checkout", action="confirm_payment_otp", entities=entities, language=language, otp=result.get("otp"))

        _log(req.shop, message, result.get("status", "done"), intent, action, entities, result.get("reply", ""))
        return result
    except Exception as e:
        _span_error(str(e))
        print(f"chatbot_widget: dispatch error: {e}")
        _log(req.shop, message, "error", intent, action, entities)
        return {"reply": "Sorry, something went wrong completing that. Please try again."}


@router.post("/confirm")
@observe(name="confirm_request")
async def confirm(req: ConfirmRequest):
    _CURRENT_SESSION_ID.set(req.session_id)
    _CURRENT_VISITOR_ID.set(req.visitor_id)
    store = _get_store(req.shop)
    if not store:
        return {"reply": "Sorry, I couldn't verify this store."}

    cfg = repo.ensure_customization(store.id)
    if cfg.get("status", "active") == "inactive":
        return {"reply": "This assistant isn't available right now."}

    customer_id = customer_profiles.read_session_token(req.customer_token, req.shop) or req.customer_id or _session_customer(req.shop, req.session_id)
    customer_email = (req.customer_email or "").strip().lower() or _session_customer_email(req.shop, req.session_id)
    customer_name = (req.customer_name or "").strip() or _session_customer_name(req.shop, req.session_id)
    _remember_customer(req.shop, req.session_id, customer_id, customer_email, customer_name)

    _prune_pending()
    pending = PENDING.pop(_pkey(req.shop, req.session_id), None)
    if not pending or pending.get("type", "confirm") != "confirm":
        return {"status": "expired", "reply": "That request has expired — please ask again."}

    if not req.confirmed:
        cancel_reply = _safe_reply("cancelled", {"message": "The shopper declined."}, pending["language"], "cancel",
                                   fallback="No problem — that request was not submitted.")
        _log(req.shop, "confirmed=false", "cancelled", pending["intent"], pending["action"], pending["entities"], cancel_reply)
        return {"status": "cancelled", "reply": cancel_reply, "language": pending["language"]}

    try:
        result = await _execute_and_reply(store, pending["intent"], pending["action"], pending["entities"], pending["language"], "confirmed", req.session_id, customer_id=customer_id)
        _arm_verification_if_needed(_pkey(req.shop, req.session_id), pending["intent"], pending["action"], pending["entities"], pending["language"], result)
    except Exception as e:
        get_client().update_current_span(level="ERROR", status_message=str(e))
        print(f"chatbot_widget: confirm dispatch error: {e}")
        _log(req.shop, "confirmed=true", "error", pending["intent"], pending["action"], pending["entities"])
        return {"reply": "Sorry, something went wrong completing that. Please try again."}
    _log(req.shop, "confirmed=true", result.get("status", "done"), pending["intent"], pending["action"], pending["entities"], result.get("reply", ""))
    return result


@router.post("/save-payment-credentials")
async def save_payment_credentials_endpoint(req: PaymentCredentialsRequest):
    store = _get_store(req.shop)
    if not store:
        return {"error": "unknown_store"}
    customer_id = customer_profiles.read_session_token(req.customer_token, req.shop) or req.customer_id or _session_customer(req.shop, req.session_id)
    customer_email = (req.customer_email or "").strip().lower() or _session_customer_email(req.shop, req.session_id)
    customer_name = (req.customer_name or "").strip() or _session_customer_name(req.shop, req.session_id)
    _remember_customer(req.shop, req.session_id, customer_id, customer_email, customer_name)
    entities = {
        "cardholder_name": req.cardholder_name,
        "card_number": req.card_number,
        "expiry": req.expiry,
        "cvv": req.cvv,
    }
    return await shopify_actions.save_payment_credentials(store, entities, req.session_id, customer_id=customer_id)


@router.post("/initiate-payment")
async def initiate_payment_endpoint(req: InitiatePaymentRequest):
    store = _get_store(req.shop)
    if not store:
        return {"error": "unknown_store"}
    customer_id = customer_profiles.read_session_token(req.customer_token, req.shop) or req.customer_id or _session_customer(req.shop, req.session_id)
    customer_email = (req.customer_email or "").strip().lower() or _session_customer_email(req.shop, req.session_id)
    customer_name = (req.customer_name or "").strip() or _session_customer_name(req.shop, req.session_id)
    _remember_customer(req.shop, req.session_id, customer_id, customer_email, customer_name)
    entities = {}
    if req.cart_data:
        parsed = _parse_storefront_cart(req.cart_data)
        if parsed:
            entities["cart_data"] = parsed
            if parsed.get("items"):
                entities["items"] = parsed["items"]
            if parsed.get("total"):
                entities["amount"] = parsed["total"]
            if parsed.get("currency"):
                entities["currency"] = parsed["currency"]
    result = await shopify_actions.initiate_payment(store, entities, req.session_id, customer_id=customer_id)
    if result.get("status") == "otp_required":
        key = _pkey(req.shop, req.session_id)
        _set_pending(key, type="await_payment_otp", intent="payment_checkout", action="confirm_payment_otp", entities=entities, language="en", otp=result.get("otp"))
    return result


@router.post("/verify-otp")
async def verify_otp_endpoint(req: VerifyOtpRequest):
    store = _get_store(req.shop)
    if not store:
        return {"error": "unknown_store"}
    customer_id = customer_profiles.read_session_token(req.customer_token, req.shop) or req.customer_id or _session_customer(req.shop, req.session_id)
    customer_email = (req.customer_email or "").strip().lower() or _session_customer_email(req.shop, req.session_id)
    customer_name = (req.customer_name or "").strip() or _session_customer_name(req.shop, req.session_id)
    _remember_customer(req.shop, req.session_id, customer_id, customer_email, customer_name)
    result = await shopify_actions.confirm_payment_otp(store, {"otp_code": req.otp_code}, req.session_id, customer_id=customer_id)
    if result.get("status") == "payment_success":
        key = _pkey(req.shop, req.session_id)
        PENDING.pop(key, None)
    return result


@router.get("/customer-session")
async def customer_session(request: Request):
    if not customer_profiles.verify_app_proxy(request.query_params.multi_items()):
        return {"authenticated": False, "error": "invalid_app_proxy_signature"}
    shop = request.query_params.get("shop", "")
    customer_id = request.query_params.get("logged_in_customer_id") or ""
    if not shop or not customer_id:
        return {"authenticated": False, "shop": shop}
    store = _get_store(shop)
    if not store:
        return {"authenticated": False, "error": "unknown_store"}
    token = customer_profiles.make_session_token(shop, customer_id)
    profile = await customer_profiles.get_profile(shop, customer_id) or {}
    customer = await shopify_actions.get_customer(store, customer_id)
    if customer:
        email = ((customer.get("defaultEmailAddress") or {}).get("emailAddress")) or ""
        fields = {"first_name": customer.get("firstName") or "", "last_name": customer.get("lastName") or "",
                  "email": email, "order_count": int(customer.get("numberOfOrders") or 0), "synced_at": int(time.time())}
        stale = (int(profile.get("synced_at") or 0) + 3600) < int(time.time())
        if stale or not profile.get("purchased_product_ids"):
            order_data = await shopify_actions.get_customer_orders(store, customer_id, first=50, max_orders=50)
            if not order_data.get("error"):
                fields["purchased_product_ids"] = order_data.get("purchased_product_ids") or []
            names = []
            variants = []
            for order in (order_data.get("active_orders") or []) + (order_data.get("past_orders") or []):
                for item in order.get("items") or []:
                    if item.get("name"):
                        names.append(item["name"])
                    if item.get("variant"):
                        variants.append(item["variant"])
            if names:
                fields["top_types"] = list(dict.fromkeys(names))[:20]
            if variants:
                fields["top_tags"] = list(dict.fromkeys(variants))[:20]
        await customer_profiles.save_profile(shop, customer_id, fields)
        profile = {**profile, **fields}
    return {"authenticated": True, "customer_id": str(customer_id), "customer_token": token,
            "first_name": (customer or {}).get("firstName") or profile.get("first_name") or ""}


@router.get("/widget-config")
async def widget_config(shop: str):
    store = repo.get_store(shop)
    if not store:
        return {"error": "unknown store"}
    cfg = repo.ensure_customization(store["$id"])
    agent_name = cfg.get("agent_name", "AI Assistant")
    agent_title = cfg.get("agent_title", "How can I help you today?")
    greeting = cfg.get("greeting_message") or (
        f"Hey there! I'm {agent_name}. How can I help you today?"
        if agent_title == "How can I help you today?"
        else f"Hey there! I'm {agent_name}. {agent_title}"
    )
    return {
        "status": cfg.get("status", "active"),
        "agent_name": agent_name,
        "agent_title": agent_title,
        "greeting_message": greeting,
        "icon_type": cfg.get("icon_type", "preset"),
        "theme_color": cfg.get("theme_color", "#2b2b2b"),
        "custom_icon_url": cfg.get("custom_icon_url", ""),
        "customer_session_path": os.environ.get("SHOPIFY_APP_PROXY_PATH", "/apps/ai-agent/customer-session"),
    }


def _extract_name_from_email(email: str) -> tuple[str, str]:
    if not email or "@" not in email:
        return "", ""
    local = email.split("@")[0]
    clean = re.sub(r"[0-9._-]+.*$", "", local).strip()
    if not clean:
        clean = re.sub(r"[0-9]+", "", local).strip("._-")
    if not clean:
        return "", ""
    surnames = r"(thakur|sharma|singh|kumar|patel|gupta|verma|khan|reddy|smith|johnson|williams|brown|jones|miller|davis)"
    m = re.match(rf"^([a-zA-Z]{{3,15}}?){surnames}$", clean, re.IGNORECASE)
    if m:
        first = m.group(1).capitalize()
        last = m.group(2).capitalize()
        return first, f"{first} {last}"
    capitalized = clean.capitalize()
    return capitalized, capitalized


@router.get("/customer-profile")
async def get_customer_profile(shop: str = "", customer_id: str = None, email: str = None):
    if not shop:
        return {"found": False}
    store = _get_store(shop)
    if not store:
        return {"found": False}

    cid = str(customer_id or "").strip()
    mail = (email or "").strip().lower()
    fn = ""
    ln = ""
    name = ""

    if cid:
        try:
            cust = await shopify_actions.get_customer(store, cid)
            if cust:
                fn = (cust.get("firstName") or "").strip()
                ln = (cust.get("lastName") or "").strip()
                name = (f"{fn} {ln}").strip() or fn or ln
                cust_email = ((cust.get("defaultEmailAddress") or {}).get("emailAddress")) or ""
                if cust_email:
                    mail = cust_email.strip().lower()
        except Exception as e:
            print(f"customer-profile by id failed: {e!r}")

    if not fn and mail:
        try:
            query = """
            query CustomerByEmail($query: String!) {
              customers(first: 1, query: $query) {
                nodes {
                  id firstName lastName displayName
                  defaultEmailAddress { emailAddress }
                }
              }
            }
            """
            data = await shopify_actions._graphql_admin(store, query, {"query": f"email:{mail}"})
            nodes = ((data or {}).get("customers") or {}).get("nodes") or []
            if nodes:
                c = nodes[0]
                if not cid:
                    cid = str(c.get("id") or "").split("/")[-1]
                fn = (c.get("firstName") or "").strip()
                ln = (c.get("lastName") or "").strip()
                name = (f"{fn} {ln}").strip() or fn or (c.get("displayName") or "").strip()
        except Exception as e:
            print(f"customer-profile by email failed: {e!r}")

    # Fallback name derivation from email handle if customer record in Shopify lacks firstName
    if not fn and mail:
        derived_fn, derived_full = _extract_name_from_email(mail)
        if derived_fn:
            fn = derived_fn
            name = name or derived_full

    if fn or mail or cid:
        return {
            "found": True,
            "first_name": fn,
            "last_name": ln,
            "name": name or fn,
            "email": mail,
            "customer_id": cid,
        }
    return {"found": False}


@router.get("/widget.js")
async def widget_js():
    return Response(
        content=WIDGET_JS,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# --------------------------------------------------------------------------
# Embeddable widget JS & rich UI components
# --------------------------------------------------------------------------
WIDGET_JS = r"""
(function () {
  "use strict";

  var THIS_SCRIPT = document.currentScript;
  var SHOP = (THIS_SCRIPT && THIS_SCRIPT.dataset.shop) || "";
  var ORIGIN = THIS_SCRIPT ? new URL(THIS_SCRIPT.src).origin : "";
  var CFG = {
    chatEndpoint: ORIGIN + "/chat",
    confirmEndpoint: ORIGIN + "/confirm",
    saveCredentialsEndpoint: ORIGIN + "/save-payment-credentials",
    initiatePaymentEndpoint: ORIGIN + "/initiate-payment",
    verifyOtpEndpoint: ORIGIN + "/verify-otp",
    configEndpoint: ORIGIN + "/widget-config?shop=" + encodeURIComponent(SHOP),
    customerProfileEndpoint: ORIGIN + "/customer-profile?shop=" + encodeURIComponent(SHOP),
    customerSessionPath: (THIS_SCRIPT && THIS_SCRIPT.dataset.proxyPath) || "/apps/ai-agent/customer-session"
  };

  if (!SHOP) { console.warn("[chat widget] missing data-shop attribute on script tag"); return; }
  if (document.getElementById("ai-chat-widget-root")) return;

  var style = document.createElement("style");
  style.textContent = [
    "#ai-chat-widget-root, #ai-chat-widget-root * { box-sizing:border-box; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }",
    "#ai-chat-widget-root .chat-fab { position:fixed; bottom:18px; right:18px; width:44px; height:44px; border-radius:50%; border:none; box-shadow:0 6px 18px rgba(0,0,0,.25); cursor:pointer; display:flex; align-items:center; justify-content:center; z-index:2147483000; transition:transform .2s cubic-bezier(.34,1.56,.64,1); background-size:cover; background-position:center; }",
    "#ai-chat-widget-root .chat-fab:hover { transform:scale(1.08); }",
    "#ai-chat-widget-root .chat-fab svg { width:20px; height:20px; stroke:#fff; }",
    "#ai-chat-widget-root .chat-fab .icon-close { display:none; }",
    "#ai-chat-widget-root .chat-fab.open .icon-mic { display:none; }",
    "#ai-chat-widget-root .chat-fab.open .icon-close { display:block; }",
    "#ai-chat-widget-root .chat-fab.custom-icon .icon-mic { display:none; }",
    "#ai-chat-widget-root .fab-greeting { position:fixed; bottom:20px; right:70px; width:max-content; max-width:min(230px, calc(100vw - 86px)); background:#ffffff; color:#111827; padding:8px 24px 8px 12px; border-radius:14px; border-bottom-right-radius:3px; box-shadow:0 6px 20px rgba(0,0,0,.10), 0 1px 3px rgba(0,0,0,.04); border:1px solid rgba(0,0,0,.08); z-index:2147482999; cursor:pointer; opacity:0; transform:translateY(6px) scale(.97); pointer-events:none; transition:opacity .25s cubic-bezier(.16,1,.3,1), transform .25s cubic-bezier(.16,1,.3,1), box-shadow .2s ease; display:none; align-items:center; }",
    "#ai-chat-widget-root .fab-greeting:hover { transform:translateY(-1px) scale(1.01); box-shadow:0 10px 28px rgba(0,0,0,.14); }",
    "#ai-chat-widget-root .fab-greeting.show { opacity:1; transform:translateY(0) scale(1); pointer-events:auto; display:flex !important; }",
    "#ai-chat-widget-root .fab-greeting::after { content:''; position:absolute; bottom:11px; right:-6px; width:0; height:0; border-top:5px solid transparent; border-bottom:5px solid transparent; border-left:6px solid #ffffff; filter:drop-shadow(2px 0 1px rgba(0,0,0,.04)); }",
    "#ai-chat-widget-root .fg-close { position:absolute; top:4px; right:5px; width:16px; height:16px; display:flex; align-items:center; justify-content:center; background:transparent; border:none; color:#9ca3af; font-size:13px; line-height:1; cursor:pointer; padding:0; border-radius:50%; transition:color .15s ease, background .15s ease; }",
    "#ai-chat-widget-root .fg-close:hover { color:#111827; background:rgba(0,0,0,.06); }",
    "#ai-chat-widget-root .fg-text { font-size:11.5px; font-weight:500; color:#1f2937; line-height:1.35; word-break:break-word; }",
    "#ai-chat-widget-root .widget { position:fixed; bottom:max(72px, env(safe-area-inset-bottom) + 60px); right:18px; width:min(340px, calc(100vw - 24px)); max-height:min(540px, calc(100vh - 90px)); background:rgba(255,255,255,.88); backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px); border:1.5px solid rgba(0,0,0,.12); border-radius:18px; padding:12px; box-shadow:0 14px 34px rgba(0,0,0,.16); transform-origin:bottom right; transform:scale(.9) translateY(10px); opacity:0; pointer-events:none; transition:transform .2s cubic-bezier(.2,.9,.3,1.2), opacity .15s ease, width .25s ease, max-height .25s ease; z-index:2147483000; display:flex; flex-direction:column; }",
    "#ai-chat-widget-root .widget.open { transform:scale(1) translateY(0); opacity:1; pointer-events:auto; }",
    "#ai-chat-widget-root .widget.expanded { width:min(420px, calc(100vw - 24px)); max-height:min(640px, calc(100vh - 40px)); }",
    "#ai-chat-widget-root .header { display:flex; align-items:center; gap:8px; margin-bottom:10px; }",
    "#ai-chat-widget-root .avatar { width:26px; height:26px; border-radius:50%; color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:10px; background-size:cover; background-position:center; }",
    "#ai-chat-widget-root .header h1 { margin:0; font-size:13px; font-weight:700; color:#1a1a1a; }",
    "#ai-chat-widget-root .header-cust-badge { display:none; align-items:center; font-size:9.5px; font-weight:600; color:#166534; background:#dcfce7; border:1px solid #bbf7d0; padding:2px 7px; border-radius:999px; max-width:115px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-left:4px; }",
    "#ai-chat-widget-root .header-right { margin-left:auto; display:flex; align-items:center; gap:6px; }",
    "#ai-chat-widget-root .icon-btn { position:relative; width:24px; height:24px; border-radius:50%; background:#f5f5f5; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; }",
    "#ai-chat-widget-root .icon-btn svg { width:14px; height:14px; stroke:#444; }",
    "#ai-chat-widget-root .icon-btn.active { background:#2b2b2b; }",
    "#ai-chat-widget-root .icon-btn.active svg { stroke:#fff; }",
    "#ai-chat-widget-root .cart-badge { position:absolute; top:-4px; right:-4px; background:#d64545; color:#fff; font-size:9.5px; font-weight:700; min-width:16px; height:16px; border-radius:999px; display:none; align-items:center; justify-content:center; padding:0 3px; }",
    "#ai-chat-widget-root .cart-badge.show { display:flex; }",
    "#ai-chat-widget-root #expandToggle .icon-collapse { display:none; }",
    "#ai-chat-widget-root #expandToggle.active-expand .icon-expand { display:none; }",
    "#ai-chat-widget-root #expandToggle.active-expand .icon-collapse { display:block; }",
    "#ai-chat-widget-root .history-divider { text-align:center; font-size:10px; font-weight:600; color:#888; margin:8px 0 4px; }",

    "#ai-chat-widget-root .order-card { background:#fff; border:1px solid rgba(0,0,0,.1); border-radius:12px; padding:10px; margin:6px 0; font-size:11px; color:#222; box-shadow:0 2px 8px rgba(0,0,0,.04); }",
    "#ai-chat-widget-root .order-card-top { display:flex; align-items:center; justify-content:space-between; gap:8px; }",
    "#ai-chat-widget-root .order-status { font-size:9.5px; font-weight:600; padding:3px 7px; border-radius:999px; background:#f0f4f8; color:#1e40af; text-transform:capitalize; }",
    "#ai-chat-widget-root .order-meta { color:#777; margin-top:3px; font-size:10px; }",
    "#ai-chat-widget-root .order-items { margin-top:5px; line-height:1.4; color:#333; font-weight:500; }",
    "#ai-chat-widget-root .order-total { margin-top:5px; font-weight:700; color:#111; font-size:11.5px; }",
    "#ai-chat-widget-root .order-actions { display:flex; gap:6px; margin-top:8px; }",
    "#ai-chat-widget-root .order-act-btn { flex:1; border:1px solid #ddd; background:#fafafa; color:#222; font-size:10px; font-weight:600; padding:4px 8px; border-radius:6px; cursor:pointer; text-align:center; }",
    "#ai-chat-widget-root .order-act-btn:hover { background:#f0f0f0; border-color:#bbb; }",

    "#ai-chat-widget-root .otp-box { background:#fff; border:1.5px solid #2563eb; border-radius:12px; padding:12px; margin:6px 0; font-size:11px; color:#222; box-shadow:0 4px 12px rgba(37,99,235,.08); }",
    "#ai-chat-widget-root .otp-title { font-weight:700; font-size:12px; color:#1d4ed8; margin-bottom:4px; display:flex; align-items:center; gap:5px; }",
    "#ai-chat-widget-root .otp-detail { color:#555; margin-bottom:8px; line-height:1.4; }",
    "#ai-chat-widget-root .otp-row { display:flex; gap:6px; }",
    "#ai-chat-widget-root .otp-input { flex:1; padding:7px 10px; border:1.5px solid #ccc; border-radius:6px; font-size:13px; font-weight:700; letter-spacing:2px; text-align:center; outline:none; }",
    "#ai-chat-widget-root .otp-input:focus { border-color:#2563eb; }",
    "#ai-chat-widget-root .otp-btn { background:#2563eb; color:#fff; border:none; border-radius:6px; padding:0 12px; font-size:11px; font-weight:700; cursor:pointer; }",
    "#ai-chat-widget-root .otp-btn:hover { background:#1d4ed8; }",

    "#ai-chat-widget-root .pay-form-box { background:#fff; border:1px solid #ddd; border-radius:12px; padding:12px; margin:6px 0; font-size:11px; color:#222; }",
    "#ai-chat-widget-root .pay-form-title { font-weight:700; font-size:12px; margin-bottom:8px; color:#111; }",
    "#ai-chat-widget-root .pay-form-group { margin-bottom:6px; }",
    "#ai-chat-widget-root .pay-form-label { display:block; font-size:9.5px; font-weight:600; color:#666; margin-bottom:2px; }",
    "#ai-chat-widget-root .pay-form-input { width:100%; padding:6px 8px; border:1px solid #ccc; border-radius:6px; font-size:11px; outline:none; }",
    "#ai-chat-widget-root .pay-form-row { display:flex; gap:6px; }",
    "#ai-chat-widget-root .pay-form-submit { width:100%; margin-top:6px; background:#16a34a; color:#fff; border:none; border-radius:6px; padding:7px 0; font-size:11px; font-weight:700; cursor:pointer; }",

    "#ai-chat-widget-root .receipt-box { background:#f0fdf4; border:1.5px solid #16a34a; border-radius:12px; padding:12px; margin:6px 0; font-size:11px; color:#14532d; }",
    "#ai-chat-widget-root .receipt-title { font-weight:700; font-size:13px; color:#15803d; margin-bottom:4px; }",
    "#ai-chat-widget-root .receipt-line { margin:3px 0; }",

    "#ai-chat-widget-root .conversation { flex:1; min-height:0; overflow-y:auto; overflow-x:hidden; display:flex; flex-direction:column; gap:8px; margin-bottom:10px; padding-right:2px; }",
    "#ai-chat-widget-root .bubble { box-sizing:border-box !important; display:block !important; height:auto !important; max-height:none !important; overflow:visible !important; border-radius:12px; padding:8px 11px; font-size:11px; line-height:1.45; width:fit-content; max-width:92%; overflow-wrap:anywhere; word-break:break-word; white-space:pre-wrap; min-width:0; }",
    "#ai-chat-widget-root .bubble.bot { background:rgba(255,255,255,.9) !important; border:1px solid rgba(220,220,220,.8); color:#222; box-shadow:0 2px 8px rgba(0,0,0,.05); align-self:flex-start; }",
    "#ai-chat-widget-root .bubble.user { background:#2b2b2b !important; color:#fff; align-self:flex-end; }",
    "#ai-chat-widget-root .bubble.typing { color:#999; font-style:italic; }",
    "#ai-chat-widget-root #greetingBubble { font-size:14px; font-weight:600; line-height:1.5; }",

    "#ai-chat-widget-root .quick-actions { display:flex; flex-direction:column; gap:6px; align-self:flex-start; max-width:95%; }",
    "#ai-chat-widget-root .quick-action-btn { border:1px solid #e2e2e2; background:#fafafa; color:#2b2b2b; font-size:11px; font-weight:600; padding:6px 10px; border-radius:999px; text-align:left; cursor:pointer; opacity:0; transform:translateY(6px); transition:opacity .28s ease, transform .28s ease, background .15s ease; }",
    "#ai-chat-widget-root .quick-action-btn.show { opacity:1; transform:translateY(0); }",
    "#ai-chat-widget-root .quick-action-btn:hover { background:#f0f0f0; }",

    "#ai-chat-widget-root .product-row { display:flex; flex-shrink:0; gap:8px; overflow-x:auto; overflow-y:visible; padding:2px 2px 4px; align-self:flex-start; max-width:100%; min-height:fit-content; }",
    "#ai-chat-widget-root .product-card { flex:0 0 auto; width:120px; border:1px solid #ececec; border-radius:10px; padding:7px; background:#fff; box-shadow:0 2px 8px rgba(0,0,0,.05); display:flex; flex-direction:column; gap:4px; cursor:pointer; transition:box-shadow .15s ease, transform .15s ease; }",
    "#ai-chat-widget-root .product-card:hover { box-shadow:0 4px 12px rgba(0,0,0,.1); transform:translateY(-1px); }",
    "#ai-chat-widget-root .product-card img { width:100%; height:75px; object-fit:cover; border-radius:6px; background:#f2f2f2; }",
    "#ai-chat-widget-root .product-card .p-name { font-size:10.5px; font-weight:600; color:#222; max-height:28px; overflow:hidden; }",
    "#ai-chat-widget-root .product-card .p-price { font-size:11px; font-weight:700; color:#2b2b2b; }",
    "#ai-chat-widget-root .product-card button { margin-top:2px; border:none; background:#2b2b2b; color:#fff; font-size:10px; padding:4px 0; border-radius:999px; cursor:pointer; }",
    "#ai-chat-widget-root .product-card button:disabled { background:#9c9c9c; }",

    "#ai-chat-widget-root .confirm-row { display:flex; gap:8px; align-self:flex-start; }",
    "#ai-chat-widget-root .confirm-btn { border:1px solid rgba(236,236,236,.8); background:rgba(255,255,255,.75); color:#2a2a2a; font-size:11px; font-weight:700; padding:6px 16px; border-radius:999px; cursor:pointer; }",
    "#ai-chat-widget-root .confirm-btn:hover:not(:disabled) { background:#2b2b2b; color:#fff; }",

    "#ai-chat-widget-root .input-row { position:relative; margin-top:10px; }",
    "#ai-chat-widget-root .input-row input { width:100%; padding:11px 42px 11px 13px; border-radius:999px; border:1px solid #e5e5e5; background:#fff; font-size:12px; color:#333; outline:none; box-shadow:0 2px 8px rgba(0,0,0,.05); }",
    "#ai-chat-widget-root .mic-btn { position:absolute; right:5px; top:50%; transform:translateY(-50%); width:31px; height:31px; border-radius:50%; background:#2b2b2b; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; }",
    "#ai-chat-widget-root .mic-btn.listening { background:#d64545; }",
    "#ai-chat-widget-root .mic-btn svg { width:17px; height:17px; stroke:#fff; }",
    "#ai-chat-widget-root .mic-btn .icon-send-inner { display:none; }",
    "#ai-chat-widget-root .mic-btn.has-text .icon-mic-inner { display:none; }",
    "#ai-chat-widget-root .mic-btn.has-text .icon-send-inner { display:block; }",
    "#ai-chat-widget-root .mic-status { font-size:9.5px; color:#b04040; margin-top:4px; min-height:12px; }"
  ].join("\n");
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.id = "ai-chat-widget-root";
  root.innerHTML =
    '<button class="chat-fab" id="chatFab" aria-label="Open chat">' +
      '<svg class="icon-mic" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>' +
      '<svg class="icon-close" viewBox="0 0 24 24" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
    "</button>" +
    '<div class="fab-greeting" id="fabGreeting" style="display:none;">' +
      '<button class="fg-close" id="fgClose" aria-label="Close" title="Dismiss">&times;</button>' +
      '<div class="fg-text" id="fgText">Hi there! How can I help you today?</div>' +
    '</div>' +
    '<div class="widget" id="chatWidget">' +
      '<div class="header">' +
        '<div class="avatar" id="headerAvatar">AI</div>' +
        '<h1 id="headerName">AI Assistant</h1>' +
        '<div class="header-right">' +
          '<button class="icon-btn" id="ttsToggle" title="Toggle spoken replies">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>' +
          "</button>" +
          '<div class="icon-btn" id="headerCart">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>' +
            '<span class="cart-badge" id="cartBadge">0</span>' +
          "</div>" +
          '<button class="icon-btn" id="expandToggle" title="Expand chat" aria-label="Expand chat">' +
            '<svg class="icon-expand" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
            '<svg class="icon-collapse" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
          "</button>" +
        "</div>" +
      "</div>" +
      '<div class="conversation" id="conversation">' +
        '<div class="bubble bot" id="greetingBubble">Hi! How can I help you today?</div>' +
      "</div>" +
      '<div class="input-row">' +
        '<input type="text" id="chatInput" placeholder="Search, order status, warranty, pay cart....." />' +
        '<button class="mic-btn" id="micBtn" aria-label="Voice input">' +
          '<svg class="icon-mic-inner" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>' +
          '<svg class="icon-send-inner" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        "</button>" +
      "</div>" +
      '<div class="mic-status" id="micStatus"></div>' +
    "</div>";
  root.style.display = "none";
  document.body.appendChild(root);

  var fab = document.getElementById("chatFab");
  var widget = document.getElementById("chatWidget");
  var input = document.getElementById("chatInput");
  var micBtn = document.getElementById("micBtn");
  var micStatus = document.getElementById("micStatus");
  var conversation = document.getElementById("conversation");
  var cartBadge = document.getElementById("cartBadge");
  var headerCart = document.getElementById("headerCart");
  var ttsToggle = document.getElementById("ttsToggle");
  var expandToggle = document.getElementById("expandToggle");
  var headerAvatar = document.getElementById("headerAvatar");
  var headerName = document.getElementById("headerName");
  var greetingBubble = document.getElementById("greetingBubble");
  var fabGreeting = document.getElementById("fabGreeting");
  var fgClose = document.getElementById("fgClose");
  var fgText = document.getElementById("fgText");
  var greetingDismissed = false;
  try {
    greetingDismissed = sessionStorage.getItem("aiChatGreetingDismissed_" + SHOP) === "1";
  } catch (e) {}

  var lastCustomGreeting = "";
  function getGreetingText(customMsg) {
    if (customMsg !== undefined && customMsg !== null && customMsg !== "") {
      lastCustomGreeting = customMsg;
    }
    var name = customerName || "";
    if (!name && customerEmail) {
      var nick = customerEmail.split("@")[0].replace(/[0-9._-]+.*$/, "");
      var mNick = nick.match(/^([a-z]{3,15}?)(thakur|sharma|singh|kumar|patel|gupta|verma|khan|reddy|smith|johnson|williams)$/i);
      if (mNick && mNick[1]) nick = mNick[1];
      if (nick.length >= 2) name = nick.charAt(0).toUpperCase() + nick.slice(1);
    }
    if (name) {
      return "Hi " + name + "! How can I help you today?";
    }
    if (lastCustomGreeting && !/^(hi|hey|hello)\b/i.test(lastCustomGreeting)) return lastCustomGreeting;
    return "Hi there! How can I help you today?";
  }

  function updateOutsideGreetingText() {
    var txt = getGreetingText();
    if (fgText) fgText.textContent = txt;
    if (greetingBubble && !widget.classList.contains("open")) {
      greetingBubble.textContent = txt;
    }
  }

  function showOutsideGreeting(message) {
    if (greetingDismissed) return;
    if (widget.classList.contains("open")) return;
    if (!fabGreeting) return;

    if (fgText) fgText.textContent = getGreetingText(message);

    fabGreeting.style.display = "flex";
    setTimeout(function () {
      if (!widget.classList.contains("open") && !greetingDismissed) {
        fabGreeting.classList.add("show");
      }
    }, 400);
  }

  function hideOutsideGreeting(permanent) {
    if (!fabGreeting) return;
    fabGreeting.classList.remove("show");
    setTimeout(function () {
      if (!fabGreeting.classList.contains("show")) {
        fabGreeting.style.display = "none";
      }
    }, 300);
    if (permanent) {
      greetingDismissed = true;
      try { sessionStorage.setItem("aiChatGreetingDismissed_" + SHOP, "1"); } catch (e) {}
    }
  }

  if (fabGreeting) {
    fabGreeting.addEventListener("click", function (e) {
      if (e.target && (e.target.id === "fgClose" || e.target.classList.contains("fg-close"))) {
        e.stopPropagation();
        hideOutsideGreeting(true);
        return;
      }
      hideOutsideGreeting(false);
      var isOpen = widget.classList.toggle("open", true);
      fab.classList.toggle("open", true);
      renderQuickActions();
      persistState();
    });
  }

  if (fgClose) {
    fgClose.addEventListener("click", function (e) {
      e.stopPropagation();
      hideOutsideGreeting(true);
    });
  }

  var SESSION_ID = (function () {
    var k = "aiChatSessionId_" + SHOP;
    var s = null;
    try { s = sessionStorage.getItem(k); } catch (e) {}
    if (!s) {
      s = "sess_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
      try { sessionStorage.setItem(k, s); } catch (e) {}
    }
    return s;
  })();

  var VISITOR_ID = (function () {
    var k = "aiChatVisitorId_" + SHOP;
    var v = null;
    try { v = localStorage.getItem(k); } catch (e) {}
    if (!v) {
      v = "vis_" + Date.now() + "_" + Math.random().toString(36).slice(2, 10);
      try { localStorage.setItem(k, v); } catch (e) {}
    }
    return v;
  })();

  var customerToken = null;
  var customerId = null;
  var customerEmail = null;
  var customerName = null;

  function updateHeaderProfileBadge() {
    var badgeEl = document.getElementById("headerCustBadge");
    if (!badgeEl) {
      badgeEl = document.createElement("div");
      badgeEl.id = "headerCustBadge";
      badgeEl.className = "header-cust-badge";
      var headerRight = document.querySelector("#ai-chat-widget-root .header-right");
      if (headerRight && headerRight.parentNode) headerRight.parentNode.insertBefore(badgeEl, headerRight);
    }
    if (customerEmail || customerName) {
      var displayLabel = customerName || (customerEmail ? customerEmail.split("@")[0] : "Logged in");
      badgeEl.textContent = "\uD83D\uDC64 " + displayLabel;
      badgeEl.title = "Logged in as " + (customerEmail || customerName);
      badgeEl.style.display = "inline-flex";
    } else {
      badgeEl.style.display = "none";
    }
  }

  var profileFetchInFlight = false;
  function fetchRemoteProfile() {
    if (profileFetchInFlight) return;
    if (!CFG.customerProfileEndpoint || (!customerId && !customerEmail)) return;
    profileFetchInFlight = true;
    fetch(CFG.customerProfileEndpoint + "&customer_id=" + encodeURIComponent(customerId || "") + "&email=" + encodeURIComponent(customerEmail || ""))
      .then(function (r) { return r.json(); })
      .then(function (p) {
        profileFetchInFlight = false;
        if (p && p.found) {
          if (p.first_name) customerName = p.first_name;
          else if (p.name) customerName = p.name.split(" ")[0];
          if (p.email) customerEmail = p.email;
          if (p.customer_id) customerId = p.customer_id;
          if (customerName) sessionStorage.setItem("ai_cust_name_" + SHOP, customerName);
          if (customerEmail) sessionStorage.setItem("ai_cust_email_" + SHOP, customerEmail);
          if (customerId) sessionStorage.setItem("ai_cust_id_" + SHOP, customerId);
          updateHeaderProfileBadge();
          updateOutsideGreetingText();
        }
      })
      .catch(function () { profileFetchInFlight = false; });
  }

  function syncCustomerSession() {
    try {
      customerId = customerId || sessionStorage.getItem("ai_cust_id_" + SHOP) || null;
      customerEmail = customerEmail || sessionStorage.getItem("ai_cust_email_" + SHOP) || null;
      customerName = customerName || sessionStorage.getItem("ai_cust_name_" + SHOP) || null;

      if (window.__st) {
        if (window.__st.cid) customerId = String(window.__st.cid);
        else if (window.__st.rid && window.__st.rtyp === "customer") customerId = String(window.__st.rid);
      }
      if (window.ShopifyAnalytics && window.ShopifyAnalytics.meta && window.ShopifyAnalytics.meta.page && window.ShopifyAnalytics.meta.page.customerId) {
        customerId = String(window.ShopifyAnalytics.meta.page.customerId);
      }
      if (window.Shopify && window.Shopify.customer) {
        if (window.Shopify.customer.id) customerId = String(window.Shopify.customer.id);
        if (window.Shopify.customer.first_name) customerName = window.Shopify.customer.first_name;
        if (window.Shopify.customer.email) customerEmail = window.Shopify.customer.email;
      }

      var isAccountPage = window.location.pathname.indexOf("/account") !== -1;
      var logoutLink = document.querySelector('a[href*="/account/logout"], a[href*="logout"]');
      if (logoutLink || isAccountPage) {
        var emailRe = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/;
        var emailEl = document.querySelector('[data-customer-email], .customer-email, .account-email, #customer_email');
        if (emailEl && emailEl.textContent && emailRe.test(emailEl.textContent)) {
          var m = emailEl.textContent.match(emailRe);
          if (m) customerEmail = m[0].toLowerCase();
        }
        if (!customerEmail) {
          var accountSection = document.querySelector('.customer__account, .account, .customer, #account, main, body');
          if (accountSection && emailRe.test(accountSection.innerText)) {
            var m2 = accountSection.innerText.match(emailRe);
            if (m2) customerEmail = m2[0].toLowerCase();
          }
        }
        var nameEl = document.querySelector('.customer-name, [data-customer-name], .header__account-name');
        if (nameEl && nameEl.textContent) {
          var cleanName = nameEl.textContent.replace(/(account|details|orders|overview|my\s*account)/gi, "").trim();
          if (cleanName && cleanName.length > 1 && cleanName.length < 50) {
            customerName = cleanName.split(" ")[0];
          }
        }
        var headings = document.querySelectorAll('h2, h3, p');
        for (var i = 0; i < headings.length; i++) {
          if (/account details|addresses/i.test(headings[i].textContent)) {
            var targetEl = headings[i].nextElementSibling || headings[i].parentElement;
            if (targetEl) {
              var pLines = (targetEl.innerText || "").split("\n");
              for (var j = 0; j < pLines.length; j++) {
                var line = (pLines[j] || "").trim();
                if (line && line.length > 1 && line.length < 40 && line.indexOf("@") === -1 && !/details|addresses|account|orders/i.test(line)) {
                  customerName = line.split(" ")[0];
                  break;
                }
              }
            }
          }
        }
      }

      if (!customerName && customerEmail) {
        var nick = customerEmail.split("@")[0].replace(/[0-9._-]+.*$/, "");
        var mNick = nick.match(/^([a-z]{3,15}?)(thakur|sharma|singh|kumar|patel|gupta|verma|khan|reddy|smith|johnson|williams)$/i);
        if (mNick && mNick[1]) nick = mNick[1];
        if (nick.length >= 2) customerName = nick.charAt(0).toUpperCase() + nick.slice(1);
      }

      if (customerId) sessionStorage.setItem("ai_cust_id_" + SHOP, customerId);
      if (customerEmail) sessionStorage.setItem("ai_cust_email_" + SHOP, customerEmail);
      if (customerName) sessionStorage.setItem("ai_cust_name_" + SHOP, customerName);

      updateHeaderProfileBadge();
      updateOutsideGreetingText();

      if (customerId || customerEmail) {
        fetchRemoteProfile();
      } else {
        fetch("/account", { credentials: "same-origin", headers: { Accept: "text/html" } })
          .then(function (res) {
            if (res.ok && res.url && res.url.indexOf("/account/login") === -1) {
              return res.text();
            }
            return null;
          })
          .then(function (html) {
            if (!html) return;
            var emailRe = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/;
            var match = html.match(emailRe);
            if (match && match[0]) {
              customerEmail = match[0].toLowerCase();
              sessionStorage.setItem("ai_cust_email_" + SHOP, customerEmail);
            }
            var cidMatch = html.match(/"cid":\s*"?(\d+)"?/) || html.match(/"customerId":\s*"?(\d+)"?/);
            if (cidMatch && cidMatch[1]) {
              customerId = cidMatch[1];
              sessionStorage.setItem("ai_cust_id_" + SHOP, customerId);
            }
            if (customerId || customerEmail) fetchRemoteProfile();
          })
          .catch(function () {});
      }

      if (CFG.customerSessionPath) {
        fetch(CFG.customerSessionPath, { credentials: "same-origin" })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            if (data && data.authenticated) {
              customerToken = data.customer_token || customerToken;
              customerId = data.customer_id || customerId;
              if (data.first_name) customerName = data.first_name;
              if (customerId) sessionStorage.setItem("ai_cust_id_" + SHOP, customerId);
              if (customerName) sessionStorage.setItem("ai_cust_name_" + SHOP, customerName);
              updateHeaderProfileBadge();
              updateOutsideGreetingText();
            }
          })
          .catch(function () {});
      }
    } catch (e) {}
    updateHeaderProfileBadge();
    updateOutsideGreetingText();
  }
  var STATE_KEY = "aiChatWidgetState_" + SHOP;
  var chatHistory = [];
  var quickActionsRendered = false;

  function persistState() {
    try {
      sessionStorage.setItem(STATE_KEY, JSON.stringify({
        open: widget.classList.contains("open"),
        expanded: widget.classList.contains("expanded"),
        quickActionsShown: quickActionsRendered,
        history: chatHistory
      }));
    } catch (e) {}
  }

  function autoResizeConversation() {
    requestAnimationFrame(function () { conversation.scrollTop = conversation.scrollHeight; });
  }

  function addBubble(text, who, opts) {
    opts = opts || {};
    var el = document.createElement("div");
    el.className = "bubble " + who;
    el.textContent = text;
    conversation.appendChild(el);
    autoResizeConversation();
    if (who.indexOf("typing") === -1 && opts.record !== false) {
      chatHistory.push({ type: "bubble", text: text, who: who });
      persistState();
    }
    return el;
  }

  function addProductRow(products, opts) {
    opts = opts || {};
    var row = document.createElement("div");
    row.className = "product-row";
    products.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "product-card";
      var img = document.createElement("img");
      img.src = p.image || ""; img.alt = p.name || "";
      var name = document.createElement("div");
      name.className = "p-name"; name.textContent = p.name || "Unnamed product";
      var price = document.createElement("div");
      price.className = "p-price";
      price.textContent = p.price !== undefined ? "$" + p.price : "";
      var btn = document.createElement("button");
      btn.textContent = "Add to cart";
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        cartAdd(p.id, 1, btn);
      });
      card.appendChild(img); card.appendChild(name); card.appendChild(price); card.appendChild(btn);
      row.appendChild(card);
    });
    conversation.appendChild(row);
    autoResizeConversation();
    if (opts.record !== false) { chatHistory.push({ type: "products", products: products }); persistState(); }
  }

  function addOrderPicker(orders, opts) {
    opts = opts || {};
    var heading = document.createElement("div");
    heading.className = "history-divider";
    heading.textContent = "Your Orders (" + orders.length + ")";
    conversation.appendChild(heading);

    orders.forEach(function (o) {
      var card = document.createElement("div");
      card.className = "order-card";
      var top = document.createElement("div");
      top.className = "order-card-top";
      var num = document.createElement("strong");
      var ordNum = o.order_number || o.id || "";
      num.textContent = "#" + ordNum;
      top.appendChild(num);
      var st = document.createElement("span");
      st.className = "order-status";
      st.textContent = o.status || o.fulfillment_status || "Active";
      top.appendChild(st);
      card.appendChild(top);

      if (o.date) {
        var d = document.createElement("div");
        d.className = "order-meta";
        d.textContent = "Ordered on: " + o.date;
        card.appendChild(d);
      }
      if (o.items && o.items.length) {
        var it = document.createElement("div");
        it.className = "order-items";
        it.textContent = o.items.map(function (i) { return (i.name || "Item") + " ×" + (i.quantity || 1); }).join(", ");
        card.appendChild(it);
      }
      if (o.total) {
        var total = document.createElement("div");
        total.className = "order-total";
        total.textContent = "Total: " + (o.currency || "$") + " " + o.total;
        card.appendChild(total);
      }

      var actionsRow = document.createElement("div");
      actionsRow.className = "order-actions";

      var trackBtn = document.createElement("button");
      trackBtn.className = "order-act-btn";
      trackBtn.textContent = "🚚 Track Order";
      trackBtn.addEventListener("click", function () {
        sendMessage("Track order #" + ordNum);
      });
      actionsRow.appendChild(trackBtn);

      var claimBtn = document.createElement("button");
      claimBtn.className = "order-act-btn";
      claimBtn.textContent = "🛡️ Warranty Claim";
      claimBtn.addEventListener("click", function () {
        sendMessage("Claim warranty for order #" + ordNum);
      });
      actionsRow.appendChild(claimBtn);

      card.appendChild(actionsRow);
      conversation.appendChild(card);
    });

    autoResizeConversation();
    if (opts.record !== false) { chatHistory.push({ type: "orders", orders: orders }); persistState(); }
  }

  function addOtpPrompt(otp, amount, currency, paymentMethod) {
    var box = document.createElement("div");
    box.className = "otp-box";
    box.innerHTML =
      '<div class="otp-title">🔐 Payment Authorization Required</div>' +
      '<div class="otp-detail">Authorizing <strong>' + (currency || "$") + ' ' + (amount || "") + '</strong> using <strong>' + (paymentMethod || "Saved Card") + '</strong>. Enter your OTP code below to confirm payment:</div>' +
      '<div class="otp-row">' +
        '<input type="text" class="otp-input" id="otpBoxInput" maxlength="6" value="' + (otp || "") + '" placeholder="6-digit OTP" />' +
        '<button class="otp-btn" id="otpBoxBtn">Authorize & Pay</button>' +
      '</div>';
    conversation.appendChild(box);
    autoResizeConversation();

    var inputEl = box.querySelector("#otpBoxInput");
    var btnEl = box.querySelector("#otpBoxBtn");
    btnEl.addEventListener("click", function () {
      var val = (inputEl.value || "").trim();
      if (!val) return;
      btnEl.disabled = true;
      btnEl.textContent = "Processing\u2026";
      sendMessage("Confirm payment with OTP " + val);
    });
  }

  function addPaymentForm(total, currency) {
    var box = document.createElement("div");
    box.className = "pay-form-box";
    box.innerHTML =
      '<div class="pay-form-title">💳 Secure Payment Setup</div>' +
      '<div class="pay-form-group"><label class="pay-form-label">Cardholder Name</label><input type="text" class="pay-form-input" id="pfName" value="Valued Customer" /></div>' +
      '<div class="pay-form-group"><label class="pay-form-label">Card Number</label><input type="text" class="pay-form-input" id="pfNumber" placeholder="4242 4242 4242 4242" value="4242424242424242" /></div>' +
      '<div class="pay-form-row">' +
        '<div class="pay-form-group" style="flex:1"><label class="pay-form-label">Expiry (MM/YY)</label><input type="text" class="pay-form-input" id="pfExp" value="12/28" /></div>' +
        '<div class="pay-form-group" style="flex:1"><label class="pay-form-label">CVV</label><input type="text" class="pay-form-input" id="pfCvv" value="123" /></div>' +
      '</div>' +
      '<button class="pay-form-submit" id="pfSubmit">Save & Proceed to Pay</button>';
    conversation.appendChild(box);
    autoResizeConversation();

    box.querySelector("#pfSubmit").addEventListener("click", function () {
      var name = box.querySelector("#pfName").value;
      var num = box.querySelector("#pfNumber").value;
      var exp = box.querySelector("#pfExp").value;
      box.querySelector("#pfSubmit").disabled = true;
      box.querySelector("#pfSubmit").textContent = "Saving\u2026";
      fetch(CFG.saveCredentialsEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ shop: SHOP, session_id: SESSION_ID, cardholder_name: name, card_number: num, expiry: exp, customer_token: customerToken, customer_id: customerId, customer_email: customerEmail, customer_name: customerName })
      }).then(function () {
        sendMessage("Pay for my cart");
      }).catch(function () {
        sendMessage("Pay for my cart");
      });
    });
  }

  function addReceiptCard(data) {
    var box = document.createElement("div");
    box.className = "receipt-box";
    box.innerHTML =
      '<div class="receipt-title">🎉 Order Confirmed & Paid!</div>' +
      '<div class="receipt-line"><strong>Order Number:</strong> #' + (data.order_number || "1055") + '</div>' +
      '<div class="receipt-line"><strong>Total Paid:</strong> ' + (data.currency || "$") + ' ' + (data.amount_paid || "") + '</div>' +
      '<div class="receipt-line"><strong>Payment Method:</strong> ' + (data.payment_method || "Verified Card") + '</div>' +
      '<div class="receipt-line"><strong>Status:</strong> Paid (Authorized with OTP)</div>' +
      '<div class="receipt-line"><strong>Delivery:</strong> 3-5 business days</div>';
    conversation.appendChild(box);
    autoResizeConversation();
  }

  function addConfirmationButtons() {
    var row = document.createElement("div");
    row.className = "confirm-row";
    [{ label: "Yes", confirmed: true }, { label: "No", confirmed: false }].forEach(function (opt) {
      var btn = document.createElement("button");
      btn.className = "confirm-btn";
      btn.textContent = opt.label;
      btn.addEventListener("click", function () {
        row.remove();
        addBubble(opt.label, "user");
        var typingEl = addBubble("typing\u2026", "bot typing");
        fetch(CFG.confirmEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: SESSION_ID, shop: SHOP, confirmed: opt.confirmed, customer_token: customerToken, customer_id: customerId, customer_email: customerEmail, customer_name: customerName, visitor_id: VISITOR_ID })
        })
          .then(function (res) { return res.json(); })
          .then(function (data) { typingEl.remove(); handleChatResponse(data); })
          .catch(function () { typingEl.remove(); addBubble("Sorry, server error. Please try again.", "bot"); });
      });
      row.appendChild(btn);
    });
    conversation.appendChild(row);
    autoResizeConversation();
  }

  function handleChatResponse(data) {
    if (data.reply) addBubble(data.reply, "bot");
    if (Array.isArray(data.products) && data.products.length > 0) addProductRow(data.products);
    if (Array.isArray(data.orders) && data.orders.length > 0) addOrderPicker(data.orders);
    if (data.status === "otp_required") addOtpPrompt(data.otp, data.amount, data.currency, data.payment_method);
    if (data.needs_credentials_form) addPaymentForm(data.total, data.currency);
    if (data.status === "payment_success") addReceiptCard(data);
    if (data.status === "confirmation_required") addConfirmationButtons();
    if (data.widget_action) runWidgetAction(data.widget_action);
    refreshCartBadge();
  }

  var lastKnownCart = null;

  function updateCartState(cart) {
    if (!cart) return;
    lastKnownCart = cart;
    if (cartBadge) {
      cartBadge.textContent = cart.item_count || 0;
      cartBadge.classList.toggle("show", (cart.item_count || 0) > 0);
    }
  }

  function refreshCartBadge() {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      updateCartState(cart);
    }).catch(function () {});
  }

  function cartAdd(variantId, quantity, btnEl) {
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = "Adding\u2026"; }
    fetch("/cart/add.js", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [{ id: variantId, quantity: quantity || 1 }] })
    })
      .then(function (res) { if (!res.ok) throw new Error("add failed"); return res.json(); })
      .then(function () {
        if (btnEl) btnEl.textContent = "Added \u2713";
        refreshCartBadge();
      })
      .catch(function () {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = "Add to cart"; }
        addBubble("Could not add item to cart. Please try again.", "bot");
      });
  }

  function cartChangeItem(action, quantity) {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      var targetLine = null;
      if (action.variant_id) {
        for (var i = 0; i < cart.items.length; i++) {
          if (String(cart.items[i].variant_id) === String(action.variant_id)) {
            targetLine = i + 1; break;
          }
        }
      }
      if (!targetLine && action.product_name) {
        var needle = action.product_name.toLowerCase();
        for (var j = 0; j < cart.items.length; j++) {
          if (cart.items[j].product_title.toLowerCase().indexOf(needle) !== -1) {
            targetLine = j + 1; break;
          }
        }
      }
      if (targetLine) {
        fetch("/cart/change.js", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ line: targetLine, quantity: quantity })
        }).then(function () { refreshCartBadge(); });
      }
    }).catch(function () {});
  }

  function cartView() {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      updateCartState(cart);
      if (!cart.items || !cart.items.length) {
        addBubble("Your cart is currently empty.", "bot");
        return;
      }
      var lines = cart.items.map(function (it) {
        return (it.quantity || 1) + "x " + it.product_title + " ($" + (it.final_line_price / 100).toFixed(2) + ")";
      });
      addBubble("🛒 Your Shopping Cart:\n" + lines.join("\n") + "\n\nTotal: $" + (cart.total_price / 100).toFixed(2), "bot");
    }).catch(function () {});
  }

  function cartClear() {
    lastKnownCart = null;
    fetch("/cart/clear.js", { method: "POST" }).then(function () {
      refreshCartBadge();
    }).catch(function () {});
  }

  function runWidgetAction(action) {
    if (!action || !action.type) return;
    switch (action.type) {
      case "redirect":
        if (action.url) setTimeout(function () { window.location.href = action.url; }, 600);
        break;
      case "cart_add":
        cartAdd(action.variant_id, action.quantity || 1, null);
        break;
      case "cart_remove":
        cartChangeItem(action, 0);
        break;
      case "cart_set_quantity":
        cartChangeItem(action, action.quantity || 1);
        break;
      case "cart_view":
        cartView();
        break;
      case "cart_clear":
        cartClear();
        break;
      case "checkout":
        window.location.href = "/checkout";
        break;
    }
  }

  var QUICK_ACTIONS = [
    { icon: "📦", label: "Show all my orders", command: "Show all my orders" },
    { icon: "🚚", label: "Track an order", command: "Track order" },
    { icon: "💳", label: "Pay for my cart", command: "Pay for my cart" },
    { icon: "🛡️", label: "Claim a warranty", command: "I want to claim a warranty" },
    { icon: "📋", label: "Return & store policies", command: "What is your return policy?" },
    { icon: "🛒", label: "View my cart", command: "What's in my cart?" },
    { icon: "🔍", label: "Search products", command: "Show me products" }
  ];

  function renderQuickActions() {
    if (quickActionsRendered) return;
    quickActionsRendered = true;
    var row = document.createElement("div");
    row.className = "quick-actions";
    conversation.appendChild(row);
    QUICK_ACTIONS.forEach(function (act, i) {
      var btn = document.createElement("button");
      btn.className = "quick-action-btn";
      btn.textContent = act.icon + " " + act.label;
      btn.addEventListener("click", function () { sendMessage(act.command); });
      row.appendChild(btn);
      setTimeout(function () {
        btn.classList.add("show");
        autoResizeConversation();
      }, i * 120);
    });
  }

  function sendMessage(text) {
    text = (text || "").trim();
    if (!text) return;
    syncCustomerSession();
    addBubble(text, "user");
    input.value = "";
    micBtn.classList.remove("has-text");

    var typingEl = addBubble("typing\u2026", "bot typing");
    var getCart = fetch("/cart.js")
      .then(function (r) { return r.json(); })
      .then(function (c) { updateCartState(c); return c; })
      .catch(function () { return lastKnownCart; });

    getCart.then(function (cartPayload) {
      return fetch(CFG.chatEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          session_id: SESSION_ID,
          shop: SHOP,
          customer_token: customerToken,
          customer_id: customerId,
          customer_email: customerEmail,
          customer_name: customerName,
          visitor_id: VISITOR_ID,
          cart_data: cartPayload || lastKnownCart
        })
      });
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        typingEl.remove();
        handleChatResponse(data);
      })
      .catch(function () {
        typingEl.remove();
        addBubble("Sorry, could not reach the assistant. Please try again.", "bot");
      });
  }

  input.addEventListener("keydown", function (e) { if (e.key === "Enter") sendMessage(input.value); });
  input.addEventListener("input", function () {
    micBtn.classList.toggle("has-text", input.value.trim().length > 0);
  });

  fab.addEventListener("click", function () {
    syncCustomerSession();
    var isOpen = widget.classList.toggle("open");
    fab.classList.toggle("open", isOpen);
    if (isOpen) {
      hideOutsideGreeting(false);
      renderQuickActions();
    }
    persistState();
  });

  expandToggle.addEventListener("click", function () {
    var isExpanded = widget.classList.toggle("expanded");
    expandToggle.classList.toggle("active-expand", isExpanded);
    expandToggle.title = isExpanded ? "Collapse chat" : "Expand chat";
    autoResizeConversation();
    persistState();
  });

  var CART_TRIGGER_SELECTORS = [
    "#siteCartBtn", "#cart-icon-bubble", "#CartDrawer-Toggle", "#cart-icon",
    "#CartLink", "[data-cart-drawer-toggle]", "[data-drawer-toggle='cart']",
    "[aria-controls='CartDrawer']", ".header__icon--cart", ".cart-icon-bubble", "a[href='/cart']"
  ];
  headerCart.addEventListener("click", function () {
    for (var i = 0; i < CART_TRIGGER_SELECTORS.length; i++) {
      var el = document.querySelector(CART_TRIGGER_SELECTORS[i]);
      if (el && !root.contains(el) && el.offsetParent !== null) {
        el.click(); return;
      }
    }
    window.location.href = "/cart";
  });

  // Voice speech-to-text
  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRecognition) {
    var recognizer = new SpeechRecognition();
    recognizer.continuous = false;
    recognizer.interimResults = false;
    var isListening = false;
    recognizer.onstart = function () {
      isListening = true;
      micBtn.classList.add("listening");
      micStatus.textContent = "Listening\u2026 speak now";
    };
    recognizer.onend = function () {
      isListening = false;
      micBtn.classList.remove("listening");
      micStatus.textContent = "";
    };
    recognizer.onresult = function (event) {
      var transcript = event.results[0][0].transcript;
      if (transcript) sendMessage(transcript);
    };
    recognizer.onerror = function () {
      isListening = false;
      micBtn.classList.remove("listening");
      micStatus.textContent = "Could not hear audio";
    };

    micBtn.addEventListener("click", function () {
      if (input.value.trim().length > 0) {
        sendMessage(input.value);
        return;
      }
      if (isListening) recognizer.stop();
      else recognizer.start();
    });
  } else {
    micBtn.addEventListener("click", function () {
      if (input.value.trim().length > 0) sendMessage(input.value);
    });
  }

  // Fetch widget config and activate
  syncCustomerSession();
  refreshCartBadge();
  setTimeout(syncCustomerSession, 300);
  setTimeout(syncCustomerSession, 800);
  setTimeout(syncCustomerSession, 1800);
  setTimeout(syncCustomerSession, 3500);

  fetch(CFG.configEndpoint)
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      if (cfg && cfg.status !== "inactive") {
        root.style.display = "block";
        syncCustomerSession();
        if (cfg.agent_name) headerName.textContent = cfg.agent_name;
        if (cfg.agent_title) greetingBubble.textContent = cfg.agent_title;
        if (cfg.theme_color) {
          fab.style.background = cfg.theme_color;
          headerAvatar.style.background = cfg.theme_color;
        }
        refreshCartBadge();
        var greetingMsg = cfg.greeting_message || cfg.agent_title || "";
        showOutsideGreeting(greetingMsg);
      }
    })
    .catch(function () {
      root.style.display = "block";
      syncCustomerSession();
      showOutsideGreeting();
    });
})();
"""
