"""
Client Supabase centralizzato per PricePilot.

Usa solo variabili ambiente:
- SUPABASE_URL
- SUPABASE_ANON_KEY
- SUPABASE_SERVICE_ROLE_KEY opzionale, solo server-side

Se una delle due manca, l'app resta in modalita locale senza rompere dashboard,
landing o test.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("pricepilot.supabase")


def get_supabase_settings() -> tuple[str, str]:
    return (
        os.environ.get("SUPABASE_URL", "").strip(),
        os.environ.get("SUPABASE_ANON_KEY", "").strip(),
    )


def get_supabase_service_role_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def is_supabase_configured() -> bool:
    url, key = get_supabase_settings()
    return bool(url and key)


def get_supabase_client(*, use_auth_session: bool = True) -> Any | None:
    """
    Ritorna un client Supabase, oppure None se non configurato/disponibile.

    Il client non viene tenuto in cache: in Streamlit piu utenti condividono lo
    stesso processo Python e un client globale potrebbe conservare la sessione
    sbagliata. Quando disponibile, agganciamo al client il token Supabase della
    sessione Streamlit corrente, cosi le policy RLS basate su auth.uid() possono
    funzionare correttamente.
    """
    url, key = get_supabase_settings()
    if not url or not key:
        return None

    try:
        from supabase import create_client
    except Exception as exc:
        logger.warning("Supabase configurato ma il pacchetto non e disponibile: %s", exc)
        return None

    try:
        client = create_client(url, key)
    except Exception as exc:
        logger.warning("Impossibile creare il client Supabase: %s", exc)
        return None

    if use_auth_session:
        _attach_current_auth_session(client)
    return client


def get_supabase_admin_client() -> Any | None:
    """
    Ritorna un client Supabase con service role, solo per codice server-side.

    Il service role bypassa RLS: non va mai esposto al browser e va usato solo
    dopo che PricePilot ha gia risolto server-side account_id/utente.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = get_supabase_service_role_key()
    if not url or not key:
        return None

    try:
        from supabase import create_client
    except Exception as exc:
        logger.warning("Supabase service role configurato ma pacchetto assente: %s", exc)
        return None

    try:
        return create_client(url, key)
    except Exception as exc:
        logger.warning("Impossibile creare il client Supabase service role: %s", exc)
        return None


def has_supabase_auth_session() -> bool:
    access_token, _ = _streamlit_auth_tokens()
    return bool(access_token)


def get_supabase_account_client(*, allow_service_role: bool = True) -> Any | None:
    """
    Client per tabelle account-scoped.

    Usa prima la sessione Supabase dell'utente, cosi RLS resta il percorso
    principale. Se non c'e sessione ma e configurata una service role server-side,
    usa quella per sync/migrazioni controllate. Non torna mai un client anonimo
    per scrivere o leggere dati tenant-scoped.
    """
    if has_supabase_auth_session():
        return get_supabase_client(use_auth_session=True)
    if allow_service_role:
        return get_supabase_admin_client()
    return None


def supabase_available() -> bool:
    return get_supabase_client(use_auth_session=False) is not None


def _attach_current_auth_session(client: Any) -> None:
    access_token, refresh_token = _streamlit_auth_tokens()
    if not access_token:
        return

    try:
        client.auth.set_session(access_token, refresh_token or "")
        return
    except Exception as exc:
        logger.debug("Supabase set_session non riuscito: %s", exc)

    # Fallback leggero: PostgREST accetta direttamente il bearer token.
    try:
        client.postgrest.auth(access_token)
    except Exception as exc:
        logger.debug("Supabase postgrest auth fallback non riuscito: %s", exc)


def _streamlit_auth_tokens() -> tuple[str, str]:
    try:
        import streamlit as st
    except Exception:
        return "", ""

    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        try:
            ctx = get_script_run_ctx(suppress_warning=True)
        except TypeError:
            ctx = get_script_run_ctx()
        if ctx is None:
            return "", ""
    except Exception:
        return "", ""

    try:
        session = st.session_state.get("pp_auth_session")
    except Exception:
        return "", ""

    if not session:
        return "", ""
    return (
        str(getattr(session, "access_token", "") or ""),
        str(getattr(session, "refresh_token", "") or ""),
    )
