"""Repository Supabase canonico per i dati operativi di PricePilot.

Il modulo conserva il contratto storico di ``pricepilot.core.database``
(identificativi numerici e dizionari legacy), ma legge e scrive unicamente
nelle tabelle cloud. Questo permette a Streamlit, API Render e worker Telegram
di vedere la stessa proprieta, decisione e approvazione.

Non usare questo modulo come sync best-effort: quando il backend e ``supabase``
un errore remoto deve fermare l'operazione. Un fallback locale creerebbe dati
divergenti tra i processi.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Optional

from pricepilot.core.data_backend import CloudDatabaseUnavailable, server_runtime
from pricepilot.core.supabase_client import (
    get_supabase_account_client,
    get_supabase_admin_client,
    has_supabase_auth_session,
)


DEFAULT_GUARDRAIL_POLICY = {
    "max_change_pct": 0.20,
    "require_approval_pct": 0.15,
    "min_confidence_auto": 0.80,
    "competitor_outlier_pct": 0.60,
    "max_daily_auto_changes": 4,
    "auto_enabled": 1,
}

_JSON_COLUMNS = {"factors", "payload", "details", "summary"}
_BOOL_COLUMNS = {
    "applied", "active", "auto_enabled", "telegram_enabled", "daily_digest",
    "approval_alerts", "auto_reports", "is_primary", "ok", "is_stub",
    "terms_accepted", "privacy_accepted", "marketing_accepted",
}
_ID_RENAMES = {
    "local_id": "id",
    "property_local_id": "property_id",
    "decision_log_local_id": "decision_log_id",
    "telegram_link_local_id": "telegram_link_id",
}

# Le tabelle in questo insieme devono sempre essere interrogate con un account
# esplicito, oppure tramite una sessione Supabase soggetta alle policy RLS.
# Questo protegge il caso di ripristino della sessione applicativa con cookie:
# in quel caso il codice usa una chiave server, ma non deve mai leggere dati di
# altri account.
_ACCOUNT_SCOPED_TABLES = {
    "properties", "pricing_rules", "guardrail_policies", "price_calendar",
    "decision_log", "occupancy_history", "market_history", "telegram_links",
    "telegram_approvals", "property_integrations", "operation_runs",
    "audit_events", "notification_preferences", "notification_log",
    "user_consents", "price_updates", "pricing_decisions", "competitors",
    "events", "market_snapshots",
}


def _client() -> Any:
    client = get_supabase_account_client(allow_service_role=True)
    if client is None:
        raise CloudDatabaseUnavailable(
            "Supabase cloud primario attivo, ma non esiste una sessione utente "
            "o una SUPABASE_SERVICE_ROLE_KEY server-side."
        )
    return client


def _session_client() -> Any:
    """Client server-side per i cookie di sessione PricePilot.

    ``app_sessions`` contiene esclusivamente hash di token HttpOnly e non ha
    policy RLS per i client. Anche dopo un login Supabase riuscito, il client
    con bearer token dell'utente non deve poter leggere o scrivere quella
    tabella: queste operazioni restano nel processo server tramite service
    role.
    """
    client = get_supabase_admin_client()
    if client is None:
        raise CloudDatabaseUnavailable(
            "SUPABASE_SERVICE_ROLE_KEY necessaria per gestire le sessioni cloud."
        )
    return client


def _data(response: Any) -> list[Dict]:
    data = getattr(response, "data", None) or []
    return [dict(row) for row in data]


def _one(rows: list[Dict]) -> Optional[Dict]:
    return rows[0] if rows else None


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {"value": value}
    return value if value is not None else {}


def _legacy_row(row: Optional[Dict]) -> Optional[Dict]:
    if row is None:
        return None
    result = dict(row)
    for source, target in _ID_RENAMES.items():
        if result.get(source) is not None:
            result[target] = int(result[source])
    for column in _JSON_COLUMNS:
        if isinstance(result.get(column), (dict, list)):
            result[column] = json.dumps(result[column], ensure_ascii=False)
    for column in _BOOL_COLUMNS:
        if column in result and result[column] is not None:
            result[column] = int(bool(result[column]))
    return result


def _legacy_rows(rows: Iterable[Dict]) -> list[Dict]:
    return [_legacy_row(row) or {} for row in rows]


def _dashboard_account_id() -> Optional[int]:
    """Restituisce l'account dell'utente Streamlit, senza assumere RLS."""
    try:
        import streamlit as st

        user = st.session_state.get("pp_auth_user") or {}
        account_id = int(user.get("account_id") or 0)
        return account_id if account_id > 0 else None
    except Exception:
        return None


def _scoped_filters(table: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Completa i filtri tenant quando il client usa una chiave server.

    I worker API/Telegram sono gli unici processi autorizzati a effettuare
    scansioni multi-account; la dashboard puo operare solo sul suo account.
    """
    result = dict(filters or {})
    if table not in _ACCOUNT_SCOPED_TABLES or "account_id" in result:
        return result
    if has_supabase_auth_session() or server_runtime():
        return result
    account_id = _dashboard_account_id()
    if account_id is None:
        raise CloudDatabaseUnavailable(
            f"Query cloud non circoscritta all'account per la tabella '{table}'."
        )
    result["account_id"] = account_id
    return result


def _scoped_payload(table: str, payload: Dict) -> Dict:
    """Impedisce insert/upsert tenant-scoped senza account_id."""
    result = dict(payload)
    if table not in _ACCOUNT_SCOPED_TABLES or result.get("account_id") is not None:
        return result
    if has_supabase_auth_session() or server_runtime():
        raise CloudDatabaseUnavailable(
            f"Scrittura cloud senza account_id per la tabella '{table}'."
        )
    account_id = _dashboard_account_id()
    if account_id is None:
        raise CloudDatabaseUnavailable(
            f"Scrittura cloud non circoscritta all'account per la tabella '{table}'."
        )
    result["account_id"] = account_id
    return result


def _query(table: str, *, filters: Optional[Dict[str, Any]] = None) -> tuple[Any, Dict[str, Any]]:
    scoped = _scoped_filters(table, filters)
    query = _client().table(table).select("*")
    for column, value in scoped.items():
        if value is not None:
            query = query.eq(column, value)
    return query, scoped


def _select(
    table: str,
    *,
    filters: Optional[Dict[str, Any]] = None,
    order: Optional[tuple[str, bool]] = None,
    limit: Optional[int] = None,
) -> list[Dict]:
    query, _ = _query(table, filters=filters)
    if order:
        query = query.order(order[0], desc=order[1])
    if limit is not None:
        query = query.limit(int(limit))
    return _data(query.execute())


def _insert(table: str, payload: Dict) -> Dict:
    payload = _scoped_payload(table, payload)
    rows = _data(_client().table(table).insert(payload).execute())
    if not rows:
        raise CloudDatabaseUnavailable(f"Supabase non ha restituito la riga inserita in {table}.")
    return rows[0]


def _upsert(table: str, payload: Dict, conflict: str) -> Dict:
    payload = _scoped_payload(table, payload)
    rows = _data(_client().table(table).upsert(payload, on_conflict=conflict).execute())
    if not rows:
        # PostgREST puo non restituire representation in rari casi: rileggiamo.
        filter_columns = [name.strip() for name in conflict.split(",")]
        filters = {key: payload[key] for key in filter_columns if key in payload}
        row = _one(_select(table, filters=filters, limit=1))
        if row:
            return row
        raise CloudDatabaseUnavailable(f"Supabase non ha confermato l'upsert in {table}.")
    return rows[0]


def _update(table: str, payload: Dict, *, filters: Dict[str, Any]) -> list[Dict]:
    filters = _scoped_filters(table, filters)
    query = _client().table(table).update(payload)
    for column, value in filters.items():
        query = query.eq(column, value)
    return _data(query.execute())


def _delete(table: str, *, filters: Dict[str, Any]) -> None:
    filters = _scoped_filters(table, filters)
    query = _client().table(table).delete()
    for column, value in filters.items():
        query = query.eq(column, value)
    query.execute()


def _account_from_property(property_id: int) -> Optional[int]:
    row = _one(_select("properties", filters={"local_id": int(property_id)}, limit=1))
    return int(row["account_id"]) if row else None


def _id_from_row(row: Dict, table: str) -> int:
    local_id = row.get("local_id")
    if local_id is None:
        raise CloudDatabaseUnavailable(
            f"La tabella cloud '{table}' non assegna local_id. Applica supabase/cloud_primary_cutover.sql."
        )
    return int(local_id)


def _property_payload(prop: Dict) -> Dict:
    return {
        "account_id": int(prop.get("account_id") or 1),
        "name": str(prop.get("name") or "La mia proprieta"),
        "platform": str(prop.get("platform") or "airbnb"),
        "listing_url": str(prop.get("listing_url") or ""),
        "listing_id": str(prop.get("listing_id") or ""),
        "city": str(prop.get("city") or ""),
        "latitude": prop.get("latitude"),
        "longitude": prop.get("longitude"),
        "min_price": float(prop.get("min_price") or 50),
        "max_price": float(prop.get("max_price") or 500),
        "sync_mode": str(prop.get("sync_mode") or "advisory"),
        "strategy": str(prop.get("strategy") or "balanced"),
        "plan": str(prop.get("plan") or "free"),
    }


def _ensure_property_pricing_rule(prop: Dict) -> None:
    payload = {
        "account_id": int(prop.get("account_id") or 1),
        "property_local_id": int(prop["id"]),
        "min_price": float(prop.get("min_price") or 50),
        "max_price": float(prop.get("max_price") or 500),
        "strategy": str(prop.get("strategy") or "balanced"),
        "sync_mode": str(prop.get("sync_mode") or "advisory"),
        "source": "pricepilot_cloud",
    }
    _upsert("pricing_rules", payload, "account_id,property_local_id")


# ---------------------------------------------------------------------------
# Account, properties and commercial settings
# ---------------------------------------------------------------------------

def create_account(name: str, plan: str = "free", billing_status: str = "dev") -> Dict:
    payload = {
        "name": (name or "La mia attivita").strip() or "La mia attivita",
        "plan": plan or "free",
        "billing_status": billing_status or "dev",
    }
    try:
        user = _client().auth.get_user()
        user_id = getattr(getattr(user, "user", None), "id", None)
        if user_id:
            payload["owner_user_id"] = str(user_id)
    except Exception:
        pass
    row = _insert("accounts", payload)
    account_id = int(row["id"])
    ensure_default_guardrail_policy(account_id)
    ensure_default_notification_preferences(account_id)
    return _legacy_row(row) or {}


def _cloud_user_from_profile(profile: Dict, account_id: Optional[int] = None) -> Optional[Dict]:
    """Restituisce il formato utente legacy partendo da profiles/account_members."""
    profile_id = str(profile.get("id") or "")
    local_id = profile.get("local_id")
    if not profile_id or local_id is None:
        return None

    memberships = _select(
        "account_members",
        filters={"user_id": profile_id, "account_id": account_id} if account_id else {"user_id": profile_id},
        order=("created_at", False),
        limit=1,
    )
    membership = _one(memberships)
    if not membership:
        return None
    return {
        "id": int(local_id),
        "account_id": int(membership["account_id"]),
        "full_name": str(profile.get("full_name") or ""),
        "email": str(profile.get("email") or "").lower(),
        "role": str(membership.get("role") or "owner"),
        "password_hash": "",
        "auth_provider": "supabase",
        "external_user_id": profile_id,
        "last_login_at": None,
        "created_at": profile.get("created_at"),
        "updated_at": profile.get("updated_at"),
    }


def _profile_by_local_id(user_id: int) -> Optional[Dict]:
    return _one(_select("profiles", filters={"local_id": int(user_id)}, limit=1))


def get_user(user_id: int) -> Optional[Dict]:
    profile = _profile_by_local_id(user_id)
    return _cloud_user_from_profile(profile) if profile else None


def get_user_by_email(email: str) -> Optional[Dict]:
    profile = _one(_select("profiles", filters={"email": str(email or "").strip().lower()}, limit=1))
    return _cloud_user_from_profile(profile) if profile else None


def get_users(account_id: Optional[int] = None) -> list[Dict]:
    filters = {"account_id": int(account_id)} if account_id is not None else {}
    memberships = _select("account_members", filters=filters, order=("created_at", False))
    users: list[Dict] = []
    for membership in memberships:
        profile = _one(_select("profiles", filters={"id": membership["user_id"]}, limit=1))
        if not profile:
            continue
        user = _cloud_user_from_profile(profile, int(membership["account_id"]))
        if user:
            users.append(user)
    return users


def ensure_authenticated_user(
    *,
    email: str,
    external_user_id: str,
    plan: str = "free",
    account_name: str = "",
    full_name: str = "",
) -> Optional[Dict]:
    """Crea il profilo e il primo account dopo un login Supabase confermato.

    L'account non viene creato quando la registrazione e ancora in attesa di
    conferma email: in quel momento Supabase non fornisce una sessione RLS.
    """
    external_user_id = str(external_user_id or "").strip()
    email = str(email or "").strip().lower()
    if not external_user_id or not email:
        return None

    profile_payload = {
        "id": external_user_id,
        "email": email,
        "full_name": str(full_name or ""),
    }
    _upsert("profiles", profile_payload, "id")
    profile = _one(_select("profiles", filters={"id": external_user_id}, limit=1))
    if not profile:
        raise CloudDatabaseUnavailable("Profilo Supabase non disponibile dopo il login.")

    existing_user = _cloud_user_from_profile(profile)
    if existing_user:
        return existing_user

    account_payload = {
        "name": (account_name or "La mia attivita").strip() or "La mia attivita",
        "plan": str(plan or "free"),
        "billing_status": "dev",
        "owner_user_id": external_user_id,
    }
    account = _insert("accounts", account_payload)
    account_id = int(account["id"])
    _upsert(
        "account_members",
        {"account_id": account_id, "user_id": external_user_id, "role": "owner"},
        "account_id,user_id",
    )
    ensure_default_guardrail_policy(account_id)
    ensure_default_notification_preferences(account_id)
    return _cloud_user_from_profile(profile, account_id)


def create_user(account_id: int, email: str, role: str = "manager", full_name: str = "") -> Dict:
    raise CloudDatabaseUnavailable(
        "Gli inviti utente richiedono Supabase Auth: crea prima l'utente tramite invito email."
    )


def update_user(user_id: int, data: Dict) -> Optional[Dict]:
    profile = _profile_by_local_id(user_id)
    if not profile:
        return None
    profile_payload = {
        key: value for key, value in (data or {}).items()
        if key in {"email", "full_name"}
    }
    if profile_payload:
        _update("profiles", profile_payload, filters={"id": profile["id"]})
    if "role" in (data or {}):
        memberships = _select("account_members", filters={"user_id": profile["id"]}, limit=1)
        membership = _one(memberships)
        if membership:
            _update(
                "account_members",
                {"role": str(data["role"])},
                filters={"account_id": int(membership["account_id"]), "user_id": profile["id"]},
            )
    return get_user(user_id)


def delete_user(user_id: int) -> bool:
    """Rimuove l'accesso PricePilot, senza eliminare l'identita Auth."""
    profile = _profile_by_local_id(user_id)
    if not profile:
        return False
    _delete("account_members", filters={"user_id": profile["id"]})
    return True


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_auth_session(user_id: int, ttl_days: int = 14) -> str:
    profile = _profile_by_local_id(user_id)
    if not profile:
        raise CloudDatabaseUnavailable("Utente cloud non trovato per la sessione.")
    token = secrets.token_urlsafe(48)
    expires_at = datetime.utcnow() + timedelta(days=max(1, int(ttl_days)))
    rows = _data(_session_client().table("app_sessions").insert({
        "user_id": profile["id"],
        "token_hash": _hash_session_token(token),
        "expires_at": expires_at.isoformat(),
    }).execute())
    if not rows:
        raise CloudDatabaseUnavailable("Supabase non ha creato la sessione PricePilot.")
    return token


def get_user_by_auth_session(token: str) -> Optional[Dict]:
    if not token:
        return None
    session = _one(_data(
        _session_client()
        .table("app_sessions")
        .select("*")
        .eq("token_hash", _hash_session_token(token))
        .limit(1)
        .execute()
    ))
    if not session or session.get("revoked_at"):
        return None
    try:
        if datetime.fromisoformat(str(session["expires_at"]).replace("Z", "+00:00")).replace(tzinfo=None) <= datetime.utcnow():
            return None
    except (TypeError, ValueError):
        return None
    profile = _one(_data(
        _session_client()
        .table("profiles")
        .select("*")
        .eq("id", session["user_id"])
        .limit(1)
        .execute()
    ))
    return _cloud_user_from_profile(profile) if profile else None


def revoke_auth_session(token: str) -> None:
    if token:
        _session_client().table("app_sessions").update({
            "revoked_at": datetime.utcnow().isoformat(),
        }).eq("token_hash", _hash_session_token(token)).execute()


def record_user_consent(
    user_id: int,
    account_id: int,
    *,
    terms_accepted: bool,
    privacy_accepted: bool,
    marketing_accepted: bool,
    terms_version: str,
    privacy_version: str,
    source: str = "signup",
    accepted_at: Optional[str] = None,
) -> Dict:
    profile = _profile_by_local_id(user_id)
    if not profile:
        raise CloudDatabaseUnavailable("Utente cloud non trovato per salvare il consenso.")
    row = _upsert("user_consents", {
        "account_id": int(account_id),
        "user_id": profile["id"],
        "terms_accepted": bool(terms_accepted),
        "privacy_accepted": bool(privacy_accepted),
        "marketing_accepted": bool(marketing_accepted),
        "terms_version": terms_version,
        "privacy_version": privacy_version,
        "source": source,
        "accepted_at": accepted_at or datetime.utcnow().isoformat(),
    }, "user_id,terms_version,privacy_version")
    result = _legacy_row(row) or {}
    result["user_id"] = int(user_id)
    return result


def get_latest_user_consent(user_id: int) -> Optional[Dict]:
    profile = _profile_by_local_id(user_id)
    if not profile:
        return None
    row = _one(_select("user_consents", filters={"user_id": profile["id"]}, order=("accepted_at", True), limit=1))
    result = _legacy_row(row)
    if result:
        result["user_id"] = int(user_id)
    return result


def get_account(account_id: int = 1) -> Optional[Dict]:
    return _legacy_row(_one(_select("accounts", filters={"id": int(account_id)}, limit=1)))


def update_account(account_id: int, data: Dict) -> Optional[Dict]:
    allowed = {
        "name", "plan", "billing_status", "trial_ends_at", "current_period_ends_at",
        "stripe_customer_id", "stripe_subscription_id",
    }
    payload = {key: value for key, value in data.items() if key in allowed}
    if not payload:
        return get_account(account_id)
    _update("accounts", payload, filters={"id": int(account_id)})
    return get_account(account_id)


def upsert_property(prop: Dict) -> int:
    payload = _property_payload(prop)
    if prop.get("id"):
        payload["local_id"] = int(prop["id"])
        row = _upsert("properties", payload, "account_id,local_id")
    else:
        row = _insert("properties", payload)
    legacy = _legacy_row(row) or {}
    _ensure_property_pricing_rule(legacy)
    return _id_from_row(row, "properties")


def get_properties() -> list[Dict]:
    return _legacy_rows(_select("properties", order=("local_id", False)))


def get_property(prop_id: int) -> Optional[Dict]:
    return _legacy_row(_one(_select("properties", filters={"local_id": int(prop_id)}, limit=1)))


def delete_property(prop_id: int) -> None:
    prop = get_property(prop_id)
    if not prop:
        return
    account_id = int(prop["account_id"])
    filters = {"account_id": account_id, "property_local_id": int(prop_id)}
    for table in (
        "pricing_rules", "guardrail_policies", "price_calendar", "decision_log",
        "occupancy_history", "market_history", "telegram_links", "telegram_approvals",
        "property_integrations", "notification_preferences", "notification_log",
        "audit_events", "price_updates",
    ):
        _delete(table, filters=filters)
    _delete("properties", filters={"account_id": account_id, "local_id": int(prop_id)})


# ---------------------------------------------------------------------------
# Guardrails, notifications, audit and operation runs
# ---------------------------------------------------------------------------

def ensure_default_guardrail_policy(account_id: int = 1, conn: Any = None) -> int:
    existing = _one(_select("guardrail_policies", filters={"account_id": int(account_id), "property_local_id": 0}, limit=1))
    if existing:
        return _id_from_row(existing, "guardrail_policies") if existing.get("local_id") is not None else 0
    row = _upsert(
        "guardrail_policies",
        {"account_id": int(account_id), "property_local_id": 0, **DEFAULT_GUARDRAIL_POLICY},
        "account_id,property_local_id",
    )
    return _id_from_row(row, "guardrail_policies") if row.get("local_id") is not None else 0


def get_guardrail_policy(account_id: int = 1, property_id: Optional[int] = None) -> Dict:
    account_id = int(account_id)
    if property_id is not None:
        row = _one(_select("guardrail_policies", filters={"account_id": account_id, "property_local_id": int(property_id)}, limit=1))
        if row:
            return _legacy_row(row) or {}
    row = _one(_select("guardrail_policies", filters={"account_id": account_id, "property_local_id": 0}, limit=1))
    if row:
        return _legacy_row(row) or {}
    ensure_default_guardrail_policy(account_id)
    return {**DEFAULT_GUARDRAIL_POLICY, "account_id": account_id, "property_id": 0}


def update_guardrail_policy(account_id: int = 1, property_id: int = 0, data: Dict | None = None) -> Dict:
    existing = get_guardrail_policy(account_id, property_id if property_id else None)
    merged = {**DEFAULT_GUARDRAIL_POLICY, **existing, **(data or {})}
    payload = {
        "account_id": int(account_id),
        "property_local_id": int(property_id or 0),
        "max_change_pct": float(merged["max_change_pct"]),
        "require_approval_pct": float(merged["require_approval_pct"]),
        "min_confidence_auto": float(merged["min_confidence_auto"]),
        "competitor_outlier_pct": float(merged["competitor_outlier_pct"]),
        "max_daily_auto_changes": int(merged["max_daily_auto_changes"]),
        "auto_enabled": bool(merged["auto_enabled"]),
    }
    row = _upsert("guardrail_policies", payload, "account_id,property_local_id")
    return _legacy_row(row) or {}


def count_auto_actions_today(property_id: int, target_date: Optional[str] = None) -> int:
    account_id = _account_from_property(property_id)
    if account_id is None:
        return 0
    target = target_date or date.today().isoformat()
    query = _client().table("decision_log").select("id", count="exact")
    query = query.eq("account_id", account_id).eq("property_local_id", int(property_id))
    query = query.eq("applied", True).like("decision", "AUTO_APPLIED%").eq("date", target)
    response = query.execute()
    return int(getattr(response, "count", 0) or 0)


def record_audit_event(action: str, entity_type: str = "system", entity_id: Optional[Any] = None,
                       account_id: int = 1, property_id: Optional[int] = None,
                       source: str = "system", status: str = "ok", details: Optional[Dict] = None) -> int:
    row = _insert("audit_events", {
        "account_id": int(account_id), "property_local_id": int(property_id) if property_id is not None else None,
        "source": source, "action": action, "entity_type": entity_type,
        "entity_id": "" if entity_id is None else str(entity_id), "status": status,
        "details": details or {},
    })
    return _id_from_row(row, "audit_events")


def get_audit_events(limit: int = 100, account_id: Optional[int] = None, property_id: Optional[int] = None) -> list[Dict]:
    filters: Dict[str, Any] = {}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    if property_id is not None:
        filters["property_local_id"] = int(property_id)
    return _legacy_rows(_select("audit_events", filters=filters, order=("timestamp", True), limit=limit))


def _stale_operation_runs(account_id: int, stale_after_minutes: int) -> Optional[Dict]:
    rows = _select("operation_runs", filters={"account_id": int(account_id), "status": "running"}, order=("started_at", True))
    cutoff = datetime.utcnow() - timedelta(minutes=max(1, int(stale_after_minutes)))
    active: Optional[Dict] = None
    for row in rows:
        raw = str(row.get("started_at") or "")
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            started = datetime.utcnow()
        if started >= cutoff and active is None:
            active = row
        else:
            _update("operation_runs", {"status": "stale", "finished_at": datetime.utcnow().isoformat(),
                                       "error": "Ciclo rimasto running oltre la soglia: marcato come stale."},
                    filters={"account_id": int(account_id), "local_id": int(row["local_id"])})
    return _legacy_row(active)


def start_operation_run(account_id: int = 1, source: str = "scheduler", next_run_at: Optional[str] = None) -> int:
    row = _insert("operation_runs", {"account_id": int(account_id), "source": source,
                                      "status": "running", "next_run_at": next_run_at})
    return _id_from_row(row, "operation_runs")


def get_active_operation_run(account_id: int = 1, stale_after_minutes: int = 120) -> Optional[Dict]:
    return _stale_operation_runs(int(account_id), stale_after_minutes)


def try_start_operation_run(account_id: int = 1, source: str = "scheduler", next_run_at: Optional[str] = None,
                            stale_after_minutes: int = 120) -> tuple[Optional[int], Optional[Dict]]:
    active = _stale_operation_runs(int(account_id), stale_after_minutes)
    if active:
        return None, active
    return start_operation_run(int(account_id), source, next_run_at), None


def finish_operation_run(run_id: int, status: str, decisions_count: int = 0, summary: Optional[Dict] = None,
                         error: str = "", next_run_at: Optional[str] = None) -> Optional[Dict]:
    existing = get_operation_run(run_id)
    if not existing:
        return None
    _update("operation_runs", {
        "status": status, "finished_at": datetime.utcnow().isoformat(),
        "decisions_count": int(decisions_count), "summary": summary or {},
        "error": error, "next_run_at": next_run_at,
    }, filters={"account_id": int(existing["account_id"]), "local_id": int(run_id)})
    return get_operation_run(run_id)


def get_operation_run(run_id: int) -> Optional[Dict]:
    return _legacy_row(_one(_select("operation_runs", filters={"local_id": int(run_id)}, limit=1)))


def get_operation_runs(limit: int = 50, account_id: Optional[int] = None) -> list[Dict]:
    filters = {"account_id": int(account_id)} if account_id is not None else {}
    return _legacy_rows(_select("operation_runs", filters=filters, order=("started_at", True), limit=limit))


def get_last_operation_run(account_id: int = 1) -> Optional[Dict]:
    rows = get_operation_runs(1, account_id)
    return rows[0] if rows else None


def ensure_default_notification_preferences(account_id: int = 1, conn: Any = None) -> int:
    row = _one(_select("notification_preferences", filters={"account_id": int(account_id), "property_local_id": 0}, limit=1))
    if row:
        return _id_from_row(row, "notification_preferences") if row.get("local_id") is not None else 0
    row = _upsert("notification_preferences", {
        "account_id": int(account_id), "property_local_id": 0, "telegram_enabled": True,
        "quiet_hours_start": "", "quiet_hours_end": "", "daily_digest": True,
        "approval_alerts": True, "auto_reports": True,
    }, "account_id,property_local_id")
    return _id_from_row(row, "notification_preferences") if row.get("local_id") is not None else 0


def get_notification_preferences(account_id: int = 1, property_id: Optional[int] = None) -> Dict:
    if property_id is not None:
        row = _one(_select("notification_preferences", filters={"account_id": int(account_id), "property_local_id": int(property_id)}, limit=1))
        if row:
            return _legacy_row(row) or {}
    row = _one(_select("notification_preferences", filters={"account_id": int(account_id), "property_local_id": 0}, limit=1))
    if row:
        return _legacy_row(row) or {}
    ensure_default_notification_preferences(account_id)
    return {"account_id": int(account_id), "property_id": property_id or 0, "telegram_enabled": 1,
            "daily_digest": 1, "approval_alerts": 1, "auto_reports": 1}


def update_notification_preferences(account_id: int = 1, property_id: int = 0, data: Dict | None = None) -> Dict:
    existing = get_notification_preferences(account_id, property_id if property_id else None)
    merged = {**existing, **(data or {})}
    row = _upsert("notification_preferences", {
        "account_id": int(account_id), "property_local_id": int(property_id or 0),
        "telegram_enabled": bool(merged.get("telegram_enabled", 1)),
        "quiet_hours_start": str(merged.get("quiet_hours_start") or ""),
        "quiet_hours_end": str(merged.get("quiet_hours_end") or ""),
        "daily_digest": bool(merged.get("daily_digest", 1)),
        "approval_alerts": bool(merged.get("approval_alerts", 1)),
        "auto_reports": bool(merged.get("auto_reports", 1)),
    }, "account_id,property_local_id")
    return _legacy_row(row) or {}


def record_notification_log(event_type: str, status: str, account_id: int = 1, property_id: Optional[int] = None,
                            channel: str = "telegram", recipient: str = "", message_id: str = "", error: str = "",
                            payload: Optional[Dict] = None) -> int:
    row = _insert("notification_log", {"account_id": int(account_id), "property_local_id": property_id,
                                        "channel": channel, "event_type": event_type, "recipient": recipient,
                                        "status": status, "message_id": str(message_id or ""), "error": error,
                                        "payload": payload or {}})
    return _id_from_row(row, "notification_log")


def get_notification_log(limit: int = 100, account_id: Optional[int] = None, property_id: Optional[int] = None) -> list[Dict]:
    filters: Dict[str, Any] = {}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    if property_id is not None:
        filters["property_local_id"] = int(property_id)
    return _legacy_rows(_select("notification_log", filters=filters, order=("timestamp", True), limit=limit))


# ---------------------------------------------------------------------------
# Pricing state, market history and decision history
# ---------------------------------------------------------------------------

def save_decision_log(entry: Dict) -> int:
    payload = {
        "account_id": int(entry.get("account_id") or 1), "property_local_id": int(entry.get("property_id") or 1),
        "timestamp": entry.get("timestamp") or datetime.utcnow().isoformat(), "date": entry.get("date"),
        "old_price": float(entry["old_price"]), "new_price": float(entry["new_price"]),
        "market_avg": entry.get("market_avg"), "occupancy": entry.get("occupancy"),
        "decision": str(entry.get("decision") or ""), "mode": str(entry.get("mode") or "advisory"),
        "applied": bool(entry.get("applied", False)), "notes": str(entry.get("notes") or ""),
        "competitor_avg": entry.get("competitor_avg"), "strategy": entry.get("strategy"),
        "factors": _json_value(entry.get("factors")), "mpi": entry.get("mpi"),
        "current_price_source": str(entry.get("current_price_source") or "manual"),
        "data_source": str(entry.get("data_source") or "demo"),
    }
    row = _insert("decision_log", payload)
    return _id_from_row(row, "decision_log")


def get_decision_log(limit: int = 200, property_id: Optional[int] = None, account_id: Optional[int] = None) -> list[Dict]:
    filters: Dict[str, Any] = {}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    if property_id is not None:
        filters["property_local_id"] = int(property_id)
    return _legacy_rows(_select("decision_log", filters=filters, order=("timestamp", True), limit=limit))


def get_decision_log_entry(log_id: int, account_id: Optional[int] = None) -> Optional[Dict]:
    filters: Dict[str, Any] = {"local_id": int(log_id)}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    return _legacy_row(_one(_select("decision_log", filters=filters, limit=1)))


def update_decision_state(log_id: int, *, account_id: int, applied: bool, decision: str) -> Optional[Dict]:
    _update("decision_log", {"applied": bool(applied), "decision": decision},
            filters={"account_id": int(account_id), "local_id": int(log_id)})
    return get_decision_log_entry(log_id, account_id)


def mark_decision_rejected(log_id: int, account_id: int) -> Optional[Dict]:
    row = get_decision_log_entry(log_id, account_id)
    if not row:
        return None
    decision = str(row.get("decision") or "")
    if "[REJECTED]" not in decision:
        decision = f"{decision} [REJECTED]".strip()
    return update_decision_state(log_id, account_id=int(account_id), applied=False, decision=decision)


def save_occupancy(property_id: int, date_str: str, occupancy: float, source: str = "manual", account_id: int = 1) -> None:
    _upsert("occupancy_history", {"account_id": int(account_id), "property_local_id": int(property_id),
                                   "date": date_str, "occupancy": float(occupancy), "source": source},
            "account_id,property_local_id,date")


def get_occupancy_history(property_id: int = 1, limit: int = 90, account_id: Optional[int] = None) -> list[Dict]:
    filters = {"property_local_id": int(property_id)}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    return _legacy_rows(_select("occupancy_history", filters=filters, order=("date", True), limit=limit))


def save_market_history(entry: Dict) -> None:
    payload = {"account_id": int(entry.get("account_id") or 1), "property_local_id": int(entry.get("property_id") or 1),
               "date": entry["date"], "market_avg": entry.get("market_avg"), "market_min": entry.get("market_min"),
               "market_max": entry.get("market_max"), "market_std": entry.get("market_std"),
               "competitor_count": entry.get("competitor_count"), "source": entry.get("source", "demo"),
               "recorded_at": entry.get("recorded_at") or datetime.utcnow().isoformat()}
    _insert("market_history", payload)


def get_market_history(property_id: int = 1, limit: int = 90, account_id: Optional[int] = None) -> list[Dict]:
    filters = {"property_local_id": int(property_id)}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    return _legacy_rows(_select("market_history", filters=filters, order=("date", True), limit=limit))


def get_calendar_price(property_id: int, date_str: str, account_id: Optional[int] = None) -> Optional[Dict]:
    filters: Dict[str, Any] = {"property_local_id": int(property_id), "date": date_str}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    return _legacy_row(_one(_select("price_calendar", filters=filters, order=("updated_at", True), limit=1)))


def get_price_calendar(account_id: Optional[int] = None, property_id: Optional[int] = None,
                       date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 180) -> list[Dict]:
    filters: Dict[str, Any] = {}
    if account_id is not None:
        filters["account_id"] = int(account_id)
    query, _ = _query("price_calendar", filters=filters)
    if property_id is not None:
        query = query.eq("property_local_id", int(property_id))
    if date_from:
        query = query.gte("date", date_from)
    if date_to:
        query = query.lte("date", date_to)
    return _legacy_rows(_data(query.order("date").limit(int(limit)).execute()))


def upsert_calendar_price(entry: Dict) -> Dict:
    payload = {
        "account_id": int(entry.get("account_id") or 1), "property_local_id": int(entry["property_id"]),
        "date": entry["date"], "current_price": float(entry["current_price"]),
        "current_price_source": entry.get("current_price_source", "manual"),
        "recommended_price": entry.get("recommended_price"), "status": entry.get("status", "current"),
        "decision_log_local_id": entry.get("decision_log_id"), "applied_price": entry.get("applied_price"),
        "notes": entry.get("notes", ""),
    }
    row = _upsert("price_calendar", payload, "account_id,property_local_id,date")
    return _legacy_row(row) or {}


def get_current_price_for_date(prop: Dict, date_str: str) -> tuple[float, str]:
    row = get_calendar_price(int(prop.get("id") or 1), date_str, int(prop.get("account_id") or 1))
    if row and row.get("current_price") is not None:
        if str(row.get("status") or "").lower() == "locked":
            return float(row["current_price"]), "manual_lock"
        return float(row["current_price"]), str(row.get("current_price_source") or "calendar")
    current = prop.get("current_price")
    if current is not None:
        return float(current), "property"
    return (float(prop.get("min_price") or 50) + float(prop.get("max_price") or 500)) / 2, "midpoint_fallback"


def save_price_recommendation(
    account_id: int,
    property_id: int,
    date_str: str,
    current_price: float,
    recommended_price: float,
    status: str,
    decision_log_id: Optional[int] = None,
    notes: str = "",
    current_price_source: str = "manual",
) -> Dict:
    """Mantiene la firma del repository SQLite per il calendario cloud."""
    return upsert_calendar_price({
        "account_id": account_id,
        "property_id": property_id,
        "date": date_str,
        "current_price": current_price,
        "current_price_source": current_price_source,
        "recommended_price": recommended_price,
        "status": status,
        "decision_log_id": decision_log_id,
        "notes": notes,
    })


def update_calendar_status_for_decision(
    decision_log_id: int,
    status: str,
    applied_price: Optional[float] = None,
    notes: Optional[str] = None,
) -> Optional[Dict]:
    decision = get_decision_log_entry(decision_log_id)
    if not decision:
        return None
    filters = {"account_id": int(decision["account_id"]), "property_local_id": int(decision["property_id"]),
               "decision_log_local_id": int(decision_log_id)}
    rows = _update("price_calendar", {
        "status": status,
        "applied_price": applied_price,
        **({"notes": notes} if notes is not None else {}),
    }, filters=filters)
    return _legacy_row(rows[0]) if rows else None


def update_decision_tg_message(log_id: int, tg_message_id: int) -> None:
    decision = get_decision_log_entry(log_id)
    if decision:
        _update("decision_log", {"tg_message_id": str(tg_message_id)},
                filters={"account_id": int(decision["account_id"]), "local_id": int(log_id)})


# ---------------------------------------------------------------------------
# Telegram, integrations and price sync history
# ---------------------------------------------------------------------------

def save_telegram_link(entry: Dict) -> int:
    payload = {
        "account_id": int(entry.get("account_id") or _account_from_property(int(entry["property_id"])) or 1),
        "property_local_id": int(entry["property_id"]), "token": str(entry["token"]),
        "chat_id": entry.get("chat_id"), "telegram_username": str(entry.get("telegram_username") or ""),
        "active": bool(entry.get("active", True)),
    }
    if entry.get("id"):
        payload["local_id"] = int(entry["id"])
        row = _upsert("telegram_links", payload, "account_id,local_id")
    else:
        existing = _one(_select("telegram_links", filters={"account_id": payload["account_id"], "token": payload["token"]}, limit=1))
        if existing:
            payload["local_id"] = int(existing["local_id"])
            row = _upsert("telegram_links", payload, "account_id,local_id")
        else:
            row = _insert("telegram_links", payload)
    return _id_from_row(row, "telegram_links")


def get_telegram_link_by_token(token: str) -> Optional[Dict]:
    return _legacy_row(_one(_select("telegram_links", filters={"token": token, "active": True}, order=("created_at", True), limit=1)))


def get_telegram_link_by_property(property_id: int) -> Optional[Dict]:
    return _legacy_row(_one(_select("telegram_links", filters={"property_local_id": int(property_id), "active": True}, order=("created_at", True), limit=1)))


def revoke_telegram_link(property_id: int) -> None:
    account_id = _account_from_property(int(property_id))
    if account_id is not None:
        _update("telegram_links", {"active": False}, filters={"account_id": account_id, "property_local_id": int(property_id)})


def get_all_telegram_links() -> list[Dict]:
    return _legacy_rows(_select("telegram_links", order=("created_at", True)))


def get_telegram_decision_context(log_id: int, chat_id: int) -> Optional[Dict]:
    decision = get_decision_log_entry(log_id)
    if not decision:
        return None
    link = _one(_select("telegram_links", filters={"account_id": int(decision["account_id"]),
                                                   "property_local_id": int(decision["property_id"]),
                                                   "chat_id": int(chat_id), "active": True},
                       order=("created_at", True), limit=1))
    if not link:
        return None
    return {"id": int(log_id), "account_id": int(decision["account_id"]),
            "property_id": int(decision["property_id"]), "telegram_link_id": int(link["local_id"]),
            "telegram_username": str(link.get("telegram_username") or "")}


def record_telegram_approval(entry: Dict) -> int:
    account_id = int(entry.get("account_id") or 1)
    row = _insert("telegram_approvals", {
        "account_id": account_id, "property_local_id": entry.get("property_id"),
        "decision_log_local_id": int(entry["decision_log_id"]), "telegram_link_local_id": entry.get("telegram_link_id"),
        "chat_id": entry.get("chat_id"), "telegram_username": str(entry.get("telegram_username") or ""),
        "action": str(entry["action"]), "status": str(entry["status"]), "source": str(entry.get("source") or "telegram"),
        "message_id": str(entry.get("message_id") or ""), "callback_query_id": str(entry.get("callback_query_id") or ""),
        "error": str(entry.get("error") or ""), "payload": _json_value(entry.get("payload")),
        "timestamp": entry.get("timestamp") or datetime.utcnow().isoformat(),
    })
    return _id_from_row(row, "telegram_approvals")


def get_telegram_approvals(limit: int = 100, account_id: Optional[int] = None, property_id: Optional[int] = None,
                           decision_log_id: Optional[int] = None) -> list[Dict]:
    filters: Dict[str, Any] = {}
    if account_id is not None: filters["account_id"] = int(account_id)
    if property_id is not None: filters["property_local_id"] = int(property_id)
    if decision_log_id is not None: filters["decision_log_local_id"] = int(decision_log_id)
    return _legacy_rows(_select("telegram_approvals", filters=filters, order=("timestamp", True), limit=limit))


def get_pending_approvals(
    property_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> list[Dict]:
    rows = get_decision_log(limit=500, property_id=property_id, account_id=account_id)
    return [row for row in rows if str(row.get("mode")) == "approval" and not int(row.get("applied") or 0)
            and "[REJECTED]" not in str(row.get("decision") or "") and "[APPROVED" not in str(row.get("decision") or "")]


def get_property_integrations(property_id: int) -> list[Dict]:
    return _legacy_rows(_select("property_integrations", filters={"property_local_id": int(property_id)}, order=("local_id", False)))


def upsert_property_integration(entry: Dict) -> int:
    account_id = int(entry.get("account_id") or _account_from_property(int(entry["property_id"])) or 1)
    payload = {"account_id": account_id, "property_local_id": int(entry["property_id"]),
               "platform": str(entry["platform"]), "listing_url": str(entry.get("listing_url") or ""),
               "listing_id": str(entry.get("listing_id") or ""), "is_primary": bool(entry.get("is_primary", False))}
    if entry.get("id"):
        payload["local_id"] = int(entry["id"])
        row = _upsert("property_integrations", payload, "account_id,local_id")
    else:
        row = _upsert("property_integrations", payload, "account_id,property_local_id,platform")
    return _id_from_row(row, "property_integrations")


def delete_property_integration(integration_id: int) -> None:
    row = _one(_select("property_integrations", filters={"local_id": int(integration_id)}, limit=1))
    if row:
        _delete("property_integrations", filters={"account_id": int(row["account_id"]), "local_id": int(integration_id)})


def record_price_update(prop: Dict, result: Dict, target_date: date) -> int:
    row = _insert("price_updates", {
        "account_id": int(prop.get("account_id") or 1), "property_local_id": int(prop["id"]),
        "platform": str(result.get("platform") or prop.get("platform") or ""),
        "listing_id": str(result.get("listing_id") or prop.get("listing_id") or ""),
        "target_date": target_date.isoformat(), "new_price": result.get("new_price"),
        "ok": bool(result.get("ok")), "error": str(result.get("error") or ""),
        "applied_at": result.get("applied_at") or datetime.utcnow().isoformat(),
        "is_stub": bool((result.get("raw") or {}).get("stub", False)),
    })
    return _id_from_row(row, "price_updates")


def get_price_updates(property_ids: list[int], limit: int = 500) -> list[Dict]:
    if not property_ids:
        return []
    query, _ = _query("price_updates")
    query = query.in_("property_local_id", [int(item) for item in property_ids])
    return _legacy_rows(_data(query.order("applied_at", desc=True).limit(int(limit)).execute()))


# ---------------------------------------------------------------------------
# Legacy analytics tables kept cloud-backed during the final migration.
# ---------------------------------------------------------------------------

def save_decision(decision: Dict[str, Any]) -> int:
    payload = {"account_id": int(decision.get("account_id") or 1), "timestamp": decision.get("timestamp") or datetime.utcnow().isoformat(),
               "date": decision["date"], "property_id": str(decision.get("property_id") or "default"),
               "old_price": decision["old_price"], "new_price": decision["new_price"], "pct_change": decision["pct_change"],
               "competitor_price": decision.get("competitor_price"), "market_price": decision.get("market_price"),
               "competitor_count": decision.get("competitor_count"), "competitor_min": decision.get("competitor_min"),
               "competitor_max": decision.get("competitor_max"), "occupancy": decision.get("occupancy"),
               "event": decision.get("event", ""), "strategy": decision.get("strategy", "balanced"),
               "decision": decision.get("decision", ""), "applied": bool(decision.get("applied", False))}
    row = _insert("pricing_decisions", payload)
    return _id_from_row(row, "pricing_decisions")


def get_decisions(limit: int = 200, date_from: Optional[str] = None, date_to: Optional[str] = None,
                  account_id: Optional[int] = None) -> list[Dict]:
    filters = {"account_id": int(account_id)} if account_id is not None else {}
    query, _ = _query("pricing_decisions", filters=filters)
    if date_from: query = query.gte("date", date_from)
    if date_to: query = query.lte("date", date_to)
    return _legacy_rows(_data(query.order("timestamp", desc=True).limit(int(limit)).execute()))


def save_competitors(competitors: list[Dict]) -> None:
    if not competitors: return
    now = datetime.utcnow().isoformat()
    rows = [{"account_id": int(item.get("account_id") or 1), "timestamp": now, "date": item["date"],
             "source": item.get("source", "unknown"), "property_name": item.get("property_name", ""),
             "price": item["price"], "occupancy_rate": item.get("occupancy_rate"), "rating": item.get("rating"),
             "num_reviews": item.get("num_reviews")} for item in competitors]
    for row in rows:
        _insert("competitors", row)


def get_competitors(target_date: str) -> list[Dict]:
    return _legacy_rows(_select("competitors", filters={"date": target_date}, order=("price", False)))


def upsert_event(evt: Dict) -> None:
    payload = {"account_id": int(evt.get("account_id") or 1), "date": evt["date"], "name": evt["name"],
               "event_type": evt.get("event_type", "generic"), "impact_level": evt.get("impact_level", "medium"),
               "description": evt.get("description", "")}
    _upsert("events", payload, "account_id,date,name")


def get_events(date_from: Optional[str] = None, date_to: Optional[str] = None) -> list[Dict]:
    query, _ = _query("events")
    if date_from: query = query.gte("date", date_from)
    if date_to: query = query.lte("date", date_to)
    return _legacy_rows(_data(query.order("date").execute()))


def save_market_snapshot(snap: Dict) -> None:
    _insert("market_snapshots", {"account_id": int(snap.get("account_id") or 1), "timestamp": datetime.utcnow().isoformat(),
                                  "date": snap["date"], "market_avg": snap.get("market_avg"), "market_min": snap.get("market_min"),
                                  "market_max": snap.get("market_max"), "competitor_count": snap.get("competitor_count"),
                                  "our_price": snap.get("our_price"), "position": snap.get("position", "")})


def get_market_snapshots(limit: int = 90) -> list[Dict]:
    return _legacy_rows(_select("market_snapshots", order=("date", True), limit=limit))


def get_summary_stats(account_id: Optional[int] = None) -> Dict[str, Any]:
    rows = get_decisions(limit=10_000, account_id=account_id)
    prices = [float(row["new_price"]) for row in rows if row.get("new_price") is not None]
    changes = [abs(float(row["pct_change"])) for row in rows if row.get("pct_change") is not None]
    return {"total_decisions": len(rows), "avg_price": round(sum(prices) / len(prices), 2) if prices else 0,
            "last_decision": rows[0] if rows else None,
            "avg_change": round(sum(changes) / len(changes), 2) if changes else 0}


_HANDLERS: Dict[str, Callable[..., Any]] = {
    name: value for name, value in globals().items()
    if callable(value) and not name.startswith("_") and name not in {"Callable", "Dict", "Iterable", "Optional", "Any"}
}


def dispatch(name: str, *args: Any, **kwargs: Any) -> Any:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise CloudDatabaseUnavailable(f"Operazione cloud non implementata: {name}.")
    return handler(*args, **kwargs)
