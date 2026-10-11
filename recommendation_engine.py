"""Personalized product recommendations for authenticated Shopify customers."""
from __future__ import annotations
import re
from collections import Counter
import shopify_actions

_STOP = {"the", "and", "for", "with", "new", "small", "large", "medium"}


def _words(value) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(w) >= 3 and w not in _STOP}


def _num_id(value) -> str:
    """Normalises gid://shopify/Product/123 and 123 to '123'."""
    m = re.search(r"(\d+)\s*$", str(value or ""))
    return m.group(1) if m else str(value or "")


def _tags_list(product: dict) -> list:
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    return tags


def _product_words(product: dict) -> set[str]:
    out = set()
    for v in (product.get("title", ""), product.get("product_type", ""), " ".join(_tags_list(product))):
        out |= _words(v)
    return out


def _price(product: dict):
    try:
        return float((product.get("variants") or [{}])[0].get("price"))
    except (TypeError, ValueError, AttributeError):
        return None


def _card(product: dict, store) -> dict:
    variant = (product.get("variants") or [{}])[0] or {}
    price = _price(product)
    if price is not None and price.is_integer():
        price = int(price)
    image = product.get("image") or ""
    if isinstance(image, dict):
        image = image.get("src", "")
    card = {
        "id": str(variant.get("id") or ""),
        "name": product.get("title", "Unnamed product"),
        "price": price,
        "image": image,
    }
    if product.get("handle"):
        card["url"] = f"https://{store.shop_domain}/products/{product['handle']}"
    return card


async def recommend_products(store, customer_profile: dict | None, limit: int = 6, price_max=None, category=None) -> dict:
    catalog = await shopify_actions.get_active_products_graphql(store, first=100)
    if not catalog:
        return {"results": [], "reason": "catalog_unavailable"}

    # Optional budget / category the shopper mentioned ("suggest something under 1000").
    try:
        price_cap = float(price_max) if price_max not in (None, "") else None
    except (TypeError, ValueError):
        price_cap = None
    category_words = _words(category) if category else set()
    if price_cap or category_words:
        def _fits(p):
            price = _price(p)
            if price_cap and price is not None and price > price_cap:
                return False
            return not category_words or bool(category_words & _product_words(p))
        catalog = [p for p in catalog if _fits(p)]
        if not catalog:
            return {"results": [], "reason": "no_match_for_filters"}

    profile = customer_profile or {}
    purchased = {_num_id(x) for x in (profile.get("purchased_product_ids") or [])}

    # word -> weight, built from every signal we have
    weights: Counter = Counter()
    for field, w in (("top_types", 2), ("top_tags", 2), ("recent_searches", 4), ("favorite_colors", 2)):
        for value in profile.get(field) or []:
            for word in _words(value):
                weights[word] += w
    # Real type/tags of what they actually bought (top_types/top_tags hold
    # product and variant names, which aren't true types or tags).
    for product in catalog:
        if _num_id(product.get("id")) in purchased:
            for word in _words(product.get("product_type")) | _words(" ".join(_tags_list(product))):
                weights[word] += 3

    budget_max = profile.get("budget_max") or 0
    scored = []
    for product in catalog:
        words = _product_words(product)
        score = float(sum(weights[w] for w in words if w in weights))
        if _num_id(product.get("id")) in purchased:
            score -= 8  # don't re-recommend what they already own
        else:
            score += 2
        if words & {"new", "arrival", "arrivals"}:
            score += 2
        price = _price(product)
        if budget_max and price is not None:
            if price <= budget_max:
                score += 3
            elif price > budget_max * 1.5:
                score -= 2
        scored.append((score, product))

    scored.sort(key=lambda x: x[0], reverse=True)
    # Never re-recommend something they already bought, unless the catalog is too small to fill the list.
    unseen = [x for x in scored if _num_id(x[1].get("id")) not in purchased]
    if len(unseen) >= min(limit, 3):
        scored = unseen
    personalized = bool(weights or purchased)
    return {
        "results": [_card(p, store) for _, p in scored[:limit]],
        "reason": "personalized" if personalized else "catalog",
    }
