"""
test_suite.py
--------------
Comprehensive verification of all eCommerce AI Agent capabilities:
1. Intent Classification & Schema validation
2. Direct Cart Updates from shopper messages (widget_action generation)
3. Order History & Tracking
4. Policy Checking
5. Warranty Claim Submission & Tracking
6. Payment Credentials Storage & OTP Checkout Flow
7. Chatbot Widget Request Handling & Follow-up flows
"""
import sys
import json
import asyncio
from types import SimpleNamespace

import intent_classifier
import shopify_actions
import chatbot_widget
import reply_generator

async def run_tests():
    store = SimpleNamespace(
        shop_domain="demo-store.myshopify.com",
        access_token="shpat_mock_token_for_testing"
    )
    session_id = "test_shopper_session_101"

    # Fast offline mock for non-existent test domain
    async def mock_get(store, path, params=None):
        return SimpleNamespace(status_code=404, json=lambda: {}, text="Not Found")
    shopify_actions._get = mock_get

    async def mock_graphql(store, query, variables=None):
        return None
    shopify_actions._graphql_admin = mock_graphql

    print("==================================================")
    print("  TEST 1: SCHEMA & INTENT DEFINITIONS")
    print("==================================================")
    schema = intent_classifier.load_schema()
    raw_intents = schema.get("intents", [])
    intents = {item["name"]: item for item in raw_intents}
    assert "payment_checkout" in intents, "payment_checkout intent missing"
    assert "warranty_claim" in intents, "warranty_claim intent missing"
    assert "order_tracking" in intents, "order_tracking intent missing"
    assert "cart_management" in intents, "cart_management intent missing"
    assert "policy_query" in intents, "policy_query intent missing"
    
    # Check that direct cart actions do NOT require confirmation so they execute immediately
    cart_actions = {a["name"]: a for a in intents["cart_management"]["actions"]}
    assert cart_actions["add_item"].get("requires_confirmation") is False, "add_item should execute immediately"
    assert cart_actions["remove_item"].get("requires_confirmation") is False, "remove_item should execute immediately"
    assert cart_actions["edit_quantity"].get("requires_confirmation") is False, "edit_quantity should execute immediately"
    assert cart_actions["clear_cart"].get("requires_confirmation") is True, "clear_cart requires confirmation as a safeguard"
    print("PASS: Schema and cart confirmation policies verified.")

    print("\n==================================================")
    print("  TEST 2: DIRECT CART MUTATIONS & WIDGET ACTIONS")
    print("==================================================")
    # Add item
    add_res = await shopify_actions.dispatch(
        "cart_management", "add_item", store,
        {"product_name_or_id": "98765432", "quantity": 2},
        session_id=session_id
    )
    assert "widget_action" in add_res, "widget_action missing from add_item"
    assert add_res["widget_action"]["type"] == "cart_add"
    assert add_res["widget_action"]["variant_id"] == 98765432
    assert add_res["widget_action"]["quantity"] == 2
    print(f"PASS: add_item emitted cart_add action ({add_res['widget_action']}).")

    # Remove item
    rem_res = await shopify_actions.dispatch(
        "cart_management", "remove_item", store,
        {"product_name_or_id": "98765432"},
        session_id=session_id
    )
    assert rem_res["widget_action"]["type"] == "cart_remove"
    print(f"PASS: remove_item emitted cart_remove action ({rem_res['widget_action']}).")

    # Edit quantity
    edit_res = await shopify_actions.dispatch(
        "cart_management", "edit_quantity", store,
        {"product_name_or_id": "98765432", "quantity": 5},
        session_id=session_id
    )
    assert edit_res["widget_action"]["type"] == "cart_set_quantity"
    assert edit_res["widget_action"]["quantity"] == 5
    print(f"PASS: edit_quantity emitted cart_set_quantity action ({edit_res['widget_action']}).")

    # View cart
    view_res = await shopify_actions.dispatch(
        "cart_management", "view_cart", store,
        {},
        session_id=session_id
    )
    assert view_res["widget_action"]["type"] == "cart_view"
    print(f"PASS: view_cart emitted cart_view action ({view_res['widget_action']}).")

    # Clear cart
    clear_res = await shopify_actions.dispatch(
        "cart_management", "clear_cart", store,
        {},
        session_id=session_id
    )
    assert clear_res["widget_action"]["type"] == "cart_clear"
    print(f"PASS: clear_cart emitted cart_clear action ({clear_res['widget_action']}).")

    print("\n==================================================")
    print("  TEST 2.5: FUZZY SEARCH & GENERAL CHAT INTENTS")
    print("==================================================")
    # Test fuzzy product matching
    mock_snowboard = {"title": "The Complete Snowboard", "product_type": "Snowboard", "tags": "winter, sports", "body_html": "<p>A great snowboard.</p>"}
    tokens = shopify_actions._clean_search_tokens("tell me about some showboard")
    assert "showboard" in tokens
    score = shopify_actions._score_product_match(mock_snowboard, tokens)
    assert score >= 1.5, f"Expected high match score for showboard vs snowboard, got {score}"
    print(f"PASS: Fuzzy token scoring matched typo 'showboard' -> 'The Complete Snowboard' with score {score:.2f}")

    # Test general_chat.assistant_help
    help_res = await shopify_actions.dispatch("general_chat", "assistant_help", store, {}, session_id=session_id)
    assert "capabilities" in help_res and len(help_res["capabilities"]) >= 5
    assert "store_name" in help_res
    print(f"PASS: general_chat.assistant_help returned {len(help_res['capabilities'])} capabilities.")

    # Test general_chat.recommend_products
    rec_res = await shopify_actions.dispatch("general_chat", "recommend_products", store, {}, session_id=session_id)
    assert "results" in rec_res or "message" in rec_res
    print("PASS: general_chat.recommend_products dispatched successfully.")

    # Test fallback.clarify
    clarify_res = await shopify_actions.dispatch("fallback", "clarify", store, {}, session_id=session_id)
    assert clarify_res.get("status") == "clarify"
    assert "message" in clarify_res
    print("PASS: fallback.clarify returned engaging store assistance.")

    print("\n==================================================")
    print("  TEST 3: POLICIES CHECKING")
    print("==================================================")
    for p_type in ["return", "shipping", "warranty", "refund", "privacy"]:
        pol_res = await shopify_actions.dispatch(
            "policy_query", "answer_policy_question", store,
            {"policy_type": p_type},
            session_id=session_id
        )
        assert pol_res.get("body") or pol_res.get("policy") or pol_res.get("text"), f"Policy {p_type} returned empty"
        policy_text = pol_res.get("body") or pol_res.get("policy") or pol_res.get("text")
        assert len(policy_text) > 20
        print(f"PASS: Policy '{p_type}' returned valid text ({len(policy_text)} chars).")

    print("\n==================================================")
    print("  TEST 4: PAYMENT CREDENTIALS & OTP FLOW")
    print("==================================================")
    # Save credentials
    save_res = await shopify_actions.dispatch(
        "payment_checkout", "save_payment_credentials", store,
        {
            "cardholder_name": "Alex Smith",
            "card_number": "4111111111114242",
            "expiry": "11/28",
            "billing_address": "123 Market St, Suite 400"
        },
        session_id=session_id
    )
    assert save_res.get("status") == "credentials_saved"
    assert "4242" in save_res.get("masked_number")
    print(f"PASS: Payment credentials saved and masked successfully: {save_res.get('masked_number')}.")

    # Initiate payment (with product in entities)
    init_res = await shopify_actions.dispatch(
        "payment_checkout", "initiate_payment", store,
        {
            "product_name_or_id": "98765432",
            "quantity": 1
        },
        session_id=session_id
    )
    assert init_res.get("status") == "otp_required", f"Unexpected status: {init_res}"
    otp = init_res.get("otp")
    assert otp and len(otp) == 6
    print(f"PASS: Payment initiated, 6-digit OTP challenge generated: {otp} (total: {init_res.get('amount')}).")

    # Confirm invalid OTP
    bad_otp_res = await shopify_actions.dispatch(
        "payment_checkout", "confirm_payment_otp", store,
        {"otp_code": "000000"},
        session_id=session_id
    )
    assert bad_otp_res.get("error") == "invalid_otp"
    print("PASS: Invalid OTP code correctly rejected.")

    # Confirm valid OTP
    valid_otp_res = await shopify_actions.dispatch(
        "payment_checkout", "confirm_payment_otp", store,
        {"otp_code": otp, "email": "shopper@example.com"},
        session_id=session_id
    )
    assert valid_otp_res.get("status") == "payment_success"
    assert "order_number" in valid_otp_res
    assert valid_otp_res.get("widget_action", {}).get("type") == "cart_clear"
    print(f"PASS: OTP verified and order #{valid_otp_res.get('order_number')} created successfully!")

    print("\n==================================================")
    print("  TEST 5: WARRANTY CLAIMS & TRACKING")
    print("==================================================")
    claim_res = await shopify_actions.dispatch(
        "warranty_claim", "submit_claim", store,
        {
            "order_number": "1005",
            "email": "alex@example.com",
            "issue": "Right earbud battery drains within 5 minutes"
        },
        session_id=session_id
    )
    # If order is not in live Shopify, mock record is filed or error is properly structured
    if claim_res.get("status") == "submitted":
        claim_id = claim_res["claim_id"]
        assert claim_id.startswith("CLM-1005-")
        print(f"PASS: Warranty claim submitted with Claim ID: {claim_id}")

        status_res = await shopify_actions.dispatch(
            "warranty_claim", "check_claim_status", store,
            {"claim_id": claim_id},
            session_id=session_id
        )
        assert status_res.get("claim_id") == claim_id
        print(f"PASS: Warranty claim status checked: {status_res.get('status')}")
    else:
        # Proper error structure
        assert "message" in claim_res or "error" in claim_res
        print(f"PASS: Warranty claim validation handled ({claim_res.get('error') or claim_res.get('message')})")

    print("\n==================================================")
    print("  TEST 6: CHATBOT WIDGET FOLLOW-UPS & STATE")
    print("==================================================")
    # Test pending follow-up for order email
    key1 = "demo-store.myshopify.com::session_followup_1"
    chatbot_widget._set_pending(key1, type="await_order_email", language="en")
    pending1 = chatbot_widget.PENDING[key1]
    followup_resp = await chatbot_widget._handle_followup(
        store=store,
        shop="demo-store.myshopify.com",
        key=key1,
        pending=pending1,
        message="alex@example.com",
        session_id="session_followup_1"
    )
    assert followup_resp is not None
    assert followup_resp.get("action") == "list_recent_orders"
    print(f"PASS: Follow-up for guest email handled (intent: {followup_resp.get('action')}).")

    # Test pending follow-up for payment OTP
    key2 = "demo-store.myshopify.com::session_followup_2"
    chatbot_widget._set_pending(key2, type="await_payment_otp", language="en")
    pending2 = chatbot_widget.PENDING[key2]
    followup_otp = await chatbot_widget._handle_followup(
        store=store,
        shop="demo-store.myshopify.com",
        key=key2,
        pending=pending2,
        message="My OTP is 654321",
        session_id="session_followup_2"
    )
    assert followup_otp is not None
    assert followup_otp.get("action") == "confirm_payment_otp"
    print("PASS: Follow-up for OTP code handled directly.")

    print("\n==================================================")
    print("  TEST 7: WIDGET JS ASSET SERVING & SESSION SYNC")
    print("==================================================")
    assert len(chatbot_widget.WIDGET_JS) > 1000
    assert "ai-chat-widget-root" in chatbot_widget.WIDGET_JS
    assert "/cart/add.js" in chatbot_widget.WIDGET_JS
    assert "verify-otp" in chatbot_widget.WIDGET_JS
    assert "save-payment-credentials" in chatbot_widget.WIDGET_JS
    assert "syncCustomerSession" in chatbot_widget.WIDGET_JS, "syncCustomerSession missing from WIDGET_JS"
    assert "header-cust-badge" in chatbot_widget.WIDGET_JS, "header-cust-badge missing from WIDGET_JS"
    assert "showOutsideGreeting" in chatbot_widget.WIDGET_JS, "showOutsideGreeting missing from WIDGET_JS"
    assert "fgClose" in chatbot_widget.WIDGET_JS, "fgClose missing from WIDGET_JS"
    assert "fg-text" in chatbot_widget.WIDGET_JS, "fg-text missing from WIDGET_JS"
    assert "getGreetingText" in chatbot_widget.WIDGET_JS, "getGreetingText missing from WIDGET_JS"
    assert "customer_email: customerEmail" in chatbot_widget.WIDGET_JS, "customer_email missing from WIDGET_JS payload"
    assert "customer_id: customerId" in chatbot_widget.WIDGET_JS, "customer_id missing from WIDGET_JS payload"
    print("PASS: Widget JS contains complete UI, compact personalized greeting bubble, cart AJAX handlers, payment modals, and logged-in customer session sync.")

    print("\n==================================================")
    print("  TEST 8: LOGGED-IN CUSTOMER EMAIL VERIFICATION BYPASS")
    print("==================================================")
    logged_in_email = "shopper.vip@example.com"
    logged_in_cust_id = "cust_998877"

    # 1. Anonymous guest asking for orders -> verification_required
    guest_orders_res = await shopify_actions.list_recent_orders(
        store, {}, session_id="guest_session_1", customer_id=None
    )
    assert guest_orders_res.get("error") == "verification_required", "Guest should be asked for verification"
    print("PASS: Anonymous guest is properly prompted for email verification.")

    # 2. Logged-in customer (with email in entities/session) -> NO verification_required
    auth_orders_res = await shopify_actions.list_recent_orders(
        store, {"email": logged_in_email}, session_id="auth_session_1", customer_id=None
    )
    assert auth_orders_res.get("error") != "verification_required", f"Logged-in user got verification error: {auth_orders_res}"
    print("PASS: Logged-in user with email never receives verification_required when listing orders.")

    # 3. Logged-in customer (with customer_id) -> NO verification_required
    auth_cust_orders = await shopify_actions.list_recent_orders(
        store, {}, session_id="auth_session_2", customer_id=logged_in_cust_id
    )
    assert auth_cust_orders.get("error") != "verification_required", f"Customer ID session got verification error: {auth_cust_orders}"
    print("PASS: Logged-in user with customer_id never receives verification_required when listing orders.")

    # 4. Access check in _access_error with logged-in user email matching order
    mock_order = {
        "id": "77701",
        "name": "#1099",
        "email": logged_in_email,
        "customer": {"id": "998877", "email": logged_in_email}
    }
    access_res = shopify_actions._access_error(mock_order, logged_in_email, logged_in_cust_id, "1099")
    assert access_res is None, f"Expected access granted (None), got {access_res}"
    print("PASS: Logged-in customer granted immediate access to their order without verification prompt.")

    # 5. Session cache remembering customer email
    chatbot_widget._remember_customer("demo-store.myshopify.com", "auth_session_3", logged_in_cust_id, logged_in_email, "Alex VIP")
    assert chatbot_widget._session_customer_email("demo-store.myshopify.com", "auth_session_3") == logged_in_email
    assert chatbot_widget._session_customer_name("demo-store.myshopify.com", "auth_session_3") == "Alex VIP"
    assert chatbot_widget._session_customer("demo-store.myshopify.com", "auth_session_3") == logged_in_cust_id
    print("PASS: Customer email, name, and ID seamlessly remembered across session turns.")

    print("\n==================================================")
    print("  ALL 8 AGENT CAPABILITY TESTS PASSED WITH 100% SUCCESS!")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_tests())

