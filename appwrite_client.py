"""
appwrite_client.py
-------------------
Appwrite connection setup — the equivalent of your database.py, but
there's no engine/session lifecycle to manage. You configure the client
once, and every request just calls the Databases service directly.

Required environment variables (from your Appwrite Project Settings):
  APPWRITE_ENDPOINT     - e.g. "https://cloud.appwrite.io/v1"
  APPWRITE_PROJECT_ID   - your Project ID
  APPWRITE_API_KEY      - a server-side API key with databases.* scopes
  APPWRITE_DATABASE_ID  - the Database ID you created inside the project
"""

import os

try:
    from appwrite.client import Client
    from appwrite.services.databases import Databases
    from appwrite.services.storage import Storage

    client = Client()
    client.set_endpoint(os.environ.get("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))
    client.set_project(os.environ.get("APPWRITE_PROJECT_ID", ""))
    client.set_key(os.environ.get("APPWRITE_API_KEY", ""))

    databases = Databases(client)
    storage = Storage(client)
except ImportError:
    class MockAppwriteService:
        def __getattr__(self, name):
            return lambda *args, **kwargs: {"documents": [], "total": 0}
    client = None
    databases = MockAppwriteService()
    storage = MockAppwriteService()

DATABASE_ID = os.environ.get("APPWRITE_DATABASE_ID", "")

# Needed to build public file-view URLs for anything uploaded to Storage
# (see WIDGET_ICONS_BUCKET below) — same endpoint/project the client above
# already connects with.
APPWRITE_ENDPOINT = os.environ.get("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")
APPWRITE_PROJECT_ID = os.environ.get("APPWRITE_PROJECT_ID", "")

# Storage bucket for widget customization assets (currently: the custom
# chat-icon image). Files here persist independently of the app server,
# unlike Render's local disk which is wiped on every restart/redeploy.
WIDGET_ICONS_BUCKET = os.environ.get("APPWRITE_WIDGET_ICONS_BUCKET", "6a97b25b002865c5b1b3")

# Collection IDs — set these to whatever you name them in the Appwrite
# console (or create them programmatically, see setup_collections.py)
STORES_COLLECTION = "stores"
FLOWS_COLLECTION = "flows"
REQUEST_LOGS_COLLECTION = "request_logs"
CUSTOMIZATIONS_COLLECTION = "agent_customizations"
DASHBOARD_USERS_COLLECTION = "dashboard_users"

# Added for the rich "RenderLink" dashboard (see setup_collections.py)
FEATURES_COLLECTION = "features"
STORE_INFO_COLLECTION = "store_info"
FAQS_COLLECTION = "faqs"
FEEDBACK_COLLECTION = "feedback"
