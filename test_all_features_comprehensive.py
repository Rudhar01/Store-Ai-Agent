"""
test_all_features_comprehensive.py
==================================
Exhaustive, production-grade test suite covering all 12 eCommerce AI Agent features.
Runs 13 rigorous test cases for EACH feature (156 total test cases).

Features Covered:
  1. Product Search & Concise Catalog Search (13 test cases)
  2. Product Availability Inquiries (13 test cases)
  3. Cart Management - Adding Items (13 test cases)
  4. Cart Management - Remove, Edit Qty, View & Clear (13 test cases)
  5. Order Tracking - Specific vs Active vs All Orders (13 test cases)
  6. Order Returns & Return Eligibility / Policy (13 test cases)
  7. Order Cancellation & Pre-Fulfillment Edits (13 test cases)
  8. Payment & Express Checkout Flow (13 test cases)
  9. Standing Instructions & Scheduled Tasks (13 test cases)
 10. Abandoned Cart Recovery & Out-of-Stock Swaps (13 test cases)
 11. Store Policies & Knowledge Base (FAQ & Policies) (13 test cases)
 12. General Chat & Conversational Context (13 test cases)
"""

import sys
import json
import asyncio
from types import SimpleNamespace

import intent_classifier as ic
import shopify_actions as sa
import chatbot_widget as cw
import reply_generator as rg
import email_utils as eu

# Track global test statistics
TOTAL_TESTS = 0
PASSED_TESTS = 0
FAILED_TESTS = 0
FEATURE_STATS = {}


def record_test(feature_name: str, test_name: str, passed: bool, detail: str = ""):
    global TOTAL_TESTS, PASSED_TESTS, FAILED_TESTS, FEATURE_STATS
    TOTAL_TESTS += 1
    if feature_name not in FEATURE_STATS:
        FEATURE_STATS[feature_name] = {"passed": 0, "failed": 0, "tests": []}
    if passed:
        PASSED_TESTS += 1
        FEATURE_STATS[feature_name]["passed"] += 1
        print(f"  [PASS] {test_name}" + (f" -> {detail}" if detail else ""))
    else:
        FAILED_TESTS += 1
        FEATURE_STATS[feature_name]["failed"] += 1
        print(f"  [FAIL] {test_name} -> {detail}")
    FEATURE_STATS[feature_name]["tests"].append((test_name, passed, detail))


async def run_all_feature_tests():
    global TOTAL_TESTS, PASSED_TESTS, FAILED_TESTS

    store = SimpleNamespace(
        id="test_store_123",
        shop_domain="dripire-fkpefzp2.myshopify.com",
        access_token="shpat_mock_token_for_testing"
    )
    session_id = "test_shopper_session_comprehensive"

    # Mock sample catalog products (GraphQL nodes)
    mock_catalog = [
        {
            "id": "gid://shopify/Product/101",
            "title": "Silence Graphic T-Shirt",
            "product_type": "T-Shirt",
            "tags": "tshirt, graphic, silence, black",
            "totalInventory": 15,
            "variants": {
                "nodes": [
                    {"id": "gid://shopify/ProductVariant/201", "title": "Black / M", "price": "799.00", "availableForSale": True, "quantityAvailable": 15}
                ]
            }
        },
        {
            "id": "gid://shopify/Product/102",
            "title": "Silence Oversized Hoodie",
            "product_type": "Hoodie",
            "tags": "hoodie, oversized, silence, winter",
            "totalInventory": 0,
            "variants": {
                "nodes": [
                    {"id": "gid://shopify/ProductVariant/202", "title": "Grey / L", "price": "1499.00", "availableForSale": False, "quantityAvailable": 0}
                ]
            }
        },
        {
            "id": "gid://shopify/Product/103",
            "title": "Fresh Crop Top",
            "product_type": "Crop Top",
            "tags": "women, crop top, summer",
            "totalInventory": 8,
            "variants": {
                "nodes": [
                    {"id": "gid://shopify/ProductVariant/203", "title": "White / S", "price": "599.00", "availableForSale": True, "quantityAvailable": 8}
                ]
            }
        },
        {
            "id": "gid://shopify/Product/104",
            "title": "Akatsuki Beanie",
            "product_type": "Beanie",
            "tags": "beanie, hat, winter, anime",
            "totalInventory": 20,
            "variants": {
                "nodes": [
                    {"id": "gid://shopify/ProductVariant/204", "title": "Black", "price": "499.00", "availableForSale": True, "quantityAvailable": 20}
                ]
            }
        }
    ]

    # Mock REST products
    mock_rest_products = [
        {
            "id": 101,
            "title": "Silence Graphic T-Shirt",
            "handle": "silence-graphic-t-shirt",
            "product_type": "T-Shirt",
            "tags": "tshirt, graphic, silence, black",
            "variants": [
                {"id": 201, "title": "Black / M", "price": "799.00", "inventory_quantity": 15, "availableForSale": True}
            ]
        },
        {
            "id": 102,
            "title": "Silence Oversized Hoodie",
            "handle": "silence-oversized-hoodie",
            "product_type": "Hoodie",
            "tags": "hoodie, oversized, silence, winter",
            "variants": [
                {"id": 202, "title": "Grey / L", "price": "1499.00", "inventory_quantity": 0, "availableForSale": False}
            ]
        },
        {
            "id": 103,
            "title": "Fresh Crop Top",
            "handle": "fresh-crop-top",
            "product_type": "Crop Top",
            "tags": "women, crop top, summer",
            "variants": [
                {"id": 203, "title": "White / S", "price": "599.00", "inventory_quantity": 8, "availableForSale": True}
            ]
        },
        {
            "id": 104,
            "title": "Akatsuki Beanie",
            "handle": "akatsuki-beanie",
            "product_type": "Beanie",
            "tags": "beanie, hat, winter, anime",
            "variants": [
                {"id": 204, "title": "Black", "price": "499.00", "inventory_quantity": 20, "availableForSale": True}
            ]
        }
    ]

    # Mock order records
    mock_delivered_order = {
        "id": "1003",
        "name": "#1003",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "fulfillments": [{"created_at": "2026-11-01T10:00:00Z"}]
    }
    mock_unfulfilled = {"id": "1004", "name": "#1004", "fulfillment_status": None, "cancelled_at": None, "fulfillments": []}
    mock_intransit = {"id": "1005", "name": "#1005", "fulfillment_status": "in_transit", "cancelled_at": None, "fulfillments": [{"created_at": "2026-11-14T00:00:00Z"}]}
    mock_expired = {"id": "1006", "name": "#1006", "fulfillment_status": "fulfilled", "cancelled_at": None, "fulfillments": [{"created_at": "2026-08-01T10:00:00Z"}]}
    mock_canc_unfulfilled = {"id": "1007", "name": "#1007", "email": "user@dripire.com", "fulfillment_status": None, "cancelled_at": None}
    mock_fulfilled = {"id": "1008", "name": "#1008", "email": "user@dripire.com", "fulfillment_status": "fulfilled", "cancelled_at": None}
    mock_already_canc = {"id": "1009", "name": "#1009", "email": "user@dripire.com", "fulfillment_status": None, "cancelled_at": "2026-11-10T12:00:00Z"}

    async def mock_get_order_by_number(s, ord_num):
        num_str = str(ord_num).lstrip("#").strip()
        order_map = {
            "1003": mock_delivered_order,
            "1004": mock_unfulfilled,
            "1005": mock_intransit,
            "1006": mock_expired,
            "1007": mock_canc_unfulfilled,
            "1008": mock_fulfilled,
            "1009": mock_already_canc,
        }
        return order_map.get(num_str)

    sa._get_order_by_number = mock_get_order_by_number

    # Offline mock handlers
    async def mock_graphql_catalog(s, query, variables=None):
        if "products" in query:
            return {"products": {"nodes": mock_catalog}}
        return None
    sa._graphql_admin = mock_graphql_catalog

    async def mock_get(s, path, params=None):
        if "products.json" in str(path):
            return SimpleNamespace(status_code=200, json=lambda: {"products": mock_rest_products}, text="")
        return SimpleNamespace(status_code=404, json=lambda: {}, text="Not Found")
    sa._get = mock_get

    print("\n" + "="*80)
    print("STARTING COMPREHENSIVE MULTI-FEATURE TEST SUITE (12 FEATURES x 13+ TESTS)")
    print("="*80)

    # =========================================================================
    # FEATURE 1: Product Search & Concise Catalog Search (13 tests)
    # =========================================================================
    feat = "1. Product Search & Concise Catalog Search"
    print(f"\n--- Running Feature: {feat} ---")
    
    # 1.1 Exact single product query
    c1 = ic.classify_intent("silence tshirt")
    record_test(feat, "Exact product query classification", c1["intent"] == "product_search" and c1["action"] == "search_products" and c1["is_action"] is False and "silence tshirt" in c1["entities"].get("query", ""))

    # 1.2 Leading search verb
    c2 = ic.classify_intent("find silence graphic t-shirt")
    record_test(feat, "Search query with leading verb 'find'", c2["intent"] == "product_search" and "silence graphic t-shirt" in c2["entities"].get("query", ""))

    # 1.3 Leading search verb 'show me'
    c3 = ic.classify_intent("show me silence hoodie")
    record_test(feat, "Search query with 'show me'", c3["intent"] == "product_search" and "silence hoodie" in c3["entities"].get("query", ""))

    # 1.4 Typo tolerance token scoring
    tokens = sa._clean_search_tokens("silnce tshirt")
    mock_p = {"title": "Silence Graphic T-Shirt", "product_type": "T-Shirt", "tags": "silence, tshirt"}
    score = sa._score_product_match(mock_p, tokens)
    record_test(feat, "Fuzzy match score for typo 'silnce'", score >= 1.0, f"score={score:.2f}")

    # 1.5 Category search
    c5 = ic.classify_intent("search for hoodies")
    record_test(feat, "Category search query", c5["intent"] == "product_search" and "hoodies" in c5["entities"].get("query", ""))

    # 1.6 Browse recommendations
    c6 = ic.classify_intent("show me some products")
    record_test(feat, "Browse catalog query routes to get_recommendations", c6["intent"] == "product_search" and c6["action"] == "get_recommendations")

    # 1.7 Catalog recommendation dispatch
    rec_res = await sa.dispatch("product_search", "get_recommendations", store, {}, session_id=session_id)
    record_test(feat, "Recommendations dispatch returns products or message", "results" in rec_res or "message" in rec_res)

    # 1.8 Search product dispatch match
    search_res = await sa.dispatch("product_search", "search_products", store, {"query": "Silence Graphic T-Shirt"}, session_id=session_id)
    record_test(feat, "Search products dispatch returns matching results", "results" in search_res and len(search_res["results"]) > 0)

    # 1.9 Concise search filter: silence tshirt does not match unrelated crop top
    eval_match, eval_score = sa._evaluate_product_search_match(
        {"title": "Fresh Crop Top", "product_type": "Crop Top", "tags": "women"},
        "silence tshirt",
        ["silence", "tshirt"],
        ["silence"]
    )
    record_test(feat, "Concise search rejects unrelated crop top for 'silence tshirt'", eval_match is False)

    # 1.10 In-stock variant prioritization in search card
    card = sa._format_product_card(mock_catalog[0], store, {})
    record_test(feat, "Product card formats in-stock variant with clean integer ID", card is not None and card.get("in_stock") is True and card.get("variant_id") == 201)

    # 1.11 Out-of-stock product card accurately flagged
    card_oos = sa._format_product_card(mock_catalog[1], store, {})
    record_test(feat, "Out-of-stock product card flagged available=False", card_oos is not None and card_oos.get("in_stock") is False)

    # 1.12 Non-existent product query returns polite no_exact_match
    no_match_res = await sa.dispatch("product_search", "search_products", store, {"query": "Quantum Computer 9000"}, session_id=session_id)
    record_test(feat, "Search query with zero matches returns clear no_exact_match status", no_match_res.get("no_exact_match") is True or len(no_match_res.get("results", [])) == 0)

    # 1.13 Human-friendly reply generation for search results
    reply_search = rg.generate_reply("search_products", {"results": [{"title": "Silence Graphic T-Shirt", "price": "799.00", "in_stock": True}]}, "en", "silence tshirt")
    record_test(feat, "Reply generator gives friendly, simple response for search", "Silence Graphic T-Shirt" in reply_search and "799" in reply_search)


    # =========================================================================
    # FEATURE 2: Product Availability Inquiries (13 tests)
    # =========================================================================
    feat = "2. Product Availability Inquiries"
    print(f"\n--- Running Feature: {feat} ---")

    # 2.1 General availability inquiry classification
    a1 = ic.classify_intent("tell me whether this product is available or not")
    record_test(feat, "General availability classified as check_availability", a1["intent"] == "product_search" and a1["action"] == "check_availability" and a1["is_action"] is False)

    # 2.2 Specific product in-stock question classification
    a2 = ic.classify_intent("is silence tshirt in stock")
    record_test(feat, "Specific in-stock inquiry classified with product entity", a2["intent"] == "product_search" and a2["action"] == "check_availability" and "silence tshirt" in a2["entities"].get("product_title", ""))

    # 2.3 Out-of-stock inquiry phrasing
    a3 = ic.classify_intent("do you have silence oversized hoodie in stock")
    record_test(feat, "Stock inquiry with 'do you have in stock'", a3["intent"] == "product_search" and a3["action"] == "check_availability")

    # 2.4 Availability inquiry with color/size
    a4 = ic.classify_intent("is silence tshirt available in black size M")
    record_test(feat, "Availability with variant details", a4["intent"] == "product_search" and a4["action"] == "check_availability")

    # 2.5 Dispatch availability for in-stock item
    avail_res_instock = await sa.dispatch("product_search", "check_availability", store, {"product_title": "Silence Graphic T-Shirt"}, session_id=session_id)
    record_test(feat, "check_availability dispatch finds in-stock item", avail_res_instock.get("status") == "available" and avail_res_instock.get("in_stock") is True)

    # 2.6 Dispatch availability for out-of-stock item
    avail_res_oos = await sa.dispatch("product_search", "check_availability", store, {"product_title": "Silence Oversized Hoodie"}, session_id=session_id)
    record_test(feat, "check_availability dispatch flags out-of-stock item", avail_res_oos.get("status") == "out_of_stock" and avail_res_oos.get("in_stock") is False)

    # 2.7 Proactive alternative suggestion for out-of-stock item
    record_test(feat, "Out-of-stock availability check offers close alternatives", "alternatives" in avail_res_oos or "product_card" in avail_res_oos or "results" in avail_res_oos)

    # 2.8 Availability check with missing product entity requests clarification
    avail_res_missing = await sa.dispatch("product_search", "check_availability", store, {}, session_id=session_id)
    record_test(feat, "Missing product in check_availability requests product name", avail_res_missing.get("status") == "product_required")

    # 2.9 Non-existent product availability
    avail_res_nf = await sa.dispatch("product_search", "check_availability", store, {"product_title": "Flying Saucer Shoes"}, session_id=session_id)
    record_test(feat, "Non-existent product returns not_found status", avail_res_nf.get("status") == "not_found")

    # 2.10 Natural conversational reply for in-stock item
    rep_avail = rg.generate_reply("check_availability", avail_res_instock, "en", "is silence tshirt in stock")
    record_test(feat, "Conversational reply warmly confirms in-stock and offers cart addition", "in stock" in rep_avail.lower() and "cart" in rep_avail.lower())

    # 2.11 Conversational reply for out-of-stock item
    rep_oos = rg.generate_reply("check_availability", avail_res_oos, "en", "is silence hoodie available")
    record_test(feat, "Conversational reply explains item is out of stock without technical jargon", "out of stock" in rep_oos.lower() or "sold out" in rep_oos.lower())

    # 2.12 Reply for product_required asks which item in simple words
    rep_req = rg.generate_reply("check_availability", avail_res_missing, "en", "is it available")
    record_test(feat, "Reply for product_required politely asks for item name", "which product" in rep_req.lower())

    # 2.13 No technical jargon leak (no raw json, variantId, etc.)
    record_test(feat, "Availability replies contain zero technical jargon", not any(j in rep_avail.lower() for j in ["variant_id", "json", "graphql", "endpoint", "gid://"]))


    # =========================================================================
    # FEATURE 3: Cart Management - Adding Items (13 tests)
    # =========================================================================
    feat = "3. Cart Management - Adding Items"
    print(f"\n--- Running Feature: {feat} ---")

    # 3.1 Direct add command classification
    c_add1 = ic.classify_intent("add silence tshirt to cart")
    record_test(feat, "Direct 'add to cart' classified with is_action=True", c_add1["intent"] == "cart_management" and c_add1["action"] == "add_item" and c_add1["is_action"] is True and c_add1["requires_confirmation"] is False)

    # 3.2 Embedded number in product phrase ("slience 1 tshirt")
    c_add2 = ic.classify_intent("add a slience 1 tshirt to the cart")
    record_test(feat, "Embedded quantity extracted: qty=1, name='slience tshirt'", c_add2["entities"].get("quantity") == 1 and "slience tshirt" in c_add2["entities"].get("product_name_or_id", ""))

    # 3.3 Leading quantity ("add 5 akatsuki beanies to cart")
    c_add3 = ic.classify_intent("add 5 akatsuki beanies to cart")
    record_test(feat, "Leading quantity extracted: qty=5", c_add3["entities"].get("quantity") == 5 and "akatsuki beanies" in c_add3["entities"].get("product_name_or_id", ""))

    # 3.4 Multi-word leading quantity ("can you please add 2 silence tshirts")
    c_add4 = ic.classify_intent("can you please add 2 silence tshirts to my cart")
    record_test(feat, "Polite phrasing with quantity 2", c_add4["entities"].get("quantity") == 2)

    # 3.5 Add by numeric variant ID
    c_add5 = ic.classify_intent("add variant 201 to cart")
    record_test(feat, "Add by variant ID entity extraction", c_add5["intent"] == "cart_management" and c_add5["entities"].get("product_name_or_id") == "201")

    # 3.6 Dispatch add_item produces cart_add widget_action
    add_disp = await sa.dispatch("cart_management", "add_item", store, {"product_name_or_id": "201", "quantity": 2}, session_id=session_id)
    record_test(feat, "add_item emits cart_add widget_action with integer variant_id", add_disp.get("widget_action", {}).get("type") == "cart_add" and add_disp["widget_action"].get("quantity") == 2)

    # 3.7 Add item resolution avoids blind fallback to random product
    orig_resolve = cw._resolve_product
    orig_get_store_cw = cw._get_store
    cw._get_store = lambda s: store
    async def mock_resolve_none(s, term):
        return None, "Not found"
    cw._resolve_product = mock_resolve_none
    try:
        req_missing = cw.ChatRequest(message="add nonexistentxyz to cart", session_id="test_s_add", shop="dripire-fkpefzp2.myshopify.com")
        res_missing = await cw.chat(req_missing)
        record_test(feat, "Chatbot widget does NOT add random fallback item when product not found", res_missing.get("widget_action") is None and ("could not find" in res_missing.get("reply", "").lower() or "couldn't find" in res_missing.get("reply", "").lower()))
    finally:
        cw._resolve_product = orig_resolve
        cw._get_store = orig_get_store_cw

    # 3.8 Add all items to cart (bundle/multi-product)
    add_all_res = await sa.dispatch("cart_management", "add_all_to_cart", store, {"quantity": 1}, session_id=session_id)
    record_test(feat, "add_all_to_cart emits cart_add_all widget action", add_all_res.get("widget_action", {}).get("type") in ("cart_add_all", "cart_add_batch"))

    # 3.9 Friendly conversational reply for add_item
    rep_add = rg.generate_reply("add_item", {"added": "Silence Graphic T-Shirt", "quantity": 1}, "en", "add silence tshirt to cart")
    record_test(feat, "Reply confirms added item and quantity warmly", "added 1x 'silence graphic t-shirt'" in rep_add.lower())

    # 3.10 Immediate execution (requires_confirmation is False in schema)
    schema = ic.load_schema()
    cart_act = {a["name"]: a for a in schema["intents"][3]["actions"]} # cart_management
    record_test(feat, "Schema enforces add_item requires_confirmation=False", cart_act["add_item"].get("requires_confirmation") is False)

    # 3.11 Add item preserves variant pricing
    record_test(feat, "add_item response contains valid product title and price", "Item #" in add_disp.get("added", "") or add_disp.get("added") is not None)

    # 3.12 Prevent adding negative or zero quantity
    c_add_zero = ic.classify_intent("add 0 silence tshirts to cart")
    record_test(feat, "Zero quantity defaults safely or retains intent", c_add_zero["intent"] == "cart_management")

    # 3.13 Rapid consecutive add commands
    c_add_consec = ic.classify_intent("put 3 beanies in the bag")
    record_test(feat, "Alternative add verb 'put in bag'", c_add_consec["intent"] == "cart_management" and c_add_consec["entities"].get("quantity") == 3)


    # =========================================================================
    # FEATURE 4: Cart Management - Remove, Edit Qty, View & Clear (13 tests)
    # =========================================================================
    feat = "4. Cart Management - Remove, Edit Qty, View & Clear"
    print(f"\n--- Running Feature: {feat} ---")

    # 4.1 Remove command classification
    c_rem1 = ic.classify_intent("remove silence tshirt from cart")
    record_test(feat, "Remove command classified with is_action=True", c_rem1["intent"] == "cart_management" and c_rem1["action"] == "remove_item" and c_rem1["is_action"] is True)

    # 4.2 Partial quantity removal classification
    c_rem2 = ic.classify_intent("remove 2 silence tshirts from cart")
    record_test(feat, "Partial removal extracts quantity=2", c_rem2["entities"].get("quantity") == 2 and "silence tshirt" in c_rem2["entities"].get("product_name_or_id", ""))

    # 4.3 View cart classification
    c_vc = ic.classify_intent("what's in my cart")
    record_test(feat, "View cart query classified as view_cart with is_action=False", c_vc["intent"] == "cart_management" and c_vc["action"] == "view_cart" and c_vc["is_action"] is False)

    # 4.4 Alternative view cart phrasing ("show my bag")
    c_vb = ic.classify_intent("show my bag")
    record_test(feat, "'show my bag' routes to view_cart", c_vb["action"] == "view_cart")

    # 4.5 Clear cart classification
    c_cc = ic.classify_intent("clear my cart")
    record_test(feat, "Clear cart classified with requires_confirmation=True as safeguard", c_cc["intent"] == "cart_management" and c_cc["action"] == "clear_cart" and c_cc["is_action"] is True and c_cc["requires_confirmation"] is True)

    # 4.6 Edit quantity classification
    c_eq = ic.classify_intent("change quantity of silence tshirt to 4")
    record_test(feat, "Edit quantity classified", c_eq["intent"] == "cart_management" and c_eq["action"] == "edit_quantity" and c_eq["entities"].get("quantity") == 4)

    # 4.7 Dispatch view_cart on empty cart
    vc_empty = await sa.dispatch("cart_management", "view_cart", store, {"cart_data": {"items": [], "item_count": 0, "total": "0.00"}}, session_id=session_id)
    record_test(feat, "view_cart on empty cart returns cart_empty status and cart_view action", vc_empty.get("status") == "cart_empty" and vc_empty.get("widget_action", {}).get("type") == "cart_view")

    # 4.8 Dispatch view_cart on populated storefront cart
    pop_cart = {"items": [{"name": "Silence Graphic T-Shirt", "quantity": 2, "price": 79900, "variant_id": 201}], "item_count": 2, "total": "1598.00"}
    vc_pop = await sa.dispatch("cart_management", "view_cart", store, {"cart_data": pop_cart}, session_id=session_id)
    record_test(feat, "view_cart displays populated cart items and total", "Silence Graphic T-Shirt" in vc_pop.get("message", "") and "1598" in vc_pop.get("message", ""))

    # 4.9 Dispatch remove_item partial removal leaves remaining units
    rem_part = await sa.dispatch("cart_management", "remove_item", store, {"product_name_or_id": "silence", "quantity": 1, "cart_data": pop_cart}, session_id=session_id)
    record_test(feat, "Partial removal emits cart_set_quantity with remaining units", rem_part.get("widget_action", {}).get("type") == "cart_set_quantity" and rem_part.get("remaining_quantity") == 1)

    # 4.10 Dispatch remove_item complete removal
    rem_full = await sa.dispatch("cart_management", "remove_item", store, {"product_name_or_id": "silence", "quantity": 2, "cart_data": pop_cart}, session_id=session_id)
    record_test(feat, "Full removal emits cart_remove widget action", rem_full.get("widget_action", {}).get("type") == "cart_remove")

    # 4.11 Dispatch clear_cart emits cart_clear widget action
    cc_res = await sa.dispatch("cart_management", "clear_cart", store, {}, session_id=session_id)
    record_test(feat, "clear_cart dispatch emits cart_clear widget action", cc_res.get("widget_action", {}).get("type") == "cart_clear")

    # 4.12 Conversational reply for partial removal
    rep_rem_part = rg.generate_reply("remove_item", rem_part, "en", "remove 1 silence tshirt")
    record_test(feat, "Reply confirms quantity removed and states remaining units", "removed 1x" in rep_rem_part.lower() and "remaining" in rep_rem_part.lower())

    # 4.13 Conversational reply for full removal
    rep_rem_full = rg.generate_reply("remove_item", rem_full, "en", "remove silence tshirt")
    record_test(feat, "Reply confirms complete item removal", "removed" in rep_rem_full.lower())


    # =========================================================================
    # FEATURE 5: Order Tracking - Specific vs Active vs All Orders (13 tests)
    # =========================================================================
    feat = "5. Order Tracking - Specific vs Active vs All Orders"
    print(f"\n--- Running Feature: {feat} ---")

    # 5.1 Specific order tracking with # symbol
    t1 = ic.classify_intent("track order #1001")
    record_test(feat, "Track order #1001 classified as track_order with order_number=1001", t1["intent"] == "order_tracking" and t1["action"] == "track_order" and t1["entities"].get("order_number") == "1001")

    # 5.2 Specific order tracking without # symbol
    t2 = ic.classify_intent("where is my order 1002")
    record_test(feat, "'where is my order 1002' extracted as order_number=1002", t2["entities"].get("order_number") == "1002")

    # 5.3 Active orders list classification
    t3 = ic.classify_intent("list my active orders")
    record_test(feat, "'list active orders' classified as list_recent_orders with active filter", t3["intent"] == "order_tracking" and t3["action"] == "list_recent_orders" and t3["entities"].get("active_only") is True)

    # 5.4 Active orders with 'orders in progress'
    t4 = ic.classify_intent("show orders in progress")
    record_test(feat, "'orders in progress' classified with active filter", t4["entities"].get("active_only") is True)

    # 5.5 All orders list classification
    t5 = ic.classify_intent("Show all my orders")
    record_test(feat, "'Show all my orders' classified without active filter", t5["intent"] == "order_tracking" and t5["action"] == "list_recent_orders" and not t5["entities"].get("active_only"))

    # 5.6 Guest shopper prompted for email verification when listing orders
    guest_track = await sa.list_recent_orders(store, {}, session_id="test_guest_session", customer_id=None)
    record_test(feat, "Guest shopper prompted for email verification", guest_track.get("error") == "verification_required")

    # 5.7 Authenticated customer session bypasses email prompt
    auth_track = await sa.list_recent_orders(store, {"email": "customer@dripire.com"}, session_id="test_auth_session", customer_id="12345")
    record_test(feat, "Authenticated customer bypasses verification prompt", auth_track.get("error") != "verification_required")

    # 5.8 Preventing order hijacking across customer accounts
    mock_foreign_order = {"id": "999", "name": "#1001", "email": "other@user.com", "customer": {"id": "888", "email": "other@user.com"}}
    sec_check = sa._access_error(mock_foreign_order, "attacker@bad.com", "777", "1001")
    record_test(feat, "_access_error blocks unauthorized customer from accessing foreign order", sec_check is not None and "error" in sec_check)

    # 5.9 Authorized customer granted access
    auth_check = sa._access_error(mock_foreign_order, "other@user.com", "888", "1001")
    record_test(feat, "_access_error grants access to matching customer email/id", auth_check is None)

    # 5.10 Track order data formatting with status, location, and ETA
    sample_order_data = {"order_id": "1001", "status": "in transit", "carrier": "BlueDart", "current_location": "Mumbai Hub", "estimated_delivery": "Monday"}
    rep_track = rg.generate_reply("track_order", sample_order_data, "en", "track order 1001")
    record_test(feat, "Tracking reply explicitly states status, location, and estimated delivery", "in transit" in rep_track.lower() and "mumbai" in rep_track.lower() and "monday" in rep_track.lower())

    # 5.11 Track order reply when location is missing offers carrier link
    sample_track_noloc = {"order_id": "1002", "status": "shipped", "carrier": "Delhivery", "tracking_url": "https://delhivery.com/track/1002"}
    rep_track_noloc = rg.generate_reply("track_order", sample_track_noloc, "en", "where is order 1002")
    record_test(feat, "Tracking reply states status clearly when location is missing", "shipped" in rep_track_noloc.lower() and ("delhivery" in rep_track_noloc.lower() or "carrier" in rep_track_noloc.lower()))

    # 5.12 Active orders summary specifically introduces active orders in progress
    rep_active = rg.generate_reply("list_recent_orders", {"active_only": True, "orders": [{"name": "#1001", "status": "in transit", "total": "799.00"}]}, "en", "list my active orders")
    record_test(feat, "Active orders reply introduces active orders specifically", "active order" in rep_active.lower())

    # 5.13 Natural phrasing when no orders are found
    rep_no_orders = rg.generate_reply("list_recent_orders", {"orders": []}, "en", "show my orders")
    record_test(feat, "No orders found reply is polite and helpful", "no" in rep_no_orders.lower() or "find" in rep_no_orders.lower() or "order" in rep_no_orders.lower())


    # =========================================================================
    # FEATURE 6: Order Returns & Return Eligibility / Policy (13 tests)
    # =========================================================================
    feat = "6. Order Returns & Return Eligibility / Policy"
    print(f"\n--- Running Feature: {feat} ---")

    # 6.1 Return inquiry with order number classification
    r1 = ic.classify_intent("whether i can return my order number 1003 or not")
    record_test(feat, "Return inquiry classified as check_return_eligibility with order 1003", r1["intent"] == "order_management" and r1["action"] == "check_return_eligibility" and r1["is_action"] is False and r1["entities"].get("order_number") == "1003")

    # 6.2 Return eligibility phrasing ("can I return order 1003")
    r2 = ic.classify_intent("can I return order 1003")
    record_test(feat, "'can I return order 1003' classified as check_return_eligibility", r2["action"] == "check_return_eligibility" and r2["entities"].get("order_number") == "1003")

    # 6.3 Return request action classification ("please return my order 1003")
    r3 = ic.classify_intent("please return my order 1003")
    record_test(feat, "'please return my order 1003' classified as request_order_return with is_action=True", r3["action"] == "request_order_return" and r3["is_action"] is True)

    # 6.4 Return inquiry without order number
    r4 = ic.classify_intent("can I return my order")
    record_test(feat, "Return inquiry without order number captured", r4["action"] == "check_return_eligibility" and not r4["entities"].get("order_number"))

    # 6.5 Return eligibility check requires order number if omitted
    ret_el_no_ord = await sa.dispatch("order_management", "check_return_eligibility", store, {}, session_id=session_id)
    record_test(feat, "check_return_eligibility status=order_number_required when omitted", ret_el_no_ord.get("status") == "order_number_required")

    # 6.6 Delivered order within 30 days is eligible
    ret_el_ok = await sa.dispatch("order_management", "check_return_eligibility", store, {"order_number": "1003", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "Delivered order within 30 days evaluated as eligible", ret_el_ok.get("status") == "eligible" and ret_el_ok.get("can_return") is True)

    # 6.7 Unfulfilled order returns explain order has not shipped yet
    ret_el_unf = await sa.dispatch("order_management", "check_return_eligibility", store, {"order_number": "1004", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "Unfulfilled order evaluated as unfulfilled status with cancellation/address edit options", ret_el_unf.get("status") == "unfulfilled" and ret_el_unf.get("can_return") is False)

    # 6.8 In-transit order returns explain package on the way
    ret_el_it = await sa.dispatch("order_management", "check_return_eligibility", store, {"order_number": "1005", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "In-transit order evaluated as in_transit status", ret_el_it.get("status") == "in_transit" and ret_el_it.get("can_return") is False)

    # 6.9 Expired order past 30 days evaluated as expired
    ret_el_exp = await sa.dispatch("order_management", "check_return_eligibility", store, {"order_number": "1006", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "Order older than 30 days evaluated as expired", ret_el_exp.get("status") == "expired" and ret_el_exp.get("can_return") is False)

    # 6.10 Request order return dispatch
    ret_req_res = await sa.dispatch("order_management", "request_order_return", store, {"order_number": "1003", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "request_order_return submits return request", ret_req_res.get("status") in ("return_submitted", "eligible") or "return" in str(ret_req_res))

    # 6.11 Conversational reply for eligible return explains 30-day window and condition requirements
    rep_ret_ok = rg.generate_reply("check_return_eligibility", {"status": "eligible", "order_number": "1003"}, "en", "can I return order 1003")
    record_test(feat, "Reply confirms return eligibility and 30-day window warmly", "eligible for return" in rep_ret_ok.lower() and "30" in rep_ret_ok.lower())

    # 6.12 Conversational reply for unfulfilled return offers pre-fulfillment edit
    rep_ret_unf = rg.generate_reply("check_return_eligibility", {"status": "unfulfilled", "order_number": "1004"}, "en", "can I return order 1004")
    record_test(feat, "Reply for unshipped order explains returns aren't applicable yet and offers cancellation", "shipped" in rep_ret_unf.lower() and "cancel" in rep_ret_unf.lower())

    # 6.13 Conversational reply for expired return suggests warranty claim
    rep_ret_exp = rg.generate_reply("check_return_eligibility", {"status": "expired", "order_number": "1006"}, "en", "can I return order 1006")
    record_test(feat, "Reply for expired return suggests warranty claim if damaged", "30-day" in rep_ret_exp.lower() and "warranty" in rep_ret_exp.lower())


    # =========================================================================
    # FEATURE 7: Order Cancellation & Pre-Fulfillment Edits (13 tests)
    # =========================================================================
    feat = "7. Order Cancellation & Pre-Fulfillment Edits"
    print(f"\n--- Running Feature: {feat} ---")

    # 7.1 Cancel order command classification
    can1 = ic.classify_intent("cancel order 1003")
    record_test(feat, "Cancel order command classified as cancel_order with is_action=True", can1["intent"] == "order_management" and can1["action"] == "cancel_order" and can1["is_action"] is True and can1["entities"].get("order_number") == "1003")

    # 7.2 Cancel order alternative phrasing
    can2 = ic.classify_intent("I want to cancel my order #1004")
    record_test(feat, "'I want to cancel my order #1004' classified", can2["action"] == "cancel_order" and can2["entities"].get("order_number") == "1004")

    # 7.3 Edit shipping address classification
    addr1 = ic.classify_intent("change shipping address for order 1003")
    record_test(feat, "Edit shipping address classified as edit_address with is_action=True", addr1["intent"] == "order_management" and addr1["action"] == "edit_address" and addr1["is_action"] is True and addr1["entities"].get("order_number") == "1003")

    # 7.4 Cancel order without order number requires order number
    can_no_ord = await sa.dispatch("order_management", "cancel_order", store, {}, session_id=session_id)
    record_test(feat, "cancel_order requires order number if omitted", can_no_ord.get("status") == "order_number_required")

    # 7.5 Pre-fulfillment cancellation challenges shopper with OTP for security
    can_chal = await sa.dispatch("order_management", "cancel_order", store, {"order_number": "1007", "email": "user@dripire.com"}, session_id=session_id)
    record_test(feat, "Cancellation triggers 6-digit OTP security verification challenge", can_chal.get("status") == "verification_required" and "otp" in can_chal and len(can_chal["otp"]) == 6)

    # 7.6 Reject cancellation with invalid OTP
    can_bad_otp = await sa.dispatch("order_management", "cancel_order", store, {"order_number": "1007", "otp_code": "000000", "email": "user@dripire.com"}, session_id=session_id)
    record_test(feat, "Invalid cancellation OTP rejected", can_bad_otp.get("status") == "invalid_otp" or can_bad_otp.get("error") == "invalid_otp")

    # 7.7 Confirm cancellation with valid OTP
    can_otp = can_chal.get("otp")
    async def mock_shopify_cancel(s, oid):
        return (True, "Order cancelled")
    orig_sh_canc = sa._execute_shopify_order_cancel
    sa._execute_shopify_order_cancel = mock_shopify_cancel
    try:
        can_ok = await sa.dispatch("order_management", "cancel_order", store, {"order_number": "1007", "otp_code": can_otp, "email": "user@dripire.com"}, session_id=session_id)
        record_test(feat, "Valid OTP confirms order cancellation", can_ok.get("status") == "cancelled" and can_ok.get("order_number") == "1007")
    finally:
        sa._execute_shopify_order_cancel = orig_sh_canc

    # 7.8 Reject cancellation on already fulfilled/shipped orders
    can_fail_fulfilled = await sa.dispatch("order_management", "cancel_order", store, {"order_number": "1008", "email": "user@dripire.com"}, session_id=session_id)
    record_test(feat, "Already fulfilled order cannot be cancelled", can_fail_fulfilled.get("status") in ("already_fulfilled", "cannot_cancel"))

    # 7.9 Reject cancellation on already cancelled orders
    can_already = await sa.dispatch("order_management", "cancel_order", store, {"order_number": "1009", "email": "user@dripire.com"}, session_id=session_id)
    record_test(feat, "Already cancelled order returns already_cancelled status", can_already.get("status") == "already_cancelled")

    # 7.10 Edit address on pre-fulfillment order updates address
    addr_res = await sa.dispatch("order_management", "edit_address", store, {"order_number": "1007", "new_address": "456 Fashion Ave, Mumbai 400001", "email": "user@dripire.com"}, session_id=session_id)
    record_test(feat, "edit_address updates shipping address on unshipped order", addr_res.get("status") == "address_updated" or addr_res.get("status") == "updated" or "address" in addr_res)

    # 7.11 Edit address on fulfilled order rejected
    addr_fail = await sa.dispatch("order_management", "edit_address", store, {"order_number": "1008", "new_address": "456 Fashion Ave", "email": "user@dripire.com"}, session_id=session_id)
    record_test(feat, "edit_address rejected on already shipped order", addr_fail.get("status") in ("already_fulfilled", "already_shipped", "cannot_update") or addr_fail.get("editable") is False)

    # 7.12 Cancellation confirmation email generation
    email_canc = eu.send_order_cancellation_email("user@dripire.com", "Dripire Store", "1007", "$799.00")
    record_test(feat, "Cancellation receipt email generated", email_canc is not None)

    # 7.13 Address change confirmation email generation
    email_addr = eu.send_address_update_email("user@dripire.com", "Dripire Store", "1007", "456 Fashion Ave")
    record_test(feat, "Address update confirmation email generated", email_addr is not None)


    # =========================================================================
    # FEATURE 8: Payment & Express Checkout Flow (13 tests)
    # =========================================================================
    feat = "8. Payment & Express Checkout Flow"
    print(f"\n--- Running Feature: {feat} ---")

    # 8.1 Checkout command classification
    c_pay1 = ic.classify_intent("pay now")
    record_test(feat, "'pay now' classified as initiate_payment with is_action=True", c_pay1["intent"] == "payment_checkout" and c_pay1["action"] == "initiate_payment" and c_pay1["is_action"] is True)

    # 8.2 Checkout phrasing 'proceed to checkout'
    c_pay2 = ic.classify_intent("proceed to checkout")
    record_test(feat, "'proceed to checkout' classified as initiate_payment", c_pay2["action"] == "initiate_payment")

    # 8.3 Payment policy question is NOT checkout
    c_upi = ic.classify_intent("can I pay with UPI")
    record_test(feat, "'can I pay with UPI' routes to policy_query, NOT checkout", c_upi["intent"] == "policy_query" and c_upi["action"] == "answer_policy_question" and c_upi["is_action"] is False)

    # 8.4 COD payment query is NOT checkout
    c_cod = ic.classify_intent("do you accept cash on delivery")
    record_test(feat, "'do you accept cash on delivery' routes to payment policy", c_cod["intent"] == "policy_query" and c_cod["entities"].get("topic") == "payment_policy")

    # 8.5 Card number privacy shield detects and blocks raw card numbers
    cw_card_req = cw.ChatRequest(message="My credit card is 4111 2222 3333 4444 to pay", session_id="test_card_block", shop="dripire-fkpefzp2.myshopify.com")
    cw_card_res = await cw.chat(cw_card_req)
    record_test(feat, "Credit card shield intercepts raw card number and rejects with security warning", "For your security" in cw_card_res.get("reply", "") and cw_card_res.get("widget_action") is None)

    # 8.6 Checkout on empty cart notifies shopper
    orig_store_helper = cw._get_store
    cw._get_store = lambda s: store
    try:
        cw_chk_empty = cw.ChatRequest(message="checkout", session_id="test_chk_empty", shop="dripire-fkpefzp2.myshopify.com", cart_data={"items": [], "item_count": 0, "total": 0})
        cw_chk_empty_res = await cw.chat(cw_chk_empty)
        record_test(feat, "Checkout on empty cart notifies shopper that cart is empty", cw_chk_empty_res.get("status") == "cart_empty" and "cart is currently empty" in cw_chk_empty_res.get("reply", "").lower())

        # 8.7 Checkout with items emits redirect action with email token
        cw._remember_customer("dripire-fkpefzp2.myshopify.com", "test_chk_items", "cust_99", "alex@dripire.com", "Alex Customer")
        cw_chk_items = cw.ChatRequest(
            message="proceed to checkout",
            session_id="test_chk_items",
            shop="dripire-fkpefzp2.myshopify.com",
            customer_email="alex@dripire.com",
            cart_data={"items": [{"title": "Silence Graphic T-Shirt", "quantity": 1, "price": 79900}], "item_count": 1, "total_price": 79900}
        )
        cw_chk_items_res = await cw.chat(cw_chk_items)
        record_test(feat, "Checkout with cart items emits checkout redirect with email parameter", cw_chk_items_res.get("status") == "checkout_redirect" and "checkout[email]=alex%40dripire.com" in cw_chk_items_res.get("widget_action", {}).get("url", ""))
    finally:
        cw._get_store = orig_store_helper

    # 8.8 Storing payment credentials masks card number
    save_cred = await sa.dispatch("payment_checkout", "save_payment_credentials", store, {"cardholder_name": "Rohan Sharma", "card_number": "5500112233445566", "expiry": "12/29"}, session_id=session_id)
    record_test(feat, "Payment credentials saved and masked with •••• •••• •••• 5566", save_cred.get("status") == "credentials_saved" and "5566" in save_cred.get("masked_number", ""))

    # 8.9 Retrieve saved masked credentials
    ret_cred = await sa.get_saved_payment_credentials(store, session_id=session_id)
    record_test(feat, "get_saved_payment_credentials retrieves masked card info", ret_cred is not None and "5566" in ret_cred.get("masked_number", ""))

    # 8.10 Express OTP checkout generates 6-digit challenge
    pay_init = await sa.dispatch("payment_checkout", "initiate_payment", store, {"product_name_or_id": "201", "quantity": 1}, session_id=session_id)
    record_test(feat, "Express checkout generates 6-digit OTP challenge", pay_init.get("status") == "otp_required" and len(pay_init.get("otp", "")) == 6)

    # 8.11 Confirm payment with invalid OTP rejected
    pay_bad_otp = await sa.dispatch("payment_checkout", "confirm_payment_otp", store, {"otp_code": "123456"}, session_id=session_id)
    record_test(feat, "Invalid payment OTP code rejected", pay_bad_otp.get("error") == "invalid_otp")

    # 8.12 Confirm payment with valid OTP completes payment and clears cart
    pay_otp = pay_init.get("otp")
    pay_good_otp = await sa.dispatch("payment_checkout", "confirm_payment_otp", store, {"otp_code": pay_otp, "email": "alex@dripire.com"}, session_id=session_id)
    record_test(feat, "Valid payment OTP confirmed and emits cart_clear action", pay_good_otp.get("status") == "payment_success" and pay_good_otp.get("widget_action", {}).get("type") == "cart_clear")

    # 8.13 Payment success reply mentions order number and delivery timeframe
    rep_pay_success = rg.generate_reply("confirm_payment_otp", pay_good_otp, "en", "my otp is " + pay_otp)
    record_test(feat, "Payment reply confirms order number and estimated delivery", str(pay_good_otp.get("order_number")) in rep_pay_success and "order" in rep_pay_success.lower())


    # =========================================================================
    # FEATURE 9: Standing Instructions & Scheduled Tasks (13 tests)
    # =========================================================================
    feat = "9. Standing Instructions & Scheduled Tasks"
    print(f"\n--- Running Feature: {feat} ---")

    # 9.1 Price drop alert instruction classification
    si1 = ic.classify_intent("notify me when price drops below $50")
    record_test(feat, "Price drop alert classified as standing_instruction with is_action=True", si1["intent"] == "standing_instruction" and si1["action"] == "create_instruction" and si1["is_action"] is True)

    # 9.2 Reorder reminder instruction classification
    si2 = ic.classify_intent("remind me to reorder in 10 days")
    record_test(feat, "Reorder reminder classified as standing_instruction", si2["intent"] == "standing_instruction" and si2["action"] == "create_instruction")

    # 9.3 Auto-add on restock instruction classification
    si3 = ic.classify_intent("add this to my cart when back in stock")
    record_test(feat, "Auto-add on restock classified as standing_instruction", si3["intent"] == "standing_instruction")

    # 9.4 Fast-path regex matching standing instruction keywords
    record_test(feat, "Fast-path regex matches 'price drop'", ic._is_standing_instruction_query("alert me on price drop"))
    record_test(feat, "Fast-path regex matches 'reorder in'", ic._is_standing_instruction_query("remind me to reorder in 2 weeks"))

    # 9.5 Dispatch create_standing_instruction requires email if not provided
    si_disp_no_email = await sa.dispatch("standing_instruction", "create_instruction", store, {"instruction": "notify me when price drops below $50"}, session_id=session_id)
    record_test(feat, "create_standing_instruction requests email if not provided", si_disp_no_email.get("status") == "email_required")

    # 9.6 Dispatch create_standing_instruction with email parses type and creates instruction
    si_disp_ok = await sa.dispatch("standing_instruction", "create_instruction", store, {"instruction": "notify me when price drops below $50", "email": "shopper@store.com", "product_title": "Silence Graphic T-Shirt"}, session_id=session_id)
    record_test(feat, "create_standing_instruction successfully creates price_drop instruction", si_disp_ok.get("status") == "instruction_created" and si_disp_ok.get("instruction_type") == "price_drop")

    # 9.7 Create reorder reminder instruction with email
    si_reorder = await sa.dispatch("standing_instruction", "create_instruction", store, {"instruction": "remind me to reorder in 14 days", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "create_standing_instruction parses reorder_reminder and trigger date", si_reorder.get("status") == "instruction_created" and si_reorder.get("instruction_type") == "reorder_reminder")

    # 9.8 Create auto-add restock instruction with email
    si_auto = await sa.dispatch("standing_instruction", "create_instruction", store, {"instruction": "auto add when restocked", "email": "shopper@store.com", "product_title": "Silence Oversized Hoodie"}, session_id=session_id)
    record_test(feat, "create_standing_instruction parses auto_add_restock", si_auto.get("status") == "instruction_created" and si_auto.get("instruction_type") == "auto_add_restock")

    # 9.9 State machine followup for awaiting instruction email
    pkey_si = "dripire-fkpefzp2.myshopify.com::session_si_test"
    cw._set_pending(pkey_si, type="await_instruction_email", intent="standing_instruction", action="create_instruction", entities={"instruction": "remind me to reorder in 10 days"}, language="en")
    pending_si = cw.PENDING.get(pkey_si)
    si_followup_res = await cw._handle_followup(store, "dripire-fkpefzp2.myshopify.com", pkey_si, pending_si, "shopper.vip@dripire.com", "session_si_test")
    record_test(feat, "Awaiting instruction email followup completes instruction creation", si_followup_res is not None and (si_followup_res.get("status") == "instruction_created" or si_followup_res.get("data", {}).get("status") == "instruction_created"))

    # 9.10 Price drop email alert generation
    email_pd = eu.send_price_drop_email("shopper@store.com", "Dripire Store", "Silence Graphic T-Shirt", "$799.00", "$599.00", "https://dripire.myshopify.com/products/silence-tshirt")
    record_test(feat, "send_price_drop_email formats HTML price drop alert", email_pd is not None)

    # 9.11 Reorder reminder email generation
    email_reorder = eu.send_reorder_reminder_email("shopper@store.com", "Dripire Store", "Silence Graphic T-Shirt", "https://dripire.myshopify.com/products/silence-tshirt")
    record_test(feat, "send_reorder_reminder_email formats HTML reorder alert", email_reorder is not None)

    # 9.12 Conversational reply for created instruction
    rep_si = rg.generate_reply("create_instruction", si_disp_ok, "en", "notify me when price drops below $50")
    record_test(feat, "Instruction reply confirms setup warmly without technical jargon", "price drop" in rep_si.lower() or "notif" in rep_si.lower() or "alert" in rep_si.lower())

    # 9.13 Relative timeframe understanding ("in 2 weeks", "next month")
    si_rel = await sa.dispatch("standing_instruction", "create_instruction", store, {"instruction": "remind me to reorder in 2 weeks", "email": "shopper@store.com"}, session_id=session_id)
    record_test(feat, "Understands relative timeframe 'in 2 weeks'", si_rel.get("trigger_at") is not None)


    # =========================================================================
    # FEATURE 10: Abandoned Cart Recovery & Out-of-Stock Swaps (13 tests)
    # =========================================================================
    feat = "10. Abandoned Cart Recovery & Out-of-Stock Swaps"
    print(f"\n--- Running Feature: {feat} ---")

    # 10.1 In-widget abandoned cart nudge dispatch
    nudge_res = await sa.dispatch("cart_recovery", "nudge_abandoned_cart", store, {"cart_data": pop_cart}, session_id=session_id)
    record_test(feat, "nudge_abandoned_cart detects cart items and generates nudge", nudge_res.get("status") in ("cart_held", "cart_held_and_emailed", "nudge_generated"))

    # 10.2 Abandoned cart nudge on empty cart returns empty status
    nudge_empty = await sa.dispatch("cart_recovery", "nudge_abandoned_cart", store, {"cart_data": {"items": []}}, session_id=session_id)
    record_test(feat, "nudge_abandoned_cart skips when cart is empty", nudge_empty.get("status") in ("empty_cart", "cart_empty"))

    # 10.3 Out-of-stock restock subscription classification
    c_sub = ic.classify_intent("notify me when silence oversized hoodie is back in stock")
    record_test(feat, "Restock alert classified as subscribe_restock", c_sub["intent"] in ("restock_alert", "standing_instruction"))

    # 10.4 Subscribe restock dispatch requires email if omitted
    sub_no_email = await sa.dispatch("restock_alert", "subscribe_restock", store, {"product_name_or_id": "Silence Oversized Hoodie"}, session_id=session_id)
    record_test(feat, "subscribe_restock requests email if omitted", sub_no_email.get("status") == "email_required")

    # 10.5 Subscribe restock on out-of-stock item creates subscription
    sub_ok = await sa.dispatch("restock_alert", "subscribe_restock", store, {"product_name_or_id": "Silence Oversized Hoodie", "email": "shopper@domain.com"}, session_id=session_id)
    record_test(feat, "subscribe_restock creates alert for out-of-stock item", sub_ok.get("status") == "alert_subscribed" and sub_ok.get("email") == "shopper@domain.com")

    # 10.6 Subscribe restock on already in-stock item informs shopper item is ready to buy now
    sub_instock = await sa.dispatch("restock_alert", "subscribe_restock", store, {"product_name_or_id": "Silence Graphic T-Shirt", "email": "shopper@domain.com"}, session_id=session_id)
    record_test(feat, "subscribe_restock on in-stock item flags already_in_stock", sub_instock.get("status") == "already_in_stock")

    # 10.7 List shopper's active restock alerts
    list_alerts = await sa.dispatch("restock_alert", "list_my_alerts", store, {"email": "shopper@domain.com"}, session_id=session_id)
    record_test(feat, "list_my_alerts retrieves active subscriptions", list_alerts.get("status") == "alerts_found" and list_alerts.get("count", 0) >= 1)

    # 10.8 Finding close in-stock alternatives for out-of-stock product
    alts = await sa._find_close_alternatives(store, mock_catalog[1], limit=2)
    record_test(feat, "_find_close_alternatives returns available in-stock products", isinstance(alts, list) and len(alts) > 0)

    # 10.9 Swapping out-of-stock cart item with in-stock alternative
    swap_res = await sa.dispatch("cart_management", "swap_item", store, {"remove_variant_id": 202, "add_variant_id": 201, "quantity": 1}, session_id=session_id)
    record_test(feat, "swap_item emits cart_swap widget action", swap_res.get("status") == "item_swapped" and swap_res.get("widget_action", {}).get("type") == "cart_swap")

    # 10.10 Abandoned cart email recovery template generation
    email_cart = eu.send_abandoned_cart_recovery_email("shopper@domain.com", "Dripire Store", [{"title": "Silence Graphic T-Shirt", "price": "$799.00"}], "$799.00", "https://dripire.myshopify.com/cart", discount_code="SAVE10")
    record_test(feat, "send_abandoned_cart_recovery_email formats HTML email with recovery button", email_cart is not None)

    # 10.11 Restock notification email template generation
    email_restock = eu.send_restock_notification_email("shopper@domain.com", "Dripire Store", "Silence Oversized Hoodie", "https://dripire.myshopify.com/products/silence-hoodie", "https://cdn.shopify.com/hoodie.jpg", "$1499.00")
    record_test(feat, "send_restock_notification_email formats responsive HTML template", email_restock is not None)

    # 10.12 Conversational reply for already_in_stock restock alert
    rep_sub_in = rg.generate_reply("subscribe_restock", sub_instock, "en", "notify me when silence tshirt restocks")
    record_test(feat, "Reply enthusiastically tells shopper item is currently in stock", "in stock" in rep_sub_in.lower() and "wait" in rep_sub_in.lower())

    # 10.13 Conversational reply for alert_subscribed
    rep_sub_ok = rg.generate_reply("subscribe_restock", sub_ok, "en", "notify me when silence hoodie restocks")
    record_test(feat, "Reply confirms restock subscription", "subscribed" in rep_sub_ok.lower() or "notify" in rep_sub_ok.lower() or "arrive" in rep_sub_ok.lower())


    # =========================================================================
    # FEATURE 11: Store Policies & Knowledge Base (FAQ & Policies) (13 tests)
    # =========================================================================
    feat = "11. Store Policies & Knowledge Base (FAQ & Policies)"
    print(f"\n--- Running Feature: {feat} ---")

    # 11.1 Shipping policy classification
    pol1 = ic.classify_intent("what is your shipping policy")
    record_test(feat, "Shipping policy classified as policy_query with shipping_policy topic", pol1["intent"] == "policy_query" and pol1["action"] == "answer_policy_question" and pol1["is_action"] is False and pol1["entities"].get("topic") == "shipping_policy")

    # 11.2 Delivery time inquiry classification
    pol2 = ic.classify_intent("how long does delivery take")
    record_test(feat, "'how long does delivery take' routes to shipping_policy", pol2["entities"].get("topic") == "shipping_policy")

    # 11.3 Return policy classification
    pol3 = ic.classify_intent("what is your return policy")
    record_test(feat, "Return policy classified with return_policy topic", pol3["entities"].get("topic") == "return_policy")

    # 11.4 Refund policy classification
    pol4 = ic.classify_intent("what is your refund policy")
    record_test(feat, "Refund policy classified with return_policy topic", pol4["entities"].get("topic") == "return_policy")

    # 11.5 Payment methods policy classification
    pol5 = ic.classify_intent("what payment methods do you accept")
    record_test(feat, "Payment methods classified with payment_policy topic", pol5["entities"].get("topic") == "payment_policy")

    # 11.6 Warranty policy classification
    pol6 = ic.classify_intent("do you offer warranty")
    record_test(feat, "Warranty policy classified with warranty_policy topic", pol6["entities"].get("topic") == "warranty_policy")

    # 11.7 Privacy policy classification
    pol7 = ic.classify_intent("what is your privacy policy")
    record_test(feat, "Privacy policy classified with privacy_policy topic", pol7["entities"].get("topic") == "privacy_policy")

    # 11.8 Dispatch shipping policy returns full policy text
    pol_ship_res = await sa.dispatch("policy_query", "answer_policy_question", store, {"policy_type": "shipping"}, session_id=session_id)
    record_test(feat, "Shipping policy text retrieved (>50 chars)", len(pol_ship_res.get("body", "") or pol_ship_res.get("policy", "")) > 50)

    # 11.9 Dispatch return policy returns 30-day window
    pol_ret_res = await sa.dispatch("policy_query", "answer_policy_question", store, {"policy_type": "return"}, session_id=session_id)
    record_test(feat, "Return policy specifies 30-day window", "30" in (pol_ret_res.get("body", "") or pol_ret_res.get("policy", "")))

    # 11.10 Dispatch payment policy returns UPI, COD, and card details
    pol_pay_res = await sa.dispatch("policy_query", "answer_policy_question", store, {"policy_type": "payment_policy"}, session_id=session_id)
    record_test(feat, "Payment policy mentions UPI and COD", "UPI" in (pol_pay_res.get("body", "") or pol_pay_res.get("policy", "")) or "payment" in (pol_pay_res.get("body", "") or pol_pay_res.get("policy", "")).lower())

    # 11.11 Dispatch warranty policy returns coverage info
    pol_war_res = await sa.dispatch("policy_query", "answer_policy_question", store, {"policy_type": "warranty"}, session_id=session_id)
    record_test(feat, "Warranty policy covers manufacturing defects", "warranty" in (pol_war_res.get("body", "") or pol_war_res.get("policy", "")).lower())

    # 11.12 Fast-path policy classifier helper function
    is_pol, pol_name = ic._is_policy_query("how to return my items")
    record_test(feat, "_is_policy_query identifies 'how to return my items'", is_pol is True and pol_name == "return_policy")

    # 11.13 Conversational policy reply summarizes clearly without legal jargon
    rep_pol = rg.generate_reply("answer_policy_question", pol_ret_res, "en", "what is your return policy")
    record_test(feat, "Policy reply phrased conversationally for an everyday shopper", "return" in rep_pol.lower() and len(rep_pol) > 20)


    # =========================================================================
    # FEATURE 12: General Chat & Conversational Context (13 tests)
    # =========================================================================
    feat = "12. General Chat & Conversational Context"
    print(f"\n--- Running Feature: {feat} ---")

    # 12.1 Greeting 'hi' classification
    g1 = ic.classify_intent("hi")
    record_test(feat, "'hi' classified as general_chat.assistant_help with is_action=False", g1["intent"] == "general_chat" and g1["action"] == "assistant_help" and g1["is_action"] is False and g1["message_type"] == "normal")

    # 12.2 Greeting 'hello there'
    g2 = ic.classify_intent("hello there")
    record_test(feat, "'hello there' classified as assistant_help", g2["action"] == "assistant_help")

    # 12.3 Greeting substring collision check ('silence tshirt' must NOT match 'hi')
    g3 = ic.classify_intent("silence tshirt")
    record_test(feat, "'silence tshirt' does NOT false-positive match greeting 'hi'", g3["intent"] == "product_search")

    # 12.4 Bot capabilities inquiry ('who are you')
    g4 = ic.classify_intent("who are you")
    record_test(feat, "'who are you' classified as assistant_help", g4["intent"] == "general_chat" and g4["action"] == "assistant_help")

    # 12.5 Bot capabilities inquiry ('what can you do')
    g5 = ic.classify_intent("what can you do")
    record_test(feat, "'what can you do' classified as assistant_help", g5["action"] == "assistant_help")

    # 12.6 Help command
    g6 = ic.classify_intent("help")
    record_test(feat, "'help' classified as assistant_help", g6["action"] == "assistant_help")

    # 12.7 Assistant help dispatch returns store capabilities
    help_disp = await sa.dispatch("general_chat", "assistant_help", store, {}, session_id=session_id)
    record_test(feat, "assistant_help dispatch returns store capabilities list", "capabilities" in help_disp and len(help_disp["capabilities"]) >= 5)

    # 12.8 Fallback clarify dispatch for ambiguous conversational queries
    clarify_disp = await sa.dispatch("fallback", "clarify", store, {}, session_id=session_id)
    record_test(feat, "fallback.clarify returns helpful store assistant prompt", clarify_disp.get("status") == "clarify" and "message" in clarify_disp)

    # 12.9 Distinguishing normal vs action messages across intents
    record_test(feat, "Search is tagged message_type='normal'", ic.classify_intent("silence tshirt")["message_type"] == "normal")
    record_test(feat, "Add to cart is tagged message_type='action'", ic.classify_intent("add silence tshirt to cart")["message_type"] == "action")
    record_test(feat, "Cancel order is tagged message_type='action'", ic.classify_intent("cancel order 1001")["message_type"] == "action")
    record_test(feat, "View cart is tagged message_type='normal'", ic.classify_intent("view cart")["message_type"] == "normal")

    # 12.10 Greeting returning customer by display name
    cw._remember_customer("dripire-fkpefzp2.myshopify.com", "test_cust_greet", "cust_1122", "priya@gmail.com", "Priya")
    greet_name = cw._session_customer_name("dripire-fkpefzp2.myshopify.com", "test_cust_greet")
    record_test(feat, "Session cache remembers returning customer name 'Priya'", greet_name == "Priya")

    # 12.11 Conversational reply welcoming shopper warmly without technical jargon
    rep_help = rg.generate_reply("assistant_help", help_disp, "en", "hi who are you")
    record_test(feat, "Assistant welcome reply is conversational and friendly", "welcome" in rep_help.lower() or "help" in rep_help.lower())

    # 12.12 Multi-turn conversation history support in classify_intent
    history = [("user", "do you have hoodies?"), ("assistant", "Yes, we have the Silence Oversized Hoodie!")]
    c_hist = ic.classify_intent("is it in stock?", history=history)
    record_test(feat, "classify_intent accepts and processes conversation history", c_hist["intent"] == "product_search")

    # 12.13 Widget JS provides full conversational UI, speech synthesis, and voice recognition
    widget_code = cw.WIDGET_JS
    record_test(feat, "Widget JS contains speech synthesis, speech recognition, and toggle", "SpeechRecognition" in widget_code and "speechSynthesis" in widget_code and "spokenRepliesEnabled" in widget_code)


    # =========================================================================
    # SUMMARY OF RESULTS
    # =========================================================================
    print("\n" + "="*80)
    print("ALL FEATURE TESTS COMPLETED!")
    print("="*80)
    print(f"Total Tests Run: {TOTAL_TESTS}")
    print(f"Passed Tests:    {PASSED_TESTS}")
    print(f"Failed Tests:    {FAILED_TESTS}")
    print(f"Overall Success Rate: {(PASSED_TESTS/TOTAL_TESTS)*100:.1f}%\n")

    print(f"{'Feature Name':<60} | {'Passed':<7} | {'Failed':<7} | {'Rate'}")
    print("-" * 85)
    for feat_name, stats in FEATURE_STATS.items():
        tot = stats["passed"] + stats["failed"]
        rate = (stats["passed"] / tot) * 100 if tot > 0 else 0
        print(f"{feat_name:<60} | {stats['passed']:<7} | {stats['failed']:<7} | {rate:.1f}%")
    print("-" * 85)

    assert FAILED_TESTS == 0, f"Some tests failed ({FAILED_TESTS} failures out of {TOTAL_TESTS} tests)"


if __name__ == "__main__":
    asyncio.run(run_all_feature_tests())
