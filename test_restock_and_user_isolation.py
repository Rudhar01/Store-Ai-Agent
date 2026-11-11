"""
test_restock_and_user_isolation.py
-----------------------------------
Verifies:
1. Restock Alert:
   - Product verification in store catalog
   - In-stock detection: informs user directly on chat and returns product card
   - Out-of-stock detection: prompts for email or subscribes shopper to email alert
   - Product not found handling: informs user product does not exist in store
2. User Confidentiality, Device Binding & Time Limit:
   - Switching user accounts does not leak chats across users
   - Anonymous guests cannot access authenticated customers' chats
   - Sessions respect time limits (pruned after TTL)
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import chatbot_widget
import shopify_actions


async def test_restock_alerts():
    print("--- TEST 1: Restock Alert - In-Stock Product ---")
    mock_products = [
        {
            "id": 1001,
            "title": "Classic Leather Oxford Shoes",
            "handle": "classic-leather-oxford-shoes",
            "image": {"src": "https://cdn.example.com/shoes.jpg"},
            "variants": [
                {
                    "id": 2001,
                    "title": "Size 10",
                    "price": "120.00",
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "inventory_quantity": 15,  # IN STOCK
                    "option1": "10",
                }
            ],
        },
        {
            "id": 1002,
            "title": "Pro Carbon Snowboard",
            "handle": "pro-carbon-snowboard",
            "image": {"src": "https://cdn.example.com/snowboard.jpg"},
            "variants": [
                {
                    "id": 2002,
                    "title": "158cm",
                    "price": "450.00",
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "inventory_quantity": 0,  # OUT OF STOCK
                    "option1": "158cm",
                }
            ],
        },
    ]

    async def mock_get(store, path, params=None):
        if path == "products.json":
            return SimpleNamespace(status_code=200, json=lambda: {"products": mock_products})
        return SimpleNamespace(status_code=404, json=lambda: {})

    shopify_actions._get = mock_get

    store = SimpleNamespace(id="store_1", shop_domain="test-store.myshopify.com")

    # 1. Ask for restock on IN-STOCK item
    res_in_stock = await shopify_actions.subscribe_restock(
        store,
        {"product_name_or_id": "Classic Leather Oxford Shoes", "size": "10"},
        session_id="sess_1",
    )
    assert res_in_stock["status"] == "already_in_stock", f"Expected already_in_stock, got {res_in_stock}"
    assert "in stock" in res_in_stock["message"].lower()
    assert len(res_in_stock.get("results", [])) == 1
    assert res_in_stock["results"][0]["in_stock"] is True
    print(f"PASS: In-stock product reported immediately in chat with product card: {res_in_stock['message']}")

    # 2. Ask for restock on OUT-OF-STOCK item without email
    res_oos_no_email = await shopify_actions.subscribe_restock(
        store,
        {"product_name_or_id": "Pro Carbon Snowboard"},
        session_id="sess_1",
    )
    assert res_oos_no_email["status"] == "email_required", f"Expected email_required, got {res_oos_no_email}"
    assert "out of stock" in res_oos_no_email["message"].lower()
    assert "email" in res_oos_no_email["message"].lower()
    print(f"PASS: Out-of-stock product prompts for email: {res_oos_no_email['message']}")

    # 3. Ask for restock on OUT-OF-STOCK item WITH email
    res_oos_with_email = await shopify_actions.subscribe_restock(
        store,
        {"product_name_or_id": "Pro Carbon Snowboard", "email": "customer@example.com"},
        session_id="sess_1",
    )
    assert res_oos_with_email["status"] == "alert_subscribed", f"Expected alert_subscribed, got {res_oos_with_email}"
    assert res_oos_with_email["email"] == "customer@example.com"
    assert "Pro Carbon Snowboard" in res_oos_with_email["product_title"]
    print(f"PASS: Out-of-stock product subscribed to email alert: {res_oos_with_email['message']}")

    # 4. Ask for restock on non-existent product
    res_not_found = await shopify_actions.subscribe_restock(
        store,
        {"product_name_or_id": "Super Galactic Spaceship 9000"},
        session_id="sess_1",
    )
    assert res_not_found["status"] == "not_found", f"Expected not_found, got {res_not_found}"
    assert "couldn't find" in res_not_found["message"].lower() or "not find" in res_not_found["message"].lower()
    print(f"PASS: Non-existent product handled accurately: {res_not_found['message']}")


async def test_user_session_confidentiality():
    print("\n--- TEST 2: User Confidentiality & Account Switch Isolation ---")
    shop = "test-store.myshopify.com"

    # Reset in-memory session caches for clean test
    chatbot_widget.HISTORY.clear()
    chatbot_widget._CUSTOMER_SESSIONS.clear()
    chatbot_widget._CUSTOMER_EMAILS.clear()

    # User A chats on device 1
    session_user_a = "sess_cust_userA_dev1_1001"
    chatbot_widget._remember_turn(
        chatbot_widget._pkey(shop, session_user_a),
        "What is the status of my confidential order #9999?",
        "Your secret order #9999 is shipped to User A's private address.",
        customer_id="cust_A",
        visitor_id="dev1",
    )

    # User B logs in on same device (different session id)
    session_user_b = "sess_cust_userB_dev1_1002"
    chatbot_widget._remember_turn(
        chatbot_widget._pkey(shop, session_user_b),
        "Show my sneakers order #8888",
        "Your order #8888 is delivered.",
        customer_id="cust_B",
        visitor_id="dev1",
    )

    # 1. User B queries chat history: MUST NOT SEE User A's chat!
    hist_b = await chatbot_widget.chat_history(
        shop=shop,
        session_id=session_user_b,
        customer_id="cust_B",
        visitor_id="dev1",
    )
    assert hist_b["status"] == "success"
    chats_b = hist_b.get("chats", [])
    user_a_messages = [c for c in chats_b if "#9999" in c.get("user_message", "") or "User A" in c.get("agent_reply", "")]
    assert len(user_a_messages) == 0, f"LEAK DETECTED! User B saw User A's private messages: {user_a_messages}"
    print("PASS: User B cannot see User A's chat history on the same device.")

    # 2. Anonymous Guest on same device: MUST NOT SEE Customer A or B's chats!
    session_guest = "sess_guest_dev1_1003"
    chatbot_widget._remember_turn(
        chatbot_widget._pkey(shop, session_guest),
        "Hello, what products do you sell?",
        "We sell premium apparel and equipment!",
        customer_id=None,
        visitor_id="dev1",
    )
    hist_guest = await chatbot_widget.chat_history(
        shop=shop,
        session_id=session_guest,
        customer_id="",
        visitor_id="dev1",
    )
    chats_guest = hist_guest.get("chats", [])
    leaked_auth_chats = [c for c in chats_guest if "#9999" in c.get("user_message", "") or "#8888" in c.get("user_message", "")]
    assert len(leaked_auth_chats) == 0, f"LEAK DETECTED! Guest saw authenticated customer orders: {leaked_auth_chats}"
    assert any("what products do you sell" in c.get("user_message", "") for c in chats_guest)
    print("PASS: Anonymous guest only sees their own guest chat, zero customer leakage.")

    # 3. Account switch detection in chat endpoint
    # If a request comes with session_user_a, but customer_id is cust_B (account switched on same tab):
    chatbot_widget._remember_customer(shop, "sess_reused", "cust_A", "a@example.com")
    req = chatbot_widget.ChatRequest(
        shop=shop,
        session_id="sess_reused",
        customer_id="cust_B",
        customer_email="b@example.com",
        message="Hello",
        visitor_id="dev1",
    )
    # Mock store and customization
    chatbot_widget._get_store = lambda s: SimpleNamespace(id="store_1", shop_domain=shop, access_token="token")
    chatbot_widget.repo.ensure_customization = lambda s: {"status": "active"}

    await chatbot_widget._chat_impl(req)
    # The session customer should now be cust_B, and cust_A cleared
    assert chatbot_widget._session_customer(shop, "sess_reused") == "cust_B"
    assert chatbot_widget._session_customer_email(shop, "sess_reused") == "b@example.com"
    print("PASS: Session correctly switches ownership when user account changes.")

    # 4. Time limit TTL check
    old_key = chatbot_widget._pkey(shop, "sess_old_expired")
    chatbot_widget.HISTORY[old_key] = {
        "ts": time.time() - (chatbot_widget.HISTORY_TTL_SECONDS + 500),  # expired
        "turns": [("user", "Old secret question"), ("assistant", "Old secret answer")],
        "customer_id": "cust_B",
        "visitor_id": "dev1",
    }
    hist_expired = await chatbot_widget.chat_history(
        shop=shop,
        session_id="sess_old_expired",
        customer_id="cust_B",
        visitor_id="dev1",
    )
    assert len(hist_expired.get("chats", [])) == 0, "Expired turns should be pruned by TTL"
    print("PASS: Chat history respects time limit (TTL) and prunes expired sessions.")


async def main():
    await test_restock_alerts()
    await test_user_session_confidentiality()
    print("\n" + "=" * 55)
    print("  ALL RESTOCK & USER CONFIDENTIALITY TESTS PASSED!  ")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
