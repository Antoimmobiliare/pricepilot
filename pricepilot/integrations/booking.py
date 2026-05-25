"""
PricePilot - Booking.com Channel Adapter
Integrazione con l'API Booking.com per aggiornamento automatico dei prezzi.

Documentazione API ufficiale:
  https://developers.booking.com/

Configurazione (.env):
  BOOKING_API_KEY=<username:password in base64, o token API>
  BOOKING_HOTEL_ID=<id hotel su Booking.com>
  BOOKING_ROOM_ID=<id camera/appartamento>

NOTE SULL'API BOOKING.COM:
  - L'API Booking.com è riservata ai partner certificati (Connectivity Partners)
  - Per richiedere accesso: https://partner.booking.com/en-us/programs
  - L'aggiornamento prezzi avviene tramite:
      POST /v3/properties/{hotel_id}/availability
  - Per sviluppo/test: i metodi marcati con [STUB] simulano la risposta
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request
from datetime import date, datetime
from typing import Dict, List, Optional

from pricepilot.integrations.base import ChannelAdapter, CalendarDay, PriceUpdateResult

logger = logging.getLogger("pricepilot.integrations.booking")

# Endpoint base Booking.com Connectivity API
_API_BASE = "https://supply-xml.booking.com/hotels/ota"


class BookingAdapter(ChannelAdapter):
    """
    Adapter per Booking.com.

    In modalità STUB (no credenziali reali) simula tutte le operazioni.
    Pronto per go-live: imposta BOOKING_API_KEY e BOOKING_HOTEL_ID in .env.
    """
    platform_name = "booking"

    def __init__(
        self,
        listing_id: str = "",
        api_token:  str = "",
        hotel_id:   str = "",
        room_id:    str = "",
        **kwargs,
    ):
        token    = api_token  or os.environ.get("BOOKING_API_KEY", "")
        hotel    = hotel_id   or os.environ.get("BOOKING_HOTEL_ID", listing_id or "")
        room     = room_id    or os.environ.get("BOOKING_ROOM_ID", "")
        super().__init__(listing_id=hotel, api_token=token, **kwargs)
        self.hotel_id = hotel
        self.room_id  = room
        self._stub    = not bool(token and hotel)

    # ── API calls ─────────────────────────────────────────────────────────────

    def _auth_header(self) -> str:
        """Genera l'header di autenticazione Basic o Bearer."""
        if ":" in self.api_token:
            # username:password → Basic Auth
            encoded = base64.b64encode(self.api_token.encode()).decode()
            return f"Basic {encoded}"
        return f"Bearer {self.api_token}"

    def _call(self, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        url  = f"{_API_BASE}{path}"
        data = json.dumps(body).encode() if body else None
        req  = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Content-Type":  "application/json",
                "Authorization": self._auth_header(),
                "Accept":        "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", errors="replace")
            logger.error(f"Booking.com HTTP {e.code}: {body_txt}")
            return {"error": body_txt, "status_code": e.code}
        except Exception as exc:
            logger.error(f"Booking.com API error: {exc}")
            return {"error": str(exc)}

    # ── Implementazione astratti ──────────────────────────────────────────────

    def update_price(
        self,
        new_price:   float,
        target_date: date,
        min_nights:  int = 1,
    ) -> PriceUpdateResult:
        """
        Aggiorna il prezzo per la data target su Booking.com.

        Endpoint reale (partner connectivity):
          POST /v3/properties/{hotel_id}/availability
          Body: OTA_HotelAvailNotifRQ (XML o JSON a seconda del livello API)
        """
        if self._stub:
            logger.info(
                f"[BOOKING STUB] UPDATE PRICE | hotel={self.hotel_id or 'N/A'} | "
                f"room={self.room_id or 'N/A'} | "
                f"date={target_date.isoformat()} | new_price=€{new_price:.2f}"
            )
            logger.info(
                "Per attivare l'integrazione reale imposta in .env:\n"
                "  BOOKING_API_KEY=<api_key>\n"
                "  BOOKING_HOTEL_ID=<hotel_id>\n"
                "  BOOKING_ROOM_ID=<room_id>"
            )
            return PriceUpdateResult(
                ok         = True,   # Stub: simula successo
                platform   = self.platform_name,
                listing_id = self.hotel_id or "stub",
                new_price  = new_price,
                raw        = {
                    "stub": True,
                    "date": target_date.isoformat(),
                    "room": self.room_id,
                },
            )

        # ── CHIAMATA REALE ────────────────────────────────────────────────────
        # Booking.com usa Rate Plans per aggiornare i prezzi
        payload = {
            "hotel_id":  self.hotel_id,
            "room_id":   self.room_id,
            "rates": [{
                "date":          target_date.isoformat(),
                "price":         new_price,
                "min_occupancy": 1,
                "min_nights":    min_nights,
            }],
        }
        result = self._call("POST", f"/v3/properties/{self.hotel_id}/rates", payload)

        ok    = "error" not in result
        error = result.get("error") if not ok else None
        if ok:
            logger.info(f"[BOOKING] Prezzo aggiornato: hotel={self.hotel_id} €{new_price:.2f}")
        else:
            logger.error(f"[BOOKING] Aggiornamento fallito: {error}")

        return PriceUpdateResult(
            ok=ok, platform=self.platform_name,
            listing_id=self.hotel_id, new_price=new_price,
            error=error, raw=result,
        )

    def get_current_price(self, target_date: Optional[date] = None) -> Optional[float]:
        """Legge il prezzo corrente da Booking.com."""
        if self._stub:
            logger.info(f"[BOOKING STUB] GET PRICE | hotel={self.hotel_id or 'N/A'}")
            return None

        d = target_date or date.today()
        path   = f"/v3/properties/{self.hotel_id}/availability"
        params = f"?room_id={self.room_id}&date={d.isoformat()}"
        result = self._call("GET", path + params)
        try:
            return float(result["rates"][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def get_calendar(self, date_from: date, date_to: date) -> List[CalendarDay]:
        """Restituisce la disponibilità Booking.com per il periodo."""
        if self._stub:
            logger.info(f"[BOOKING STUB] GET CALENDAR | {date_from} → {date_to}")
            return []

        path   = f"/v3/properties/{self.hotel_id}/availability"
        params = (
            f"?room_id={self.room_id}"
            f"&date_from={date_from.isoformat()}"
            f"&date_to={date_to.isoformat()}"
        )
        result = self._call("GET", path + params)

        days = []
        for day in result.get("availability", []):
            days.append(CalendarDay(
                date_str   = day.get("date", ""),
                price      = float(day.get("price", 0)),
                available  = day.get("available", True),
                min_nights = day.get("min_nights", 1),
                raw        = day,
            ))
        return days

    def is_connected(self) -> bool:
        return bool(self.api_token and self.hotel_id)
