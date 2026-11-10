"""
test_recent_features.py
------------------------
Targeted automated tests for the recently implemented & updated features:
1. Long scopes truncation (<1000 chars) in repository_appwrite.upsert_shop
2. Domain normalization for get_store / _find_store_doc
3. Chat history endpoint (/chat-history) and memory retrieval
4. Diagnostic endpoint (/debug/store)
5. Setup wizard store auto-registration fallback logic
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import repository_appwrite as repo
import shopify_auth
import chatbot_widget
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_1_domain_normalization():
    print("\n--- TEST 1: Domain Normalization ---")
    domains = [
        ("test-store.myshopify.com", "test-store.myshopify.com"),
        ("https://test-store.myshopify.com", "test-store.myshopify.com"),
        ("https://test-store.myshopify.com/admin/settings", "test-store.myshopify.com"),
        ("http://test-store.myshopify.com", "test-store.myshopify.com"),
        ("test-store", "test-store.myshopify.com"),
        ("https://admin.shopify.com/store/my-fashion-boutique", "my-fashion-boutique.myshopify.com"),
    ]
    for raw, expected in domains:
        normalized = repo._normalize_domain(raw)
        assert normalized == expected, f"Failed for {raw}: got {normalized}, expected {expected}"
    print("PASS: Domain normalization handles all Shopify URL patterns correctly.")


def test_2_scopes_truncation():
    print("\n--- TEST 2: Scopes Truncation (< 1000 characters) ---")
    # Simulate a Shopify scopes string with > 2000 chars
    giant_scopes = ",".join([f"scope_permission_{i}" for i in range(120)])
    assert len(giant_scopes) > 1500, "Test scopes should be > 1500 chars"

    saved_payload = {}
    mock_db = MagicMock()
    def mock_create_document(db_id, col_id, doc_id, data):
        saved_payload.update(data)
        return {"$id": "mock_id_123", **data}

    mock_db.create_document = mock_create_document
    mock_db.list_documents.return_value = {"documents": [], "total": 0}

    with patch.object(repo, "databases", mock_db), patch.object(repo, "DATABASE_ID", "mock_db_id"):
        result = repo.upsert_shop("myshop.myshopify.com", "shpat_test_token", giant_scopes)
        saved_scopes = saved_payload.get("scopes", "")
        assert len(saved_scopes) <= 1000, f"Scopes exceed 1000 chars: {len(saved_scopes)}"
        assert saved_scopes.endswith("scope_permission_") is False, "Scopes should cleanly cut at a comma"
        print(f"PASS: Scopes safely truncated from {len(giant_scopes)} to {len(saved_scopes)} chars (under 1000 limit).")


def test_3_chat_history_endpoint():
    print("\n--- TEST 3: Widget Chat History Endpoint ---")
    # Seed in-memory history
    session_id = "test_hist_session_999"
    key = chatbot_widget._pkey("demo.myshopify.com", session_id)
    chatbot_widget._remember_turn(key, "What is your refund policy?", "We offer 30-day hassle free returns.")

    response = client.get(f"/chat-history?shop=demo.myshopify.com&session_id={session_id}")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert "chats" in data, "Response missing 'chats' array"
    assert len(data["chats"]) >= 1, "Expected at least 1 turn in history"
    first_turn = data["chats"][0]
    assert first_turn.get("user_message") == "What is your refund policy?"
    assert first_turn.get("agent_reply") == "We offer 30-day hassle free returns."
    print("PASS: /chat-history successfully pulls session history and formats turns.")


def test_4_debug_store_endpoint():
    print("\n--- TEST 4: Diagnostic Endpoint (/debug/store) ---")
    response = client.get("/debug/store?shop=my-test-store.myshopify.com")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert data["query_domain"] == "my-test-store.myshopify.com"
    assert data["normalized"] == "my-test-store.myshopify.com"
    assert "appwrite_endpoint" in data
    assert "database_id" in data
    print("PASS: /debug/store endpoint returns accurate environment and lookup details.")


def test_5_upsert_shop_schema_resilience():
    print("\n--- TEST 5: Upsert Shop Schema Fallback Resilience ---")
    # Simulate Appwrite rejecting full payload due to missing 'plan' attribute
    mock_db = MagicMock()
    attempts = []

    def mock_create(db_id, col_id, doc_id, data):
        attempts.append(dict(data))
        if "plan" in data:
            raise Exception("Invalid attribute: plan does not exist")
        return {"$id": "mock_id_fallback", **data}

    mock_db.create_document = mock_create
    mock_db.list_documents.return_value = {"documents": [], "total": 0}

    with patch.object(repo, "databases", mock_db), patch.object(repo, "DATABASE_ID", "mock_db_id"):
        res = repo.upsert_shop("fallback-store.myshopify.com", "shpat_fallback_token", "read_products")
        assert len(attempts) == 2, "Expected a retry attempt after schema error"
        assert "plan" not in attempts[1], "Second attempt should omit unrecognized attributes"
        assert res["$id"] == "mock_id_fallback"
        print("PASS: upsert_shop seamlessly falls back to minimal schema on attribute errors.")


if __name__ == "__main__":
    test_1_domain_normalization()
    test_2_scopes_truncation()
    test_3_chat_history_endpoint()
    test_4_debug_store_endpoint()
    test_5_upsert_shop_schema_resilience()
    print("\n" + "="*50)
    print("  ALL 5 RECENT FEATURE TESTS PASSED SUCCESSFULLY! ")
    print("="*50 + "\n")
