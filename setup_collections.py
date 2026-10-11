"""
setup_collections.py
---------------------
Adds the Appwrite structure needed for the rich "RenderLink" dashboard
on top of what you already have (Stores, Flows, RequestLogs,
AgentCustomizations, DashboardUsers — those are assumed to already
exist and are not touched by this script).

This script only:
  1. Adds `setup_completed` to your existing Stores collection, and
     `reset_token` / `reset_token_expires` to your existing
     DashboardUsers collection (needed for the login/setup-wizard and
     forgot-password flows in dashboard_nicegui.py).
  2. Adds `instructions`, `status`, `widget_position` attributes to
     your existing AgentCustomizations collection.
  3. Creates 4 new collections — Features, StoreInfo, FAQs, Feedback —
     each with a relationship back to Stores.

Safe to re-run: every create_* call is wrapped so a 409 "already
exists" response is printed and skipped instead of crashing the
script.

Also handles Appwrite's ASYNC attribute creation automatically: an
attribute briefly stays in "processing" status right after creation,
and any index or relationship that references it will fail with
"attribute ... is not yet available" if it runs too soon. This script
retries automatically with a short delay instead of making you rerun
it by hand every time that happens.

>>> BEFORE RUNNING <<<
Add these four new collection ID constants to appwrite_client.py
(pick any string ids you like, e.g. "features", "store_info", "faqs",
"feedback") and export them alongside your existing constants:

    FEATURES_COLLECTION
    STORE_INFO_COLLECTION
    FAQS_COLLECTION
    FEEDBACK_COLLECTION

Run: python setup_collections.py
"""

import time

from appwrite_client import (
    databases, DATABASE_ID, STORES_COLLECTION, CUSTOMIZATIONS_COLLECTION,
    FEATURES_COLLECTION, STORE_INFO_COLLECTION, FAQS_COLLECTION, FEEDBACK_COLLECTION,
    DASHBOARD_USERS_COLLECTION,
)
from appwrite.exception import AppwriteException
from appwrite.enums.relationship_type import RelationshipType

NOT_YET_AVAILABLE_RETRIES = 12   # up to ~1 minute of waiting per step
NOT_YET_AVAILABLE_DELAY_SECONDS = 5


def _run(step_description, fn, *args, **kwargs):
    """Call fn(*args, **kwargs).
    - 409 "already exists"      -> treated as success, skipped
    - "not yet available"       -> Appwrite is still processing a
                                    dependency (e.g. an attribute this
                                    index/relationship references) —
                                    wait and retry a few times
    - anything else             -> re-raised, stops the script
    """
    print(step_description)
    attempt = 0
    while True:
        try:
            fn(*args, **kwargs)
            return
        except AppwriteException as e:
            message = str(e).lower()
            if getattr(e, "code", None) == 409 or "already exists" in message:
                print("  -> already exists, skipped")
                return
            if "not yet available" in message or "processing" in message:
                attempt += 1
                if attempt > NOT_YET_AVAILABLE_RETRIES:
                    print(f"  -> FAILED (still not available after {attempt - 1} retries): {e}")
                    raise
                print(f"  -> not ready yet, waiting {NOT_YET_AVAILABLE_DELAY_SECONDS}s and retrying ({attempt}/{NOT_YET_AVAILABLE_RETRIES})...")
                time.sleep(NOT_YET_AVAILABLE_DELAY_SECONDS)
                continue
            print(f"  -> FAILED: {e}")
            raise


def setup():
    # ---- New attribute on the existing Stores collection ----
    _run(
        "  attribute 'stores.setup_completed'...",
        databases.create_boolean_attribute, DATABASE_ID, STORES_COLLECTION,
        "setup_completed", required=False, default=False,
    )

    # ---- New attributes on the existing DashboardUsers collection ----
    # (password reset flow — reset_token is looked up directly, so it
    # isn't marked as an Appwrite "required" field: a user who's never
    # requested a reset simply has it unset/None.)
    _run(
        "  attribute 'dashboard_users.reset_token'...",
        databases.create_string_attribute, DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        "reset_token", size=255, required=False,
    )
    _run(
        "  attribute 'dashboard_users.reset_token_expires'...",
        databases.create_string_attribute, DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        "reset_token_expires", size=64, required=False,
    )

    # ---- New attributes on the existing AgentCustomizations collection ----
    _run(
        "  attribute 'agent_customizations.instructions'...",
        databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION,
        "instructions", size=5000, required=False,
    )
    _run(
        "  attribute 'agent_customizations.status'...",
        databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION,
        "status", size=20, required=False, default="active",
    )
    _run(
        "  attribute 'agent_customizations.widget_position'...",
        databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION,
        "widget_position", size=20, required=False, default="bottom-right",
    )

    # ---- Features collection — one row per store, boolean toggles ----
    _run(f"Collection '{FEATURES_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=FEATURES_COLLECTION, name="Features", permissions=[])
    _run("  attribute 'product_search'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "product_search", required=False, default=True)
    _run("  attribute 'recommendations'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "recommendations", required=False, default=True)
    _run("  attribute 'product_filtering'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "product_filtering", required=False, default=True)
    _run("  attribute 'warranty'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "warranty", required=False, default=True)
    _run("  attribute 'cart_editing'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "cart_editing", required=False, default=True)
    _run("  attribute 'returns'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "returns", required=False, default=False)
    _run("  attribute 'track_orders'...", databases.create_boolean_attribute, DATABASE_ID, FEATURES_COLLECTION, "track_orders", required=False, default=True)

    # ---- StoreInfo collection — one row per store, business details ----
    _run(f"Collection '{STORE_INFO_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=STORE_INFO_COLLECTION, name="StoreInfo", permissions=[])
    _run("  attribute 'business_name'...", databases.create_string_attribute, DATABASE_ID, STORE_INFO_COLLECTION, "business_name", size=200, required=False)
    _run("  attribute 'support_email'...", databases.create_string_attribute, DATABASE_ID, STORE_INFO_COLLECTION, "support_email", size=255, required=False)
    _run("  attribute 'timezone'...", databases.create_string_attribute, DATABASE_ID, STORE_INFO_COLLECTION, "timezone", size=50, required=False, default="UTC")

    # ---- FAQs collection — many rows per store ----
    _run(f"Collection '{FAQS_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=FAQS_COLLECTION, name="FAQs", permissions=[])
    _run("  attribute 'question'...", databases.create_string_attribute, DATABASE_ID, FAQS_COLLECTION, "question", size=500, required=True)
    _run("  attribute 'answer'...", databases.create_string_attribute, DATABASE_ID, FAQS_COLLECTION, "answer", size=2000, required=True)

    # ---- Feedback collection — many rows per store, write-mostly ----
    _run(f"Collection '{FEEDBACK_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=FEEDBACK_COLLECTION, name="Feedback", permissions=[])
    _run("  attribute 'message'...", databases.create_string_attribute, DATABASE_ID, FEEDBACK_COLLECTION, "message", size=2000, required=True)

    # ---- Relationships — link each new collection back to Stores ----
    _run(
        "  relationship 'features.store' <-> 'stores.features'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=FEATURES_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.ONETOONE,   # one features row per store
        two_way=True,
        key="store",
        two_way_key="features",
    )
    _run(
        "  relationship 'store_info.store' <-> 'stores.store_info'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=STORE_INFO_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.ONETOONE,   # one store_info row per store
        two_way=True,
        key="store",
        two_way_key="store_info",
    )
    _run(
        "  relationship 'faqs.store' <-> 'stores.faqs'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=FAQS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.MANYTOONE,  # many FAQs belong to one store
        two_way=True,
        key="store",
        two_way_key="faqs",
    )
    _run(
        "  relationship 'feedback.store' <-> 'stores.feedback'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=FEEDBACK_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.MANYTOONE,  # many feedback rows belong to one store
        two_way=True,
        key="store",
        two_way_key="feedback",
    )

    print("\nDone. If anything above said 'FAILED' (not 'already exists, skipped'),\n"
          "wait ~10-15 seconds for Appwrite to finish processing the prior attribute\n"
          "and run this script again — it will pick up from there safely.")


if __name__ == "__main__":
    setup()
