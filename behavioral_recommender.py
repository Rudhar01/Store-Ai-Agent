"""
behavioral_recommender.py
-------------------------
Multi-signal personalized product recommendation engine powered by:
  1. User website activity & click monitoring (visitor cookie / session / customer).
  2. Subcategory and metadata taxonomy (via product_enrichment.py).
  3. Shopify customer purchase history & orders (via customer_profiles.py).
  4. Global click popularity & trending items for cold-start visitors.
"""

from __future__ import annotations
import re
import time
from collections import Counter
from typing import Any

import activity_tracker
import product_enrichment
import shopify_actions

# ---------------------------------------------------------------------------
# In-Memory Enriched Catalog Cache (per shop domain)
# Default TTL: 5 minutes (300 seconds)
# Prevents expensive repeated Shopify GraphQL calls and rate-limiting.
# ---------------------------------------------------------------------------
_CATALOG_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CATALOG_TTL_SECONDS = 300


async def get_enriched_catalog(store, force_refresh: bool = False) -> list[dict]:
    """
    Retrieves the shop's product catalog with subcategory and style taxonomy.
    Uses in-memory TTL caching to drop recommendation latency from ~800ms to <1ms.
    """
    shop_domain = getattr(store, "shop_domain", "") or ""
    now = time.time()

    if not force_refresh and shop_domain in _CATALOG_CACHE:
        cached_time, cached_items = _CATALOG_CACHE[shop_domain]
        if now - cached_time < _CATALOG_TTL_SECONDS:
            return cached_items

    raw_catalog = await shopify_actions.get_active_products_graphql(store, first=100)
    if not raw_catalog:
        # Graceful fallback: return stale cache if Shopify API is temporarily slow/down
        if shop_domain in _CATALOG_CACHE:
            return _CATALOG_CACHE[shop_domain][1]
        return []

    enriched = product_enrichment.enrich_catalog(raw_catalog)
    _CATALOG_CACHE[shop_domain] = (now, enriched)
    return enriched


def invalidate_catalog_cache(shop_domain: str | None = None) -> None:
    """Evicts catalog cache, e.g. after a webhook or product update."""
    if shop_domain:
        _CATALOG_CACHE.pop(shop_domain, None)
    else:
        _CATALOG_CACHE.clear()


_STOP = {"the", "and", "for", "with", "new", "small", "large", "medium"}


def _words(value) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(w) >= 3 and w not in _STOP}


def _num_id(value) -> str:
    """Normalises gid://shopify/Product/123 and 123 to '123'."""
    m = re.search(r"(\d+)\s*$", str(value or ""))
    return m.group(1) if m else str(value or "")


def _price(product: dict) -> float | None:
    try:
        variants = product.get("variants") or [{}]
        return float(variants[0].get("price"))
    except (TypeError, ValueError, AttributeError, IndexError):
        return None


def _is_product_in_stock(product: dict) -> bool:
    """
    Checks if a product has at least one purchasable, in-stock variant.
    Filters out sold-out items (availableForSale == False, inventory <= 0, or 'sold out' tags).
    """
    if product.get("availableForSale") is False or product.get("available_for_sale") is False:
        return False

    tags = product.get("tags") or []
    if isinstance(tags, str):
        tag_list = [t.strip().lower() for t in tags.split(",")]
    else:
        tag_list = [str(t).strip().lower() for t in tags]

    if any(t in ("sold out", "out of stock", "discontinued", "backorder") for t in tag_list):
        return False

    variants = product.get("variants") or []
    if not variants:
        return False

    has_available_variant = False
    for v in variants:
        if not isinstance(v, dict):
            continue
        v_avail = v.get("availableForSale") if "availableForSale" in v else v.get("available_for_sale")
        if v_avail is False:
            continue

        v_qty = v.get("inventory_quantity") if "inventory_quantity" in v else v.get("inventoryQuantity")
        if v_qty is not None:
            try:
                if int(v_qty) <= 0:
                    continue
            except (ValueError, TypeError):
                pass

        has_available_variant = True
        break

    return has_available_variant


def _format_card(product: dict, store, score_reason: str = "") -> dict[str, Any]:
    variants = product.get("variants") or [{}]
    # Pick the first in-stock variant if multiple variants exist
    variant = next(
        (v for v in variants if isinstance(v, dict) and v.get("availableForSale") is not False and (v.get("inventory_quantity") is None or v.get("inventory_quantity") > 0)),
        variants[0] if variants else {}
    )
    price = _price(product)
    if price is not None and price.is_integer():
        price = int(price)
    image = product.get("image") or ""
    if isinstance(image, dict):
        image = image.get("src", "")
    
    meta = product.get("_metadata") or {}
    card = {
        "id": str(variant.get("id") or ""),
        "product_id": _num_id(product.get("id")),
        "name": product.get("title", "Unnamed product"),
        "price": price,
        "image": image,
        "category": meta.get("category", "General"),
        "sub_category": meta.get("sub_category", "Products"),
        "style": meta.get("style", "Casual"),
        "reason": score_reason or "Recommended pick",
    }
    if product.get("handle"):
        card["url"] = f"https://{store.shop_domain}/products/{product['handle']}"
    return card


async def recommend_products(
    store,
    customer_profile: dict | None = None,
    visitor_id: str | None = None,
    customer_id: str | None = None,
    limit: int = 6,
    price_max: float | str | None = None,
    category: str | None = None,
    target_product_id: str | int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Computes personalized product recommendations for the shopper using:
      - Live website clicks and views (from activity_tracker)
      - Enriched subcategory & style taxonomy (from product_enrichment)
      - Past Shopify purchases and preferences (from customer_profile)
      - Order & browsing co-occurrence ("Frequently Bought Together")
      - Session fatigue suppression (prioritizes fresh unseen products)
      - Global trending items (cold-start fallback)
    """
    shop_domain = getattr(store, "shop_domain", "") or ""

    # 1. Fetch & enrich product catalog (using in-memory TTL cache)
    catalog = await get_enriched_catalog(store)
    if not catalog:
        return {"results": [], "reason": "catalog_unavailable"}

    # 2. Filter out out-of-stock / sold-out items
    in_stock_catalog = [p for p in catalog if _is_product_in_stock(p)]
    if in_stock_catalog:
        catalog = in_stock_catalog

    # 3. Filter by conversational budget or category constraints if specified
    try:
        price_cap = float(price_max) if price_max not in (None, "") else None
    except (TypeError, ValueError):
        price_cap = None

    category_words = _words(category) if category else set()
    if price_cap or category_words:
        def _fits(p):
            p_price = _price(p)
            if price_cap and p_price is not None and p_price > price_cap:
                return False
            if category_words:
                meta = p.get("_metadata", {})
                p_text = f"{p.get('title', '')} {meta.get('sub_category', '')} {meta.get('category', '')}"
                if not (category_words & _words(p_text)):
                    return False
            return True
        catalog = [p for p in catalog if _fits(p)]
        if not catalog:
            return {"results": [], "reason": "no_match_for_filters"}

    # 4. Gather User Behavioral Signals (Clicks, Views, Cart history)
    user_activity = activity_tracker.get_user_activity_summary(shop_domain, visitor_id, customer_id)
    user_click_counts = user_activity.get("product_click_counts", {})
    user_subcat_affinity = dict(user_activity.get("top_subcategories", []))
    user_style_affinity = dict(user_activity.get("top_styles", []))
    trending_pids = set(activity_tracker.get_trending_product_ids(shop_domain, limit=10))

    # 5. Gather Customer Purchase Signals (from Shopify Orders)
    profile = customer_profile or {}
    purchased_ids = {_num_id(x) for x in (profile.get("purchased_product_ids") or [])}
    budget_max = profile.get("budget_max") or 0
    fav_colors = {str(c).lower() for c in (profile.get("favorite_colors") or [])}

    # 6. Gather Co-Occurrence and Session Fatigue Signals
    anchor_pids = list(user_click_counts.keys()) + list(purchased_ids)
    if target_product_id:
        anchor_pids.append(_num_id(target_product_id))
    fbt_affinities = activity_tracker.get_cooccurring_affinities(shop_domain, anchor_pids)
    shown_pids = activity_tracker.get_session_shown_products(session_id) if session_id else set()

    # 7. Score and Rank Each Product
    scored_items: list[tuple[float, dict, str]] = []

    for product in catalog:
        pid = _num_id(product.get("id"))
        meta = product.get("_metadata", {})
        subcat = meta.get("sub_category", "")
        style = meta.get("style", "")
        primary_color = str(meta.get("primary_color", "")).lower()

        score = 0.0
        reasons = []

        # Signal A: Direct clicks on this product by user
        direct_clicks = user_click_counts.get(pid, 0)
        if direct_clicks > 0:
            score += direct_clicks * 5.0
            reasons.append("Frequently viewed by you")

        # Signal B: Subcategory affinity (e.g. user clicked multiple Graphic Tees)
        subcat_weight = user_subcat_affinity.get(subcat, 0)
        if subcat_weight > 0:
            score += subcat_weight * 3.5
            if not reasons:
                reasons.append(f"Based on your interest in {subcat}")

        # Signal C: Style affinity (e.g. user browses Streetwear / Oversized)
        style_weight = user_style_affinity.get(style, 0)
        if style_weight > 0:
            score += style_weight * 2.0

        # Signal D: Frequently Bought Together (Order & Browsing Co-Occurrence)
        fbt_weight = fbt_affinities.get(pid, 0)
        if fbt_weight > 0:
            score += min(float(fbt_weight) * 3.0, 9.0)
            if not reasons:
                reasons.append("Frequently bought together with your items")

        # Signal E: Past purchase penalty (don't re-recommend what they already own)
        if pid in purchased_ids:
            score -= 10.0
        else:
            score += 1.0

        # Signal F: Color preference
        if primary_color in fav_colors:
            score += 2.5

        # Signal G: Budget alignment
        p_price = _price(product)
        if budget_max and p_price is not None:
            if p_price <= budget_max:
                score += 2.0
            elif p_price > budget_max * 1.5:
                score -= 2.0

        # Signal H: Global trending popularity
        if pid in trending_pids:
            score += 3.0
            if not reasons:
                reasons.append("Trending bestseller on store")

        # Signal I: New arrival tag boost
        tags_str = " ".join(meta.get("tags", []))
        if any(w in tags_str for w in ("new", "arrival", "drop", "fresh")):
            score += 1.5

        # Signal J: Session Fatigue / Repetition Suppression
        if pid in shown_pids:
            # Heavily demote unclicked items already shown in this chat session
            if pid not in user_click_counts:
                score -= 25.0
            else:
                score -= 5.0

        final_reason = reasons[0] if reasons else ("Top pick for you" if user_activity.get("total_clicks", 0) > 0 else "Curated bestseller")
        scored_items.append((score, product, final_reason))

    # Sort descending by composite score
    scored_items.sort(key=lambda item: item[0], reverse=True)

    # Filter out purchased items and prioritize items never shown in this session
    fresh_unseen_items = [
        item for item in scored_items
        if _num_id(item[1].get("id")) not in purchased_ids and _num_id(item[1].get("id")) not in shown_pids
    ]
    if len(fresh_unseen_items) >= min(limit, 3):
        tail_items = [
            item for item in scored_items
            if _num_id(item[1].get("id")) not in purchased_ids and _num_id(item[1].get("id")) in shown_pids
        ]
        scored_items = fresh_unseen_items + tail_items
    else:
        unseen_items = [item for item in scored_items if _num_id(item[1].get("id")) not in purchased_ids]
        if len(unseen_items) >= min(limit, 3):
            scored_items = unseen_items

    selected_cards = [_format_card(p, store, score_reason=r) for _, p, r in scored_items[:limit]]

    # Remember displayed cards for this session to prevent duplicate carousels
    if session_id and selected_cards:
        activity_tracker.record_session_shown_products(
            session_id, [c.get("product_id") for c in selected_cards if c.get("product_id")]
        )

    has_personalization = bool(user_activity.get("total_clicks", 0) > 0 or purchased_ids or fbt_affinities)

    return {
        "results": selected_cards,
        "reason": "personalized_activity" if has_personalization else "catalog_bestsellers",
        "based_on": {
            "user_clicks": user_activity.get("total_clicks", 0),
            "top_subcategories": [s for s, _ in user_activity.get("top_subcategories", [])[:3]],
            "previous_orders": bool(purchased_ids),
            "cooccurrence_matched": bool(fbt_affinities),
        },
    }


async def handle_recommendations(
    store,
    entities: dict,
    session_id: str,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """
    Main entrypoint called by shopify_actions router.
    Resolves customer profile / live orders, gathers visitor session entities,
    and runs multi-signal behavioral recommendations.
    """
    import customer_profiles
    profile = (await customer_profiles.get_profile(store.shop_domain, customer_id) or {}) if customer_id else {}
    if customer_id and not profile.get("purchased_product_ids"):
        # Stored profile missing/empty (first visit, DB not configured, sync not run yet): read the orders live.
        live = await shopify_actions.get_customer_orders(store, customer_id, first=50)
        if not live.get("error"):
            names = [
                i.get("name")
                for o in (live.get("active_orders") or []) + (live.get("past_orders") or [])
                for i in (o.get("items") or [])
                if i.get("name")
            ]
            profile = {
                **profile,
                "purchased_product_ids": live.get("purchased_product_ids") or [],
                "top_types": list(dict.fromkeys(names))[:20],
            }
            if live.get("order_baskets"):
                activity_tracker.record_order_cooccurrences(store.shop_domain, live.get("order_baskets") or [])

    visitor_id = entities.get("visitor_id") or entities.get("_visitor_id")
    result = await recommend_products(
        store,
        customer_profile=profile,
        visitor_id=visitor_id,
        customer_id=customer_id,
        limit=6,
        price_max=entities.get("price_max"),
        category=entities.get("category"),
        session_id=session_id,
    )
    result["based_on"] = {
        "previous_orders": bool(profile.get("purchased_product_ids")),
        "recent_searches": (profile.get("recent_searches") or [])[:3],
        "website_activity": result.get("based_on", {}),
    }
    if not customer_id and not result.get("based_on", {}).get("user_clicks"):
        result["note"] = "These are our trending bestsellers. Explore or click any product to get customized picks tailored to your style!"
    return result

