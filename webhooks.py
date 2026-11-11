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


@router.post("/orders/create")
@router.post("/orders/updated")
async def orders_webhook(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop = request.headers.get("X-Shopify-Shop-Domain") or ""
    try:
        payload = json.loads(body or b"{}")
        cust = payload.get("customer") or {}
        cid = str(cust.get("id") or "").strip()
        email = (cust.get("email") or payload.get("email") or "").strip().lower()
        if shop and (cid or email):
            # Fetch existing profile to append order
            existing = await customer_profiles.get_profile(shop, customer_id=cid, email=email) or {}
            recent_orders = existing.get("recent_orders") or []
            if isinstance(recent_orders, str):
                try:
                    recent_orders = json.loads(recent_orders)
                except Exception:
                    recent_orders = []

            ord_num = str(payload.get("order_number") or payload.get("name") or "").lstrip("#")
            items = [{"name": it.get("name") or it.get("title"), "quantity": it.get("quantity")} for it in payload.get("line_items", [])]
            total_price = str(payload.get("total_price") or "0.00")
            curr = str(payload.get("currency") or "INR")

            # Update or prepend order
            new_order = {
                "order_number": ord_num,
                "order_id": str(payload.get("id") or ""),
                "created_at": payload.get("created_at") or "",
                "fulfillment_status": str(payload.get("fulfillment_status") or "unfulfilled"),
                "financial_status": str(payload.get("financial_status") or "paid"),
                "total_price": total_price,
                "currency": curr,
                "items": items,
            }
            # Remove duplicate order if already in list
            recent_orders = [o for o in recent_orders if str(o.get("order_number")) != ord_num]
            recent_orders.insert(0, new_order)
            recent_orders = recent_orders[:25]

            orders_count = int(existing.get("order_count") or 0) + 1
            await customer_profiles.save_customer_profile(
                shop=shop,
                customer_id=cid or None,
                email=email or None,
                first_name=cust.get("first_name"),
                last_name=cust.get("last_name"),
                phone=cust.get("phone"),
                order_count=orders_count,
                recent_orders=recent_orders,
                currency=curr,
            )
    except Exception as e:
        print(f"webhooks: orders_webhook sync error: {e!r}")
    return PlainTextResponse("ok")


@router.post("/customers/update")
async def customers_update_webhook(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop = request.headers.get("X-Shopify-Shop-Domain") or ""
    try:
        payload = json.loads(body or b"{}")
        cid = str(payload.get("id") or "").strip()
        email = (payload.get("email") or "").strip().lower()
        if shop and (cid or email):
            def_addr = payload.get("default_address") or {}
            addr_str = ", ".join(filter(None, [def_addr.get("address1"), def_addr.get("city"), def_addr.get("province"), def_addr.get("country"), def_addr.get("zip")]))
            await customer_profiles.save_customer_profile(
                shop=shop,
                customer_id=cid or None,
                email=email or None,
                first_name=payload.get("first_name"),
                last_name=payload.get("last_name"),
                phone=payload.get("phone"),
                order_count=payload.get("orders_count"),
                total_spent=payload.get("total_spent"),
                tags=payload.get("tags"),
                currency=payload.get("currency"),
                addresses=addr_str,
            )
    except Exception as e:
        print(f"webhooks: customers_update_webhook sync error: {e!r}")
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
