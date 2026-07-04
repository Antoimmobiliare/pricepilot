"""
PricePilot - Smoobu Channel Manager Adapter.

This adapter prepares PricePilot for the real channel-manager flow:
Decision Engine -> Telegram approval -> Smoobu rates update -> OTA sync.

Official docs:
  https://docs.smoobu.com/

Configuration (.env):
  Recommended HMAC auth:
    SMOOBU_API_CONSUMER_KEY=<consumer key from Smoobu>
    SMOOBU_API_CONSUMER_SECRET=<consumer secret from Smoobu>

  Legacy auth, deprecated by Smoobu:
    SMOOBU_LEGACY_API_KEY=<legacy Api-Key>

  Property mapping:
    SMOOBU_APARTMENT_ID=<Smoobu apartment id>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pricepilot.integrations.base import CalendarDay, ChannelAdapter, PriceUpdateResult

logger = logging.getLogger("pricepilot.integrations.smoobu")

_DEFAULT_BASE_URL = "https://login.smoobu.com"


class SmoobuAdapter(ChannelAdapter):
    """Adapter for Smoobu rates and apartments APIs."""

    platform_name = "smoobu"

    def __init__(
        self,
        listing_id: str = "",
        api_token: str = "",
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "",
        **kwargs,
    ):
        legacy_key = (
            api_token
            or os.environ.get("SMOOBU_LEGACY_API_KEY", "")
            or os.environ.get("SMOOBU_API_TOKEN", "")
        )
        hmac_key = (
            api_key
            or os.environ.get("SMOOBU_API_KEY", "")
            or os.environ.get("SMOOBU_API_CONSUMER_KEY", "")
        )
        hmac_secret = (
            api_secret
            or os.environ.get("SMOOBU_API_SECRET", "")
            or os.environ.get("SMOOBU_API_CONSUMER_SECRET", "")
        )
        apartment_id = listing_id or os.environ.get("SMOOBU_APARTMENT_ID", "")
        super().__init__(listing_id=str(apartment_id or ""), api_token=legacy_key, **kwargs)
        self.api_key = hmac_key
        self.api_secret = hmac_secret
        self.base_url = (base_url or os.environ.get("SMOOBU_API_BASE", _DEFAULT_BASE_URL)).rstrip("/")
        self._stub = not bool((self.api_key and self.api_secret) or self.api_token)

    def _json_bytes(self, body: Optional[Dict[str, Any]]) -> bytes:
        if not body:
            return b""
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _query_string(self, query: Optional[Dict[str, Any]]) -> str:
        if not query:
            return ""
        items: List[Tuple[str, Any]] = []
        for key in sorted(query.keys()):
            value = query[key]
            if isinstance(value, (list, tuple)):
                for item in value:
                    items.append((key, item))
            else:
                items.append((key, value))
        return urllib.parse.urlencode(items, doseq=True)

    def _hmac_headers(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
        query_string: str,
    ) -> Dict[str, str]:
        body_bytes = self._json_bytes(body)
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = str(uuid.uuid4())
        canonical = "\n".join(
            [
                method.upper(),
                path,
                query_string,
                timestamp,
                nonce,
                body_hash,
                self.api_key,
            ]
        )
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return {
            "X-API-Key": self.api_key,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _headers(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
        query_string: str,
    ) -> Dict[str, str]:
        if self.api_key and self.api_secret:
            return self._hmac_headers(method, path, body, query_string)
        return {
            "Api-Key": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        if self._stub:
            return {"ok": False, "stub": True, "error": "Smoobu credentials not configured"}

        query_string = self._query_string(query)
        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        data = self._json_bytes(body) if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method.upper(),
            headers=self._headers(method, path, body, query_string),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(text) if text else {}
                return {"ok": True, "status_code": resp.status, "data": parsed}
        except urllib.error.HTTPError as exc:
            body_txt = exc.read().decode("utf-8", errors="replace")
            logger.error("Smoobu HTTP %s: %s", exc.code, body_txt)
            return {
                "ok": False,
                "status_code": exc.code,
                "error": body_txt,
            }
        except Exception as exc:
            logger.error("Smoobu API error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def test_connection(self) -> Dict[str, Any]:
        """Checks credentials by reading the Smoobu apartment list."""
        result = self._call("GET", "/api/apartments")
        if not result.get("ok"):
            return result
        data = result.get("data") or {}
        apartments = data.get("apartments") if isinstance(data, dict) else []
        return {
            "ok": True,
            "apartments_count": len(apartments or []),
            "apartments": apartments or [],
            "raw": data,
        }

    def auth_mode(self) -> str:
        if self.api_key and self.api_secret:
            return "hmac"
        if self.api_token:
            return "legacy_api_key"
        return "not_configured"

    def connection_status(self) -> Dict[str, Any]:
        return {
            "platform": self.platform_name,
            "auth_mode": self.auth_mode(),
            "has_credentials": self.has_credentials(),
            "listing_id": self.listing_id,
            "is_connected": self.is_connected(),
            "base_url": self.base_url,
        }

    def list_apartments(self) -> List[Dict[str, Any]]:
        result = self.test_connection()
        return list(result.get("apartments") or []) if result.get("ok") else []

    def get_rates(self, date_from: date, date_to: date) -> Dict[str, Any]:
        if not self.listing_id:
            return {"ok": False, "error": "SMOOBU_APARTMENT_ID/listing_id missing"}
        return self._call(
            "GET",
            "/api/rates",
            query={
                "apartments[]": [self.listing_id],
                "start_date": date_from.isoformat(),
                "end_date": date_to.isoformat(),
            },
        )

    def update_price(
        self,
        new_price: float,
        target_date: date,
        min_nights: int = 1,
    ) -> PriceUpdateResult:
        if self._stub or not self.listing_id:
            logger.info(
                "[SMOOBU STUB] UPDATE PRICE | apartment=%s | date=%s | price=%.2f",
                self.listing_id or "N/A",
                target_date.isoformat(),
                new_price,
            )
            return PriceUpdateResult(
                ok=True,
                platform=self.platform_name,
                listing_id=self.listing_id or "stub",
                new_price=new_price,
                raw={
                    "stub": True,
                    "provider": "smoobu",
                    "date": target_date.isoformat(),
                    "min_nights": min_nights,
                },
            )

        payload = {
            "apartments": [int(self.listing_id) if str(self.listing_id).isdigit() else self.listing_id],
            "operations": [
                {
                    "dates": [target_date.isoformat()],
                    "daily_price": float(new_price),
                    "min_length_of_stay": int(min_nights),
                }
            ],
        }
        result = self._call("POST", "/api/rates", payload)
        ok = bool(result.get("ok") and (result.get("data") or {}).get("success", True))
        error = None if ok else str(result.get("error") or result.get("data") or "Smoobu update failed")
        return PriceUpdateResult(
            ok=ok,
            platform=self.platform_name,
            listing_id=self.listing_id,
            new_price=new_price,
            error=error,
            raw=result,
        )

    def get_current_price(self, target_date: Optional[date] = None) -> Optional[float]:
        if self._stub or not self.listing_id:
            return None
        d = target_date or date.today()
        result = self.get_rates(d, d)
        if not result.get("ok"):
            return None
        data = (result.get("data") or {}).get("data") or {}
        apartment_rates = data.get(str(self.listing_id)) or data.get(int(self.listing_id)) or {}
        day = apartment_rates.get(d.isoformat()) if isinstance(apartment_rates, dict) else None
        try:
            price = day.get("price") if isinstance(day, dict) else None
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    def get_calendar(self, date_from: date, date_to: date) -> List[CalendarDay]:
        if self._stub or not self.listing_id:
            return []
        result = self.get_rates(date_from, date_to)
        if not result.get("ok"):
            return []
        data = (result.get("data") or {}).get("data") or {}
        apartment_rates = data.get(str(self.listing_id)) or data.get(int(self.listing_id)) or {}
        days: List[CalendarDay] = []
        if isinstance(apartment_rates, dict):
            for date_str, values in sorted(apartment_rates.items()):
                values = values or {}
                try:
                    price = float(values.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                try:
                    min_stay = int(values.get("min_length_of_stay") or 1)
                except (TypeError, ValueError):
                    min_stay = 1
                days.append(
                    CalendarDay(
                        date_str=date_str,
                        price=price,
                        available=bool(values.get("available", 1)),
                        min_nights=min_stay,
                        raw=values,
                    )
                )
        return days

    def is_connected(self) -> bool:
        return bool(((self.api_key and self.api_secret) or self.api_token) and self.listing_id)

    def has_credentials(self) -> bool:
        return bool((self.api_key and self.api_secret) or self.api_token)
