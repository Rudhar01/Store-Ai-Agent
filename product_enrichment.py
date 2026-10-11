"""
product_enrichment.py
----------------------
Enriches raw Shopify product catalog data by extracting fine-grained
taxonomy metadata and subcategories.

Raw Shopify products often have minimal structure (e.g., general "Apparel"
or unstructured tags). This module extracts:
  - category: Macro category (e.g., Apparel, Footwear, Accessories, Lifestyle)
  - sub_category: Specific item classification (e.g., "Graphic Tees", "Oversized T-Shirts",
                  "Hoodies & Sweatshirts", "Cargo Pants", "Caps & Hats", "Sneakers")
  - style: Streetwear, Minimalist, Vintage, Casual, Athletic, Formal
  - fit: Oversized, Regular, Slim, Relaxed, Boxy
  - gender: Unisex, Men, Women
  - price_bracket: Budget, Mid-range, Premium, Luxury
  - color_palette: Primary color family inferred from options / tags / title
"""

import re
from typing import Any


# ---------------------------------------------------------------------------
# Taxonomy Rules: Mapping keywords to specific subcategories and macro categories
# ---------------------------------------------------------------------------
_SUBCATEGORY_RULES = [
    # Tops & Streetwear
    (r"\b(graphic|anime|printed|vintage graphic)\s*(tee|t-?shirt|tshirt)\b", "Apparel", "Graphic Tees"),
    (r"\b(oversized?|drop shoulder|boxy)\s*(tee|t-?shirt|tshirt|top)\b", "Apparel", "Oversized T-Shirts"),
    (r"\b(tee|t-?shirt|tshirt|crewneck tee)\b", "Apparel", "T-Shirts"),
    (r"\b(hoodie|hooded sweatshirt|pullover hoodie|zip-?up hoodie)\b", "Apparel", "Hoodies & Sweatshirts"),
    (r"\b(sweatshirt|crewneck sweatshirt|fleece crewneck)\b", "Apparel", "Sweatshirts"),
    (r"\b(jacket|bomber|varsity|windbreaker|denim jacket|puffer|coat)\b", "Apparel", "Jackets & Outerwear"),
    (r"\b(shirt|button-?down|flannel|linen shirt|oxford)\b", "Apparel", "Shirts"),
    (r"\b(polo|polo shirt)\b", "Apparel", "Polo Shirts"),
    (r"\b(tank|sleeveless|vest|tank top)\b", "Apparel", "Tank Tops"),

    # Bottoms
    (r"\b(cargo|tactical cargo|utility pants)\b", "Apparel", "Cargo Pants"),
    (r"\b(sweatpants|joggers|track pants)\b", "Apparel", "Joggers & Sweatpants"),
    (r"\b(jeans|denim|straight jeans|baggy jeans)\b", "Apparel", "Jeans"),
    (r"\b(shorts|cargo shorts|sweat shorts|denim shorts)\b", "Apparel", "Shorts"),
    (r"\b(trousers|chinos|pants)\b", "Apparel", "Trousers & Pants"),

    # Footwear
    (r"\b(sneakers?|kicks|running shoes|trainer|low top|high top)\b", "Footwear", "Sneakers"),
    (r"\b(boots?|chelsea boot|combat boot)\b", "Footwear", "Boots"),
    (r"\b(slides?|sandals?|slippers?|crocs?)\b", "Footwear", "Slides & Sandals"),

    # Accessories
    (r"\b(cap|dad hat|snapback|trucker hat|baseball cap)\b", "Accessories", "Caps & Hats"),
    (r"\b(beanie|knit cap)\b", "Accessories", "Beanies"),
    (r"\b(tote bag|backpack|crossbody|waist bag|duffle|shoulder bag)\b", "Accessories", "Bags"),
    (r"\b(necklace|chain|pendant|bracelet|ring|earring|jewelry)\b", "Accessories", "Jewelry"),
    (r"\b(sunglasses|shades|eyewear)\b", "Accessories", "Eyewear"),
    (r"\b(socks|crew socks)\b", "Accessories", "Socks"),
    (r"\b(belt|leather belt)\b", "Accessories", "Belts"),
]

# Style detection patterns
_STYLE_PATTERNS = [
    (r"\b(streetwear|urban|grunge|skate|y2k|hype|drip|cyber)\b", "Streetwear"),
    (r"\b(minimal|minimalist|clean|plain|monochrome|basic|essential)\b", "Minimalist"),
    (r"\b(vintage|retro|washed|acid wash|faded|distressed|90s|80s)\b", "Vintage"),
    (r"\b(athletic|gym|sport|activewear|training|performance)\b", "Athletic"),
    (r"\b(formal|smart casual|office|tailored)\b", "Formal"),
    (r"\b(casual|everyday|classic)\b", "Casual"),
]

# Fit detection patterns
_FIT_PATTERNS = [
    (r"\b(oversized?|boxy|loose|baggy|wide leg)\b", "Oversized"),
    (r"\b(slim|slim-?fit|skinny|tapered)\b", "Slim"),
    (r"\b(relaxed|relaxed-?fit)\b", "Relaxed"),
    (r"\b(regular|classic-?fit|standard)\b", "Regular"),
]

# Gender detection patterns
_GENDER_PATTERNS = [
    (r"\b(men|mens|male|gentleman)\b", "Men"),
    (r"\b(women|womens|female|lady|ladies)\b", "Women"),
    (r"\b(unisex|all genders?)\b", "Unisex"),
]

# Color family patterns
_COLOR_PATTERNS = [
    (r"\b(black|charcoal|jet black|onyx|ebony)\b", "Black"),
    (r"\b(white|off-?white|ivory|cream|snow)\b", "White"),
    (r"\b(grey|gray|heather|smoke)\b", "Grey"),
    (r"\b(blue|navy|indigo|cobalt|cyan|sky blue)\b", "Blue"),
    (r"\b(red|crimson|maroon|burgundy|scarlet)\b", "Red"),
    (r"\b(green|olive|sage|emerald|forest green|khaki)\b", "Green"),
    (r"\b(brown|tan|beige|camel|mocha|sand)\b", "Brown"),
    (r"\b(yellow|mustard|gold)\b", "Yellow"),
    (r"\b(pink|rose|pastel pink)\b", "Pink"),
    (r"\b(purple|lavender|violet)\b", "Purple"),
    (r"\b(orange|terracotta|coral)\b", "Orange"),
]


def _extract_tags_list(product: dict) -> list[str]:
    tags = product.get("tags") or []
    if isinstance(tags, str):
        return [t.strip().lower() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip().lower() for t in tags if str(t).strip()]
    return []


def _extract_price(product: dict) -> float | None:
    variants = product.get("variants") or []
    if variants and isinstance(variants, list):
        v0 = variants[0] if isinstance(variants[0], dict) else {}
        p = v0.get("price")
        if p is not None:
            try:
                return float(p)
            except (ValueError, TypeError):
                pass
    return None


def get_price_bracket(price: float | None) -> str:
    if price is None:
        return "Unknown"
    if price < 800:
        return "Budget"
    if price <= 1800:
        return "Mid-range"
    if price <= 4000:
        return "Premium"
    return "Luxury"


def enrich_product_metadata(product: dict) -> dict[str, Any]:
    """
    Enriches a single raw Shopify product dict with metadata and taxonomy.
    Preserves all original fields and adds a '_metadata' dict.
    """
    title = str(product.get("title") or "").strip()
    product_type = str(product.get("product_type") or "").strip()
    tags = _extract_tags_list(product)
    desc = str(product.get("body_html") or product.get("description") or "")
    
    # Text corpus for feature detection
    text_corpus = f"{title} {product_type} {' '.join(tags)} {desc}".lower()

    # 1. Infer Category and Subcategory
    category = "General"
    sub_category = "General Products"
    matched_subcat = False

    for pattern, cat, subcat in _SUBCATEGORY_RULES:
        if re.search(pattern, text_corpus, re.IGNORECASE):
            category = cat
            sub_category = subcat
            matched_subcat = True
            break

    if not matched_subcat and product_type:
        category = "Apparel" if any(w in product_type.lower() for w in ["shirt", "top", "pant", "hoodie", "jacket", "dress"]) else product_type.title()
        sub_category = product_type.title()

    # 2. Infer Style
    style = "Casual"
    for pattern, detected_style in _STYLE_PATTERNS:
        if re.search(pattern, text_corpus, re.IGNORECASE):
            style = detected_style
            break

    # 3. Infer Fit
    fit = "Regular"
    for pattern, detected_fit in _FIT_PATTERNS:
        if re.search(pattern, text_corpus, re.IGNORECASE):
            fit = detected_fit
            break

    # 4. Infer Gender
    gender = "Unisex"
    for pattern, detected_gender in _GENDER_PATTERNS:
        if re.search(pattern, text_corpus, re.IGNORECASE):
            gender = detected_gender
            break

    # 5. Infer Colors
    detected_colors = []
    # Check variant option values first
    options = product.get("options") or []
    for opt in options:
        if isinstance(opt, dict) and str(opt.get("name", "")).lower() in ("color", "colour"):
            detected_colors.extend([str(v).capitalize() for v in opt.get("values", []) if v])

    # Check text patterns if no variant options matched
    if not detected_colors:
        for pattern, color_name in _COLOR_PATTERNS:
            if re.search(pattern, text_corpus, re.IGNORECASE):
                if color_name not in detected_colors:
                    detected_colors.append(color_name)

    primary_color = detected_colors[0] if detected_colors else "Multicolor"

    # 6. Price Bracket
    price = _extract_price(product)
    price_bracket = get_price_bracket(price)

    # Compile structured metadata
    metadata = {
        "category": category,
        "sub_category": sub_category,
        "style": style,
        "fit": fit,
        "gender": gender,
        "primary_color": primary_color,
        "colors": detected_colors,
        "price_bracket": price_bracket,
        "tags": tags,
    }

    # Return enriched copy
    enriched = dict(product)
    enriched["_metadata"] = metadata
    return enriched


def enrich_catalog(products: list[dict]) -> list[dict]:
    """Batch enriches a list of products with subcategory and style taxonomy."""
    return [enrich_product_metadata(p) for p in products]
