"""Selezione esplicita del database operativo di PricePilot.

SQLite resta utile per sviluppo, test e demo locale. In un ambiente distribuito
(Streamlit, API, worker Telegram) la sorgente dati deve invece essere una sola:
Supabase. Il backend cloud non fa fallback silenziosi su SQLite, per evitare
che due servizi leggano dati diversi.
"""
from __future__ import annotations

import os

from pricepilot.core.supabase_client import (
    get_supabase_service_role_key,
    is_supabase_configured,
)


class CloudDatabaseUnavailable(RuntimeError):
    """Supabase e richiesto dal runtime ma non e utilizzabile."""


def database_backend() -> str:
    """Restituisce ``sqlite`` o ``supabase``; rifiuta valori ambigui."""
    value = os.getenv("PRICEPILOT_DATABASE_BACKEND", "sqlite").strip().lower()
    if value in {"", "sqlite", "local"}:
        return "sqlite"
    if value in {"supabase", "cloud"}:
        return "supabase"
    raise CloudDatabaseUnavailable(
        "PRICEPILOT_DATABASE_BACKEND non valido. Usa 'sqlite' o 'supabase'."
    )


def is_supabase_primary() -> bool:
    return database_backend() == "supabase"


def require_supabase_primary(*, require_service_role: bool = False) -> None:
    """Valida la configurazione prima di usare il database cloud.

    ``require_service_role`` e per i processi server-side PricePilot. La chiave
    non viene mai inviata al browser.
    """
    if not is_supabase_primary():
        return
    if not is_supabase_configured():
        raise CloudDatabaseUnavailable(
            "Database cloud attivo ma SUPABASE_URL o SUPABASE_ANON_KEY mancano."
        )
    if require_service_role and not get_supabase_service_role_key():
        raise CloudDatabaseUnavailable(
            "Database cloud attivo ma SUPABASE_SERVICE_ROLE_KEY manca nel servizio server."
        )


def server_runtime() -> bool:
    """True per API/worker Render; False per dashboard Streamlit interattiva."""
    runtime = os.getenv("PRICEPILOT_RUNTIME", "").strip().lower()
    return runtime in {"api", "worker", "scheduler"}
