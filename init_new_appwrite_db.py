"""
init_new_appwrite_db.py
------------------------
Automated script to initialize a brand new Appwrite database from scratch.
Creates all 10 collections, their attributes, and indexes for Store-Ai-Agent:
  1. stores
  2. agent_customizations
  3. dashboard_users
  4. features
  5. store_info
  6. faqs
  7. feedback
  8. flows
  9. request_logs
  10. analytics_events

Usage:
  1. In Appwrite console -> Databases -> Create Database (e.g. named "store-agent-test").
  2. Copy the new Database ID.
  3. Run:
       python init_new_appwrite_db.py --database-id <YOUR_NEW_DATABASE_ID>
     or set APPWRITE_DATABASE_ID in your environment and run:
       python init_new_appwrite_db.py
"""

import os
import sys
import time
import argparse

try:
    from appwrite.client import Client
    from appwrite.services.databases import Databases
    from appwrite.exception import AppwriteException
    from appwrite.enums.index_type import IndexType
except ImportError:
    print("Error: The 'appwrite' package is required. Run: pip install appwrite")
    sys.exit(1)

from appwrite_client import (
    APPWRITE_ENDPOINT, APPWRITE_PROJECT_ID,
    STORES_COLLECTION, CUSTOMIZATIONS_COLLECTION, DASHBOARD_USERS_COLLECTION,
    FEATURES_COLLECTION, STORE_INFO_COLLECTION, FAQS_COLLECTION, FEEDBACK_COLLECTION,
    FLOWS_COLLECTION, REQUEST_LOGS_COLLECTION,
)

ANALYTICS_EVENTS_COLLECTION = "analytics_events"

RETRIES = 15
DELAY_SECONDS = 4


def _run(desc, fn, *args, **kwargs):
    print(f"  {desc}...", end=" ", flush=True)
    attempt = 0
    while True:
        try:
            res = fn(*args, **kwargs)
            print("OK")
            return res
        except AppwriteException as e:
            msg = str(e).lower()
            if getattr(e, "code", None) == 409 or "already exists" in msg:
                print("Already exists (skipped)")
                return None
            if "not yet available" in msg or "processing" in msg:
                attempt += 1
                if attempt > RETRIES:
                    print(f"FAILED (timed out waiting): {e}")
                    raise
                print(f"Processing (wait {DELAY_SECONDS}s, retry {attempt}/{RETRIES})...", end=" ", flush=True)
                time.sleep(DELAY_SECONDS)
                continue
            print(f"FAILED: {e}")
            raise


def create_all_collections(databases, db_id):
    print("\n[1/3] Creating collections...")
    collections = [
        (STORES_COLLECTION, "Stores"),
        (CUSTOMIZATIONS_COLLECTION, "Agent Customizations"),
        (DASHBOARD_USERS_COLLECTION, "Dashboard Users"),
        (FEATURES_COLLECTION, "Features"),
        (STORE_INFO_COLLECTION, "Store Info"),
        (FAQS_COLLECTION, "FAQs"),
        (FEEDBACK_COLLECTION, "Feedback"),
        (FLOWS_COLLECTION, "Flows"),
        (REQUEST_LOGS_COLLECTION, "Request Logs"),
        (ANALYTICS_EVENTS_COLLECTION, "Analytics Events"),
    ]
    for cid, name in collections:
        _run(f"Collection '{cid}'", databases.create_collection, db_id, cid, name)

    print("\n[2/3] Creating attributes...")
    # 1. stores
    _run("stores.shop_domain", databases.create_string_attribute, db_id, STORES_COLLECTION, "shop_domain", 255, True)
    _run("stores.access_token", databases.create_string_attribute, db_id, STORES_COLLECTION, "access_token", 512, True)
    _run("stores.scopes", databases.create_string_attribute, db_id, STORES_COLLECTION, "scopes", 1000, False)
    _run("stores.uninstalled", databases.create_boolean_attribute, db_id, STORES_COLLECTION, "uninstalled", False, False)
    _run("stores.setup_completed", databases.create_boolean_attribute, db_id, STORES_COLLECTION, "setup_completed", False, False)
    _run("stores.plan", databases.create_string_attribute, db_id, STORES_COLLECTION, "plan", 50, False, "free")

    # 2. agent_customizations
    _run("customizations.store", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "store", 255, True)
    _run("customizations.agent_name", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "agent_name", 100, False, "AI Assistant")
    _run("customizations.agent_title", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "agent_title", 200, False, "How can I help you today?")
    _run("customizations.theme_color", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "theme_color", 30, False, "#2b2b2b")
    _run("customizations.icon_type", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "icon_type", 30, False, "preset")
    _run("customizations.icon_url", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "icon_url", 1000, False)
    _run("customizations.instructions", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "instructions", 10000, False)
    _run("customizations.status", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "status", 30, False, "active")
    _run("customizations.widget_position", databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION, "widget_position", 30, False, "bottom-right")

    # 3. dashboard_users
    _run("users.store", databases.create_string_attribute, db_id, DASHBOARD_USERS_COLLECTION, "store", 255, True)
    _run("users.email", databases.create_string_attribute, db_id, DASHBOARD_USERS_COLLECTION, "email", 255, True)
    _run("users.password_hash", databases.create_string_attribute, db_id, DASHBOARD_USERS_COLLECTION, "password_hash", 255, True)
    _run("users.reset_token", databases.create_string_attribute, db_id, DASHBOARD_USERS_COLLECTION, "reset_token", 255, False)
    _run("users.reset_token_expires", databases.create_integer_attribute, db_id, DASHBOARD_USERS_COLLECTION, "reset_token_expires", False)

    # 4. features
    _run("features.store", databases.create_string_attribute, db_id, FEATURES_COLLECTION, "store", 255, True)
    _run("features.product_search", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "product_search", False, True)
    _run("features.recommendations", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "recommendations", False, True)
    _run("features.product_filtering", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "product_filtering", False, True)
    _run("features.warranty", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "warranty", False, True)
    _run("features.cart_editing", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "cart_editing", False, True)
    _run("features.returns", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "returns", False, False)
    _run("features.track_orders", databases.create_boolean_attribute, db_id, FEATURES_COLLECTION, "track_orders", False, True)

    # 5. store_info
    _run("store_info.store", databases.create_string_attribute, db_id, STORE_INFO_COLLECTION, "store", 255, True)
    _run("store_info.business_name", databases.create_string_attribute, db_id, STORE_INFO_COLLECTION, "business_name", 255, False, "")
    _run("store_info.support_email", databases.create_string_attribute, db_id, STORE_INFO_COLLECTION, "support_email", 255, False, "")
    _run("store_info.timezone", databases.create_string_attribute, db_id, STORE_INFO_COLLECTION, "timezone", 100, False, "UTC")

    # 6. faqs
    _run("faqs.store", databases.create_string_attribute, db_id, FAQS_COLLECTION, "store", 255, True)
    _run("faqs.question", databases.create_string_attribute, db_id, FAQS_COLLECTION, "question", 500, True)
    _run("faqs.answer", databases.create_string_attribute, db_id, FAQS_COLLECTION, "answer", 5000, True)

    # 7. feedback
    _run("feedback.store", databases.create_string_attribute, db_id, FEEDBACK_COLLECTION, "store", 255, True)
    _run("feedback.message", databases.create_string_attribute, db_id, FEEDBACK_COLLECTION, "message", 5000, True)

    # 8. flows
    _run("flows.store", databases.create_string_attribute, db_id, FLOWS_COLLECTION, "store", 255, True)
    _run("flows.intent", databases.create_string_attribute, db_id, FLOWS_COLLECTION, "intent", 100, True)
    _run("flows.action", databases.create_string_attribute, db_id, FLOWS_COLLECTION, "action", 100, True)
    _run("flows.url", databases.create_string_attribute, db_id, FLOWS_COLLECTION, "url", 1000, False)
    _run("flows.steps_json", databases.create_string_attribute, db_id, FLOWS_COLLECTION, "steps_json", 10000, False)

    # 9. request_logs
    _run("logs.store", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "store", 255, False)
    _run("logs.message", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "message", 2000, False)
    _run("logs.status", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "status", 50, False)
    _run("logs.detected_intent", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "detected_intent", 100, False)
    _run("logs.detected_action", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "detected_action", 100, False)
    _run("logs.reply", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "reply", 5000, False)
    _run("logs.entities", databases.create_string_attribute, db_id, REQUEST_LOGS_COLLECTION, "entities", 5000, False)

    # 10. analytics_events
    _run("events.store", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "store", 255, True)
    _run("events.event_type", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "event_type", 32, True)
    _run("events.platform", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "platform", 32, False, "shopify")
    _run("events.name", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "name", 255, False)
    _run("events.category", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "category", 255, False)
    _run("events.value", databases.create_float_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "value", False, 1.0)
    _run("events.currency", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "currency", 8, False, "USD")
    _run("events.session_id", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "session_id", 100, False)
    _run("events.visitor_id", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "visitor_id", 100, False)
    _run("events.external_id", databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION, "external_id", 100, False)

    print("\n[3/3] Creating indexes...")
    _run("idx_stores_domain", databases.create_index, db_id, STORES_COLLECTION, "idx_shop_domain", IndexType.KEY, ["shop_domain"])
    _run("idx_customizations_store", databases.create_index, db_id, CUSTOMIZATIONS_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_users_store", databases.create_index, db_id, DASHBOARD_USERS_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_users_email", databases.create_index, db_id, DASHBOARD_USERS_COLLECTION, "idx_email", IndexType.KEY, ["email"])
    _run("idx_features_store", databases.create_index, db_id, FEATURES_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_store_info_store", databases.create_index, db_id, STORE_INFO_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_faqs_store", databases.create_index, db_id, FAQS_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_feedback_store", databases.create_index, db_id, FEEDBACK_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_flows_store", databases.create_index, db_id, FLOWS_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_events_store", databases.create_index, db_id, ANALYTICS_EVENTS_COLLECTION, "idx_store", IndexType.KEY, ["store"])
    _run("idx_events_type", databases.create_index, db_id, ANALYTICS_EVENTS_COLLECTION, "idx_event_type", IndexType.KEY, ["event_type"])

    print("\n=======================================================")
    print(" ALL 10 COLLECTIONS, ATTRIBUTES, & INDEXES INITIALIZED!")
    print("=======================================================")


def main():
    parser = argparse.ArgumentParser(description="Initialize new Appwrite database collections.")
    parser.add_argument("--database-id", help="Appwrite Database ID")
    parser.add_argument("--project-id", help="Appwrite Project ID")
    parser.add_argument("--api-key", help="Appwrite Server API Key")
    parser.add_argument("--endpoint", help="Appwrite Endpoint URL")
    args = parser.parse_args()

    endpoint = args.endpoint or os.environ.get("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")
    project_id = args.project_id or os.environ.get("APPWRITE_PROJECT_ID", "")
    api_key = args.api_key or os.environ.get("APPWRITE_API_KEY", "")
    db_id = args.database_id or os.environ.get("APPWRITE_DATABASE_ID", "")

    if not project_id or not api_key:
        print("Error: Missing APPWRITE_PROJECT_ID or APPWRITE_API_KEY.")
        sys.exit(1)
    if not db_id:
        print("Error: Please provide --database-id <ID> or set APPWRITE_DATABASE_ID.")
        sys.exit(1)

    print(f"Connecting to Appwrite: {endpoint}")
    print(f"Project ID: {project_id}")
    print(f"Target Database ID: {db_id}")

    client = Client()
    client.set_endpoint(endpoint)
    client.set_project(project_id)
    client.set_key(api_key)

    databases = Databases(client)
    create_all_collections(databases, db_id)


if __name__ == "__main__":
    main()
