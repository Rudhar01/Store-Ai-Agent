"""
migrate_appwrite_db.py
-----------------------
Adds attributes that were introduced in newer code versions to an existing
Appwrite database without touching documents that are already there.

Run this whenever you see errors like:
  - "Unknown attribute: plan"
  - "Unknown attribute: setup_completed"
  - upsert_shop writes failing silently

Usage (all args are optional if env vars are set):
  python migrate_appwrite_db.py
  python migrate_appwrite_db.py --project-id <ID> --api-key <KEY> --database-id <DB_ID>
"""

import os
import sys
import time
import argparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from appwrite.client import Client
    from appwrite.services.databases import Databases
    from appwrite.exception import AppwriteException
except ImportError:
    print("Error: The 'appwrite' package is required. Run: pip install appwrite")
    sys.exit(1)

RETRIES = 12
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
            code = getattr(e, "code", None)
            if code == 409 or "already exists" in msg:
                print("Already exists (skipped)")
                return None
            if "not yet available" in msg or "processing" in msg:
                attempt += 1
                if attempt > RETRIES:
                    print(f"FAILED (timed out waiting): {e}")
                    raise
                print(f"Processing… retrying {attempt}/{RETRIES} ", end="", flush=True)
                time.sleep(DELAY_SECONDS)
                continue
            print(f"SKIPPED ({e})")
            return None


def migrate(databases, db_id):
    from appwrite_client import (
        STORES_COLLECTION,
        CUSTOMIZATIONS_COLLECTION,
        DASHBOARD_USERS_COLLECTION,
        FEATURES_COLLECTION,
        STORE_INFO_COLLECTION,
        FAQS_COLLECTION,
        FEEDBACK_COLLECTION,
        FLOWS_COLLECTION,
        REQUEST_LOGS_COLLECTION,
    )
    ANALYTICS_EVENTS_COLLECTION = "analytics_events"
    CUSTOMER_PROFILES_COLLECTION = "customer_profiles"

    print("\n── Stores collection ─────────────────────────────────")
    # New optional attributes added after the initial schema
    _run("stores.setup_completed",
         databases.create_boolean_attribute, db_id, STORES_COLLECTION,
         "setup_completed", False, False)
    _run("stores.plan",
         databases.create_string_attribute, db_id, STORES_COLLECTION,
         "plan", 50, False, "free")

    print("\n── Agent Customizations ───────────────────────────────")
    _run("agent_customizations.instructions",
         databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION,
         "instructions", 8000, False)
    _run("agent_customizations.status",
         databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION,
         "status", 20, False, "active")
    _run("agent_customizations.widget_position",
         databases.create_string_attribute, db_id, CUSTOMIZATIONS_COLLECTION,
         "widget_position", 30, False, "bottom-right")

    print("\n── Store Info ─────────────────────────────────────────")
    _run("store_info.business_name",
         databases.create_string_attribute, db_id, STORE_INFO_COLLECTION,
         "business_name", 255, False)
    _run("store_info.support_email",
         databases.create_string_attribute, db_id, STORE_INFO_COLLECTION,
         "support_email", 255, False)
    _run("store_info.timezone",
         databases.create_string_attribute, db_id, STORE_INFO_COLLECTION,
         "timezone", 100, False, "UTC")
    _run("store_info.store",
         databases.create_string_attribute, db_id, STORE_INFO_COLLECTION,
         "store", 36, False)

    print("\n── Customer Profiles (new collection) ─────────────────")
    # Create the collection first (409 = already exists = fine)
    try:
        databases.create_collection(db_id, CUSTOMER_PROFILES_COLLECTION, "Customer Profiles")
        print(f"  Created collection '{CUSTOMER_PROFILES_COLLECTION}'")
    except AppwriteException as e:
        if getattr(e, "code", None) == 409 or "already exists" in str(e).lower():
            print(f"  Collection '{CUSTOMER_PROFILES_COLLECTION}' already exists (skipped)")
        else:
            print(f"  Could not create '{CUSTOMER_PROFILES_COLLECTION}': {e}")
    _run("customer_profiles.store",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "store", 255, False)
    _run("customer_profiles.shop",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "shop", 255, False)
    _run("customer_profiles.customer_id",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "customer_id", 128, False)
    _run("customer_profiles.email",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "email", 255, False)
    _run("customer_profiles.first_name",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "first_name", 100, False)
    _run("customer_profiles.last_name",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "last_name", 100, False)
    _run("customer_profiles.name",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "name", 255, False)
    _run("customer_profiles.phone",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "phone", 60, False)
    _run("customer_profiles.order_count",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "order_count", False, 0)
    _run("customer_profiles.orders_count",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "orders_count", False, 0)
    _run("customer_profiles.total_spent",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "total_spent", 60, False)
    _run("customer_profiles.recent_orders",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "recent_orders", 65535, False)
    _run("customer_profiles.orders_data",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "orders_data", 65535, False)
    _run("customer_profiles.active_orders",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "active_orders", 10000, False)
    _run("customer_profiles.purchased_product_ids",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "purchased_product_ids", 65535, False)
    _run("customer_profiles.top_types",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "top_types", 4096, False)
    _run("customer_profiles.top_tags",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "top_tags", 4096, False)
    _run("customer_profiles.recent_searches",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "recent_searches", 4096, False)
    _run("customer_profiles.favorite_colors",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "favorite_colors", 1024, False)
    _run("customer_profiles.budget_min",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "budget_min", False, 0)
    _run("customer_profiles.budget_max",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "budget_max", False, 0)
    _run("customer_profiles.chat_history",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "chat_history", 65535, False)
    _run("customer_profiles.last_message",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "last_message", 1000, False)
    _run("customer_profiles.total_chats",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "total_chats", False, 0)
    _run("customer_profiles.preferences",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "preferences", 4000, False)
    _run("customer_profiles.tags",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "tags", 1000, False)
    _run("customer_profiles.currency",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "currency", 20, False)
    _run("customer_profiles.addresses",
         databases.create_string_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "addresses", 2000, False)
    _run("customer_profiles.synced_at",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "synced_at", False, 0)
    _run("customer_profiles.last_seen",
         databases.create_integer_attribute, db_id, CUSTOMER_PROFILES_COLLECTION,
         "last_seen", False, 0)

    print("\n── Analytics Events ───────────────────────────────────")
    # Create the collection if it doesn't exist
    try:
        databases.create_collection(db_id, ANALYTICS_EVENTS_COLLECTION, "Analytics Events")
        print(f"  Created collection '{ANALYTICS_EVENTS_COLLECTION}'")
    except AppwriteException as e:
        if getattr(e, "code", None) == 409 or "already exists" in str(e).lower():
            print(f"  Collection '{ANALYTICS_EVENTS_COLLECTION}' already exists (skipped)")
        else:
            print(f"  Could not create '{ANALYTICS_EVENTS_COLLECTION}': {e}")
    _run("analytics_events.store",
         databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION,
         "store", 36, False)
    _run("analytics_events.event_type",
         databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION,
         "event_type", 100, False)
    _run("analytics_events.session_id",
         databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION,
         "session_id", 100, False)
    _run("analytics_events.payload",
         databases.create_string_attribute, db_id, ANALYTICS_EVENTS_COLLECTION,
         "payload", 8000, False)

    print("\n── Indexes (new) ──────────────────────────────────────")
    try:
        from appwrite.enums.databases_index_type import DatabasesIndexType
        IDX_KEY = DatabasesIndexType.KEY
    except ImportError:
        try:
            from appwrite.enums.index_type import IndexType
            IDX_KEY = IndexType.KEY
        except ImportError:
            IDX_KEY = "key"

    _run("idx_stores_domain",
         databases.create_index, db_id, STORES_COLLECTION,
         "idx_shop_domain", IDX_KEY, ["shop_domain"])
    _run("idx_analytics_store",
         databases.create_index, db_id, ANALYTICS_EVENTS_COLLECTION,
         "idx_store", IDX_KEY, ["store"])
    _run("idx_profiles_store",
         databases.create_index, db_id, CUSTOMER_PROFILES_COLLECTION,
         "idx_store", IDX_KEY, ["store"])
    _run("idx_profiles_cid",
         databases.create_index, db_id, CUSTOMER_PROFILES_COLLECTION,
         "idx_customer_id", IDX_KEY, ["customer_id"])

    print("\nMigration complete.")


def main():
    parser = argparse.ArgumentParser(description="Migrate Appwrite DB schema")
    parser.add_argument("--project-id", default=os.environ.get("APPWRITE_PROJECT_ID", ""))
    parser.add_argument("--api-key", default=os.environ.get("APPWRITE_API_KEY", ""))
    parser.add_argument("--database-id", default=os.environ.get("APPWRITE_DATABASE_ID", ""))
    parser.add_argument("--endpoint", default=os.environ.get("APPWRITE_ENDPOINT", "https://sgp.cloud.appwrite.io/v1"))
    args = parser.parse_args()

    missing = [k for k, v in [("project-id", args.project_id), ("api-key", args.api_key), ("database-id", args.database_id)] if not v]
    if missing:
        print(f"Error: Missing required args/env vars: {', '.join(missing)}")
        sys.exit(1)

    client = Client()
    client.set_endpoint(args.endpoint)
    client.set_project(args.project_id)
    client.set_key(args.api_key)
    databases = Databases(client)

    print(f"Connecting to: {args.endpoint}")
    print(f"Project:       {args.project_id}")
    print(f"Database:      {args.database_id}")
    print("Starting migration…\n")

    migrate(databases, args.database_id)


if __name__ == "__main__":
    main()

