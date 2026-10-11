"""
webhooks.py
-----------
Same four Shopify webhooks as before, now backed by Appwrite:

  POST /webhooks/app/uninstalled       - marks the store uninstalled,
                                          drops the (now-revoked) token
  POST /webhooks/customers/data_request - GDPR: export request (stubbed)
  POST /webhooks/customers/redact       - GDPR: delete customer data (stubbed)
  POST /webhooks/shop/redact             - GDPR: delete the whole store's
                                            data — this one now actually
                                            deletes the Appwrite document
"""

import base64
import hashlib
import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

import repository_appwrite as repo
import customer_profiles
import json

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")


async def _verify(request: Request) -> bytes | None:
    body = await request.body()
    sent_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")
    digest = hmac.new(SHOPIFY_API_SECRET.encode(), body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    if not hmac.compare_digest(computed, sent_hmac):
        return None
    return body


@router.post("/app/uninstalled")
async def app_uninstalled(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)

    shop = request.headers.get("X-Shopify-Shop-Domain")
    if shop:
        repo.mark_shop_uninstalled(shop)
    return PlainTextResponse("ok")


@router.post("/customers/data_request")
async def customers_data_request(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    # This app doesn't retain shopper-identifying chat data beyond the
    # order tags/notes written for warranty claims. Add real export
    # logic here if that changes.
    return PlainTextResponse("ok")


@router.post("/customers/redact")
async def customers_redact(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop=request.headers.get("X-Shopify-Shop-Domain") or ""
    try:
        payload=json.loads(body or b"{}")
        customer_id=(payload.get("customer") or {}).get("id")
        if shop and customer_id:
            await customer_profiles.delete_profile(shop,str(customer_id))
    except Exception as e:
        print(f"webhooks: customer redact failed: {e!r}")
    return PlainTextResponse("ok")


@router.post("/shop/redact")
async def shop_redact(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop = request.headers.get("X-Shopify-Shop-Domain")
    if shop:
        repo.delete_shop(shop)
    return PlainTextResponse("ok")


@router.post("/products/update")
@router.post("/products/create")
@router.post("/products/delete")
async def products_webhook(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop = request.headers.get("X-Shopify-Shop-Domain")
    if shop:
        import behavioral_recommender
        behavioral_recommender.invalidate_catalog_cache(shop)
    return PlainTextResponse("ok")


@router.post("/inventory_levels/update")
@router.post("/inventory_items/update")
async def inventory_webhook(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop = request.headers.get("X-Shopify-Shop-Domain")
    if shop:
        import behavioral_recommender
        behavioral_recommender.invalidate_catalog_cache(shop)
    return PlainTextResponse("ok")
