#!/usr/bin/env python3
"""Indexes a Shopify store's policies and product descriptions into the RAG service. Safe to re-run: unchanged
documents are skipped (no embedding cost) and products/policies removed from Shopify are removed from the index.

  python tools/rag_sync.py my-store.myshopify.com [--token <token>] [--no-products] [--no-policies]

Env:  RAG_SERVICE_URL, RAG_API_KEY            the RAG service
      SHOPIFY_ADMIN_TOKEN                     Admin API token with read_products + read_legal_policies
      SHOPIFY_API_VERSION                     default 2024-10
Prices and stock are deliberately NOT indexed: they change too often. Your backend answers those from Shopify live.
"""
import argparse
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
    # Check current, parent, and root directory .env files
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for rel in [".env", "../.env", "../../.env"]:
        p = os.path.normpath(os.path.join(base_dir, rel))
        if os.path.exists(p):
            load_dotenv(p)
except ImportError:
    pass

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.htmlmd import html_to_markdown  # noqa: E402

RAG_URL = os.environ.get("RAG_SERVICE_URL", "").rstrip("/")
RAG_KEY = os.environ.get("RAG_API_KEY", "")
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

POLICIES_Q = "{ shop { shopPolicies { title body type url } } }"
PRODUCTS_Q = """query($after: String) { products(first: 100, after: $after, query: "status:active") {
  pageInfo { hasNextPage endCursor }
  nodes { handle title descriptionHtml productType vendor tags onlineStoreUrl options { name values } } } }"""


def get_store_token_from_db(shop: str) -> str:
    """Attempt to retrieve the store's access token from Appwrite database."""
    try:
        root_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
        import repository_appwrite as repo
        store_doc = repo.get_store(shop)
        if store_doc and store_doc.get("access_token"):
            return store_doc["access_token"]
    except Exception:
        pass
    return ""


def shopify(shop: str, query: str, variables: dict | None = None, token: str = "", version: str = "") -> dict:
    active_token = token or TOKEN
    active_version = version or VERSION or "2024-10"
    base = os.environ.get("SHOPIFY_BASE_URL", f"https://{shop}")
    url = f"{base}/admin/api/{active_version}/graphql.json"

    r = httpx.post(
        url,
        json={"query": query, "variables": variables or {}},
        headers={"X-Shopify-Access-Token": active_token},
        timeout=60,
    )
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            masked = f"{active_token[:6]}...{active_token[-4:]}" if len(active_token) > 10 else f"'{active_token}'"
            print("\n" + "=" * 65)
            print(f"❌ 401 Unauthorized from Shopify for store: {shop}")
            print(f"   URL: {url}")
            print(f"   Token used: {masked}")
            print(f"   Shopify Response: {r.text}")
            print("\nWhy this happens:")
            print("  1. The access token provided is unrecognized, expired, or was revoked.")
            print("  2. In Shopify, each store has its own unique access token.")
            print("  3. The token lacks required scopes ('read_products', 'read_legal_policies').")
            print("\nHow to fix:")
            print(f"  • Pass the store's token directly: python tools/rag_sync.py {shop} --token <token>")
            print(f"  • Or set SHOPIFY_ADMIN_TOKEN=<token> in your environment or .env file.")
            print(f"  • Or copy the access_token from your Appwrite 'stores' collection for this shop.")
            print("=" * 65 + "\n")
        raise

    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
    return body["data"]


def rag(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, f"{RAG_URL}{path}", headers={"Authorization": f"Bearer {RAG_KEY}"}, timeout=300, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"RAG service {r.status_code}: {r.text[:300]}")
    return r.json()


def policy_docs(shop: str, token: str = "", version: str = "") -> list[dict]:
    docs = []
    data = shopify(shop, POLICIES_Q, token=token, version=version)
    for p in data.get("shop", {}).get("shopPolicies", []):
        ptype = (p.get("type") or "").lower()
        text = html_to_markdown(p.get("body") or "")
        if text:
            docs.append({"doc_id": f"policy:{ptype}", "title": p.get("title") or ptype.replace("_", " ").title(),
                         "text": f"# {p.get('title') or ptype}\n\n{text}", "kind": "policy", "policy_type": ptype,
                         "url": p.get("url") or ""})
    return docs


def product_docs(shop: str, token: str = "", version: str = "") -> list[dict]:
    docs, after = [], None
    while True:
        page = shopify(shop, PRODUCTS_Q, {"after": after}, token=token, version=version)["products"]
        for p in page["nodes"]:
            lines = [f"# {p['title']}"]
            facts = [f"Type: {p['productType']}" if p.get("productType") else "",
                     f"Brand: {p['vendor']}" if p.get("vendor") else "",
                     f"Tags: {', '.join(p['tags'])}" if p.get("tags") else ""]
            facts += [f"{o['name']} options: {', '.join(o['values'])}" for o in p.get("options") or []
                      if o["name"] != "Title"]
            lines += [f for f in facts if f]
            body = html_to_markdown(p.get("descriptionHtml") or "")
            if body:
                lines += ["", body]
            docs.append({"doc_id": f"product:{p['handle']}", "title": p["title"], "text": "\n".join(lines),
                         "kind": "product", "url": p.get("onlineStoreUrl") or f"https://{shop}/products/{p['handle']}"})
        if not page["pageInfo"]["hasNextPage"]:
            return docs
        after = page["pageInfo"]["endCursor"]


def push(shop: str, docs: list[dict], prefix: str) -> None:
    summary: dict = {}
    errors = []
    for i in range(0, len(docs), 100):
        out = rag("POST", "/v1/documents/batch", json={"tenant": shop, "documents": docs[i:i + 100]})
        for k, v in out["summary"].items():
            summary[k] = summary.get(k, 0) + v
        errors += [r for r in out["results"] if r["status"] == "error"]
    print(f"{prefix}: {summary}")
    for e in errors[:5]:
        print("   error:", e)
    if errors:
        print(f"   {prefix}: {len(errors)} failed, so nothing was pruned")
        return
    keep = {d["doc_id"] for d in docs}      # remove what no longer exists in Shopify
    for d in rag("GET", "/v1/documents", params={"tenant": shop})["documents"]:
        if d["doc_id"].startswith(prefix) and d["doc_id"] not in keep:
            rag("DELETE", "/v1/documents", json={"tenant": shop, "doc_id": d["doc_id"]})
            print(f"   pruned {d['doc_id']}")


def main() -> None:
    global RAG_URL, RAG_KEY, TOKEN, VERSION
    ap = argparse.ArgumentParser(description="Sync Shopify store policies and products into RAG service")
    ap.add_argument("shop", help="Store domain (e.g. my-store.myshopify.com)")
    ap.add_argument("--token", "-t", help="Shopify Admin API access token (e.g. shpat_... or shpca_...). If omitted, reads SHOPIFY_ADMIN_TOKEN or looks up the store in Appwrite.")
    ap.add_argument("--api-version", default=None, help="Shopify Admin API version (defaults to SHOPIFY_API_VERSION env or 2024-10)")
    ap.add_argument("--rag-url", default=None, help="RAG service URL (or set RAG_SERVICE_URL env)")
    ap.add_argument("--rag-key", default=None, help="RAG service API key (or set RAG_API_KEY env)")
    ap.add_argument("--no-products", action="store_true", help="Skip syncing products")
    ap.add_argument("--no-policies", action="store_true", help="Skip syncing policies")
    a = ap.parse_args()

    shop = a.shop.strip().lower()
    version = a.api_version or os.environ.get("SHOPIFY_API_VERSION") or VERSION or "2024-10"

    rag_url = (a.rag_url or os.environ.get("RAG_SERVICE_URL") or RAG_URL or "").rstrip("/")
    rag_key = (a.rag_key or os.environ.get("RAG_API_KEY") or RAG_KEY or "").strip()
    RAG_URL = rag_url
    RAG_KEY = rag_key

    # Resolve token priority: 1) CLI argument, 2) Environment variable, 3) Appwrite database
    token = (a.token or os.environ.get("SHOPIFY_ADMIN_TOKEN", "")).strip()
    if not token:
        token = get_store_token_from_db(shop)
        if token:
            print(f"✓ Retrieved access token for '{shop}' from Appwrite database.")

    if not RAG_URL or not RAG_KEY:
        sys.exit("Error: RAG_SERVICE_URL and RAG_API_KEY must be set via arguments (--rag-url, --rag-key) or in environment / .env file.")
    if not token:
        sys.exit(f"Error: No Shopify access token found for '{shop}'.\nProvide one via --token <token> or set SHOPIFY_ADMIN_TOKEN.")

    print(f"Syncing Shopify data for {shop} (API version: {version})...")
    if not a.no_policies:
        push(shop, policy_docs(shop, token=token, version=version), "policy:")
    if not a.no_products:
        push(shop, product_docs(shop, token=token, version=version), "product:")
    print("stats:", rag("GET", f"/v1/tenants/{shop}/stats"))


if __name__ == "__main__":
    main()
