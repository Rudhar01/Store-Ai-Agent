"""
tracking_provider.py
---------------------
Fetches a shipment's live location/status from AfterShip's Tracking API
(version 2024-04), given the carrier tracking number Shopify has on file.

Env vars:
  AFTERSHIP_API_KEY   AfterShip API key (app.aftership.com -> Settings -> API Keys)

If AFTERSHIP_API_KEY isn't set, get_live_status() returns None and
shopify_actions.track_order falls back to Shopify's own data.

Fixes vs. the previous version (these were why live tracking never worked):
  1. 2024-04 has NO /trackings/{slug}/{number} route. Single trackings are
     fetched by AfterShip's own id, or via the list endpoint filtered by
     tracking_numbers (used here).
  2. POST /trackings takes a FLAT body ({"tracking_number": ...}); the old
     {"tracking": {...}} wrapper was rejected with a 400.
  3. Estimated delivery now lives in aftership_estimated_delivery_date /
     courier_estimated_delivery_date, not expected_delivery.
  4. Every failure is now logged (status code + body) instead of silently
     returning None, so you can see what AfterShip is actually saying.
"""

import os
import httpx

AFTERSHIP_API_KEY = os.environ.get("AFTERSHIP_API_KEY", "").strip()
AFTERSHIP_BASE_URL = "https://api.aftership.com/tracking/2024-04"


def _headers() -> dict:
    return {"as-api-key": AFTERSHIP_API_KEY, "Content-Type": "application/json"}


def _log(msg: str) -> None:
    print(f"tracking_provider: {msg}")


def _unwrap(data: dict) -> dict | None:
    """Create/get responses may put the tracking directly under `data` or
    under `data.tracking` depending on endpoint — accept both."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("tracking"), dict):
        return data["tracking"]
    if "tracking_number" in data:
        return data
    return None


async def _get_tracking(tracking_number: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{AFTERSHIP_BASE_URL}/trackings",
            headers=_headers(),
            params={"tracking_numbers": tracking_number},
        )
    if resp.status_code != 200:
        _log(f"GET /trackings -> {resp.status_code} {resp.text[:300]}")
        return None

    trackings = (resp.json().get("data") or {}).get("trackings") or []
    return trackings[0] if trackings else None


async def _create_tracking(tracking_number: str, carrier_slug: str | None = None) -> dict | None:
    payload = {"tracking_number": tracking_number}   # flat body in 2024-04
    if carrier_slug:
        payload["slug"] = carrier_slug

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{AFTERSHIP_BASE_URL}/trackings", headers=_headers(), json=payload)

    if resp.status_code in (200, 201):
        return _unwrap(resp.json().get("data") or {})

    # "Tracking already exists" -> just fetch it.
    if resp.status_code in (400, 409):
        _log(f"POST /trackings -> {resp.status_code} {resp.text[:300]}")
        return await _get_tracking(tracking_number)

    _log(f"POST /trackings -> {resp.status_code} {resp.text[:300]}")
    return None


def _estimated_delivery(tracking: dict) -> str | None:
    for key in ("aftership_estimated_delivery_date", "courier_estimated_delivery_date"):
        obj = tracking.get(key)
        if isinstance(obj, dict):
            val = obj.get("estimated_delivery_date") or obj.get("estimated_delivery_date_min")
            if val:
                return val
    return tracking.get("expected_delivery")  # legacy fallback


def _summarize(tracking: dict) -> dict:
    checkpoints = tracking.get("checkpoints") or []
    eta = _estimated_delivery(tracking)

    if not checkpoints:
        return {
            "status": tracking.get("tag") or "Pending",
            "location": None,
            "message": "Tracking was just requested from the carrier — a live update should be available shortly.",
            "checkpoint_time": None,
            "estimated_delivery": eta,
        }

    latest = max(checkpoints, key=lambda c: c.get("checkpoint_time") or "")
    country = latest.get("country_region_name") or latest.get("country_name")
    location = ", ".join(p for p in [latest.get("city"), latest.get("state"), country] if p) or None

    return {
        "status": tracking.get("tag"),
        "location": location,
        "message": latest.get("message"),
        "checkpoint_time": latest.get("checkpoint_time"),
        "estimated_delivery": eta,
    }


async def get_live_status(tracking_number: str, carrier_slug: str | None = None) -> dict | None:
    if not AFTERSHIP_API_KEY:
        _log("AFTERSHIP_API_KEY is not set in the server process — skipping live tracking")
        return None
    if not (tracking_number or "").strip():
        return None

    try:
        tracking = await _get_tracking(tracking_number)
        if tracking is None:
            tracking = await _create_tracking(tracking_number, carrier_slug)
        if tracking is None:
            return None
        return _summarize(tracking)
    except Exception as e:  # noqa: BLE001
        _log(f"unexpected error: {e!r}")
        return None
