"""Bounded Beds24 authentication/property/calendar diagnostic (GET only)."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import httpx


BASE = "https://beds24.com/api/v2"


def main() -> int:
    refresh = os.environ.get("BEDS24_LUMA_REFRESH_TOKEN", "").strip()
    if not refresh:
        raise SystemExit("Missing BEDS24_LUMA_REFRESH_TOKEN")
    with httpx.Client(base_url=BASE, timeout=20, follow_redirects=False) as client:
        token_reply = client.get("/authentication/token", headers={"refreshToken": refresh})
        token_reply.raise_for_status()
        token_payload = token_reply.json()
        access = token_payload.get("token")
        if not isinstance(access, str) or not access:
            raise SystemExit("Beds24 did not return an access token")
        headers = {"token": access}

        details = client.get("/authentication/details", headers=headers)
        details.raise_for_status()
        detail_payload = details.json()
        token_details = detail_payload.get("token") if isinstance(detail_payload, dict) else {}
        token_details = token_details if isinstance(token_details, dict) else {}
        scopes = sorted(str(item) for item in (token_details.get("scopes") or []))

        properties = client.get("/properties", headers=headers, params={"id": 357389})
        properties.raise_for_status()
        property_payload = properties.json()
        records = property_payload.get("data", []) if isinstance(property_payload, dict) else []
        property_ok = any(isinstance(item, dict) and item.get("id") == 357389 for item in records)

        day = date.today()
        calendar = client.get(
            "/inventory/rooms/calendar",
            headers=headers,
            params={
                "propertyId": 357389,
                "roomId": 736801,
                "startDate": day.isoformat(),
                "endDate": (day + timedelta(days=1)).isoformat(),
                "includePrices": "true",
                "includeNumAvail": "true",
                "includeOverride": "true",
                "includeMinStay": "true",
                "page": 1,
            },
        )
        calendar.raise_for_status()
        calendar_payload = calendar.json()
        calendar_rows = calendar_payload.get("data", []) if isinstance(calendar_payload, dict) else []
        calendar_ok = any(isinstance(item, dict) and item.get("roomId") == 736801 for item in calendar_rows)

    print(json.dumps({
        "refresh_token_authentication": True,
        "scopes": scopes,
        "only_property_id": token_details.get("onlyPropertyId"),
        "property_357389": property_ok,
        "calendar_room_736801": calendar_ok,
        "calendar_date": day.isoformat(),
        "writes_performed": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
