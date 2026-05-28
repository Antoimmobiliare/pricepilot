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

from pricepilot.core.database import get_property, upsert_property
from pricepilot.core.supabase_client import get_supabase_client, supabase_available

logger = logging.getLogger("pricepilot.supabase_repository")

PROPERTIES_TABLE = os.environ.get("PRICEPILOT_SUPABASE_PROPERTIES_TABLE", "properties")
PRICING_RULES_TABLE = os.environ.get("PRICEPILOT_SUPABASE_PRICING_RULES_TABLE", "pricing_rules")


def is_supabase_db_ready() -> bool:
    return supabase_available()


def refresh_properties_from_supabase(account_id: int) -> int:
    """Aggiorna SQLite con eventuali proprieta piu recenti presenti su Supabase."""
    client = get_supabase_client()
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


def sync_property_to_supabase(prop: Dict) -> Optional[Dict]:
    client = get_supabase_client()
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
    client = get_supabase_client()
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
    client = get_supabase_client()
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
