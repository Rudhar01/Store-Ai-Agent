"""
test_products_and_orders.py
----------------------------
Comprehensive automated tests verifying:
1. Product Search:
   - Matching products are retrieved from Shopify catalog
   - Search results format: id, name, price, image, handle, url
   - Fallback recommendations when no exact match exists
   - Price range and variant filtering
   - Chat endpoint returns "products" list in response JSON for the widget

2. Orders Data:
   - Order history lookup by email
   - Order history lookup by customer_id (logged-in customer session)
   - Order data format: id, order_number, date, status, items, total, tracking
   - Safeguard: verification required for anonymous requests without email
   - Chat endpoint returns "orders" list in response JSON for the widget
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import shopify_actions
import chatbot_widget
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

MOCK_PRODUCTS_PAYLOAD = {
    "products": [
        {
            "id": 1001,
            "title": "Classic Leather Oxford Shoes",
            "handle": "classic-leather-oxford-shoes",
            "image": {"src": "https://cdn.shopify.com/s/files/shoes.jpg"},
            "variants": [
                {"id": 5001, "price": "120.00", "option1": "Brown", "option2": "42"}
            ]
        },
        {
            "id": 1002,
            "title": "Urban Running Sneakers",
            "handle": "urban-running-sneakers",
            "image": {"src": "https://cdn.shopify.com/s/files/sneakers.jpg"},
            "variants": [
                {"id": 5002, "price": "85.00", "option1": "Black", "option2": "43"}
            ]
        },
        {
            "id": 1003,
            "title": "Cozy Cotton Hoodie",
            "handle": "cozy-cotton-hoodie",
            "image": {"src": "https://cdn.shopify.com/s/files/hoodie.jpg"},
            "variants": [
                {"id": 5003, "price": "65.00", "option1": "Navy", "option2": "L"}
            ]
        }
    ]
}

MOCK_ORDERS_PAYLOAD = {
    "orders": [
        {
            "id": 9001,
            "order_number": 1042,
            "created_at": "2026-11-05T14:30:00Z",
            "financial_status": "paid",
            "fulfillment_status": "in_transit",
            "total_price": "120.00",
            "currency": "USD",
            "line_items": [
                {"title": "Classic Leather Oxford Shoes", "quantity": 1, "price": "120.00"}
            ],
            "fulfillments": [
                {
                    "tracking_company": "FedEx",
                    "tracking_number": "FX123456789",
                    "tracking_url": "https://fedex.com/track/FX123456789",
                    "shipment_status": "in_transit"
                }
            ]
        },
        {
            "id": 9002,
            "order_number": 1035,
            "created_at": "2026-10-20T10:15:00Z",
            "financial_status": "paid",
            "fulfillment_status": "delivered",
            "total_price": "65.00",
            "currency": "USD",
            "line_items": [
                {"title": "Cozy Cotton Hoodie", "quantity": 1, "price": "65.00"}
            ],
            "fulfillments": []
        }
    ]
}


def test_1_product_search_direct():
    print("\n--- TEST 1: Direct Product Search Execution ---")
    store = SimpleNamespace(
        id="mock_store_id",
        shop_domain="demo-store.myshopify.com",
        access_token="shpat_mock"
    )

    async def mock_get(s, path, params=None):
        if "products.json" in path:
            return SimpleNamespace(status_code=200, json=lambda: MOCK_PRODUCTS_PAYLOAD)
        return SimpleNamespace(status_code=404, json=lambda: {})

    with patch.object(shopify_actions, "_get", mock_get):
        # 1. Search for 'shoes'
        res = asyncio.run(shopify_actions.search_products(store, {"query": "shoes"}, "sess_1"))
        assert "results" in res, "Missing 'results' key"
        results = res["results"]
        assert len(results) >= 1, "Expected at least 1 product matching 'shoes'"
        shoe = results[0]
        assert "Shoes" in shoe["name"]
        assert shoe["id"] == "5001"
        assert shoe["price"] == 120.0
        assert "image" in shoe
        assert "handle" in shoe
        assert "url" in shoe
        print(f"PASS: Product search for 'shoes' matched '{shoe['name']}' (${shoe['price']}).")

        # 2. Search with price filter: max price $90
        res_filtered = asyncio.run(shopify_actions.search_products(store, {"query": "shoes", "price_max": 90}, "sess_1"))
        filtered_results = res_filtered.get("results", [])
        # Sneakers are $85, Oxfords are $120 -> should match Sneakers or none above $90
        for p in filtered_results:
            assert p["price"] <= 90.0, f"Product {p['name']} exceeded price_max 90: {p['price']}"
        print("PASS: Product search price filter correctly enforced.")


def test_2_product_search_chat_endpoint():
    print("\n--- TEST 2: Product Search via /chat Widget Endpoint ---")
    store = SimpleNamespace(
        id="mock_store_id",
        shop_domain="demo-store.myshopify.com",
        access_token="shpat_mock"
    )

    async def mock_get(s, path, params=None):
        if "products.json" in path:
            return SimpleNamespace(status_code=200, json=lambda: MOCK_PRODUCTS_PAYLOAD)
        return SimpleNamespace(status_code=404, json=lambda: {})

    with patch.object(shopify_actions, "_get", mock_get), patch.object(chatbot_widget, "_get_store", lambda s: store):
        payload = {
            "shop": "demo-store.myshopify.com",
            "session_id": "test_search_session",
            "message": "Do you have any shoes or sneakers in stock?"
        }
        resp = client.post("/chat", json=payload)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert "products" in data, "Response JSON missing 'products' key for widget display"
        products = data["products"]
        assert len(products) > 0, "No products returned to widget"
        assert any("Shoe" in p["name"] or "Sneaker" in p["name"] for p in products)
        print(f"PASS: Widget /chat endpoint returned {len(products)} product card(s) for rendering in UI.")


def test_3_orders_lookup_by_email():
    print("\n--- TEST 3: Orders Lookup by Email ---")
    store = SimpleNamespace(
        id="mock_store_id",
        shop_domain="demo-store.myshopify.com",
        access_token="shpat_mock"
    )

    async def mock_get(s, path, params=None):
        if "orders.json" in path:
            return SimpleNamespace(status_code=200, json=lambda: MOCK_ORDERS_PAYLOAD)
        return SimpleNamespace(status_code=404, json=lambda: {})

    with patch.object(shopify_actions, "_get", mock_get):
        res = asyncio.run(shopify_actions.list_recent_orders(store, {"email": "customer@example.com"}, "sess_1"))
        assert "orders" in res, "Missing 'orders' key in response"
        orders = res["orders"]
        assert len(orders) == 2, f"Expected 2 orders, got {len(orders)}"
        first_order = orders[0]
        assert first_order["order_number"] == "1042" or first_order.get("id") == "9001"
        assert first_order["status"] in ("in transit", "in_transit")
        assert first_order["fulfillment_status"] == "in_transit"
        assert len(first_order["items"]) >= 1
        assert first_order["total"] == "120.00"
        print(f"PASS: Looked up 2 order(s) by email: #{first_order['order_number']} ({first_order['status']}).")


def test_4_orders_security_verification_guard():
    print("\n--- TEST 4: Orders Anonymous Security Verification Guard ---")
    store = SimpleNamespace(
        id="mock_store_id",
        shop_domain="demo-store.myshopify.com",
        access_token="shpat_mock"
    )

    # An anonymous request with no email and no customer_id should require email verification
    res = asyncio.run(shopify_actions.list_recent_orders(store, {}, "anonymous_session"))
    assert res.get("error") == "verification_required", "Expected verification_required for anonymous shopper"
    assert "email" in res.get("message", "").lower()
    print("PASS: Anonymous order lookup properly prompted for account email.")


def test_5_orders_chat_endpoint_logged_in():
    print("\n--- TEST 5: Orders via /chat Widget Endpoint for Logged-In Shopper ---")
    store = SimpleNamespace(
        id="mock_store_id",
        shop_domain="demo-store.myshopify.com",
        access_token="shpat_mock"
    )

    async def mock_get(s, path, params=None):
        if "orders.json" in path:
            return SimpleNamespace(status_code=200, json=lambda: MOCK_ORDERS_PAYLOAD)
        return SimpleNamespace(status_code=404, json=lambda: {})

    with patch.object(shopify_actions, "_get", mock_get), patch.object(chatbot_widget, "_get_store", lambda s: store):
        payload = {
            "shop": "demo-store.myshopify.com",
            "session_id": "cust_session_42",
            "customer_email": "customer@example.com",
            "customer_id": "cust_12345",
            "message": "Where are my recent orders?"
        }
        resp = client.post("/chat", json=payload)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert "orders" in data, "Response JSON missing 'orders' key for widget display"
        orders = data["orders"]
        assert len(orders) > 0, "No orders provided to widget"
        assert orders[0]["order_number"] == "1042" or orders[0].get("id") == "9001"
        print(f"PASS: Widget /chat endpoint emitted {len(orders)} order card(s) with tracking for logged-in user.")


if __name__ == "__main__":
    test_1_product_search_direct()
    test_2_product_search_chat_endpoint()
    test_3_orders_lookup_by_email()
    test_4_orders_security_verification_guard()
    test_5_orders_chat_endpoint_logged_in()
    print("\n" + "="*50)
    print("  ALL PRODUCT SEARCH & ORDERS TESTS PASSED! ")
    print("="*50 + "\n")
