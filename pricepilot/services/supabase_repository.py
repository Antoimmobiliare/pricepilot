"""
Repository Supabase opzionale.

SQLite resta il fallback locale, ma quando SUPABASE_URL e SUPABASE_ANON_KEY sono
presenti PricePilot sincronizza proprieta e regole prezzo su Supabase.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pricepilot.core.database import get_properties, get_property, upsert_property
from pricepilot.core.supabase_client import (
    get_supabase_account_client,
    supabase_available,
)

logger = logging.getLogger("pricepilot.supabase_repository")

ACCOUNTS_TABLE = os.environ.get("PRICEPILOT_SUPABASE_ACCOUNTS_TABLE", "accounts")
ACCOUNT_MEMBERS_TABLE = os.environ.get("PRICEPILOT_SUPABASE_ACCOUNT_MEMBERS_TABLE", "account_members")
PROFILES_TABLE = os.environ.get("PRICEPILOT_SUPABASE_PROFILES_TABLE", "profiles")
USER_CONSENTS_TABLE = os.environ.get("PRICEPILOT_SUPABASE_USER_CONSENTS_TABLE", "user_consents")
PROPERTIES_TABLE = os.environ.get("PRICEPILOT_SUPABASE_PROPERTIES_TABLE", "properties")
PRICING_RULES_TABLE = os.environ.get("PRICEPILOT_SUPABASE_PRICING_RULES_TABLE", "pricing_rules")
_BACKFILLED_ACCOUNTS: set[int] = set()


def is_supabase_db_ready() -> bool:
    return supabase_available()


def has_supabase_write_context() -> bool:
    """True quando possiamo accedere a tabelle RLS account-scoped."""
    return get_supabase_account_client() is not None


def sync_account_to_supabase(account: Dict) -> bool:
    """Crea/aggiorna solo l'account su Supabase, usato da billing/webhook backend."""
    client = get_supabase_account_client()
    account_id = _as_int((account or {}).get("id"))
    if client is None or not account_id:
        return False

    payload = {
        "id": account_id,
        "name": account.get("name") or "La mia attivita",
        "plan": account.get("plan") or "free",
        "billing_status": account.get("billing_status") or "dev",
        "trial_ends_at": account.get("trial_ends_at"),
        "current_period_ends_at": account.get("current_period_ends_at"),
        "stripe_customer_id": account.get("stripe_customer_id") or "",
        "stripe_subscription_id": account.get("stripe_subscription_id") or "",
    }
    result = _safe_execute(
        lambda: client.table(ACCOUNTS_TABLE)
        .upsert(payload, on_conflict="id")
        .execute(),
        default=None,
        action="sync_account_billing",
    )
    return result is not None


def sync_account_membership_to_supabase(
    account: Dict,
    user: Dict,
    supabase_user_id: str,
) -> bool:
    """
    Crea/aggiorna profilo, account e membership su Supabase.

    Serve per far funzionare le policy RLS basate su auth.uid(): prima di
    sincronizzare proprieta o regole prezzo, Supabase deve sapere a quale
    account appartiene l'utente autenticato.
    """
    client = get_supabase_account_client()
    if client is None or not account or not user or not supabase_user_id:
        return False

    account_id = _as_int(account.get("id") or user.get("account_id"))
    if not account_id:
        return False

    email = str(user.get("email") or "").strip().lower()
    role = str(user.get("role") or "owner").strip().lower()
    if role not in {"owner", "manager", "viewer"}:
        role = "owner"

    profile_payload = {
        "id": supabase_user_id,
        "email": email,
        "full_name": user.get("full_name", "") or "",
    }
    account_payload = {
        "id": account_id,
        "name": account.get("name") or "La mia attivita",
        "plan": account.get("plan") or "free",
        "billing_status": account.get("billing_status") or "dev",
        "trial_ends_at": account.get("trial_ends_at"),
        "current_period_ends_at": account.get("current_period_ends_at"),
        "stripe_customer_id": account.get("stripe_customer_id") or "",
        "stripe_subscription_id": account.get("stripe_subscription_id") or "",
        "owner_user_id": supabase_user_id,
    }
    membership_payload = {
        "account_id": account_id,
        "user_id": supabase_user_id,
        "role": role,
    }

    profile = _safe_execute(
        lambda: client.table(PROFILES_TABLE)
        .upsert(profile_payload, on_conflict="id")
        .execute(),
        default=None,
        action="sync_profile",
    )
    remote_account = _safe_execute(
        lambda: client.table(ACCOUNTS_TABLE)
        .upsert(account_payload, on_conflict="id")
        .execute(),
        default=None,
        action="sync_account",
    )
    membership = _safe_execute(
        lambda: client.table(ACCOUNT_MEMBERS_TABLE)
        .upsert(membership_payload, on_conflict="account_id,user_id")
        .execute(),
        default=None,
        action="sync_account_membership",
    )
    return profile is not None and remote_account is not None and membership is not None


def sync_user_consent_to_supabase(
    consent: Dict,
    supabase_user_id: str,
) -> bool:
    """Sincronizza il consenso utente su Supabase quando la sessione auth e valida."""
    client = get_supabase_account_client()
    if client is None or not consent or not supabase_user_id:
        return False

    payload = {
        "account_id": int(consent.get("account_id") or 1),
        "user_id": supabase_user_id,
        "terms_accepted": bool(consent.get("terms_accepted")),
        "privacy_accepted": bool(consent.get("privacy_accepted")),
        "marketing_accepted": bool(consent.get("marketing_accepted")),
        "terms_version": consent.get("terms_version") or "2026-06-23",
        "privacy_version": consent.get("privacy_version") or "2026-06-23",
        "source": consent.get("source") or "signup",
        "accepted_at": consent.get("accepted_at") or datetime.utcnow().isoformat(),
    }
    rows = _safe_execute(
        lambda: client.table(USER_CONSENTS_TABLE)
        .upsert(payload, on_conflict="user_id,terms_version,privacy_version")
        .execute(),
        default=None,
        action="sync_user_consent",
    )
    return rows is not None


def refresh_properties_from_supabase(account_id: int) -> int:
    """Aggiorna SQLite con eventuali proprieta piu recenti presenti su Supabase."""
    client = get_supabase_account_client()
    if client is None:
        return 0

    rows = _safe_execute(
        lambda: client.table(PROPERTIES_TABLE)
        .select("*")
        .eq("account_id", int(account_id))
        .execute(),
        default=[],
        action="fetch_properties",
    )
    if not rows:
        return 0

    refreshed = 0
    for row in rows:
        local_id = _as_int(row.get("local_id") or row.get("id"))
        if not local_id:
            continue
        local = get_property(local_id)
        if local and not _remote_is_newer(row.get("updated_at"), local.get("updated_at")):
            continue
        upsert_property(_row_to_property(row, account_id=account_id, local_id=local_id))
        refreshed += 1
    return refreshed


def backfill_account_properties_to_supabase(
    account_id: int,
    properties: Optional[list[Dict]] = None,
) -> Dict[str, int]:
    """
    Sincronizza una volta le proprieta locali gia esistenti verso Supabase.

    Serve quando un utente collega Supabase dopo aver creato dati in SQLite:
    il normale flusso di creazione sincronizza i nuovi record, ma i record
    precedenti devono essere copiati senza costringere l'utente a ricrearli.
    """
    account_id = int(account_id or 1)
    if get_supabase_account_client() is None:
        return {"properties": 0, "pricing_rules": 0, "skipped": 1}

    if account_id in _BACKFILLED_ACCOUNTS:
        return {"properties": 0, "pricing_rules": 0, "skipped": 1}

    local_props = properties
    if local_props is None:
        local_props = [
            p for p in get_properties()
            if int(p.get("account_id") or 1) == account_id
        ]

    if not local_props:
        _BACKFILLED_ACCOUNTS.add(account_id)
        return {"properties": 0, "pricing_rules": 0, "skipped": 0}

    synced_properties = 0
    synced_rules = 0
    for prop in local_props:
        try:
            if sync_property_to_supabase(prop):
                synced_properties += 1
            if sync_pricing_rule_to_supabase(prop):
                synced_rules += 1
        except Exception as exc:
            logger.warning(
                "Supabase backfill proprieta non riuscito account=%s property=%s: %s",
                account_id,
                prop.get("id"),
                exc,
            )

    if synced_properties == len(local_props) and synced_rules == len(local_props):
        _BACKFILLED_ACCOUNTS.add(account_id)

    return {
        "properties": synced_properties,
        "pricing_rules": synced_rules,
        "skipped": 0,
    }


def sync_property_to_supabase(prop: Dict) -> Optional[Dict]:
    client = get_supabase_account_client()
    if client is None or not prop:
        return None

    payload = _property_payload(prop)
    rows = _safe_execute(
        lambda: client.table(PROPERTIES_TABLE)
        .upsert(payload, on_conflict="account_id,local_id")
        .execute(),
        default=None,
        action="sync_property",
    )
    return rows[0] if isinstance(rows, list) and rows else None


def delete_property_from_supabase(prop: Dict) -> bool:
    client = get_supabase_account_client()
    if client is None or not prop:
        return False

    _safe_execute(
        lambda: client.table(PRICING_RULES_TABLE)
        .delete()
        .eq("account_id", int(prop.get("account_id") or 1))
        .eq("property_local_id", int(prop["id"]))
        .execute(),
        default=None,
        action="delete_pricing_rule",
    )
    _safe_execute(
        lambda: client.table(PROPERTIES_TABLE)
        .delete()
        .eq("account_id", int(prop.get("account_id") or 1))
        .eq("local_id", int(prop["id"]))
        .execute(),
        default=None,
        action="delete_property",
    )
    return True


def sync_pricing_rule_to_supabase(prop: Dict, rules: Optional[Dict] = None) -> Optional[Dict]:
    client = get_supabase_account_client()
    if client is None or not prop:
        return None

    payload = _pricing_rule_payload(prop, rules or {})
    rows = _safe_execute(
        lambda: client.table(PRICING_RULES_TABLE)
        .upsert(payload, on_conflict="account_id,property_local_id")
        .execute(),
        default=None,
        action="sync_pricing_rule",
    )
    return rows[0] if isinstance(rows, list) and rows else None


def sync_property_and_pricing_to_supabase(prop: Dict, rules: Optional[Dict] = None) -> None:
    sync_property_to_supabase(prop)
    sync_pricing_rule_to_supabase(prop, rules)


def _property_payload(prop: Dict) -> Dict:
    now = datetime.utcnow().isoformat()
    return {
        "account_id": int(prop.get("account_id") or 1),
        "local_id": int(prop["id"]),
        "name": prop.get("name", ""),
        "platform": prop.get("platform", "airbnb"),
        "listing_url": prop.get("listing_url", ""),
        "listing_id": prop.get("listing_id", ""),
        "city": prop.get("city", ""),
        "latitude": prop.get("latitude"),
        "longitude": prop.get("longitude"),
        "min_price": float(prop.get("min_price", 50.0)),
        "max_price": float(prop.get("max_price", 500.0)),
        "sync_mode": prop.get("sync_mode", "advisory"),
        "strategy": prop.get("strategy", "balanced"),
        "plan": prop.get("plan", "free"),
        "updated_at": prop.get("updated_at") or now,
    }


def _pricing_rule_payload(prop: Dict, rules: Dict) -> Dict:
    now = datetime.utcnow().isoformat()
    return {
        "account_id": int(prop.get("account_id") or 1),
        "property_local_id": int(prop["id"]),
        "min_price": float(rules.get("min_price", prop.get("min_price", 50.0))),
        "max_price": float(rules.get("max_price", prop.get("max_price", 500.0))),
        "strategy": rules.get("strategy", prop.get("strategy", "balanced")),
        "sync_mode": rules.get("sync_mode", prop.get("sync_mode", "advisory")),
        "max_change_pct": _optional_float(rules.get("max_change_pct")),
        "occupancy_low_threshold": _optional_float(rules.get("occupancy_low_threshold")),
        "occupancy_high_threshold": _optional_float(rules.get("occupancy_high_threshold")),
        "source": rules.get("source", "pricepilot_dashboard"),
        "updated_at": rules.get("updated_at") or prop.get("updated_at") or now,
    }


def _row_to_property(row: Dict, *, account_id: int, local_id: int) -> Dict:
    return {
        "id": local_id,
        "account_id": int(row.get("account_id") or account_id),
        "name": row.get("name") or "Proprieta",
        "platform": row.get("platform") or "airbnb",
        "listing_url": row.get("listing_url") or "",
        "listing_id": row.get("listing_id") or "",
        "city": row.get("city") or "",
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "min_price": float(row.get("min_price") or 50.0),
        "max_price": float(row.get("max_price") or 500.0),
        "sync_mode": row.get("sync_mode") or "advisory",
        "strategy": row.get("strategy") or "balanced",
        "plan": row.get("plan") or "free",
    }


def _safe_execute(fn, *, default: Any, action: str) -> Any:
    try:
        response = fn()
        return _response_data(response)
    except Exception as exc:
        logger.warning("Supabase %s non riuscito: %s", action, exc)
        return default


def _response_data(response: Any) -> Any:
    if response is None:
        return None
    data = getattr(response, "data", None)
    if data is not None:
        return data
    if isinstance(response, dict):
        return response.get("data")
    return response


def _remote_is_newer(remote_updated_at: Any, local_updated_at: Any) -> bool:
    remote_dt = _parse_dt(remote_updated_at)
    local_dt = _parse_dt(local_updated_at)
    if remote_dt is None:
        return False
    if local_dt is None:
        return True
    return remote_dt > local_dt


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
