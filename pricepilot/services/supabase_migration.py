"""
Migrazione controllata SQLite -> Supabase.

Il progetto usa ancora SQLite come fallback locale. Questo modulo prepara il
passaggio a Supabase copiando i dati tenant-scoped dell'account corrente senza
toccare il pricing engine.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from pricepilot.core.data_backend import is_supabase_primary
from pricepilot.core.database import get_conn
from pricepilot.services.supabase_repository import (
    has_supabase_write_context,
)

logger = logging.getLogger("pricepilot.supabase_migration")


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
        "local_table": "telegram_approvals",
        "remote_table": "telegram_approvals",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM telegram_approvals WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _optional_int(row.get("property_id")),
            "decision_log_local_id": _as_int(row.get("decision_log_id")),
            "telegram_link_local_id": _optional_int(row.get("telegram_link_id")),
            "chat_id": _optional_int(row.get("chat_id")),
            "telegram_username": row.get("telegram_username") or "",
            "action": row.get("action") or "",
            "status": row.get("status") or "",
            "source": row.get("source") or "telegram",
            "message_id": row.get("message_id") or "",
            "callback_query_id": row.get("callback_query_id") or "",
            "error": row.get("error") or "",
            "payload": _json_obj(row.get("payload")),
            "timestamp": row.get("timestamp") or _now_iso(),
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
    {
        "local_table": "price_updates",
        "remote_table": "price_updates",
        "on_conflict": "account_id,local_id",
        "query": "SELECT * FROM price_updates WHERE account_id=? ORDER BY id",
        "payload": lambda row: {
            "account_id": _as_int(row.get("account_id")),
            "local_id": _as_int(row.get("id")),
            "property_local_id": _as_int(row.get("property_id")),
            "platform": row.get("platform") or "",
            "listing_id": row.get("listing_id") or "",
            "target_date": row.get("target_date"),
            "new_price": _optional_float(row.get("new_price")),
            "ok": _as_bool(row.get("ok")),
            "error": row.get("error") or "",
            "applied_at": row.get("applied_at") or _now_iso(),
            "is_stub": _as_bool(row.get("is_stub")),
        },
    },
)


def migrate_sqlite_account_to_supabase(
    source_account_id: int = 1,
    cloud_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Copia su Supabase i dati locali di un account.

    ``source_account_id`` e' l'ID del database SQLite. ``cloud_account_id``
    e' l'ID dell'account gia creato su Supabase; puo essere diverso. Questa
    separazione e' essenziale per non mescolare tenant creati in momenti
    diversi durante la fase di transizione.
    """
    source_account_id = max(1, int(source_account_id or 1))
    cloud_account_id = max(1, int(cloud_account_id or source_account_id))
    if is_supabase_primary():
        return {
            "ok": False,
            "source_account_id": source_account_id,
            "cloud_account_id": cloud_account_id,
            "error": "La migrazione va eseguita prima di attivare PRICEPILOT_DATABASE_BACKEND=supabase.",
            "tables": {},
        }
    if not has_supabase_write_context():
        return {
            "ok": False,
            "source_account_id": source_account_id,
            "cloud_account_id": cloud_account_id,
            "error": (
                "Manca un contesto Supabase autenticato: accedi con Supabase "
                "oppure configura SUPABASE_SERVICE_ROLE_KEY solo server-side."
            ),
            "tables": {},
        }

    from pricepilot.core.supabase_client import (
        get_supabase_account_client,
        get_supabase_admin_client,
    )
    client = get_supabase_admin_client() or get_supabase_account_client()
    if client is None:
        return {
            "ok": False,
            "source_account_id": source_account_id,
            "cloud_account_id": cloud_account_id,
            "error": "Client Supabase non disponibile.",
            "tables": {},
        }
    if not _cloud_account_exists(client, cloud_account_id):
        return {
            "ok": False,
            "source_account_id": source_account_id,
            "cloud_account_id": cloud_account_id,
            "error": (
                f"L'account cloud {cloud_account_id} non esiste. "
                "Registra prima l'utente su PricePilot/Supabase."
            ),
            "tables": {},
        }

    summary: Dict[str, Any] = {
        "ok": True,
        "source_account_id": source_account_id,
        "cloud_account_id": cloud_account_id,
        "tables": {},
        "errors": [],
    }

    properties = _fetch_rows(
        "SELECT * FROM properties WHERE account_id=? ORDER BY id",
        (source_account_id,),
    )
    property_payloads = [_property_payload(prop, cloud_account_id) for prop in properties]
    pricing_payloads = [_pricing_rule_payload(prop, cloud_account_id) for prop in properties]
    synced_properties = _upsert_payloads(
        client, table="properties", payloads=property_payloads,
        on_conflict="account_id,local_id",
    )
    synced_pricing_rules = _upsert_payloads(
        client, table="pricing_rules", payloads=pricing_payloads,
        on_conflict="account_id,property_local_id",
    )
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
        if not _local_table_exists(spec["local_table"]):
            summary["tables"][spec["remote_table"]] = {"read": 0, "synced": 0, "skipped": True}
            continue
        rows = _fetch_rows(spec["query"], (source_account_id,))
        payloads = [
            _strip_none({**spec["payload"](row), "account_id": cloud_account_id})
            for row in rows
        ]
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

    consent_result = _migrate_user_consents(
        client,
        source_account_id=source_account_id,
        cloud_account_id=cloud_account_id,
    )
    summary["tables"]["user_consents"] = consent_result
    if consent_result["synced"] < consent_result["read"]:
        summary["errors"].append({
            "table": "user_consents",
            "error": f"Sincronizzate {consent_result['synced']}/{consent_result['read']} righe.",
        })

    summary["ok"] = not summary["errors"]
    return summary


def dry_run_sqlite_account_migration(source_account_id: int = 1) -> Dict[str, Any]:
    """Conta le righe che verrebbero migrate, senza scrivere su Supabase."""
    source_account_id = max(1, int(source_account_id or 1))
    if is_supabase_primary():
        return {"ok": False, "error": "Imposta il backend SQLite prima del dry-run.", "tables": {}}
    tables: Dict[str, int] = {
        "properties": len(_fetch_rows(
            "SELECT id FROM properties WHERE account_id=?",
            (source_account_id,),
        )),
    }
    tables["pricing_rules"] = tables["properties"]
    for spec in TENANT_TABLES:
        tables[spec["remote_table"]] = (
            len(_fetch_rows(spec["query"], (source_account_id,)))
            if _local_table_exists(spec["local_table"]) else 0
        )
    tables["user_consents"] = (
        len(_fetch_rows("SELECT id FROM user_consents WHERE account_id=?", (source_account_id,)))
        if _local_table_exists("user_consents") else 0
    )
    return {
        "ok": True,
        # ``account_id`` resta per compatibilita con gli strumenti di verifica
        # gia presenti; source_account_id chiarisce il lato SQLite.
        "account_id": source_account_id,
        "source_account_id": source_account_id,
        "tables": tables,
    }


def _fetch_rows(query: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def _local_table_exists(table: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    return bool(row)


def _cloud_account_exists(client: Any, account_id: int) -> bool:
    try:
        response = client.table("accounts").select("id").eq("id", int(account_id)).limit(1).execute()
        return bool(getattr(response, "data", None))
    except Exception as exc:
        logger.warning("Verifica account cloud %s non riuscita: %s", account_id, exc)
        return False


def _property_payload(prop: Dict[str, Any], cloud_account_id: int) -> Dict[str, Any]:
    return {
        "account_id": cloud_account_id,
        "local_id": _as_int(prop.get("id")),
        "name": prop.get("name") or "La mia proprieta",
        "platform": prop.get("platform") or "airbnb",
        "listing_url": prop.get("listing_url") or "",
        "listing_id": prop.get("listing_id") or "",
        "city": prop.get("city") or "",
        "latitude": _optional_float(prop.get("latitude")),
        "longitude": _optional_float(prop.get("longitude")),
        "min_price": _as_float(prop.get("min_price"), default=50.0),
        "max_price": _as_float(prop.get("max_price"), default=500.0),
        "sync_mode": prop.get("sync_mode") or "advisory",
        "strategy": prop.get("strategy") or "balanced",
        "plan": prop.get("plan") or "free",
    }


def _pricing_rule_payload(prop: Dict[str, Any], cloud_account_id: int) -> Dict[str, Any]:
    return {
        "account_id": cloud_account_id,
        "property_local_id": _as_int(prop.get("id")),
        "min_price": _as_float(prop.get("min_price"), default=50.0),
        "max_price": _as_float(prop.get("max_price"), default=500.0),
        "strategy": prop.get("strategy") or "balanced",
        "sync_mode": prop.get("sync_mode") or "advisory",
        "source": "sqlite_cutover",
    }


def _migrate_user_consents(
    client: Any,
    *,
    source_account_id: int,
    cloud_account_id: int,
) -> Dict[str, int]:
    if not (_local_table_exists("user_consents") and _local_table_exists("users")):
        return {"read": 0, "synced": 0}
    rows = _fetch_rows(
        "SELECT uc.*, u.email FROM user_consents uc JOIN users u ON u.id=uc.user_id "
        "WHERE uc.account_id=? ORDER BY uc.id",
        (source_account_id,),
    )
    payloads: list[Dict[str, Any]] = []
    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        if not email:
            continue
        try:
            response = client.table("profiles").select("id").eq("email", email).limit(1).execute()
            profiles = getattr(response, "data", None) or []
        except Exception as exc:
            logger.warning("Profilo cloud non leggibile per consenso %s: %s", email, exc)
            continue
        if not profiles:
            continue
        payloads.append({
            "account_id": cloud_account_id,
            "user_id": profiles[0]["id"],
            "terms_accepted": _as_bool(row.get("terms_accepted")),
            "privacy_accepted": _as_bool(row.get("privacy_accepted")),
            "marketing_accepted": _as_bool(row.get("marketing_accepted")),
            "terms_version": row.get("terms_version") or "2026-07-13",
            "privacy_version": row.get("privacy_version") or "2026-07-13",
            "source": row.get("source") or "sqlite_cutover",
            "accepted_at": row.get("accepted_at") or _now_iso(),
        })
    return {"read": len(rows), "synced": _upsert_payloads(
        client, table="user_consents", payloads=payloads,
        on_conflict="user_id,terms_version,privacy_version",
    )}


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


def _main() -> int:
    """CLI deliberata: prima mostra il dry-run, poi migra con --execute."""
    from pricepilot.core.config import _load_dotenv

    _load_dotenv()
    parser = argparse.ArgumentParser(description="Migra un account SQLite nel tenant Supabase scelto.")
    parser.add_argument("--source-account-id", type=int, default=1, help="Account nel database SQLite locale.")
    parser.add_argument("--cloud-account-id", type=int, required=True, help="Account gia creato su Supabase.")
    parser.add_argument("--execute", action="store_true", help="Esegue le scritture; senza flag mostra solo il dry-run.")
    args = parser.parse_args()
    if args.execute:
        result = migrate_sqlite_account_to_supabase(
            source_account_id=args.source_account_id,
            cloud_account_id=args.cloud_account_id,
        )
    else:
        result = dry_run_sqlite_account_migration(args.source_account_id)
        result["cloud_account_id"] = int(args.cloud_account_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


if __name__ == "__main__":
    sys.exit(_main())
