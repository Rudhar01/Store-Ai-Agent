"""
migrate_relationship_to_store_id.py
--------------------------------------
Run this ONCE to fix the duplicate-document bug: adds a plain, instantly
-queryable `store_id` string attribute (+ index) to every collection that
was using a relationship attribute called `store`, then backfills
store_id on every EXISTING document by resolving that old relationship.

Safe to re-run (same idempotent pattern as setup_collections.py: 409
"already exists" is skipped, "not yet available" is retried).

Run: python migrate_relationship_to_store_id.py
"""

import time

from appwrite_client import (
    databases, DATABASE_ID,
    FLOWS_COLLECTION, REQUEST_LOGS_COLLECTION, CUSTOMIZATIONS_COLLECTION,
    DASHBOARD_USERS_COLLECTION, FEATURES_COLLECTION, STORE_INFO_COLLECTION,
    FAQS_COLLECTION, FEEDBACK_COLLECTION,
)
from appwrite.exception import AppwriteException
from appwrite.enums.index_type import IndexType

RETRIES = 12
DELAY_SECONDS = 5

# Every collection that used a `store` relationship attribute and needs
# a plain `store_id` string field instead.
COLLECTIONS = [
    FLOWS_COLLECTION,
    REQUEST_LOGS_COLLECTION,
    CUSTOMIZATIONS_COLLECTION,
    DASHBOARD_USERS_COLLECTION,
    FEATURES_COLLECTION,
    STORE_INFO_COLLECTION,
    FAQS_COLLECTION,
    FEEDBACK_COLLECTION,
]


def _run(step_description, fn, *args, **kwargs):
    print(step_description)
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except AppwriteException as e:
            message = str(e).lower()
            if getattr(e, "code", None) == 409 or "already exists" in message:
                print("  -> already exists, skipped")
                return None
            if "not yet available" in message or "processing" in message:
                attempt += 1
                if attempt > RETRIES:
                    print(f"  -> FAILED (still not available after {attempt - 1} retries): {e}")
                    raise
                print(f"  -> not ready yet, waiting {DELAY_SECONDS}s and retrying ({attempt}/{RETRIES})...")
                time.sleep(DELAY_SECONDS)
                continue
            print(f"  -> FAILED: {e}")
            raise


def _resolve_old_store_ref(doc: dict) -> str | None:
    """The old `store` relationship field returns either a nested dict
    (the related store document) or just its ID string, depending on
    Appwrite's relationship config — handle both."""
    ref = doc.get("store")
    if isinstance(ref, dict):
        return ref.get("$id")
    if isinstance(ref, str) and ref:
        return ref
    return None


def add_store_id_field(collection_id: str):
    _run(f"attribute 'store_id' on {collection_id}...",
         databases.create_string_attribute, DATABASE_ID, collection_id, "store_id", size=64, required=False)
    _run(f"index 'idx_store_id' on {collection_id}...",
         databases.create_index, DATABASE_ID, collection_id, key="idx_store_id", type=IndexType.KEY, attributes=["store_id"])


def backfill_collection(collection_id: str):
    print(f"Backfilling {collection_id}...")
    offset = 0
    page_size = 100
    updated, skipped = 0, 0
    while True:
        result = databases.list_documents(DATABASE_ID, collection_id, queries=[])
        docs = result["documents"][offset:offset + page_size]
        if not docs:
            break
        for doc in docs:
            if doc.get("store_id"):
                skipped += 1
                continue
            resolved = _resolve_old_store_ref(doc)
            if not resolved:
                print(f"  -> document {doc['$id']} has no resolvable old 'store' relationship — skipped, check it manually")
                continue
            databases.update_document(DATABASE_ID, collection_id, doc["$id"], data={"store_id": resolved})
            updated += 1
        offset += page_size
        if offset >= result["total"]:
            break
    print(f"  -> {updated} updated, {skipped} already had store_id")


def migrate():
    for collection_id in COLLECTIONS:
        add_store_id_field(collection_id)

    print("\nWaiting a few seconds for the new attributes/indexes to finish indexing before backfilling...")
    time.sleep(8)

    for collection_id in COLLECTIONS:
        backfill_collection(collection_id)

    print(
        "\nDone. The old 'store' relationship attributes are still there (unused,\n"
        "harmless) — remove them by hand in the console later if you want to\n"
        "tidy up, but it's not required for the fix to work.\n\n"
        "IMPORTANT: also check for pre-existing DUPLICATE documents created\n"
        "while this bug was active (e.g. multiple agent_customizations rows\n"
        "for the same store) — this script backfills store_id onto all of\n"
        "them, but doesn't delete duplicates. Sort by $createdAt in the\n"
        "console and keep whichever one has your real saved data, delete\n"
        "the rest."
    )


if __name__ == "__main__":
    migrate()
