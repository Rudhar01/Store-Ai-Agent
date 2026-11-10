"""
test_appwrite_connection.py
----------------------------
Run this script to verify that your Appwrite environment variables are
correct and that the app can actually READ and WRITE to your database.

Usage:
    python test_appwrite_connection.py

It will print a clear PASS / FAIL for each check and tell you exactly
what to fix if something is wrong.
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

print("\n" + "=" * 60)
print("  Appwrite Connection Diagnostic")
print("=" * 60)

# ── 1. Env vars ──────────────────────────────────────────────
REQUIRED = {
    "APPWRITE_ENDPOINT":    "Appwrite API endpoint URL",
    "APPWRITE_PROJECT_ID":  "Appwrite Project ID",
    "APPWRITE_API_KEY":     "Appwrite server-side API key",
    "APPWRITE_DATABASE_ID": "Appwrite Database ID",
}
missing = []
for var, desc in REQUIRED.items():
    val = os.environ.get(var, "").strip()
    if val:
        print(f"  ✅  {var} = {val[:30]}{'...' if len(val) > 30 else ''}")
    else:
        print(f"  ❌  {var}  — MISSING  ({desc})")
        missing.append(var)

if missing:
    print("\n  STOP: Set the missing env vars above and re-run.\n")
    sys.exit(1)

# ── 2. SDK import ────────────────────────────────────────────
print("\n--- Appwrite SDK ---")
try:
    from appwrite.client import Client
    from appwrite.services.databases import Databases
    print("  ✅  appwrite SDK is installed")
except ImportError:
    print("  ❌  appwrite SDK NOT installed — run: pip install appwrite")
    sys.exit(1)

# ── 3. Client connection ─────────────────────────────────────
print("\n--- Client connection ---")
try:
    client = Client()
    client.set_endpoint(os.environ["APPWRITE_ENDPOINT"])
    client.set_project(os.environ["APPWRITE_PROJECT_ID"])
    client.set_key(os.environ["APPWRITE_API_KEY"])
    db = Databases(client)
    print("  ✅  Appwrite client created")
except Exception as e:
    print(f"  ❌  Failed to create client: {e!r}")
    sys.exit(1)

# ── 4. Database reachability ─────────────────────────────────
print("\n--- Database reachability ---")
DATABASE_ID = os.environ["APPWRITE_DATABASE_ID"]
try:
    result = db.get_database(DATABASE_ID)
    db_name = result.get("name") if isinstance(result, dict) else getattr(result, "name", str(result))
    print(f"  ✅  Database found: '{db_name}' (id={DATABASE_ID!r})")
except Exception as e:
    err = str(e)
    print(f"  ❌  Cannot reach database: {err}")
    if "401" in err or "unauthorized" in err.lower():
        print("       → Your APPWRITE_API_KEY is wrong or missing 'databases.read' scope.")
    elif "404" in err or "not found" in err.lower():
        print("       → APPWRITE_DATABASE_ID doesn't match any database in your project.")
    sys.exit(1)

# ── 5. Required collections ──────────────────────────────────
print("\n--- Collections ---")
COLLECTIONS = [
    ("stores",              "Stores"),
    ("dashboard_users",     "Dashboard Users"),
    ("agent_customizations","Agent Customizations"),
    ("features",            "Features"),
    ("store_info",          "Store Info"),
    ("faqs",                "FAQs"),
    ("feedback",            "Feedback"),
]
all_ok = True
for col_id, col_name in COLLECTIONS:
    try:
        result = db.get_collection(DATABASE_ID, col_id)
        print(f"  ✅  Collection '{col_id}' exists")
    except Exception as e:
        err = str(e)
        if "404" in err or "not found" in err.lower():
            print(f"  ❌  Collection '{col_id}' NOT FOUND — run setup_collections.py")
        else:
            print(f"  ⚠️   Collection '{col_id}' error: {err}")
        all_ok = False

# ── 6. Write test ────────────────────────────────────────────
print("\n--- Write test (creates then deletes a test document) ---")
from appwrite.id import ID
TEST_DOC_ID = "diagnostic_write_test"
try:
    # Try to delete any leftover test doc first
    try:
        db.delete_document(DATABASE_ID, "stores", TEST_DOC_ID)
    except Exception:
        pass

    created = db.create_document(
        DATABASE_ID, "stores", TEST_DOC_ID,
        data={
            "shop_domain": "__diagnostic_test__.myshopify.com",
            "access_token": "test",
            "scopes": "",
            "uninstalled": True,
        },
    )
    doc_id = created.get("$id") if isinstance(created, dict) else getattr(created, "$id", "?")
    print(f"  ✅  Write succeeded (doc id={doc_id!r})")

    db.delete_document(DATABASE_ID, "stores", TEST_DOC_ID)
    print(f"  ✅  Cleanup succeeded")

except Exception as e:
    err = str(e)
    print(f"  ❌  Write failed: {err}")
    if "401" in err or "unauthorized" in err.lower():
        print("       → API key is missing 'databases.write' scope.")
    elif "attribute" in err.lower():
        print("       → 'stores' collection schema mismatch — run setup_collections.py")
    all_ok = False

# ── Summary ──────────────────────────────────────────────────
print("\n" + "=" * 60)
if all_ok:
    print("  🎉  ALL CHECKS PASSED — Appwrite is correctly configured!")
    print("      App installs WILL be saved to your database.")
else:
    print("  ⚠️   SOME CHECKS FAILED — fix the issues above.")
    print("      App installs will NOT be saved until these are resolved.")
print("=" * 60 + "\n")
