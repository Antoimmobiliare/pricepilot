"""
PricePilot - Channel Manager
Orchestratore centrale per le integrazioni con le piattaforme di affitto.

Il ChannelManager:
  1. Seleziona l'adapter corretto in base alla piattaforma della proprietà
  2. Invoca l'aggiornamento prezzi sul listing remoto
  3. Salva il risultato nel database (tabella price_updates)
  4. Gestisce i fallback e i log

Aggiungere una nuova piattaforma:
  1. Crea pricepilot/integrations/<platform>.py con la classe adapter
  2. Aggiungi al dizionario ADAPTER_REGISTRY qui sotto
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Dict, Optional

from pricepilot.integrations.base import ChannelAdapter, PriceUpdateResult

logger = logging.getLogger("pricepilot.channel_manager")


# ─── Registry delle piattaforme supportate ───────────────────────────────────
# Formato: "platform_name" → callable che restituisce un adapter istanziato

def _get_adapter_registry() -> Dict[str, type]:
    """Importa gli adapter lazy (evita errori se mancano dipendenze opzionali)."""
    from pricepilot.integrations.airbnb  import AirbnbAdapter
    from pricepilot.integrations.booking import BookingAdapter
    return {
        "airbnb":  AirbnbAdapter,
        "booking": BookingAdapter,
        # Aggiungi qui nuove piattaforme:
        # "vrbo":    VrboAdapter,
        # "direct":  DirectAdapter,
    }


# ─── Channel Manager ─────────────────────────────────────────────────────────

class ChannelManager:
    """
    Orchestratore per aggiornamenti prezzi multi-piattaforma.

    Uso tipico (dal Decision Engine):
        cm = ChannelManager()
        result = cm.update_price(prop, new_price=142.50, target_date=date.today())
        if result.ok:
            print(f"✅ {result.platform}/{result.listing_id} → €{result.new_price}")
    """

    def get_adapter(self, prop: Dict) -> Optional[ChannelAdapter]:
        """
        Restituisce l'adapter corretto per la proprietà.

        Cerca nell'ordine:
          1. platform dal record proprietà (es. "airbnb", "booking")
          2. ADAPTER_REGISTRY
          3. listing_id dalla proprietà (o da env)
          4. api_token dall'env specifico della piattaforma
        """
        platform = prop.get("platform", "airbnb").lower().strip()
        registry = _get_adapter_registry()

        if platform not in registry:
            logger.warning(
                f"Piattaforma '{platform}' non supportata. "
                f"Disponibili: {list(registry.keys())}"
            )
            return None

        AdapterClass = registry[platform]
        listing_id   = prop.get("listing_id", "")
        api_token    = _env_token_for_platform(platform)

        return AdapterClass(listing_id=listing_id, api_token=api_token)

    def update_price(
        self,
        prop:        Dict,
        new_price:   float,
        target_date: date,
        min_nights:  int = 1,
    ) -> PriceUpdateResult:
        """
        Aggiorna il prezzo sul listing della piattaforma associata alla proprietà.

        Se l'adapter non è disponibile o è in modalità STUB, il risultato sarà
        ok=True con raw["stub"]=True (comportamento simulato).

        Args:
            prop:        Record proprietà dal DB (dict con 'platform', 'listing_id', ecc.)
            new_price:   Nuovo prezzo per notte (€).
            target_date: Data per cui aggiornare il prezzo.
            min_nights:  Soggiorno minimo.

        Returns:
            PriceUpdateResult.
        """
        adapter = self.get_adapter(prop)

        if adapter is None:
            platform = prop.get("platform", "unknown")
            logger.info(f"[CHANNEL] Nessun adapter per '{platform}' – skip aggiornamento listing")
            return PriceUpdateResult(
                ok         = False,
                platform   = platform,
                listing_id = prop.get("listing_id", ""),
                new_price  = new_price,
                error      = f"Adapter non disponibile per platform='{platform}'",
            )

        logger.info(
            f"[CHANNEL] Aggiorno prezzo | platform={adapter.platform_name} | "
            f"listing={adapter.listing_id or 'stub'} | €{new_price:.2f} | {target_date}"
        )

        result = adapter.update_price(new_price, target_date, min_nights)

        # Persisti il risultato nel DB (tabella price_updates)
        _persist_update(prop, result, target_date)

        return result

    def is_real_mode(self, prop: Dict) -> bool:
        """
        True se l'adapter per la proprietà è configurato con credenziali reali
        (non è in modalità STUB).
        """
        adapter = self.get_adapter(prop)
        if adapter is None:
            return False
        platform = prop.get("platform", "airbnb").lower()
        token    = _env_token_for_platform(platform)
        listing  = prop.get("listing_id", "")
        return bool(token and listing)

    def get_status(self, prop: Dict) -> Dict:
        """
        Restituisce lo stato dell'integrazione per una proprietà.
        Utile per la dashboard.
        """
        platform   = prop.get("platform", "unknown")
        listing_id = prop.get("listing_id", "")
        token      = _env_token_for_platform(platform)
        is_real    = bool(token and listing_id)
        adapter    = self.get_adapter(prop)
        supported  = adapter is not None

        return {
            "platform":   platform,
            "listing_id": listing_id,
            "supported":  supported,
            "is_real":    is_real,
            "mode":       "🟢 Live" if is_real else ("🟡 Stub" if supported else "🔴 Non supportato"),
            "token_set":  bool(token),
        }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _env_token_for_platform(platform: str) -> str:
    """Mappa platform → variabile d'ambiente del token."""
    env_map = {
        "airbnb":  "AIRBNB_API_TOKEN",
        "booking": "BOOKING_API_KEY",
        "vrbo":    "VRBO_API_KEY",
        "direct":  "",
        "other":   "",
    }
    env_key = env_map.get(platform.lower(), "")
    return os.environ.get(env_key, "") if env_key else ""


def _persist_update(prop: Dict, result: PriceUpdateResult, target_date: date) -> None:
    """Salva l'esito dell'aggiornamento nel database (tabella price_updates)."""
    try:
        from pricepilot.core.database import get_conn
        with get_conn() as conn:
            # Crea tabella se non esiste (migrazione lazy)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_updates (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    property_id INTEGER,
                    platform    TEXT,
                    listing_id  TEXT,
                    target_date TEXT,
                    new_price   REAL,
                    ok          INTEGER,
                    error       TEXT,
                    applied_at  TEXT,
                    is_stub     INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                INSERT INTO price_updates
                    (property_id, platform, listing_id, target_date,
                     new_price, ok, error, applied_at, is_stub)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                prop.get("id"),
                result.platform,
                result.listing_id,
                target_date.isoformat(),
                result.new_price,
                int(result.ok),
                result.error or "",
                result.applied_at,
                int(result.raw.get("stub", False)),
            ))
    except Exception as exc:
        logger.error(f"_persist_update error: {exc}")


# ─── Singleton globale ────────────────────────────────────────────────────────

_channel_manager: Optional[ChannelManager] = None


def get_channel_manager() -> ChannelManager:
    """Restituisce il singleton ChannelManager."""
    global _channel_manager
    if _channel_manager is None:
        _channel_manager = ChannelManager()
    return _channel_manager
