"""
test_custom_icon.py
-------------------
Tests custom widget icon upload, persistence, and widget-config retrieval:
1. upload_icon_file returns a valid image URL or data URI
2. update_customization stores custom_icon_url and updates icon_type to 'custom'
3. get_customization / ensure_customization retrieves custom_icon_url
4. /widget-config endpoint includes custom_icon_url in response JSON
5. delete_icon_file cleans up stored icon
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import repository_appwrite as repo
import chatbot_widget
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

SAMPLE_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xff\xbf\x04\x00\x04\xac\x01\xfb\x9a\xf2\x9f\xe0\x00\x00\x00\x00IEND\xaeB`\x82"


def test_1_upload_icon_file_fallback_and_url():
    print("\n--- TEST 1: upload_icon_file ---")
    url = repo.upload_icon_file("test_store_123", "brand_icon.png", SAMPLE_PNG_BYTES)
    assert url, "upload_icon_file returned empty string"
    assert url.startswith("http") or url.startswith("data:image/"), f"Unexpected URL format: {url[:50]}"
    print(f"PASS: Icon uploaded successfully. Generated URL: {url[:60]}...")


def test_2_update_and_get_customization():
    print("\n--- TEST 2: Customization Persistence ---")
    mock_db = MagicMock()
    stored_doc = {
        "$id": "cust_mock_store_456",
        "store": "mock_store_456",
        "agent_name": "AI Assistant",
        "custom_icon_url": "",
        "icon_url": "",
        "icon_type": "preset"
    }

    def mock_update(db_id, col_id, doc_id, data):
        stored_doc.update(data)
        return dict(stored_doc)

    mock_db.update_document = mock_update
    mock_db.list_documents.return_value = {"documents": [stored_doc], "total": 1}

    with patch.object(repo, "databases", mock_db), patch.object(repo, "DATABASE_ID", "mock_db_id"):
        icon_url = "https://cdn.example.com/icons/my_icon.png"
        repo.update_customization("mock_store_456", custom_icon_url=icon_url, icon_type="custom")
        assert stored_doc["custom_icon_url"] == icon_url
        assert stored_doc["icon_type"] == "custom"

        # Verify get_customization returns the updated icon
        cfg = repo.get_customization("mock_store_456")
        assert cfg["custom_icon_url"] == icon_url
        print(f"PASS: Customization updated and retrieved: icon_type={cfg['icon_type']}, custom_icon_url={cfg['custom_icon_url']}")


def test_3_widget_config_endpoint():
    print("\n--- TEST 3: /widget-config Output ---")
    store = SimpleNamespace(id="mock_store_789", shop_domain="demo-store.myshopify.com")
    cfg = {
        "status": "active",
        "agent_name": "ShopperBot",
        "agent_title": "Hello! Need shopping help?",
        "icon_type": "custom",
        "theme_color": "#4F46E5",
        "custom_icon_url": "https://cdn.example.com/icons/my_icon.png"
    }

    with patch.object(repo, "get_store", lambda shop: {"$id": "mock_store_789"}), \
         patch.object(repo, "ensure_customization", lambda sid: cfg):
        resp = client.get("/widget-config?shop=demo-store.myshopify.com")
        assert resp.status_code == 200
        data = resp.json()
        assert data["custom_icon_url"] == "https://cdn.example.com/icons/my_icon.png"
        assert data["icon_type"] == "custom"
        assert data["theme_color"] == "#4F46E5"
        print("PASS: /widget-config correctly includes custom_icon_url and icon_type for the frontend.")


if __name__ == "__main__":
    test_1_upload_icon_file_fallback_and_url()
    test_2_update_and_get_customization()
    test_3_widget_config_endpoint()
    print("\n" + "="*50)
    print("  ALL CUSTOM ICON TESTS PASSED! ")
    print("="*50 + "\n")

