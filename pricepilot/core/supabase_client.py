"""
Client Supabase centralizzato per PricePilot.

Usa solo variabili ambiente:
- SUPABASE_URL
- SUPABASE_ANON_KEY

Se una delle due manca, l'app resta in modalita locale senza rompere dashboard,
landing o test.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger("pricepilot.supabase")


def get_supabase_settings() -> tuple[str, str]:
    return (
        os.environ.get("SUPABASE_URL", "").strip(),
        os.environ.get("SUPABASE_ANON_KEY", "").strip(),
    )


def is_supabase_configured() -> bool:
    url, key = get_supabase_settings()
    return bool(url and key)


@lru_cache(maxsize=1)
def get_supabase_client() -> Any | None:
    """Ritorna il client Supabase, oppure None se non configurato/disponibile."""
    url, key = get_supabase_settings()
    if not url or not key:
        return None

    try:
        from supabase import create_client
    except Exception as exc:
        logger.warning("Supabase configurato ma il pacchetto non e disponibile: %s", exc)
        return None

    try:
        return create_client(url, key)
    except Exception as exc:
        logger.warning("Impossibile creare il client Supabase: %s", exc)
        return None


def supabase_available() -> bool:
    return get_supabase_client() is not None
