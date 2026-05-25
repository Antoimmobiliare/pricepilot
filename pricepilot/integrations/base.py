"""
PricePilot - Base Channel Adapter
Interfaccia astratta per le integrazioni con piattaforme di affitto (Airbnb, Booking.com, ecc.)

Per aggiungere una nuova piattaforma:
  1. Crea un file in pricepilot/integrations/<platform>.py
  2. Estendi ChannelAdapter
  3. Implementa tutti i metodi astratti
  4. Registra l'adapter in channel_manager.py → ADAPTER_REGISTRY
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pricepilot.integrations")


# ─── Data models ─────────────────────────────────────────────────────────────

class PriceUpdateResult:
    """Risultato di un aggiornamento prezzo sul listing."""
    __slots__ = ("ok", "platform", "listing_id", "new_price", "applied_at", "error", "raw")

    def __init__(
        self,
        ok:         bool,
        platform:   str,
        listing_id: str,
        new_price:  float,
        error:      Optional[str] = None,
        raw:        Optional[Dict] = None,
    ):
        self.ok         = ok
        self.platform   = platform
        self.listing_id = listing_id
        self.new_price  = new_price
        self.applied_at = datetime.utcnow().isoformat()
        self.error      = error
        self.raw        = raw or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok":         self.ok,
            "platform":   self.platform,
            "listing_id": self.listing_id,
            "new_price":  self.new_price,
            "applied_at": self.applied_at,
            "error":      self.error,
        }

    def __repr__(self):
        status = "✅" if self.ok else "❌"
        return f"<PriceUpdateResult {status} {self.platform}/{self.listing_id} €{self.new_price:.2f}>"


class CalendarDay:
    """Rappresenta un giorno del calendario listing."""
    __slots__ = ("date", "price", "available", "min_nights", "raw")

    def __init__(self, date_str: str, price: float, available: bool = True,
                 min_nights: int = 1, raw: Optional[Dict] = None):
        self.date       = date_str
        self.price      = price
        self.available  = available
        self.min_nights = min_nights
        self.raw        = raw or {}


# ─── Abstract base ────────────────────────────────────────────────────────────

class ChannelAdapter(ABC):
    """
    Interfaccia astratta per integrare una piattaforma di affitto breve.
    Ogni adapter è responsabile di:
      - aggiornare il prezzo sul listing remoto
      - leggere il prezzo corrente
      - restituire il calendario disponibilità
    """

    # Deve essere ridefinito nelle sottoclassi (es. "airbnb", "booking")
    platform_name: str = "unknown"

    def __init__(self, listing_id: str, api_token: str, **kwargs):
        self.listing_id = listing_id
        self.api_token  = api_token
        self.extra      = kwargs
        self._log       = logging.getLogger(f"pricepilot.integrations.{self.platform_name}")

    # ── Metodi obbligatori ───────────────────────────────────────────────────

    @abstractmethod
    def update_price(
        self,
        new_price:  float,
        target_date: date,
        min_nights: int = 1,
    ) -> PriceUpdateResult:
        """
        Aggiorna il prezzo del listing per la data target.

        Args:
            new_price:    Nuovo prezzo per notte (€).
            target_date:  Data per cui aggiornare il prezzo.
            min_nights:   Soggiorno minimo (default 1).

        Returns:
            PriceUpdateResult con ok=True se l'aggiornamento è riuscito.
        """
        ...

    @abstractmethod
    def get_current_price(self, target_date: Optional[date] = None) -> Optional[float]:
        """
        Legge il prezzo corrente del listing per la data target.
        Se target_date è None, restituisce il prezzo base.
        """
        ...

    @abstractmethod
    def get_calendar(
        self,
        date_from: date,
        date_to:   date,
    ) -> List[CalendarDay]:
        """
        Restituisce il calendario del listing per il periodo indicato.
        """
        ...

    # ── Metodi opzionali con default ─────────────────────────────────────────

    def is_connected(self) -> bool:
        """True se le credenziali sono configurate e l'API è raggiungibile."""
        return bool(self.api_token and self.listing_id)

    def get_platform_name(self) -> str:
        return self.platform_name

    def __repr__(self):
        return f"<{self.__class__.__name__} listing={self.listing_id} connected={self.is_connected()}>"
