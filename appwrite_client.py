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

class NormalizedDocument(dict):
    """Subclass of dict that allows both dict access (d['$id'], d['shop_domain'])
    and attribute access (d.id, d.shop_domain), mapping $id <-> id seamlessly."""
    def __getattr__(self, name):
        if name in self:
            return self[name]
        if name == "id" and "$id" in self:
            return self["$id"]
        raise AttributeError(f"'NormalizedDocument' has no attribute '{name}'")


class NormalizedDocumentList(dict):
    """Subclass of dict that allows both dict access (res['documents'], res['total'])
    and attribute access (res.documents, res.total)."""
    @property
    def documents(self):
        return self.get("documents", [])

    @property
    def total(self):
        return self.get("total", 0)


def _normalize_doc(doc):
    if doc is None:
        return None
    if isinstance(doc, NormalizedDocument):
        return doc
    if isinstance(doc, dict):
        d = dict(doc)
    elif hasattr(doc, "to_dict"):
        d = doc.to_dict()
    elif hasattr(doc, "model_dump"):
        d = doc.model_dump()
    elif hasattr(doc, "__dict__"):
        d = getattr(doc, "__dict__", {})
    else:
        try:
            d = dict(doc)
        except Exception:
            return doc

    data_field = d.get("data")
    if isinstance(data_field, dict):
        d = {**data_field, **d}
    return NormalizedDocument(d)


def _normalize_list(res):
    if res is None:
        return NormalizedDocumentList({"total": 0, "documents": []})
    if isinstance(res, NormalizedDocumentList):
        return res
    if isinstance(res, dict):
        raw_docs = res.get("documents", [])
        total = res.get("total", len(raw_docs))
    elif hasattr(res, "documents"):
        raw_docs = res.documents
        total = getattr(res, "total", len(raw_docs))
    elif hasattr(res, "to_dict"):
        d = res.to_dict()
        raw_docs = d.get("documents", [])
        total = d.get("total", len(raw_docs))
    else:
        raw_docs = getattr(res, "documents", [])
        total = getattr(res, "total", len(raw_docs))

    norm_docs = [_normalize_doc(d) for d in raw_docs if d is not None]
    return NormalizedDocumentList({
        "total": total,
        "documents": norm_docs,
    })


class DatabaseWrapper:
    def __init__(self, target):
        self._target = target

    def list_documents(self, *args, **kwargs):
        res = self._target.list_documents(*args, **kwargs)
        return _normalize_list(res)

    def get_document(self, *args, **kwargs):
        res = self._target.get_document(*args, **kwargs)
        return _normalize_doc(res)

    def create_document(self, *args, **kwargs):
        res = self._target.create_document(*args, **kwargs)
        return _normalize_doc(res)

    def update_document(self, *args, **kwargs):
        res = self._target.update_document(*args, **kwargs)
        return _normalize_doc(res)

    def delete_document(self, *args, **kwargs):
        return self._target.delete_document(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._target, name)


try:
    from appwrite.client import Client
    from appwrite.services.databases import Databases
    from appwrite.services.storage import Storage

    client = Client()
    client.set_endpoint(os.environ.get("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))
    client.set_project(os.environ.get("APPWRITE_PROJECT_ID", ""))
    client.set_key(os.environ.get("APPWRITE_API_KEY", ""))

    databases = DatabaseWrapper(Databases(client))
    storage = Storage(client)
except ImportError:
    class MockAppwriteService:
        def __getattr__(self, name):
            return lambda *args, **kwargs: NormalizedDocumentList({"documents": [], "total": 0})
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
