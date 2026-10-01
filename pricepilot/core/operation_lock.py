"""Cross-process lease for one account/property/date pricing calculation."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import secrets

from pricepilot.core import database as db
from pricepilot.core.data_backend import CloudDatabaseUnavailable, is_supabase_primary
from pricepilot.core.supabase_client import get_supabase_admin_client
from pricepilot.core.data_quality import DataUnavailable


@contextmanager
def pricing_date_lease(account_id: int, property_id: int, date_str: str, ttl_seconds: int = 300, deadline=None):
    if min(account_id, property_id) < 1 or not 30 <= ttl_seconds <= 1800:
        raise ValueError("Lease pricing non valida.")
    token = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    acquired = _acquire(account_id, property_id, date_str, token, expires, deadline=deadline)
    if not acquired:
        raise DataUnavailable("Analisi della stessa data già in corso; attendere il ciclo attivo.")
    try:
        yield
    finally:
        _release(account_id, property_id, date_str, token)


def _acquire(account_id, property_id, date_str, token, expires, deadline=None):
    import time
    if deadline is not None and time.monotonic() >= deadline:
        raise CloudDatabaseUnavailable("Lock cloud non disponibile entro il limite del ciclo.")
    if is_supabase_primary():
        try:
            # The lock RPC is deliberately executable only by service_role.
            # Account-scoped reads still use the authenticated tenant client;
            # coordination must use the server-side admin client instead.
            client = get_supabase_admin_client()
            if client is None:
                raise CloudDatabaseUnavailable(
                    "Lock cloud non disponibile: SUPABASE_SERVICE_ROLE_KEY non rilevata nell'ambiente server."
                )
            response = client.rpc("acquire_pricepilot_pricing_lock", {
                "p_account_id": account_id, "p_property_id": property_id,
                "p_target_date": date_str, "p_owner_token": token,
                "p_expires_at": expires.isoformat(),
            }).execute()
            return bool(response.data)
        except CloudDatabaseUnavailable:
            raise
        except Exception as exc:
            # Keep the operation log useful without copying provider bodies,
            # headers, tokens, or connection details into tenant-visible data.
            error_kind = type(exc).__name__
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            suffix = f" HTTP {status}" if isinstance(status, (int, str)) and str(status)[:1].isdigit() else ""
            raise CloudDatabaseUnavailable(
                f"Lock cloud RPC rifiutata ({error_kind}{suffix}); verificare funzione e permessi Supabase."
            ) from None
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS pricing_date_locks (
            account_id INTEGER NOT NULL, property_id INTEGER NOT NULL,
            target_date TEXT NOT NULL, owner_token TEXT NOT NULL, expires_at TEXT NOT NULL,
            PRIMARY KEY(account_id,property_id,target_date))""")
        conn.execute("DELETE FROM pricing_date_locks WHERE expires_at < ?", (now,))
        try:
            conn.execute("INSERT INTO pricing_date_locks VALUES (?,?,?,?,?)",
                (account_id, property_id, date_str, token, expires.isoformat()))
            return True
        except Exception as exc:
            import sqlite3
            if isinstance(exc, sqlite3.IntegrityError):
                return False
            raise


def _release(account_id, property_id, date_str, token):
    if is_supabase_primary():
        try:
            client = get_supabase_admin_client()
            if client is None:
                return
            client.rpc("release_pricepilot_pricing_lock", {
                "p_account_id": account_id, "p_property_id": property_id,
                "p_target_date": date_str, "p_owner_token": token,
            }).execute()
            return
        except Exception:
            # Lease expires server-side; do not hide the completed pricing result.
            return
    with db.get_conn() as conn:
        conn.execute("DELETE FROM pricing_date_locks WHERE account_id=? AND property_id=? AND target_date=? AND owner_token=?",
            (account_id, property_id, date_str, token))
