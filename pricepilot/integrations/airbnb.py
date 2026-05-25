"""
PricePilot - Airbnb Channel Adapter
Integrazione con l'API Airbnb per aggiornamento automatico dei prezzi.

Documentazione API ufficiale:
  https://developers.airbnb.com/docs/

Configurazione (.env):
  AIRBNB_API_TOKEN=<il tuo token OAuth2>
  AIRBNB_LISTING_ID=<id numerico del listing>

NOTE SULL'API AIRBNB:
  - Airbnb richiede un accesso partner (Channel Manager API)
  - Per account standard usa iCal export/import o tool di terze parti
    come PriceLabs, Wheelhouse, o Beyond
  - L'endpoint pricing è disponibile solo per partner certificati:
    POST /v2/listings/{id}/pricing_settings
  - Per sviluppo/test: i metodi marcati con [STUB] simulano la risposta
    senza effettuare chiamate reali
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date, datetime
from typing import Dict, List, Optional

from pricepilot.integrations.base import ChannelAdapter, CalendarDay, PriceUpdateResult

logger = logging.getLogger("pricepilot.integrations.airbnb")

# Endpoint base dell'API Airbnb (v2 / partner)
_API_BASE = "https://api.airbnb.com/v2"


class AirbnbAdapter(ChannelAdapter):
    """
    Adapter per Airbnb.

    In modalità STUB (no token reale) simula tutte le operazioni e
    logga cosa farebbe in produzione. Pronto per il go-live:
    basta impostare AIRBNB_API_TOKEN e AIRBNB_LISTING_ID in .env.
    """
    platform_name = "airbnb"

    def __init__(self, listing_id: str = "", api_token: str = "", **kwargs):
        # Legge da env se non passato esplicitamente
        token      = api_token  or os.environ.get("AIRBNB_API_TOKEN", "")
        listing    = listing_id or os.environ.get("AIRBNB_LISTING_ID", "")
        super().__init__(listing_id=listing, api_token=token, **kwargs)
        self._stub = not bool(token and listing)

    # ── API calls (private) ───────────────────────────────────────────────────

    def _call(self, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        """Esegue una chiamata autenticata all'API Airbnb."""
        url  = f"{_API_BASE}{path}"
        data = json.dumps(body).encode() if body else None
        req  = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type":  "application/json",
                "X-Airbnb-OAuth-Token": self.api_token,
                "Accept":        "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", errors="replace")
            logger.error(f"Airbnb HTTP {e.code}: {body_txt}")
            return {"error": body_txt, "status_code": e.code}
        except Exception as exc:
            logger.error(f"Airbnb API error: {exc}")
            return {"error": str(exc)}

    # ── Implementazione astratti ──────────────────────────────────────────────

    def update_price(
        self,
        new_price:   float,
        target_date: date,
        min_nights:  int = 1,
    ) -> PriceUpdateResult:
        """
        Aggiorna il prezzo per la data target su Airbnb.

        Endpoint reale (partner):
          PUT /v2/calendar_operations
          Body: { "operations": [{"listing_id": ..., "date": ..., "daily_price": ...}] }
        """
        if self._stub:
            # ── MODALITÀ STUB ─────────────────────────────────────────────────
            logger.info(
                f"[AIRBNB STUB] UPDATE PRICE | listing={self.listing_id or 'N/A'} | "
                f"date={target_date.isoformat()} | new_price=€{new_price:.2f}"
            )
            logger.info(
                "Per attivare l'integrazione reale imposta in .env:\n"
                "  AIRBNB_API_TOKEN=<token>\n"
                "  AIRBNB_LISTING_ID=<listing_id>"
            )
            return PriceUpdateResult(
                ok         = True,   # Stub: simula successo
                platform   = self.platform_name,
                listing_id = self.listing_id or "stub",
                new_price  = new_price,
                raw        = {"stub": True, "date": target_date.isoformat()},
            )

        # ── CHIAMATA REALE ────────────────────────────────────────────────────
        payload = {
            "operations": [{
                "listing_id":  self.listing_id,
                "date":        target_date.isoformat(),
                "daily_price": int(new_price * 100),  # Airbnb usa centesimi
                "min_nights":  min_nights,
                "available":   True,
            }]
        }
        result = self._call("PUT", "/calendar_operations", payload)

        ok    = "error" not in result
        error = result.get("error") if not ok else None
        if ok:
            logger.info(f"[AIRBNB] Prezzo aggiornato: listing={self.listing_id} €{new_price:.2f}")
        else:
            logger.error(f"[AIRBNB] Aggiornamento fallito: {error}")

        return PriceUpdateResult(
            ok=ok, platform=self.platform_name,
            listing_id=self.listing_id, new_price=new_price,
            error=error, raw=result,
        )

    def get_current_price(self, target_date: Optional[date] = None) -> Optional[float]:
        """Legge il prezzo corrente dal listing Airbnb."""
        if self._stub:
            logger.info(f"[AIRBNB STUB] GET PRICE | listing={self.listing_id or 'N/A'}")
            return None  # Stub: non ha un prezzo reale

        d = target_date or date.today()
        path   = f"/listings/{self.listing_id}/pricing_settings"
        result = self._call("GET", path)
        try:
            return float(result["pricing_settings"]["default_daily_price"]) / 100
        except (KeyError, TypeError, ValueError):
            return None

    def get_calendar(self, date_from: date, date_to: date) -> List[CalendarDay]:
        """Restituisce il calendario Airbnb per il periodo."""
        if self._stub:
            logger.info(
                f"[AIRBNB STUB] GET CALENDAR | {date_from} → {date_to}"
            )
            return []

        path   = f"/listings/{self.listing_id}/calendar"
        params = f"?start={date_from.isoformat()}&end={date_to.isoformat()}"
        result = self._call("GET", path + params)

        days = []
        for day in result.get("calendar_days", []):
            days.append(CalendarDay(
                date_str   = day.get("date", ""),
                price      = float(day.get("price", {}).get("local_price", 0)) / 100,
                available  = day.get("available", True),
                min_nights = day.get("min_nights", 1),
                raw        = day,
            ))
        return days

    def is_connected(self) -> bool:
        return bool(self.api_token and self.listing_id)
