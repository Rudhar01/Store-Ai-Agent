"""
test_new_features.py
--------------------
Verifies all 5 requirements:
0. Sender email support@dripire.com & database-retrieved Reply-To store owner email.
1. Standing instructions and scheduled tasks (price drop, reorder reminder, auto-add restock).
2. Abandoned cart recovery (in-widget nudge & email recovery).
3. Out-of-stock close alternatives, swap offer, and restock alert combo.
4. Order edits & cancellation (pre-fulfillment only, verification check, Auto/Ask/Off settings).
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import email_utils
import repository_appwrite as repo
import shopify_actions
import chatbot_widget

async def test_email_sender_and_reply_to():
    print("\n--- TEST 0: Sender support@dripire.com & Reply-To Store Owner ---")
    
    # Verify default sender is support@dripire.com
    _, _, _, _, from_addr = email_utils._get_smtp_config()
    assert from_addr == "support@dripire.com", f"Expected support@dripire.com, got {from_addr}"
    print(f"PASS: Default sender is {from_addr}")

    # Mock store owner email in repo
    test_shop = "dripire-test.myshopify.com"
    repo.ensure_store_info(test_shop, business_name="Dripire Apparel", support_email="owner@dripire-apparel.com")
    owner_email = repo.get_store_owner_email(test_shop)
    assert owner_email == "owner@dripire-apparel.com", f"Expected owner@dripire-apparel.com, got {owner_email}"
    print(f"PASS: Retrieved store owner email: {owner_email}")

    # Test send_html_email includes From: support@dripire.com and Reply-To: owner_email
    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "support@dripire.com", "SMTP_PASSWORD": "secretpassword"}):
            sent = email_utils.send_restock_notification_email(
                to_email="shopper@example.com",
                store_name="Dripire Apparel",
                product_title="Oversized Streetwear Hoodie",
                price="₹1,499",
                reply_to=owner_email,
            )
            assert sent is True, "Failed to send mock restock notification"
            # Verify the call to sendmail
            mock_server.sendmail.assert_called_once()
            args, _ = mock_server.sendmail.call_args
            from_arg, to_arg, msg_arg = args
            assert from_arg == "support@dripire.com"
            assert "Reply-To: owner@dripire-apparel.com" in msg_arg
            assert "support@dripire.com" in msg_arg
            print("PASS: Restock notification sent from support@dripire.com with Reply-To: owner@dripire-apparel.com")

async def test_standing_instructions():
    print("\n--- TEST 1: Standing Instructions & Scheduled Tasks ---")
    store = SimpleNamespace(shop_domain="dripire-test.myshopify.com", access_token="shpat_123", id="store_1")

    # Mock catalog
    mock_products = {
        "products": [
            {
                "id": "prod_1",
                "title": "Classic Leather Oxford Shoes",
                "handle": "classic-leather-oxford-shoes",
                "product_type": "Shoes",
                "variants": [{"id": "var_1", "price": "1499.00", "inventory_quantity": 5}],
            }
        ]
    }

    with patch.object(shopify_actions, "_get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_products
        mock_get.return_value = mock_resp

        # Example 1: Tell me when this drops below ₹999
        res1 = await shopify_actions.create_standing_instruction(
            store,
            {"instruction": "Tell me when Classic Leather Oxford Shoes drops below ₹999", "email": "shopper@example.com"},
            session_id="sess_1",
        )
        assert res1["status"] == "instruction_created"
        assert res1["instruction_type"] == "price_drop"
        assert "999" in res1["message"]
        print("PASS: Price drop instruction created:", res1["message"])

        # Example 2: Remind me to reorder in 30 days
        res2 = await shopify_actions.create_standing_instruction(
            store,
            {"instruction": "Remind me to reorder Classic Leather Oxford Shoes in 30 days", "email": "shopper@example.com"},
            session_id="sess_1",
        )
        assert res2["status"] == "instruction_created"
        assert res2["instruction_type"] == "reorder_reminder"
        assert "30 days" in res2["message"]
        print("PASS: Reorder reminder instruction created:", res2["message"])

        # Example 3: Add this to my cart when it's back in stock
        res3 = await shopify_actions.create_standing_instruction(
            store,
            {"instruction": "Add this to my cart when it's back in stock", "product_title": "Classic Leather Oxford Shoes", "email": "shopper@example.com"},
            session_id="sess_1",
        )
        assert res3["status"] == "instruction_created"
        assert res3["instruction_type"] == "auto_add_restock"
        print("PASS: Auto-add restock instruction created:", res3["message"])

async def test_abandoned_cart_recovery():
    print("\n--- TEST 2: Abandoned Cart Recovery (In-widget & Email) ---")
    store = SimpleNamespace(shop_domain="dripire-test.myshopify.com", access_token="shpat_123", id="store_1")

    # In-widget nudge response
    cart_data = {
        "items": [{"title": "Oversized Denim Jacket", "quantity": 1, "price": "₹2,499"}],
        "total": "2499.00",
    }
    res = await shopify_actions.nudge_abandoned_cart(store, {"cart_data": cart_data}, session_id="sess_1")
    assert res["status"] == "cart_held"
    assert "Oversized Denim Jacket" in res["message"]
    assert "https://dripire-test.myshopify.com/checkout" in res["checkout_url"]
    print("PASS: In-widget abandoned cart nudge held successfully:", res["message"])

    # Email recovery formatting
    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "support@dripire.com", "SMTP_PASSWORD": "secretpassword"}):
            sent = email_utils.send_abandoned_cart_email(
                to_email="customer@example.com",
                store_name="Dripire Apparel",
                cart_items=cart_data["items"],
                checkout_url="https://dripire-test.myshopify.com/checkout",
                total_price="₹2,499",
                reply_to="owner@dripire-apparel.com",
            )
            assert sent is True
            args, _ = mock_server.sendmail.call_args
            _, _, msg_arg = args
            assert "Reply-To: owner@dripire-apparel.com" in msg_arg
            import email
            msg_obj = email.message_from_string(msg_arg)
            payload = "".join(p.get_payload(decode=True).decode("utf-8", errors="ignore") for p in msg_obj.walk() if p.get_content_type() in ("text/plain", "text/html"))
            assert "Oversized Denim Jacket" in payload
            print("PASS: Abandoned cart email formatted and sent with store owner Reply-To.")

async def test_close_alternatives_on_unavailable():
    print("\n--- TEST 3: Out-of-Stock Close Alternatives & Restock Alert ---")
    store = SimpleNamespace(shop_domain="dripire-test.myshopify.com", access_token="shpat_123", id="store_1")

    mock_products = {
        "products": [
            {
                "id": "prod_out",
                "title": "Vintage Graphic Tee",
                "handle": "vintage-graphic-tee",
                "product_type": "T-Shirts",
                "vendor": "Dripire",
                "tags": "cotton, streetwear, summer",
                "variants": [{"id": "var_out", "price": "799.00", "inventory_quantity": 0, "inventory_management": "shopify"}],
            },
            {
                "id": "prod_alt_1",
                "title": "Retro Street Graphic T-Shirt",
                "handle": "retro-street-graphic-t-shirt",
                "product_type": "T-Shirts",
                "vendor": "Dripire",
                "tags": "cotton, streetwear, graphic",
                "variants": [{"id": "var_alt_1", "price": "849.00", "inventory_quantity": 8, "inventory_management": "shopify"}],
            },
            {
                "id": "prod_alt_2",
                "title": "Minimalist Oversized Tee",
                "handle": "minimalist-oversized-tee",
                "product_type": "T-Shirts",
                "vendor": "Dripire",
                "tags": "cotton, summer",
                "variants": [{"id": "var_alt_2", "price": "799.00", "inventory_quantity": 12, "inventory_management": "shopify"}],
            },
        ]
    }

    with patch.object(shopify_actions, "_get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_products
        mock_get.return_value = mock_resp

        # Subscribe restock for out of stock item with email
        res = await shopify_actions.subscribe_restock(
            store,
            {"product_name_or_id": "Vintage Graphic Tee", "email": "shopper@example.com"},
            session_id="sess_1",
        )
        assert res["status"] == "alert_subscribed"
        assert len(res.get("alternatives", [])) >= 2
        assert "Retro Street Graphic T-Shirt" in [a["name"] for a in res["alternatives"]]
        assert "close in-stock alternatives" in res["message"]
        print("PASS: Proposed in-stock alternatives while registering restock alert:", res["message"])

        # Test cart swap action
        swap_res = await shopify_actions.swap_cart_item(
            store,
            {"variant_id": "var_alt_1", "remove_variant_id": "var_out"},
            session_id="sess_1",
        )
        assert swap_res["status"] == "swapped"
        assert swap_res["widget_action"]["type"] == "swap_item"
        assert swap_res["widget_action"]["add_variant_id"] == "var_alt_1"
        print("PASS: Cart item swap executed successfully:", swap_res["message"])

async def test_order_cancellation_and_edits():
    print("\n--- TEST 4: Order Cancellation & Pre-Fulfillment Edits ---")
    store = SimpleNamespace(shop_domain="dripire-test.myshopify.com", access_token="shpat_123", id="store_1")

    # Order 1: Already fulfilled (should be rejected)
    fulfilled_order = {
        "id": 1001,
        "name": "#1001",
        "order_number": 1001,
        "fulfillment_status": "fulfilled",
        "customer": {"id": 12345, "email": "shopper@example.com"},
    }

    # Order 2: Unfulfilled (eligible for cancellation)
    unfulfilled_order = {
        "id": 1002,
        "name": "#1002",
        "order_number": 1002,
        "fulfillment_status": "unfulfilled",
        "customer": {"id": 12345, "email": "shopper@example.com"},
    }

    with patch.object(shopify_actions, "_get_order_by_number") as mock_get_order:
        # Case A: Fulfilled order rejection
        mock_get_order.return_value = fulfilled_order
        res1 = await shopify_actions.cancel_order(
            store,
            {"order_number": "1001"},
            session_id="sess_1",
            customer_id="12345",
        )
        assert res1["status"] == "already_fulfilled"
        print("PASS: Fulfilled order correctly rejected for cancellation:", res1["message"])

        # Case B: Unfulfilled order with unverified customer prompts for OTP
        mock_get_order.return_value = unfulfilled_order
        res2 = await shopify_actions.cancel_order(
            store,
            {"order_number": "1002"},
            session_id="sess_2",
            customer_id=None,  # Not verified yet
        )
        assert res2["status"] == "verification_required"
        print("PASS: Unverified customer prompted for confirmation code:", res2["message"])

        # Case C: Verification OTP confirmed and auto-cancelled
        # Set mode to 'auto'
        repo.set_order_cancellation_mode(store.id, "auto")
        with patch.object(shopify_actions, "_execute_shopify_order_cancel", return_value=(True, "")):
            # Pass verified OTP
            res3 = await shopify_actions.cancel_order(
                store,
                {"order_number": "1002"},
                session_id="sess_1",
                customer_id="12345",  # Verified customer
            )
            assert res3["status"] == "cancelled"
            print("PASS: Auto-cancellation executed successfully for unfulfilled order:", res3["message"])

        # Case D: Merchant mode 'ask'
        repo.set_order_cancellation_mode(store.id, "ask")
        res4 = await shopify_actions.cancel_order(
            store,
            {"order_number": "1002"},
            session_id="sess_1",
            customer_id="12345",
        )
        assert res4["status"] == "cancellation_requested"
        print("PASS: Mode 'ask' submits review request to merchant:", res4["message"])

        # Case E: Shipping address edit before fulfillment
        with patch.object(shopify_actions, "_put", return_value=MagicMock(status_code=200)):
            edit_res = await shopify_actions.edit_order_address(
                store,
                {"order_number": "1002", "address": "42 Marine Drive, Mumbai 400020"},
                session_id="sess_1",
                customer_id="12345",
            )
            assert edit_res["status"] == "address_updated"
            print("PASS: Pre-fulfillment shipping address updated:", edit_res["message"])

async def main():
    await test_email_sender_and_reply_to()
    await test_standing_instructions()
    await test_abandoned_cart_recovery()
    await test_close_alternatives_on_unavailable()
    await test_order_cancellation_and_edits()
    print("\n" + "=" * 60)
    print("  ALL 5 FEATURE TEST SUITES COMPLETED AND PASSED!  ")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
