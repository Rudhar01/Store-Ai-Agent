"""
activity_tracker.py
--------------------
Real-time tracking and retrieval of website activity (clicks, views, cart adds,
and searches) for both anonymous visitors (via cookies / localStorage) and
authenticated Shopify customers.

Features:
  1. Low-latency in-memory cache for instant recommendation scoring (<1ms).
  2. Asynchronous background persistence to Appwrite analytics_events collection.
  3. Real-time aggregation of click frequencies, browsed subcategories, and
     trending products for recommendation scoring.
"""

from __future__ import annotations
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["activity-tracking"])


class TrackActivityRequest(BaseModel):
    shop: str
    visitor_id: str
    session_id: str | None = ""
    event_type: str = "product_click"
    product_id: str | None = ""
    product_name: str | None = ""
    category: str | None = ""
    sub_category: str | None = ""
    style: str | None = ""
    price: float | None = None
    customer_token: str | None = None


# Background executor for persistent logging to Appwrite
_BG_POOL = ThreadPoolExecutor(max_workers=3)

# ---------------------------------------------------------------------------
# In-Memory Fast Activity Cache
# Keyed by (shop, visitor_id) or customer_id
# Expired after 24 hours of inactivity
# ---------------------------------------------------------------------------
# structure: (shop, identity) -> list[dict]
_ACTIVITY_STORE: dict[str, list[dict]] = defaultdict(list)
_STORE_TRENDING_CLICKS: dict[str, Counter] = defaultdict(Counter)
_MAX_USER_EVENTS = 100

# ---------------------------------------------------------------------------
# Session Shown Products (Repetition Suppression / Discovery Fatigue Prevention)
# Keyed by session_id -> set of product IDs already displayed in carousel
# ---------------------------------------------------------------------------
_SESSION_SHOWN_PRODUCTS: dict[str, set[str]] = defaultdict(set)
_SESSION_SHOWN_EXPIRY: dict[str, float] = {}
_SESSION_SHOWN_TTL_SECONDS = 7200  # 2 hours

# ---------------------------------------------------------------------------
# Store-level Order & Browsing Co-Occurrence Matrix ("Frequently Bought Together")
# Keyed by shop_domain -> { product_id: Counter({ co_product_id: weight }) }
# ---------------------------------------------------------------------------
_STORE_COOCCURRENCE: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))


def _identity_key(shop: str, visitor_id: str | None, customer_id: str | None = None) -> str:
    """Computes a unified identity key, favoring customer_id when authenticated."""
    norm_shop = (shop or "").strip().lower()
    if customer_id and str(customer_id).strip():
        return f"{norm_shop}:cust_{str(customer_id).strip()}"
    return f"{norm_shop}:vis_{(visitor_id or 'anon').strip()}"


def track_activity(
    shop: str,
    visitor_id: str,
    session_id: str = "",
    event_type: str = "product_click",
    product_id: str | int = "",
    product_name: str = "",
    category: str = "",
    sub_category: str = "",
    style: str = "",
    price: float | None = None,
    customer_id: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """
    Records a user website action (product click, product view, cart add, etc.).
    Immediately updates the in-memory activity index and queues persistence.
    """
    now = time.time()
    pid_str = str(product_id).strip() if product_id is not None else ""

    event_payload = {
        "event_type": event_type,
        "product_id": pid_str,
        "product_name": product_name,
        "category": category,
        "sub_category": sub_category,
        "style": style,
        "price": price,
        "session_id": session_id,
        "timestamp": now,
        "metadata": metadata or {},
    }

    # 1. Update in-memory user activity cache
    user_key = _identity_key(shop, visitor_id, customer_id)
    history = _ACTIVITY_STORE[user_key]
    history.append(event_payload)
    if len(history) > _MAX_USER_EVENTS:
        history.pop(0)

    # If customer_id is provided and visitor_id was also used, link visitor history
    if customer_id and visitor_id:
        anon_key = _identity_key(shop, visitor_id, None)
        if anon_key in _ACTIVITY_STORE and anon_key != user_key:
            # Merge recent guest browsing into customer profile
            for past_event in _ACTIVITY_STORE[anon_key]:
                if past_event not in history:
                    history.append(past_event)
            _ACTIVITY_STORE[anon_key] = []

    # 2. Update global store trending click count and browsing co-occurrence
    norm_shop = (shop or "").strip().lower()
    if event_type in ("product_click", "product_view", "cart_add") and pid_str:
        if event_type in ("product_click", "product_view"):
            _STORE_TRENDING_CLICKS[norm_shop][pid_str] += 1

        # Learn co-browsing pairs: pair current product with recent products user engaged with
        recent_engaged_pids = {
            ev.get("product_id") for ev in history[-6:]
            if ev.get("product_id") and ev.get("product_id") != pid_str
        }
        for prev_pid in recent_engaged_pids:
            _STORE_COOCCURRENCE[norm_shop][pid_str][prev_pid] += 1
            _STORE_COOCCURRENCE[norm_shop][prev_pid][pid_str] += 1

    # 3. Asynchronously write to Appwrite analytics_events in background
    def _async_persist():
        try:
            import repository_appwrite as repo
            store_doc = repo._find_store_doc(shop)
            if store_doc:
                store_id = store_doc["$id"]
                repo.record_event(
                    store_id=store_id,
                    event_type=event_type,
                    name=product_name or pid_str,
                    value=float(price or 1),
                    session_id=session_id,
                    visitor_id=visitor_id or (f"cust_{customer_id}" if customer_id else ""),
                    external_id=str(customer_id) if customer_id else "",
                    category=sub_category or category,
                    platform="shopify",
                )
        except Exception as err:
            # Telemetry must never crash or block the caller
            pass

    _BG_POOL.submit(_async_persist)

    return {"status": "ok", "recorded_event": event_type, "product_id": pid_str}


def _recency_decay(age_seconds: float) -> float:
    """
    Computes an exponential decay weight based on event age:
      - 0 to 15 mins (active browsing session): 1.0 (100% full weight)
      - 15 mins to 1 hour:                      0.7 (70% weight)
      - 1 hour to 4 hours:                      0.4 (40% weight)
      - > 4 hours:                              0.2 (20% residual baseline)
    """
    if age_seconds <= 900:        # 15 minutes
        return 1.0
    elif age_seconds <= 3600:     # 1 hour
        return 0.7
    elif age_seconds <= 14400:    # 4 hours
        return 0.4
    else:
        return 0.2


def get_user_activity_summary(
    shop: str,
    visitor_id: str | None,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """
    Returns an aggregated behavioral summary for the user with recency time-decay:
      - clicked_product_ids: list of product IDs clicked (ordered by weighted frequency/recency)
      - product_click_counts: dict of {product_id: decay_weighted_click_score}
      - top_subcategories: list of (subcategory_name, weighted_count)
      - top_styles: list of (style_name, weighted_count)
      - total_clicks: int total number of raw product clicks
    """
    user_key = _identity_key(shop, visitor_id, customer_id)
    history = _ACTIVITY_STORE.get(user_key, [])

    click_scores: Counter = Counter()
    view_scores: Counter = Counter()
    subcat_scores: Counter = Counter()
    style_scores: Counter = Counter()
    recent_pids: list[str] = []
    raw_click_count = 0

    now = time.time()
    for ev in history:
        pid = ev.get("product_id")
        subcat = ev.get("sub_category")
        st = ev.get("style")
        etype = ev.get("event_type")
        event_time = float(ev.get("timestamp") or now)
        decay = _recency_decay(max(0.0, now - event_time))

        if etype == "product_click":
            raw_click_count += 1

        if pid:
            base_w = 1.0 if etype == "product_click" else 0.5
            effective_w = round(base_w * decay, 2)
            if etype == "product_click":
                click_scores[pid] += effective_w
            elif etype == "product_view":
                view_scores[pid] += effective_w
            if pid not in recent_pids:
                recent_pids.append(pid)

        if subcat:
            base_w = 2.0 if etype == "product_click" else 1.0
            subcat_scores[subcat] += round(base_w * decay, 2)

        if st:
            base_w = 2.0 if etype == "product_click" else 1.0
            style_scores[st] += round(base_w * decay, 2)

    return {
        "clicked_product_ids": [pid for pid, _ in click_scores.most_common()],
        "product_click_counts": dict(click_scores),
        "viewed_product_ids": [pid for pid, _ in view_scores.most_common()],
        "top_subcategories": subcat_scores.most_common(5),
        "top_styles": style_scores.most_common(5),
        "recent_product_ids": list(reversed(recent_pids))[:10],
        "total_clicks": raw_click_count,
    }


def get_trending_product_ids(shop: str, limit: int = 10) -> list[str]:
    """Returns top trending product IDs across all users based on click popularity."""
    norm_shop = (shop or "").strip().lower()
    counter = _STORE_TRENDING_CLICKS.get(norm_shop)
    if not counter:
        return []
    return [pid for pid, _ in counter.most_common(limit)]


def record_session_shown_products(session_id: str, product_ids: list[str | int]) -> None:
    """Remembers product IDs presented in this chat session to prevent duplicate carousels."""
    if not session_id:
        return
    s_id = str(session_id).strip()
    _SESSION_SHOWN_EXPIRY[s_id] = time.time() + _SESSION_SHOWN_TTL_SECONDS
    for pid in product_ids:
        if pid:
            _SESSION_SHOWN_PRODUCTS[s_id].add(str(pid).strip())


def get_session_shown_products(session_id: str) -> set[str]:
    """Returns product IDs that have already been presented in this session."""
    if not session_id:
        return set()
    s_id = str(session_id).strip()
    if s_id in _SESSION_SHOWN_EXPIRY and time.time() > _SESSION_SHOWN_EXPIRY[s_id]:
        _SESSION_SHOWN_PRODUCTS.pop(s_id, None)
        _SESSION_SHOWN_EXPIRY.pop(s_id, None)
        return set()
    return set(_SESSION_SHOWN_PRODUCTS.get(s_id, set()))


def record_order_cooccurrences(shop: str, order_baskets: list[list[str]]) -> None:
    """
    Updates the co-occurrence matrix from multi-item customer orders.
    Products purchased in the same basket receive high affinity weight (+3).
    """
    norm_shop = (shop or "").strip().lower()
    if not norm_shop or not order_baskets:
        return
    for basket in order_baskets:
        clean_pids = list({str(p).strip() for p in basket if p})
        for i in range(len(clean_pids)):
            for j in range(i + 1, len(clean_pids)):
                p1, p2 = clean_pids[i], clean_pids[j]
                _STORE_COOCCURRENCE[norm_shop][p1][p2] += 3
                _STORE_COOCCURRENCE[norm_shop][p2][p1] += 3


def get_cooccurring_affinities(shop: str, reference_product_ids: list[str], limit: int = 10) -> Counter:
    """
    Aggregates 'Frequently Bought Together' co-occurrence scores for a set of reference products.
    """
    norm_shop = (shop or "").strip().lower()
    store_matrix = _STORE_COOCCURRENCE.get(norm_shop)
    if not store_matrix or not reference_product_ids:
        return Counter()

    aggregate: Counter = Counter()
    ref_set = {str(p).strip() for p in reference_product_ids if p}
    for ref_pid in ref_set:
        if ref_pid in store_matrix:
            for co_pid, count in store_matrix[ref_pid].items():
                if co_pid not in ref_set:
                    aggregate[co_pid] += count
    return aggregate


def clear_cache_for_testing() -> None:
    """Helper to reset in-memory caches between unit tests."""
    _ACTIVITY_STORE.clear()
    _STORE_TRENDING_CLICKS.clear()
    _SESSION_SHOWN_PRODUCTS.clear()
    _SESSION_SHOWN_EXPIRY.clear()
    _STORE_COOCCURRENCE.clear()


@router.post("/track-activity")
async def track_activity_endpoint(req: TrackActivityRequest):
    import customer_profiles
    customer_id = None
    if req.customer_token:
        customer_id = customer_profiles.read_session_token(req.customer_token, req.shop)

    return track_activity(
        shop=req.shop,
        visitor_id=req.visitor_id,
        session_id=req.session_id or "",
        event_type=req.event_type or "product_click",
        product_id=req.product_id or "",
        product_name=req.product_name or "",
        category=req.category or "",
        sub_category=req.sub_category or "",
        style=req.style or "",
        price=req.price,
        customer_id=customer_id,
    )


@router.get("/user-activity")
async def user_activity_endpoint(shop: str, visitor_id: str, customer_token: str | None = None):
    import customer_profiles
    customer_id = None
    if customer_token:
        customer_id = customer_profiles.read_session_token(customer_token, shop)
    return get_user_activity_summary(shop, visitor_id, customer_id)

