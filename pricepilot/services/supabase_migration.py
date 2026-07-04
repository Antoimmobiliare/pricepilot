"""
Migrazione controllata SQLite -> Supabase.

Il progetto usa ancora SQLite come fallback locale. Questo modulo prepara il
passaggio a Supabase copiando i dati tenant-scoped dell'account corrente senza
toccare il pricing engine.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from pricepilot.core.database import get_conn
from pricepilot.services.supabase_repository import (
    has_supabase_write_context,
    sync_pricing_rule_to_supabase,
    sync_property_to_supabase,
)

logger = logging.getLogger("pricepilot.supabase_migration")


MigrationPayload = Callable[[Dict[str, Any]], Dict[str, Any]]


TENANT_TABLES: tuple[dict[str, Any], ...] = (
    {
        "local_table": "guardrail_policies",
        "remote_table": "guardrail_policies",
        "on_conflict": "account_id,property_local_id",
        "query": "SELECT * FROM guardrail_policies WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id"), default=0),
            "max_change_pct": _as_float(row.get("max_change_pct")),
            "require_approval_pct": _as_float(row.get("require_approval_pct")),
            "min_confidence_auto": _as_float(row.get("min_confidence_auto")),
            "competitor_outlier_pct": _as_float(row.get("competitor_outlier_pct")),
            "max_daily_auto_changes": _as_int(row.get("max_daily_auto_changes")),
            "auto_enabled": _as_bool(row.get("auto_enabled")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        },
    },
    {
        "local_table": "price_calendar",
        "remote_table": "price_calendar",
        "on_conflict": "account_id,property_local_id,date",
        "query": "SELECT * FROM price_calendar WHERE account_id=? ORDER BY date, property_id, id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "date": row.get("date"),
            "current_price": _as_float(row.get("current_price")),
            "current_price_source": row.get("current_price_source") or "manual",
            "recommended_price": _optional_float(row.get("recommended_price")),
            "status": row.get("status") or "current",
            "decision_log_local_id": _optional_int(row.get("decision_log_id")),
            "applied_price": _optional_float(row.get("applied_price")),
            "notes": row.get("notes") or "",
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        },
    },
    {
        "local_table": "decision_log",
        "remote_table": "decision_log",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM decision_log WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "timestamp": row.get("timestamp"),
            "date": row.get("date"),
            "old_price": _as_float(row.get("old_price")),
            "new_price": _as_float(row.get("new_price")),
            "market_avg": _optional_float(row.get("market_avg")),
            "competitor_avg": _optional_float(row.get("competitor_avg")),
            "occupancy": _optional_float(row.get("occupancy")),
            "decision": row.get("decision") or "",
            "mode": row.get("mode") or "advisory",
            "applied": _as_bool(row.get("applied")),
            "strategy": row.get("strategy"),
            "factors": _json_obj(row.get("factors")),
            "mpi": _optional_float(row.get("mpi")),
            "current_price_source": row.get("current_price_source") or "manual",
            "data_source": row.get("data_source") or "demo",
            "notes": row.get("notes") or "",
            "tg_message_id": str(row.get("tg_message_id") or ""),
        },
    },
    {
        "local_table": "occupancy_history",
        "remote_table": "occupancy_history",
        "on_conflict": "account_id,property_local_id,date",
        "query": "SELECT * FROM occupancy_history WHERE account_id=? ORDER BY date, property_id, id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "date": row.get("date"),
            "occupancy": _as_float(row.get("occupancy")),
            "source": row.get("source") or "manual",
            "updated_at": _now_iso(),
        },
    },
    {
        "local_table": "market_history",
        "remote_table": "market_history",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM market_history WHERE account_id=? ORDER BY date, property_id, id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "date": row.get("date"),
            "market_avg": _optional_float(row.get("market_avg")),
            "market_min": _optional_float(row.get("market_min")),
            "market_max": _optional_float(row.get("market_max")),
            "market_std": _optional_float(row.get("market_std")),
            "competitor_count": _optional_int(row.get("competitor_count")),
            "source": row.get("source") or "demo",
            "recorded_at": row.get("recorded_at") or _now_iso(),
        },
    },
    {
        "local_table": "telegram_links",
        "remote_table": "telegram_links",
        "on_conflict": "account_id,local_id",
        "query": (
            "SELECT tl.*, p.account_id AS account_id "
            "FROM telegram_links tl "
            "JOIN properties p ON p.id=tl.property_id "
            "WHERE p.account_id=? "
            "ORDER BY tl.id"
        ),
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "token": row.get("token") or "",
            "chat_id": _optional_int(row.get("chat_id")),
            "telegram_username": row.get("telegram_username") or "",
            "active": _as_bool(row.get("active")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        },
    },
    {
        "local_table": "property_integrations",
        "remote_table": "property_integrations",
        "on_conflict": "account_id,local_id",
        "query": (
            "SELECT pi.*, p.account_id AS account_id "
            "FROM property_integrations pi "
            "JOIN properties p ON p.id=pi.property_id "
            "WHERE p.account_id=? "
            "ORDER BY pi.id"
        ),
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "platform": row.get("platform") or "",
            "listing_url": row.get("listing_url") or "",
            "listing_id": row.get("listing_id") or "",
            "is_primary": _as_bool(row.get("is_primary")),
            "created_at": row.get("created_at"),
            "updated_at": _now_iso(),
        },
    },
    {
        "local_table": "operation_runs",
        "remote_table": "operation_runs",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM operation_runs WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "source": row.get("source") or "scheduler",
            "status": row.get("status") or "running",
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "next_run_at": row.get("next_run_at"),
            "decisions_count": _as_int(row.get("decisions_count"), default=0),
            "summary": _json_obj(row.get("summary")),
            "error": row.get("error") or "",
        },
    },
    {
        "local_table": "audit_events",
        "remote_table": "audit_events",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM audit_events WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _optional_int(row.get("property_id")),
            "timestamp": row.get("timestamp"),
            "source": row.get("source") or "system",
            "action": row.get("action") or "",
            "entity_type": row.get("entity_type") or "system",
            "entity_id": str(row.get("entity_id") or ""),
            "status": row.get("status") or "ok",
            "details": _json_obj(row.get("details")),
        },
    },
    {
        "local_table": "notification_preferences",
        "remote_table": "notification_preferences",
        "on_conflict": "account_id,property_local_id",
        "query": "SELECT * FROM notification_preferences WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id"), default=0),
            "telegram_enabled": _as_bool(row.get("telegram_enabled")),
            "quiet_hours_start": row.get("quiet_hours_start") or "",
            "quiet_hours_end": row.get("quiet_hours_end") or "",
            "daily_digest": _as_bool(row.get("daily_digest")),
            "approval_alerts": _as_bool(row.get("approval_alerts")),
            "auto_reports": _as_bool(row.get("auto_reports")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        },
    },
    {
        "local_table": "notification_log",
        "remote_table": "notification_log",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM notification_log WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _optional_int(row.get("property_id")),
            "timestamp": row.get("timestamp"),
            "channel": row.get("channel") or "telegram",
            "event_type": row.get("event_type") or "",
            "recipient": row.get("recipient") or "",
            "status": row.get("status") or "",
            "message_id": row.get("message_id") or "",
            "error": row.get("error") or "",
            "payload": _json_obj(row.get("payload")),
        },
    },
)


def migrate_sqlite_account_to_supabase(account_id: int = 1) -> Dict[str, Any]:
    """
    Copia su Supabase i dati locali di un account.

    Richiede una sessione Supabase autenticata con membership per l'account,
    oppure SUPABASE_SERVICE_ROLE_KEY configurata server-side. In assenza di
    questo contesto la migrazione resta bloccata in modo esplicito.
    """
    account_id = max(1, int(account_id or 1))
    if not has_supabase_write_context():
        return {
            "ok": False,
            "account_id": account_id,
            "error": (
                "Manca un contesto Supabase autenticato: accedi con Supabase "
                "oppure configura SUPABASE_SERVICE_ROLE_KEY solo server-side."
            ),
            "tables": {},
        }

    from pricepilot.core.supabase_client import get_supabase_account_client
    client = get_supabase_account_client()

    summary: Dict[str, Any] = {
        "ok": True,
        "account_id": account_id,
        "tables": {},
        "errors": [],
    }

    properties = _fetch_rows(
        "SELECT * FROM properties WHERE account_id=? ORDER BY id",
        (account_id,),
    )
    synced_properties = 0
    synced_pricing_rules = 0
    for prop in properties:
        try:
            if sync_property_to_supabase(prop):
                synced_properties += 1
            if sync_pricing_rule_to_supabase(prop):
                synced_pricing_rules += 1
        except Exception as exc:
            summary["errors"].append({
                "table": "properties",
                "local_id": prop.get("id"),
                "error": str(exc),
            })
    summary["tables"]["properties"] = {
        "read": len(properties),
        "synced": synced_properties,
    }
    summary["tables"]["pricing_rules"] = {
        "read": len(properties),
        "synced": synced_pricing_rules,
    }
    if synced_properties < len(properties):
        summary["errors"].append({
            "table": "properties",
            "error": f"Sincronizzate {synced_properties}/{len(properties)} righe.",
        })
    if synced_pricing_rules < len(properties):
        summary["errors"].append({
            "table": "pricing_rules",
            "error": f"Sincronizzate {synced_pricing_rules}/{len(properties)} righe.",
        })

    for spec in TENANT_TABLES:
        rows = _fetch_rows(spec["query"], (account_id,))
        payloads = [_strip_none(spec["payload"](row)) for row in rows]
        synced = _upsert_payloads(
            client,
            table=spec["remote_table"],
            payloads=payloads,
            on_conflict=spec["on_conflict"],
        )
        summary["tables"][spec["remote_table"]] = {
            "read": len(rows),
            "synced": synced,
        }
        if synced < len(rows):
            summary["errors"].append({
                "table": spec["remote_table"],
                "error": f"Sincronizzate {synced}/{len(rows)} righe.",
            })

    summary["ok"] = not summary["errors"]
    return summary


def dry_run_sqlite_account_migration(account_id: int = 1) -> Dict[str, Any]:
    """Conta le righe che verrebbero migrate, senza scrivere su Supabase."""
    account_id = max(1, int(account_id or 1))
    tables: Dict[str, int] = {
        "properties": len(_fetch_rows(
            "SELECT id FROM properties WHERE account_id=?",
            (account_id,),
        )),
    }
    tables["pricing_rules"] = tables["properties"]
    for spec in TENANT_TABLES:
        tables[spec["remote_table"]] = len(_fetch_rows(spec["query"], (account_id,)))
    return {"account_id": account_id, "tables": tables}


def _fetch_rows(query: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def _upsert_payloads(client: Any, *, table: str, payloads: List[Dict], on_conflict: str) -> int:
    if not payloads:
        return 0
    try:
        response = client.table(table).upsert(payloads, on_conflict=on_conflict).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return len(data)
        return len(payloads)
    except Exception as exc:
        logger.warning("Migrazione Supabase tabella %s non riuscita: %s", table, exc)
        return 0


def _strip_none(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        return {"raw": str(value)}


def _as_int(value: Any, default: int = 1) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    parsed = _optional_float(value)
    return default if parsed is None else parsed


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _now_iso() -> str:
    return datetime.utcnow().isoformat()
